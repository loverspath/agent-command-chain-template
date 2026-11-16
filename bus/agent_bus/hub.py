"""Durable Agent Bus Hub (Internal Tailnet WS + Shared Core for Dual-Auth Gateway + I4 Auth Sync)."""

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
import urllib.parse
import uuid
from typing import Any, Callable, Coroutine

import websockets

from .auth import (
    AuthResult,
    AuthSyncError,
    ReplicaApplyResult,
    allowed_nodes,
    apply_replica_manifest,
    authenticate_ws_peer,
    build_auth_sync_manifest,
    check_external_send_permission,
    is_loopback_ip,
    load_tokens,
    load_tombstones,
    read_replica_current_generation,
    read_replica_last_sync_at,
    sync_source_auth_state,
)
from .config import (
    ALIASES,
    BUS_RULES,
    DEFAULT_BANNER_TIMEOUT,
    DEFAULT_DIAL_TIMEOUT,
    DEFAULT_HELLO_TIMEOUT,
    DEFAULT_PRUNE_INTERVAL,
    HUB_CAPS,
    LEASE_TTL,
    MAX_HOP,
    MAX_TEXT,
    QUEUE_TTL,
    RATE_PER_MIN,
    cfg_dir,
    db_path,
    get_auth_max_stale,
    get_auth_source,
    get_auth_sync_interval,
    get_auth_watch_interval,
    get_banner_timeout,
    get_bus_host,
    get_bus_port,
    get_dial_timeout,
    get_gateway_host,
    get_gateway_port,
    get_hello_timeout,
    get_hub_peers,
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
    token_id: str | None = None


def resolve_target(to: str | None) -> str:
    raw = (to or "*").strip().lower()
    if raw == "*":
        return "*"
    node, sep, label = raw.partition("/")
    resolved_node = ALIASES.get(node, node)
    return f"{resolved_node}/{label}" if sep and label else resolved_node


def sysmsg(text: str, **kw: Any) -> str:
    return json.dumps({"op": "sys", "from": "relay", "text": text, **kw}, ensure_ascii=False)


def _normalize_ws_source_url(raw_url: str) -> str:
    clean = raw_url.strip()
    low = clean.lower()
    if low.startswith("http://"):
        return "ws://" + clean[7:]
    if low.startswith("https://"):
        return "wss://" + clean[8:]
    return clean


class BusHub:
    """Core message router, SQLite WAL mailbox, rate limiter, audit logger, and I4 auth sync."""

    def __init__(
        self,
        *,
        config_dir: pathlib.Path | None = None,
        database_path: pathlib.Path | None = None,
        logs_dir: pathlib.Path | None = None,
        prune_interval: float | None = None,
        auth_source: str | None = None,
        hub_peers: Any = None,
        auth_sync_interval: float | None = None,
        auth_max_stale: float | None = None,
        auth_watch_interval: float | None = None,
        bind_host: str | None = None,
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

        cfg = load_config(self.cfg_dir)
        self.auth_source: str = get_auth_source(cfg, config_dir=self.cfg_dir, explicit=auth_source)
        self.auth_mode: str = "replica" if self.auth_source else "source"
        self._explicit_hub_peers: Any = hub_peers
        self.auth_sync_interval: float = get_auth_sync_interval(
            explicit=auth_sync_interval,
            cfg=cfg,
            config_dir=self.cfg_dir,
        )
        self.auth_max_stale: float = get_auth_max_stale(
            explicit=auth_max_stale,
            cfg=cfg,
            config_dir=self.cfg_dir,
        )
        self.auth_watch_interval: float = get_auth_watch_interval(
            explicit=auth_watch_interval,
            cfg=cfg,
            config_dir=self.cfg_dir,
        )
        self.bind_host: str = str(bind_host or "").strip()

        self.auth_generation: int = 0
        self._enforced_auth_generation: int = 0
        self.auth_synced_at: float | None = None
        self._auth_synced_mono: float | None = None
        self._last_recorded_synced_wall: float | None = None
        self._auth_task: asyncio.Task[None] | None = None
        self._auth_sync_lock = asyncio.Lock()
        self.auth_sync_runs: int = 0
        self.auth_sync_errors: int = 0
        self.last_auth_sync_error: str = ""

        if self.auth_mode == "source":
            st = sync_source_auth_state(self.cfg_dir, explicit_auth_source="")
            self.auth_generation = st.generation
            self._enforced_auth_generation = st.generation
            self._record_auth_sync_timestamp(time.time())
        else:
            gen = read_replica_current_generation(self.cfg_dir)
            self.auth_generation = gen
            self._enforced_auth_generation = gen
            last_ts = read_replica_last_sync_at(self.cfg_dir)
            if gen > 0 and last_ts is not None:
                self._record_auth_sync_timestamp(last_ts)

        self.store = BusStore(self.db_path)
        self.peers: dict[str, PeerConn] = {}
        self.rate: dict[str, list[float]] = collections.defaultdict(list)
        # Seed SQLite sequence & unexpired messages from legacy messages.jsonl on first upgrade.
        self.store.import_jsonl_if_empty(self.logs_dir / "messages.jsonl", ttl=QUEUE_TTL)

    @property
    def hub_id(self) -> str:
        return self.store.hub_id

    @property
    def is_replica(self) -> bool:
        return self.auth_mode == "replica"

    def _record_auth_sync_timestamp(self, wall_ts: float) -> None:
        ts_f = float(wall_ts)
        elapsed = max(0.0, time.time() - ts_f)
        self.auth_synced_at = ts_f
        self._last_recorded_synced_wall = ts_f
        self._auth_synced_mono = time.monotonic() - elapsed

    def get_hub_peers(self) -> set[str]:
        return get_hub_peers(config_dir=self.cfg_dir, explicit=self._explicit_hub_peers)

    def current_auth_age_s(self, now: float | None = None) -> float | None:
        if self.auth_synced_at is None:
            return None
        if now is not None:
            return round(max(0.0, float(now) - self.auth_synced_at), 3)
        if (
            self._auth_synced_mono is not None
            and self._last_recorded_synced_wall == self.auth_synced_at
        ):
            return round(max(0.0, time.monotonic() - self._auth_synced_mono), 3)
        return round(max(0.0, time.time() - self.auth_synced_at), 3)

    def is_auth_stale(self, now: float | None = None) -> bool:
        """Return True on a replica when unsynced or elapsed age exceeds `auth_max_stale`."""
        if self.auth_mode != "replica":
            return False
        if self.auth_generation <= 0:
            return True
        age = self.current_auth_age_s(now=now)
        if age is None:
            return True
        return age > self.auth_max_stale

    def refresh_source_auth(self) -> bool:
        """Re-evaluate local `allow.txt`/`tokens.json` hashes on a source hub.

        Returns True when the local auth generation changed since last observed by this hub.
        """
        if self.auth_mode != "source":
            return False
        try:
            st = sync_source_auth_state(self.cfg_dir, explicit_auth_source="")
        except Exception as exc:
            print(
                f"[auth-source-error] cfg_dir={self.cfg_dir} err={type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return False
        self.auth_generation = st.generation
        self._record_auth_sync_timestamp(time.time())
        return st.generation != self._enforced_auth_generation or st.changed

    def is_peer_session_valid(
        self,
        peer: PeerConn,
        *,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Verify whether an active WS session remains authorized under the current generation."""
        if peer.trust == "internal":
            if self.auth_mode == "replica" and self.auth_generation <= 0:
                return False, "auth replica not synced"
            allowed = allowed_nodes(self.cfg_dir, explicit_auth_source=self.auth_source)
            if peer.node not in allowed:
                return False, "node removed from allow.txt"
            return True, ""

        # External (`ext`) token session
        if self.auth_mode == "replica":
            if self.auth_generation <= 0:
                return False, "auth replica not synced"
            if self.is_auth_stale(now=now):
                return False, "auth replica stale"
        if not peer.token_id:
            return False, "missing token_id"
        tombstones = load_tombstones(self.cfg_dir, explicit_auth_source=self.auth_source)
        if peer.token_id in tombstones:
            return False, "token revoked"
        active_tokens = load_tokens(
            self.cfg_dir,
            explicit_auth_source=self.auth_source,
            now=now,
        )
        if peer.token_id not in active_tokens:
            return False, "token disabled or expired"
        return True, ""

    async def enforce_active_sessions(self, *, now: float | None = None) -> list[str]:
        """Immediately evict any open WS sessions invalidated by generation update or staleness (`4003`)."""
        self._enforced_auth_generation = self.auth_generation
        to_evict: list[tuple[PeerConn, str]] = []
        for k, peer in list(self.peers.items()):
            ok, reason = self.is_peer_session_valid(peer, now=now)
            if not ok:
                if self.peers.get(k) is peer:
                    del self.peers[k]
                to_evict.append((peer, reason))
        if not to_evict:
            return []

        async def _evict_one(p: PeerConn, rsn: str) -> None:
            tok_part = f" token_id={p.token_id}" if p.token_id else ""
            print(
                f"[auth-evict] peer={p.key} trust={p.trust}{tok_part} code=4003 reason={rsn}",
                file=sys.stderr,
                flush=True,
            )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(p.close_conn(4003, rsn), timeout=2.0)

        await asyncio.gather(*(_evict_one(p, r) for p, r in to_evict), return_exceptions=True)
        return [p.key for p, _ in to_evict]

    async def handle_auth_sync_request(self, auth: AuthResult) -> dict[str, Any]:
        """Serve `op: 'auth_sync'` strictly to `internal` peers listed in `hub_peers`."""
        if self.auth_mode != "source":
            print(
                f"[auth-sync-reject] node={auth.node} trust={auth.trust} reason=not_source_hub",
                file=sys.stderr,
                flush=True,
            )
            return {
                "op": "auth_sync",
                "ok": False,
                "error": "forbidden",
                "code": 403,
                "detail": "hub is not configured as auth source",
            }
        allowed_peers = self.get_hub_peers()
        if auth.trust != "internal" or not auth.node or auth.node not in allowed_peers:
            print(
                f"[auth-sync-reject] node={auth.node} trust={auth.trust} reason=not_in_hub_peers",
                file=sys.stderr,
                flush=True,
            )
            return {
                "op": "auth_sync",
                "ok": False,
                "error": "forbidden",
                "code": 403,
                "detail": "peer is not authorized in hub_peers",
            }

        if self.refresh_source_auth():
            await self.enforce_active_sessions()
        manifest = build_auth_sync_manifest(self.cfg_dir, explicit_auth_source="")
        self.auth_generation = int(manifest["generation"])
        self._record_auth_sync_timestamp(time.time())
        return {
            "op": "auth_sync",
            "ok": True,
            **manifest,
        }

    def _resolve_auth_sync_local_addr(self, ws_url: str) -> tuple[str, int] | None:
        explicit_bind = (os.environ.get("BUS_AUTH_SYNC_BIND_HOST") or "").strip()
        if explicit_bind:
            return (explicit_bind, 0)
        if self.bind_host and is_loopback_ip(self.bind_host) and self.bind_host != "127.0.0.1":
            parsed = urllib.parse.urlsplit(ws_url)
            target_host = parsed.hostname or ""
            if is_loopback_ip(target_host):
                return (self.bind_host, 0)
        return None

    async def sync_auth_once(self) -> ReplicaApplyResult:
        """Pull `op: 'auth_sync'` from `auth_source`, atomically swap generation, and evict revoked peers."""
        if self.auth_mode != "replica" or not self.auth_source:
            raise AuthSyncError("sync_auth_once called on non-replica hub")

        async with self._auth_sync_lock:
            ws_url = _normalize_ws_source_url(self.auth_source)
            cfg = load_config(self.cfg_dir)
            dial_t = get_dial_timeout(cfg=cfg) if cfg else DEFAULT_DIAL_TIMEOUT
            hello_t = get_hello_timeout(cfg=cfg) if cfg else DEFAULT_HELLO_TIMEOUT
            banner_t = get_banner_timeout(cfg=cfg) if cfg else DEFAULT_BANNER_TIMEOUT

            connect_kw: dict[str, Any] = {
                "open_timeout": dial_t,
                "close_timeout": 2.0,
                "max_size": 4 << 20,
            }
            local_addr = self._resolve_auth_sync_local_addr(ws_url)
            if local_addr is not None:
                connect_kw["local_addr"] = local_addr

            ws = await asyncio.wait_for(websockets.connect(ws_url, **connect_kw), timeout=dial_t + 1.0)
            try:
                hello = {
                    "v": 2,
                    "label": f"auth-sync-{uuid.uuid4().hex[:6]}",
                    "since": 0,
                    "accept_inbox": False,
                    "no_replay": True,
                }
                await asyncio.wait_for(ws.send(json.dumps(hello)), timeout=hello_t)
                raw_banner = await asyncio.wait_for(ws.recv(), timeout=banner_t)
                banner = json.loads(raw_banner)
                if not isinstance(banner, dict) or banner.get("op") != "sys" or "you" not in banner:
                    raise AuthSyncError("invalid banner from auth_source")

                await asyncio.wait_for(
                    ws.send(json.dumps({"op": "auth_sync"})),
                    timeout=hello_t,
                )
                deadline = time.monotonic() + banner_t
                resp: dict[str, Any] | None = None
                while True:
                    rem = max(0.05, deadline - time.monotonic())
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=rem)
                    parsed = json.loads(raw_msg)
                    if isinstance(parsed, dict) and parsed.get("op") == "auth_sync":
                        resp = parsed
                        break

                if not resp.get("ok"):
                    err_code = str(resp.get("error") or "auth_sync_failed")
                    raise AuthSyncError(f"auth_sync rejected by source: {err_code}")

                apply_res = apply_replica_manifest(self.cfg_dir, resp)
                self.auth_generation = apply_res.generation
                self._record_auth_sync_timestamp(apply_res.synced_at)
                self.auth_sync_runs += 1
                self.last_auth_sync_error = ""
            finally:
                with contextlib.suppress(Exception):
                    await ws.close()

            await self.enforce_active_sessions()
            return apply_res

    def start_auth_task(self, interval: float | None = None) -> asyncio.Task[None]:
        """Start background auth task: replica pull+staleness loop OR source file-change watcher."""
        if self._auth_task is not None and not self._auth_task.done():
            return self._auth_task

        if self.auth_mode == "replica":
            eff_interval = max(0.02, float(interval)) if interval is not None else self.auth_sync_interval

            async def _replica_loop() -> None:
                try:
                    while True:
                        try:
                            await self.sync_auth_once()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            self.auth_sync_errors += 1
                            self.last_auth_sync_error = f"{type(exc).__name__}: {exc}"
                            print(
                                f"[auth-sync-error] source={self.auth_source} err={self.last_auth_sync_error}",
                                file=sys.stderr,
                                flush=True,
                            )
                            await self.enforce_active_sessions()

                        next_sync_mono = time.monotonic() + eff_interval
                        while True:
                            now_m = time.monotonic()
                            rem_sync = next_sync_mono - now_m
                            if rem_sync <= 0:
                                break
                            sleep_dur = rem_sync
                            if (
                                self._auth_synced_mono is not None
                                and not self.is_auth_stale()
                                and any(p.trust == "external" for p in self.peers.values())
                            ):
                                stale_at_mono = self._auth_synced_mono + self.auth_max_stale + 0.005
                                sleep_dur = min(sleep_dur, max(0.01, stale_at_mono - now_m))
                            await asyncio.sleep(sleep_dur)
                            if self.is_auth_stale() and any(
                                p.trust == "external" for p in self.peers.values()
                            ):
                                await self.enforce_active_sessions()
                except asyncio.CancelledError:
                    raise

            self._auth_task = asyncio.create_task(_replica_loop(), name="abus-hub-auth-replica")
            return self._auth_task

        eff_watch = max(0.02, float(interval)) if interval is not None else self.auth_watch_interval

        async def _source_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(eff_watch)
                    if self.refresh_source_auth():
                        await self.enforce_active_sessions()
            except asyncio.CancelledError:
                raise

        self._auth_task = asyncio.create_task(_source_loop(), name="abus-hub-auth-source")
        return self._auth_task

    async def stop_auth_task(self) -> None:
        task = self._auth_task
        self._auth_task = None
        if task is not None:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

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
        if self._auth_task is not None and not self._auth_task.done():
            self._auth_task.cancel()
        self._auth_task = None
        if self._prune_task is not None and not self._prune_task.done():
            self._prune_task.cancel()
        self._prune_task = None
        self.store.close()

    def health_snapshot(self, now: float | None = None) -> dict[str, Any]:
        """Return unauthenticated hub, queue, and auth health metrics (`GET /v1/health`, WS `op='health'`)."""
        if self.auth_mode == "source" and now is None:
            with contextlib.suppress(Exception):
                st = sync_source_auth_state(self.cfg_dir, explicit_auth_source="")
                if st.generation != self.auth_generation:
                    self.auth_generation = st.generation
                    self._record_auth_sync_timestamp(time.time())
        elif self.auth_mode == "replica":
            disk_gen = read_replica_current_generation(self.cfg_dir)
            if disk_gen > self.auth_generation:
                self.auth_generation = disk_gen
                disk_ts = read_replica_last_sync_at(self.cfg_dir)
                if disk_ts is not None:
                    self._record_auth_sync_timestamp(disk_ts)

        wall_now = float(now) if now is not None else time.time()
        uptime_s = round(max(0.0, time.monotonic() - self._started_mono), 3)
        q = self.store.get_queue_metrics(now=wall_now)
        auth_gen = int(self.auth_generation or 0)
        auth_age_s = self.current_auth_age_s(now=wall_now if now is not None else None)
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
            "auth_mode": self.auth_mode,
            "auth_generation": auth_gen,
            "auth_age_s": auth_age_s,
            "auth": {
                "mode": self.auth_mode,
                "generation": auth_gen,
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

            if self.auth_mode == "source" and self.refresh_source_auth():
                await self.enforce_active_sessions()

            auth = await authenticate_ws_peer(
                remote_ip=remote_ip,
                headers=headers,
                hello=hello,
                store=self.store,
                cfg_dir=self.cfg_dir,
                is_gateway=is_gateway,
                ws_path=ws_path,
                explicit_auth_source=self.auth_source,
                auth_stale=self.is_auth_stale(),
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
                token_id=auth.token_id,
            )
            self.peers[key] = peer

            # Guard against a generation/staleness race between `authenticate_ws_peer` and registration.
            ok_reg, reason_reg = self.is_peer_session_valid(peer)
            if not ok_reg:
                if self.peers.get(key) is peer:
                    del self.peers[key]
                await close_conn(4003, reason_reg)
                return

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
                if self.auth_mode == "source" and self.refresh_source_auth():
                    await self.enforce_active_sessions()
                ok_live, reason_live = self.is_peer_session_valid(peer)
                if not ok_live:
                    if self.peers.get(key) is peer:
                        del self.peers[key]
                    await close_conn(4003, reason_live)
                    return

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
                elif op == "auth_sync":
                    sync_resp = await self.handle_auth_sync_request(auth)
                    await send_text(json.dumps(sync_resp, ensure_ascii=False))
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
    auth_source: str | None = None,
    hub_peers: Any = None,
    auth_sync_interval: float | None = None,
    auth_max_stale: float | None = None,
) -> None:
    from .gateway import start_gateway_server

    cfg = load_config(config_dir)
    ws_host = host if host is not None else get_bus_host(cfg)
    ws_port = port if port is not None else get_bus_port(cfg)
    gw_host = gateway_host if gateway_host is not None else get_gateway_host(cfg)
    gw_port = gateway_port if gateway_port is not None else get_gateway_port(cfg)

    hub = BusHub(
        config_dir=config_dir,
        database_path=database_path,
        logs_dir=logs_dir,
        prune_interval=prune_interval,
        auth_source=auth_source,
        hub_peers=hub_peers,
        auth_sync_interval=auth_sync_interval,
        auth_max_stale=auth_max_stale,
        bind_host=ws_host,
    )
    gw_server = None
    try:
        hub.start_prune_task()
        hub.start_auth_task()
        # Yield briefly so replica mode kicks off its initial pull immediately upon startup.
        await asyncio.sleep(0)
        async with websockets.serve(
            lambda ws: _websockets_adapter(ws, hub),
            ws_host,
            ws_port,
            max_size=1 << 20,
            ping_interval=20,
            ping_timeout=60,
        ):
            cur_allow = sorted(allowed_nodes(hub.cfg_dir, explicit_auth_source=hub.auth_source))
            print(
                f"agent-bus hub ws://{ws_host}:{ws_port} hub_id={hub.hub_id} "
                f"auth_mode={hub.auth_mode} auth_gen={hub.auth_generation} "
                f"allow={cur_allow} seq={hub.store.get_max_seq()}",
                flush=True,
            )
            if gw_port and gw_port > 0:
                gw_server = await start_gateway_server(hub, gw_host, gw_port)
                print(f"agent-bus gateway http+ws://{gw_host}:{gw_port}/bus/v1", flush=True)
            await asyncio.Future()
    finally:
        await hub.stop_auth_task()
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
    p.add_argument("--auth-source", default=None, help="Source hub WS URL for replica auth sync")
    p.add_argument("--hub-peers", default=None, help="Comma-separated peer node names allowed for auth_sync")
    p.add_argument("--auth-sync-interval", type=float, default=None)
    p.add_argument("--auth-max-stale", type=float, default=None)
    args = p.parse_args(argv)
    try:
        asyncio.run(
            serve_all(
                host=args.host,
                port=args.port,
                gateway_host=args.gateway_host,
                gateway_port=args.gateway_port,
                auth_source=args.auth_source,
                hub_peers=args.hub_peers,
                auth_sync_interval=args.auth_sync_interval,
                auth_max_stale=args.auth_max_stale,
            )
        )
    except KeyboardInterrupt:
        pass
