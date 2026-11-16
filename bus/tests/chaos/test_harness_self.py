"""Self-verification unit & integration tests for the I0 chaos harness (`tests/chaos/`)."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import time
import pytest
import websockets

from agent_bus import client as bus_client
from .hub_proc import (
    BUS_ROOT,
    HUB_A_PORT,
    HUB_B_PORT,
    HubController,
    is_port_listening,
    validate_sandbox_port,
)
from .oracle import ChaosOracle, make_ask_prompt
from .tcp_proxy import PROXY_PORT, ProxyController


async def _ws_hello_and_banner(url: str, label: str = "probe", since: int = 0, timeout: float = 2.0) -> dict:
    async with websockets.connect(url, open_timeout=timeout, close_timeout=1.0) as ws:
        await ws.send(json.dumps({"v": 2, "label": label, "since": since, "accept_inbox": False}))
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        return json.loads(raw)


def test_worktree_pythonpath_isolation_and_claude_double_guard(
    tmp_path: pathlib.Path,
    sandbox_env: dict[str, pathlib.Path],
) -> None:
    """Verify worktree PYTHONPATH (`abus-wt/bus`), port guards, and double `claude` block."""
    client_file = pathlib.Path(bus_client.__file__).resolve()
    assert BUS_ROOT == pathlib.Path("/home/rerun/abus-wt/bus").resolve()
    assert str(client_file).startswith(str(BUS_ROOT)), f"Imported from wrong checkout: {client_file}"

    # Verify HOME and BUS_* are strictly confined inside pytest tmp_path
    assert pathlib.Path(os.environ["HOME"]).is_relative_to(tmp_path)
    assert pathlib.Path(os.environ["BUS_CONFIG_DIR"]).is_relative_to(tmp_path)
    assert pathlib.Path(os.environ["BUS_DB_PATH"]).is_relative_to(tmp_path)
    assert pathlib.Path(os.environ["BUS_LOG_DIR"]).is_relative_to(tmp_path)

    # Production / peer ports must be rejected immediately
    for forbidden in (8765, 8766, 18769):
        with pytest.raises(ValueError, match="forbidden"):
            validate_sandbox_port(forbidden)

    # Layer 1 guard: unstubbed in-process `ask_claude` must raise RuntimeError
    with pytest.raises(RuntimeError, match="blocked by chaos harness"):
        bus_client.ask_claude("hello", "cat-food/main")

    # Layer 2 guard: subprocess `BUS_CLAUDE_CMD` executable must fail closed (rc=99)
    guard_cmd = os.environ["BUS_CLAUDE_CMD"]
    res = subprocess.run([guard_cmd, "-p"], input="hi", capture_output=True, text=True, timeout=5)
    assert res.returncode == 99
    assert "blocked by chaos guard" in res.stderr


async def test_hub_controller_sigstop_sigcont_sigkill_and_state_isolation(
    hub_factory: HubController,
) -> None:
    """Verify PID control (`SIGSTOP`, `SIGCONT`, `SIGKILL`), restart options, and log isolation."""
    hub_a = hub_factory.start_hub_a(port=HUB_A_PORT)
    hub_b = hub_factory.start_hub_b(port=HUB_B_PORT)
    assert hub_a.is_running() and hub_b.is_running()
    assert hub_a.logs_dir != hub_b.logs_dir

    # Enqueue 1 message on Hub A so seq=1
    async with websockets.connect(hub_a.ws_url) as ws:
        await ws.send(json.dumps({"v": 2, "label": "cli-tx", "since": 10**9, "accept_inbox": False}))
        banner_a = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
        assert banner_a["seq"] == 0
        await ws.send(json.dumps({"v": 2, "op": "send", "id": "h-m1", "to": "cat-food/rx", "text": "ping", "kind": "event"}))
        ack_a = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
        assert ack_a["seq"] == 1

    # Hub B has isolated logs_dir/db_path so its seq remains 0
    banner_b = await _ws_hello_and_banner(hub_b.ws_url)
    assert banner_b["seq"] == 0

    # Freeze Hub A with SIGSTOP -> process enters 'T' state and WS banner times out
    hub_a.sigstop()
    assert hub_a.is_stopped
    assert hub_a.proc_state() in ("T", "t")
    with pytest.raises(asyncio.TimeoutError):
        await _ws_hello_and_banner(hub_a.ws_url, timeout=0.25)

    # Resume Hub A with SIGCONT -> immediately responsive again with seq=1
    hub_a.sigcont()
    assert not hub_a.is_stopped
    resumed_banner = await _ws_hello_and_banner(hub_a.ws_url, timeout=2.0)
    assert resumed_banner["seq"] == 1

    # Restart Hub A preserving state -> seq stays 1
    hub_a.restart(wipe_db=False, wipe_logs=False)
    assert (await _ws_hello_and_banner(hub_a.ws_url))["seq"] == 1

    # Restart Hub A wiping DB & logs -> seq resets cleanly to 0
    hub_a.restart(wipe_db=True, wipe_logs=True)
    assert (await _ws_hello_and_banner(hub_a.ws_url))["seq"] == 0

    # SIGKILL Hub A -> port 18765 released immediately; leave Hub B SIGSTOPped to test safe teardown!
    hub_a.sigkill()
    assert not is_port_listening("127.0.0.1", HUB_A_PORT)
    hub_b.sigstop()
    assert hub_b.is_stopped


async def test_tcp_fault_proxy_block_delay_cut_and_upstream_switch(
    hub_factory: HubController,
    proxy_factory: ProxyController,
) -> None:
    """Verify `127.0.0.1:18785` TCP fault proxy: relay, delay, block (reject/blackhole), cut, switch."""
    hub_a = hub_factory.start_hub_a(port=HUB_A_PORT)
    hub_b = hub_factory.start_hub_b(port=HUB_B_PORT)

    # Advance Hub B to seq=1 so we can distinguish Hub A (seq=0) from Hub B (seq=1)
    async with websockets.connect(hub_b.ws_url) as ws_b:
        await ws_b.send(json.dumps({"v": 2, "label": "cli-seed", "since": 10**9, "accept_inbox": False}))
        await ws_b.recv()
        await ws_b.send(json.dumps({"v": 2, "op": "send", "id": "b-seed", "to": "cat-food/rx", "text": "b", "kind": "event"}))
        await ws_b.recv()

    proxy = await proxy_factory.start_proxy(listen_port=PROXY_PORT, upstream_port=HUB_A_PORT)

    # 1. Normal relay to Hub A (seq=0)
    b0 = await _ws_hello_and_banner(proxy.ws_url)
    assert b0["seq"] == 0
    assert proxy.bytes_c2u > 0 and proxy.bytes_u2c > 0

    # 2. Delay toggle adds measurable round-trip latency
    proxy.set_delay(0.08)
    t_delayed = time.monotonic()
    await _ws_hello_and_banner(proxy.ws_url)
    elapsed_delayed = time.monotonic() - t_delayed
    assert elapsed_delayed >= 0.14, f"Expected >=0.14s with 80ms bidirectional delay, got {elapsed_delayed:.3f}s"

    proxy.clear_delay()
    fast_samples: list[float] = []
    for _ in range(2):
        t_fast = time.monotonic()
        await _ws_hello_and_banner(proxy.ws_url)
        dt = time.monotonic() - t_fast
        fast_samples.append(dt)
        if dt < 0.12:
            break
    elapsed_fast = min(fast_samples)
    assert elapsed_fast < 0.12

    # 3. Block (reject mode) drops new connections immediately
    proxy.block(True, mode="reject")
    with pytest.raises(Exception):
        await _ws_hello_and_banner(proxy.ws_url, timeout=0.5)
    assert proxy.blocked_count >= 1
    proxy.unblock()

    # 4. Block (blackhole mode) stalls handshake until unblocked
    proxy.block(True, mode="blackhole")
    with pytest.raises((asyncio.TimeoutError, Exception)):
        await _ws_hello_and_banner(proxy.ws_url, timeout=0.2)
    proxy.unblock()

    # 5. Cut immediately severs active open WS session
    ws_live = await websockets.connect(proxy.ws_url)
    try:
        await ws_live.send(json.dumps({"v": 2, "label": "live-victim", "since": 0, "accept_inbox": False}))
        await asyncio.wait_for(ws_live.recv(), 2.0)
        assert proxy.active_count == 1
        severed = await proxy.cut()
        assert severed == 1
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await asyncio.wait_for(ws_live.recv(), 1.0)
    finally:
        await ws_live.close()

    # 6. Switch upstream target 18765 -> 18775
    proxy.set_upstream(HUB_B_PORT)
    b_switched = await _ws_hello_and_banner(proxy.ws_url)
    assert b_switched["seq"] == 1


async def test_oracle_ledger_side_effect_counter_and_blocking_hooks(
    oracle: ChaosOracle,
) -> None:
    """Verify `RequestLedger`, file-flushed `SideEffectCounter`, `ExecutionGate`, and `asyncio.Event` hook."""
    req1 = "ora-req-1"
    req2 = "ora-req-2"

    oracle.ledger.record_sent(req1, target="cat-food/main")
    oracle.ledger.record_hub_ack(req1, hub="hub_a", seq=1)
    oracle.ledger.record_hub_ack(req1, hub="hub_b", seq=1)
    oracle.ledger.record_received(req1)

    gate1 = oracle.stub.gate(req1)
    loop = asyncio.get_running_loop()
    exec_fut1 = loop.run_in_executor(None, oracle.stub, make_ask_prompt(req1, "hello"), "cat-food/cli", None)

    await gate1.wait_entered(timeout=2.0)
    assert oracle.counter.count(req1) == 1
    assert not exec_fut1.done()

    gate1.release()
    out1, st1 = await asyncio.wait_for(exec_fut1, timeout=2.0)
    assert st1 == "ok"
    assert req1 in out1
    oracle.ledger.record_result(req1, status=st1)

    # Also verify direct `asyncio.Event` blocking hook (`set_block_event`)
    release_ev = asyncio.Event()
    entered_ev = asyncio.Event()
    oracle.stub.set_block_event(req2, release_ev, entered_event=entered_ev)
    exec_fut2 = loop.run_in_executor(None, oracle.stub, make_ask_prompt(req2, "world"), "cat-food/cli", None)

    await asyncio.wait_for(entered_ev.wait(), timeout=2.0)
    assert oracle.counter.count(req2) == 1
    assert not exec_fut2.done()

    release_ev.set()
    out2, st2 = await asyncio.wait_for(exec_fut2, timeout=2.0)
    assert st2 == "ok" and req2 in out2

    # Verify ledger timestamps & side-effect disk persistence
    entry1 = oracle.ledger.get(req1)
    assert entry1 is not None
    assert entry1.sent is not None and entry1.hub_ack is not None
    assert entry1.received is not None and entry1.executed is not None and entry1.result is not None
    assert entry1.acked_hubs == ["hub_a", "hub_b"]
    assert oracle.counter.all_ids() == [req1, req2]
