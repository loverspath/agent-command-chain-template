"""Agent Bus Node Client: Sync RPC, Durable Journal Outbox/Inbox, ACC Bridge, Supervisor, and Doctor."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import pathlib
import random
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

import websockets

from .auth import build_hmac_headers
from .bridge import ack_inbox, deliver_to_inbox, read_inbox
from .config import (
    FRAME,
    MAX_HOP,
    cfg_dir,
    get_auth_credentials,
    get_bus_url,
    home_dir,
    load_config,
    mode_file_path,
    save_config,
)
from .journal import (
    NodeJournal,
    compute_deadline,
    new_message_id,
    register_worker_session,
)

for _st in (sys.stdout, sys.stderr):  # Preserve UTF-8 output under Windows cp949 redirects.
    try:
        _st.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_AUTH_ERR_PATTERNS = (
    "failed to authenticate",
    "login reached its time limit",
    "not logged in",
    "oauth token",
    "authentication_error",
    "please run `claude /login`",
)

_TERMINAL_ERROR_STATUSES = frozenset({"exec_error", "exec_uncertain", "expired"})

# Optional chaos-test fault injection callback `(stage: str, row: dict[str, Any]) -> None`.
_TEST_CRASH_HOOK: Callable[[str, dict[str, Any]], None] | None = None


def is_http_url(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def to_ws_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme in ("ws", "wss"):
        return url
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/")
    if path.endswith("/v1/ws"):
        ws_path = path
    elif path.endswith("/bus"):
        ws_path = path + "/v1/ws"
    elif path:
        ws_path = path + "/bus/v1/ws"
    else:
        ws_path = "/bus/v1/ws"
    return urllib.parse.urlunsplit((ws_scheme, parsed.netloc, ws_path, parsed.query, ""))


def to_http_endpoint(url: str, route: str) -> str:
    """Resolve e.g. route='/v1/send' against either https://host/bus or http://host:8766."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme in ("ws", "wss"):
        http_scheme = "https" if parsed.scheme == "wss" else "http"
    else:
        http_scheme = parsed.scheme or "http"
    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/v1/ws"):
        base_path = base_path[:-6]
    if base_path.endswith("/bus"):
        full_path = base_path + route
    elif base_path:
        full_path = base_path + "/bus" + route
    else:
        full_path = "/bus" + route
    return urllib.parse.urlunsplit((http_scheme, parsed.netloc, full_path, "", ""))


