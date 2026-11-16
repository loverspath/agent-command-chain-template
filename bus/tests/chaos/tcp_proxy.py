"""Asyncio bidirectional TCP fault-injection proxy (`127.0.0.1:18785` family).

Supports runtime fault controls for chaos tests (I0 and X01–X17):
1. `block` / `unblock`:
   - `mode="reject"` (default): immediately drops/closes new TCP connections and stops
     in-flight byte forwarding.
   - `mode="blackhole"`: accepts TCP handshake (leaves socket open) while silently
     withholding/discarding bytes (useful for X11 banner hang / half-open tests).
2. `set_delay` / `clear_delay`: symmetric or directional (`client_to_upstream`,
   `upstream_to_client`) byte-forwarding latency injection.
3. `cut` (`disconnect_all`): immediately terminates all open active TCP sessions
   (with optional `SO_LINGER(1, 0)` TCP RST) and supports live upstream target switching
   (e.g., `18765` -> `18775`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import socket
import struct
from typing import Any

from .hub_proc import HUB_A_PORT, HUB_HOST, validate_sandbox_port

PROXY_PORT = 18785
PROXY_B_PORT = 18786


def _force_close_writer(writer: asyncio.StreamWriter, *, rst: bool = True) -> None:
    if rst:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except Exception:
                pass
        transport = writer.transport
        if transport is not None:
            try:
                transport.abort()
                return
            except Exception:
                pass
    try:
        writer.close()
    except Exception:
        pass


@dataclasses.dataclass(eq=False)
class _ProxySession:
    session_id: int
    client_reader: asyncio.StreamReader
    client_writer: asyncio.StreamWriter
    upstream_reader: asyncio.StreamReader | None = None
    upstream_writer: asyncio.StreamWriter | None = None
    task: asyncio.Task[Any] | None = None
    closed: bool = False


class TcpFaultProxy:
    """Deterministic `asyncio` loopback TCP fault proxy."""

    def __init__(
        self,
        *,
        listen_port: int = PROXY_PORT,
        upstream_port: int = HUB_A_PORT,
        listen_host: str = HUB_HOST,
        upstream_host: str = HUB_HOST,
    ) -> None:
        validate_sandbox_port(listen_port)
        validate_sandbox_port(upstream_port)
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port

        self._server: asyncio.AbstractServer | None = None
        self._sessions: set[_ProxySession] = set()
        self._next_session_id = 0

        # Fault state
        self._blocked = False
        self._block_mode = "reject"  # "reject" | "blackhole"
        self._unblock_event: asyncio.Event | None = None
        self._delay_c2u = 0.0
        self._delay_u2c = 0.0

        # Telemetry counters
        self.accepted_count = 0
        self.blocked_count = 0
        self.cut_count = 0
        self.bytes_c2u = 0
        self.bytes_u2c = 0

    @property
    def ws_url(self) -> str:
        return f"ws://{self.listen_host}:{self.listen_port}"

    @property
    def http_url(self) -> str:
        return f"http://{self.listen_host}:{self.listen_port}/bus"

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    @property
    def is_blocked(self) -> bool:
        return self._blocked

    async def __aenter__(self) -> "TcpFaultProxy":
        return await self.start()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def start(self) -> "TcpFaultProxy":
        if self._server is not None:
            return self
        self._unblock_event = asyncio.Event()
        if not self._blocked:
            self._unblock_event.set()
        self._server = await asyncio.start_server(
            self._on_client_connected,
            self.listen_host,
            self.listen_port,
            reuse_address=True,
        )
        return self

    def block(
        self,
        enabled: bool = True,
        *,
        mode: str = "reject",
        cut_existing: bool = False,
    ) -> None:
        """Toggle connection/byte blocking (`mode='reject'` or `mode='blackhole'`)."""
        if mode not in ("reject", "blackhole"):
            raise ValueError(f"Unsupported block mode: {mode!r}")
        self._blocked = bool(enabled)
        self._block_mode = mode
        if self._unblock_event is not None:
            if self._blocked:
                self._unblock_event.clear()
            else:
                self._unblock_event.set()
        if enabled and cut_existing:
            self._cut_nowait(rst=True)

    def unblock(self) -> None:
        self.block(False)

    def set_delay(
        self,
        delay_sec: float = 0.0,
        *,
        client_to_upstream: float | None = None,
        upstream_to_client: float | None = None,
    ) -> None:
        """Configure symmetric or per-direction forwarding delay in seconds."""
        base = max(0.0, float(delay_sec))
        self._delay_c2u = max(0.0, float(client_to_upstream)) if client_to_upstream is not None else base
        self._delay_u2c = max(0.0, float(upstream_to_client)) if upstream_to_client is not None else base

    def clear_delay(self) -> None:
        self.set_delay(0.0)

    def set_upstream(self, port: int, host: str = HUB_HOST, *, cut_existing: bool = False) -> None:
        """Switch upstream target port/host (e.g., 18765 -> 18775)."""
        validate_sandbox_port(port)
        self.upstream_host = host
        self.upstream_port = port
        if cut_existing:
            self._cut_nowait(rst=True)

    async def switch_upstream(self, port: int, host: str = HUB_HOST, *, cut_existing: bool = True) -> int:
        validate_sandbox_port(port)
        self.upstream_host = host
        self.upstream_port = port
        if cut_existing:
            return await self.cut(rst=True)
        return 0

    def _cut_nowait(self, *, rst: bool = True) -> list[_ProxySession]:
        sessions = list(self._sessions)
        self._sessions.clear()
        for sess in sessions:
            if sess.closed:
                continue
            sess.closed = True
            self.cut_count += 1
            _force_close_writer(sess.client_writer, rst=rst)
            if sess.upstream_writer is not None:
                _force_close_writer(sess.upstream_writer, rst=rst)
            if sess.task is not None and not sess.task.done():
                sess.task.cancel()
        return sessions

    async def cut(self, *, rst: bool = True) -> int:
        """Immediately sever all active TCP sessions through this proxy."""
        sessions = self._cut_nowait(rst=rst)
        tasks = [s.task for s in sessions if s.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(sessions)

    disconnect_all = cut

    async def _on_client_connected(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        if self._blocked and self._block_mode == "reject":
            self.blocked_count += 1
            _force_close_writer(client_writer, rst=True)
            return

        self.accepted_count += 1
        self._next_session_id += 1
        sess = _ProxySession(
            session_id=self._next_session_id,
            client_reader=client_reader,
            client_writer=client_writer,
        )
        sess.task = asyncio.current_task()
        self._sessions.add(sess)

        try:
            if self._blocked and self._block_mode == "blackhole":
                self.blocked_count += 1
                assert self._unblock_event is not None
                # Hold client connection silent until unblocked or client disconnects.
                wait_unblock = asyncio.create_task(self._unblock_event.wait())
                wait_eof = asyncio.create_task(client_reader.read(1))
                done, pending = await asyncio.wait(
                    {wait_unblock, wait_eof},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for p in pending:
                    p.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if wait_eof in done:
                    first_byte = wait_eof.result()
                    if not first_byte:
                        return
                else:
                    first_byte = b""
            else:
                first_byte = b""

            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(self.upstream_host, self.upstream_port),
                timeout=3.0,
            )
            sess.upstream_reader = up_reader
            sess.upstream_writer = up_writer

            if first_byte:
                if self._delay_c2u > 0:
                    await asyncio.sleep(self._delay_c2u)
                up_writer.write(first_byte)
                await up_writer.drain()
                self.bytes_c2u += len(first_byte)

            c2u_task = asyncio.create_task(self._pipe(sess, client_reader, up_writer, direction="c2u"))
            u2c_task = asyncio.create_task(self._pipe(sess, up_reader, client_writer, direction="u2c"))
            done, pending = await asyncio.wait(
                {c2u_task, u2c_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
            await asyncio.gather(*pending, *done, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            sess.closed = True
            self._sessions.discard(sess)
            _force_close_writer(client_writer, rst=False)
            if sess.upstream_writer is not None:
                _force_close_writer(sess.upstream_writer, rst=False)

    async def _pipe(
        self,
        sess: _ProxySession,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        direction: str,
    ) -> None:
        try:
            while not sess.closed:
                data = await reader.read(65536)
                if not data:
                    try:
                        if writer.can_write_eof():
                            writer.write_eof()
                    except Exception:
                        pass
                    break

                if self._blocked:
                    self.blocked_count += 1
                    if self._block_mode == "reject":
                        _force_close_writer(writer, rst=True)
                        break
                    # blackhole mode: stall while blocked
                    if self._unblock_event is not None:
                        await self._unblock_event.wait()

                delay = self._delay_c2u if direction == "c2u" else self._delay_u2c
                if delay > 0:
                    await asyncio.sleep(delay)

                writer.write(data)
                await writer.drain()
                if direction == "c2u":
                    self.bytes_c2u += len(data)
                else:
                    self.bytes_u2c += len(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def close(self) -> None:
        """Close the listener and abort/close every open client & upstream socket."""
        srv = self._server
        self._server = None
        if self._unblock_event is not None:
            self._unblock_event.set()
        if srv is not None:
            srv.close()
        await self.cut(rst=True)
        if srv is not None:
            try:
                await srv.wait_closed()
            except Exception:
                pass


class ProxyController:
    """Tracks all `TcpFaultProxy` instances in a test and closes them on teardown."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._proxies: list[TcpFaultProxy] = []

    async def start_proxy(
        self,
        *,
        listen_port: int = PROXY_PORT,
        upstream_port: int = HUB_A_PORT,
        listen_host: str = HUB_HOST,
        upstream_host: str = HUB_HOST,
    ) -> TcpFaultProxy:
        proxy = TcpFaultProxy(
            listen_port=listen_port,
            upstream_port=upstream_port,
            listen_host=listen_host,
            upstream_host=upstream_host,
        )
        self._proxies.append(proxy)
        return await proxy.start()

    def close_all(self) -> None:
        if not self._proxies:
            return
        if not self.loop.is_closed():
            for proxy in reversed(self._proxies):
                try:
                    self.loop.run_until_complete(proxy.close())
                except Exception:
                    proxy._cut_nowait(rst=True)
        self._proxies.clear()
