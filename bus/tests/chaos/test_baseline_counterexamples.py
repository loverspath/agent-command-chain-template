"""Baseline counterexample tests (`R05`, `R07`, `R08`, `R16`) for `abus` resilience v2.

Each test reproduces Sol's adversarial counterexample while asserting the **v2 target
acceptance criteria** (`design.md` §2, §6, §7).
Current WBS status after `I2`:
- `R05` -> `strict xfail` (resolved in `I3`: client dual-home + multi-hub ACK outbox retention)
- `R07` -> **PASSING** in `I2` (hub/store independent RPC result upsert + external query)
- `R08` -> **PASSING** in `I1` (node durable inbox journal + monotonic cursor watermark)
- `R16` -> **PASSING** in `I1` (node durable inbox/execution deduplication across reconnects)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import time
import urllib.error
import urllib.request
from typing import Any

import pytest
import websockets

from agent_bus import auth as bus_auth
from agent_bus import client as bus_client
from agent_bus import config as bus_config
from .conftest import NodeRunner
from .hub_proc import HUB_A_PORT, HUB_B_PORT, HubController, pick_free_port
from .oracle import ChaosOracle, make_ask_prompt
from .tcp_proxy import PROXY_PORT, ProxyController


async def _wait_until(predicate: Any, *, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = predicate()
        if asyncio.iscoroutine(res):
            res = await res
        if res:
            return
        await asyncio.sleep(interval)
    raise TimeoutError(f"Condition not met within {timeout}s")


async def _recv_hub_ack(ws: Any, expected_id: str, *, timeout: float = 3.0) -> dict[str, Any]:
    """Read frames from `ws` until the hub storage `ack` for `expected_id` arrives."""
    deadline = time.monotonic() + timeout
    while True:
        rem = max(0.05, deadline - time.monotonic())
        raw = await asyncio.wait_for(ws.recv(), timeout=rem)
        frame = json.loads(raw)
        if frame.get("op") == "sys" and frame.get("error"):
            raise RuntimeError(f"Hub rejected send for {expected_id}: {frame}")
        if frame.get("op") == "sys" and frame.get("ack") == expected_id:
            return frame


def _signed_http_json(
    base_http_url: str,
    route: str,
    *,
    token_id: str,
    secret: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    hdrs = bus_auth.build_hmac_headers(token_id, secret, raw)
    hdrs["Tailscale-Funnel-Request"] = "?1"
    if payload is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{base_http_url.rstrip('/')}{route}",
        data=raw if method.upper() == "POST" else None,
        headers=hdrs,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"ok": False, "error": body}
        return exc.code, parsed


@pytest.mark.chaos
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="R05: 허브A 단독 저장ACK 직후 발신 outbox 비움으로 빈 허브B 복구 불가 — 후속 I3(듀얼홈·전 허브 저장ACK 보존)에서 해소",
)
async def test_r05_acked_outbox_cleared_before_hub_b_loses_message(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    hub_factory: HubController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """R05: Sender flushes outbox to Hub A while receiver & Hub B are offline, then Hub A is lost.

    v2 Acceptance Criterion (G1+G2, X01):
    Outbox retains `r05-msg-1` until ALL configured hubs (`urls=[hub_a, hub_b]`) return storage
    ACKs, so when Hub A dies permanently (`SIGKILL` + DB loss) and empty Hub B starts, sender
    republishes `r05-msg-1` to Hub B and receiver executes it (`loss == 0`).
    """
    hub_a = hub_factory.start_hub_a(port=HUB_A_PORT, local_node="cat-food")
    hub_b_url = f"ws://127.0.0.1:{HUB_B_PORT}"

    sender_cfg = tmp_path / "sender_cfg"
    receiver_cfg = tmp_path / "receiver_cfg"
    sender_cfg.mkdir(parents=True, exist_ok=True)
    receiver_cfg.mkdir(parents=True, exist_ok=True)

    # Configure dual-home URLs on sender (`url` for legacy + `urls` / `BUS_URLS` for v2 I3).
    monkeypatch.setenv("BUS_CONFIG_DIR", str(sender_cfg))
    monkeypatch.setenv("BUS_URLS", f"{hub_a.ws_url},{hub_b_url}")
    bus_config.save_config({"url": hub_a.ws_url, "urls": [hub_a.ws_url, hub_b_url]})

    req_id = "r05-msg-1"
    payload = {
        "v": 2,
        "op": "send",
        "id": req_id,
        "corr_id": req_id,
        "kind": "task",
        "to": "cat-food/rx",
        "text": make_ask_prompt(req_id, "must-survive-hub-a-loss"),
        "hop": 0,
    }
    oracle.ledger.record_sent(req_id, to="cat-food/rx")
    bus_client.append_outbox(payload, sender_cfg)
    assert len(bus_client.load_outbox(sender_cfg)) == 1

    # Step 1: Receiver `cat-food/rx` and Hub B are offline. Sender flushes outbox to Hub A.
    async with websockets.connect(hub_a.ws_url) as tx_ws:
        await tx_ws.send(json.dumps(bus_client.build_ws_hello("cli-tx", 10**9, accept_inbox=False)))
        await asyncio.wait_for(tx_ws.recv(), 2.0)  # banner

        ack_events: dict[str, asyncio.Event] = {}

        async def _pump_acks() -> None:
            async for raw in tx_ws:
                frame = json.loads(raw)
                if frame.get("op") == "sys" and frame.get("ack"):
                    oracle.ledger.record_hub_ack(req_id, hub="hub_a", seq=frame.get("seq"))
                    ev = ack_events.get(str(frame["ack"]))
                    if ev is not None:
                        ev.set()

        ack_task = asyncio.create_task(_pump_acks())
        try:
            await bus_client._flush_outbox_over_ws(tx_ws, sender_cfg, ack_events)
        finally:
            ack_task.cancel()
            await asyncio.gather(ack_task, return_exceptions=True)

    hub_a_snapshot = hub_a.inspect_db(consumer="cat-food/rx")
    outbox_after_hub_a_ack = bus_client.load_outbox(sender_cfg)

    # Step 2: Hub A crashes catastrophically (SIGKILL) before receiver ever connects.
    hub_a.sigkill()

    # Step 3: Empty-DB Hub B comes online. Sender daemon + Receiver connect to Hub B.
    hub_b = hub_factory.start_hub_b(port=HUB_B_PORT, local_node="cat-food")
    bus_config.save_config({"url": hub_b.ws_url, "urls": [hub_a.ws_url, hub_b.ws_url]})

    # Start sender node daemon (so outbox pump flushes any retained items to Hub B)
    monkeypatch.setenv("BUS_CONFIG_DIR", str(sender_cfg))
    tx_daemon = node_runner.start(url=hub_b.ws_url, label="txdaemon", mode="watch", peers="cat-food")
    await asyncio.sleep(0.05)

    # Start receiver worker `cat-food/rx` on Hub B
    monkeypatch.setenv("BUS_CONFIG_DIR", str(receiver_cfg))
    rx_daemon = node_runner.start(url=hub_b.ws_url, label="rx", mode="hybrid", peers="cat-food")

    barrier_id = "r05-hub-b-barrier"
    async with websockets.connect(hub_b.ws_url) as probe_ws:
        await probe_ws.send(json.dumps({"v": 2, "label": "cli-probe", "since": 10**9, "accept_inbox": False}))
        await asyncio.wait_for(probe_ws.recv(), 2.0)

        async def _both_daemons_online() -> bool:
            await probe_ws.send(json.dumps({"op": "peers"}))
            resp = json.loads(await asyncio.wait_for(probe_ws.recv(), 2.0))
            peers_now = set(resp.get("peers") or [])
            return {"cat-food/txdaemon", "cat-food/rx"}.issubset(peers_now)

        await _wait_until(_both_daemons_online, timeout=3.0)
        await asyncio.sleep(0.1)  # allow txdaemon outbox pump to flush any retained entries
        hub_b_before_barrier = hub_b.inspect_db(consumer="cat-food/rx")

        await probe_ws.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": barrier_id,
                    "to": "cat-food/rx",
                    "kind": "task",
                    "text": make_ask_prompt(barrier_id, "verify-rx-alive"),
                }
            )
        )
        await _recv_hub_ack(probe_ws, barrier_id)

    await _wait_until(lambda: oracle.counter.count(barrier_id) == 1, timeout=3.0)

    await node_runner.stop_task(tx_daemon)
    await node_runner.stop_task(rx_daemon)

    hub_b_req_ids = [m["id"] for m in hub_b_before_barrier["messages"]]
    executed_count = oracle.counter.count(req_id)

    # v2 Acceptance Criterion: message was NOT dropped from sender journal before Hub B ACK,
    # so Hub B received it and `cat-food/rx` executed `r05-msg-1` exactly once (loss == 0).
    assert executed_count == 1, (
        f"R05 message loss reproduced: Hub A stored {len(hub_a_snapshot['messages'])} msg(s) and ACKed, "
        f"sender_outbox_after_hub_a_ack={outbox_after_hub_a_ack} (prematurely emptied before Hub B ACK), "
        f"hub_b_republished_ids={hub_b_req_ids} (missing {req_id!r}), "
        f"receiver_executed_count={executed_count} (expected 1)"
    )


@pytest.mark.chaos
async def test_r07_reply_first_on_hub_b_sticks_pending_and_ext_403(
    hub_factory: HubController,
    oracle: ChaosOracle,
) -> None:
    """R07: RPC request lands on Hub A, Hub A dies, and worker's RPC reply arrives first at empty Hub B.

    v2 Acceptance Criterion (G5, F08, X08):
    1) Hub B independently upserts terminal RPC result (`status='ok'`, `text='R07_ANSWER_42'`)
       even when `corr_id` was not previously seen as a request on Hub B (`mid != corr_id`).
    2) External caller `ext-caller` owning the RPC target/request queries `/v1/rpc/<corr_id>`
       on Hub B with HTTP 200 (not 403) and retrieves the terminal result.
    """
    gw_a = pick_free_port()
    gw_b = pick_free_port()
    hub_a = hub_factory.create_hub("hub_a", port=HUB_A_PORT, gateway_port=gw_a, local_node="cat-food")
    tok = hub_a.create_token(
        "caller",
        scopes=["rpc:cat-food/main", "send:cat-food/inbox", "poll:ext-caller"],
        allow_exec=True,
    )
    hub_b = hub_factory.create_hub("hub_b", port=HUB_B_PORT, gateway_port=gw_b, local_node="cat-food")
    hub_b.copy_auth_from(hub_a)

    hub_a.start()

    corr_id = "r07-req-1"
    reply_id = "r07-rep-1"
    expected_answer = "R07_ANSWER_42"

    # Step 1: External caller submits RPC request to Hub A via HTTP Gateway.
    oracle.ledger.record_sent(corr_id, caller=tok["node"], hub="hub_a")
    send_code, send_body = await asyncio.to_thread(
        _signed_http_json,
        hub_a.gateway_http_url,
        "/v1/send",
        token_id=tok["id"],
        secret=tok["secret"],
        method="POST",
        payload={
            "v": 2,
            "id": corr_id,
            "corr_id": corr_id,
            "kind": "rpc",
            "to": "cat-food/main",
            "text": make_ask_prompt(corr_id, "compute-42"),
            "label": "extcli",
        },
    )
    assert send_code == 200 and send_body["ok"] is True
    oracle.ledger.record_hub_ack(corr_id, hub="hub_a", seq=send_body["seq"])

    # Worker pulls the RPC request from Hub A before Hub A crashes.
    async with websockets.connect(hub_a.ws_url) as worker_ws_a:
        await worker_ws_a.send(json.dumps({"v": 2, "label": "main", "since": 0, "accept_inbox": True}))
        await asyncio.wait_for(worker_ws_a.recv(), 2.0)  # banner
        req_msg = json.loads(await asyncio.wait_for(worker_ws_a.recv(), 2.0))
        assert req_msg["id"] == corr_id
        oracle.ledger.record_received(corr_id, seq=req_msg["seq"])

    # Step 2: Hub A crashes (SIGKILL) while worker executes; empty Hub B comes online.
    hub_a.sigkill()
    hub_b.start()

    # Step 3: Worker finishes and sends terminal RPC reply (`mid != corr_id`) to Hub B first!
    async with websockets.connect(hub_b.ws_url) as worker_ws_b:
        await worker_ws_b.send(json.dumps({"v": 2, "label": "main", "since": 0, "accept_inbox": True}))
        await asyncio.wait_for(worker_ws_b.recv(), 2.0)
        await worker_ws_b.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": reply_id,
                    "corr_id": corr_id,
                    "kind": "rpc",
                    "status": "ok",
                    "to": req_msg["from"],  # "ext-caller/extcli"
                    "text": expected_answer,
                    "hop": 1,
                }
            )
        )
        rep_ack = json.loads(await asyncio.wait_for(worker_ws_b.recv(), 2.0))
        assert rep_ack.get("ack") == reply_id

        # Query RPC result from internal perspective on Hub B
        await worker_ws_b.send(json.dumps({"op": "rpc_result", "corr_id": corr_id}))
        internal_rpc = json.loads(await asyncio.wait_for(worker_ws_b.recv(), 2.0))

    # Step 4: External caller queries `/v1/rpc/r07-req-1` on Hub B's HTTP Gateway.
    ext_code, ext_body = await asyncio.to_thread(
        _signed_http_json,
        hub_b.gateway_http_url,
        f"/v1/rpc/{corr_id}",
        token_id=tok["id"],
        secret=tok["secret"],
        method="GET",
    )

    # v2 Acceptance Criterion: terminal status/text preserved on Hub B AND external caller gets 200 OK.
    assert (
        internal_rpc.get("status") == "ok"
        and internal_rpc.get("text") == expected_answer
        and ext_code == 200
        and ext_body.get("found") is True
        and ext_body.get("status") == "ok"
        and ext_body.get("text") == expected_answer
    ), (
        f"R07 reply-first RPC failure reproduced on Hub B: "
        f"internal_rpc=(status={internal_rpc.get('status')!r}, text={internal_rpc.get('text')!r}), "
        f"ext_http=(code={ext_code}, body={ext_body})"
    )


@pytest.mark.chaos
async def test_r08_higher_seq_cursor_implicitly_acks_unexecuted_lower_seq(
    sandbox_env: dict[str, pathlib.Path],
    hub_factory: HubController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """R08: Higher-seq fast event saves cursor=3 while unexecuted seq=2 job sits in memory queue.

    Scenario:
    - `seq=1` (`r08-blocker`) enters `ask_claude` and blocks on `gate_1`.
    - `seq=2` (`r08-victim`) is queued in `run_node`'s in-memory `jobs` queue (`0` executions!).
    - `seq=3` (`r08-fast-event`, `kind='event'`) arrives and immediately writes `cursor-main=3`.
    - Worker disconnects/restarts before `seq=2` ever starts, reconnecting with `since=3`.
    - Hub implicitly marks `seq <= 3` as `acked`; `r08-victim` (`seq=2`) is permanently lost!

    v2 Acceptance Criterion (G3+G4, F07, X07):
    Unexecuted lower-seq task `r08-victim` is durably persisted in node inbox journal (or cursor
    only advances to contiguous persisted/completed watermark) so after restart `r08-victim` is
    executed (`oracle.counter.count('r08-victim') == 1`).
    """
    hub_a = hub_factory.start_hub_a(port=HUB_A_PORT, local_node="cat-food")
    client_cfg = sandbox_env["cfg"]

    blocker_id = "r08-blocker"
    victim_id = "r08-victim"
    event_id = "r08-fast-event"
    probe_id = "r08-post-restart-probe"

    gate_1 = oracle.stub.gate(blocker_id)

    # Start worker generation #1
    worker_task_1 = node_runner.start(url=hub_a.ws_url, label="main", mode="hybrid", peers="cat-food")

    async with websockets.connect(hub_a.ws_url) as tx_ws:
        await tx_ws.send(json.dumps({"v": 2, "label": "cli-tx", "since": 10**9, "accept_inbox": False}))
        await asyncio.wait_for(tx_ws.recv(), 2.0)

        # Wait for `cat-food/main` to register on Hub A
        async def _worker_online() -> bool:
            await tx_ws.send(json.dumps({"op": "peers"}))
            resp = json.loads(await asyncio.wait_for(tx_ws.recv(), 2.0))
            return "cat-food/main" in (resp.get("peers") or [])

        await _wait_until(_worker_online, timeout=3.0)

        # 1. Send seq=1 (`blocker_id`) -> blocks inside `ask_claude`
        oracle.ledger.record_sent(blocker_id)
        await tx_ws.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": blocker_id,
                    "to": "cat-food/main",
                    "kind": "task",
                    "text": make_ask_prompt(blocker_id, "hold-worker"),
                }
            )
        )
        ack1 = await _recv_hub_ack(tx_ws, blocker_id)
        assert ack1["seq"] == 1
        await gate_1.wait_entered(timeout=3.0)

        # 2. Send seq=2 (`victim_id`) -> sits unexecuted behind `blocker_id`
        oracle.ledger.record_sent(victim_id)
        await tx_ws.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": victim_id,
                    "to": "cat-food/main",
                    "kind": "task",
                    "text": make_ask_prompt(victim_id, "unexecuted-lower-seq-job"),
                }
            )
        )
        ack2 = await _recv_hub_ack(tx_ws, victim_id)
        assert ack2["seq"] == 2

        # 3. Send seq=3 (`event_id`, non-`!ask` watch message) -> immediately saves cursor=3 & ACKs!
        await tx_ws.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": event_id,
                    "to": "cat-food/main",
                    "kind": "task",
                    "text": "plain-watch-note-advances-cursor",
                }
            )
        )
        ack3 = await _recv_hub_ack(tx_ws, event_id)
        assert ack3["seq"] == 3

        # Wait until Hub A observes seq=3 ACKed and client cursor on disk reached 3
        await _wait_until(lambda: bus_client.load_cursor("main", client_cfg) >= 3, timeout=3.0)
        assert oracle.counter.count(victim_id) == 0, "Victim seq=2 must still be unexecuted before crash"

        # 4. Simulate worker crash/restart while seq=2 (`victim_id`) is still unexecuted
        saved_cursor_before_restart = bus_client.load_cursor("main", client_cfg)
        await node_runner.stop_task(worker_task_1)
        gate_1.release()

        # Start worker generation #2 (same node/cfg_dir -> reconnects to Hub A)
        worker_task_2 = node_runner.start(url=hub_a.ws_url, label="main", mode="hybrid", peers="cat-food")
        await _wait_until(_worker_online, timeout=3.0)

        # Send seq=4 probe so we deterministically know generation #2 drained all queued work
        await tx_ws.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": probe_id,
                    "to": "cat-food/main",
                    "kind": "task",
                    "text": make_ask_prompt(probe_id, "drain-barrier"),
                }
            )
        )
        await _recv_hub_ack(tx_ws, probe_id)

    await _wait_until(lambda: oracle.counter.count(probe_id) == 1, timeout=3.0)
    await node_runner.stop_task(worker_task_2)

    db_state = hub_a.inspect_db(consumer="cat-food/main")
    delivery_states = {d["seq"]: d["state"] for d in db_state["deliveries"]}
    victim_exec_count = oracle.counter.count(victim_id)

    # v2 Acceptance Criterion: unexecuted `victim_id` (seq=2) must NOT be lost (`count == 1`).
    assert victim_exec_count == 1, (
        f"R08 implicit-ACK task loss reproduced: saved_cursor_before_restart={saved_cursor_before_restart}, "
        f"hub_delivery_states={delivery_states} (seq=2 implicitly marked acked on reconnect!), "
        f"victim_executed_count={victim_exec_count} (expected 1), all_executed={oracle.counter.all_ids()}"
    )


@pytest.mark.chaos
async def test_r16_reconnect_reclaim_causes_duplicate_execution(
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """R16: Worker disconnects mid-`ask_claude`, reconnects, and executes the same `id` twice.

    Scenario:
    - Worker `cat-food/main` connects to Hub A (`18765`) via `TcpFaultProxy` (`18785`).
    - Request `r16-req-1` (`seq=1`) enters `ask_claude` stub (execution #1 recorded in
      `SideEffectCounter`, paused on `gate`).
    - `await proxy.cut()` severs the active WS TCP session while `ask_claude` is still running.
    - `run_node` reconnects with `since=0`; Hub A's `reclaim_on_reconnect=True` immediately
      replays `r16-req-1` (`attempts=2`).
    - `gate.release()` lets execution #1 finish, after which `worker` pulls the duplicate from
      `jobs` and executes `r16-req-1` a SECOND time!

    v2 Acceptance Criterion (G3+G4, F03, X02):
    `SideEffectCounter.count('r16-req-1') == 1` (at-most-once `!ask` execution per `id`).
    """
    hub_a = hub_factory.start_hub_a(port=HUB_A_PORT, local_node="cat-food")
    proxy = await proxy_factory.start_proxy(listen_port=PROXY_PORT, upstream_port=HUB_A_PORT)

    req_id = "r16-req-1"
    gate = oracle.stub.gate(req_id, only_first=True)

    worker_task = node_runner.start(url=proxy.ws_url, label="main", mode="hybrid", peers="cat-food")

    async with websockets.connect(hub_a.ws_url) as tx_ws:
        await tx_ws.send(json.dumps({"v": 2, "label": "cli-tx", "since": 10**9, "accept_inbox": False}))
        await asyncio.wait_for(tx_ws.recv(), 2.0)

        async def _worker_online() -> bool:
            await tx_ws.send(json.dumps({"op": "peers"}))
            resp = json.loads(await asyncio.wait_for(tx_ws.recv(), 2.0))
            return "cat-food/main" in (resp.get("peers") or [])

        await _wait_until(_worker_online, timeout=3.0)

        # Send `r16-req-1` to `cat-food/main`
        oracle.ledger.record_sent(req_id, to="cat-food/main")
        await tx_ws.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": req_id,
                    "corr_id": req_id,
                    "kind": "rpc",
                    "to": "cat-food/main",
                    "text": make_ask_prompt(req_id, "non-idempotent-mutation"),
                }
            )
        )
        send_ack = await _recv_hub_ack(tx_ws, req_id)
        oracle.ledger.record_hub_ack(req_id, hub="hub_a", seq=send_ack["seq"])

        # Wait until worker enters `ask_claude` (execution #1 recorded on disk)
        await gate.wait_entered(timeout=3.0)
        assert oracle.counter.count(req_id) == 1

        # Cut the worker's WS TCP connection via `127.0.0.1:18785` proxy while execution #1 is running!
        severed = await proxy.cut()
        assert severed >= 1

        # Wait until `run_node` reconnects after `proxy.cut()`:
        # - On pre-I1 baseline, Hub A re-leases `seq=1` (`attempts >= 2`).
        # - On v2 I1 (G3 inbox-commit-then-ACK), `seq=1` was already ACKed on disk commit
        #   before `proxy.cut()`, so `run_node` opens session #2 (`proxy.accepted_count >= 2`)
        #   and re-registers `cat-food/main` without duplicate replay.
        async def _reclaimed_on_reconnect() -> bool:
            dels = hub_a.inspect_db(consumer="cat-food/main")["deliveries"]
            if dels and int(dels[0]["attempts"]) >= 2:
                return True
            if proxy.accepted_count >= 2 and dels and dels[0]["state"] == "acked":
                return await _worker_online()
            return False

        await _wait_until(_reclaimed_on_reconnect, timeout=4.0)

        # Release execution #1 and send a FIFO drain barrier job to deterministically await queue completion
        gate.release()
        barrier_id = "r16-drain-barrier"
        await tx_ws.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": barrier_id,
                    "to": "cat-food/main",
                    "kind": "task",
                    "text": make_ask_prompt(barrier_id, "fifo-barrier"),
                }
            )
        )
        await _recv_hub_ack(tx_ws, barrier_id)

    await _wait_until(lambda: oracle.counter.count(barrier_id) == 1, timeout=3.0)
    await node_runner.stop_task(worker_task)

    dels = hub_a.inspect_db(consumer="cat-food/main")["deliveries"]
    attempts = int(dels[0]["attempts"]) if dels else 0
    exec_count = oracle.counter.count(req_id)

    # v2 Acceptance Criterion: `!ask` side-effect executes AT MOST ONCE (`count == 1`).
    assert exec_count == 1, (
        f"R16 duplicate execution reproduced: req_id={req_id!r} executed {exec_count} times "
        f"(expected 1) across hub_delivery_attempts={attempts}; "
        f"req_records={len(oracle.counter.records(req_id))}, all_ids={oracle.counter.all_ids()}"
    )
