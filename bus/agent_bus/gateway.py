"""Dual-Auth HTTP + WebSocket Gateway for Tailscale Funnel (pure asyncio+websockets + optional FastAPI)."""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
from http import HTTPStatus
from typing import Any

import websockets
import websockets.frames
import websockets.server

from .auth import authenticate_http_request
from .config import LEASE_TTL
from .hub import BusHub


def _normalize_path(raw_path: str) -> tuple[str, dict[str, str]]:
    parsed = urllib.parse.urlsplit(raw_path)
    path = parsed.path or "/"
    if path.startswith("/bus/"):
        path = path[4:]
    elif path == "/bus":
        path = "/"
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
    params = {k: v[-1] for k, v in qs.items() if v}
    return path, params


def _sanitize_label(raw: str | None) -> str:
    val = "".join(c for c in str(raw or "main") if c.isalnum() or c in "-_")[:24]
    return val or "main"


def _log_http_auth_reject(remote_ip: str, path: str, auth: Any) -> None:
    who_part = f" whois={auth.node}" if getattr(auth, "node", "") else ""
    tok_part = f" token_id={auth.token_id}" if getattr(auth, "token_id", "") else ""
    print(
        f"[auth-reject] surface=gw-http ip={remote_ip} path={path}{who_part}{tok_part} "
        f"code={auth.code} reason={auth.error}",
        file=sys.stderr,
        flush=True,
    )


async def handle_gateway_http(
    hub: BusHub,
    *,
    method: str,
    raw_path: str,
    headers: dict[str, str],
    raw_body: bytes,
    remote_ip: str,
) -> tuple[int, dict[str, Any]]:
    path, params = _normalize_path(raw_path)
    method = method.upper()

    if method == "GET" and path == "/v1/health":
        return 200, {"ok": True, "seq": hub.store.get_max_seq()}

    auth = await authenticate_http_request(
        remote_ip=remote_ip,
        headers=headers,
        raw_body=raw_body,
        store=hub.store,
        cfg_dir=hub.cfg_dir,
    )
    if not auth.ok:
        _log_http_auth_reject(remote_ip, path, auth)
        return auth.code or 401, {"ok": False, "error": auth.error}

    if method == "POST" and path == "/v1/send":
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            return 400, {"ok": False, "error": "invalid json"}
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "body must be json object"}
        label = _sanitize_label(body.get("label") or body.get("from_label"))
        sender_key = f"{auth.node}/{label}"
        res = await hub.publish_message(auth=auth, sender_key=sender_key, payload=body)
        if not res.get("ok"):
            return int(res.get("code") or 400), {"ok": False, "error": res.get("detail") or res.get("error")}
        return 200, res

    if method == "GET" and path == "/v1/poll":
        label = _sanitize_label(params.get("label"))
        since = int(params.get("since") or 0)
        limit = min(500, max(1, int(params.get("limit") or 50)))
        consumer = f"{auth.node}/{label}"
        msgs = hub.store.poll_for_consumer(
            node=auth.node,
            consumer=consumer,
            since=since,
            limit=limit,
            trust=auth.trust,
            lease_ttl=LEASE_TTL,
            reclaim_on_reconnect=False,
        )
        return 200, {"ok": True, "consumer": consumer, "messages": msgs, "seq": hub.store.get_max_seq()}

    if method == "POST" and path == "/v1/ack":
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            return 400, {"ok": False, "error": "invalid json"}
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "body must be json object"}
        label = _sanitize_label(body.get("label"))
        seqs: list[int] = []
        if isinstance(body.get("seqs"), list):
            seqs.extend(int(s) for s in body["seqs"])
        if body.get("seq") is not None:
            seqs.append(int(body["seq"]))
        acked = hub.store.ack_deliveries(f"{auth.node}/{label}", seqs)
        return 200, {"ok": True, "acked": acked}

    if method == "GET" and path.startswith("/v1/rpc/"):
        corr_id = urllib.parse.unquote(path[len("/v1/rpc/"):]).strip()
        if not corr_id:
            return 400, {"ok": False, "error": "missing corr_id"}
        res = hub.query_rpc_result(auth, corr_id)
        code = int(res.pop("code", 200))
        return code, res

    if method == "GET" and path == "/v1/peers":
        return 200, {"ok": True, "peers": hub.visible_peers(auth)}

    return 404, {"ok": False, "error": f"not found: {method} {path}"}


