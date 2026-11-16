"""Durable Agent Bus Hub (Internal Tailnet WS + Shared Core for Dual-Auth Gateway)."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import json
import math
import os
import pathlib
import sys
import time
import uuid
from typing import Any, Callable, Coroutine

import websockets

from .auth import (
    AuthResult,
    allowed_nodes,
    authenticate_ws_peer,
    check_external_send_permission,
)
from .config import (
    ALIASES,
    BUS_RULES,
    DEFAULT_PRUNE_INTERVAL,
    HUB_CAPS,
    LEASE_TTL,
    MAX_HOP,
    MAX_TEXT,
    QUEUE_TTL,
    RATE_PER_MIN,
    cfg_dir,
    db_path,
    get_bus_host,
    get_bus_port,
    get_gateway_host,
    get_gateway_port,
    load_config,
    log_dir,
)
from .store import VALID_RKINDS, BusStore, IdConflictError


@dataclasses.dataclass
class PeerConn:
    key: str
    node: str
    label: str
    trust: str
    accept_inbox: bool
    send_text: Callable[[str], Coroutine[Any, Any, None]]
    close_conn: Callable[[int, str], Coroutine[Any, Any, None]]


def resolve_target(to: str | None) -> str:
    raw = (to or "*").strip().lower()
    if raw == "*":
        return "*"
    node, sep, label = raw.partition("/")
    resolved_node = ALIASES.get(node, node)
    return f"{resolved_node}/{label}" if sep and label else resolved_node


def sysmsg(text: str, **kw: Any) -> str:
    return json.dumps({"op": "sys", "from": "relay", "text": text, **kw}, ensure_ascii=False)


class BusHub:
    """Core message router, SQLite WAL mailbox, rate limiter, and audit logger."""

    def __init__(
        self,
        *,
        config_dir: pathlib.Path | None = None,
        database_path: pathlib.Path | None = None,
        logs_dir: pathlib.Path | None = None,
        prune_interval: float | None = None,
    ) -> None:
        self.cfg_dir = pathlib.Path(config_dir) if config_dir else cfg_dir()
        self.db_path = pathlib.Path(database_path) if database_path else db_path()
        self.logs_dir = pathlib.Path(logs_dir) if logs_dir else log_dir()
        self.started_at = time.time()
        self._started_mono = time.monotonic()
        self.prune_interval = self._resolve_prune_interval(prune_interval)
        self.prune_runs: int = 0
        self.last_prune_at: float | None = None
        self._prune_task: asyncio.Task[None] | None = None
        # Auth generation / age placeholders (populated in WBS I4).
        self.auth_generation: int | None = None
        self.auth_synced_at: float | None = None

        self.store = BusStore(self.db_path)
        self.peers: dict[str, PeerConn] = {}
        self.rate: dict[str, list[float]] = collections.defaultdict(list)
        # Seed SQLite sequence & unexpired messages from legacy messages.jsonl on first upgrade.
        self.store.import_jsonl_if_empty(self.logs_dir / "messages.jsonl", ttl=QUEUE_TTL)

    @property
    def hub_id(self) -> str:
        return self.store.hub_id

    @staticmethod
    def _resolve_prune_interval(explicit: float | None) -> float:
        if explicit is not None:
            return max(0.01, float(explicit))
        raw = os.environ.get("BUS_PRUNE_INTERVAL_SEC")
        if raw is not None and raw.strip() != "":
            return max(0.01, float(raw.strip()))
        return float(DEFAULT_PRUNE_INTERVAL)

    def start_prune_task(self, interval: float | None = None) -> asyncio.Task[None]:
        """Start periodic background task invoking `store.prune_expired()` (default 600s)."""
        if self._prune_task is not None and not self._prune_task.done():
            return self._prune_task
        eff_interval = max(0.01, float(interval)) if interval is not None else self.prune_interval

        async def _prune_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(eff_interval)
                    self.store.prune_expired()
                    self.prune_runs += 1
                    self.last_prune_at = time.time()
            except asyncio.CancelledError:
                raise

        self._prune_task = asyncio.create_task(_prune_loop(), name="abus-hub-prune")
        return self._prune_task

    async def stop_prune_task(self) -> None:
        task = self._prune_task
        self._prune_task = None
        if task is not None:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def close(self) -> None:
        if self._prune_task is not None and not self._prune_task.done():
            self._prune_task.cancel()
        self._prune_task = None
        self.store.close()

    def health_snapshot(self, now: float | None = None) -> dict[str, Any]:
        """Return unauthenticated hub & queue health metrics (`GET /v1/health`, WS `op='health'`)."""
        wall_now = float(now) if now is not None else time.time()
        uptime_s = round(max(0.0, time.monotonic() - self._started_mono), 3)
        q = self.store.get_queue_metrics(now=wall_now)
        auth_age_s = (
            round(max(0.0, wall_now - self.auth_synced_at), 3)
            if self.auth_synced_at is not None
            else None
        )
        return {
            "ok": True,
            "hub_id": self.hub_id,
            "seq": q["max_seq"],
            "max_seq": q["max_seq"],
            "pending": q["pending"],
            "leased": q["leased"],
            "oldest_pending_age": q["oldest_pending_age"],
            "dead": q["dead"],
            "queue": {
                "pending": q["pending"],
                "leased": q["leased"],
                "oldest_pending_age": q["oldest_pending_age"],
                "dead": q["dead"],
            },
            "wall_clock": wall_now,
            "uptime_s": uptime_s,
            "uptime": uptime_s,
            "auth_generation": self.auth_generation,
            "auth_age_s": auth_age_s,
            "auth": {
                "generation": self.auth_generation,
                "age_s": auth_age_s,
            },
        }

    def append_audit_log(self, msg: dict[str, Any]) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        # Keep internal messages in messages.jsonl in exact bus-sync.py compatible schema.
        target_file = (
            self.logs_dir / "messages.jsonl"
            if msg.get("trust", "internal") == "internal"
            else self.logs_dir / "external-messages.jsonl"
        )
        with open(target_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def matches_peer(self, msg: dict[str, Any], peer: PeerConn) -> bool:
        to = msg["to"]
        if peer.trust == "external":
            if (msg["sender"] if "sender" in msg else msg.get("from")) == peer.key:
                return False
            if to in (peer.node, peer.key):
                return True
            corr_id = msg.get("corr_id")
            if corr_id:
                orig_node = self.store.get_message_sender_node(str(corr_id))
                if orig_node is not None:
                    return orig_node == peer.node
                rk = msg.get("rkind")
                is_result = (rk == "result") or (rk is None and msg.get("id") != corr_id)
                if (
                    is_result
                    and msg.get("fnode") != peer.node
                    and (to == peer.node or to.startswith(f"{peer.node}/"))
                ):
                    return True
            return False

        if to == "*":
            return msg["fnode"] != peer.node
        if to in (peer.node, peer.key):
            return True

        # Route <node>/inbox or <node>/<session> events to <node>/main (inbox bridge)
        # if no dedicated <node>/<label> peer is currently connected.
        if peer.accept_inbox and peer.label == "main":
            t_node, sep, t_label = to.partition("/")
            if (
                sep
                and t_node == peer.node
                and t_label
                and not t_label.startswith("cli-")
                and to not in self.peers
                and (t_label == "inbox" or msg.get("kind") == "event")
            ):
                return True
        return False

    def _effective_rate_per_min(self) -> int:
        raw = os.environ.get("BUS_TEST_RATE_PER_MIN")
        if raw is not None and raw.strip() != "":
            return int(raw.strip())
        return RATE_PER_MIN

    def _check_rate_and_limits(
        self,
        *,
        node: str,
        text: str,
        hop: int,
    ) -> str | None:
        now = time.time()
        window = [t for t in self.rate[node] if now - t < 60.0]
        self.rate[node] = window
        if len(window) >= self._effective_rate_per_min():
            return "rate"
        if len(text) > MAX_TEXT:
            return "size"
        if hop > MAX_HOP:
            return "hop"
        return None

    async def publish_message(
        self,
        *,
        auth: AuthResult,
        sender_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate, persist, audit-log, and fan out a message."""
        raw_to = payload.get("to")
        target = resolve_target(str(raw_to) if raw_to is not None else "*")
        kind = str(payload.get("kind") or "task").strip().lower()
        if kind not in ("rpc", "task", "event", "ack", "legacy"):
            kind = "task"
        status = str(payload.get("status") or "ok").strip().lower()
        text = str(payload.get("text", ""))
        hop = int(payload.get("hop", 0) or 0)
        ttl = float(payload.get("ttl") or QUEUE_TTL)
        corr_id = payload.get("corr_id")
        if corr_id is not None:
            corr_id = str(corr_id).strip() or None
        mid = str(payload.get("id") or uuid.uuid4().hex[:12])

        raw_rkind = payload.get("rkind")
        rkind: str | None = None
        if raw_rkind is not None:
            cand_rk = str(raw_rkind).strip().lower()
            if cand_rk in VALID_RKINDS:
                rkind = cand_rk

        raw_deadline = payload.get("deadline")
        deadline: float | None = None
        if raw_deadline is not None:
            try:
                dl_val = float(raw_deadline)
            except (TypeError, ValueError):
                return {"ok": False, "error": "invalid_deadline", "detail": "invalid deadline", "code": 400}
            if not math.isfinite(dl_val) or dl_val <= 0:
                return {"ok": False, "error": "invalid_deadline", "detail": "invalid deadline", "code": 400}
            deadline = dl_val

        if auth.trust == "external":
            ok, reason = check_external_send_permission(auth, target, kind, text=text)
            if not ok:
                return {"ok": False, "error": "forbidden", "detail": reason, "code": 403}

        err = self._check_rate_and_limits(node=auth.node, text=text, hop=hop)
        if err == "rate":
            return {"ok": False, "error": "rate", "detail": "rate limit exceeded", "code": 429}
        if err == "size":
            return {"ok": False, "error": "size", "detail": "text too large", "code": 413}
        if err == "hop":
            return {
                "ok": False,
                "error": "hop",
                "detail": f"dropped: hop {hop} > {MAX_HOP} (loop guard)",
                "code": 400,
            }

        try:
            msg, is_dup = self.store.enqueue_message(
                mid=mid,
                sender=sender_key,
                fnode=auth.node,
                target=target,
                text=text,
                hop=hop,
                kind=kind,
                rkind=rkind,
                status=status,
                corr_id=corr_id,
                ttl=ttl,
                deadline=deadline,
                trust=auth.trust,
            )
        except IdConflictError as exc:
            print(
                f"[id-conflict] node={auth.node} sender={sender_key} id={mid}",
                file=sys.stderr,
                flush=True,
            )
            return {
                "ok": False,
                "error": "id_conflict",
                "detail": str(exc),
                "code": 409,
            }
        if is_dup:
            return {
                "ok": True,
                "duplicate": True,
                "hub_id": self.hub_id,
                "ack": mid,
                "seq": msg["seq"],
                "corr_id": msg.get("corr_id"),
                "delivered": [],
            }

        self.rate[auth.node].append(time.time())
        self.append_audit_log(msg)

        out = json.dumps(msg, ensure_ascii=False)
        delivered: list[str] = []
        for k, peer in list(self.peers.items()):
            if k == sender_key:
                continue
            if self.matches_peer(msg, peer):
                try:
                    await peer.send_text(out)
                    self.store.lease_live_delivery(msg["seq"], k, lease_ttl=LEASE_TTL)
                    delivered.append(k)
                except Exception:
                    pass

        return {
            "ok": True,
            "duplicate": False,
            "hub_id": self.hub_id,
            "ack": mid,
            "seq": msg["seq"],
            "corr_id": msg.get("corr_id"),
            "delivered": delivered,
            "peer_online": bool(delivered),
        }

    async def handle_progress(
        self,
        *,
        auth: AuthResult,
        sender_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        corr_id = str(payload.get("corr_id") or "").strip()
        if not corr_id:
            return {"ok": False, "error": "missing corr_id"}

        # Progress frames must never forge terminal RPC statuses (ok/exec_error/auth_error).
        status = "running"
        text = str(payload.get("text") or "")
        to_target = resolve_target(payload.get("to"))

        if auth.trust == "external":
            routing = self.store.get_message_routing(corr_id)
            if routing is None:
                return {"ok": False, "error": "forbidden", "detail": "unknown corr_id for external progress"}
            orig_fnode, orig_target = routing
            if orig_target not in (auth.node, sender_key):
                return {"ok": False, "error": "forbidden", "detail": "external peer does not own target of corr_id"}
            if to_target != orig_fnode and not to_target.startswith(f"{orig_fnode}/"):
                ok, reason = check_external_send_permission(auth, to_target, "event", text=text)
                if not ok:
                    return {"ok": False, "error": "forbidden", "detail": reason}

        self.store.record_progress(corr_id=corr_id, sender=sender_key, status=status, text=text)

        frame = json.dumps(
            {
                "op": "progress",
                "corr_id": corr_id,
                "from": sender_key,
                "status": status,
                "text": text,
                "ts": time.time(),
            },
            ensure_ascii=False,
        )
        for k, peer in list(self.peers.items()):
            if k == sender_key:
                continue
            if to_target in (peer.node, peer.key) or (
                peer.trust == "external"
                and (
                    to_target == peer.node
                    or to_target.startswith(f"{peer.node}/")
                    or self.store.get_message_sender_node(corr_id) == peer.node
                )
            ):
                try:
                    await peer.send_text(frame)
                except Exception:
                    pass
        return {"ok": True}

    def visible_peers(self, auth: AuthResult) -> list[str]:
        if auth.trust == "internal":
            return sorted(self.peers.keys())
        # External tokens only see peers they are allowed to target or themselves.
        out: list[str] = []
        for k in sorted(self.peers.keys()):
            node = k.split("/", 1)[0]
            if node == auth.node:
                out.append(k)
                continue
            ok, _ = check_external_send_permission(auth, k, "event")
            ok_rpc, _ = check_external_send_permission(auth, k, "rpc")
            if ok or ok_rpc:
                out.append(k)
        return out

    def query_rpc_result(self, auth: AuthResult, corr_id: str) -> dict[str, Any]:
        if auth.trust == "external":
            if not self.store.can_external_access_rpc(corr_id, auth.node):
                return {"ok": False, "error": "forbidden", "code": 403}
        res = self.store.get_rpc_result(corr_id)
        if res is None:
            return {"ok": True, "found": False, "corr_id": corr_id, "code": 200}
        return {"ok": True, "found": True, **res, "code": 200}

    async def handle_ws_session(
        self,
        *,
        remote_ip: str,
        headers: dict[str, str] | None,
        recv_text: Callable[[], Coroutine[Any, Any, str]],
        send_text: Callable[[str], Coroutine[Any, Any, None]],
        close_conn: Callable[[int, str], Coroutine[Any, Any, None]],
        is_gateway: bool = False,
        ws_path: str = "/bus/v1/ws",
    ) -> None:
        key: str | None = None
        peer: PeerConn | None = None
        surface = "gw-ws" if is_gateway else "ws"
        try:
            try:
                raw_hello = await asyncio.wait_for(recv_text(), 5.0)
            except asyncio.TimeoutError:
                print(
                    f"[auth-reject] surface={surface} ip={remote_ip} code=4001 reason=hello_timeout",
                    file=sys.stderr,
                    flush=True,
                )
                await close_conn(4001, "hello timeout")
                return
            try:
                hello = json.loads(raw_hello)
            except Exception as exc:
                print(
                    f"[auth-reject] surface={surface} ip={remote_ip} code=4001 reason=invalid_hello_json:{type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                await close_conn(4001, "invalid hello")
                return
            if not isinstance(hello, dict):
                print(
                    f"[auth-reject] surface={surface} ip={remote_ip} code=4001 reason=hello_not_object",
                    file=sys.stderr,
                    flush=True,
                )
                await close_conn(4001, "invalid hello")
                return

            auth = await authenticate_ws_peer(
                remote_ip=remote_ip,
                headers=headers,
                hello=hello,
                store=self.store,
                cfg_dir=self.cfg_dir,
                is_gateway=is_gateway,
                ws_path=ws_path,
            )
            if not auth.ok:
                who_part = f" whois={auth.node}" if auth.node else ""
                tok_part = f" token_id={auth.token_id}" if auth.token_id else ""
                reason = auth.error or "node not allowed"
                print(
                    f"[auth-reject] surface={surface} ip={remote_ip}{who_part}{tok_part} code=4003 reason={reason}",
                    file=sys.stderr,
                    flush=True,
                )
                await close_conn(4003, reason)
                return

            raw_label = str(hello.get("label") or hello.get("name") or "main")
            label = "".join(c for c in raw_label if c.isalnum() or c in "-_")[:24] or "main"
            key = f"{auth.node}/{label}"
            since = int(hello.get("since", 0) or 0)
            hello_hub_id = str(hello.get("hub_id") or "").strip()
            if hello_hub_id and hello_hub_id != self.hub_id:
                # Hub DB was replaced/reinitialized with a new hub_id (H10): replay from 0.
                since = 0
            accept_inbox = bool(hello.get("accept_inbox", label == "main"))
            raw_no_replay = hello.get("no_replay")
            if raw_no_replay is not None:
                skip_replay = bool(raw_no_replay)
            else:
                skip_replay = (not accept_inbox) and label.startswith(("cli-", "doc-"))

            if key in self.peers:
                try:
                    await self.peers[key].close_conn(4002, "replaced")
                except Exception:
                    pass

            peer = PeerConn(
                key=key,
                node=auth.node,
                label=label,
                trust=auth.trust,
                accept_inbox=accept_inbox,
                send_text=send_text,
                close_conn=close_conn,
            )
            self.peers[key] = peer

            max_seq = self.store.get_max_seq()
            banner_kw: dict[str, Any] = {
                "you": key,
                "peers": self.visible_peers(auth),
                "seq": max_seq,
                "trust": auth.trust,
                "hub_id": self.hub_id,
                "caps": list(HUB_CAPS),
            }
            if auth.trust == "internal":
                banner_kw["rules"] = BUS_RULES
            await send_text(sysmsg(f"connected as {key}; peers={banner_kw['peers']}", **banner_kw))

            # Replay undelivered/pending messages with lease protection (unless suppressed).
            if not skip_replay:
                pending = self.store.poll_for_consumer(
                    node=auth.node,
                    consumer=key,
                    since=since,
                    limit=500,
                    trust=auth.trust,
                    accept_inbox=(accept_inbox and label == "main"),
                    lease_ttl=LEASE_TTL,
                    reclaim_on_reconnect=True,
                )
                for m in pending:
                    await send_text(json.dumps(m, ensure_ascii=False))

            while True:
                raw = await recv_text()
                m = json.loads(raw)
                if not isinstance(m, dict):
                    continue
                op = str(m.get("op") or "send")
                if op == "peers":
                    plist = self.visible_peers(auth)
                    await send_text(sysmsg("peers=" + ",".join(plist), peers=plist))
                elif op == "health":
                    await send_text(
                        json.dumps({"op": "health", **self.health_snapshot()}, ensure_ascii=False)
                    )
                elif op == "ack":
                    seqs: list[int] = []
                    if isinstance(m.get("seqs"), list):
                        seqs.extend(int(s) for s in m["seqs"])
                    if m.get("seq") is not None:
                        seqs.append(int(m["seq"]))
                    self.store.ack_deliveries(key, seqs)
                elif op == "nack":
                    if m.get("seq") is not None:
                        self.store.nack_delivery(key, int(m["seq"]))
                elif op == "progress":
                    prog_res = await self.handle_progress(auth=auth, sender_key=key, payload=m)
                    if not prog_res.get("ok"):
                        await send_text(sysmsg(prog_res.get("detail") or prog_res.get("error", "forbidden"), error="forbidden"))
                elif op in ("rpc_result", "result"):
                    cid = str(m.get("corr_id") or "").strip()
                    if cid:
                        res = self.query_rpc_result(auth, cid)
                        await send_text(json.dumps({"op": "rpc_result", **res}, ensure_ascii=False))
                elif op == "send":
                    res = await self.publish_message(auth=auth, sender_key=key, payload=m)
                    if not res.get("ok"):
                        err_kw: dict[str, Any] = {"error": res["error"]}
                        if m.get("id") is not None:
                            err_kw["id"] = str(m["id"])
                        await send_text(sysmsg(res["detail"], **err_kw))
                    else:
                        dl = res["delivered"] or "queued"
                        await send_text(
                            sysmsg(
                                f"ack seq={res['seq']} delivered={dl}",
                                ack=res["ack"],
                                seq=res["seq"],
                                corr_id=res.get("corr_id"),
                                delivered=res["delivered"],
                                duplicate=res.get("duplicate", False),
                            )
                        )
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as exc:
            if key is None:
                print(
                    f"[handshake-error] surface={surface} ip={remote_ip} err={type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        finally:
            if key and self.peers.get(key) is peer:
                del self.peers[key]


async def _websockets_adapter(ws: Any, hub: BusHub) -> None:
    ip = (ws.remote_address or ("",))[0]
    req = getattr(ws, "request", None)
    headers = dict(req.headers) if req is not None and getattr(req, "headers", None) else {}

    async def recv_text() -> str:
        msg = await ws.recv()
        return msg.decode("utf-8") if isinstance(msg, bytes) else str(msg)

    async def send_text(txt: str) -> None:
        await ws.send(txt)

    async def close_conn(code: int, reason: str) -> None:
        await ws.close(code, reason)

    await hub.handle_ws_session(
        remote_ip=ip,
        headers=headers,
        recv_text=recv_text,
        send_text=send_text,
        close_conn=close_conn,
        is_gateway=False,
        ws_path="/",
    )


async def serve_all(
    *,
    host: str | None = None,
    port: int | None = None,
    gateway_host: str | None = None,
    gateway_port: int | None = None,
    config_dir: pathlib.Path | None = None,
    database_path: pathlib.Path | None = None,
    logs_dir: pathlib.Path | None = None,
    prune_interval: float | None = None,
) -> None:
    from .gateway import start_gateway_server

    cfg = load_config()
    ws_host = host if host is not None else get_bus_host(cfg)
    ws_port = port if port is not None else get_bus_port(cfg)
    gw_host = gateway_host if gateway_host is not None else get_gateway_host(cfg)
    gw_port = gateway_port if gateway_port is not None else get_gateway_port(cfg)

    hub = BusHub(
        config_dir=config_dir,
        database_path=database_path,
        logs_dir=logs_dir,
        prune_interval=prune_interval,
    )
    gw_server = None
    try:
        hub.start_prune_task()
        async with websockets.serve(
            lambda ws: _websockets_adapter(ws, hub),
            ws_host,
            ws_port,
            max_size=1 << 20,
            ping_interval=20,
            ping_timeout=60,
        ):
            print(
                f"agent-bus hub ws://{ws_host}:{ws_port} hub_id={hub.hub_id} allow={sorted(allowed_nodes(hub.cfg_dir))} seq={hub.store.get_max_seq()}",
                flush=True,
            )
            if gw_port and gw_port > 0:
                gw_server = await start_gateway_server(hub, gw_host, gw_port)
                print(f"agent-bus gateway http+ws://{gw_host}:{gw_port}/bus/v1", flush=True)
            await asyncio.Future()
    finally:
        await hub.stop_prune_task()
        if gw_server is not None:
            gw_server.close()
            await gw_server.wait_closed()
        hub.close()


def main(argv: list[str] | None = None) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="abus hub", description="Run the Agent Bus Hub + Dual-Auth Gateway")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--gateway-host", default=None)
    p.add_argument("--gateway-port", type=int, default=None, help="Set 0 to disable gateway")
    args = p.parse_args(argv)
    try:
        asyncio.run(
            serve_all(
                host=args.host,
                port=args.port,
                gateway_host=args.gateway_host,
                gateway_port=args.gateway_port,
            )
        )
    except KeyboardInterrupt:
        pass
