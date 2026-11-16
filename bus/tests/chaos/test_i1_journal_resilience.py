"""WBS `I1` resilience & chaos tests (`X02`, `X07`, `X12`) for node-level SQLite journal.

Verifies:
- `X02`: Crash before claim (`queued`), after claim before side-effect (`running`),
  real OS `SIGKILL` during `ask_claude`, crash after `ask_claude` before DB save,
  crash after atomic DB save (`inbox=done` + `outbox=pending`) before WS send,
  concurrent dual-label (`main` + `aux`) shared `journal.db` atomic claim & live sibling
  protection during recovery, and duplicate `id` WS delivery deduplication.
- `X07`: Slow `!ask` execution under interleaved valid tasks, plain watch notes, and
  `event` deliveries + 3 TCP fault proxy `cut()` reconnections + `deadline` boundary
  (queued-then-expired vs already-expired vs 24h `event` retention).
- `X12`: Default `RATE_PER_MIN=20` preservation when `BUS_TEST_RATE_PER_MIN` is unset,
  rate-limit burst rejection never mistaken for ACK, `oneshot` 32-hex ID preservation across
  failure/retry, legacy `outbox.jsonl` -> `outbox.jsonl.migrated` atomic migration,
  concurrent multi-process + multi-thread `outbox` writers (`0` SQLite lock errors),
  `600`-item backlog (`>500` replay cap) full drain & receive via `abus journal drain`
  and `abus journal status`.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import pathlib
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any

import pytest
import websockets

from agent_bus import client as bus_client
from agent_bus import config as bus_config
from agent_bus.journal import NodeJournal
from .conftest import NodeRunner
from .hub_proc import BUS_ROOT, HUB_A_PORT, HUB_B_PORT, HubController
from .oracle import ChaosOracle, make_ask_prompt
from .tcp_proxy import PROXY_PORT, ProxyController


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


async def _recv_hub_ack(ws: Any, expected_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        rem = max(0.05, deadline - time.monotonic())
        raw = await asyncio.wait_for(ws.recv(), timeout=rem)
        frame = json.loads(raw)
        if frame.get("op") == "sys" and frame.get("error"):
            raise RuntimeError(f"Hub rejected send for {expected_id}: {frame}")
        if frame.get("op") == "sys" and frame.get("ack") == expected_id:
            return frame


class _SimulatedCrash(RuntimeError):
    """Deterministic crash exception that terminates `run_node` immediately without retry."""

    _fatal_node_crash = True


class _ReplyCollector:
    """Subscribes as `cat-food/cli-caller` on a hub and collects incoming reply messages by `corr_id`."""

    def __init__(self, ws_url: str, label: str = "cli-caller") -> None:
        self.ws_url = ws_url
        self.label = label
        self.replies: dict[str, list[dict[str, Any]]] = {}
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._ack_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> "_ReplyCollector":
        self._ws = await websockets.connect(self.ws_url, ping_interval=20, ping_timeout=60)
        await self._ws.send(
            json.dumps({"v": 2, "label": self.label, "since": 0, "accept_inbox": False})
        )
        await asyncio.wait_for(self._ws.recv(), 3.0)  # banner
        self._task = asyncio.create_task(self._pump())
        return self

    async def _pump(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                frame = json.loads(raw)
                op = frame.get("op")
                if op == "sys":
                    ack_id = str(frame.get("ack") or "")
                    if ack_id and ack_id in self._ack_waiters and not self._ack_waiters[ack_id].done():
                        self._ack_waiters[ack_id].set_result(frame)
                    elif frame.get("peers") is not None and "__peers__" in self._ack_waiters:
                        fut = self._ack_waiters.pop("__peers__")
                        if not fut.done():
                            fut.set_result(frame)
                elif op == "msg":
                    await self._ws.send(
                        json.dumps({"op": "ack", "seq": frame["seq"], "id": frame.get("id")})
                    )
                    cid = str(frame.get("corr_id") or frame.get("id") or "")
                    self.replies.setdefault(cid, []).append(frame)
        except Exception:
            pass

    async def peers(self) -> list[str]:
        assert self._ws is not None
        async with self._send_lock:
            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._ack_waiters["__peers__"] = fut
            await self._ws.send(json.dumps({"op": "peers"}))
            resp = await asyncio.wait_for(fut, 3.0)
            return list(resp.get("peers") or [])

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ws is not None
        mid = str(payload["id"])
        async with self._send_lock:
            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._ack_waiters[mid] = fut
            try:
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
                return await asyncio.wait_for(fut, 5.0)
            finally:
                self._ack_waiters.pop(mid, None)

    def first_reply(self, corr_id: str) -> dict[str, Any] | None:
        items = self.replies.get(corr_id)
        return items[0] if items else None

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()


@pytest.mark.chaos
async def test_x02_crash_boundaries_sigkill_and_post_save_outbox_recovery(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    sandbox_env: dict[str, pathlib.Path],
    hub_factory: HubController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """X02 (Part 1): Crash before claim, after claim before exec, mid-exec SIGKILL,
    after exec before DB commit, and after DB commit before WS send."""
    hub_a = hub_factory.start_hub_a(
        port=HUB_A_PORT,
        local_node="cat-food",
    )
    client_cfg = sandbox_env["cfg"]
    collector = await _ReplyCollector(hub_a.ws_url, label="cli-x02").start()
    spawned_procs: list[subprocess.Popen[str]] = []

    def _cleanup_subprocs() -> None:
        for proc in spawned_procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5.0)
                except Exception:
                    pass

    try:
        # ------------------------------------------------------------------
        # 1) Crash right AFTER inbox commit + hub ACK, BEFORE worker claims (`queued`)
        # ------------------------------------------------------------------
        req_pre_queued = "x02-pre-queued"

        def _hook_pre_queued(stage: str, row: dict[str, Any]) -> None:
            if stage == "after_inbox_ack_before_claim" and row.get("id") == req_pre_queued:
                raise _SimulatedCrash("crash before claim")

        monkeypatch.setattr(bus_client, "_TEST_CRASH_HOOK", _hook_pre_queued)
        w1 = node_runner.start(url=hub_a.ws_url, label="main", mode="hybrid", peers="cat-food")
        await _wait_until(lambda: collector.peers(), timeout=3.0)
        await _wait_until(
            lambda: asyncio.ensure_future(collector.peers()),
            timeout=3.0,
        )
        while "cat-food/main" not in await collector.peers():
            await asyncio.sleep(0.02)

        await collector.send(
            {
                "v": 2,
                "op": "send",
                "id": req_pre_queued,
                "corr_id": req_pre_queued,
                "kind": "rpc",
                "to": "cat-food/main",
                "text": make_ask_prompt(req_pre_queued, "crash-while-queued"),
            }
        )
        await _wait_until(lambda: w1.done(), timeout=3.0)
        await node_runner.stop_task(w1)
        monkeypatch.setattr(bus_client, "_TEST_CRASH_HOOK", None)

        assert oracle.counter.count(req_pre_queued) == 0
        with NodeJournal(client_cfg) as j:
            row_pq = j.get_inbox_row(req_pre_queued)
            assert row_pq is not None and row_pq["state"] == "queued"

        # ------------------------------------------------------------------
        # 2) Crash AFTER atomic claim (`running`), BEFORE `ask_claude` runs
        # ------------------------------------------------------------------
        req_pre_exec = "x02-after-claim-before-exec"

        def _hook_pre_exec(stage: str, row: dict[str, Any]) -> None:
            if stage == "after_claim_before_exec" and row.get("id") == req_pre_exec:
                raise _SimulatedCrash("crash after claim before ask_claude")

        monkeypatch.setattr(bus_client, "_TEST_CRASH_HOOK", _hook_pre_exec)
        w2 = node_runner.start(url=hub_a.ws_url, label="main", mode="hybrid", peers="cat-food")
        # Note: w2 will first execute `req_pre_queued` cleanly, then crash on `req_pre_exec`!
        await _wait_until(lambda: collector.first_reply(req_pre_queued) is not None, timeout=4.0)
        assert oracle.counter.count(req_pre_queued) == 1
        assert collector.first_reply(req_pre_queued)["status"] == "ok"

        await collector.send(
            {
                "v": 2,
                "op": "send",
                "id": req_pre_exec,
                "corr_id": req_pre_exec,
                "kind": "rpc",
                "to": "cat-food/main",
                "text": make_ask_prompt(req_pre_exec, "crash-before-side-effect"),
            }
        )
        await _wait_until(lambda: w2.done(), timeout=3.0)
        await node_runner.stop_task(w2)
        monkeypatch.setattr(bus_client, "_TEST_CRASH_HOOK", None)
        assert oracle.counter.count(req_pre_exec) == 0
        with NodeJournal(client_cfg) as j:
            row_pe = j.get_inbox_row(req_pre_exec)
            assert row_pe is not None and row_pe["state"] == "running"

        # ------------------------------------------------------------------
        # 3) Real OS subprocess worker + `SIGKILL` (`kill -9`) mid-`ask_claude`
        # ------------------------------------------------------------------
        req_sigkill = "x02-mid-sigkill"
        sigkill_marker = tmp_path / "sigkill_entered.marker"
        stub_script = tmp_path / "x02_subproc_claude_stub.py"
        stub_script.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, re, sys, time\n"
            "raw = sys.stdin.read()\n"
            r"m = re.search(r'\[(?:req_)?id=([^\]\s]+)\]', raw)" + "\n"
            "mid = m.group(1) if m else 'unknown'\n"
            f"counter_path = {str(oracle.counter.path)!r}\n"
            "rec = {'seq': 0, 'id': mid, 'from': 'subproc', 'text': raw[-120:], 'ts': time.time(), 'pid': os.getpid()}\n"
            "with open(counter_path, 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps(rec, ensure_ascii=False) + '\\n')\n"
            "    fh.flush()\n"
            "    os.fsync(fh.fileno())\n"
            f"if mid == {req_sigkill!r}:\n"
            f"    pathlib.Path({str(sigkill_marker)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
            "    time.sleep(30.0)\n"
            "sys.stdout.write(f'STUB_OK:{mid}\\n')\n",
            encoding="utf-8",
        )
        stub_script.chmod(stub_script.stat().st_mode | stat.S_IRWXU)

        sub_env = os.environ.copy()
        sub_env["PYTHONPATH"] = str(BUS_ROOT)
        sub_env["BUS_CONFIG_DIR"] = str(client_cfg)
        sub_env["BUS_CLAUDE_CMD"] = str(stub_script)
        sub_proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "agent_bus.cli",
                "run",
                "--label",
                "main",
                "--mode",
                "hybrid",
                "--peers",
                "cat-food",
                "--url",
                hub_a.ws_url,
                "--no-auto-update",
            ],
            env=sub_env,
            cwd=str(sandbox_env["home"]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        spawned_procs.append(sub_proc)

        # Wait until subprocess recovers `req_pre_exec` -> `exec_uncertain` and registers online
        await _wait_until(lambda: collector.first_reply(req_pre_exec) is not None, timeout=5.0)
        assert collector.first_reply(req_pre_exec)["status"] == "exec_uncertain"
        assert oracle.counter.count(req_pre_exec) == 0

        # Send `req_sigkill` -> wait until stub fsyncs side-effect and writes marker -> SIGKILL!
        await collector.send(
            {
                "v": 2,
                "op": "send",
                "id": req_sigkill,
                "corr_id": req_sigkill,
                "kind": "rpc",
                "to": "cat-food/main",
                "text": make_ask_prompt(req_sigkill, "kill-9-during-claude"),
            }
        )
        await _wait_until(lambda: sigkill_marker.exists(), timeout=5.0)
        assert oracle.counter.count(req_sigkill) == 1

        os.killpg(sub_proc.pid, signal.SIGKILL)
        sub_proc.wait(timeout=5.0)

        with NodeJournal(client_cfg) as j:
            row_sk = j.get_inbox_row(req_sigkill)
            assert row_sk is not None and row_sk["state"] == "running"

        # ------------------------------------------------------------------
        # 4) Crash AFTER `ask_claude` returns, BEFORE saving result (`done`+`outbox`)
        # ------------------------------------------------------------------
        req_before_save = "x02-after-exec-before-save"

        def _hook_before_save(stage: str, row: dict[str, Any]) -> None:
            if stage == "after_exec_before_save" and row.get("id") == req_before_save:
                raise _SimulatedCrash("crash after ask_claude before journal save")

        monkeypatch.setattr(bus_client, "_TEST_CRASH_HOOK", _hook_before_save)
        w4 = node_runner.start(url=hub_a.ws_url, label="main", mode="hybrid", peers="cat-food")
        # w4 recovers `req_sigkill` -> `uncertain` and notifies caller!
        await _wait_until(lambda: collector.first_reply(req_sigkill) is not None, timeout=4.0)
        assert collector.first_reply(req_sigkill)["status"] == "exec_uncertain"
        assert oracle.counter.count(req_sigkill) == 1
        with NodeJournal(client_cfg) as j:
            row_sk_after = j.get_inbox_row(req_sigkill)
            assert row_sk_after is not None
            assert row_sk_after["state"] == "uncertain"
            assert row_sk_after["result_status"] == "exec_uncertain"

        await collector.send(
            {
                "v": 2,
                "op": "send",
                "id": req_before_save,
                "corr_id": req_before_save,
                "kind": "rpc",
                "to": "cat-food/main",
                "text": make_ask_prompt(req_before_save, "crash-before-commit"),
            }
        )
        await _wait_until(lambda: w4.done(), timeout=3.0)
        await node_runner.stop_task(w4)
        monkeypatch.setattr(bus_client, "_TEST_CRASH_HOOK", None)
        assert oracle.counter.count(req_before_save) == 1

        # ------------------------------------------------------------------
        # 5) Crash AFTER atomic save (`inbox='done'` + `outbox='pending'`), BEFORE WS send
        # ------------------------------------------------------------------
        req_after_save = "x02-after-save-before-send"

        def _hook_after_save(stage: str, row: dict[str, Any]) -> None:
            if stage == "after_save_before_send" and row.get("id") == req_after_save:
                raise _SimulatedCrash("crash after journal commit before WS send")

        monkeypatch.setattr(bus_client, "_TEST_CRASH_HOOK", _hook_after_save)
        w5 = node_runner.start(url=hub_a.ws_url, label="main", mode="hybrid", peers="cat-food")
        await _wait_until(lambda: collector.first_reply(req_before_save) is not None, timeout=4.0)
        assert collector.first_reply(req_before_save)["status"] == "exec_uncertain"
        assert oracle.counter.count(req_before_save) == 1

        await collector.send(
            {
                "v": 2,
                "op": "send",
                "id": req_after_save,
                "corr_id": req_after_save,
                "kind": "rpc",
                "to": "cat-food/main",
                "text": make_ask_prompt(req_after_save, "saved-before-crash"),
            }
        )
        await _wait_until(lambda: w5.done(), timeout=3.0)
        await node_runner.stop_task(w5)
        monkeypatch.setattr(bus_client, "_TEST_CRASH_HOOK", None)

        # Verify: `inbox` is ALREADY `done`, `outbox` holds `pending` reply, and caller has not received it yet!
        assert collector.first_reply(req_after_save) is None
        assert oracle.counter.count(req_after_save) == 1
        with NodeJournal(client_cfg) as j:
            row_as = j.get_inbox_row(req_after_save)
            assert row_as is not None and row_as["state"] == "done"
            pending_out = j.list_outbox_pending()
            matching_out = [p for p in pending_out if p.get("corr_id") == req_after_save]
            assert len(matching_out) == 1
            saved_reply_id = str(matching_out[0]["id"])
            assert len(saved_reply_id) == 32

        # Final recovery worker w6: must NOT re-execute `req_after_save`, and must flush exact `saved_reply_id`!
        w6 = node_runner.start(url=hub_a.ws_url, label="main", mode="hybrid", peers="cat-food")
        await _wait_until(lambda: collector.first_reply(req_after_save) is not None, timeout=4.0)
        rep_as = collector.first_reply(req_after_save)
        assert rep_as is not None
        assert rep_as["status"] == "ok"
        assert "STUB_OK:x02-after-save-before-send" in rep_as["text"]
        assert rep_as["id"] == saved_reply_id
        assert oracle.counter.count(req_after_save) == 1

        with NodeJournal(client_cfg) as j:
            out_row = j.get_outbox_row(saved_reply_id)
            assert out_row is not None and out_row["state"] == "done"

        await node_runner.stop_task(w6)

        # Global X02 invariant: every request executed <= 1 time!
        for rid in (req_pre_queued, req_pre_exec, req_sigkill, req_before_save, req_after_save):
            assert oracle.counter.count(rid) <= 1, f"{rid} executed > 1 time!"
    finally:
        monkeypatch.setattr(bus_client, "_TEST_CRASH_HOOK", None)
        _cleanup_subprocs()
        await collector.close()


@pytest.mark.chaos
async def test_x02_dual_label_shared_journal_live_sibling_and_duplicate_id(
    monkeypatch: pytest.MonkeyPatch,
    sandbox_env: dict[str, pathlib.Path],
    hub_factory: HubController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """X02 (Part 2): Concurrent `main` + `aux` labels sharing `<cfg_dir>/journal.db`,
    crash recovery sparing live sibling's `running` item, bare-node broadcast contention,
    and duplicate `id` WS delivery deduplication."""
    hub_a = hub_factory.start_hub(
        "hub_a",
        port=HUB_A_PORT,
        local_node="cat-food",
        extra_env={"BUS_TEST_RATE_PER_MIN": "500"},
    )
    client_cfg = sandbox_env["cfg"]
    collector = await _ReplyCollector(hub_a.ws_url, label="cli-dual").start()

    try:
        live_main_id = "x02-dual-live-main"
        crash_aux_id = "x02-dual-crash-aux"
        gate_main = oracle.stub.gate(live_main_id)

        def _crash_only_aux(stage: str, row: dict[str, Any]) -> None:
            if stage == "after_exec_before_save" and row.get("id") == crash_aux_id:
                raise _SimulatedCrash("aux crashed mid-flight")

        monkeypatch.setattr(bus_client, "_TEST_CRASH_HOOK", _crash_only_aux)

        t_main = node_runner.start(url=hub_a.ws_url, label="main", mode="hybrid", peers="cat-food")
        t_aux_1 = node_runner.start(url=hub_a.ws_url, label="aux", mode="hybrid", peers="cat-food")

        async def _both_labels_online() -> bool:
            p = set(await collector.peers())
            return {"cat-food/main", "cat-food/aux"}.issubset(p)

        await _wait_until(_both_labels_online, timeout=4.0)

        # 1. `main` claims `live_main_id` and stays running inside `ask_claude`
        await collector.send(
            {
                "v": 2,
                "op": "send",
                "id": live_main_id,
                "corr_id": live_main_id,
                "kind": "rpc",
                "to": "cat-food/main",
                "text": make_ask_prompt(live_main_id, "main-long-running"),
            }
        )
        await gate_main.wait_entered(timeout=3.0)

        # 2. `aux` claims `crash_aux_id` and crashes while `main` is STILL running `live_main_id`!
        await collector.send(
            {
                "v": 2,
                "op": "send",
                "id": crash_aux_id,
                "corr_id": crash_aux_id,
                "kind": "rpc",
                "to": "cat-food/aux",
                "text": make_ask_prompt(crash_aux_id, "aux-crashes"),
            }
        )
        await _wait_until(lambda: t_aux_1.done(), timeout=3.0)
        await node_runner.stop_task(t_aux_1)
        monkeypatch.setattr(bus_client, "_TEST_CRASH_HOOK", None)

        # 3. Restart `aux` WHILE `main` is still blocked inside `live_main_id`!
        t_aux_2 = node_runner.start(url=hub_a.ws_url, label="aux", mode="hybrid", peers="cat-food")
        await _wait_until(lambda: collector.first_reply(crash_aux_id) is not None, timeout=4.0)
        assert collector.first_reply(crash_aux_id)["status"] == "exec_uncertain"

        # Verify `live_main_id` was NOT falsely marked `uncertain` by `aux`'s startup recovery!
        with NodeJournal(client_cfg) as j:
            row_main = j.get_inbox_row(live_main_id)
            assert row_main is not None and row_main["state"] == "running"

        gate_main.release()
        await _wait_until(lambda: collector.first_reply(live_main_id) is not None, timeout=4.0)
        assert collector.first_reply(live_main_id)["status"] == "ok"
        assert oracle.counter.count(live_main_id) == 1
        assert oracle.counter.count(crash_aux_id) == 1

        # 4. Bare-node fanout (`to='cat-food'`): Hub delivers EVERY message to BOTH `main` and `aux`!
        batch_ids = [f"x02-race-{i:02d}" for i in range(12)]
        for bid in batch_ids:
            await collector.send(
                {
                    "v": 2,
                    "op": "send",
                    "id": bid,
                    "corr_id": bid,
                    "kind": "rpc",
                    "to": "cat-food",
                    "text": make_ask_prompt(bid, "bare-node-fanout"),
                }
            )
        await _wait_until(
            lambda: all(collector.first_reply(bid) is not None for bid in batch_ids),
            timeout=6.0,
        )
        for bid in batch_ids:
            assert oracle.counter.count(bid) == 1, f"Duplicate claim on {bid}"
            assert collector.first_reply(bid)["status"] == "ok"

        # 5. Deliver the exact same `id` (`batch_ids[0]`) a 2nd time via Hub B (`18775`)!
        await node_runner.stop_task(t_aux_2)
        await node_runner.stop_task(t_main)
        cursor_before_dup = bus_client.load_cursor("main", client_cfg)
        assert cursor_before_dup >= 12

        hub_b = hub_factory.start_hub_b(port=HUB_B_PORT, local_node="cat-food")
        async with websockets.connect(hub_b.ws_url) as seed_ws:
            await seed_ws.send(json.dumps({"v": 2, "label": "cli-dup-seed", "since": 10**9, "accept_inbox": False}))
            await asyncio.wait_for(seed_ws.recv(), 2.0)
            await seed_ws.send(
                json.dumps(
                    {
                        "v": 2,
                        "op": "send",
                        "id": batch_ids[0],
                        "corr_id": batch_ids[0],
                        "kind": "rpc",
                        "to": "cat-food/main",
                        "text": make_ask_prompt(batch_ids[0], "bare-node-fanout"),
                    }
                )
            )
            await _recv_hub_ack(seed_ws, batch_ids[0])

        t_main_re = node_runner.start(url=hub_b.ws_url, label="main", mode="hybrid", peers="cat-food")
        await _wait_until(
            lambda: any(
                d["seq"] == 1 and d["state"] == "acked"
                for d in hub_b.inspect_db(consumer="cat-food/main")["deliveries"]
            ),
            timeout=4.0,
        )
        await node_runner.stop_task(t_main_re)

        # Duplicate `id` was ACKed on Hub B without overwriting `done`, re-executing, or decreasing `cursor-main`!
        assert oracle.counter.count(batch_ids[0]) == 1
        assert bus_client.load_cursor("main", client_cfg) == cursor_before_dup
        with NodeJournal(client_cfg) as j:
            dup_row = j.get_inbox_row(batch_ids[0])
            assert dup_row is not None and dup_row["state"] == "done"
    finally:
        monkeypatch.setattr(bus_client, "_TEST_CRASH_HOOK", None)
        await collector.close()


@pytest.mark.chaos
async def test_x07_slow_ask_interleaved_events_three_reconnects_and_deadline_boundary(
    sandbox_env: dict[str, pathlib.Path],
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """X07: Slow `!ask` execution + interleaved valid tasks/watch notes/events +
    3 TCP fault proxy `cut()` reconnections + `deadline` boundary (`queued -> expired`
    and arrival-expired vs 24h `event` deadline)."""
    hub_a = hub_factory.start_hub(
        "hub_a",
        port=HUB_A_PORT,
        local_node="cat-food",
        extra_env={"BUS_TEST_RATE_PER_MIN": "500"},
    )
    proxy = await proxy_factory.start_proxy(listen_port=PROXY_PORT, upstream_port=HUB_A_PORT)
    client_cfg = sandbox_env["cfg"]
    collector = await _ReplyCollector(hub_a.ws_url, label="cli-x07").start()

    try:
        slow_id = "x07-slow-head"
        gate_slow = oracle.stub.gate(slow_id)

        worker_task = node_runner.start(
            url=proxy.ws_url,
            label="main",
            mode="hybrid",
            peers="cat-food",
        )
        await _wait_until(
            lambda: asyncio.ensure_future(collector.peers()),
            timeout=3.0,
        )
        while "cat-food/main" not in await collector.peers():
            await asyncio.sleep(0.02)

        # 1. Start slow `!ask` so worker stays busy throughout 3 reconnects & message bursts
        await collector.send(
            {
                "v": 2,
                "op": "send",
                "id": slow_id,
                "corr_id": slow_id,
                "kind": "rpc",
                "to": "cat-food/main",
                "text": make_ask_prompt(slow_id, "slow-anchor-job"),
            }
        )
        await gate_slow.wait_entered(timeout=3.0)
        assert oracle.counter.count(slow_id) == 1

        valid_ids = [f"x07-valid-{i}" for i in range(8)]
        event_ids = [f"x07-event-{i}" for i in range(6)]
        watch_ids = [f"x07-watch-{i}" for i in range(4)]
        expire_queued_id = "x07-expire-while-queued"
        expire_arrival_id = "x07-expire-on-arrival"
        aged_event_id = "x07-aged-event-still-valid"

        # 2. Cut #1 (temporarily block proxy so we can set exact SQLite timestamps for deadline items)
        proxy.block(True, mode="reject", cut_existing=True)
        await collector.send(
            {
                "v": 2,
                "op": "send",
                "id": expire_queued_id,
                "corr_id": expire_queued_id,
                "kind": "rpc",
                "to": "cat-food/main",
                "text": make_ask_prompt(expire_queued_id, "expires-in-queued-state"),
            }
        )
        await collector.send(
            {
                "v": 2,
                "op": "send",
                "id": expire_arrival_id,
                "corr_id": expire_arrival_id,
                "kind": "rpc",
                "to": "cat-food/main",
                "text": make_ask_prompt(expire_arrival_id, "already-past-deadline"),
            }
        )
        await collector.send(
            {
                "v": 2,
                "op": "send",
                "id": aged_event_id,
                "corr_id": aged_event_id,
                "kind": "event",
                "to": "cat-food/main",
                "text": "event older than 1h but within 24h deadline",
            }
        )

        # Set deterministic timestamps on Hub A DB before unblocking reconnect #1:
        # - `expire_queued_id`: `ts = now - 3599.55` -> `deadline = now + 0.45s` (valid on arrival, expires while queued!)
        # - `expire_arrival_id`: `ts = now - 3615.0` -> `deadline = now - 15.0s` (already expired on arrival!)
        # - `aged_event_id`: `ts = now - 7200.0` -> `deadline = ts + 86400` (2h old event -> still valid!)
        now_mark = time.time()
        conn = sqlite3.connect(str(hub_a.db_path), timeout=5.0)
        try:
            conn.execute("UPDATE messages SET ts = ? WHERE id = ?", (now_mark - 3599.55, expire_queued_id))
            conn.execute("UPDATE messages SET ts = ? WHERE id = ?", (now_mark - 3615.0, expire_arrival_id))
            conn.execute("UPDATE messages SET ts = ? WHERE id = ?", (now_mark - 7200.0, aged_event_id))
            conn.commit()
        finally:
            conn.close()

        accepted_before_r1 = proxy.accepted_count
        proxy.unblock()

        async def _reconnected_after(prev_accepted: int) -> bool:
            return proxy.accepted_count > prev_accepted and ("cat-food/main" in await collector.peers())

        await _wait_until(lambda: _reconnected_after(accepted_before_r1), timeout=5.0)

        # Wait until `expire_queued_id` is durably recorded in `journal.db` `inbox` as `queued`
        def _queued_recorded() -> bool:
            with NodeJournal(client_cfg) as j:
                r = j.get_inbox_row(expire_queued_id)
                return r is not None and r["state"] == "queued"

        await _wait_until(_queued_recorded, timeout=3.0)

        # 3. Interleave valid tasks, events, and watch notes across Cuts #2 and #3!
        for idx in range(4):
            await collector.send(
                {
                    "v": 2,
                    "op": "send",
                    "id": valid_ids[idx],
                    "corr_id": valid_ids[idx],
                    "kind": "rpc",
                    "to": "cat-food/main",
                    "text": make_ask_prompt(valid_ids[idx], f"valid-batch-a-{idx}"),
                }
            )
        for idx in range(3):
            await collector.send(
                {
                    "v": 2,
                    "op": "send",
                    "id": event_ids[idx],
                    "kind": "event",
                    "to": "cat-food/inbox",
                    "text": f"live-event-a-{idx}",
                }
            )
        for idx in range(2):
            await collector.send(
                {
                    "v": 2,
                    "op": "send",
                    "id": watch_ids[idx],
                    "kind": "task",
                    "to": "cat-food/main",
                    "text": f"plain watch note {idx}",
                }
            )

        # Cut #2
        acc_r2 = proxy.accepted_count
        await proxy.cut()
        await _wait_until(lambda: _reconnected_after(acc_r2), timeout=5.0)

        for idx in range(4, 8):
            await collector.send(
                {
                    "v": 2,
                    "op": "send",
                    "id": valid_ids[idx],
                    "corr_id": valid_ids[idx],
                    "kind": "rpc",
                    "to": "cat-food/main",
                    "text": make_ask_prompt(valid_ids[idx], f"valid-batch-b-{idx}"),
                }
            )
        for idx in range(3, 6):
            await collector.send(
                {
                    "v": 2,
                    "op": "send",
                    "id": event_ids[idx],
                    "kind": "event",
                    "to": "cat-food/inbox",
                    "text": f"live-event-b-{idx}",
                }
            )
        for idx in range(2, 4):
            await collector.send(
                {
                    "v": 2,
                    "op": "send",
                    "id": watch_ids[idx],
                    "kind": "task",
                    "to": "cat-food/main",
                    "text": f"plain watch note {idx}",
                }
            )

        # Cut #3
        acc_r3 = proxy.accepted_count
        await proxy.cut()
        await _wait_until(lambda: _reconnected_after(acc_r3), timeout=5.0)

        # Ensure `expire_queued_id`'s deadline (`now_mark + 0.45s`) has definitely elapsed
        rem_sleep = (now_mark + 0.55) - time.time()
        if rem_sleep > 0:
            await asyncio.sleep(rem_sleep)

        # Release `slow_id` -> worker sweeps expired items first, then executes all 8 valid tasks!
        gate_slow.release()

        all_expected_replies = [slow_id, *valid_ids, expire_queued_id, expire_arrival_id]
        await _wait_until(
            lambda: all(collector.first_reply(cid) is not None for cid in all_expected_replies),
            timeout=8.0,
        )
        await node_runner.stop_task(worker_task)

        # Assertions:
        assert proxy.cut_count >= 3, f"Expected >=3 TCP cuts, got {proxy.cut_count}"
        assert proxy.accepted_count >= 4, f"Expected >=4 TCP sessions (initial + 3 reconnects), got {proxy.accepted_count}"

        # 1) 0 lost valid tasks, each executed exactly once with `status == 'ok'`
        for vid in [slow_id, *valid_ids]:
            assert oracle.counter.count(vid) == 1, f"Valid task {vid} count={oracle.counter.count(vid)}"
            rep = collector.first_reply(vid)
            assert rep is not None and rep["status"] == "ok"

        # 2) Expired tasks NEVER executed (`count == 0`), `inbox.state == 'expired'`, `status == 'expired'` reply delivered
        for eid in (expire_queued_id, expire_arrival_id):
            assert oracle.counter.count(eid) == 0, f"Expired task {eid} must NOT execute!"
            rep_e = collector.first_reply(eid)
            assert rep_e is not None and rep_e["status"] == "expired"
            with NodeJournal(client_cfg) as j:
                inb_e = j.get_inbox_row(eid)
                assert inb_e is not None
                assert inb_e["state"] == "expired"
                assert inb_e["result_status"] == "expired"

        # 3) All 7 events (including 2h-old `aged_event_id`) delivered to `inbox.jsonl`
        local_inbox_ids = {it["id"] for it in bus_client.read_inbox(client_cfg, include_acked=True)}
        for ev_id in [*event_ids, aged_event_id]:
            assert ev_id in local_inbox_ids, f"Missing event {ev_id} in inbox.jsonl"

        # 4) Zero `dead` deliveries on Hub A
        dels = hub_a.inspect_db(consumer="cat-food/main")["deliveries"]
        dead_dels = [d for d in dels if d["state"] == "dead"]
        assert dead_dels == [], f"Unexpected dead deliveries: {dead_dels}"
    finally:
        gate_slow.release()
        await collector.close()


def _subproc_outbox_writer_entry(cfg_dir_str: str, start_idx: int, count: int) -> int:
    """Helper entry executed inside concurrent processes/threads to hammer `journal.db`."""
    cdir = pathlib.Path(cfg_dir_str)
    with NodeJournal(cdir) as j:
        for idx in range(start_idx, start_idx + count):
            mid = f"x12-msg-{idx:04d}"
            j.enqueue_outbox(
                {
                    "v": 2,
                    "op": "send",
                    "id": mid,
                    "corr_id": mid,
                    "kind": "task",
                    "to": "cat-food/rx",
                    "text": make_ask_prompt(mid, f"payload-{idx}"),
                    "hop": 0,
                }
            )
    return count


@pytest.mark.chaos
async def test_x12_concurrent_outbox_600_backlog_rate_hook_migration_and_cli(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    hub_factory: HubController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """X12: Default `RATE_PER_MIN=20` enforcement + rate burst rejection never mistaken for ACK +
    `oneshot` failure/retry identical ID reuse + legacy `outbox.jsonl` -> `.migrated` migration +
    multi-process/thread concurrent `outbox` writes (`600` backlog > `500` replay limit) +
    `abus journal status` / `abus journal drain` CLI verification."""
    # ----------------------------------------------------------------------
    # Part 1: Unset `BUS_TEST_RATE_PER_MIN` preserves default `20/min`,
    #         rate rejection is NOT mistaken for ACK, and `oneshot` reuses ID!
    # ----------------------------------------------------------------------
    monkeypatch.delenv("BUS_TEST_RATE_PER_MIN", raising=False)
    hub_default = hub_factory.start_hub("hub_default", port=HUB_A_PORT, local_node="cat-food")

    rate_cfg = tmp_path / "rate_cfg"
    rate_cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BUS_CONFIG_DIR", str(rate_cfg))

    # Verify `oneshot` failure when hub is unreachable keeps exact 32-char UUID (no new UUID on retry)
    dead_url = "ws://127.0.0.1:18799"
    rc_off = await bus_client.oneshot(
        argparse.Namespace(
            cmd="send",
            to="cat-food/rx",
            text="offline-test",
            wait=0,
            hop=0,
            wait_first=False,
            update=False,
            say=False,
            kind="task",
            corr_id=None,
            ttl=86400,
            url=dead_url,
            offline_queue=True,
        )
    )
    assert rc_off == 0
    pending_1 = bus_client.load_outbox(rate_cfg)
    assert len(pending_1) == 1
    first_uuid = str(pending_1[0]["id"])
    assert len(first_uuid) == 32
    assert pending_1[0]["corr_id"] == first_uuid

    # Burst 24 more `oneshot` sends against `hub_default` (total 25 items vs default rate 20/min):
    # Note: 1st `oneshot` opportunistically flushes `first_uuid` (#1) + sends its own (#2), etc.
    burst_rcs: list[int] = []
    for i in range(24):
        rc = await bus_client.oneshot(
            argparse.Namespace(
                cmd="send",
                to="cat-food/rx",
                text=f"burst-{i}",
                wait=0,
                hop=0,
                wait_first=False,
                update=False,
                say=True,
                kind="event",
                corr_id=None,
                ttl=86400,
                url=hub_default.ws_url,
                offline_queue=False,
            )
        )
        burst_rcs.append(rc)

    hub_default_msgs = hub_default.inspect_db()["messages"]
    assert len(hub_default_msgs) == 20, (
        f"Default RATE_PER_MIN=20 violated when BUS_TEST_RATE_PER_MIN unset: stored {len(hub_default_msgs)}"
    )
    assert any(m["id"] == first_uuid for m in hub_default_msgs), "Original offline outbox UUID was not reused!"
    assert burst_rcs.count(1) >= 5, "Rate-limited sends must return non-zero when offline_queue=False"

    with NodeJournal(rate_cfg) as j_rate:
        rate_summary = j_rate.status_summary()
        # Total created = 1 (offline) + 24 (burst) = 25 -> 20 `done`, 5 `pending` (NEVER mistaken for `done`!)
        assert rate_summary["outbox"]["done"] == 20
        assert rate_summary["outbox"]["pending"] == 5
        assert rate_summary["outbox"]["expired"] == 0

    hub_default.stop()

    # ----------------------------------------------------------------------
    # Part 2: Legacy `outbox.jsonl` -> `outbox.jsonl.migrated` atomic migration
    #         + `save_cursor` monotonic non-decreasing guarantee
    # ----------------------------------------------------------------------
    mig_cfg = tmp_path / "mig_cfg"
    mig_cfg.mkdir(parents=True, exist_ok=True)

    # Verify legacy `cursor-<label>` never decreases
    bus_client.save_cursor("main", 42, mig_cfg)
    bus_client.save_cursor("main", 7, mig_cfg)
    assert bus_client.load_cursor("main", mig_cfg) == 42

    # Pre-seed 1 item in journal.db, then write 3 items (1 duplicate + 2 new) in `outbox.jsonl`
    with NodeJournal(mig_cfg) as j_pre:
        j_pre.enqueue_outbox({"v": 2, "op": "send", "id": "leg-dup-01", "to": "cat-food/rx", "text": "pre"})

    legacy_file = mig_cfg / "outbox.jsonl"
    legacy_lines = [
        json.dumps({"v": 2, "op": "send", "id": "leg-dup-01", "to": "cat-food/rx", "text": "dup"}),
        json.dumps({"v": 2, "op": "send", "id": "leg-new-02", "to": "cat-food/rx", "text": "two"}),
        json.dumps({"v": 2, "op": "send", "id": "leg-new-03", "to": "cat-food/rx", "text": "three"}),
    ]
    legacy_file.write_text("\n".join(legacy_lines) + "\n", encoding="utf-8")

    cli_env = os.environ.copy()
    cli_env["PYTHONPATH"] = str(BUS_ROOT)
    cli_env["BUS_CONFIG_DIR"] = str(mig_cfg)
    st_proc = subprocess.run(
        [sys.executable, "-m", "agent_bus.cli", "journal", "status", "--json"],
        env=cli_env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert st_proc.returncode == 0, st_proc.stderr
    assert not legacy_file.exists(), "outbox.jsonl must be renamed on open"
    assert (mig_cfg / "outbox.jsonl.migrated").is_file(), "outbox.jsonl.migrated must exist"
    mig_status = json.loads(st_proc.stdout)
    assert mig_status["outbox"] == {"pending": 3, "done": 0, "expired": 0}

    # ----------------------------------------------------------------------
    # Part 3: Multi-process + multi-thread concurrent `outbox` append (`600` items)
    #         + `abus journal drain` + `>500` receiver backlog full delivery
    # ----------------------------------------------------------------------
    hub_fast = hub_factory.start_hub(
        "hub_fast",
        port=HUB_B_PORT,
        local_node="cat-food",
        extra_env={"BUS_TEST_RATE_PER_MIN": "5000"},
    )
    tx_cfg = tmp_path / "tx_600_cfg"
    rx_cfg = tmp_path / "rx_600_cfg"
    tx_cfg.mkdir(parents=True, exist_ok=True)
    rx_cfg.mkdir(parents=True, exist_ok=True)

    # Launch 6 OS subprocess writers (6 * 75 = 450) + 6 thread writers (6 * 25 = 150) = 600 items simultaneously!
    writer_script = (
        "import pathlib, sys; "
        f"sys.path.insert(0, {str(BUS_ROOT)!r}); "
        "from tests.chaos.test_i1_journal_resilience import _subproc_outbox_writer_entry; "
        "_subproc_outbox_writer_entry(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))"
    )
    writer_procs: list[subprocess.Popen[str]] = []
    try:
        for p_idx in range(6):
            p = subprocess.Popen(
                [sys.executable, "-c", writer_script, str(tx_cfg), str(p_idx * 75), "75"],
                env=cli_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            writer_procs.append(p)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            thread_futs = [
                pool.submit(_subproc_outbox_writer_entry, str(tx_cfg), 450 + t_idx * 25, 25)
                for t_idx in range(6)
            ]
            for f in thread_futs:
                assert f.result(timeout=20.0) == 25

        for p in writer_procs:
            out_s, err_s = p.communicate(timeout=20.0)
            assert p.returncode == 0, f"Concurrent outbox subprocess failed: {err_s} {out_s}"
    finally:
        for p in writer_procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=3.0)

    tx_env = dict(cli_env, BUS_CONFIG_DIR=str(tx_cfg))
    pre_drain_status = json.loads(
        subprocess.run(
            [sys.executable, "-m", "agent_bus.cli", "journal", "status", "--json"],
            env=tx_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    )
    assert pre_drain_status["outbox"] == {"pending": 600, "done": 0, "expired": 0}

    # Drain all 600 pending items via `abus journal drain` CLI
    drain_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_bus.cli",
            "journal",
            "drain",
            "--url",
            hub_fast.ws_url,
            "--timeout",
            "30",
            "--json",
        ],
        env=tx_env,
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert drain_proc.returncode == 0, f"drain failed: {drain_proc.stderr} {drain_proc.stdout}"
    drain_res = json.loads(drain_proc.stdout)
    assert drain_res == {"ok": True, "drained": 600, "expired": 0, "remaining": 0}

    post_drain_status = json.loads(
        subprocess.run(
            [sys.executable, "-m", "agent_bus.cli", "journal", "status", "--json"],
            env=tx_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    )
    assert post_drain_status["outbox"] == {"pending": 0, "done": 600, "expired": 0}
    assert len(hub_fast.inspect_db()["messages"]) == 600

    # Now start offline receiver `cat-food/rx` AFTER all 600 items are queued on Hub (>500 replay limit!)
    oracle.counter.reset()
    monkeypatch.setenv("BUS_CONFIG_DIR", str(rx_cfg))
    rx_task = node_runner.start(
        url=hub_fast.ws_url,
        label="rx",
        mode="hybrid",
        peers="cat-food",
    )

    await _wait_until(lambda: len(oracle.counter.all_ids()) >= 600, timeout=25.0, interval=0.05)
    await node_runner.stop_task(rx_task)

    executed_ids = oracle.counter.all_ids()
    expected_ids = {f"x12-msg-{i:04d}" for i in range(600)}
    assert len(executed_ids) == 600, f"Expected 600 executions, got {len(executed_ids)}"
    assert set(executed_ids) == expected_ids, f"Missing IDs: {expected_ids - set(executed_ids)}"

    rx_env = dict(cli_env, BUS_CONFIG_DIR=str(rx_cfg))
    rx_status_json = json.loads(
        subprocess.run(
            [sys.executable, "-m", "agent_bus.cli", "journal", "status", "--json"],
            env=rx_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    )
    assert rx_status_json["inbox"]["done"] == 600
    assert rx_status_json["inbox"]["queued"] == 0
    assert rx_status_json["inbox"]["running"] == 0

    # Also verify human-readable `abus journal status` output
    rx_status_txt = subprocess.run(
        [sys.executable, "-m", "agent_bus.cli", "journal", "status"],
        env=rx_env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout
    assert "inbox: queued=0" in rx_status_txt
    assert "done=600" in rx_status_txt
    assert "uncertain (0): none" in rx_status_txt
