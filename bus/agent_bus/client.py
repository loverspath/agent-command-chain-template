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
    MAX_RECONNECT_BACKOFF,
    cfg_dir,
    get_ack_timeout,
    get_auth_credentials,
    get_banner_timeout,
    get_bus_url,
    get_bus_urls,
    get_configured_multi_urls,
    get_dial_timeout,
    get_hello_timeout,
    home_dir,
    load_config,
    mode_file_path,
    parse_url_list,
    save_config,
)
from .journal import (
    NodeJournal,
    canonical_hub_url,
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
_PERMANENT_REJECT_ERRORS = frozenset(
    {"forbidden", "size", "hop", "id_conflict", "invalid_deadline"}
)

# Optional chaos-test fault injection callback `(stage: str, row: dict[str, Any]) -> Any`.
# Returning `False` on `"before_claim"` or `"before_outbox_pump_send"` suppresses that
# background poll tick; raising an exception with `_fatal_node_crash = True` halts sibling tasks.
_TEST_CRASH_HOOK: Callable[[str, dict[str, Any]], Any] | None = None


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


def build_ws_hello(
    label: str,
    since: int,
    accept_inbox: bool = True,
    *,
    no_replay: bool = False,
    hub_id: str | None = None,
) -> dict[str, Any]:
    hello: dict[str, Any] = {
        "v": 2,
        "label": label,
        "since": since,
        "accept_inbox": accept_inbox,
    }
    if no_replay:
        hello["no_replay"] = True
    if hub_id:
        hello["hub_id"] = str(hub_id)
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


def _is_permanent_http_reject(code: int, data: dict[str, Any]) -> tuple[bool, str]:
    err_str = str(data.get("error") or code)
    if err_str in _PERMANENT_REJECT_ERRORS or code in (400, 409, 413):
        return True, err_str
    return False, err_str


def _resolve_quorum_urls(config_dir: pathlib.Path, target_urls: list[str]) -> list[str]:
    """Return the required hub endpoint URL quorum for `outbox` completion (`G2`)."""
    cfg = load_config(config_dir)
    multi = get_configured_multi_urls(cfg)
    if not multi:
        return list(target_urls)
    multi_canons = {canonical_hub_url(u) for u in multi}
    target_canons = {canonical_hub_url(u) for u in target_urls}
    if multi_canons & target_canons:
        return parse_url_list([*multi, *target_urls])
    return list(target_urls)


def _infer_ws_endpoint_url(ws: Any) -> str | None:
    rem = getattr(ws, "remote_address", None)
    if isinstance(rem, tuple) and len(rem) >= 2 and rem[0] and rem[1]:
        return f"ws://{rem[0]}:{rem[1]}"
    return None


async def _open_ws_with_deadlines(
    ws_url: str,
    hello: dict[str, Any],
    *,
    dial_timeout: float,
    hello_timeout: float,
    banner_timeout: float,
) -> tuple[Any, dict[str, Any]]:
    """Connect WS (`dial_timeout`), send `hello` (`hello_timeout`), and read banner (`banner_timeout`)."""
    ws = await asyncio.wait_for(
        websockets.connect(
            ws_url,
            open_timeout=dial_timeout,
            close_timeout=2.0,
            ping_interval=20,
            ping_timeout=60,
        ),
        timeout=dial_timeout + 0.5,
    )
    try:
        await asyncio.wait_for(
            ws.send(json.dumps(hello, ensure_ascii=False)),
            timeout=hello_timeout,
        )
        banner_raw = await asyncio.wait_for(ws.recv(), timeout=banner_timeout)
        banner = json.loads(banner_raw)
        if not isinstance(banner, dict) or banner.get("op") != "sys" or not banner.get("you"):
            raise ConnectionError(f"invalid hub banner from {ws_url}: {banner_raw!r}")
        return ws, banner
    except BaseException:
        with contextlib.suppress(Exception):
            await ws.close()
        raise


async def _flush_outbox_over_ws(
    ws: Any,
    config_dir: pathlib.Path,
    ack_events: dict[str, asyncio.Event],
    *,
    hub_key: str | None = None,
) -> int:
    """Flush pending `journal.db` outbox items over `ws` (backward-compatible signature)."""
    inferred_url = _infer_ws_endpoint_url(ws)
    flushed = 0
    with NodeJournal(config_dir) as j:
        if hub_key:
            eff_hub = str(hub_key)
            quorum_urls = _resolve_quorum_urls(config_dir, [inferred_url] if inferred_url else [eff_hub])
        elif inferred_url:
            eff_hub = j.resolve_hub_id(inferred_url) or canonical_hub_url(inferred_url)
            quorum_urls = _resolve_quorum_urls(config_dir, [inferred_url])
        else:
            eff_hub = "default"
            quorum_urls = ["default"]

        pending = j.list_outbox_pending(hub_key=eff_hub)
        for item in pending:
            mid = str(item.get("id") or "")
            ev = asyncio.Event()
            if mid:
                ack_events[mid] = ev
            try:
                await ws.send(json.dumps(item, ensure_ascii=False))
                if mid:
                    await asyncio.wait_for(ev.wait(), 5.0)
                    j.mark_outbox_acked(mid, eff_hub, required_hubs=quorum_urls)
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
    """Republish all `pending` outbox entries across configured hubs until `pending == 0` or `timeout`."""
    cdir = pathlib.Path(config_dir).expanduser() if config_dir is not None else cfg_dir()
    cfg = load_config(cdir)
    target_urls = get_bus_urls(cfg, explicit_url=url)
    quorum_urls = _resolve_quorum_urls(cdir, target_urls)
    deadline_mono = time.monotonic() + max(0.1, float(timeout))
    dial_t = get_dial_timeout(cfg=cfg)
    hello_t = get_hello_timeout(cfg=cfg)
    banner_t = get_banner_timeout(cfg=cfg)
    ack_t = get_ack_timeout(cfg=cfg)
    counters = {"drained": 0, "expired": 0, "rejected": 0}

    with NodeJournal(cdir) as journal:
        counters["expired"] += journal.expire_outbox()
        initial_pending = len(journal.list_outbox_pending(expire_now=False))
        if initial_pending == 0:
            return {
                "ok": True,
                "drained": 0,
                "expired": counters["expired"],
                "rejected": 0,
                "remaining": 0,
            }

        async def _drain_one_hub(target_url: str) -> None:
            canon_url = canonical_hub_url(target_url)
            if is_http_url(target_url):
                backoff = 0.2
                while time.monotonic() < deadline_mono:
                    counters["expired"] += journal.expire_outbox()
                    cur_key = journal.resolve_hub_id(target_url) or canon_url
                    pending = journal.list_outbox_pending(hub_key=cur_key, expire_now=False)
                    if not pending:
                        break
                    rate_limited = False
                    for item in pending:
                        if time.monotonic() >= deadline_mono:
                            break
                        mid = str(item["id"])
                        row_now = journal.get_outbox_row(mid)
                        if row_now is not None and row_now["state"] != "pending":
                            continue
                        http_payload = dict(item)
                        http_payload.setdefault("label", f"cli-drain-{uuid.uuid4().hex[:6]}")
                        req_timeout = max(0.1, min(10.0, deadline_mono - time.monotonic()))
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
                            hid = str(data.get("hub_id") or "").strip()
                            if hid:
                                journal.remember_hub_id(target_url, hid)
                                cur_key = hid
                            if journal.mark_outbox_acked(mid, cur_key, required_hubs=quorum_urls):
                                counters["drained"] += 1
                            backoff = 0.2
                        elif code == 429 or data.get("error") == "rate":
                            rate_limited = True
                            break
                        else:
                            perm, err_str = _is_permanent_http_reject(code, data)
                            if perm:
                                if journal.mark_outbox_rejected(mid, hub_key=cur_key, error=err_str):
                                    counters["rejected"] += 1
                            else:
                                rate_limited = True
                                break
                    if rate_limited and time.monotonic() < deadline_mono:
                        await asyncio.sleep(min(backoff, max(0.02, deadline_mono - time.monotonic())))
                        backoff = min(backoff * 2, 2.0)
                return

            ws_url = to_ws_url(target_url)
            backoff = 0.2
            while time.monotonic() < deadline_mono:
                counters["expired"] += journal.expire_outbox()
                est_key = journal.resolve_hub_id(target_url) or canon_url
                if not journal.list_outbox_pending(hub_key=est_key, expire_now=False):
                    break
                rem = max(0.1, deadline_mono - time.monotonic())
                cli_label = f"cli-drain-{uuid.uuid4().hex[:6]}"
                ws: Any = None
                try:
                    ws, banner = await _open_ws_with_deadlines(
                        ws_url,
                        build_ws_hello(cli_label, 10**9, accept_inbox=False, no_replay=True),
                        dial_timeout=min(dial_t, rem),
                        hello_timeout=min(hello_t, max(0.1, deadline_mono - time.monotonic())),
                        banner_timeout=min(banner_t, max(0.1, deadline_mono - time.monotonic())),
                    )
                    if banner.get("hub_id"):
                        hub_key = str(banner["hub_id"])
                        journal.remember_hub_id(target_url, hub_key)
                    else:
                        journal.clear_hub_id(target_url)
                        hub_key = canon_url
                    backoff = 0.2

                    while time.monotonic() < deadline_mono:
                        counters["expired"] += journal.expire_outbox()
                        pending = journal.list_outbox_pending(hub_key=hub_key, expire_now=False)
                        if not pending:
                            break
                        hit_rate = False
                        for item in pending:
                            if time.monotonic() >= deadline_mono:
                                break
                            mid = str(item["id"])
                            row_now = journal.get_outbox_row(mid)
                            if row_now is not None and row_now["state"] != "pending":
                                continue
                            await asyncio.wait_for(
                                ws.send(json.dumps(item, ensure_ascii=False)),
                                timeout=min(hello_t, max(0.1, deadline_mono - time.monotonic())),
                            )
                            while True:
                                rem_recv = max(0.05, min(ack_t, deadline_mono - time.monotonic()))
                                raw_frame = await asyncio.wait_for(ws.recv(), timeout=rem_recv)
                                frame = json.loads(raw_frame)
                                if frame.get("op") != "sys":
                                    continue
                                if frame.get("ack") == mid and not frame.get("error"):
                                    if journal.mark_outbox_acked(mid, hub_key, required_hubs=quorum_urls):
                                        counters["drained"] += 1
                                    break
                                err = str(frame.get("error") or "")
                                if err == "rate":
                                    hit_rate = True
                                    break
                                if err in _PERMANENT_REJECT_ERRORS:
                                    if journal.mark_outbox_rejected(mid, hub_key=hub_key, error=err):
                                        counters["rejected"] += 1
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
                finally:
                    if ws is not None:
                        with contextlib.suppress(Exception):
                            await ws.close()

        await asyncio.gather(*(_drain_one_hub(u) for u in target_urls))

        counters["expired"] += journal.expire_outbox()
        remaining = len(journal.list_outbox_pending(expire_now=False))
        return {
            "ok": remaining == 0,
            "drained": counters["drained"],
            "expired": counters["expired"],
            "rejected": counters["rejected"],
            "remaining": remaining,
        }


async def run_node(a: argparse.Namespace) -> None:
    label = a.label
    peers = {p.strip().lower() for p in a.peers.split(",") if p.strip()}
    raw_cdir = getattr(a, "config_dir", None)
    cdir = pathlib.Path(raw_cdir).expanduser() if raw_cdir else cfg_dir()
    cfg = load_config(cdir)
    raw_urls = get_bus_urls(cfg, explicit_url=getattr(a, "url", None))
    quorum_urls = _resolve_quorum_urls(cdir, raw_urls)
    dial_timeout = get_dial_timeout(getattr(a, "dial_timeout", None), cfg)
    hello_timeout = get_hello_timeout(getattr(a, "hello_timeout", None), cfg)
    banner_timeout = get_banner_timeout(getattr(a, "banner_timeout", None), cfg)
    ack_timeout = get_ack_timeout(getattr(a, "ack_timeout", None), cfg)
    work_cwd = pathlib.Path(a.cwd).expanduser() if getattr(a, "cwd", None) else home_dir()
    ack_events: dict[str, asyncio.Event] = {}

    if not getattr(a, "no_auto_update", False) and update_repo():
        restart_self()

    loop = asyncio.get_running_loop()
    journal = NodeJournal(cdir)
    initial_upgrade_cursor = not journal.has_any_cursor(label=label)
    worker_session = register_worker_session(cdir, label)

    inbox_wakeup = asyncio.Event()
    inbox_wakeup.set()

    # Startup recovery: transition any orphaned `running` items (from dead workers) to
    # `uncertain` (`exec_uncertain`) and enqueue `status='exec_uncertain'` into `outbox`.
    journal.recover_orphaned_running()

    main_task = asyncio.current_task()
    worker_task_ref: list[asyncio.Task[None] | None] = [None]
    hub_tasks: list[asyncio.Task[None]] = []
    pump_tasks: set[asyncio.Task[None]] = set()
    bg_tasks: set[asyncio.Task[Any]] = set()
    fatal_crash_flag: list[bool] = [False]
    fatal_exc_slot: list[BaseException | None] = [None]

    hub_states: list[dict[str, Any]] = []
    for raw_u in raw_urls:
        ws_u = to_ws_url(raw_u)
        canon_u = canonical_hub_url(raw_u)
        known_id = journal.resolve_hub_id(raw_u)
        ev = asyncio.Event()
        ev.set()
        hub_states.append(
            {
                "raw_url": raw_u,
                "ws_url": ws_u,
                "canon_url": canon_u,
                "hub_key": known_id or canon_u,
                "caps": frozenset(),
                "pump_wakeup": ev,
                "my_node": "",
            }
        )

    def _notify_all_pumps() -> None:
        for st in hub_states:
            st["pump_wakeup"].set()

    def _cancel_all_siblings(cur: asyncio.Task[Any] | None) -> None:
        for pt in list(pump_tasks):
            if pt is not cur and not pt.done():
                pt.cancel()
        for ht in list(hub_tasks):
            if ht is not cur and not ht.done():
                ht.cancel()
        wt = worker_task_ref[0]
        if wt is not None and wt is not cur and not wt.done():
            wt.cancel()
        if main_task is not None and main_task is not cur and not main_task.done():
            main_task.cancel()

    def _fire_test_crash_hook(stage: str, info: dict[str, Any]) -> Any:
        hook = _TEST_CRASH_HOOK
        if hook is None:
            return None
        try:
            return hook(stage, info)
        except BaseException as exc:
            if getattr(exc, "_fatal_node_crash", False):
                fatal_crash_flag[0] = True
                if fatal_exc_slot[0] is None:
                    fatal_exc_slot[0] = exc
                _cancel_all_siblings(asyncio.current_task())
            raise

    async def _transmit_outbox_item(h_state: dict[str, Any], mid: str, payload: dict[str, Any]) -> str:
        """Send 1 already-persisted outbox item on `h_state`'s active WS under FIFO lock.

        Returns `'acked'`, `'rate'`, `'rejected'`, `'already_done'`, or `'disconnected'`.
        """
        ws = h_state.get("ws")
        send_lock: asyncio.Lock | None = h_state.get("send_lock")
        inflight: dict[str, asyncio.Future[dict[str, Any]]] | None = h_state.get("inflight")
        current_mid_slot: list[str] | None = h_state.get("current_mid")
        hub_key = str(h_state.get("hub_key") or h_state["canon_url"])
        if ws is None or send_lock is None or inflight is None or current_mid_slot is None:
            return "disconnected"

        async with send_lock:
            if h_state.get("ws") is not ws:
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
                await asyncio.wait_for(ws.send(json.dumps(payload, ensure_ascii=False)), timeout=hello_timeout)
                sys_frame = await asyncio.wait_for(fut, timeout=ack_timeout)
            except Exception:
                return "disconnected"
            finally:
                inflight.pop(mid, None)
                if current_mid_slot == [mid]:
                    current_mid_slot.clear()

            if sys_frame.get("ack") == mid and not sys_frame.get("error"):
                journal.mark_outbox_acked(mid, hub_key, required_hubs=quorum_urls)
                return "acked"
            err = str(sys_frame.get("error") or "")
            if err == "rate":
                return "rate"
            if err in _PERMANENT_REJECT_ERRORS:
                journal.mark_outbox_rejected(mid, hub_key=hub_key, error=err)
                return "rejected"
            return "disconnected"

    async def _outbox_pump(h_state: dict[str, Any], ws_conn: Any) -> None:
        pump_wakeup: asyncio.Event = h_state["pump_wakeup"]
        rate_backoff = 0.25
        while h_state.get("ws") is ws_conn and not fatal_crash_flag[0]:
            pump_wakeup.clear()
            hub_key = str(h_state.get("hub_key") or h_state["canon_url"])
            pending = await loop.run_in_executor(
                None,
                lambda hk=hub_key: journal.list_outbox_pending(hub_key=hk),
            )
            hit_backoff = False
            for item in pending:
                if h_state.get("ws") is not ws_conn or fatal_crash_flag[0]:
                    return
                if _TEST_CRASH_HOOK is not None and _fire_test_crash_hook("before_outbox_pump_send", item) is False:
                    continue
                mid = str(item["id"])
                res = await _transmit_outbox_item(h_state, mid, item)
                if res == "disconnected":
                    return
                if res == "rate":
                    hit_backoff = True
                    break
                rate_backoff = 0.25
            if hit_backoff:
                await asyncio.sleep(rate_backoff)
                rate_backoff = min(rate_backoff * 1.5, 2.0)
                continue
            try:
                await asyncio.wait_for(pump_wakeup.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    def _broadcast_progress_best_effort(corr_id: str, sender: str) -> None:
        if not corr_id:
            return
        frame_txt = json.dumps(
            {
                "op": "progress",
                "corr_id": corr_id,
                "to": sender,
                "status": "running",
            },
            ensure_ascii=False,
        )
        for st in hub_states:
            ws_cur = st.get("ws")
            slock: asyncio.Lock | None = st.get("send_lock")
            if ws_cur is None or slock is None:
                continue

            async def _send_prog(w: Any = ws_cur, lk: asyncio.Lock = slock) -> None:
                with contextlib.suppress(Exception):
                    async with lk:
                        await asyncio.wait_for(w.send(frame_txt), timeout=1.5)

            t = asyncio.create_task(_send_prog())
            bg_tasks.add(t)
            t.add_done_callback(bg_tasks.discard)

    async def worker() -> None:
        while not fatal_crash_flag[0]:
            inbox_wakeup.clear()

            # 1. Transition any expired `queued` items (`now >= deadline`) -> `expired` + outbox reply.
            expired_replies = await loop.run_in_executor(None, journal.expire_queued_inbox)
            if expired_replies:
                _notify_all_pumps()

            # 2. Claim 1 unexpired `queued` item atomically if this node label is an active worker.
            claimed: dict[str, Any] | None = None
            allow_claim = get_mode(a.mode) != "watch" and not fatal_crash_flag[0]
            if allow_claim and _TEST_CRASH_HOOK is not None:
                if _fire_test_crash_hook("before_claim", {"label": label}) is False:
                    allow_claim = False
            if allow_claim:
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

            _broadcast_progress_best_effort(corr_id, sender)

            if _TEST_CRASH_HOOK is not None:
                _fire_test_crash_hook("after_claim_before_exec", claimed)

            try:
                ans, status = await loop.run_in_executor(
                    None, ask_claude, str(claimed["text"]), sender, work_cwd
                )
            except Exception as exc:
                ans, status = f"[agent error] {exc}", "exec_error"

            if _TEST_CRASH_HOOK is not None:
                _fire_test_crash_hook("after_exec_before_save", claimed)

            reply_payload = await loop.run_in_executor(
                None,
                lambda: journal.complete_inbox_and_enqueue_reply(
                    mid,
                    result_status=status,
                    result_text=ans,
                ),
            )

            if _TEST_CRASH_HOOK is not None:
                _fire_test_crash_hook("after_save_before_send", claimed)

            if reply_payload is not None:
                _notify_all_pumps()
                await asyncio.sleep(0)

    async def _run_hub_loop(hub_idx: int, h_state: dict[str, Any]) -> None:
        raw_url = h_state["raw_url"]
        ws_url = h_state["ws_url"]
        canon_url = h_state["canon_url"]
        is_primary = hub_idx == 0
        delay = 0.5
        carry_live_seq = 0
        known_hub_id = journal.resolve_hub_id(raw_url)
        active_hub_key = known_hub_id or canon_url
        h_state["hub_key"] = active_hub_key

        while not fatal_crash_flag[0]:
            ws: Any = None
            pump_task: asyncio.Task[None] | None = None
            inflight: dict[str, asyncio.Future[dict[str, Any]]] = {}
            current_mid_slot: list[str] = []
            reconnect_immediately = False
            try:
                legacy_seq = load_cursor(label, cdir) if is_primary else 0
                hello_since = journal.get_cursor(
                    active_hub_key,
                    label=label,
                    fallback_seq=legacy_seq,
                    use_fallback_when_missing=(
                        is_primary and initial_upgrade_cursor and known_hub_id is None
                    ),
                )
                ws, banner = await _open_ws_with_deadlines(
                    ws_url,
                    build_ws_hello(
                        label,
                        hello_since,
                        accept_inbox=(label == "main"),
                        hub_id=known_hub_id,
                    ),
                    dial_timeout=dial_timeout,
                    hello_timeout=hello_timeout,
                    banner_timeout=banner_timeout,
                )

                # Reset exponential backoff ONLY after a valid connected banner arrives (F15).
                delay = 0.5
                my_node = str(banner["you"]).split("/", 1)[0]
                h_state["my_node"] = my_node
                banner_seq = int(banner.get("seq") or 0)
                raw_caps = banner.get("caps")
                h_state["caps"] = (
                    frozenset(str(c) for c in raw_caps) if isinstance(raw_caps, list) else frozenset()
                )

                replay_count = 0
                last_replay_seq = hello_since
                max_live_seq = carry_live_seq

                if banner.get("hub_id"):
                    banner_hub_id = str(banner["hub_id"])
                    journal.remember_hub_id(raw_url, banner_hub_id)
                    if known_hub_id is not None and banner_hub_id != known_hub_id:
                        # Hub DB recreated with new hub_id (H10) -> cursor starts at 0 & outbox republishes.
                        active_hub_key = banner_hub_id
                        known_hub_id = banner_hub_id
                        hello_since = 0
                        last_replay_seq = 0
                    else:
                        active_hub_key = banner_hub_id
                        if known_hub_id is None and hello_since > 0 and hello_since <= banner_seq:
                            journal.advance_cursor(active_hub_key, hello_since, label=label)
                        known_hub_id = banner_hub_id
                else:
                    # Legacy hub without `hub_id` / `caps` (F16 fallback): key by canonical URL.
                    journal.clear_hub_id(raw_url)
                    known_hub_id = None
                    active_hub_key = canon_url

                h_state["hub_key"] = active_hub_key
                h_state["ws"] = ws
                h_state["send_lock"] = asyncio.Lock()
                h_state["inflight"] = inflight
                h_state["current_mid"] = current_mid_slot

                print(f"[hub:{ws_url}] {banner.get('text')}", flush=True)
                if banner.get("rules"):
                    print(banner["rules"], flush=True)

                h_state["pump_wakeup"].set()
                pump_task = asyncio.create_task(_outbox_pump(h_state, ws))
                pump_tasks.add(pump_task)

                async for raw in ws:
                    m = json.loads(raw)
                    op = m.get("op")
                    if op == "sys":
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

                        print(f"[hub:{ws_url}] {m.get('text')}", flush=True)
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

                    # 1. `!update` control path (inbox commit -> hub ack -> cursor).
                    if raw_text.strip() == "!update":
                        inserted, _ = await loop.run_in_executor(
                            None,
                            lambda m_id=mid, s_from=sender, c_id=corr_id, k_val=kind, r_txt=raw_text, h_val=hop, r_at=received_at, m_dl=msg_deadline: journal.receive_inbox(
                                mid=m_id,
                                sender=s_from,
                                corr_id=c_id,
                                kind=k_val,
                                text=r_txt,
                                hop=h_val,
                                received_at=r_at,
                                deadline=m_dl,
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
                        if is_primary:
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

                    # 2. ACC bridge / local `inbox.jsonl` delivery path (atomic dedup across dual hubs).
                    is_inbox_msg, target_sess = _is_inbox_delivery(m, label)
                    if is_inbox_msg:
                        inserted, _ = await loop.run_in_executor(
                            None,
                            lambda m_id=mid, s_from=sender, c_id=corr_id, k_val=kind, r_txt=raw_text, h_val=hop, r_at=received_at, m_dl=msg_deadline: journal.receive_inbox(
                                mid=m_id,
                                sender=s_from,
                                corr_id=c_id,
                                kind=k_val,
                                text=r_txt,
                                hop=h_val,
                                received_at=r_at,
                                deadline=m_dl,
                                eligible=False,
                            ),
                        )
                        if inserted and received_at < msg_deadline:
                            deliver_to_inbox(m, cfg_dir=cdir, target_session=target_sess)
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
                        if is_primary:
                            save_cursor(label, watermark, cdir)
                        if is_replay and replay_count >= 500 and last_replay_seq > hello_since and last_replay_seq < banner_seq:
                            carry_live_seq = max_live_seq
                            reconnect_immediately = True
                            break
                        continue

                    # 3. Standard request/watch path:
                    #    Step A: Commit into `journal.db` `inbox` (`queued` vs `skipped`).
                    #    Step B: Send hub `ack` ONLY after `inbox` transaction commits.
                    #    Step C: Advance monotonic cursor watermark for this hub.
                    mode_now = get_mode(a.mode)
                    self_key = f"{my_node}/{label}".lower() if my_node else ""
                    not_self = not (self_key and sender.lower() == self_key)
                    in_status = str(m.get("status") or "ok").strip().lower()
                    in_rkind = str(m.get("rkind") or "").strip().lower()
                    eligible = (
                        not_self
                        and (in_rkind != "result")
                        and (in_status not in _TERMINAL_ERROR_STATUSES)
                        and (hop + 1 <= MAX_HOP)
                        and peer_ok
                        and should_reply(mode_now, raw_text)
                    )
                    stored_text = strip_ask(raw_text) if eligible else raw_text

                    changed, eff_state = await loop.run_in_executor(
                        None,
                        lambda m_id=mid, s_from=sender, c_id=corr_id, k_val=kind, s_txt=stored_text, h_val=hop, r_at=received_at, m_dl=msg_deadline, el=eligible: journal.receive_inbox(
                            mid=m_id,
                            sender=s_from,
                            corr_id=c_id,
                            kind=k_val,
                            text=s_txt,
                            hop=h_val,
                            received_at=r_at,
                            deadline=m_dl,
                            eligible=el,
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
                    if is_primary:
                        save_cursor(label, watermark, cdir)

                    if changed and eff_state == "queued":
                        inbox_wakeup.set()

                    if _TEST_CRASH_HOOK is not None and changed and eff_state == "queued":
                        _fire_test_crash_hook("after_inbox_ack_before_claim", {"id": mid, "seq": seq})

                    if is_replay and replay_count >= 500 and last_replay_seq > hello_since and last_replay_seq < banner_seq:
                        carry_live_seq = max_live_seq
                        reconnect_immediately = True
                        break
                else:
                    carry_live_seq = 0

                if not reconnect_immediately and max_live_seq > last_replay_seq and replay_count < 500:
                    journal.advance_cursor(active_hub_key, max_live_seq, label=label)
                    if is_primary:
                        save_cursor(label, max_live_seq, cdir)
                    carry_live_seq = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if getattr(exc, "_fatal_node_crash", False):
                    raise
                print(
                    f"[disconnected:{ws_url}: {type(exc).__name__}] retry in {delay:.1f}s",
                    flush=True,
                )
            finally:
                h_state.pop("ws", None)
                h_state.pop("send_lock", None)
                h_state.pop("inflight", None)
                h_state.pop("current_mid", None)
                for fut in list(inflight.values()):
                    if not fut.done():
                        fut.set_exception(ConnectionError("websocket disconnected"))
                inflight.clear()
                if pump_task is not None:
                    pump_tasks.discard(pump_task)
                    pump_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pump_task
                if ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.close()

            if fatal_crash_flag[0]:
                break
            if reconnect_immediately:
                continue
            await asyncio.sleep(delay * (0.5 + random.random()))
            delay = min(delay * 2, MAX_RECONNECT_BACKOFF)

    worker_task = asyncio.create_task(worker())
    worker_task_ref[0] = worker_task

    def _on_child_done(t: asyncio.Task[None]) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            if fatal_exc_slot[0] is None:
                fatal_exc_slot[0] = exc
            _cancel_all_siblings(t)

    worker_task.add_done_callback(_on_child_done)
    for idx, st in enumerate(hub_states):
        ht = asyncio.create_task(_run_hub_loop(idx, st))
        ht.add_done_callback(_on_child_done)
        hub_tasks.append(ht)

    try:
        await asyncio.gather(worker_task, *hub_tasks)
    except asyncio.CancelledError:
        if fatal_exc_slot[0] is not None:
            raise fatal_exc_slot[0]
        raise
    finally:
        for bt in list(bg_tasks):
            bt.cancel()
        for pt in list(pump_tasks):
            pt.cancel()
        for ht in hub_tasks:
            ht.cancel()
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.gather(worker_task, *hub_tasks, *pump_tasks, *bg_tasks, return_exceptions=True)
        worker_session.close()
        journal.close()


def _exit_code_for_rpc_status(status: str) -> int:
    st = str(status or "ok").strip().lower()
    if st == "auth_error":
        return 3
    return 1 if st in _TERMINAL_ERROR_STATUSES else 0


async def oneshot(a: argparse.Namespace) -> int:
    raw_cdir = getattr(a, "config_dir", None)
    cdir = pathlib.Path(raw_cdir).expanduser() if raw_cdir else cfg_dir()
    cfg = load_config(cdir)
    target_urls = get_bus_urls(cfg, explicit_url=getattr(a, "url", None))
    quorum_urls = _resolve_quorum_urls(cdir, target_urls)
    dial_t = get_dial_timeout(getattr(a, "dial_timeout", None), cfg)
    hello_t = get_hello_timeout(getattr(a, "hello_timeout", None), cfg)
    banner_t = get_banner_timeout(getattr(a, "banner_timeout", None), cfg)
    ack_t = get_ack_timeout(getattr(a, "ack_timeout", None), cfg)
    cmd = a.cmd

    # ------------------------------------------------------------------
    # 1. `peers`: parallel query & merge across all configured hubs
    # ------------------------------------------------------------------
    if cmd == "peers":
        async def _query_peers_one(u: str) -> list[str]:
            if is_http_url(u):
                code, data = await asyncio.to_thread(
                    http_request_json,
                    u,
                    "/v1/peers",
                    method="GET",
                    timeout=max(1.0, dial_t + banner_t),
                )
                if code >= 400 or not data.get("ok"):
                    raise RuntimeError(str(data.get("error") or code))
                return [str(p) for p in (data.get("peers") or []) if str(p)]
            ws_url = to_ws_url(u)
            cli_label = f"cli-{uuid.uuid4().hex[:6]}"
            ws, _banner = await _open_ws_with_deadlines(
                ws_url,
                build_ws_hello(cli_label, 10**9, accept_inbox=False, no_replay=True),
                dial_timeout=dial_t,
                hello_timeout=hello_t,
                banner_timeout=banner_t,
            )
            try:
                await asyncio.wait_for(ws.send(json.dumps({"op": "peers"})), timeout=hello_t)
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=ack_t))
                if isinstance(resp.get("peers"), list):
                    return [str(p) for p in resp["peers"] if str(p)]
                txt = str(resp.get("text") or "")
                if txt.startswith("peers="):
                    return [p for p in txt[6:].split(",") if p]
                return []
            finally:
                with contextlib.suppress(Exception):
                    await ws.close()

        outcomes = await asyncio.gather(*(_query_peers_one(u) for u in target_urls), return_exceptions=True)
        merged_peers: set[str] = set()
        errors: list[str] = []
        any_ok = False
        for out in outcomes:
            if isinstance(out, BaseException):
                errors.append(str(out))
            else:
                any_ok = True
                merged_peers.update(out)
        if not any_ok:
            print(f"[error] {'; '.join(errors) or 'no reachable hub'}", file=sys.stderr)
            return 1
        print("peers=" + ",".join(sorted(merged_peers)))
        return 0

    # ------------------------------------------------------------------
    # 2. `result`: parallel query across all hubs, adopt first terminal
    # ------------------------------------------------------------------
    if cmd == "result":
        cid = a.corr_id
        wait = max(0.0, float(getattr(a, "wait", 0) or 0))
        deadline_mono = time.monotonic() + wait

        async def _query_result_once(u: str) -> tuple[str, dict[str, Any]]:
            if is_http_url(u):
                code, data = await asyncio.to_thread(
                    http_request_json,
                    u,
                    f"/v1/rpc/{urllib.parse.quote(cid)}",
                    method="GET",
                    timeout=max(1.0, dial_t + ack_t),
                )
                if code >= 400:
                    return ("http_error", {"error": data.get("error", code), "code": code})
                if data.get("found"):
                    st = str(data.get("status") or "")
                    if st and st not in ("pending", "running"):
                        return ("terminal", data)
                    return ("pending", data)
                return ("not_found", data)

            ws_url = to_ws_url(u)
            cli_label = f"cli-{uuid.uuid4().hex[:6]}"
            ws, _banner = await _open_ws_with_deadlines(
                ws_url,
                build_ws_hello(cli_label, 10**9, accept_inbox=False, no_replay=True),
                dial_timeout=dial_t,
                hello_timeout=hello_t,
                banner_timeout=banner_t,
            )
            try:
                await asyncio.wait_for(
                    ws.send(json.dumps({"op": "rpc_result", "corr_id": cid})),
                    timeout=hello_t,
                )
                res = json.loads(await asyncio.wait_for(ws.recv(), timeout=ack_t))
                if res.get("ok") is False or res.get("error"):
                    return ("http_error", {"error": res.get("error", "error")})
                if res.get("found"):
                    st = str(res.get("status") or "")
                    if st and st not in ("pending", "running"):
                        return ("terminal", res)
                    return ("pending", res)
                return ("not_found", res)
            finally:
                with contextlib.suppress(Exception):
                    await ws.close()

        saw_pending: dict[str, Any] | None = None
        saw_not_found = False
        last_err = ""
        while True:
            tasks = {asyncio.create_task(_query_result_once(u)) for u in target_urls}
            terminal_hit: dict[str, Any] | None = None
            try:
                pending_tasks = set(tasks)
                while pending_tasks:
                    done_set, pending_tasks = await asyncio.wait(
                        pending_tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in done_set:
                        try:
                            tag, payload = t.result()
                        except BaseException as exc:
                            last_err = str(exc)
                            continue
                        if tag == "terminal":
                            terminal_hit = payload
                            break
                        if tag == "pending":
                            saw_pending = payload
                        elif tag == "not_found":
                            saw_not_found = True
                        elif tag == "http_error":
                            last_err = str(payload.get("error") or "error")
                    if terminal_hit is not None:
                        break
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            if terminal_hit is not None:
                print(
                    json.dumps(terminal_hit, ensure_ascii=False)
                    if getattr(a, "json", False)
                    else f"[{terminal_hit.get('sender')}] ({terminal_hit.get('status')}) {terminal_hit.get('text')}"
                )
                return _exit_code_for_rpc_status(str(terminal_hit.get("status") or "ok"))

            if time.monotonic() >= deadline_mono:
                if saw_pending is not None:
                    print(
                        f"[pending] corr_id={cid} status={saw_pending.get('status')}",
                        file=sys.stderr,
                    )
                    return 124
                if saw_not_found:
                    print(f"[not found] corr_id={cid}", file=sys.stderr)
                    return 124
                print(f"[error] {last_err or 'no reachable hub'}", file=sys.stderr)
                return 1

            await asyncio.sleep(min(0.2, max(0.02, deadline_mono - time.monotonic())))

    # ------------------------------------------------------------------
    # 3. `send` (and `send --wait`): Persist to `outbox` BEFORE network,
    #    then publish across all configured hubs in parallel.
    # ------------------------------------------------------------------
    explicit_id = getattr(a, "id", None)
    mid = str(explicit_id).strip() if explicit_id else new_message_id()
    corr_id = str(getattr(a, "corr_id", None) or mid)
    is_update = bool(getattr(a, "update", False))
    is_say = bool(getattr(a, "say", False))
    wait_s = float(getattr(a, "wait", 0) or 0)
    kind = getattr(a, "kind", None) or ("event" if is_say else ("rpc" if wait_s > 0 else "task"))
    explicit_rkind = getattr(a, "rkind", None)
    if explicit_rkind in ("request", "result", "event"):
        rkind = str(explicit_rkind)
    else:
        rkind = "event" if kind == "event" else "request"
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
        "rkind": rkind,
        "to": a.to,
        "text": text,
        "hop": hop,
        "ttl": ttl,
        "deadline": send_deadline,
    }

    with NodeJournal(cdir) as journal:
        saved_payload = journal.enqueue_outbox(
            send_payload,
            created=created_now,
            deadline=send_deadline,
        )
        mid = str(saved_payload["id"])
        corr_id = str(saved_payload.get("corr_id") or mid)

    cli_label = f"cli-{uuid.uuid4().hex[:6]}"

    async def _flush_earlier_ws(ws: Any, hub_key: str) -> None:
        with NodeJournal(cdir) as journal:
            earlier = [
                it
                for it in journal.list_outbox_pending(hub_key=hub_key, limit=33)
                if str(it.get("id")) != mid
            ][:32]
        for item in earlier:
            item_id = str(item["id"])
            try:
                await asyncio.wait_for(ws.send(json.dumps(item, ensure_ascii=False)), timeout=hello_t)
                ack_raw = await asyncio.wait_for(ws.recv(), timeout=min(5.0, ack_t))
                ack_msg = json.loads(ack_raw)
                if ack_msg.get("op") == "sys" and ack_msg.get("ack") == item_id and not ack_msg.get("error"):
                    with NodeJournal(cdir) as journal:
                        journal.mark_outbox_acked(item_id, hub_key, required_hubs=quorum_urls)
                elif str(ack_msg.get("error") or "") in _PERMANENT_REJECT_ERRORS:
                    with NodeJournal(cdir) as journal:
                        journal.mark_outbox_rejected(
                            item_id,
                            hub_key=hub_key,
                            error=str(ack_msg.get("error")),
                        )
                else:
                    break
            except Exception:
                break

    if wait_s <= 0:
        async def _send_no_wait_one(u: str) -> tuple[str, str]:
            canon_u = canonical_hub_url(u)
            if is_http_url(u):
                http_payload = {**saved_payload, "label": cli_label}
                try:
                    code, data = await asyncio.to_thread(
                        http_request_json,
                        u,
                        "/v1/send",
                        method="POST",
                        payload=http_payload,
                        timeout=max(1.0, dial_t + ack_t),
                    )
                except Exception as exc:
                    return ("error", str(exc))
                if code >= 400 or not data.get("ok"):
                    if code == 429 or data.get("error") == "rate":
                        return ("rate", "rate")
                    perm, err_str = _is_permanent_http_reject(code, data)
                    if perm:
                        with NodeJournal(cdir) as journal:
                            journal.mark_outbox_rejected(mid, hub_key=canon_u, error=err_str)
                        return ("rejected", err_str)
                    return ("error", err_str)
                with NodeJournal(cdir) as journal:
                    hid = str(data.get("hub_id") or "").strip()
                    if hid:
                        journal.remember_hub_id(u, hid)
                        eff_key = hid
                    else:
                        eff_key = journal.resolve_hub_id(u) or canon_u
                    journal.mark_outbox_acked(mid, eff_key, required_hubs=quorum_urls)
                print(
                    f"[hub:{u}] ack seq={data.get('seq')} id={mid} delivered={data.get('delivered') or 'queued'}",
                    file=sys.stderr,
                )
                return ("acked", "")

            ws_url = to_ws_url(u)
            ws: Any = None
            try:
                ws, banner = await _open_ws_with_deadlines(
                    ws_url,
                    build_ws_hello(cli_label, 10**9, accept_inbox=False, no_replay=True),
                    dial_timeout=dial_t,
                    hello_timeout=hello_t,
                    banner_timeout=banner_t,
                )
                with NodeJournal(cdir) as journal:
                    if banner.get("hub_id"):
                        hub_key = str(banner["hub_id"])
                        journal.remember_hub_id(u, hub_key)
                    else:
                        journal.clear_hub_id(u)
                        hub_key = canon_u

                await _flush_earlier_ws(ws, hub_key)
                await asyncio.wait_for(
                    ws.send(json.dumps(saved_payload, ensure_ascii=False)),
                    timeout=hello_t,
                )
                ack_deadline = time.monotonic() + min(5.0, ack_t)
                while True:
                    rem = max(0.05, ack_deadline - time.monotonic())
                    m = json.loads(await asyncio.wait_for(ws.recv(), rem))
                    if m.get("op") != "sys":
                        continue
                    print(f"[hub:{ws_url}] {m.get('text')}", file=sys.stderr)
                    err = str(m.get("error") or "")
                    if err:
                        if err in _PERMANENT_REJECT_ERRORS:
                            with NodeJournal(cdir) as journal:
                                journal.mark_outbox_rejected(mid, hub_key=hub_key, error=err)
                            return ("rejected", err)
                        if err == "rate":
                            return ("rate", "rate")
                        return ("error", err)
                    if m.get("ack") == mid:
                        with NodeJournal(cdir) as journal:
                            journal.mark_outbox_acked(mid, hub_key, required_hubs=quorum_urls)
                        return ("acked", "")
            except Exception as exc:
                return ("error", str(exc))
            finally:
                if ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.close()

        results = await asyncio.gather(*(_send_no_wait_one(u) for u in target_urls))
        acked_cnt = sum(1 for st, _ in results if st == "acked")
        rejected_errs = [msg for st, msg in results if st == "rejected"]
        other_errs = [msg for st, msg in results if st in ("rate", "error") and msg]

        if acked_cnt >= 1 and not rejected_errs:
            return 0
        if rejected_errs and acked_cnt == 0:
            print(f"[error] {rejected_errs[0]}", file=sys.stderr)
            return 1
        if acked_cnt >= 1:
            return 0
        err_summary = "; ".join(other_errs) or "send failed"
        if getattr(a, "offline_queue", False):
            print(f"[queued-offline] id={mid} err={err_summary}", file=sys.stderr)
            return 0
        print(f"[error] {err_summary}", file=sys.stderr)
        return 1

    # `wait_s > 0`: Send to all hubs in parallel AND wait/poll for first terminal result!
    loop = asyncio.get_running_loop()
    start_mono = time.monotonic()
    end_mono_slot = [start_mono + wait_s]
    hard_end_mono = start_mono + (max(wait_s, 300.0) if wait_s >= 30.0 else wait_s)
    terminal_fut: asyncio.Future[tuple[str, str, str]] = loop.create_future()
    acked_hubs_count = [0]
    rejected_errors: list[str] = []
    transient_errors: list[str] = []

    def _extend_wait_on_progress() -> None:
        end_mono_slot[0] = min(hard_end_mono, max(end_mono_slot[0], time.monotonic() + 60.0))

    async def _send_and_wait_one(u: str, send_phase_done: asyncio.Event) -> None:
        canon_u = canonical_hub_url(u)
        if is_http_url(u):
            http_payload = {**saved_payload, "label": cli_label}
            try:
                code, data = await asyncio.to_thread(
                    http_request_json,
                    u,
                    "/v1/send",
                    method="POST",
                    payload=http_payload,
                    timeout=max(1.0, dial_t + ack_t),
                )
                if code >= 400 or not data.get("ok"):
                    if code == 429 or data.get("error") == "rate":
                        transient_errors.append("rate")
                        return
                    perm, err_str = _is_permanent_http_reject(code, data)
                    if perm:
                        with NodeJournal(cdir) as journal:
                            journal.mark_outbox_rejected(mid, hub_key=canon_u, error=err_str)
                        rejected_errors.append(err_str)
                    else:
                        transient_errors.append(err_str)
                    return
                with NodeJournal(cdir) as journal:
                    hid = str(data.get("hub_id") or "").strip()
                    if hid:
                        journal.remember_hub_id(u, hid)
                        eff_key = hid
                    else:
                        eff_key = journal.resolve_hub_id(u) or canon_u
                    journal.mark_outbox_acked(mid, eff_key, required_hubs=quorum_urls)
                acked_hubs_count[0] += 1
                print(
                    f"[hub:{u}] ack seq={data.get('seq')} id={mid} delivered={data.get('delivered') or 'queued'}",
                    file=sys.stderr,
                )
            except Exception as exc:
                transient_errors.append(str(exc))
                return
            finally:
                send_phase_done.set()

            while time.monotonic() < end_mono_slot[0] and not terminal_fut.done():
                await asyncio.sleep(min(0.25, max(0.04, end_mono_slot[0] - time.monotonic())))
                if terminal_fut.done():
                    return
                try:
                    rc, res = await asyncio.to_thread(
                        http_request_json,
                        u,
                        f"/v1/rpc/{urllib.parse.quote(corr_id)}",
                        method="GET",
                        timeout=max(0.5, min(5.0, end_mono_slot[0] - time.monotonic() + 0.5)),
                    )
                except Exception:
                    continue
                if rc == 200 and res.get("found"):
                    st = str(res.get("status") or "ok")
                    if st in ("pending", "running"):
                        if st == "running":
                            _extend_wait_on_progress()
                        continue
                    if not terminal_fut.done():
                        terminal_fut.set_result(
                            (str(res.get("sender") or ""), str(res.get("text") or ""), st)
                        )
                    return
            return

        ws_url = to_ws_url(u)
        ws: Any = None
        acked_this_hub = False
        try:
            ws, banner = await _open_ws_with_deadlines(
                ws_url,
                build_ws_hello(cli_label, 10**9, accept_inbox=False, no_replay=True),
                dial_timeout=dial_t,
                hello_timeout=hello_t,
                banner_timeout=banner_t,
            )
            with NodeJournal(cdir) as journal:
                if banner.get("hub_id"):
                    hub_key = str(banner["hub_id"])
                    journal.remember_hub_id(u, hub_key)
                else:
                    journal.clear_hub_id(u)
                    hub_key = canon_u

            await _flush_earlier_ws(ws, hub_key)
            await asyncio.wait_for(
                ws.send(json.dumps(saved_payload, ensure_ascii=False)),
                timeout=hello_t,
            )

            next_rpc_poll = time.monotonic() + 0.35
            while time.monotonic() < end_mono_slot[0] and not terminal_fut.done():
                rem_total = end_mono_slot[0] - time.monotonic()
                if rem_total <= 0:
                    break
                slice_t = min(0.35, max(0.04, rem_total))
                try:
                    raw_frame = await asyncio.wait_for(ws.recv(), timeout=slice_t)
                except asyncio.TimeoutError:
                    send_phase_done.set()
                    if time.monotonic() >= next_rpc_poll and not terminal_fut.done():
                        next_rpc_poll = time.monotonic() + 0.4
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                ws.send(json.dumps({"op": "rpc_result", "corr_id": corr_id})),
                                timeout=hello_t,
                            )
                    continue

                m = json.loads(raw_frame)
                op = m.get("op")
                if op == "sys":
                    print(f"[hub:{ws_url}] {m.get('text')}", file=sys.stderr)
                    err = str(m.get("error") or "")
                    if err:
                        if err in _PERMANENT_REJECT_ERRORS:
                            with NodeJournal(cdir) as journal:
                                journal.mark_outbox_rejected(mid, hub_key=hub_key, error=err)
                            rejected_errors.append(err)
                        else:
                            transient_errors.append(err)
                        send_phase_done.set()
                        return
                    if m.get("ack") == mid and not acked_this_hub:
                        acked_this_hub = True
                        acked_hubs_count[0] += 1
                        with NodeJournal(cdir) as journal:
                            journal.mark_outbox_acked(mid, hub_key, required_hubs=quorum_urls)
                        send_phase_done.set()
                elif op == "progress" and m.get("corr_id") == corr_id:
                    print(f"[progress] {m.get('from')} status={m.get('status')}", file=sys.stderr)
                    _extend_wait_on_progress()
                elif op == "msg":
                    m_cid = m.get("corr_id")
                    if m_cid and m_cid != corr_id:
                        continue
                    if not getattr(a, "wait_first", False) and m_cid != corr_id:
                        continue
                    if not acked_this_hub:
                        acked_this_hub = True
                        acked_hubs_count[0] += 1
                        with NodeJournal(cdir) as journal:
                            journal.mark_outbox_acked(mid, hub_key, required_hubs=quorum_urls)
                        send_phase_done.set()
                    st = str(m.get("status") or "ok")
                    if st in ("pending", "running"):
                        _extend_wait_on_progress()
                        continue
                    if not terminal_fut.done():
                        terminal_fut.set_result(
                            (str(m.get("from") or ""), str(m.get("text") or ""), st)
                        )
                    return
                elif op == "rpc_result" and m.get("found"):
                    st = str(m.get("status") or "ok")
                    if st in ("pending", "running"):
                        if st == "running":
                            _extend_wait_on_progress()
                        continue
                    if not terminal_fut.done():
                        terminal_fut.set_result(
                            (str(m.get("sender") or ""), str(m.get("text") or ""), st)
                        )
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            transient_errors.append(str(exc))
        finally:
            send_phase_done.set()
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()

    phase_events = [asyncio.Event() for _ in target_urls]
    hub_wait_tasks = [
        asyncio.create_task(_send_and_wait_one(u, ev))
        for u, ev in zip(target_urls, phase_events)
    ]
    all_hubs_done = asyncio.gather(*hub_wait_tasks, return_exceptions=True)

    try:
        await asyncio.wait(
            {terminal_fut, asyncio.ensure_future(all_hubs_done)},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if terminal_fut.done():
            # Give fast healthy sibling hubs up to 50ms to finish recording their send ACK
            # without waiting on a hung/dead hub.
            unfinished_sends = [asyncio.create_task(ev.wait()) for ev in phase_events if not ev.is_set()]
            if unfinished_sends:
                _, still_pending = await asyncio.wait(unfinished_sends, timeout=0.05)
                for p in still_pending:
                    p.cancel()
                await asyncio.gather(*unfinished_sends, return_exceptions=True)
            sender_str, reply_txt, reply_status = terminal_fut.result()
            print(f"[{sender_str}] {reply_txt}")
            return _exit_code_for_rpc_status(reply_status)
    finally:
        for t in hub_wait_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*hub_wait_tasks, return_exceptions=True)

    if rejected_errors and acked_hubs_count[0] == 0:
        print(f"[error] {rejected_errors[0]}", file=sys.stderr)
        return 1
    if acked_hubs_count[0] == 0:
        err_msg = "; ".join(transient_errors) or "no reachable hub"
        if getattr(a, "offline_queue", False):
            print(f"[queued-offline] id={mid} err={err_msg}", file=sys.stderr)
            return 0
        print(f"[error] {err_msg}", file=sys.stderr)
        return 1

    print(f"TIMEOUT_PENDING corr_id={corr_id} (retrieve via: abus result {corr_id})", file=sys.stderr)
    return 124


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
    raw_cdir = getattr(a, "config_dir", None)
    cdir = pathlib.Path(raw_cdir).expanduser() if raw_cdir else cfg_dir()
    cfg = load_config(cdir)
    urls = get_bus_urls(cfg, explicit_url=getattr(a, "url", None))
    dial_t = get_dial_timeout(getattr(a, "dial_timeout", None), cfg)
    hello_t = get_hello_timeout(getattr(a, "hello_timeout", None), cfg)
    banner_t = get_banner_timeout(getattr(a, "banner_timeout", None), cfg)

    with NodeJournal(cdir) as j:
        j_summary = j.status_summary(expire_outbox_now=True)

    report: dict[str, Any] = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "url": urls[0],
        "urls": urls,
        "config_dir": str(cdir),
        "mode": get_mode(),
        "cursor_main": load_cursor("main", cdir),
        "outbox_pending": j_summary["outbox"]["pending"],
        "outbox_rejected": j_summary["outbox"].get("rejected", 0),
        "journal_uncertain": j_summary["inbox"]["uncertain"],
        "inbox_unread": len(read_inbox(cdir, include_acked=False)),
        "hub_ok": False,
        "all_hubs_ok": False,
        "hubs": [],
        "claude_ok": False,
        "gh_ok": False,
    }

    async def _probe_one_hub(u: str) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "url": u,
            "ok": False,
            "hub_id": None,
            "caps": None,
            "max_seq": None,
        }
        try:
            if is_http_url(u):
                code, data = await asyncio.to_thread(
                    http_request_json,
                    u,
                    "/v1/health",
                    method="GET",
                    timeout=dial_t,
                )
                ok = code == 200 and bool(data.get("ok"))
                entry["ok"] = ok
                if data.get("hub_id") is not None:
                    entry["hub_id"] = data["hub_id"]
                if data.get("caps") is not None:
                    entry["caps"] = data["caps"]
                seq_val = data.get("max_seq", data.get("seq"))
                if seq_val is not None:
                    entry["max_seq"] = int(seq_val)
                if not ok:
                    entry["error"] = str(data.get("error") or f"http_{code}")
                return entry

            ws_url = to_ws_url(u)
            ws, banner = await _open_ws_with_deadlines(
                ws_url,
                build_ws_hello(
                    f"doc-{uuid.uuid4().hex[:6]}",
                    10**9,
                    accept_inbox=False,
                    no_replay=True,
                ),
                dial_timeout=dial_t,
                hello_timeout=hello_t,
                banner_timeout=banner_t,
            )
            try:
                entry["ok"] = True
                entry["peers"] = list(banner.get("peers") or [])
                entry["hub_id"] = banner.get("hub_id")
                entry["caps"] = banner.get("caps")
                if banner.get("seq") is not None:
                    entry["max_seq"] = int(banner["seq"])
                return entry
            finally:
                with contextlib.suppress(Exception):
                    await ws.close()
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            return entry

    async def _probe_all() -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(_probe_one_hub(u) for u in urls)))

    try:
        asyncio.get_running_loop()
        in_running_loop = True
    except RuntimeError:
        in_running_loop = False

    if in_running_loop:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            hubs_res = ex.submit(lambda: asyncio.run(_probe_all())).result()
    else:
        hubs_res = asyncio.run(_probe_all())

    report["hubs"] = hubs_res
    report["hub_ok"] = any(bool(h.get("ok")) for h in hubs_res)
    report["all_hubs_ok"] = bool(hubs_res) and all(bool(h.get("ok")) for h in hubs_res)

    merged_peers: set[str] = set()
    first_ok = next((h for h in hubs_res if h.get("ok")), None)
    for h in hubs_res:
        if isinstance(h.get("peers"), list):
            merged_peers.update(str(p) for p in h["peers"] if str(p))
    if first_ok is not None:
        if merged_peers or "peers" in first_ok:
            report["peers"] = sorted(merged_peers)
        if first_ok.get("hub_id") is not None:
            report["hub_id"] = first_ok["hub_id"]
        if first_ok.get("caps") is not None:
            report["caps"] = first_ok["caps"]
        if first_ok.get("max_seq") is not None:
            report["hub_seq"] = first_ok["max_seq"]
    else:
        errs = [f"{h['url']}: {h.get('error', 'unreachable')}" for h in hubs_res]
        report["hub_error"] = "; ".join(errs)

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
