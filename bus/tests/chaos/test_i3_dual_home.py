"""WBS `I3` dual-home client resilience, `X01`, `X11`, dual-receive dedup, `rejected` vs `rate`,
`rkind='result'`, and `abus journal reconcile` CLI tests.

Verifies:
1. `config.get_bus_urls()` resolution (`--url`, `BUS_URLS`, `config.json` `"urls"`, single-`url` fallback).
2. `X01` (`design.md` §6, `sol-review.md` F01):
   - (i) Hub A kill BEFORE Hub A storage.
   - (ii) Hub A kill AFTER Hub A storage (`messages` committed) BEFORE client receives `sys ack`.
   - (iii-a) Hub A kill + permanent DB deletion RIGHT AFTER Hub A ACK (`outbox` `pending`) -> Hub B alone recovers (`loss == 0`).
   - (iii-b) Hub A kill + permanent DB deletion RIGHT AFTER Hub A ACK (`outbox` `pending`) -> empty Hub A restarts on same URL (`H10` new `hub_id`) -> republishes (`loss == 0`).
   - (iv) Both hubs ACK (`outbox` `done`) -> Hub A kill + permanent DB deletion -> surviving Hub B delivers (`loss == 0`).
   - Global invariant: `0` lost items and `SideEffectCounter.count(id) == 1` (`<= 1`) for every request.
3. `X11` (`design.md` §6, `sol-review.md` F15, F16):
   - Fake banner-hanging hub (completes TCP + WS handshake, reads `hello`, never sends banner) + healthy Hub B:
     `run_node` and `oneshot` (`send`, `send --wait`, `result`, `peers`, `doctor`) isolate the hung hub within deadline and operate smoothly via Hub B.
   - Mixed unmodified legacy `e7d0da2` client + v2 dual-home client AND unmodified legacy `e7d0da2` hub (`caps` & `hub_id` absent in banner -> `caps` fallback to URL key) + v2 hub.
4. Simultaneous dual-hub delivery deduplication (`SideEffectCounter == 1`, both hubs ACKed, independent cursors, primary-only `cursor-main`).
5. Permanent rejection (`id_conflict`, `forbidden`, `size`, `hop` -> `state='rejected'`, retry halted) vs `rate` rejection (`state='pending'`, backoff retry), explicit `rkind='result'` across all 3 worker reply types (`ok`/`exec_error`, `exec_uncertain`, `expired`), pre-I3 SQLite `outbox` migration, and `abus journal reconcile --hubs <keys>`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import pathlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Any

import pytest
import websockets

from agent_bus import auth as bus_auth
from agent_bus import client as bus_client
from agent_bus import config as bus_config
from agent_bus.journal import NodeJournal, canonical_hub_url
from .conftest import NodeRunner
from .hub_proc import (
    BUS_ROOT,
    HUB_A_PORT,
    HUB_B_PORT,
    HubController,
    is_port_listening,
    pick_free_port,
    write_claude_guard_script,
)
from .oracle import ChaosOracle, make_ask_prompt
from .tcp_proxy import PROXY_PORT, ProxyController

WORKTREE_ROOT = BUS_ROOT.parent
LEGACY_COMMIT = "e7d0da2"
LEGACY_MODULE_FILES = (
    "__init__.py",
    "auth.py",
    "bridge.py",
    "cli.py",
    "client.py",
    "config.py",
    "gateway.py",
    "hub.py",
    "store.py",
)


async def _wait_until(predicate: Any, *, timeout: float = 8.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = predicate()
        if asyncio.iscoroutine(res):
            res = await res
        if res:
            return
        await asyncio.sleep(interval)
    raise TimeoutError(f"Condition not met within {timeout}s")


async def _recv_hub_ack(ws: Any, expected_id: str, *, timeout: float = 4.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        rem = max(0.05, deadline - time.monotonic())
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=rem))
        if frame.get("op") == "sys" and frame.get("error"):
            raise RuntimeError(f"Hub error for {expected_id}: {frame}")
        if frame.get("op") == "sys" and frame.get("ack") == expected_id:
            return frame


def _extract_legacy_package(dest_bus_root: pathlib.Path, commit: str = LEGACY_COMMIT) -> pathlib.Path:
    pkg_dir = dest_bus_root / "agent_bus"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    for fname in LEGACY_MODULE_FILES:
        res = subprocess.run(
            ["git", "-C", str(WORKTREE_ROOT), "show", f"{commit}:bus/agent_bus/{fname}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=10,
        )
        (pkg_dir / fname).write_text(res.stdout, encoding="utf-8")
    return dest_bus_root


def _terminate_proc(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=4.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            proc.kill()
        proc.wait(timeout=4.0)


class _BannerHangStubServer:
    """Fake hub that completes TCP+WS handshake, reads `hello`, and NEVER sends a banner."""

    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = port
        self.accepted_ws = 0
        self.hellos_received: list[dict[str, Any]] = []
        self._srv: Any = None
        self._release = asyncio.Event()
        self._open_sockets: set[Any] = set()

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    async def start(self) -> "_BannerHangStubServer":
        async def _handler(ws: Any) -> None:
            self.accepted_ws += 1
            self._open_sockets.add(ws)
            try:
                with contextlib.suppress(Exception):
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    self.hellos_received.append(json.loads(raw))
                # Deliberately withhold the banner forever until client times out or test ends.
                await self._release.wait()
            except BaseException:
                pass
            finally:
                self._open_sockets.discard(ws)

        self._srv = await websockets.serve(_handler, self.host, self.port)
        return self

    async def close(self) -> None:
        self._release.set()
        for ws in list(self._open_sockets):
            with contextlib.suppress(Exception):
                await ws.close()
        self._open_sockets.clear()
        if self._srv is not None:
            self._srv.close()
            with contextlib.suppress(Exception):
                await self._srv.wait_closed()
            self._srv = None


@pytest.mark.chaos
async def test_config_get_bus_urls_and_single_hub_default_behavior(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    hub_factory: HubController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """Verify `config.get_bus_urls()` precedence/deduplication and 100% identical single-hub
    behavior when neither `urls` nor `BUS_URLS` is configured."""
    # Verify protocol deadline defaults required by design.md §3.3 / F15
    assert bus_config.DEFAULT_DIAL_TIMEOUT == 5.0
    assert bus_config.DEFAULT_HELLO_TIMEOUT == 5.0
    assert bus_config.DEFAULT_BANNER_TIMEOUT == 10.0
    assert bus_config.MAX_RECONNECT_BACKOFF == 30.0

    cfg_d = tmp_path / "single_cfg"
    cfg_d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BUS_CONFIG_DIR", str(cfg_d))
    monkeypatch.delenv("BUS_URLS", raising=False)
    monkeypatch.delenv("BUS_URL", raising=False)

    # 1) Unset `urls` & `BUS_URLS` -> single `[get_bus_url()]`
    bus_config.save_config({"url": "ws://127.0.0.1:18765"})
    assert bus_config.get_bus_urls() == ["ws://127.0.0.1:18765"]

    # 2) `config.json` `"urls"` -> trims, drops empties, deduplicates preserving order
    bus_config.save_config(
        {
            "url": "ws://127.0.0.1:18765",
            "urls": [" ws://127.0.0.1:18765 ", "", "ws://127.0.0.1:18775", "ws://127.0.0.1:18765"],
        }
    )
    assert bus_config.get_bus_urls() == ["ws://127.0.0.1:18765", "ws://127.0.0.1:18775"]

    # 3) `BUS_URLS` env overrides `config.json` `"urls"`
    monkeypatch.setenv("BUS_URLS", " ws://127.0.0.1:18775 , , ws://127.0.0.1:18765 , ws://127.0.0.1:18775 ")
    assert bus_config.get_bus_urls() == ["ws://127.0.0.1:18775", "ws://127.0.0.1:18765"]

    # 4) Explicit `--url` argument takes top precedence
    assert bus_config.get_bus_urls(explicit_url="ws://127.0.0.1:19999") == ["ws://127.0.0.1:19999"]

    # 5) Live single-hub run when `urls` and `BUS_URLS` are completely absent
    monkeypatch.delenv("BUS_URLS", raising=False)
    hub_a = hub_factory.start_hub_a(port=HUB_A_PORT, local_node="cat-food")
    bus_config.save_config({"url": hub_a.ws_url})
    assert bus_config.get_bus_urls() == [hub_a.ws_url]

    w_task = node_runner.start(url=None, label="main", mode="hybrid", peers="cat-food", cfg_dir_override=cfg_d)
    try:
        await _wait_until(
            lambda: "cat-food/main" in (bus_client.run_doctor(argparse.Namespace(url=None, config_dir=str(cfg_d))).get("peers") or []),
            timeout=6.0,
        )
        caller_cfg = tmp_path / "single_caller"
        caller_cfg.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("BUS_CONFIG_DIR", str(caller_cfg))
        bus_config.save_config({"url": hub_a.ws_url})

        out_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf):
            rc = await bus_client.oneshot(
                argparse.Namespace(
                    cmd="send",
                    to="cat-food/main",
                    text="[id=single-1] ping",
                    wait=5.0,
                    hop=0,
                    wait_first=False,
                    update=False,
                    say=False,
                    kind="rpc",
                    rkind=None,
                    corr_id="single-1",
                    ttl=86400,
                    deadline=None,
                    url=None,
                    offline_queue=False,
                )
            )
        assert rc == 0
        assert "STUB_OK:single-1" in out_buf.getvalue()
        assert oracle.counter.count("single-1") == 1
        with NodeJournal(caller_cfg) as jc:
            assert jc.status_summary()["outbox"] == {"pending": 0, "done": 1, "expired": 0, "rejected": 0}
    finally:
        await node_runner.stop_task(w_task)


@pytest.mark.chaos
async def test_x01_dual_home_ack_boundaries_kill_and_hub_a_db_wipe(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """X01: Verify zero message loss and at-most-once execution (`== 1`) across all 4 Hub A
    kill + permanent DB loss boundaries:
      (i)   Kill Hub A BEFORE Hub A saves message.
      (ii)  Kill Hub A AFTER Hub A saves message, BEFORE client receives `sys ack`.
      (iii) Kill Hub A + wipe Hub A DB RIGHT AFTER Hub A ACK (`outbox` `pending`):
            - (iii-a) Recover via surviving/newly-started Hub B alone.
            - (iii-b) Recover via wiped & restarted empty Hub A on same URL (`H10` new `hub_id`).
      (iv)  Kill Hub A + wipe Hub A DB RIGHT AFTER both hubs ACK (`outbox` `done`).
    """
    hub_a = hub_factory.start_hub(
        "hub_a",
        port=HUB_A_PORT,
        local_node="cat-food",
        extra_env={"BUS_TEST_RATE_PER_MIN": "500"},
    )
    proxy_a = await proxy_factory.start_proxy(listen_port=PROXY_PORT, upstream_port=HUB_A_PORT)
    hub_b = hub_factory.start_hub(
        "hub_b",
        port=HUB_B_PORT,
        local_node="cat-food",
        extra_env={"BUS_TEST_RATE_PER_MIN": "500"},
    )

    tx_cfg = tmp_path / "x01_tx"
    rx_cfg = tmp_path / "x01_rx"
    tx_cfg.mkdir(parents=True, exist_ok=True)
    rx_cfg.mkdir(parents=True, exist_ok=True)

    dual_urls = [proxy_a.ws_url, hub_b.ws_url]
    for d in (tx_cfg, rx_cfg):
        (d / "config.json").write_text(
            json.dumps({"url": dual_urls[0], "urls": dual_urls, "dial_timeout": 0.6, "banner_timeout": 0.8}),
            encoding="utf-8",
        )
    monkeypatch.setenv("BUS_URLS", ",".join(dual_urls))

    # ------------------------------------------------------------------
    # Boundary (i): Hub A blocked/killed BEFORE Hub A stores message
    # ------------------------------------------------------------------
    id_b1 = "x01-b1-kill-before-save"
    proxy_a.block(True, mode="reject", cut_existing=True)
    hub_a.sigkill()

    rc_b1 = await bus_client.oneshot(
        argparse.Namespace(
            cmd="send",
            id=id_b1,
            to="cat-food/rx",
            text=f"[id={id_b1}] boundary-1",
            wait=0,
            hop=0,
            wait_first=False,
            update=False,
            say=False,
            kind="task",
            rkind="request",
            corr_id=id_b1,
            ttl=86400,
            deadline=None,
            url=None,
            config_dir=str(tx_cfg),
            offline_queue=False,
        )
    )
    assert rc_b1 == 0, "Dual-home send must succeed when >=1 hub (Hub B) ACKs"
    with NodeJournal(tx_cfg) as j:
        row_b1 = j.get_outbox_row(id_b1)
        assert row_b1 is not None and row_b1["state"] == "pending"
        assert len(json.loads(row_b1["acked_hubs"])) == 1

    # ------------------------------------------------------------------
    # Boundary (ii): Restart Hub A; withhold Hub A's `sys ack` frame AFTER Hub A commits
    #                `id_b2` to `messages`, then kill Hub A before client sees ACK!
    # ------------------------------------------------------------------
    hub_a.start(wipe_db=True)
    proxy_a.unblock()

    id_b2 = "x01-b2-kill-after-save-before-ack"
    # Connect a controlled WS session through proxy_a: once we send `id_b2`, blackhole `u2c`
    # by delaying upstream->client frames so Hub A saves `id_b2` while client gets NO ACK!
    proxy_a.set_delay(0.0, client_to_upstream=0.0, upstream_to_client=10.0)
    # Use small banner/ack timeout only after banner by temporarily letting banner pass first:
    proxy_a.clear_delay()

    with NodeJournal(tx_cfg) as j:
        j.enqueue_outbox(
            {
                "v": 2,
                "op": "send",
                "id": id_b2,
                "corr_id": id_b2,
                "kind": "task",
                "rkind": "request",
                "to": "cat-food/rx",
                "text": make_ask_prompt(id_b2, "boundary-2"),
                "hop": 0,
            }
        )

    ws_b2, banner_b2 = await bus_client._open_ws_with_deadlines(
        proxy_a.ws_url,
        bus_client.build_ws_hello("cli-b2", 10**9, accept_inbox=False, no_replay=True),
        dial_timeout=2.0,
        hello_timeout=2.0,
        banner_timeout=2.0,
    )
    try:
        with NodeJournal(tx_cfg) as j:
            j.remember_hub_id(proxy_a.ws_url, str(banner_b2["hub_id"]))
            raw_item_b2 = [it for it in j.list_outbox_pending() if it["id"] == id_b2][0]

        # Stall upstream->client ACK delivery for 15s while letting client->upstream write immediately!
        proxy_a.set_delay(0.0, client_to_upstream=0.0, upstream_to_client=15.0)
        await ws_b2.send(json.dumps(raw_item_b2, ensure_ascii=False))

        # Wait until Hub A's SQLite DB has durably stored `id_b2`!
        await _wait_until(
            lambda: any(m["id"] == id_b2 for m in hub_a.inspect_db()["messages"]),
            timeout=4.0,
        )
        # Kill Hub A + wipe DB BEFORE client ever receives the delayed `sys ack`!
        hub_a.sigkill()
        shutil.rmtree(hub_a.data_dir)
        proxy_a.clear_delay()
        await proxy_a.cut()
        with pytest.raises(Exception):
            await asyncio.wait_for(ws_b2.recv(), timeout=0.5)
    finally:
        proxy_a.clear_delay()
        with contextlib.suppress(Exception):
            await ws_b2.close()

    with NodeJournal(tx_cfg) as j:
        row_b2 = j.get_outbox_row(id_b2)
        assert row_b2 is not None
        assert row_b2["state"] == "pending"
        assert json.loads(row_b2["acked_hubs"]) == []

    # ------------------------------------------------------------------
    # Boundary (iv): Both Hub A and Hub B online -> both ACK -> `outbox` `done` ->
    #                immediately kill Hub A + permanently delete Hub A DB!
    # ------------------------------------------------------------------
    hub_a.start(wipe_db=True)
    id_b4 = "x01-b4-kill-after-both-acked-done"
    rc_b4 = await bus_client.oneshot(
        argparse.Namespace(
            cmd="send",
            id=id_b4,
            to="cat-food/rx",
            text=f"[id={id_b4}] boundary-4",
            wait=0,
            hop=0,
            wait_first=False,
            update=False,
            say=False,
            kind="task",
            rkind="request",
            corr_id=id_b4,
            ttl=86400,
            deadline=None,
            url=None,
            config_dir=str(tx_cfg),
            offline_queue=False,
        )
    )
    assert rc_b4 == 0
    with NodeJournal(tx_cfg) as j:
        row_b4 = j.get_outbox_row(id_b4)
        assert row_b4 is not None and row_b4["state"] == "done"
        assert len(json.loads(row_b4["acked_hubs"])) == 2

    # Kill Hub A + permanently destroy Hub A's DB right after `outbox` marked `id_b4` `done`!
    hub_a.sigkill()
    shutil.rmtree(hub_a.data_dir)

    # Start dual-home sender pump (`tx_daemon`) + dual-home receiver (`rx_daemon`) while ONLY Hub B is alive:
    # `id_b1`, `id_b2`, and `id_b4` must ALL be delivered via Hub B and executed once!
    tx_task = node_runner.start(url=None, label="tx", mode="watch", peers="cat-food", cfg_dir_override=tx_cfg)
    rx_task = node_runner.start(url=None, label="rx", mode="hybrid", peers="cat-food", cfg_dir_override=rx_cfg)
    try:
        await _wait_until(
            lambda: all(oracle.counter.count(x) == 1 for x in (id_b1, id_b2, id_b4)),
            timeout=8.0,
        )
    finally:
        await node_runner.stop_task(tx_task)
        await node_runner.stop_task(rx_task)

    # ------------------------------------------------------------------
    # Boundary (iii-a) & (iii-b): Hub A ACKs while Hub B is DOWN (`outbox` `pending`),
    #                             then Hub A is killed + Hub A DB permanently deleted!
    # ------------------------------------------------------------------
    hub_b.sigkill()
    shutil.rmtree(hub_b.data_dir)
    hub_a.start(wipe_db=True)

    id_b3_h10 = "x01-b3b-hub-a-wiped-restarted-h10"
    id_b3_hub_b = "x01-b3a-hub-b-only-recovery"

    for mid_3 in (id_b3_h10, id_b3_hub_b):
        rc_3 = await bus_client.oneshot(
            argparse.Namespace(
                cmd="send",
                id=mid_3,
                to="cat-food/rx",
                text=f"[id={mid_3}] boundary-3",
                wait=0,
                hop=0,
                wait_first=False,
                update=False,
                say=False,
                kind="task",
                rkind="request",
                corr_id=mid_3,
                ttl=86400,
                deadline=None,
                url=None,
                config_dir=str(tx_cfg),
                offline_queue=False,
            )
        )
        assert rc_3 == 0

    with NodeJournal(tx_cfg) as j:
        old_hub_a_id = j.resolve_hub_id(proxy_a.ws_url)
        assert old_hub_a_id is not None
        for mid_3 in (id_b3_h10, id_b3_hub_b):
            r3 = j.get_outbox_row(mid_3)
            assert r3 is not None and r3["state"] == "pending"
            assert json.loads(r3["acked_hubs"]) == [old_hub_a_id]

    # Kill Hub A + permanently delete Hub A's DB!
    hub_a.sigkill()
    shutil.rmtree(hub_a.data_dir)

    # (iii-b) Restart EMPTY Hub A on the SAME URL (`H10`: brand-new `hub_id`!) while Hub B stays DOWN!
    # Dual-home `tx` + `rx` must detect the new `hub_id` on `proxy_a.ws_url`, republish to empty Hub A,
    # and execute both `id_b3_h10` and `id_b3_hub_b` even before Hub B ever comes back!
    hub_a.start(wipe_db=True)
    tx_task_2 = node_runner.start(url=None, label="tx", mode="watch", peers="cat-food", cfg_dir_override=tx_cfg)
    rx_task_2 = node_runner.start(url=None, label="rx", mode="hybrid", peers="cat-food", cfg_dir_override=rx_cfg)
    try:
        await _wait_until(
            lambda: oracle.counter.count(id_b3_h10) == 1 and oracle.counter.count(id_b3_hub_b) == 1,
            timeout=8.0,
        )
        with NodeJournal(tx_cfg) as j:
            new_hub_a_id = j.resolve_hub_id(proxy_a.ws_url)
            assert new_hub_a_id is not None and new_hub_a_id != old_hub_a_id
            # Still `pending` until Hub B also ACKs!
            r3_mid = j.get_outbox_row(id_b3_h10)
            assert r3_mid is not None and r3_mid["state"] == "pending"

        # (iii-a) Now kill Hub A + wipe DB again, and bring up EMPTY Hub B!
        # `tx` republishes to Hub B -> `outbox` reaches `done`, and `rx` deduplicates (`count` stays 1)!
        hub_a.sigkill()
        shutil.rmtree(hub_a.data_dir)
        hub_b.start(wipe_db=True)

        def _all_x01_done() -> bool:
            with NodeJournal(tx_cfg) as j_chk:
                return all(
                    (j_chk.get_outbox_row(m) or {}).get("state") == "done"
                    for m in (id_b1, id_b2, id_b3_h10, id_b3_hub_b, id_b4)
                )

        await _wait_until(_all_x01_done, timeout=8.0)
    finally:
        await node_runner.stop_task(tx_task_2)
        await node_runner.stop_task(rx_task_2)

    # Global X01 invariant: 0 lost messages and EVERY request executed AT MOST ONCE (`== 1`)!
    for req_id in (id_b1, id_b2, id_b3_h10, id_b3_hub_b, id_b4):
        assert oracle.counter.count(req_id) == 1, f"Expected exact 1 execution for {req_id}"


@pytest.mark.chaos
async def test_x11_banner_hang_fake_hub_isolation_run_node_and_oneshot(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    hub_factory: HubController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """X11 (Part 1): A fake hub completes TCP+WS handshake and hangs forever before sending
    its banner. Dual-home `run_node` and `oneshot` (`send`, `send --wait`, `result`, `peers`,
    `doctor`) abort the hung hub within deadline, apply independent backoff, and continue
    operating without stalls through healthy Hub B."""
    fake_port = pick_free_port()
    fake_hub = await _BannerHangStubServer(fake_port).start()
    hub_b = hub_factory.start_hub_b(
        port=HUB_B_PORT,
        local_node="cat-food",
    )

    worker_cfg = tmp_path / "x11_worker_cfg"
    caller_cfg = tmp_path / "x11_caller_cfg"
    worker_cfg.mkdir(parents=True, exist_ok=True)
    caller_cfg.mkdir(parents=True, exist_ok=True)

    urls = [fake_hub.ws_url, hub_b.ws_url]
    monkeypatch.setenv("BUS_URLS", ",".join(urls))
    monkeypatch.setenv("BUS_DIAL_TIMEOUT", "0.35")
    monkeypatch.setenv("BUS_HELLO_TIMEOUT", "0.35")
    monkeypatch.setenv("BUS_BANNER_TIMEOUT", "0.45")
    monkeypatch.setenv("BUS_ACK_TIMEOUT", "1.0")

    for d in (worker_cfg, caller_cfg):
        (d / "config.json").write_text(json.dumps({"urls": urls}), encoding="utf-8")

    w_task = node_runner.start(
        url=None,
        label="main",
        mode="hybrid",
        peers="cat-food",
        cfg_dir_override=worker_cfg,
    )
    try:
        # 1) `peers`: succeeds via Hub B even while Hub A hangs on banner!
        async def _worker_visible_via_peers() -> bool:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = await bus_client.oneshot(
                    argparse.Namespace(cmd="peers", url=None, config_dir=str(caller_cfg))
                )
            return rc == 0 and "cat-food/main" in buf.getvalue()

        await _wait_until(_worker_visible_via_peers, timeout=6.0)
        assert fake_hub.accepted_ws >= 1, "Fake banner-hang hub must have been dialed"

        # 2) `doctor`: reports per-hub status (`fake_hub` ok=False, `hub_b` ok=True) within deadline!
        t0_doc = time.monotonic()
        doc_rep = bus_client.run_doctor(argparse.Namespace(url=None, config_dir=str(caller_cfg)))
        doc_elapsed = time.monotonic() - t0_doc
        assert doc_elapsed < 2.5
        assert doc_rep["hub_ok"] is True
        assert doc_rep["all_hubs_ok"] is False
        assert len(doc_rep["hubs"]) == 2
        h_fake, h_good = doc_rep["hubs"]
        assert h_fake["url"] == fake_hub.ws_url and h_fake["ok"] is False and h_fake.get("error")
        assert h_good["url"] == hub_b.ws_url and h_good["ok"] is True
        assert h_good["hub_id"] and isinstance(h_good["caps"], list)

        # 3) `send --wait`: adopts Hub B's terminal RPC reply immediately without waiting for hung Hub A!
        rpc_id = "x11-rpc-fast-1"
        t0_rpc = time.monotonic()
        out_rpc = io.StringIO()
        with contextlib.redirect_stdout(out_rpc):
            rc_rpc = await bus_client.oneshot(
                argparse.Namespace(
                    cmd="send",
                    id=rpc_id,
                    to="cat-food/main",
                    text=f"[id={rpc_id}] fast-rpc-despite-hung-hub",
                    wait=4.0,
                    hop=0,
                    wait_first=False,
                    update=False,
                    say=False,
                    kind="rpc",
                    rkind="request",
                    corr_id=rpc_id,
                    ttl=86400,
                    deadline=None,
                    url=None,
                    config_dir=str(caller_cfg),
                    offline_queue=False,
                )
            )
        rpc_elapsed = time.monotonic() - t0_rpc
        assert rc_rpc == 0
        assert f"STUB_OK:{rpc_id}" in out_rpc.getvalue()
        assert oracle.counter.count(rpc_id) == 1
        # Adopted first terminal result immediately (< 0.45s banner deadline + margin)!
        assert rpc_elapsed < 1.5

        # 4) `result`: also adopts Hub B's terminal result immediately without waiting on hung Hub A!
        out_res = io.StringIO()
        t0_res = time.monotonic()
        with contextlib.redirect_stdout(out_res):
            rc_res = await bus_client.oneshot(
                argparse.Namespace(
                    cmd="result",
                    corr_id=rpc_id,
                    wait=2.0,
                    json=True,
                    url=None,
                    config_dir=str(caller_cfg),
                )
            )
        assert rc_res == 0
        assert (time.monotonic() - t0_res) < 1.5
        parsed_res = json.loads(out_res.getvalue())
        assert parsed_res["status"] == "ok" and f"STUB_OK:{rpc_id}" in parsed_res["text"]

        # 5) `send` (no `--wait`): succeeds (`>=1` ACK from Hub B) right after fake hub banner deadline!
        fire_id = "x11-async-send-1"
        rc_fire = await bus_client.oneshot(
            argparse.Namespace(
                cmd="send",
                id=fire_id,
                to="cat-food/main",
                text=f"[id={fire_id}] async-task",
                wait=0,
                hop=0,
                wait_first=False,
                update=False,
                say=False,
                kind="task",
                rkind="request",
                corr_id=fire_id,
                ttl=86400,
                deadline=None,
                url=None,
                config_dir=str(caller_cfg),
                offline_queue=False,
            )
        )
        assert rc_fire == 0
        await _wait_until(lambda: oracle.counter.count(fire_id) == 1, timeout=5.0)
        with NodeJournal(caller_cfg) as jc:
            row_fire = jc.get_outbox_row(fire_id)
            assert row_fire is not None and row_fire["state"] == "pending"
            assert json.loads(row_fire["acked_hubs"]) == [h_good["hub_id"]]

        # Verify fake hub did NOT suffer an unthrottled tight reconnect loop (backoff applied)!
        assert fake_hub.accepted_ws < 15, f"Backoff missing on banner timeout: {fake_hub.accepted_ws}"
    finally:
        await node_runner.stop_task(w_task)
        await fake_hub.close()


@pytest.mark.chaos
async def test_x11_mixed_legacy_e7d0da2_client_and_legacy_hub_caps_fallback(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    hub_factory: HubController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """X11 (Part 2) & Spec 4 (`caps` fallback):
    - Unmodified `e7d0da2` `hub.py` (no `hub_id`, no `caps` in banner) paired with v2 `hub.py`
      in a dual-home cluster (`urls=[legacy_hub, v2_hub]`).
    - Unmodified `e7d0da2` `client.py` (`run_node` + `send --wait`) interoperating with v2
      dual-home `run_node` + `oneshot`.
    """
    legacy_pkg_root = _extract_legacy_package(tmp_path / "legacy_e7d0da2")
    leg_hub_port = pick_free_port()
    leg_hub_dir = tmp_path / "leg_hub_runtime"
    for sub in ("home", "cfg", "data", "logs", "_guard"):
        (leg_hub_dir / sub).mkdir(parents=True, exist_ok=True)
    (leg_hub_dir / "cfg" / "allow.txt").write_text("cat-food\nmother-goose\n", encoding="utf-8")
    guard_claude = write_claude_guard_script(leg_hub_dir / "_guard")

    leg_env = os.environ.copy()
    leg_env.update(
        {
            "HOME": str(leg_hub_dir / "home"),
            "PYTHONPATH": str(legacy_pkg_root),
            "PYTHONUSERBASE": "/home/rerun/.local",
            "PYTHONDONTWRITEBYTECODE": "1",
            "BUS_CONFIG_DIR": str(leg_hub_dir / "cfg"),
            "BUS_DATA_DIR": str(leg_hub_dir / "data"),
            "BUS_DB_PATH": str(leg_hub_dir / "data" / "bus.db"),
            "BUS_LOG_DIR": str(leg_hub_dir / "logs"),
            "BUS_TEST_ALLOW_LOCALHOST": "1",
            "BUS_TEST_LOCAL_NODE": "cat-food",
            "BUS_CLAUDE_CMD": str(guard_claude),
        }
    )

    leg_hub_proc: subprocess.Popen[str] | None = None
    leg_worker_proc: subprocess.Popen[str] | None = None
    try:
        # 1) Launch unmodified `e7d0da2` Hub (banner lacks `hub_id` and `caps`!)
        leg_hub_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_bus.cli",
                "hub",
                "--host",
                "127.0.0.1",
                "--port",
                str(leg_hub_port),
                "--gateway-port",
                "0",
            ],
            env=leg_env,
            cwd=str(leg_hub_dir / "home"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        await _wait_until(lambda: is_port_listening("127.0.0.1", leg_hub_port), timeout=6.0)
        leg_hub_url = f"ws://127.0.0.1:{leg_hub_port}"

        # 2) Launch v2 Hub B
        v2_hub = hub_factory.start_hub_b(port=HUB_B_PORT, local_node="cat-food")

        dual_urls = [leg_hub_url, v2_hub.ws_url]
        v2_worker_cfg = tmp_path / "v2_dual_worker"
        v2_caller_cfg = tmp_path / "v2_dual_caller"
        v2_worker_cfg.mkdir(parents=True, exist_ok=True)
        v2_caller_cfg.mkdir(parents=True, exist_ok=True)
        for d in (v2_worker_cfg, v2_caller_cfg):
            (d / "config.json").write_text(json.dumps({"urls": dual_urls}), encoding="utf-8")
        monkeypatch.setenv("BUS_URLS", ",".join(dual_urls))

        # Verify `doctor` sees BOTH hubs: legacy hub has `caps is None` & `hub_id is None`!
        doc = bus_client.run_doctor(argparse.Namespace(url=None, config_dir=str(v2_caller_cfg)))
        assert doc["hub_ok"] is True and doc["all_hubs_ok"] is True
        assert doc["hubs"][0]["url"] == leg_hub_url
        assert doc["hubs"][0]["ok"] is True
        assert doc["hubs"][0]["hub_id"] is None
        assert doc["hubs"][0]["caps"] is None
        assert doc["hubs"][1]["url"] == v2_hub.ws_url
        assert doc["hubs"][1]["ok"] is True
        assert doc["hubs"][1]["hub_id"] is not None
        assert "v2" in (doc["hubs"][1]["caps"] or [])

        # 3) Start v2 dual-home worker connected to BOTH `e7d0da2` hub and v2 hub!
        v2_w_task = node_runner.start(
            url=None,
            label="main",
            mode="hybrid",
            peers="cat-food",
            cfg_dir_override=v2_worker_cfg,
        )
        try:
            await _wait_until(
                lambda: "cat-food/main"
                in (bus_client.run_doctor(argparse.Namespace(url=leg_hub_url, config_dir=str(v2_caller_cfg))).get("peers") or []),
                timeout=6.0,
            )
            await _wait_until(
                lambda: "cat-food/main"
                in (bus_client.run_doctor(argparse.Namespace(url=v2_hub.ws_url, config_dir=str(v2_caller_cfg))).get("peers") or []),
                timeout=6.0,
            )

            # Dual-home v2 caller -> `[legacy_hub, v2_hub]` -> Dual-home v2 worker:
            # Must get storage ACK from BOTH hubs (`outbox` -> `done`), execute 1 time, and return reply!
            mix_id = "x11-caps-fallback-rpc-1"
            buf_mix = io.StringIO()
            with contextlib.redirect_stdout(buf_mix):
                rc_mix = await bus_client.oneshot(
                    argparse.Namespace(
                        cmd="send",
                        id=mix_id,
                        to="cat-food/main",
                        text=f"[id={mix_id}] caps-fallback-dual",
                        wait=5.0,
                        hop=0,
                        wait_first=False,
                        update=False,
                        say=False,
                        kind="rpc",
                        rkind="request",
                        corr_id=mix_id,
                        ttl=86400,
                        deadline=None,
                        url=None,
                        config_dir=str(v2_caller_cfg),
                        offline_queue=False,
                    )
                )
            assert rc_mix == 0
            assert f"STUB_OK:{mix_id}" in buf_mix.getvalue()
            assert oracle.counter.count(mix_id) == 1

            with NodeJournal(v2_caller_cfg) as jc:
                out_row = jc.get_outbox_row(mix_id)
                assert out_row is not None and out_row["state"] == "done"
                acked_list = json.loads(out_row["acked_hubs"])
                assert canonical_hub_url(leg_hub_url) in acked_list
                assert doc["hubs"][1]["hub_id"] in acked_list

            # 4) Unmodified legacy `e7d0da2` CLI caller -> v2 dual-home worker!
            leg_caller_dir = tmp_path / "leg_caller"
            leg_caller_dir.mkdir(parents=True, exist_ok=True)
            leg_cli_env = dict(leg_env, BUS_CONFIG_DIR=str(leg_caller_dir))
            leg_send = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-m",
                    "agent_bus.cli",
                    "send",
                    "--url",
                    v2_hub.ws_url,
                    "--to",
                    "cat-food/main",
                    "--wait",
                    "5",
                    "hello-from-e7d0da2 [id=x11-from-leg-cli]",
                ],
                env=leg_cli_env,
                cwd=str(leg_caller_dir),
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert leg_send.returncode == 0, leg_send.stderr
            assert "STUB_OK:x11-from-leg-cli" in leg_send.stdout
            assert oracle.counter.count("x11-from-leg-cli") == 1

            # 5) Start unmodified legacy `e7d0da2` `run_node` worker (`cat-food/legw`) on `v2_hub`
            #    and call it from v2 dual-home `oneshot send --wait`!
            leg_w_dir = tmp_path / "leg_worker_cfg"
            leg_w_dir.mkdir(parents=True, exist_ok=True)
            leg_stub = oracle.counter.write_subprocess_stub_script(tmp_path / "leg_worker_stub.py")
            leg_w_env = dict(
                leg_env,
                BUS_CONFIG_DIR=str(leg_w_dir),
                BUS_MODE_FILE=str(leg_w_dir / "mode"),
                BUS_CLAUDE_CMD=str(leg_stub),
            )
            leg_worker_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "agent_bus.cli",
                    "run",
                    "--label",
                    "legw",
                    "--mode",
                    "hybrid",
                    "--peers",
                    "cat-food",
                    "--url",
                    v2_hub.ws_url,
                    "--no-auto-update",
                ],
                env=leg_w_env,
                cwd=str(leg_w_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            await _wait_until(
                lambda: "cat-food/legw"
                in (bus_client.run_doctor(argparse.Namespace(url=v2_hub.ws_url, config_dir=str(v2_caller_cfg))).get("peers") or []),
                timeout=6.0,
            )

            buf_to_leg = io.StringIO()
            with contextlib.redirect_stdout(buf_to_leg):
                rc_to_leg = await bus_client.oneshot(
                    argparse.Namespace(
                        cmd="send",
                        id="x11-to-leg-worker",
                        to="cat-food/legw",
                        text="[id=x11-to-leg-worker] call-legacy-worker",
                        wait=5.0,
                        hop=0,
                        wait_first=False,
                        update=False,
                        say=False,
                        kind="rpc",
                        rkind="request",
                        corr_id="x11-to-leg-worker",
                        ttl=86400,
                        deadline=None,
                        url=None,
                        config_dir=str(v2_caller_cfg),
                        offline_queue=False,
                    )
                )
            assert rc_to_leg == 0
            assert "STUB_OK:x11-to-leg-worker" in buf_to_leg.getvalue()
            assert oracle.counter.count("x11-to-leg-worker") == 1
        finally:
            await node_runner.stop_task(v2_w_task)
    finally:
        _terminate_proc(leg_worker_proc)
        _terminate_proc(leg_hub_proc)


@pytest.mark.chaos
async def test_dual_receive_simultaneous_deduplication_and_cursors(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    hub_factory: HubController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """Verify simultaneous delivery of the exact same messages via BOTH Hub A and Hub B:
    - Both Hub A and Hub B deliveries transition to `acked`.
    - `cursors[hub_a_id]` and `cursors[hub_b_id]` advance monotonically & independently.
    - Legacy `cursor-main` tracks ONLY primary Hub A (`urls[0]`), not Hub B's higher seq (F28).
    - `SideEffectCounter.count(id) == 1` for every `!ask` message and `1` inbox entry per event.
    """
    hub_a = hub_factory.start_hub(
        "hub_a",
        port=HUB_A_PORT,
        local_node="cat-food",
        extra_env={"BUS_TEST_RATE_PER_MIN": "500"},
    )
    hub_b = hub_factory.start_hub(
        "hub_b",
        port=HUB_B_PORT,
        local_node="cat-food",
        extra_env={"BUS_TEST_RATE_PER_MIN": "500"},
    )

    # Skew Hub B's seq higher (+15) before starting worker so we also prove Hub B's seq
    # never pollutes primary Hub A's legacy `cursor-main`!
    async with websockets.connect(hub_b.ws_url) as skew_ws:
        await skew_ws.send(json.dumps({"v": 2, "label": "cli-skew", "since": 10**9, "accept_inbox": False, "no_replay": True}))
        await asyncio.wait_for(skew_ws.recv(), 2.0)
        for i in range(15):
            sid = f"skew-other-{i:02d}"
            await skew_ws.send(
                json.dumps({"v": 2, "op": "send", "id": sid, "to": "mother-goose/main", "kind": "event", "text": "pad"})
            )
            await _recv_hub_ack(skew_ws, sid)

    rx_cfg = tmp_path / "dual_rx_cfg"
    tx_cfg = tmp_path / "dual_tx_cfg"
    rx_cfg.mkdir(parents=True, exist_ok=True)
    tx_cfg.mkdir(parents=True, exist_ok=True)
    urls = [hub_a.ws_url, hub_b.ws_url]
    for d in (rx_cfg, tx_cfg):
        (d / "config.json").write_text(json.dumps({"url": urls[0], "urls": urls}), encoding="utf-8")
    monkeypatch.setenv("BUS_URLS", ",".join(urls))

    rx_task = node_runner.start(
        url=None,
        label="main",
        mode="hybrid",
        peers="cat-food",
        cfg_dir_override=rx_cfg,
    )
    try:
        await _wait_until(
            lambda: "cat-food/main" in (bus_client.run_doctor(argparse.Namespace(url=hub_a.ws_url, config_dir=str(tx_cfg))).get("peers") or [])
            and "cat-food/main" in (bus_client.run_doctor(argparse.Namespace(url=hub_b.ws_url, config_dir=str(tx_cfg))).get("peers") or []),
            timeout=6.0,
        )

        task_ids = [f"dual-dedup-task-{i:02d}" for i in range(10)]
        event_ids = [f"dual-dedup-evt-{i:02d}" for i in range(4)]

        for tid in task_ids:
            rc = await bus_client.oneshot(
                argparse.Namespace(
                    cmd="send",
                    id=tid,
                    to="cat-food/main",
                    text=f"[id={tid}] dual-task",
                    wait=0,
                    hop=0,
                    wait_first=False,
                    update=False,
                    say=False,
                    kind="task",
                    rkind="request",
                    corr_id=tid,
                    ttl=86400,
                    deadline=None,
                    url=None,
                    config_dir=str(tx_cfg),
                    offline_queue=False,
                )
            )
            assert rc == 0

        for eid in event_ids:
            rc_e = await bus_client.oneshot(
                argparse.Namespace(
                    cmd="send",
                    id=eid,
                    to="cat-food/inbox",
                    text=f"event-body-{eid}",
                    wait=0,
                    hop=0,
                    wait_first=False,
                    update=False,
                    say=True,
                    kind="event",
                    rkind="event",
                    corr_id=eid,
                    ttl=86400,
                    deadline=None,
                    url=None,
                    config_dir=str(tx_cfg),
                    offline_queue=False,
                )
            )
            assert rc_e == 0

        await _wait_until(
            lambda: all(oracle.counter.count(tid) == 1 for tid in task_ids),
            timeout=8.0,
        )
        await _wait_until(
            lambda: len([d for d in hub_a.inspect_db(consumer="cat-food/main")["deliveries"] if d["state"] == "acked"]) >= 14
            and len([d for d in hub_b.inspect_db(consumer="cat-food/main")["deliveries"] if d["state"] == "acked"]) >= 14,
            timeout=6.0,
        )
    finally:
        await node_runner.stop_task(rx_task)

    # 1) Exact 1 execution per `!ask` task (`0` duplicates despite 2 hubs delivering every item!)
    for tid in task_ids:
        assert oracle.counter.count(tid) == 1

    # 2) Exact 1 local `inbox.jsonl` entry per event (`0` duplicate ACC bridge writes!)
    inbox_entries = bus_client.read_inbox(rx_cfg, include_acked=True)
    inbox_id_counts = [it["id"] for it in inbox_entries]
    for eid in event_ids:
        assert inbox_id_counts.count(eid) == 1

    # 3) Independent `cursors` in `journal.db` + primary-only legacy `cursor-main`!
    max_acked_seq_a = max(
        d["seq"] for d in hub_a.inspect_db(consumer="cat-food/main")["deliveries"] if d["state"] == "acked"
    )
    max_acked_seq_b = max(
        d["seq"] for d in hub_b.inspect_db(consumer="cat-food/main")["deliveries"] if d["state"] == "acked"
    )
    assert max_acked_seq_b >= max_acked_seq_a + 15

    with NodeJournal(rx_cfg) as j_rx:
        hid_a = j_rx.resolve_hub_id(hub_a.ws_url)
        hid_b = j_rx.resolve_hub_id(hub_b.ws_url)
        assert hid_a and hid_b and hid_a != hid_b
        summary = j_rx.status_summary()
        assert summary["cursors"][hid_a] == max_acked_seq_a
        assert summary["cursors"][hid_b] == max_acked_seq_b
    # Legacy `cursor-main` tracks ONLY primary Hub A (`max_acked_seq_a`), NOT Hub B's higher seq!
    assert bus_client.load_cursor("main", rx_cfg) == max_acked_seq_a


@pytest.mark.chaos
async def test_permanent_rejected_vs_rate_retry_rkind_result_and_reconcile_cli(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    hub_factory: HubController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """Verify:
    1) Pre-I3 `journal.db` `outbox` schema migration (`CHECK` constraint adds `'rejected'`).
    2) Permanent rejections (`id_conflict`, `size`, `hop`, `forbidden`) transition `outbox` to
       `state == 'rejected'` and halt retries, whereas `rate` rejection keeps `state == 'pending'`
       and succeeds after backoff!
    3) Worker replies (`ok`, `exec_error`, `exec_uncertain`, `expired`) ALL explicitly carry
       `rkind == 'result'` on the wire / Hub SQLite `messages` table.
    4) `abus journal reconcile --hubs <keys> [--json]` transitions satisfied `pending` rows to `done`.
    """
    # ------------------------------------------------------------------
    # Part 1: Pre-I3 `outbox` schema (`CHECK (state IN ('pending','done','expired'))`) migration
    # ------------------------------------------------------------------
    legacy_j_dir = tmp_path / "pre_i3_journal"
    legacy_j_dir.mkdir(parents=True, exist_ok=True)
    raw_db = legacy_j_dir / "journal.db"
    conn0 = sqlite3.connect(str(raw_db))
    try:
        conn0.execute(
            """
            CREATE TABLE outbox (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created REAL NOT NULL,
                deadline REAL NOT NULL,
                acked_hubs TEXT NOT NULL DEFAULT '[]',
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'done', 'expired'))
            )
            """
        )
        conn0.execute(
            "INSERT INTO outbox (id, payload, created, deadline, acked_hubs, state) VALUES (?, ?, ?, ?, '[]', 'pending')",
            ("mig-row-1", json.dumps({"v": 2, "op": "send", "id": "mig-row-1", "to": "cat-food", "text": "x"}), time.time(), time.time() + 3600),
        )
        conn0.commit()
    finally:
        conn0.close()

    with NodeJournal(legacy_j_dir) as j_mig:
        assert j_mig.mark_outbox_rejected("mig-row-1", hub_key="h1", error="id_conflict") is True
        assert j_mig.status_summary()["outbox"] == {"pending": 0, "done": 0, "expired": 0, "rejected": 1}

    # ------------------------------------------------------------------
    # Part 2: Permanent rejections (`id_conflict`, `size`, `hop`, `forbidden`) vs `rate` retry
    # ------------------------------------------------------------------
    gw_port = pick_free_port()
    hub_a = hub_factory.create_hub(
        "hub_a",
        port=HUB_A_PORT,
        gateway_port=gw_port,
        local_node="cat-food",
        extra_env={"BUS_TEST_RATE_PER_MIN": "3"},
    )
    tok_limited = hub_a.create_token(
        "lim",
        scopes=["send:cat-food/inbox"],
        allow_exec=False,
    )
    hub_a.start()

    # Seed `conflict-id-1` on Hub A with `"original-text"` (consumes 1 of 3 rate tokens for `cat-food`)
    async with websockets.connect(hub_a.ws_url) as seed_ws:
        await seed_ws.send(json.dumps({"v": 2, "label": "cli-seed", "since": 10**9, "accept_inbox": False, "no_replay": True}))
        await asyncio.wait_for(seed_ws.recv(), 2.0)
        await seed_ws.send(
            json.dumps({"v": 2, "op": "send", "id": "conflict-id-1", "to": "cat-food/rx", "kind": "event", "text": "original-text"})
        )
        await _recv_hub_ack(seed_ws, "conflict-id-1")

    rej_cfg = tmp_path / "rej_cfg"
    rej_cfg.mkdir(parents=True, exist_ok=True)
    (rej_cfg / "config.json").write_text(json.dumps({"url": hub_a.ws_url}), encoding="utf-8")

    with NodeJournal(rej_cfg) as j_rej:
        # 1) `id_conflict` (same id, different text)
        j_rej.enqueue_outbox({"v": 2, "op": "send", "id": "conflict-id-1", "to": "cat-food/rx", "kind": "event", "text": "different-text"})
        # 2) `size` (> 16384 chars)
        j_rej.enqueue_outbox({"v": 2, "op": "send", "id": "oversize-id-1", "to": "cat-food/rx", "kind": "event", "text": "X" * 17000})
        # 3) `hop` (> MAX_HOP=3)
        j_rej.enqueue_outbox({"v": 2, "op": "send", "id": "overhop-id-1", "to": "cat-food/rx", "kind": "event", "text": "loop", "hop": 4})
        # 4) 4 valid items (only 2 rate slots remain out of 3/min -> 2 will succeed now, 2 will hit `rate` and stay `pending`!)
        for k in range(4):
            j_rej.enqueue_outbox({"v": 2, "op": "send", "id": f"rate-ok-{k}", "to": "cat-food/rx", "kind": "event", "text": f"ok-{k}"})

    drain_1 = await bus_client.drain_outbox(config_dir=rej_cfg, url=hub_a.ws_url, timeout=1.2)
    assert drain_1["rejected"] == 3
    assert drain_1["drained"] == 2
    assert drain_1["remaining"] == 2

    with NodeJournal(rej_cfg) as j_rej:
        st_1 = j_rej.status_summary()["outbox"]
        assert st_1 == {"pending": 2, "done": 2, "expired": 0, "rejected": 3}
        for r_id in ("conflict-id-1", "oversize-id-1", "overhop-id-1"):
            assert (j_rej.get_outbox_row(r_id) or {})["state"] == "rejected"

    # Also verify external `forbidden` -> `rejected`!
    ext_cfg = tmp_path / "ext_forbid_cfg"
    ext_cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BUS_AUTH_TOKEN", tok_limited["id"])
    monkeypatch.setenv("BUS_AUTH_SECRET", tok_limited["secret"])
    rc_forbid = await bus_client.oneshot(
        argparse.Namespace(
            cmd="send",
            id="ext-forbid-1",
            to="cat-food/main",  # forbidden for `send:cat-food/inbox` scope!
            text="unauthorized-rpc",
            wait=0,
            hop=0,
            wait_first=False,
            update=False,
            say=False,
            kind="rpc",
            rkind="request",
            corr_id="ext-forbid-1",
            ttl=86400,
            deadline=None,
            url=hub_a.gateway_http_url,
            config_dir=str(ext_cfg),
            offline_queue=False,
        )
    )
    monkeypatch.delenv("BUS_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("BUS_AUTH_SECRET", raising=False)
    assert rc_forbid == 1
    with NodeJournal(ext_cfg) as j_ext:
        assert (j_ext.get_outbox_row("ext-forbid-1") or {})["state"] == "rejected"

    # Raise rate limit on Hub A and restart -> remaining 2 `rate` items drain cleanly to `done`,
    # while the 3 `rejected` items are NEVER retried!
    hub_a.extra_env["BUS_TEST_RATE_PER_MIN"] = "500"
    hub_a.restart()
    drain_2 = await bus_client.drain_outbox(config_dir=rej_cfg, url=hub_a.ws_url, timeout=5.0)
    assert drain_2["ok"] is True
    assert drain_2["drained"] == 2
    assert drain_2["rejected"] == 0
    assert drain_2["remaining"] == 0
    with NodeJournal(rej_cfg) as j_rej:
        assert j_rej.status_summary()["outbox"] == {"pending": 0, "done": 4, "expired": 0, "rejected": 3}

    # ------------------------------------------------------------------
    # Part 3: Explicit `rkind == "result"` on ALL worker reply types:
    #         (`ok`, `exec_error`, `exec_uncertain`, `expired`)
    # ------------------------------------------------------------------
    rk_cfg = tmp_path / "rkind_worker_cfg"
    rk_cfg.mkdir(parents=True, exist_ok=True)
    (rk_cfg / "config.json").write_text(json.dumps({"url": hub_a.ws_url}), encoding="utf-8")

    oracle.stub.set_response("rk-err-req", "boom-failure", status="exec_error")

    # Pre-seed 1 orphaned `running` row (from dead PID 99999999) + 1 already-expired `queued` row!
    now_ts = time.time()
    with NodeJournal(rk_cfg) as j_rk:
        j_rk.receive_inbox(
            mid="rk-orphan-req",
            sender="cat-food/cli-rk",
            corr_id="rk-orphan-req",
            kind="rpc",
            text="orphan-job",
            received_at=now_ts - 10,
            deadline=now_ts + 3600,
            eligible=True,
        )
        claimed_orphan = j_rk.claim_next_queued(worker_id="dead-worker-000", worker_pid=99999999)
        assert claimed_orphan is not None and claimed_orphan["id"] == "rk-orphan-req"

        j_rk.receive_inbox(
            mid="rk-expired-req",
            sender="cat-food/cli-rk",
            corr_id="rk-expired-req",
            kind="rpc",
            text="expired-job",
            received_at=now_ts - 4000,
            deadline=now_ts - 10,
            eligible=True,
        )

    rk_worker = node_runner.start(
        url=hub_a.ws_url,
        label="rkworker",
        mode="hybrid",
        peers="cat-food",
        cfg_dir_override=rk_cfg,
    )
    try:
        await _wait_until(
            lambda: "cat-food/rkworker" in (bus_client.run_doctor(argparse.Namespace(url=hub_a.ws_url, config_dir=str(rk_cfg))).get("peers") or []),
            timeout=6.0,
        )
        async with websockets.connect(hub_a.ws_url) as rk_ws:
            await rk_ws.send(json.dumps({"v": 2, "label": "cli-rk", "since": 10**9, "accept_inbox": False, "no_replay": True}))
            await asyncio.wait_for(rk_ws.recv(), 2.0)
            for req_id in ("rk-ok-req", "rk-err-req"):
                await rk_ws.send(
                    json.dumps(
                        {
                            "v": 2,
                            "op": "send",
                            "id": req_id,
                            "corr_id": req_id,
                            "kind": "rpc",
                            "rkind": "request",
                            "to": "cat-food/rkworker",
                            "text": make_ask_prompt(req_id, "test-rkind"),
                        }
                    )
                )
                await _recv_hub_ack(rk_ws, req_id)

        def _all_four_replies_stored() -> bool:
            msgs = hub_a.inspect_db()["messages"]
            by_cid = {
                m["corr_id"]: m
                for m in msgs
                if m.get("corr_id") in ("rk-ok-req", "rk-err-req", "rk-orphan-req", "rk-expired-req")
                and m["id"] != m["corr_id"]
            }
            return len(by_cid) == 4

        await _wait_until(_all_four_replies_stored, timeout=6.0)
    finally:
        await node_runner.stop_task(rk_worker)

    stored_replies = {
        m["corr_id"]: m
        for m in hub_a.inspect_db()["messages"]
        if m.get("corr_id") in ("rk-ok-req", "rk-err-req", "rk-orphan-req", "rk-expired-req")
        and m["id"] != m["corr_id"]
    }
    assert stored_replies["rk-ok-req"]["status"] == "ok"
    assert stored_replies["rk-ok-req"]["rkind"] == "result"
    assert stored_replies["rk-err-req"]["status"] == "exec_error"
    assert stored_replies["rk-err-req"]["rkind"] == "result"
    assert stored_replies["rk-orphan-req"]["status"] == "exec_uncertain"
    assert stored_replies["rk-orphan-req"]["rkind"] == "result"
    assert stored_replies["rk-expired-req"]["status"] == "expired"
    assert stored_replies["rk-expired-req"]["rkind"] == "result"

    # ------------------------------------------------------------------
    # Part 4: `abus journal reconcile --hubs <키,…>` CLI verification
    # ------------------------------------------------------------------
    rec_cfg = tmp_path / "reconcile_cfg"
    rec_cfg.mkdir(parents=True, exist_ok=True)
    with NodeJournal(rec_cfg) as j_rec:
        j_rec.remember_hub_id("ws://127.0.0.1:18765", "hub-alpha-id")
        j_rec.remember_hub_id("ws://127.0.0.1:18775", "hub-bravo-id")

        for mid_r in ("rec-only-a", "rec-both-ab", "rec-only-b"):
            j_rec.enqueue_outbox(
                {"v": 2, "op": "send", "id": mid_r, "to": "cat-food/rx", "kind": "task", "text": mid_r}
            )
        both_req = ["ws://127.0.0.1:18765", "ws://127.0.0.1:18775"]
        assert j_rec.mark_outbox_acked("rec-only-a", "hub-alpha-id", required_hubs=both_req) is False
        assert j_rec.mark_outbox_acked("rec-both-ab", "hub-alpha-id", required_hubs=both_req) is False
        # Leave `rec-both-ab` pending too until reconcile if we only ack alpha, OR test 2-hub vs 1-hub:
        assert j_rec.mark_outbox_acked("rec-only-b", "hub-bravo-id", required_hubs=both_req) is False

    cli_env = os.environ.copy()
    cli_env["PYTHONPATH"] = str(BUS_ROOT)
    cli_env["BUS_CONFIG_DIR"] = str(rec_cfg)

    # Reconcile shrinking active hub set to `--hubs hub-alpha-id`:
    # `rec-only-a` and `rec-both-ab` have `hub-alpha-id` -> transition to `done` (2 items);
    # `rec-only-b` lacks `hub-alpha-id` -> stays `pending` (1 item)!
    rec_proc = subprocess.run(
        [sys.executable, "-m", "agent_bus.cli", "journal", "reconcile", "--hubs", "hub-alpha-id", "--json"],
        env=cli_env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    rec_json = json.loads(rec_proc.stdout)
    assert rec_json["ok"] is True
    assert rec_json["reconciled"] == 2
    assert rec_json["remaining_pending"] == 1
    assert rec_json["done_total"] == 2

    # Now reconcile remaining `rec-only-b` using endpoint URL `--hubs ws://127.0.0.1:18775` (human output)!
    rec_proc_2 = subprocess.run(
        [sys.executable, "-m", "agent_bus.cli", "journal", "reconcile", "--hubs", "ws://127.0.0.1:18775"],
        env=cli_env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "reconciled=1" in rec_proc_2.stdout
    assert "remaining_pending=0" in rec_proc_2.stdout

    st_cli = subprocess.run(
        [sys.executable, "-m", "agent_bus.cli", "journal", "status"],
        env=cli_env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "outbox: pending=0 done=3 expired=0 rejected=0" in st_cli.stdout
