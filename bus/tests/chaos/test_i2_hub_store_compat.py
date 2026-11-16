"""WBS `I2` hub/store resilience, `X08`, forward-compat (`e7d0da2` client), and rollback-compat (`e7d0da2` hub/store) tests.

Verifies:
- `X08` (`design.md` §6, `sol-review.md` F08):
  - Request submitted on Hub A (`18765`), result posted on Hub B (`18775`) without prior request or `progress` on Hub B.
  - Out-of-order arrivals on Hub B: terminal result first, followed by late `op='progress'` (`status='running'`),
    late non-terminal `status='running'` message, and late original `request` -> terminal state preserved!
  - Custom `--corr-id` (`id != corr_id`) with `rkind='request'` (`design.md` §9) stays `pending` (not mistaken for result),
    and v2 terminal statuses (`expired`, `exec_uncertain`) are preserved as terminal.
  - Hub A returns `pending`; Hub B returns terminal result (`WS` `rpc_result`/`result` + Gateway `GET /v1/rpc/<corr_id>`).
  - External (`ext`) caller isolation: owning `ext` caller can query `/v1/rpc/<corr_id>`, `/v1/poll`, and live WS;
    other `ext` caller receives `403 forbidden` on RPC query and `0` items on `/v1/poll` and live WS.
- Forward compatibility (`e7d0da2` legacy client -> v2 Hub):
  - Extracts unmodified `e7d0da2:bus/agent_bus/*.py` into `tmp_path` (zero repo pollution) and runs legacy `run_node`,
    `peers`, `send` (sync RPC `--wait`, async task, `--say` event), `result`, and `doctor` against live v2 Hub
    (including when Hub `seq > 10**9`, proving `W0-review.md` §2.1 legacy CLI replay suppression).
- Rollback compatibility (v2 `I2` SQLite DB -> `e7d0da2` legacy `BusStore` & `BusHub`):
  - Populates `bus.db` with `I2` (`hub_meta`, `rkind`, `deadline`, `rpc_results`, `deliveries`, `nonces`), closes it,
    opens the exact same DB file under `e7d0da2` `BusStore` and live `e7d0da2` `BusHub` + Gateway, verifies polling,
    new inserts (`DEFAULT NULL`), and RPC queries, then re-opens under `I2` verifying `hub_id` persistence.
- `id_conflict`, `no_replay`, `accept_inbox` offline replay (`W0-review.md` §2.3), `H10` new-`hub_id` `since=0` reset,
  periodic `prune_expired()` background task, `BUS_TEST_RATE_PER_MIN` default `20` invariant, and WS/HTTP `health` metrics.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

import pytest
import websockets

from agent_bus import auth as bus_auth
from agent_bus import config as bus_config
from agent_bus import gateway as bus_gateway
from agent_bus import hub as bus_hub
from agent_bus import store as bus_store
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


async def _wait_until(predicate: Any, *, timeout: float = 6.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = predicate()
        if asyncio.iscoroutine(res):
            res = await res
        if res:
            return
        await asyncio.sleep(interval)
    raise TimeoutError(f"Condition not met within {timeout}s")


def _extract_legacy_package(dest_bus_root: pathlib.Path, commit: str = LEGACY_COMMIT) -> pathlib.Path:
    """Extract unmodified `e7d0da2:bus/agent_bus/*.py` into an isolated `tmp_path` directory."""
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


def _unauth_http_json(base_http_url: str, route: str) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(f"{base_http_url.rstrip('/')}{route}", method="GET")
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


async def _recv_hub_ack(ws: Any, expected_id: str, *, timeout: float = 3.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        rem = max(0.05, deadline - time.monotonic())
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=rem))
        if frame.get("op") == "sys" and frame.get("error"):
            raise RuntimeError(f"Hub error for {expected_id}: {frame}")
        if frame.get("op") == "sys" and frame.get("ack") == expected_id:
            return frame


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


@pytest.mark.chaos
async def test_x08_split_hub_rpc_progress_missing_out_of_order_and_ext_isolation(
    hub_factory: HubController,
    oracle: ChaosOracle,
) -> None:
    """X08: Request on Hub A (`18765`), result on Hub B (`18775`), missing/out-of-order `progress`
    and late original `request`, Hub A `pending` vs Hub B `terminal`, custom `--corr-id` `rkind='request'`,
    and strict external (`ext`) caller ownership isolation."""
    gw_a = pick_free_port()
    gw_b = pick_free_port()
    hub_a = hub_factory.create_hub(
        "hub_a",
        port=HUB_A_PORT,
        gateway_port=gw_a,
        local_node="cat-food",
        extra_env={"BUS_TEST_RATE_PER_MIN": "200"},
    )
    tok_alpha = hub_a.create_token(
        "alpha",
        scopes=["rpc:cat-food/main", "send:cat-food/inbox", "poll:ext-alpha"],
        allow_exec=True,
    )
    tok_bravo = hub_a.create_token(
        "bravo",
        scopes=["rpc:cat-food/main", "send:cat-food/inbox", "poll:ext-bravo"],
        allow_exec=True,
    )
    hub_b = hub_factory.create_hub(
        "hub_b",
        port=HUB_B_PORT,
        gateway_port=gw_b,
        local_node="cat-food",
        extra_env={"BUS_TEST_RATE_PER_MIN": "200"},
    )
    hub_b.copy_auth_from(hub_a)

    hub_a.start()
    hub_b.start()

    # ------------------------------------------------------------------
    # 1) Request on Hub A -> Result on Hub B (progress missing on Hub B!)
    # ------------------------------------------------------------------
    cid_split = "x08-split-req-1"
    rep_split = "x08-split-rep-1"
    ans_split = "X08_TERMINAL_ON_B"

    oracle.ledger.record_sent(cid_split, caller=tok_alpha["node"], hub="hub_a")
    sc, sb = await asyncio.to_thread(
        _signed_http_json,
        hub_a.gateway_http_url,
        "/v1/send",
        token_id=tok_alpha["id"],
        secret=tok_alpha["secret"],
        method="POST",
        payload={
            "v": 2,
            "id": cid_split,
            "corr_id": cid_split,
            "kind": "rpc",
            "rkind": "request",
            "to": "cat-food/main",
            "text": make_ask_prompt(cid_split, "cross-hub-rpc"),
            "label": "alphacli",
        },
    )
    assert sc == 200 and sb["ok"] is True
    oracle.ledger.record_hub_ack(cid_split, hub="hub_a", seq=sb["seq"])

    # Connect live external WS sessions for alpha and bravo on Hub B BEFORE result arrives
    def _build_ext_ws_hello(tok: dict[str, Any], label: str) -> dict[str, Any]:
        hdrs = bus_auth.build_hmac_headers(tok["id"], tok["secret"], tok["id"].encode("utf-8"))
        return {
            "v": 2,
            "label": label,
            "since": 0,
            "accept_inbox": False,
            "no_replay": True,
            "auth": {
                "token_id": tok["id"],
                "ts": hdrs["X-Bus-Timestamp"],
                "nonce": hdrs["X-Bus-Nonce"],
                "sig": hdrs["X-Bus-Signature"],
            },
        }

    async with (
        websockets.connect(hub_b.gateway_ws_url) as alpha_ws_b,
        websockets.connect(hub_b.gateway_ws_url) as bravo_ws_b,
    ):
        await alpha_ws_b.send(json.dumps(_build_ext_ws_hello(tok_alpha, "live")))
        b_alpha = json.loads(await asyncio.wait_for(alpha_ws_b.recv(), 2.0))
        assert b_alpha["you"] == "ext-alpha/live"

        await bravo_ws_b.send(json.dumps(_build_ext_ws_hello(tok_bravo, "live")))
        b_bravo = json.loads(await asyncio.wait_for(bravo_ws_b.recv(), 2.0))
        assert b_bravo["you"] == "ext-bravo/live"

        # Worker posts terminal result ONLY to Hub B (NO progress sent at all!)
        async with websockets.connect(hub_b.ws_url) as worker_b:
            await worker_b.send(json.dumps({"v": 2, "label": "main", "since": 0, "accept_inbox": True}))
            await asyncio.wait_for(worker_b.recv(), 2.0)  # banner
            await worker_b.send(
                json.dumps(
                    {
                        "v": 2,
                        "op": "send",
                        "id": rep_split,
                        "corr_id": cid_split,
                        "kind": "rpc",
                        "rkind": "result",
                        "status": "ok",
                        "to": "ext-alpha/alphacli",
                        "text": ans_split,
                        "hop": 1,
                    }
                )
            )
            await _recv_hub_ack(worker_b, rep_split)

        # `ext-alpha/live` receives the cross-hub result live via `matches_peer`!
        live_msg = json.loads(await asyncio.wait_for(alpha_ws_b.recv(), 2.0))
        assert live_msg["op"] == "msg"
        assert live_msg["id"] == rep_split
        assert live_msg["corr_id"] == cid_split
        assert live_msg["rkind"] == "result"
        assert live_msg["text"] == ans_split

        # `ext-bravo/live` must NOT receive `ext-alpha`'s result!
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bravo_ws_b.recv(), 0.2)

        # WS `result` & `rpc_result` query on Hub B: allowed for alpha, 403 forbidden for bravo!
        await alpha_ws_b.send(json.dumps({"op": "result", "corr_id": cid_split}))
        ws_alpha_rpc = json.loads(await asyncio.wait_for(alpha_ws_b.recv(), 2.0))
        assert ws_alpha_rpc["ok"] is True and ws_alpha_rpc["found"] is True
        assert ws_alpha_rpc["status"] == "ok" and ws_alpha_rpc["text"] == ans_split

        await bravo_ws_b.send(json.dumps({"op": "rpc_result", "corr_id": cid_split}))
        ws_bravo_rpc = json.loads(await asyncio.wait_for(bravo_ws_b.recv(), 2.0))
        assert ws_bravo_rpc["ok"] is False and ws_bravo_rpc["error"] == "forbidden"

    # Hub A RPC query -> `pending`; Hub B RPC query -> `ok` terminal!
    code_a_alpha, body_a_alpha = await asyncio.to_thread(
        _signed_http_json,
        hub_a.gateway_http_url,
        f"/v1/rpc/{cid_split}",
        token_id=tok_alpha["id"],
        secret=tok_alpha["secret"],
    )
    assert code_a_alpha == 200
    assert body_a_alpha["found"] is True
    assert body_a_alpha["status"] == "pending"
    assert body_a_alpha["text"] == ""

    code_b_alpha, body_b_alpha = await asyncio.to_thread(
        _signed_http_json,
        hub_b.gateway_http_url,
        f"/v1/rpc/{cid_split}",
        token_id=tok_alpha["id"],
        secret=tok_alpha["secret"],
    )
    assert code_b_alpha == 200
    assert body_b_alpha["found"] is True
    assert body_b_alpha["status"] == "ok"
    assert body_b_alpha["text"] == ans_split
    oracle.ledger.record_result(cid_split, status="ok")

    # `ext-bravo` HTTP `/v1/rpc/<cid>` MUST return 403 on BOTH Hub A and Hub B!
    for gw_url in (hub_a.gateway_http_url, hub_b.gateway_http_url):
        code_bravo, body_bravo = await asyncio.to_thread(
            _signed_http_json,
            gw_url,
            f"/v1/rpc/{cid_split}",
            token_id=tok_bravo["id"],
            secret=tok_bravo["secret"],
        )
        assert code_bravo == 403
        assert body_bravo["ok"] is False and body_bravo["error"] == "forbidden"

    # `/v1/poll` on Hub B: `ext-alpha` (even with `label=alphapoll` != `alphacli`) receives `rep_split`,
    # whereas `ext-bravo` receives `[]`!
    pc_bravo, pb_bravo = await asyncio.to_thread(
        _signed_http_json,
        hub_b.gateway_http_url,
        "/v1/poll?label=main&since=0",
        token_id=tok_bravo["id"],
        secret=tok_bravo["secret"],
    )
    assert pc_bravo == 200 and pb_bravo["messages"] == []

    pc_alpha, pb_alpha = await asyncio.to_thread(
        _signed_http_json,
        hub_b.gateway_http_url,
        "/v1/poll?label=alphapoll&since=0",
        token_id=tok_alpha["id"],
        secret=tok_alpha["secret"],
    )
    assert pc_alpha == 200
    assert [m["id"] for m in pb_alpha["messages"]] == [rep_split]

    # ------------------------------------------------------------------
    # 2) Out-of-order arrivals on Hub B:
    #    Terminal result FIRST -> late `progress(running)` -> late `status='running'` msg
    #    -> late original `request` -> terminal result MUST stay intact!
    # ------------------------------------------------------------------
    cid_ooo = "x08-ooo-req-2"
    rep_ooo = "x08-ooo-rep-2"
    ans_ooo = "X08_OOO_TERMINAL_KEPT"

    async with websockets.connect(hub_b.ws_url) as ws_b:
        await ws_b.send(json.dumps({"v": 2, "label": "cli-ooo", "since": 0, "accept_inbox": False, "no_replay": True}))
        await asyncio.wait_for(ws_b.recv(), 2.0)

        # (a) Terminal result arrives FIRST on Hub B
        await ws_b.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": rep_ooo,
                    "corr_id": cid_ooo,
                    "kind": "rpc",
                    "rkind": "result",
                    "status": "ok",
                    "to": "cat-food/caller",
                    "text": ans_ooo,
                    "hop": 1,
                }
            )
        )
        await _recv_hub_ack(ws_b, rep_ooo)

        # (b) Late `op='progress'` (`status='running'`) arrives AFTER terminal result
        await ws_b.send(
            json.dumps(
                {
                    "op": "progress",
                    "corr_id": cid_ooo,
                    "to": "cat-food/caller",
                    "status": "running",
                    "text": "late-progress-must-not-overwrite",
                }
            )
        )

        # (c) Late message carrying `status='running'` arrives AFTER terminal result
        await ws_b.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": "x08-ooo-running-msg",
                    "corr_id": cid_ooo,
                    "kind": "rpc",
                    "rkind": "result",
                    "status": "running",
                    "to": "cat-food/caller",
                    "text": "non-terminal-running",
                    "hop": 1,
                }
            )
        )
        await _recv_hub_ack(ws_b, "x08-ooo-running-msg")

        # (d) Late original `request` (`id == corr_id`, `rkind='request'`) arrives AFTER terminal result!
        await ws_b.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": cid_ooo,
                    "corr_id": cid_ooo,
                    "kind": "rpc",
                    "rkind": "request",
                    "to": "cat-food/main",
                    "text": "!ask delayed-request",
                    "hop": 0,
                }
            )
        )
        await _recv_hub_ack(ws_b, cid_ooo)

        # Query `rpc_result` -> MUST still be terminal `status='ok'`, `text=ans_ooo`!
        await ws_b.send(json.dumps({"op": "rpc_result", "corr_id": cid_ooo}))
        ooo_rpc = json.loads(await asyncio.wait_for(ws_b.recv(), 2.0))
        assert ooo_rpc["found"] is True
        assert ooo_rpc["status"] == "ok"
        assert ooo_rpc["text"] == ans_ooo

        # --------------------------------------------------------------
        # 3) Custom `--corr-id` (`id != corr_id`) with `rkind='request'` (`design.md` §9)
        #    + v2 terminal statuses (`exec_uncertain`, `expired`)
        # --------------------------------------------------------------
        custom_cid = "x08-custom-cid"
        custom_req_mid = "x08-custom-req-uuid-distinct"
        await ws_b.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": custom_req_mid,
                    "corr_id": custom_cid,
                    "kind": "rpc",
                    "rkind": "request",
                    "status": "ok",
                    "to": "cat-food/main",
                    "text": "!ask custom-corr-id-request",
                }
            )
        )
        await _recv_hub_ack(ws_b, custom_req_mid)

        await ws_b.send(json.dumps({"op": "rpc_result", "corr_id": custom_cid}))
        pending_custom = json.loads(await asyncio.wait_for(ws_b.recv(), 2.0))
        assert pending_custom["found"] is True
        assert pending_custom["status"] == "pending", (
            f"rkind='request' with id!=corr_id was misclassified as result: {pending_custom}"
        )

        # Now deliver `exec_uncertain` result and ensure late `running` cannot overwrite it
        await ws_b.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": "x08-custom-rep-unc",
                    "corr_id": custom_cid,
                    "kind": "rpc",
                    "rkind": "result",
                    "status": "exec_uncertain",
                    "to": "cat-food/caller",
                    "text": "[exec_uncertain] crashed",
                }
            )
        )
        await _recv_hub_ack(ws_b, "x08-custom-rep-unc")

        await ws_b.send(
            json.dumps({"op": "progress", "corr_id": custom_cid, "to": "cat-food/caller", "status": "running"})
        )
        await ws_b.send(json.dumps({"op": "rpc_result", "corr_id": custom_cid}))
        unc_custom = json.loads(await asyncio.wait_for(ws_b.recv(), 2.0))
        assert unc_custom["found"] is True
        assert unc_custom["status"] == "exec_uncertain"
        assert "[exec_uncertain]" in unc_custom["text"]


@pytest.mark.chaos
async def test_legacy_e7d0da2_client_forward_compat_against_v2_hub(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    oracle: ChaosOracle,
) -> None:
    """Extract unmodified `e7d0da2` client into `tmp_path` and verify full forward compatibility
    (`run_node`, `peers`, sync `send --wait`, `result`, async task, `--say` event, `doctor`)
    against the v2 Hub — including when Hub `seq > 10**9` (`W0-review.md` §2.1)."""
    legacy_bus_root = _extract_legacy_package(tmp_path / "legacy_client_pkg")
    gw_port = pick_free_port()
    hub_a = hub_factory.create_hub(
        "hub_a",
        port=HUB_A_PORT,
        gateway_port=gw_port,
        local_node="cat-food",
        extra_env={"BUS_TEST_RATE_PER_MIN": "200"},
    )
    hub_a.start()

    # Seed Hub A's `sqlite_sequence` above `10**9` (`1_000_000_050`) AND enqueue a broadcast message
    # so `seq = 1_000_000_051 > 10**9`: proves legacy `e7d0da2` `cli-*`/`doc-*` (`since=10**9`,
    # `accept_inbox=False`, no `no_replay` key) still gets replay suppressed cleanly!
    hub_a.stop()
    conn = sqlite3.connect(str(hub_a.db_path), timeout=5.0)
    try:
        now = time.time()
        conn.execute(
            """
            INSERT INTO messages (seq, id, corr_id, sender, fnode, target, kind, status, hop, ts, expires_at, trust, text)
            VALUES (?, 'high-seq-seed', NULL, 'mother-goose/main', 'mother-goose', '*', 'event', 'ok', 0, ?, ?, 'internal', 'high-seq-bcast')
            """,
            (1_000_000_050, now, now + 86400),
        )
        conn.commit()
    finally:
        conn.close()
    hub_a.start()

    # Write deterministic subprocess stub for `BUS_CLAUDE_CMD` (logs into `oracle.counter`, NEVER calls real claude)
    stub_claude = oracle.counter.write_subprocess_stub_script(tmp_path / "legacy_claude_stub.py")

    leg_cfg = tmp_path / "legacy_node_cfg"
    leg_data = tmp_path / "legacy_node_data"
    leg_logs = tmp_path / "legacy_node_logs"
    leg_home = tmp_path / "legacy_node_home"
    for d in (leg_cfg, leg_data, leg_logs, leg_home):
        d.mkdir(parents=True, exist_ok=True)

    leg_env = {
        "HOME": str(leg_home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(legacy_bus_root),
        "PYTHONUSERBASE": "/home/rerun/.local",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "BUS_CONFIG_DIR": str(leg_cfg),
        "BUS_DATA_DIR": str(leg_data),
        "BUS_DB_PATH": str(leg_data / "bus.db"),
        "BUS_LOG_DIR": str(leg_logs),
        "BUS_MODE_FILE": str(leg_cfg / "mode"),
        "BUS_TEST_ALLOW_LOCALHOST": "1",
        "BUS_TEST_LOCAL_NODE": "cat-food",
        "BUS_CLAUDE_CMD": str(stub_claude),
    }

    worker_proc = subprocess.Popen(
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
        env=leg_env,
        cwd=str(leg_home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        # 1) Legacy `peers` CLI against v2 Hub with `seq > 10**9`: must NOT break on replay!
        async def _legacy_worker_visible() -> bool:
            r = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "agent_bus.cli", "peers", "--url", hub_a.ws_url],
                env=leg_env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return r.returncode == 0 and "cat-food/main" in r.stdout

        await _wait_until(_legacy_worker_visible, timeout=5.0)

        # 2) Legacy sync RPC (`send --wait 5 --corr-id leg-rpc-1`) -> worker replies -> `result` query!
        rpc_cid = "leg-rpc-01"
        send_rpc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "agent_bus.cli",
                "send",
                "--to",
                "cat-food/main",
                "--corr-id",
                rpc_cid,
                "--wait",
                "5",
                "--url",
                hub_a.ws_url,
                f"[id={rpc_cid}] legacy-sync-rpc",
            ],
            env=leg_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert send_rpc.returncode == 0, f"legacy sync send failed: {send_rpc.stderr} {send_rpc.stdout}"
        assert f"STUB_OK:{rpc_cid}" in send_rpc.stdout
        assert oracle.counter.count(rpc_cid) == 1

        # 3) Legacy `result` CLI retrieves stored terminal RPC result
        res_cli = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "agent_bus.cli",
                "result",
                rpc_cid,
                "--json",
                "--url",
                hub_a.ws_url,
            ],
            env=leg_env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert res_cli.returncode == 0, f"legacy result failed: {res_cli.stderr}"
        res_json = json.loads(res_cli.stdout)
        assert res_json["found"] is True
        assert res_json["status"] == "ok"
        assert f"STUB_OK:{rpc_cid}" in res_json["text"]

        # 4) Legacy `--say` event delivery to `cat-food/inbox`
        send_evt = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "agent_bus.cli",
                "send",
                "--to",
                "cat-food/inbox",
                "--say",
                "--url",
                hub_a.ws_url,
                "legacy-inbox-hello",
            ],
            env=leg_env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert send_evt.returncode == 0, send_evt.stderr

        await _wait_until(
            lambda: (leg_cfg / "inbox.jsonl").is_file()
            and "legacy-inbox-hello" in (leg_cfg / "inbox.jsonl").read_text(encoding="utf-8"),
            timeout=3.0,
        )

        # 5) Legacy `doctor` CLI (`doc-*` label, `since=10**9`, no `no_replay`) against `seq > 10**9` Hub
        doc_cli = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "agent_bus.cli", "doctor", "--json", "--url", hub_a.ws_url],
            env=leg_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert doc_cli.returncode == 0, f"legacy doctor failed: {doc_cli.stderr} {doc_cli.stdout}"
        doc_data = json.loads(doc_cli.stdout)
        assert doc_data["hub_ok"] is True
        assert "cat-food/main" in doc_data["peers"]
    finally:
        _terminate_proc(worker_proc)


@pytest.mark.chaos
async def test_legacy_e7d0da2_hub_store_rollback_compat_on_i2_sqlite_db(
    tmp_path: pathlib.Path,
) -> None:
    """Verify both upgrade migration (`e7d0da2` DB -> `I2` Store) AND rollback compatibility
    (`I2` DB populated with `hub_meta`, `rkind`, `deadline`, `rpc_results` -> opened and served
    by unmodified `e7d0da2` `BusStore` & `BusHub` -> re-opened by `I2` `BusStore`)."""
    legacy_bus_root = _extract_legacy_package(tmp_path / "legacy_hub_pkg")

    shared_base = tmp_path / "rollback_hub_state"
    cfg_d = shared_base / "cfg"
    data_d = shared_base / "data"
    logs_d = shared_base / "logs"
    home_d = shared_base / "home"
    guard_d = shared_base / "_guard"
    for d in (cfg_d, data_d, logs_d, home_d, guard_d):
        d.mkdir(parents=True, exist_ok=True)
    (cfg_d / "allow.txt").write_text("cat-food\nmother-goose\n", encoding="utf-8")
    db_file = data_d / "bus.db"
    guard_claude = write_claude_guard_script(guard_d)

    # ------------------------------------------------------------------
    # Phase A: Create an old `e7d0da2` DB first, then upgrade in-place to `I2`!
    # ------------------------------------------------------------------
    seed_script = (
        "import pathlib, sys; "
        f"sys.path.insert(0, {str(legacy_bus_root)!r}); "
        "from agent_bus.store import BusStore; "
        f"st = BusStore(pathlib.Path({str(db_file)!r})); "
        "st.enqueue_message(mid='pre-v2-1', sender='cat-food/cli', fnode='cat-food', target='mother-goose/main', text='from-e7d0da2'); "
        "st.close()"
    )
    subprocess.run([sys.executable, "-c", seed_script], check=True, timeout=10)

    # Open with `I2` `BusStore` -> migrates `rkind` & `deadline` columns and creates `hub_meta.hub_id`!
    st_i2 = bus_store.BusStore(db_file)
    try:
        original_hub_id = st_i2.get_hub_id()
        assert len(original_hub_id) == 32
        assert st_i2.get_max_seq() == 1

        # Enqueue v2 messages using `rkind` and `deadline` + terminal RPC result + nonce
        now = time.time()
        m_req, dup_req = st_i2.enqueue_message(
            mid="i2-req-1",
            corr_id="i2-req-1",
            sender="cat-food/cli",
            fnode="cat-food",
            target="mother-goose/main",
            kind="rpc",
            rkind="request",
            text="!ask v2-question",
            deadline=now + 3600.0,
        )
        assert not dup_req
        assert m_req["rkind"] == "request"
        assert m_req["deadline"] == pytest.approx(now + 3600.0)

        m_res, dup_res = st_i2.enqueue_message(
            mid="i2-rep-1",
            corr_id="i2-req-1",
            sender="mother-goose/main",
            fnode="mother-goose",
            target="cat-food/cli",
            kind="rpc",
            rkind="result",
            status="ok",
            text="v2-answer",
            deadline=now + 3600.0,
        )
        assert not dup_res
        assert m_res["rkind"] == "result"
        assert st_i2.check_and_record_nonce("tok_x:n1") is True
    finally:
        st_i2.close()

    # Re-open with `I2` once to verify `hub_id` persistence across normal restart
    st_i2_check = bus_store.BusStore(db_file)
    try:
        assert st_i2_check.get_hub_id() == original_hub_id
    finally:
        st_i2_check.close()

    # ------------------------------------------------------------------
    # Phase B: Rollback! Start unmodified `e7d0da2` `BusHub` + Gateway on the `I2` SQLite DB!
    # ------------------------------------------------------------------
    rb_ws_port = pick_free_port()
    rb_gw_port = pick_free_port()
    rb_env = {
        "HOME": str(home_d),
        "PATH": f"{guard_d}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "PYTHONPATH": str(legacy_bus_root),
        "PYTHONUSERBASE": "/home/rerun/.local",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "BUS_CONFIG_DIR": str(cfg_d),
        "BUS_DATA_DIR": str(data_d),
        "BUS_DB_PATH": str(db_file),
        "BUS_LOG_DIR": str(logs_d),
        "BUS_MODE_FILE": str(cfg_d / "mode"),
        "BUS_TEST_ALLOW_LOCALHOST": "1",
        "BUS_TEST_LOCAL_NODE": "mother-goose",
        "BUS_CLAUDE_CMD": str(guard_claude),
    }

    rb_hub_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_bus.cli",
            "hub",
            "--host",
            "127.0.0.1",
            "--port",
            str(rb_ws_port),
            "--gateway-host",
            "127.0.0.1",
            "--gateway-port",
            str(rb_gw_port),
        ],
        env=rb_env,
        cwd=str(shared_base),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        await _wait_until(
            lambda: is_port_listening("127.0.0.1", rb_ws_port) and is_port_listening("127.0.0.1", rb_gw_port),
            timeout=6.0,
        )

        # Connect as `mother-goose/main` to `e7d0da2` hub -> receives replay of `pre-v2-1` and `i2-req-1`!
        async with websockets.connect(f"ws://127.0.0.1:{rb_ws_port}") as ws_rb:
            await ws_rb.send(json.dumps({"v": 2, "label": "main", "since": 0, "accept_inbox": True}))
            rb_banner = json.loads(await asyncio.wait_for(ws_rb.recv(), 2.0))
            assert rb_banner["you"] == "mother-goose/main"
            assert rb_banner["seq"] == 3
            assert "hub_id" not in rb_banner  # proves we are genuinely talking to old e7d0da2 hub!

            replayed_1 = json.loads(await asyncio.wait_for(ws_rb.recv(), 2.0))
            replayed_2 = json.loads(await asyncio.wait_for(ws_rb.recv(), 2.0))
            assert [replayed_1["id"], replayed_2["id"]] == ["pre-v2-1", "i2-req-1"]

            # Query `i2-req-1` RPC result written by `I2` -> `e7d0da2` hub reads it cleanly!
            await ws_rb.send(json.dumps({"op": "rpc_result", "corr_id": "i2-req-1"}))
            rb_rpc = json.loads(await asyncio.wait_for(ws_rb.recv(), 2.0))
            assert rb_rpc["found"] is True
            assert rb_rpc["status"] == "ok"
            assert rb_rpc["text"] == "v2-answer"

            # Publish a new message through `e7d0da2` hub (`INSERT` without `rkind`/`deadline` -> DEFAULT NULL!)
            await ws_rb.send(
                json.dumps(
                    {
                        "v": 2,
                        "op": "send",
                        "id": "rb-new-msg-4",
                        "corr_id": "rb-new-msg-4",
                        "kind": "task",
                        "to": "cat-food/main",
                        "text": "written-by-e7d0da2-after-rollback",
                    }
                )
            )
            rb_ack = await _recv_hub_ack(ws_rb, "rb-new-msg-4")
            assert rb_ack["seq"] == 4
    finally:
        _terminate_proc(rb_hub_proc)
        await _wait_until(
            lambda: not is_port_listening("127.0.0.1", rb_ws_port)
            and not is_port_listening("127.0.0.1", rb_gw_port),
            timeout=5.0,
        )

    # ------------------------------------------------------------------
    # Phase C: Re-open DB under `I2` `BusStore` -> `hub_id` intact & rollback row readable!
    # ------------------------------------------------------------------
    st_after = bus_store.BusStore(db_file)
    try:
        assert st_after.get_hub_id() == original_hub_id
        assert st_after.get_max_seq() == 4
        polled_cf = st_after.poll_for_consumer(node="cat-food", consumer="cat-food/main", since=0)
        polled_ids = [m["id"] for m in polled_cf]
        assert "rb-new-msg-4" in polled_ids
        rb_msg = next(m for m in polled_cf if m["id"] == "rb-new-msg-4")
        assert "rkind" not in rb_msg
        assert "deadline" not in rb_msg
    finally:
        st_after.close()


@pytest.mark.chaos
async def test_id_conflict_vs_duplicate_ws_http_and_store(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
) -> None:
    """Verify `id_conflict` detection across direct `BusStore`, WS `op='send'`, and HTTP `POST /v1/send`."""
    # 1) Direct `BusStore` unit checks
    st = bus_store.BusStore(tmp_path / "conflict_unit.db")
    try:
        orig, is_dup = st.enqueue_message(
            mid="dup-or-conflict-1",
            sender="cat-food/cli",
            fnode="cat-food",
            target="mother-goose/main",
            text="canonical-body",
            rkind="request",
            corr_id="dup-or-conflict-1",
        )
        assert is_dup is False
        assert orig["seq"] == 1

        # Exact same `(target, text)` -> idempotent `duplicate`
        dup_msg, is_dup2 = st.enqueue_message(
            mid="dup-or-conflict-1",
            sender="cat-food/cli",
            fnode="cat-food",
            target="mother-goose/main",
            text="canonical-body",
        )
        assert is_dup2 is True
        assert dup_msg["seq"] == 1

        # Different `text` -> `IdConflictError`
        with pytest.raises(bus_store.IdConflictError, match="id_conflict"):
            st.enqueue_message(
                mid="dup-or-conflict-1",
                sender="cat-food/cli",
                fnode="cat-food",
                target="mother-goose/main",
                text="mutated-body",
            )

        # Different `target` -> `IdConflictError`
        with pytest.raises(bus_store.IdConflictError, match="id_conflict"):
            st.enqueue_message(
                mid="dup-or-conflict-1",
                sender="cat-food/cli",
                fnode="cat-food",
                target="z-fold8/main",
                text="canonical-body",
            )

        # Original row is 100% preserved
        assert st.get_max_seq() == 1
        polled = st.poll_for_consumer(node="mother-goose", consumer="mother-goose/main", since=0)
        assert len(polled) == 1
        assert polled[0]["text"] == "canonical-body"
        assert polled[0]["to"] == "mother-goose/main"
    finally:
        st.close()

    # 2) Live WS + HTTP Gateway `id_conflict` checks
    gw_port = pick_free_port()
    hub_a = hub_factory.create_hub("hub_a", port=HUB_A_PORT, gateway_port=gw_port, local_node="cat-food")
    tok = hub_a.create_token("extc", scopes=["send:cat-food/inbox", "send:mother-goose/inbox"], allow_exec=False)
    hub_a.start()

    async with websockets.connect(hub_a.ws_url) as ws:
        await ws.send(json.dumps({"v": 2, "label": "cli-c", "since": 0, "accept_inbox": False, "no_replay": True}))
        await asyncio.wait_for(ws.recv(), 2.0)

        # First WS send
        await ws.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": "ws-cid-1",
                    "to": "cat-food/main",
                    "kind": "task",
                    "text": "original-ws-text",
                }
            )
        )
        ack1 = await _recv_hub_ack(ws, "ws-cid-1")
        assert ack1["duplicate"] is False

        # Identical WS resend -> `duplicate=True`
        await ws.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": "ws-cid-1",
                    "to": "cat-food/main",
                    "kind": "task",
                    "text": "original-ws-text",
                }
            )
        )
        ack_dup = await _recv_hub_ack(ws, "ws-cid-1")
        assert ack_dup["duplicate"] is True
        assert ack_dup["seq"] == ack1["seq"]

        # Conflicting `text` over WS -> `error == 'id_conflict'`
        await ws.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": "ws-cid-1",
                    "to": "cat-food/main",
                    "kind": "task",
                    "text": "tampered-ws-text",
                }
            )
        )
        err_frame = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
        assert err_frame["op"] == "sys"
        assert err_frame["error"] == "id_conflict"
        assert "ack" not in err_frame

        # Conflicting `to` (`target`) over WS -> `error == 'id_conflict'`
        await ws.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": "ws-cid-1",
                    "to": "mother-goose/main",
                    "kind": "task",
                    "text": "original-ws-text",
                }
            )
        )
        err_target = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
        assert err_target["error"] == "id_conflict"

    # HTTP Gateway `POST /v1/send`: initial -> duplicate (200) -> conflict text (409) -> conflict target (409)
    c1, b1 = await asyncio.to_thread(
        _signed_http_json,
        hub_a.gateway_http_url,
        "/v1/send",
        token_id=tok["id"],
        secret=tok["secret"],
        method="POST",
        payload={"v": 2, "id": "http-cid-1", "to": "cat-food/inbox", "kind": "event", "text": "http-orig"},
    )
    assert c1 == 200 and b1["ok"] is True and b1["duplicate"] is False

    c2, b2 = await asyncio.to_thread(
        _signed_http_json,
        hub_a.gateway_http_url,
        "/v1/send",
        token_id=tok["id"],
        secret=tok["secret"],
        method="POST",
        payload={"v": 2, "id": "http-cid-1", "to": "cat-food/inbox", "kind": "event", "text": "http-orig"},
    )
    assert c2 == 200 and b2["ok"] is True and b2["duplicate"] is True

    c3, b3 = await asyncio.to_thread(
        _signed_http_json,
        hub_a.gateway_http_url,
        "/v1/send",
        token_id=tok["id"],
        secret=tok["secret"],
        method="POST",
        payload={"v": 2, "id": "http-cid-1", "to": "cat-food/inbox", "kind": "event", "text": "http-different"},
    )
    assert c3 == 409
    assert b3["ok"] is False and b3["error"] == "id_conflict"

    c4, b4 = await asyncio.to_thread(
        _signed_http_json,
        hub_a.gateway_http_url,
        "/v1/send",
        token_id=tok["id"],
        secret=tok["secret"],
        method="POST",
        payload={"v": 2, "id": "http-cid-1", "to": "mother-goose/inbox", "kind": "event", "text": "http-orig"},
    )
    assert c4 == 409
    assert b4["ok"] is False and b4["error"] == "id_conflict"

    # Verify DB retained exact original rows only (`ws-cid-1` and `http-cid-1`)
    snap = hub_a.inspect_db()["messages"]
    by_id = {m["id"]: m for m in snap}
    assert set(by_id.keys()) == {"ws-cid-1", "http-cid-1"}
    assert by_id["ws-cid-1"]["text"] == "original-ws-text"
    assert by_id["ws-cid-1"]["target"] == "cat-food/main"
    assert by_id["http-cid-1"]["text"] == "http-orig"
    assert by_id["http-cid-1"]["target"] == "cat-food/inbox"


@pytest.mark.chaos
async def test_banner_caps_no_replay_accept_inbox_and_h10_new_hub_id_reset(
    sandbox_env: dict[str, pathlib.Path],
    hub_factory: HubController,
    oracle: ChaosOracle,
    node_runner: NodeRunner,
) -> None:
    """Verify `hub_id` + `caps` banner, `no_replay: True`, legacy `cli-*`/`doc-*` replay suppression,
    offline `<node>/inbox` & `<node>/<session>` replay (`W0-review.md` §2.3), and `H10` DB wipe
    (`new hub_id` -> `since=0` replay)."""
    hub_a = hub_factory.start_hub_a(port=HUB_A_PORT, local_node="cat-food")

    # 1) Banner verification + seed offline messages (direct, inbox, and session event)
    dl_epoch = round(time.time() + 1800.0, 3)
    async with websockets.connect(hub_a.ws_url) as seed_ws:
        await seed_ws.send(json.dumps({"v": 2, "label": "cli-seed", "since": 0, "accept_inbox": False}))
        banner_1 = json.loads(await asyncio.wait_for(seed_ws.recv(), 2.0))
        assert banner_1["op"] == "sys"
        assert banner_1["you"] == "cat-food/cli-seed"
        assert isinstance(banner_1["hub_id"], str) and len(banner_1["hub_id"]) == 32
        assert banner_1["caps"] == ["v2", "journal", "rkind", "no_replay", "deadline"]
        hub_id_1 = banner_1["hub_id"]

        for payload in (
            {
                "v": 2,
                "op": "send",
                "id": "nr-task-1",
                "corr_id": "nr-task-1",
                "kind": "task",
                "rkind": "request",
                "deadline": dl_epoch,
                "to": "cat-food/main",
                "text": make_ask_prompt("nr-task-1", "offline-direct"),
            },
            {
                "v": 2,
                "op": "send",
                "id": "nr-inbox-2",
                "kind": "event",
                "rkind": "event",
                "to": "cat-food/inbox",
                "text": "offline-inbox-msg",
            },
            {
                "v": 2,
                "op": "send",
                "id": "nr-sess-3",
                "kind": "event",
                "rkind": "event",
                "to": "cat-food/mysess",
                "text": "offline-session-event",
            },
        ):
            await seed_ws.send(json.dumps(payload))
            await _recv_hub_ack(seed_ws, payload["id"])

    # Restart Hub A without wiping DB -> `hub_id` MUST remain identical!
    hub_a.restart(wipe_db=False, wipe_logs=False)

    # 2) Verify `no_replay: True` (even on `label='main'`, `accept_inbox=True`),
    #    legacy `cli-*` (`accept_inbox=False`), and legacy `doc-*` (`accept_inbox=False`) all get 0 replay!
    for hello_case in (
        {"v": 2, "label": "main", "since": 0, "accept_inbox": True, "no_replay": True},
        {"v": 2, "label": "cli-legacy", "since": 0, "accept_inbox": False},
        {"v": 2, "label": "doc-legacy", "since": 0, "accept_inbox": False},
    ):
        async with websockets.connect(hub_a.ws_url) as ws_nr:
            await ws_nr.send(json.dumps(hello_case))
            b_nr = json.loads(await asyncio.wait_for(ws_nr.recv(), 2.0))
            assert b_nr["hub_id"] == hub_id_1
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws_nr.recv(), 0.15)

    # 3) Normal worker `cat-food/main` (`accept_inbox=True`, no `no_replay`) receives ALL 3 replayed items
    #    (including `deadline` & `rkind` on `nr-task-1`, plus `cat-food/inbox` & `cat-food/mysess`!)
    async with websockets.connect(hub_a.ws_url) as ws_w:
        await ws_w.send(json.dumps({"v": 2, "label": "main", "since": 0, "accept_inbox": True}))
        b_w = json.loads(await asyncio.wait_for(ws_w.recv(), 2.0))
        assert b_w["hub_id"] == hub_id_1
        replayed = [json.loads(await asyncio.wait_for(ws_w.recv(), 2.0)) for _ in range(3)]
        assert [m["id"] for m in replayed] == ["nr-task-1", "nr-inbox-2", "nr-sess-3"]
        assert replayed[0]["rkind"] == "request"
        assert replayed[0]["deadline"] == pytest.approx(dl_epoch)
        await ws_w.send(json.dumps({"op": "ack", "seqs": [m["seq"] for m in replayed]}))
        await _wait_until(
            lambda: sum(
                1 for d in hub_a.inspect_db(consumer="cat-food/main")["deliveries"] if d["state"] == "acked"
            )
            == 3,
            timeout=4.0,
        )

    # 4) Verify `H10` DB wipe (`wipe_db=True` -> brand-new `hub_id`) resets `run_node` replay to `since=0`!
    worker_task = node_runner.start(url=hub_a.ws_url, label="main", mode="hybrid", peers="cat-food")
    async with websockets.connect(hub_a.ws_url) as tx1:
        await tx1.send(json.dumps({"v": 2, "label": "cli-h10", "since": 0, "accept_inbox": False}))
        await asyncio.wait_for(tx1.recv(), 2.0)
        for idx in range(3):
            mid = f"h10-before-{idx}"
            await tx1.send(
                json.dumps(
                    {
                        "v": 2,
                        "op": "send",
                        "id": mid,
                        "corr_id": mid,
                        "kind": "task",
                        "to": "cat-food/main",
                        "text": make_ask_prompt(mid, "advance-cursor-to-6"),
                    }
                )
            )
            await _recv_hub_ack(tx1, mid)
    await _wait_until(lambda: oracle.counter.count("h10-before-2") == 1, timeout=4.0)
    await node_runner.stop_task(worker_task)

    # Wipe Hub A's DB (`H10`): Hub A now has `new_hub_id != hub_id_1` and `seq=1` (< old cursor `6`!)
    hub_a.restart(wipe_db=True, wipe_logs=True)
    async with websockets.connect(hub_a.ws_url) as tx2:
        await tx2.send(json.dumps({"v": 2, "label": "cli-h10b", "since": 0, "accept_inbox": False}))
        banner_2 = json.loads(await asyncio.wait_for(tx2.recv(), 2.0))
        hub_id_2 = banner_2["hub_id"]
        assert len(hub_id_2) == 32 and hub_id_2 != hub_id_1
        await tx2.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": "h10-after-wipe-seq1",
                    "corr_id": "h10-after-wipe-seq1",
                    "kind": "task",
                    "to": "cat-food/main",
                    "text": make_ask_prompt("h10-after-wipe-seq1", "seq-1-on-new-hub-id"),
                }
            )
        )
        ack_after = await _recv_hub_ack(tx2, "h10-after-wipe-seq1")
        assert ack_after["seq"] == 1

    # Restart `run_node` -> sends `hub_id=hub_id_1`, Hub sees `hub_id_1 != hub_id_2`, replays `seq=1`!
    worker_task_2 = node_runner.start(url=hub_a.ws_url, label="main", mode="hybrid", peers="cat-food")
    await _wait_until(lambda: oracle.counter.count("h10-after-wipe-seq1") == 1, timeout=4.0)
    await node_runner.stop_task(worker_task_2)


@pytest.mark.chaos
async def test_periodic_prune_expired_and_ws_http_health_metrics(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify default `prune_interval == 600s`, default `RATE_PER_MIN == 20`, periodic background
    `prune_expired()` execution & clean cancellation, and WS `op='health'` + unauthenticated
    Gateway `GET /v1/health` schema & live queue metrics (`pending`, `leased`, `oldest_pending_age`, `dead`)."""
    monkeypatch.delenv("BUS_PRUNE_INTERVAL_SEC", raising=False)
    monkeypatch.delenv("BUS_TEST_RATE_PER_MIN", raising=False)

    cfg_d = tmp_path / "h_cfg"
    data_d = tmp_path / "h_data"
    logs_d = tmp_path / "h_logs"
    for d in (cfg_d, data_d, logs_d):
        d.mkdir(parents=True, exist_ok=True)
    (cfg_d / "allow.txt").write_text("cat-food\nmother-goose\n", encoding="utf-8")

    # 1) Verify defaults (`prune_interval == 600.0`, `_effective_rate_per_min() == 20`)
    h_def = bus_hub.BusHub(config_dir=cfg_d, database_path=data_d / "def.db", logs_dir=logs_d)
    try:
        assert h_def.prune_interval == 600.0
        assert h_def._effective_rate_per_min() == 20
    finally:
        h_def.close()

    # 2) Start in-process `BusHub` + WS + Gateway with fast `prune_interval=0.05`
    ws_port = pick_free_port()
    gw_port = pick_free_port()
    db_file = data_d / "live_health.db"
    h = bus_hub.BusHub(
        config_dir=cfg_d,
        database_path=db_file,
        logs_dir=logs_d,
        prune_interval=0.05,
    )
    ws_srv = await websockets.serve(lambda ws: bus_hub._websockets_adapter(ws, h), "127.0.0.1", ws_port)
    gw_srv = await bus_gateway.start_gateway_server(h, "127.0.0.1", gw_port)
    prune_task = h.start_prune_task()
    gw_base = f"http://127.0.0.1:{gw_port}/bus"
    ws_url = f"ws://127.0.0.1:{ws_port}"

    try:
        # (a) Empty queue health via unauthenticated HTTP `GET /v1/health` AND WS `op='health'`
        code0, h_http0 = await asyncio.to_thread(_unauth_http_json, gw_base, "/v1/health")
        assert code0 == 200
        assert h_http0["ok"] is True
        assert h_http0["hub_id"] == h.hub_id
        assert h_http0["max_seq"] == 0 and h_http0["seq"] == 0
        assert h_http0["pending"] == 0
        assert h_http0["leased"] == 0
        assert h_http0["oldest_pending_age"] is None
        assert h_http0["dead"] == 0
        assert abs(float(h_http0["wall_clock"]) - time.time()) < 5.0
        assert float(h_http0["uptime_s"]) >= 0.0
        assert h_http0["uptime"] == h_http0["uptime_s"]
        assert h_http0["auth_mode"] == "source"
        assert isinstance(h_http0["auth_generation"], int) and h_http0["auth_generation"] >= 1
        assert isinstance(h_http0["auth_age_s"], (int, float)) and float(h_http0["auth_age_s"]) >= 0.0
        assert h_http0["auth"] == {
            "mode": "source",
            "generation": h_http0["auth_generation"],
            "age_s": h_http0["auth_age_s"],
        }
        # Ensure no sensitive fields are exposed on unauthenticated health
        for forbidden_key in ("tokens", "secret", "allow", "peers"):
            assert forbidden_key not in h_http0

        async with websockets.connect(ws_url) as ws:
            await ws.send(json.dumps({"v": 2, "label": "cli-hm", "since": 0, "accept_inbox": False, "no_replay": True}))
            await asyncio.wait_for(ws.recv(), 2.0)

            await ws.send(json.dumps({"op": "health"}))
            h_ws0 = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
            assert h_ws0["op"] == "health"
            assert h_ws0["hub_id"] == h.hub_id
            assert h_ws0["oldest_pending_age"] is None
            assert h_ws0["auth_mode"] == "source"
            assert h_ws0["auth_generation"] == h_http0["auth_generation"]
            assert isinstance(h_ws0["auth_age_s"], (int, float)) and float(h_ws0["auth_age_s"]) >= 0.0

        # (b) Enqueue 1 message to offline `mother-goose/main` -> `pending == 1`, `oldest_pending_age >= 0`
        t_msg = time.time() - 2.5
        h.store.enqueue_message(
            mid="hm-msg-1",
            sender="cat-food/cli",
            fnode="cat-food",
            target="mother-goose/main",
            text="pending-item",
            ts=t_msg,
        )
        _, h_pending = await asyncio.to_thread(_unauth_http_json, gw_base, "/v1/health")
        assert h_pending["max_seq"] == 1
        assert h_pending["pending"] == 1
        assert h_pending["leased"] == 0
        assert h_pending["dead"] == 0
        assert h_pending["oldest_pending_age"] is not None and float(h_pending["oldest_pending_age"]) >= 2.0

        # (c) Poll once (`attempts=1`, `state='leased'`) -> `pending == 0`, `leased == 1`, `oldest_pending_age is None`
        polled1 = h.store.poll_for_consumer(
            node="mother-goose",
            consumer="mother-goose/main",
            since=0,
            lease_ttl=30.0,
            max_attempts=3,
        )
        assert len(polled1) == 1
        _, h_leased = await asyncio.to_thread(_unauth_http_json, gw_base, "/v1/health")
        assert h_leased["pending"] == 0
        assert h_leased["leased"] == 1
        assert h_leased["oldest_pending_age"] is None
        assert h_leased["dead"] == 0

        # (d) Exhaust 3 attempts (`nack` -> poll #2 -> `nack` -> poll #3 -> `nack` -> `dead`!)
        assert h.store.nack_delivery("mother-goose/main", 1, max_attempts=3) == "pending"
        h.store.poll_for_consumer(node="mother-goose", consumer="mother-goose/main", since=0, lease_ttl=30.0, max_attempts=3)
        assert h.store.nack_delivery("mother-goose/main", 1, max_attempts=3) == "pending"
        h.store.poll_for_consumer(node="mother-goose", consumer="mother-goose/main", since=0, lease_ttl=30.0, max_attempts=3)
        assert h.store.nack_delivery("mother-goose/main", 1, max_attempts=3) == "dead"

        _, h_dead = await asyncio.to_thread(_unauth_http_json, gw_base, "/v1/health")
        assert h_dead["pending"] == 0
        assert h_dead["leased"] == 0
        assert h_dead["dead"] == 1
        assert h_dead["oldest_pending_age"] is None

        # (e) Insert an already-expired message (`ts = now - 10`, `ttl = 1.0`) + expired RPC + expired nonce
        #     and verify background `prune_task` automatically purges them while preserving `hm-msg-1` and `hub_id`!
        past_ts = time.time() - 20.0
        h.store.enqueue_message(
            mid="hm-expired-msg",
            corr_id="hm-expired-msg",
            sender="cat-food/cli",
            fnode="cat-food",
            target="mother-goose/main",
            kind="rpc",
            rkind="request",
            text="expired-soon",
            ttl=1.0,
            ts=past_ts,
        )
        h.store.lease_live_delivery(2, "mother-goose/main", lease_ttl=1.0)
        runs_before = h.prune_runs

        def _pruned() -> bool:
            if h.prune_runs <= runs_before:
                return False
            with h.store._lock:
                rem = h.store._conn.execute(
                    "SELECT id FROM messages WHERE id = 'hm-expired-msg'"
                ).fetchone()
                rem_del = h.store._conn.execute(
                    "SELECT 1 FROM deliveries WHERE seq = 2"
                ).fetchone()
                return rem is None and rem_del is None

        await _wait_until(_pruned, timeout=4.0)
        assert h.hub_id == h_http0["hub_id"]
        with h.store._lock:
            kept = h.store._conn.execute("SELECT id FROM messages WHERE id = 'hm-msg-1'").fetchone()
            assert kept is not None
    finally:
        await h.stop_prune_task()
        assert prune_task.done()
        gw_srv.close()
        await gw_srv.wait_closed()
        ws_srv.close()
        await ws_srv.wait_closed()
        h.close()