async def _serve_ws_over_stream(
    hub: BusHub,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    initial_bytes: bytes,
    remote_ip: str,
    ws_path: str,
    headers: dict[str, str],
) -> None:
    proto = websockets.server.ServerProtocol()
    proto.receive_data(initial_bytes)
    events = proto.events_received()
    if not events or not isinstance(events[0], websockets.http11.Request):
        print(
            f"[handshake-error] surface=gw-ws ip={remote_ip} path={ws_path} reason=invalid_ws_upgrade_request",
            file=sys.stderr,
            flush=True,
        )
        writer.close()
        return

    resp = proto.accept(events[0])
    proto.send_response(resp)
    for chunk in proto.data_to_send():
        if chunk:
            writer.write(chunk)
    await writer.drain()
    if resp.status_code != 101:
        print(
            f"[handshake-error] surface=gw-ws ip={remote_ip} path={ws_path} status={resp.status_code} reason={resp.reason_phrase}",
            file=sys.stderr,
            flush=True,
        )
        writer.close()
        return

    msg_queue: asyncio.Queue[str | None] = asyncio.Queue()
    send_lock = asyncio.Lock()
    closed = False
    frag_buf: list[str | bytes] = []
    frag_is_text = True

    async def flush_writes() -> None:
        async with send_lock:
            chunks = [c for c in proto.data_to_send() if c]
            if chunks:
                for chunk in chunks:
                    writer.write(chunk)
                await writer.drain()

    async def pump_reader() -> None:
        nonlocal closed, frag_is_text
        Opcode = websockets.frames.Opcode
        try:
            while not closed:
                data = await reader.read(65536)
                if not data:
                    proto.receive_eof()
                    break
                proto.receive_data(data)
                for ev in proto.events_received():
                    if isinstance(ev, websockets.frames.Frame):
                        if ev.opcode == Opcode.TEXT:
                            txt = ev.data.decode("utf-8", errors="replace")
                            if ev.fin:
                                await msg_queue.put(txt)
                            else:
                                frag_is_text = True
                                frag_buf.append(txt)
                        elif ev.opcode == Opcode.BINARY:
                            if ev.fin:
                                await msg_queue.put(ev.data.decode("utf-8", errors="replace"))
                            else:
                                frag_is_text = False
                                frag_buf.append(ev.data)
                        elif ev.opcode == Opcode.CONT:
                            part = ev.data.decode("utf-8", errors="replace") if frag_is_text else ev.data
                            frag_buf.append(part)
                            if ev.fin:
                                if frag_is_text:
                                    await msg_queue.put("".join(str(x) for x in frag_buf))
                                else:
                                    await msg_queue.put(b"".join(bytes(x) for x in frag_buf).decode("utf-8", errors="replace"))
                                frag_buf.clear()
                        elif ev.opcode == Opcode.PING:
                            proto.send_pong(ev.data)
                            await flush_writes()
                        elif ev.opcode == Opcode.CLOSE:
                            await flush_writes()
                            closed = True
                            break
                await flush_writes()
        except Exception:
            pass
        finally:
            await msg_queue.put(None)

    async def recv_text() -> str:
        item = await msg_queue.get()
        if item is None:
            raise EOFError("websocket closed")
        return item

    async def send_text(txt: str) -> None:
        if closed:
            raise EOFError("websocket closed")
        proto.send_text(txt.encode("utf-8"))
        await flush_writes()

    async def close_conn(code: int, reason: str) -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            proto.send_close(code, reason)
            await flush_writes()
        except Exception:
            pass
        try:
            writer.close()
        except Exception:
            pass

    reader_task = asyncio.create_task(pump_reader())
    try:
        await hub.handle_ws_session(
            remote_ip=remote_ip,
            headers=headers,
            recv_text=recv_text,
            send_text=send_text,
            close_conn=close_conn,
            is_gateway=True,
            ws_path=ws_path,
        )
    finally:
        closed = True
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass
        try:
            writer.close()
        except Exception:
            pass


