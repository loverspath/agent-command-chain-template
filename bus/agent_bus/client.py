"""Agent Bus Node Client: Sync RPC, Durable Outbox, ACC Bridge, Supervisor, and Doctor."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import pathlib
import random
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

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


def save_cursor(label: str, seq: int, config_dir: pathlib.Path | None = None) -> None:
    base = config_dir or cfg_dir()
    base.mkdir(parents=True, exist_ok=True)
    cursor_path(label, base).write_text(str(seq), encoding="utf-8")


def outbox_path(config_dir: pathlib.Path | None = None) -> pathlib.Path:
    return (config_dir or cfg_dir()) / "outbox.jsonl"


def append_outbox(payload: dict[str, Any], config_dir: pathlib.Path | None = None) -> None:
    p = outbox_path(config_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Deduplicate by payload id
    mid = payload.get("id")
    if mid and p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                if json.loads(line).get("id") == mid:
                    return
            except Exception:
                continue
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_outbox(config_dir: pathlib.Path | None = None) -> list[dict[str, Any]]:
    p = outbox_path(config_dir)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                out.append(item)
        except Exception:
            continue
    return out


def remove_outbox_id(mid: str, config_dir: pathlib.Path | None = None) -> None:
    p = outbox_path(config_dir)
    if not p.exists():
        return
    kept: list[str] = []
    for item in load_outbox(config_dir):
        if item.get("id") != mid:
            kept.append(json.dumps(item, ensure_ascii=False))
    p.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")


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
    r = subprocess.run(
        [cmd, "-p", "--allowedTools", "Read,Grep,Glob,Bash,Edit,Write"],
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


async def _flush_outbox_over_ws(ws: Any, config_dir: pathlib.Path, ack_events: dict[str, asyncio.Event]) -> None:
    pending = load_outbox(config_dir)
    for item in pending:
        mid = str(item.get("id") or "")
        ev = asyncio.Event()
        if mid:
            ack_events[mid] = ev
        try:
            await ws.send(json.dumps(item, ensure_ascii=False))
            if mid:
                await asyncio.wait_for(ev.wait(), 5.0)
                remove_outbox_id(mid, config_dir)
        except Exception:
            break
        finally:
            if mid:
                ack_events.pop(mid, None)


async def run_node(a: argparse.Namespace) -> None:
    label = a.label
    delay = 1.0
    jobs: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    peers = {p.strip().lower() for p in a.peers.split(",") if p.strip()}
    url = to_ws_url(getattr(a, "url", None) or get_bus_url())
    cdir = cfg_dir()
    work_cwd = pathlib.Path(a.cwd).expanduser() if getattr(a, "cwd", None) else home_dir()
    ack_events: dict[str, asyncio.Event] = {}
    my_node = ""

    async def send_or_outbox(ws_ref: dict[str, Any], payload: dict[str, Any]) -> bool:
        mid = str(payload.get("id") or uuid.uuid4().hex[:12])
        payload["id"] = mid
        ws = ws_ref.get("ws")
        if ws is None:
            append_outbox(payload, cdir)
            return False
        ev = asyncio.Event()
        ack_events[mid] = ev
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
            await asyncio.wait_for(ev.wait(), 10.0)
            return True
        except Exception:
            append_outbox(payload, cdir)
            return False
        finally:
            ack_events.pop(mid, None)

    async def worker(ws_ref: dict[str, Any]) -> None:
        while True:
            m = await jobs.get()
            corr_id = str(m.get("corr_id") or m.get("id") or "")
            ws = ws_ref.get("ws")
            if ws is not None and corr_id:
                try:
                    await ws.send(
                        json.dumps(
                            {
                                "op": "progress",
                                "corr_id": corr_id,
                                "to": m["from"],
                                "status": "running",
                            },
                            ensure_ascii=False,
                        )
                    )
                except Exception:
                    pass

            try:
                ans, status = await asyncio.get_running_loop().run_in_executor(
                    None, ask_claude, m["text"], m["from"], work_cwd
                )
            except Exception as exc:
                ans, status = f"[agent error] {exc}", "exec_error"

            reply_payload = {
                "v": 2,
                "op": "send",
                "id": uuid.uuid4().hex[:12],
                "corr_id": corr_id or None,
                "kind": "rpc" if m.get("kind") == "rpc" else "task",
                "status": status,
                "to": m["from"],
                "text": ans,
                "hop": int(m.get("hop", 0)) + 1,
            }
            await send_or_outbox(ws_ref, reply_payload)

            # Commit cursor and ack AFTER execution and durable reply dispatch.
            seq = int(m["seq"])
            save_cursor(label, seq, cdir)
            ws_now = ws_ref.get("ws")
            if ws_now is not None:
                try:
                    await ws_now.send(json.dumps({"op": "ack", "seq": seq, "id": m.get("id")}))
                except Exception:
                    pass

    if not getattr(a, "no_auto_update", False) and update_repo():
        restart_self()

    ws_ref: dict[str, Any] = {}
    worker_task = asyncio.create_task(worker(ws_ref))
    try:
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps(build_ws_hello(label, load_cursor(label, cdir), accept_inbox=(label == "main"))))
                    ws_ref["ws"] = ws
                    delay = 1.0
                    # Run outbox flush after initial banner processing via background task
                    flush_task: asyncio.Task[None] | None = None
                    async for raw in ws:
                        m = json.loads(raw)
                        op = m.get("op")
                        if op == "sys":
                            if m.get("you"):
                                my_node = str(m["you"]).split("/", 1)[0]
                                if flush_task is None:
                                    flush_task = asyncio.create_task(_flush_outbox_over_ws(ws, cdir, ack_events))
                            if m.get("ack") and m["ack"] in ack_events:
                                ack_events[m["ack"]].set()
                            print(f"[hub] {m.get('text')}", flush=True)
                            if m.get("rules"):
                                print(m["rules"], flush=True)
                            continue

                        if op != "msg":
                            continue

                        seq = int(m["seq"])
                        print(f"[{m['from']}] hop={m.get('hop')} {m['text']}", flush=True)
                        fresh = not m.get("replay") or (time.time() - float(m.get("ts", 0))) < 3600
                        node = str(m["from"]).split("/")[0]
                        peer_ok = (not peers) or (node in peers)

                        if str(m.get("text", "")).strip() == "!update" and fresh and peer_ok and m.get("trust") != "external":
                            save_cursor(label, seq, cdir)
                            try:
                                await ws.send(json.dumps({"op": "ack", "seq": seq, "id": m.get("id")}))
                            except Exception:
                                pass
                            if await asyncio.get_running_loop().run_in_executor(None, update_repo):
                                restart_self()
                            continue

                        is_inbox, target_sess = _is_inbox_delivery(m, label)
                        if is_inbox:
                            deliver_to_inbox(m, cfg_dir=cdir, target_session=target_sess)
                            save_cursor(label, seq, cdir)
                            try:
                                await ws.send(json.dumps({"op": "ack", "seq": seq, "id": m.get("id")}))
                            except Exception:
                                pass
                            continue

                        if fresh and (int(m.get("hop", 0)) + 1 <= MAX_HOP) and peer_ok and should_reply(get_mode(a.mode), str(m.get("text", ""))):
                            await jobs.put({**m, "text": strip_ask(str(m.get("text", "")))})
                        else:
                            save_cursor(label, seq, cdir)
                            try:
                                await ws.send(json.dumps({"op": "ack", "seq": seq, "id": m.get("id")}))
                            except Exception:
                                pass
            except Exception as exc:
                print(f"[disconnected: {type(exc).__name__}] retry in {delay:.1f}s", flush=True)
            ws_ref.pop("ws", None)
            await asyncio.sleep(delay * (0.5 + random.random()))
            delay = min(delay * 2, 60.0)
    finally:
        worker_task.cancel()


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
                    print(json.dumps(data, ensure_ascii=False) if getattr(a, "json", False) else f"[{data.get('sender')}] ({data.get('status')}) {data.get('text')}")
                    if data.get("status") == "auth_error":
                        return 3
                    return 1 if data.get("status") == "exec_error" else 0
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
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(build_ws_hello(cli_label, 10**9, accept_inbox=False)))
                await ws.recv()
                while True:
                    await ws.send(json.dumps({"op": "rpc_result", "corr_id": cid}))
                    res = json.loads(await ws.recv())
                    if res.get("found") and res.get("status") not in ("pending", "running"):
                        print(json.dumps(res, ensure_ascii=False) if getattr(a, "json", False) else f"[{res.get('sender')}] ({res.get('status')}) {res.get('text')}")
                        if res.get("status") == "auth_error":
                            return 3
                        return 1 if res.get("status") == "exec_error" else 0
                    if time.time() >= deadline:
                        print(f"[pending] corr_id={cid}", file=sys.stderr)
                        return 124
                    await asyncio.sleep(0.2)

    if cmd == "send" and is_http_url(url):
        mid = uuid.uuid4().hex[:12]
        corr_id = getattr(a, "corr_id", None) or mid
        is_update = bool(getattr(a, "update", False))
        is_say = bool(getattr(a, "say", False))
        kind = getattr(a, "kind", None) or ("event" if is_say else ("rpc" if float(a.wait) > 0 else "task"))
        text = "!update" if is_update else (a.text if (is_say or kind == "event") else "!ask " + a.text)
        payload = {
            "v": 2,
            "id": mid,
            "corr_id": corr_id,
            "kind": kind,
            "to": a.to,
            "text": text,
            "hop": int(getattr(a, "hop", 0) or 0),
            "ttl": int(getattr(a, "ttl", 86400) or 86400),
            "label": f"cli-{uuid.uuid4().hex[:6]}",
        }
        try:
            code, data = http_request_json(url, "/v1/send", method="POST", payload=payload)
        except Exception as exc:
            if getattr(a, "offline_queue", False):
                append_outbox({"op": "send", **payload}, cdir)
                print(f"[queued-offline] id={mid} err={exc}", file=sys.stderr)
                return 0
            print(f"[error] {exc}", file=sys.stderr)
            return 1
        if code >= 400 or not data.get("ok"):
            print(f"[error] {data.get('error', code)}", file=sys.stderr)
            return 1
        print(f"[hub] ack seq={data.get('seq')} id={mid} delivered={data.get('delivered') or 'queued'}", file=sys.stderr)
        if float(a.wait) <= 0:
            return 0
        # Poll /v1/rpc/{corr_id} until deadline
        wait_f = float(a.wait)
        start_t = time.time()
        deadline = start_t + wait_f
        hard_deadline = start_t + (max(wait_f, 300.0) if wait_f >= 30.0 else wait_f)
        while time.time() < deadline:
            await asyncio.sleep(min(0.5, max(0.05, deadline - time.time())))
            rc, res = http_request_json(url, f"/v1/rpc/{urllib.parse.quote(corr_id)}", method="GET")
            if rc == 200 and res.get("found"):
                st = res.get("status")
                if st in ("pending", "running"):
                    if st == "running":
                        deadline = min(hard_deadline, max(deadline, time.time() + 60.0))
                    continue
                print(f"[{res.get('sender')}] {res.get('text')}")
                if st == "auth_error":
                    return 3
                return 1 if st == "exec_error" else 0
        print(f"TIMEOUT_PENDING corr_id={corr_id} (retrieve via: abus result {corr_id})", file=sys.stderr)
        return 124

    ws_url = to_ws_url(url)
    cli_label = f"cli-{uuid.uuid4().hex[:6]}"
    try:
        async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps(build_ws_hello(cli_label, 10**9, accept_inbox=False)))
            await ws.recv()  # connected banner

            # Flush any queued outbox items from earlier offline runs
            ack_events: dict[str, asyncio.Event] = {}
            if load_outbox(cdir):
                for item in load_outbox(cdir):
                    await ws.send(json.dumps(item, ensure_ascii=False))
                    ack_raw = await asyncio.wait_for(ws.recv(), 5.0)
                    ack_msg = json.loads(ack_raw)
                    if ack_msg.get("ack") == item.get("id"):
                        remove_outbox_id(str(item["id"]), cdir)

            if cmd == "peers":
                await ws.send(json.dumps({"op": "peers"}))
                print(json.loads(await ws.recv())["text"])
                return 0

            mid = uuid.uuid4().hex[:12]
            corr_id = getattr(a, "corr_id", None) or mid
            is_update = bool(getattr(a, "update", False))
            is_say = bool(getattr(a, "say", False))
            wait_s = float(getattr(a, "wait", 0) or 0)
            kind = getattr(a, "kind", None) or ("event" if is_say else ("rpc" if wait_s > 0 else "task"))
            text = "!update" if is_update else (a.text if (is_say or kind == "event") else "!ask " + a.text)
            await ws.send(
                json.dumps(
                    {
                        "v": 2,
                        "op": "send",
                        "id": mid,
                        "corr_id": corr_id,
                        "kind": kind,
                        "to": a.to,
                        "text": text,
                        "hop": int(getattr(a, "hop", 0) or 0),
                        "ttl": int(getattr(a, "ttl", 86400) or 86400),
                    },
                    ensure_ascii=False,
                )
            )
            if wait_s <= 0:
                # Read the immediate hub ack before exiting cleanly.
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
                    if m.get("op") == "sys":
                        print(f"[hub] {m['text']}", file=sys.stderr)
                except asyncio.TimeoutError:
                    pass
                return 0

            start_t = time.time()
            end = start_t + wait_s
            hard_end = start_t + (max(wait_s, 300.0) if wait_s >= 30.0 else wait_s)
            got_reply = False
            last_status = "ok"
            while time.time() < end:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), max(0.1, end - time.time())))
                except asyncio.TimeoutError:
                    break
                op = m.get("op")
                if op == "sys":
                    print(f"[hub] {m.get('text')}", file=sys.stderr)
                    if m.get("error"):
                        return 1
                elif op == "progress" and m.get("corr_id") == corr_id:
                    print(f"[progress] {m.get('from')} status={m.get('status')}", file=sys.stderr)
                    end = min(hard_end, max(end, time.time() + 60.0))
                elif op == "msg":
                    m_cid = m.get("corr_id")
                    if m_cid and m_cid != corr_id:
                        continue
                    print(f"[{m['from']}] {m['text']}")
                    got_reply = True
                    last_status = str(m.get("status") or "ok")
                    if getattr(a, "wait_first", False) or m_cid == corr_id:
                        if last_status == "auth_error":
                            return 3
                        return 1 if last_status == "exec_error" else 0
            if not got_reply:
                print(f"TIMEOUT_PENDING corr_id={corr_id} (retrieve via: abus result {corr_id})", file=sys.stderr)
                return 124
            return 0
    except Exception as exc:
        if cmd == "send" and getattr(a, "offline_queue", False):
            mid = uuid.uuid4().hex[:12]
            is_update = bool(getattr(a, "update", False))
            is_say = bool(getattr(a, "say", False))
            kind = getattr(a, "kind", None) or ("event" if is_say else "task")
            text = "!update" if is_update else (a.text if (is_say or kind == "event") else "!ask " + a.text)
            append_outbox(
                {
                    "v": 2,
                    "op": "send",
                    "id": mid,
                    "corr_id": getattr(a, "corr_id", None) or mid,
                    "kind": kind,
                    "to": a.to,
                    "text": text,
                    "hop": int(getattr(a, "hop", 0) or 0),
                },
                cdir,
            )
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
    report: dict[str, Any] = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "url": url,
        "config_dir": str(cdir),
        "mode": get_mode(),
        "cursor_main": load_cursor("main", cdir),
        "outbox_pending": len(load_outbox(cdir)),
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