def http_request_json(
    url: str,
    route: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any]]:
    endpoint = to_http_endpoint(url, route)
    raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    headers: dict[str, str] = {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    token_id, secret = get_auth_credentials()
    if token_id and secret:
        headers.update(build_hmac_headers(token_id, secret, raw_body))

    req = urllib.request.Request(
        endpoint,
        data=raw_body if method.upper() == "POST" else None,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        data = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(data)
        except Exception:
            parsed = {"ok": False, "error": data or str(exc)}
        return exc.code, parsed


def build_ws_hello(label: str, since: int, accept_inbox: bool = True) -> dict[str, Any]:
    hello: dict[str, Any] = {
        "v": 2,
        "label": label,
        "since": since,
        "accept_inbox": accept_inbox,
    }
    token_id, secret = get_auth_credentials()
    if token_id and secret:
        hdrs = build_hmac_headers(token_id, secret, token_id.encode("utf-8"))
        hello["auth"] = {
            "token_id": token_id,
            "ts": hdrs["X-Bus-Timestamp"],
            "nonce": hdrs["X-Bus-Nonce"],
            "sig": hdrs["X-Bus-Signature"],
        }
    return hello


def get_mode(override: str | None = None) -> str:
    """agent: reply to all / watch: log only / hybrid (default): '!ask' only. Re-read per message."""
    if override:
        return override
    try:
        v = mode_file_path().read_text(encoding="utf-8").strip().lower()
    except Exception:
        v = ""
    return v if v in ("agent", "watch", "hybrid") else "hybrid"


def should_reply(mode: str, text: str) -> bool:
    return mode == "agent" or (mode == "hybrid" and text.lstrip().startswith("!ask"))


def strip_ask(text: str) -> str:
    t = text.lstrip()
    return t[4:].lstrip() if t.startswith("!ask") else text


def cursor_path(label: str, config_dir: pathlib.Path | None = None) -> pathlib.Path:
    base = config_dir or cfg_dir()
    return base / f"cursor-{label}"


def load_cursor(label: str, config_dir: pathlib.Path | None = None) -> int:
    try:
        return int(cursor_path(label, config_dir).read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def save_cursor(label: str, seq: int, config_dir: pathlib.Path | None = None) -> int:
    """Persist legacy `cursor-<label>` monotonically (`max(existing, seq)`) via atomic replace."""
    base = config_dir or cfg_dir()
    base.mkdir(parents=True, exist_ok=True)
    p = cursor_path(label, base)
    cur = load_cursor(label, base)
    new_seq = max(cur, int(seq))
    tmp = base / f".cursor-{label}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(str(new_seq), encoding="utf-8")
    os.replace(tmp, p)
    return new_seq


def outbox_path(config_dir: pathlib.Path | None = None) -> pathlib.Path:
    """Legacy outbox.jsonl path (migrated to `<cfg_dir>/journal.db` on first open)."""
    return (config_dir or cfg_dir()) / "outbox.jsonl"


def append_outbox(payload: dict[str, Any], config_dir: pathlib.Path | None = None) -> dict[str, Any]:
    """Durably persist an outbound payload into `<cfg_dir>/journal.db` `outbox`."""
    with NodeJournal(config_dir) as j:
        return j.enqueue_outbox(payload)


def load_outbox(config_dir: pathlib.Path | None = None) -> list[dict[str, Any]]:
    """Return all unexpired `pending` outbox payloads from `<cfg_dir>/journal.db`."""
    with NodeJournal(config_dir) as j:
        return j.list_outbox_pending()


def remove_outbox_id(mid: str, config_dir: pathlib.Path | None = None) -> None:
    """Mark `mid` as `done` in `<cfg_dir>/journal.db` `outbox`."""
    with NodeJournal(config_dir) as j:
        j.mark_outbox_acked(str(mid), "default")


def detect_claude_status(returncode: int, stdout: str, stderr: str) -> str:
    combined = f"{stdout}\n{stderr}".lower()
    if any(pat in combined for pat in _AUTH_ERR_PATTERNS):
        return "auth_error"
    if returncode != 0:
        return "exec_error"
    return "ok"


def ask_claude(text: str, frm: str, cwd: pathlib.Path | None = None) -> tuple[str, str]:
    cmd = os.environ.get("BUS_CLAUDE_CMD") or shutil.which("claude") or "claude"
    work_dir = str(cwd or home_dir())
    allowed = os.environ.get("BUS_CLAUDE_ALLOWED_TOOLS") or "Read,Grep,Glob,Bash,Edit,Write,WebFetch,mcp__*"
    claude_args = [cmd, "-p"]
    if allowed and allowed.strip() != "*":
        claude_args.extend(["--allowedTools", allowed])
    extra_args = os.environ.get("BUS_CLAUDE_EXTRA_ARGS")
    if extra_args:
        claude_args.extend(shlex.split(extra_args))
    r = subprocess.run(
        claude_args,
        input=FRAME.format(frm=frm, text=text),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        cwd=work_dir,
    )
    out = (r.stdout or r.stderr or "").strip()[:8000]
    status = detect_claude_status(r.returncode, r.stdout or "", r.stderr or "")
    if status != "ok" and not out:
        out = f"[{status}] claude exited with code {r.returncode}"
    return out, status


def _git_repo_root() -> str:
    here = pathlib.Path(__file__).resolve().parent
    try:
        r = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return str(here)


def update_repo() -> bool:
    """Run git pull --ff-only on repo root. Returns True if HEAD changed."""
    repo = _git_repo_root()

    def head() -> str:
        return subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    old = head()
    try:
        subprocess.run(
            ["git", "-C", repo, "pull", "--ff-only", "-q"],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception as exc:
        print("[update] pull error", exc, flush=True)
        return False
    new = head()
    return bool(old and new and old != new)


def restart_self() -> None:
    """Restart cleanly without leaking log file handles on Windows watchdogs."""
    print("[update] new commit pulled, restarting", flush=True)
    if os.name == "nt":
        code = 42 if os.environ.get("BUS_SUPERVISOR") == "1" else 0
        sys.exit(code)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _is_inbox_delivery(msg: dict[str, Any], my_label: str) -> tuple[bool, str]:
    """Determine whether an incoming message targets inbox/ACC bridge rather than ask_claude."""
    to = str(msg.get("to") or "").lower()
    kind = str(msg.get("kind") or "task").lower()
    trust = str(msg.get("trust") or "internal").lower()
    _, sep, to_label = to.partition("/")
    if trust == "external" and kind == "event":
        return True, (to_label if sep and to_label else "inbox")
    if sep and to_label == "inbox":
        return True, "inbox"
    if sep and to_label and to_label != my_label and not to_label.startswith("cli-"):
        return True, to_label
    if kind == "event" and not str(msg.get("text") or "").lstrip().startswith("!ask"):
        return True, (to_label if sep and to_label else "inbox")
    return False, ""


async def _flush_outbox_over_ws(
    ws: Any,
    config_dir: pathlib.Path,
    ack_events: dict[str, asyncio.Event],
    *,
    hub_key: str | None = None,
) -> int:
    """Flush pending `journal.db` outbox items over `ws` (backward-compatible signature)."""
    eff_hub = str(hub_key or "default")
    flushed = 0
    with NodeJournal(config_dir) as j:
        pending = j.list_outbox_pending(hub_key=hub_key)
        for item in pending:
            mid = str(item.get("id") or "")
            ev = asyncio.Event()
            if mid:
                ack_events[mid] = ev
            try:
                await ws.send(json.dumps(item, ensure_ascii=False))
                if mid:
                    await asyncio.wait_for(ev.wait(), 5.0)
                    j.mark_outbox_acked(mid, eff_hub)
                    flushed += 1
            except Exception:
                break
            finally:
                if mid:
                    ack_events.pop(mid, None)
    return flushed


async def drain_outbox(
    *,
    config_dir: pathlib.Path | None = None,
    url: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Republish all `pending` outbox entries to the hub until `pending == 0` or `timeout`."""
    cdir = pathlib.Path(config_dir).expanduser() if config_dir is not None else cfg_dir()
    target_url = url or get_bus_url()
    deadline_mono = time.monotonic() + max(0.1, float(timeout))
    drained = 0

    with NodeJournal(cdir) as journal:
        expired_total = journal.expire_outbox()
        initial_pending = len(journal.list_outbox_pending(expire_now=False))
        if initial_pending == 0:
            return {
                "ok": True,
                "drained": 0,
                "expired": expired_total,
                "remaining": 0,
            }

        if is_http_url(target_url):
            while time.monotonic() < deadline_mono:
                expired_total += journal.expire_outbox()
                pending = journal.list_outbox_pending(hub_key=target_url, expire_now=False)
                if not pending:
                    break
                rate_limited = False
                for item in pending:
                    if time.monotonic() >= deadline_mono:
                        break
                    mid = str(item["id"])
                    http_payload = dict(item)
                    http_payload.setdefault("label", f"cli-drain-{uuid.uuid4().hex[:6]}")
                    req_timeout = max(0.2, min(10.0, deadline_mono - time.monotonic()))
                    try:
                        code, data = await asyncio.to_thread(
                            http_request_json,
                            target_url,
                            "/v1/send",
                            method="POST",
                            payload=http_payload,
                            timeout=req_timeout,
                        )
                    except Exception:
                        rate_limited = True
                        break
                    if code == 200 and data.get("ok"):
                        journal.mark_outbox_acked(mid, target_url)
                        drained += 1
                    elif code == 429 or data.get("error") == "rate":
                        rate_limited = True
                        break
                    elif code in (400, 403, 413):
                        journal.mark_outbox_expired(mid)
                        expired_total += 1
                    else:
                        rate_limited = True
                        break
                if rate_limited and time.monotonic() < deadline_mono:
                    await asyncio.sleep(min(0.25, max(0.02, deadline_mono - time.monotonic())))
        else:
            ws_url = to_ws_url(target_url)
            backoff = 0.2
            while time.monotonic() < deadline_mono:
                expired_total += journal.expire_outbox()
                if not journal.list_outbox_pending(expire_now=False):
                    break
                rem_open = max(0.2, min(5.0, deadline_mono - time.monotonic()))
                cli_label = f"cli-drain-{uuid.uuid4().hex[:6]}"
                try:
                    async with websockets.connect(
                        ws_url,
                        open_timeout=rem_open,
                        ping_interval=20,
                        ping_timeout=60,
                    ) as ws:
                        await ws.send(json.dumps(build_ws_hello(cli_label, 10**9, accept_inbox=False)))
                        banner_raw = await asyncio.wait_for(
                            ws.recv(),
                            timeout=max(0.2, min(5.0, deadline_mono - time.monotonic())),
                        )
                        banner = json.loads(banner_raw)
                        hub_key = str(banner.get("hub_id") or ws_url)
                        backoff = 0.2

                        while time.monotonic() < deadline_mono:
                            expired_total += journal.expire_outbox()
                            pending = journal.list_outbox_pending(hub_key=hub_key, expire_now=False)
                            if not pending:
                                break
                            hit_rate = False
                            for item in pending:
                                if time.monotonic() >= deadline_mono:
                                    break
                                mid = str(item["id"])
                                await ws.send(json.dumps(item, ensure_ascii=False))
                                while True:
                                    rem_recv = max(0.1, min(5.0, deadline_mono - time.monotonic()))
                                    raw_frame = await asyncio.wait_for(ws.recv(), timeout=rem_recv)
                                    frame = json.loads(raw_frame)
                                    if frame.get("op") != "sys":
                                        continue
                                    if frame.get("ack") == mid and not frame.get("error"):
                                        journal.mark_outbox_acked(mid, hub_key)
                                        drained += 1
                                        break
                                    err = frame.get("error")
                                    if err == "rate":
                                        hit_rate = True
                                        break
                                    if err in ("forbidden", "size", "hop"):
                                        journal.mark_outbox_expired(mid)
                                        expired_total += 1
                                        break
                                    if err:
                                        hit_rate = True
                                        break
                                if hit_rate:
                                    break
                            if hit_rate and time.monotonic() < deadline_mono:
                                await asyncio.sleep(min(0.25, max(0.02, deadline_mono - time.monotonic())))
                except Exception:
                    if time.monotonic() < deadline_mono:
                        await asyncio.sleep(min(backoff, max(0.02, deadline_mono - time.monotonic())))
                        backoff = min(backoff * 2, 2.0)

        expired_total += journal.expire_outbox()
        remaining = len(journal.list_outbox_pending(expire_now=False))
        return {
            "ok": remaining == 0,
            "drained": drained,
            "expired": expired_total,
            "remaining": remaining,
        }


async def run_node(a: argparse.Namespace) -> None:
    label = a.label
    delay = 0.5
    peers = {p.strip().lower() for p in a.peers.split(",") if p.strip()}
    url = to_ws_url(getattr(a, "url", None) or get_bus_url())
    cdir = cfg_dir()
    work_cwd = pathlib.Path(a.cwd).expanduser() if getattr(a, "cwd", None) else home_dir()
    ack_events: dict[str, asyncio.Event] = {}
    my_node = ""

    if not getattr(a, "no_auto_update", False) and update_repo():
        restart_self()

    loop = asyncio.get_running_loop()
    journal = NodeJournal(cdir)
    worker_session = register_worker_session(cdir, label)

    inbox_wakeup = asyncio.Event()
    outbox_wakeup = asyncio.Event()
    inbox_wakeup.set()
    outbox_wakeup.set()

    # Startup recovery: transition any orphaned `running` items (from dead workers) to
    # `uncertain` (`exec_uncertain`) and enqueue `status='exec_uncertain'` into `outbox`.
    journal.recover_orphaned_running()

    ws_ref: dict[str, Any] = {"hub_key": url}

    async def _transmit_outbox_item(mid: str, payload: dict[str, Any]) -> str:
        """Send 1 already-persisted outbox item on the active WS under FIFO lock.

        Returns `'acked'`, `'rate'`, `'rejected'`, `'already_done'`, or `'disconnected'`.
        """
        ws = ws_ref.get("ws")
        send_lock: asyncio.Lock | None = ws_ref.get("send_lock")
        inflight: dict[str, asyncio.Future[dict[str, Any]]] | None = ws_ref.get("inflight")
        current_mid_slot: list[str] | None = ws_ref.get("current_mid")
        hub_key = str(ws_ref.get("hub_key") or url)
        if ws is None or send_lock is None or inflight is None or current_mid_slot is None:
            return "disconnected"

        async with send_lock:
            if ws_ref.get("ws") is not ws:
                return "disconnected"
            row = journal.get_outbox_row(mid)
            if row is not None and row["state"] != "pending":
                return "already_done"
            if row is not None and time.time() >= float(row["deadline"]):
                journal.mark_outbox_expired(mid)
                return "rejected"

            fut: asyncio.Future[dict[str, Any]] = loop.create_future()
            inflight[mid] = fut
            current_mid_slot[:] = [mid]
            try:
                await ws.send(json.dumps(payload, ensure_ascii=False))
                sys_frame = await asyncio.wait_for(fut, timeout=10.0)
            except Exception:
                return "disconnected"
            finally:
                inflight.pop(mid, None)
                if current_mid_slot == [mid]:
                    current_mid_slot.clear()

            if sys_frame.get("ack") == mid and not sys_frame.get("error"):
                journal.mark_outbox_acked(mid, hub_key)
                return "acked"
            err = sys_frame.get("error")
            if err == "rate":
                return "rate"
            if err in ("forbidden", "size", "hop"):
                journal.mark_outbox_expired(mid)
                return "rejected"
            return "disconnected"

    async def send_or_outbox(ws_ref_arg: dict[str, Any], payload: dict[str, Any]) -> bool:
        saved = await loop.run_in_executor(None, journal.enqueue_outbox, payload)
        mid = str(saved["id"])
        row = journal.get_outbox_row(mid)
        if row is not None and row["state"] == "expired":
            return False
        res = await _transmit_outbox_item(mid, saved)
        if res in ("acked", "already_done"):
            return True
        if res in ("disconnected", "rate"):
            outbox_wakeup.set()
        return False

    async def outbox_pump(ws_conn: Any) -> None:
        while ws_ref.get("ws") is ws_conn:
            outbox_wakeup.clear()
            hub_key = str(ws_ref.get("hub_key") or url)
            pending = await loop.run_in_executor(
                None,
                lambda: journal.list_outbox_pending(hub_key=hub_key),
            )
            hit_backoff = False
            for item in pending:
                if ws_ref.get("ws") is not ws_conn:
                    return
                mid = str(item["id"])
                res = await _transmit_outbox_item(mid, item)
                if res == "disconnected":
                    return
                if res == "rate":
                    hit_backoff = True
                    break
            if hit_backoff:
                await asyncio.sleep(0.25)
                continue
            try:
                await asyncio.wait_for(outbox_wakeup.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    async def worker() -> None:
        while True:
            inbox_wakeup.clear()

            # 1. Transition any expired `queued` items (`now >= deadline`) -> `expired` + outbox reply.
            expired_replies = await loop.run_in_executor(None, journal.expire_queued_inbox)
            for rep in expired_replies:
                await send_or_outbox(ws_ref, rep)

            # 2. Claim 1 unexpired `queued` item atomically if this node label is an active worker.
            claimed: dict[str, Any] | None = None
            if get_mode(a.mode) != "watch":
                claimed = await loop.run_in_executor(
                    None,
                    lambda: journal.claim_next_queued(
                        worker_id=worker_session.worker_id,
                        worker_pid=worker_session.pid,
                    ),
                )

            if claimed is None:
                try:
                    await asyncio.wait_for(inbox_wakeup.wait(), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
                continue

            mid = str(claimed["id"])
            corr_id = str(claimed.get("corr_id") or mid)
            sender = str(claimed["from"])

            ws_cur = ws_ref.get("ws")
            if ws_cur is not None and corr_id:
                with contextlib.suppress(Exception):
                    await ws_cur.send(
                        json.dumps(
                            {
                                "op": "progress",
                                "corr_id": corr_id,
                                "to": sender,
                                "status": "running",
                            },
                            ensure_ascii=False,
                        )
                    )

            if _TEST_CRASH_HOOK is not None:
                _TEST_CRASH_HOOK("after_claim_before_exec", claimed)

            try:
                ans, status = await loop.run_in_executor(
                    None, ask_claude, str(claimed["text"]), sender, work_cwd
                )
            except Exception as exc:
                ans, status = f"[agent error] {exc}", "exec_error"

            if _TEST_CRASH_HOOK is not None:
                _TEST_CRASH_HOOK("after_exec_before_save", claimed)

            reply_payload = await loop.run_in_executor(
                None,
                lambda: journal.complete_inbox_and_enqueue_reply(
                    mid,
                    result_status=status,
                    result_text=ans,
                ),
            )

            if _TEST_CRASH_HOOK is not None:
                _TEST_CRASH_HOOK("after_save_before_send", claimed)

            if reply_payload is not None:
                await send_or_outbox(ws_ref, reply_payload)

    main_task = asyncio.current_task()
    worker_task = asyncio.create_task(worker())

    def _on_worker_done(t: asyncio.Task[None]) -> None:
        if t.cancelled():
            return
        if t.exception() is not None and main_task is not None and not main_task.done():
            main_task.cancel()

    worker_task.add_done_callback(_on_worker_done)
    active_hub_key = url
    carry_live_seq = 0
    try:
        while True:
            pump_task: asyncio.Task[None] | None = None
            inflight: dict[str, asyncio.Future[dict[str, Any]]] = {}
            current_mid_slot: list[str] = []
            reconnect_immediately = False
            try:
                legacy_seq = load_cursor(label, cdir)
                hello_since = journal.get_cursor(active_hub_key, label=label, fallback_seq=legacy_seq)
                async with websockets.connect(url, ping_interval=20, ping_timeout=60) as ws:
                    await ws.send(
                        json.dumps(
                            build_ws_hello(
                                label,
                                hello_since,
                                accept_inbox=(label == "main"),
                            )
                        )
                    )
                    ws_ref["ws"] = ws
                    ws_ref["send_lock"] = asyncio.Lock()
                    ws_ref["inflight"] = inflight
                    ws_ref["current_mid"] = current_mid_slot
                    ws_ref["hub_key"] = active_hub_key
                    delay = 0.5

                    banner_seq = 0
                    replay_count = 0
                    last_replay_seq = hello_since
                    max_live_seq = carry_live_seq

                    async for raw in ws:
                        m = json.loads(raw)
                        op = m.get("op")
                        if op == "sys":
                            if m.get("you"):
                                my_node = str(m["you"]).split("/", 1)[0]
                                banner_seq = int(m.get("seq") or 0)
                                if m.get("hub_id"):
                                    active_hub_key = str(m["hub_id"])
                                    ws_ref["hub_key"] = active_hub_key
                                    if hello_since > 0:
                                        journal.advance_cursor(active_hub_key, hello_since, label=label)
                                if pump_task is None:
                                    outbox_wakeup.set()
                                    pump_task = asyncio.create_task(outbox_pump(ws))

                            ack_id = str(m["ack"]) if m.get("ack") else ""
                            if ack_id:
                                if ack_id in ack_events:
                                    ack_events[ack_id].set()
                                fut = inflight.get(ack_id)
                                if fut is not None and not fut.done():
                                    fut.set_result(m)
                            elif m.get("error") and current_mid_slot:
                                cur_id = current_mid_slot[0]
                                fut = inflight.get(cur_id)
                                if fut is not None and not fut.done():
                                    fut.set_result(m)

                            print(f"[hub] {m.get('text')}", flush=True)
                            if m.get("rules"):
                                print(m["rules"], flush=True)
                            continue

                        if op != "msg":
                            continue

                        seq = int(m["seq"])
                        mid = str(m.get("id") or f"seq-{seq}")
                        sender = str(m.get("from") or "")
                        corr_id = str(m["corr_id"]) if m.get("corr_id") is not None else None
                        kind = str(m.get("kind") or "task").strip().lower()
                        raw_text = str(m.get("text") or "")
                        hop = int(m.get("hop", 0) or 0)
                        received_at = time.time()
                        msg_ts = float(m.get("ts") or received_at)
                        msg_deadline = compute_deadline(
                            kind=kind,
                            base_ts=msg_ts,
                            explicit_deadline=m.get("deadline"),
                        )
                        is_replay = bool(m.get("replay"))

                        print(f"[{sender}] hop={hop} {raw_text}", flush=True)
                        node = sender.split("/")[0].lower()
                        peer_ok = (not peers) or (node in peers)

                        # 1. `!update` control path (preserved; inbox commit -> hub ack -> cursor).
                        if raw_text.strip() == "!update":
                            inserted, _ = await loop.run_in_executor(
                                None,
                                lambda: journal.receive_inbox(
                                    mid=mid,
                                    sender=sender,
                                    corr_id=corr_id,
                                    kind=kind,
                                    text=raw_text,
                                    hop=hop,
                                    received_at=received_at,
                                    deadline=msg_deadline,
                                    eligible=False,
                                ),
                            )
                            with contextlib.suppress(Exception):
                                await ws.send(json.dumps({"op": "ack", "seq": seq, "id": mid}))
                            if is_replay:
                                replay_count += 1
                                last_replay_seq = max(last_replay_seq, seq)
                                watermark = last_replay_seq
                            else:
                                max_live_seq = max(max_live_seq, seq)
                                watermark = max(last_replay_seq, max_live_seq)
                            journal.advance_cursor(active_hub_key, watermark, label=label)
                            save_cursor(label, watermark, cdir)
                            if (
                                inserted
                                and received_at < msg_deadline
                                and peer_ok
                                and m.get("trust") != "external"
                            ):
                                if await loop.run_in_executor(None, update_repo):
                                    restart_self()
                            if is_replay and replay_count >= 500 and last_replay_seq > hello_since and last_replay_seq < banner_seq:
                                carry_live_seq = max_live_seq
                                reconnect_immediately = True
                                break
                            continue

                        # 2. ACC bridge / local `inbox.jsonl` delivery path.
                        is_inbox_msg, target_sess = _is_inbox_delivery(m, label)
                        if is_inbox_msg:
                            if not journal.has_inbox_id(mid) and received_at < msg_deadline:
                                deliver_to_inbox(m, cfg_dir=cdir, target_session=target_sess)
                            await loop.run_in_executor(
                                None,
                                lambda: journal.receive_inbox(
                                    mid=mid,
                                    sender=sender,
                                    corr_id=corr_id,
                                    kind=kind,
                                    text=raw_text,
                                    hop=hop,
                                    received_at=received_at,
                                    deadline=msg_deadline,
                                    eligible=False,
                                ),
                            )
                            with contextlib.suppress(Exception):
                                await ws.send(json.dumps({"op": "ack", "seq": seq, "id": mid}))
                            if is_replay:
                                replay_count += 1
                                last_replay_seq = max(last_replay_seq, seq)
                                watermark = last_replay_seq
                            else:
                                max_live_seq = max(max_live_seq, seq)
                                watermark = max(last_replay_seq, max_live_seq)
                            journal.advance_cursor(active_hub_key, watermark, label=label)
                            save_cursor(label, watermark, cdir)
                            if is_replay and replay_count >= 500 and last_replay_seq > hello_since and last_replay_seq < banner_seq:
                                carry_live_seq = max_live_seq
                                reconnect_immediately = True
                                break
                            continue

                        # 3. Standard request/watch path:
                        #    Step A: Commit into `journal.db` `inbox` (`queued` vs `skipped`).
                        #    Step B: Send hub `ack` ONLY after `inbox` transaction commits.
                        #    Step C: Advance monotonic cursor watermark.
                        mode_now = get_mode(a.mode)
                        self_key = f"{my_node}/{label}".lower() if my_node else ""
                        not_self = not (self_key and sender.lower() == self_key)
                        in_status = str(m.get("status") or "ok").strip().lower()
                        eligible = (
                            not_self
                            and (in_status not in _TERMINAL_ERROR_STATUSES)
                            and (hop + 1 <= MAX_HOP)
                            and peer_ok
                            and should_reply(mode_now, raw_text)
                        )
                        stored_text = strip_ask(raw_text) if eligible else raw_text

                        changed, eff_state = await loop.run_in_executor(
                            None,
                            lambda: journal.receive_inbox(
                                mid=mid,
                                sender=sender,
                                corr_id=corr_id,
                                kind=kind,
                                text=stored_text,
                                hop=hop,
                                received_at=received_at,
                                deadline=msg_deadline,
                                eligible=eligible,
                            ),
                        )

                        with contextlib.suppress(Exception):
                            await ws.send(json.dumps({"op": "ack", "seq": seq, "id": mid}))

                        if is_replay:
                            replay_count += 1
                            last_replay_seq = max(last_replay_seq, seq)
                            watermark = last_replay_seq
                        else:
                            max_live_seq = max(max_live_seq, seq)
                            watermark = max(last_replay_seq, max_live_seq)

                        journal.advance_cursor(active_hub_key, watermark, label=label)
                        save_cursor(label, watermark, cdir)

                        if changed and eff_state == "queued":
                            inbox_wakeup.set()

                        if _TEST_CRASH_HOOK is not None and changed and eff_state == "queued":
                            _TEST_CRASH_HOOK("after_inbox_ack_before_claim", {"id": mid, "seq": seq})

                        if is_replay and replay_count >= 500 and last_replay_seq > hello_since and last_replay_seq < banner_seq:
                            carry_live_seq = max_live_seq
                            reconnect_immediately = True
                            break
                    else:
                        carry_live_seq = 0

                    if not reconnect_immediately and max_live_seq > last_replay_seq and replay_count < 500:
                        journal.advance_cursor(active_hub_key, max_live_seq, label=label)
                        save_cursor(label, max_live_seq, cdir)
                        carry_live_seq = 0
            except asyncio.CancelledError:
                if worker_task.done() and not worker_task.cancelled():
                    w_exc = worker_task.exception()
                    if w_exc is not None:
                        raise w_exc
                raise
            except Exception as exc:
                if getattr(exc, "_fatal_node_crash", False):
                    raise
                print(f"[disconnected: {type(exc).__name__}] retry in {delay:.1f}s", flush=True)
            finally:
                ws_ref.pop("ws", None)
                ws_ref.pop("send_lock", None)
                ws_ref.pop("inflight", None)
                ws_ref.pop("current_mid", None)
                for fut in list(inflight.values()):
                    if not fut.done():
                        fut.set_exception(ConnectionError("websocket disconnected"))
                inflight.clear()
                if pump_task is not None:
                    pump_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pump_task

            if reconnect_immediately:
                continue
            await asyncio.sleep(delay * (0.5 + random.random()))
            delay = min(delay * 2, 30.0)
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker_task
        worker_session.close()
        journal.close()


async def oneshot(a: argparse.Namespace) -> int:
    url = getattr(a, "url", None) or get_bus_url()
    cdir = cfg_dir()
    cmd = a.cmd

    if cmd == "peers" and is_http_url(url):
        code, data = http_request_json(url, "/v1/peers", method="GET")
        if code >= 400 or not data.get("ok"):
            print(f"[error] {data.get('error', code)}", file=sys.stderr)
            return 1
        print("peers=" + ",".join(data.get("peers", [])))
        return 0

    if cmd == "result":
        cid = a.corr_id
        wait = float(getattr(a, "wait", 0) or 0)
        deadline = time.time() + wait
        if is_http_url(url):
            while True:
                code, data = http_request_json(url, f"/v1/rpc/{urllib.parse.quote(cid)}", method="GET")
                if code >= 400:
                    print(f"[error] {data.get('error', code)}", file=sys.stderr)
                    return 1
                if data.get("found") and data.get("status") not in ("pending", "running"):
                    print(
                        json.dumps(data, ensure_ascii=False)
                        if getattr(a, "json", False)
                        else f"[{data.get('sender')}] ({data.get('status')}) {data.get('text')}"
                    )
                    st = str(data.get("status") or "ok")
                    if st == "auth_error":
                        return 3
                    return 1 if st in _TERMINAL_ERROR_STATUSES else 0
                if time.time() >= deadline:
                    if data.get("found"):
                        print(f"[pending] corr_id={cid} status={data.get('status')}", file=sys.stderr)
                    else:
                        print(f"[not found] corr_id={cid}", file=sys.stderr)
                    return 124
                await asyncio.sleep(0.2)
        else:
            ws_url = to_ws_url(url)
            cli_label = f"cli-{uuid.uuid4().hex[:6]}"
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=60) as ws:
                await ws.send(json.dumps(build_ws_hello(cli_label, 10**9, accept_inbox=False)))
                await ws.recv()
                while True:
                    await ws.send(json.dumps({"op": "rpc_result", "corr_id": cid}))
                    res = json.loads(await ws.recv())
                    if res.get("found") and res.get("status") not in ("pending", "running"):
                        print(
                            json.dumps(res, ensure_ascii=False)
                            if getattr(a, "json", False)
                            else f"[{res.get('sender')}] ({res.get('status')}) {res.get('text')}"
                        )
                        st = str(res.get("status") or "ok")
                        if st == "auth_error":
                            return 3
                        return 1 if st in _TERMINAL_ERROR_STATUSES else 0
                    if time.time() >= deadline:
                        print(f"[pending] corr_id={cid}", file=sys.stderr)
                        return 124
                    await asyncio.sleep(0.2)

    if cmd == "peers":
        ws_url = to_ws_url(url)
        cli_label = f"cli-{uuid.uuid4().hex[:6]}"
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=60) as ws:
                await ws.send(json.dumps(build_ws_hello(cli_label, 10**9, accept_inbox=False)))
                await ws.recv()
                await ws.send(json.dumps({"op": "peers"}))
                print(json.loads(await ws.recv())["text"])
                return 0
        except Exception as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 1

    # `cmd == "send"`: Persist to `journal.db` `outbox` (`state='pending'`) BEFORE transmission
    # so retries/reconnects/fallbacks always reuse the exact same `id` and `corr_id` (G1, F31).
    explicit_id = getattr(a, "id", None)
    mid = str(explicit_id).strip() if explicit_id else new_message_id()
    corr_id = str(getattr(a, "corr_id", None) or mid)
    is_update = bool(getattr(a, "update", False))
    is_say = bool(getattr(a, "say", False))
    wait_s = float(getattr(a, "wait", 0) or 0)
    kind = getattr(a, "kind", None) or ("event" if is_say else ("rpc" if wait_s > 0 else "task"))
    text = "!update" if is_update else (a.text if (is_say or kind == "event") else "!ask " + a.text)
    hop = int(getattr(a, "hop", 0) or 0)
    ttl = int(getattr(a, "ttl", 86400) or 86400)
    created_now = time.time()
    send_deadline = compute_deadline(
        kind=kind,
        base_ts=created_now,
        explicit_deadline=getattr(a, "deadline", None),
    )

    send_payload: dict[str, Any] = {
        "v": 2,
        "op": "send",
        "id": mid,
        "corr_id": corr_id,
        "kind": kind,
        "to": a.to,
        "text": text,
        "hop": hop,
        "ttl": ttl,
    }

    with NodeJournal(cdir) as journal:
        saved_payload = journal.enqueue_outbox(
            send_payload,
            created=created_now,
            deadline=send_deadline,
        )
        mid = str(saved_payload["id"])
        corr_id = str(saved_payload.get("corr_id") or mid)

    if is_http_url(url):
        http_payload = {
            **saved_payload,
            "label": f"cli-{uuid.uuid4().hex[:6]}",
        }
        try:
            code, data = http_request_json(url, "/v1/send", method="POST", payload=http_payload)
        except Exception as exc:
            if getattr(a, "offline_queue", False):
                print(f"[queued-offline] id={mid} err={exc}", file=sys.stderr)
                return 0
            print(f"[error] {exc}", file=sys.stderr)
            return 1
        if code >= 400 or not data.get("ok"):
            if code in (400, 403, 413):
                with NodeJournal(cdir) as journal:
                    journal.mark_outbox_expired(mid)
            elif (code == 429 or data.get("error") == "rate") and getattr(a, "offline_queue", False):
                print(f"[queued-offline] id={mid} err=rate", file=sys.stderr)
                return 0
            print(f"[error] {data.get('error', code)}", file=sys.stderr)
            return 1

        with NodeJournal(cdir) as journal:
            journal.mark_outbox_acked(mid, url)
        print(
            f"[hub] ack seq={data.get('seq')} id={mid} delivered={data.get('delivered') or 'queued'}",
            file=sys.stderr,
        )
        if wait_s <= 0:
            return 0
        start_t = time.time()
        deadline = start_t + wait_s
        hard_deadline = start_t + (max(wait_s, 300.0) if wait_s >= 30.0 else wait_s)
        while time.time() < deadline:
            await asyncio.sleep(min(0.5, max(0.05, deadline - time.time())))
            rc, res = http_request_json(url, f"/v1/rpc/{urllib.parse.quote(corr_id)}", method="GET")
            if rc == 200 and res.get("found"):
                st = str(res.get("status") or "ok")
                if st in ("pending", "running"):
                    if st == "running":
                        deadline = min(hard_deadline, max(deadline, time.time() + 60.0))
                    continue
                print(f"[{res.get('sender')}] {res.get('text')}")
                if st == "auth_error":
                    return 3
                return 1 if st in _TERMINAL_ERROR_STATUSES else 0
        print(f"TIMEOUT_PENDING corr_id={corr_id} (retrieve via: abus result {corr_id})", file=sys.stderr)
        return 124

    ws_url = to_ws_url(url)
    cli_label = f"cli-{uuid.uuid4().hex[:6]}"
    try:
        async with websockets.connect(ws_url, ping_interval=20, ping_timeout=60) as ws:
            await ws.send(json.dumps(build_ws_hello(cli_label, 10**9, accept_inbox=False)))
            banner_raw = await ws.recv()  # connected banner
            banner = json.loads(banner_raw)
            hub_key = str(banner.get("hub_id") or ws_url)

            # Opportunistically flush earlier pending outbox entries (up to 32 items)
            with NodeJournal(cdir) as journal:
                earlier = [
                    it
                    for it in journal.list_outbox_pending(hub_key=hub_key, limit=33)
                    if str(it.get("id")) != mid
                ][:32]
                for item in earlier:
                    item_id = str(item["id"])
                    await ws.send(json.dumps(item, ensure_ascii=False))
                    ack_raw = await asyncio.wait_for(ws.recv(), 5.0)
                    ack_msg = json.loads(ack_raw)
                    if ack_msg.get("op") == "sys" and ack_msg.get("ack") == item_id and not ack_msg.get("error"):
                        journal.mark_outbox_acked(item_id, hub_key)
                    elif ack_msg.get("error") in ("forbidden", "size", "hop"):
                        journal.mark_outbox_expired(item_id)
                    else:
                        break

            await ws.send(json.dumps(saved_payload, ensure_ascii=False))
            if wait_s <= 0:
                ack_deadline = time.monotonic() + 5.0
                while True:
                    rem = max(0.05, ack_deadline - time.monotonic())
                    m = json.loads(await asyncio.wait_for(ws.recv(), rem))
                    if m.get("op") != "sys":
                        continue
                    print(f"[hub] {m.get('text')}", file=sys.stderr)
                    err = m.get("error")
                    if err:
                        if err in ("forbidden", "size", "hop"):
                            with NodeJournal(cdir) as journal:
                                journal.mark_outbox_expired(mid)
                        elif err == "rate" and getattr(a, "offline_queue", False):
                            return 0
                        return 1
                    if m.get("ack") == mid:
                        with NodeJournal(cdir) as journal:
                            journal.mark_outbox_acked(mid, hub_key)
                        return 0

            start_t = time.time()
            end = start_t + wait_s
            hard_end = start_t + (max(wait_s, 300.0) if wait_s >= 30.0 else wait_s)
            got_reply = False
            acked_send = False
            last_status = "ok"
            while time.time() < end:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), max(0.1, end - time.time())))
                except asyncio.TimeoutError:
                    break
                op = m.get("op")
                if op == "sys":
                    print(f"[hub] {m.get('text')}", file=sys.stderr)
                    err = m.get("error")
                    if err:
                        if err in ("forbidden", "size", "hop"):
                            with NodeJournal(cdir) as journal:
                                journal.mark_outbox_expired(mid)
                        return 1
                    if m.get("ack") == mid and not acked_send:
                        acked_send = True
                        with NodeJournal(cdir) as journal:
                            journal.mark_outbox_acked(mid, hub_key)
                elif op == "progress" and m.get("corr_id") == corr_id:
                    print(f"[progress] {m.get('from')} status={m.get('status')}", file=sys.stderr)
                    end = min(hard_end, max(end, time.time() + 60.0))
                elif op == "msg":
                    m_cid = m.get("corr_id")
                    if m_cid and m_cid != corr_id:
                        continue
                    if not acked_send:
                        acked_send = True
                        with NodeJournal(cdir) as journal:
                            journal.mark_outbox_acked(mid, hub_key)
                    print(f"[{m['from']}] {m['text']}")
                    got_reply = True
                    last_status = str(m.get("status") or "ok")
                    if getattr(a, "wait_first", False) or m_cid == corr_id:
                        if last_status == "auth_error":
                            return 3
                        return 1 if last_status in _TERMINAL_ERROR_STATUSES else 0
            if not got_reply:
                print(f"TIMEOUT_PENDING corr_id={corr_id} (retrieve via: abus result {corr_id})", file=sys.stderr)
                return 124
            return 0
    except Exception as exc:
        if getattr(a, "offline_queue", False):
            print(f"[queued-offline] id={mid} err={exc}", file=sys.stderr)
            return 0
        print(f"[error] {exc}", file=sys.stderr)
        return 1


def run_supervise(a: argparse.Namespace) -> int:
    """Cross-OS supervisor using RotatingFileHandler (avoids Windows >> log handle locks)."""
    log_file = pathlib.Path(
        getattr(a, "log_file", None) or (home_dir() / "logs" / f"agent-bus-{a.label}.log")
    ).expanduser()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"abus.supervise.{a.label}")
    logger.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)

    cmd = [sys.executable, "-u", "-m", "agent_bus.cli", "run", "--label", a.label]
    if getattr(a, "mode", None):
        cmd.extend(["--mode", a.mode])
    if getattr(a, "peers", ""):
        cmd.extend(["--peers", a.peers])
    if getattr(a, "url", None):
        cmd.extend(["--url", a.url])

    env = os.environ.copy()
    env["BUS_SUPERVISOR"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    delay = 1.0
    while True:
        logger.info("starting child: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = line.rstrip("\r\n")
            logger.info("%s", clean)
            print(clean, flush=True)
        rc = proc.wait()
        logger.info("child exited with code=%s", rc)
        if rc == 42:
            delay = 1.0
            continue
        if rc == 0 and getattr(a, "once", False):
            return 0
        time.sleep(delay)
        delay = min(delay * 2, 30.0)


def run_doctor(a: argparse.Namespace) -> dict[str, Any]:
    cdir = cfg_dir()
    url = getattr(a, "url", None) or get_bus_url()
    with NodeJournal(cdir) as j:
        j_summary = j.status_summary(expire_outbox_now=True)
    report: dict[str, Any] = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "url": url,
        "config_dir": str(cdir),
        "mode": get_mode(),
        "cursor_main": load_cursor("main", cdir),
        "outbox_pending": j_summary["outbox"]["pending"],
        "journal_uncertain": j_summary["inbox"]["uncertain"],
        "inbox_unread": len(read_inbox(cdir, include_acked=False)),
        "hub_ok": False,
        "claude_ok": False,
        "gh_ok": False,
    }
    try:
        if is_http_url(url):
            code, data = http_request_json(url, "/v1/health", method="GET", timeout=5.0)
            report["hub_ok"] = code == 200 and bool(data.get("ok"))
            report["hub_seq"] = data.get("seq")
        else:
            async def _ping() -> list[str]:
                async with websockets.connect(to_ws_url(url), open_timeout=5, close_timeout=2) as ws:
                    await ws.send(json.dumps(build_ws_hello(f"doc-{uuid.uuid4().hex[:6]}", 10**9, accept_inbox=False)))
                    banner = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
                    return list(banner.get("peers") or [])

            peers = asyncio.run(_ping())
            report["hub_ok"] = True
            report["peers"] = peers
    except Exception as exc:
        report["hub_error"] = str(exc)

    claude_bin = shutil.which("claude")
    if claude_bin:
        try:
            r = subprocess.run([claude_bin, "auth", "status"], capture_output=True, text=True, timeout=10)
            txt = (r.stdout or r.stderr).strip()
            report["claude_raw"] = txt[:300]
            try:
                parsed = json.loads(txt)
                report["claude_ok"] = bool(parsed.get("loggedIn"))
            except Exception:
                report["claude_ok"] = r.returncode == 0
        except Exception as exc:
            report["claude_error"] = str(exc)
    else:
        report["claude_error"] = "claude binary not found"

    gh_bin = shutil.which("gh")
    if gh_bin:
        try:
            r = subprocess.run([gh_bin, "auth", "status"], capture_output=True, text=True, timeout=10)
            report["gh_ok"] = r.returncode == 0
        except Exception as exc:
            report["gh_error"] = str(exc)
    else:
        report["gh_error"] = "gh binary not found"

    return report


def main_legacy(argv: list[str] | None = None) -> None:
    """Backward-compatible entrypoint for docs/tailhub-termux/bus/node.py."""
    p = argparse.ArgumentParser(prog="node.py")
    sp = p.add_subparsers(dest="cmd", required=True)

    r = sp.add_parser("run")
    r.add_argument("--label", default="main")
    r.add_argument("--agent", action="store_true", help="(compat) same as --mode agent")
    r.add_argument("--mode", choices=["agent", "watch", "hybrid"], default=None)
    r.add_argument("--peers", default="")
    r.add_argument("--url", default=None)
    r.add_argument("--cwd", default=None)
    r.add_argument("--no-auto-update", action="store_true")

    s = sp.add_parser("send")
    s.add_argument("--to", required=True)
    s.add_argument("text", nargs="?", default="")
    s.add_argument("--wait", type=float, default=0)
    s.add_argument("--hop", type=int, default=0)
    s.add_argument("--first", dest="wait_first", action="store_true")
    s.add_argument("--update", action="store_true")
    s.add_argument("--say", action="store_true")
    s.add_argument("--kind", choices=["rpc", "task", "event"], default=None)
    s.add_argument("--corr-id", default=None)
    s.add_argument("--ttl", type=int, default=86400)
    s.add_argument("--url", default=None)
    s.add_argument("--offline-queue", action="store_true")

    pr = sp.add_parser("peers")
    pr.add_argument("--url", default=None)

    res_p = sp.add_parser("result")
    res_p.add_argument("corr_id")
    res_p.add_argument("--wait", type=float, default=0)
    res_p.add_argument("--url", default=None)
    res_p.add_argument("--json", action="store_true")

    a = p.parse_args(argv)
    if getattr(a, "agent", False) and not getattr(a, "mode", None):
        a.mode = "agent"

    try:
        if a.cmd == "run":
            asyncio.run(run_node(a))
        else:
            rc = asyncio.run(oneshot(a))
            if rc:
                sys.exit(rc)
    except KeyboardInterrupt:
        pass