async def _handle_tcp_client(
    hub: BusHub,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    peername = writer.get_extra_info("peername")
    remote_ip = peername[0] if isinstance(peername, tuple) and peername else ""
    try:
        raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10.0)
    except Exception:
        writer.close()
        return

    try:
        head_text = raw_head.decode("iso-8859-1")
        lines = head_text.split("\r\n")
        req_line = lines[0].split(" ", 2)
        if len(req_line) < 2:
            writer.close()
            return
        method, raw_path = req_line[0], req_line[1]
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()

        lower_h = {k.lower(): v for k, v in headers.items()}
        norm_path, _ = _normalize_path(raw_path)

        if lower_h.get("upgrade", "").lower() == "websocket" and norm_path == "/v1/ws":
            await _serve_ws_over_stream(
                hub,
                reader,
                writer,
                raw_head,
                remote_ip,
                urllib.parse.urlsplit(raw_path).path or "/bus/v1/ws",
                headers,
            )
            return

        content_len = int(lower_h.get("content-length") or 0)
        if content_len > 2 * 1024 * 1024:
            await _write_http_json(writer, 413, {"ok": False, "error": "payload too large"})
            return
        raw_body = await asyncio.wait_for(reader.readexactly(content_len), 10.0) if content_len > 0 else b""

        status, payload = await handle_gateway_http(
            hub,
            method=method,
            raw_path=raw_path,
            headers=headers,
            raw_body=raw_body,
            remote_ip=remote_ip,
        )
        await _write_http_json(writer, status, payload)
    except Exception as exc:
        try:
            await _write_http_json(writer, 500, {"ok": False, "error": str(exc)})
        except Exception:
            pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _write_http_json(writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = "OK"
    head = (
        f"HTTP/1.1 {status} {phrase}\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii")
    writer.write(head + body)
    await writer.drain()


async def start_gateway_server(hub: BusHub, host: str, port: int) -> asyncio.AbstractServer:
    return await asyncio.start_server(lambda r, w: _handle_tcp_client(hub, r, w), host, port)


def create_fastapi_app(hub: BusHub) -> Any:
    """Optional FastAPI ASGI app wrapping the same Hub HTTP/WS handlers (when fastapi is installed)."""
    from fastapi import FastAPI, Request, WebSocket
    from fastapi.responses import JSONResponse

    app = FastAPI(title="Agent Bus Gateway", version="0.2.0")

    @app.api_route("/{full_path:path}", methods=["GET", "POST"])
    async def _http_dispatch(request: Request, full_path: str) -> JSONResponse:
        raw_body = await request.body()
        raw_path = "/" + full_path
        if request.url.query:
            raw_path += "?" + request.url.query
        remote_ip = request.client.host if request.client else ""
        status, payload = await handle_gateway_http(
            hub,
            method=request.method,
            raw_path=raw_path,
            headers=dict(request.headers),
            raw_body=raw_body,
            remote_ip=remote_ip,
        )
        return JSONResponse(status_code=status, content=payload)

    @app.websocket("/bus/v1/ws")
    @app.websocket("/v1/ws")
    async def _ws_dispatch(websocket: WebSocket) -> None:
        await websocket.accept()
        remote_ip = websocket.client.host if websocket.client else ""
        headers = dict(websocket.headers)

        async def recv_text() -> str:
            return await websocket.receive_text()

        async def send_text(txt: str) -> None:
            await websocket.send_text(txt)

        async def close_conn(code: int, reason: str) -> None:
            await websocket.close(code=code, reason=reason)

        await hub.handle_ws_session(
            remote_ip=remote_ip,
            headers=headers,
            recv_text=recv_text,
            send_text=send_text,
            close_conn=close_conn,
            is_gateway=True,
            ws_path=websocket.url.path,
        )

    return app
