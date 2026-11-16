"""Isolated unit & integration tests for agent_bus (Phase 1, Phase 2, Phase 3)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import pathlib
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

import websockets

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BUS_PKG = REPO_ROOT / "bus"
if str(BUS_PKG) not in sys.path:
    sys.path.insert(0, str(BUS_PKG))

from agent_bus import auth, bridge, client, gateway, hub, store  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class AgentBusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.cfg_dir = self.root / "cfg"
        self.data_dir = self.root / "data"
        self.log_dir = self.root / "logs"
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._orig_env = os.environ.copy()
        os.environ["BUS_CONFIG_DIR"] = str(self.cfg_dir)
        os.environ["BUS_DATA_DIR"] = str(self.data_dir)
        os.environ["BUS_LOG_DIR"] = str(self.log_dir)
        os.environ["BUS_DB_PATH"] = str(self.data_dir / "bus.db")
        os.environ["BUS_TEST_ALLOW_LOCALHOST"] = "1"
        os.environ["BUS_TEST_LOCAL_NODE"] = "cat-food"
        (self.cfg_dir / "allow.txt").write_text("cat-food\nmother-goose\nz-fold8\n", encoding="utf-8")
        auth.clear_whois_cache()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        auth.clear_whois_cache()
        self._tmp.cleanup()

    def test_store_migration_idempotency_and_leases(self) -> None:
        jsonl = self.log_dir / "messages.jsonl"
        now = time.time()
        jsonl.write_text(
            json.dumps(
                {
                    "op": "msg",
                    "seq": 152,
                    "id": "legacy-152",
                    "from": "cat-food/main",
                    "fnode": "cat-food",
                    "to": "mother-goose",
                    "text": "old",
                    "hop": 0,
                    "ts": now - 10,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        st = store.BusStore(self.data_dir / "bus.db")
        try:
            imported = st.import_jsonl_if_empty(jsonl)
            self.assertEqual(imported, 1)
            self.assertEqual(st.get_max_seq(), 152)

            m1, dup1 = st.enqueue_message(
                mid="m-abc",
                sender="cat-food/cli-1",
                fnode="cat-food",
                target="mother-goose/main",
                text="hello",
                kind="rpc",
                corr_id="m-abc",
            )
            self.assertFalse(dup1)
            self.assertEqual(m1["seq"], 153)

            m2, dup2 = st.enqueue_message(
                mid="m-abc",
                sender="cat-food/cli-1",
                fnode="cat-food",
                target="mother-goose/main",
                text="hello",
            )
            self.assertTrue(dup2)
            self.assertEqual(m2["seq"], 153)

            # First poll leases msg 153
            polled = st.poll_for_consumer(
                node="mother-goose",
                consumer="mother-goose/main",
                since=152,
                lease_ttl=0.15,
                max_attempts=3,
                reclaim_on_reconnect=False,
            )
            self.assertEqual([x["seq"] for x in polled], [153])

            # Immediate HTTP poll must NOT double-deliver while lease is active
            polled_again = st.poll_for_consumer(
                node="mother-goose",
                consumer="mother-goose/main",
                since=152,
                lease_ttl=0.15,
                max_attempts=3,
                reclaim_on_reconnect=False,
            )
            self.assertEqual(polled_again, [])

            # After lease expires, attempts=2 -> reclaimed
            time.sleep(0.2)
            polled_2 = st.poll_for_consumer(
                node="mother-goose",
                consumer="mother-goose/main",
                since=152,
                lease_ttl=0.1,
                max_attempts=3,
                reclaim_on_reconnect=False,
            )
            self.assertEqual([x["seq"] for x in polled_2], [153])

            # Attempts=3
            time.sleep(0.15)
            polled_3 = st.poll_for_consumer(
                node="mother-goose",
                consumer="mother-goose/main",
                since=152,
                lease_ttl=0.1,
                max_attempts=3,
                reclaim_on_reconnect=False,
            )
            self.assertEqual([x["seq"] for x in polled_3], [153])

            # Attempts exhausted -> dead-lettered
            time.sleep(0.15)
            polled_4 = st.poll_for_consumer(
                node="mother-goose",
                consumer="mother-goose/main",
                since=152,
                lease_ttl=0.1,
                max_attempts=3,
                reclaim_on_reconnect=False,
            )
            self.assertEqual(polled_4, [])
        finally:
            st.close()

    def test_external_hmac_replay_and_namespace_isolation(self) -> None:
        st = store.BusStore(self.data_dir / "bus.db")
        try:
            tok = auth.create_token(
                self.cfg_dir,
                "runner",
                scopes=["send:cat-food/inbox", "poll:ext-runner"],
                allow_exec=False,
            )
            self.assertEqual(tok["node"], "ext-runner")

            body = json.dumps({"to": "cat-food/inbox", "kind": "event", "text": "hi"}).encode("utf-8")
            hdrs = auth.build_hmac_headers(tok["id"], tok["secret"], body)

            res_ok = auth.verify_external_hmac(hdrs, body, st, self.cfg_dir)
            self.assertTrue(res_ok.ok)
            self.assertEqual(res_ok.trust, "external")
            self.assertEqual(res_ok.node, "ext-runner")

            # Replay with exact same headers must be rejected
            res_rep = auth.verify_external_hmac(hdrs, body, st, self.cfg_dir)
            self.assertFalse(res_rep.ok)
            self.assertEqual(res_rep.code, 401)

            # Tampered body fails HMAC
            hdrs2 = auth.build_hmac_headers(tok["id"], tok["secret"], body)
            res_bad = auth.verify_external_hmac(hdrs2, b'{"tampered":true}', st, self.cfg_dir)
            self.assertFalse(res_bad.ok)
            self.assertEqual(res_bad.code, 403)

            # Expired timestamp fails
            old_ts = str(int(time.time()) - 400)
            old_sig = auth.compute_hmac_signature(tok["secret"], old_ts, body, nonce="n1")
            res_exp = auth.verify_external_hmac(
                {
                    "Authorization": f"Bearer {tok['id']}",
                    "X-Bus-Timestamp": old_ts,
                    "X-Bus-Nonce": "n1",
                    "X-Bus-Signature": old_sig,
                },
                body,
                st,
                self.cfg_dir,
            )
            self.assertFalse(res_exp.ok)
            self.assertEqual(res_exp.code, 401)

            # Non-finite timestamp ("nan" / "inf") must also fail with 401
            for bad_ts in ("nan", "inf", "-inf"):
                bad_sig = auth.compute_hmac_signature(tok["secret"], bad_ts, body, nonce=f"n-{bad_ts}")
                res_bad_ts = auth.verify_external_hmac(
                    {
                        "Authorization": f"Bearer {tok['id']}",
                        "X-Bus-Timestamp": bad_ts,
                        "X-Bus-Nonce": f"n-{bad_ts}",
                        "X-Bus-Signature": bad_sig,
                    },
                    body,
                    st,
                    self.cfg_dir,
                )
                self.assertFalse(res_bad_ts.ok)
                self.assertEqual(res_bad_ts.code, 401)

            # Wildcard scope with allow_exec=False must still block bare node, main, non-event, and !ask/!update
            tok_wild = auth.create_token(
                self.cfg_dir,
                "wild",
                scopes=["send:cat-food", "send:cat-food/*", "send:*"],
                allow_exec=False,
            )
            wild_auth = auth.AuthResult(
                ok=True, node=tok_wild["node"], trust="external", scopes=tok_wild["scopes"], allow_exec=False
            )
            self.assertFalse(auth.check_external_send_permission(wild_auth, "cat-food", "event", "!ask hi")[0])
            self.assertFalse(auth.check_external_send_permission(wild_auth, "cat-food/main", "event", "hi")[0])
            self.assertFalse(auth.check_external_send_permission(wild_auth, "cat-food/inbox", "legacy", "hi")[0])
            self.assertFalse(auth.check_external_send_permission(wild_auth, "cat-food/inbox", "rpc", "hi")[0])
            self.assertFalse(auth.check_external_send_permission(wild_auth, "cat-food/inbox", "event", "!ask hi")[0])
            self.assertFalse(auth.check_external_send_permission(wild_auth, "cat-food/inbox", "event", "!update")[0])
            self.assertTrue(auth.check_external_send_permission(wild_auth, "cat-food/inbox", "event", "normal alert")[0])

            # Namespace & allow_exec checks
            self.assertFalse(auth.check_external_send_permission(res_ok, "*", "event")[0])
            self.assertFalse(auth.check_external_send_permission(res_ok, "cat-food/main", "rpc")[0])
            self.assertTrue(auth.check_external_send_permission(res_ok, "cat-food/inbox", "event")[0])

            # Broadcast & internal messages must NOT leak to external poll
            st.enqueue_message(
                mid="int-bcast",
                sender="cat-food/main",
                fnode="cat-food",
                target="*",
                text="internal secret",
                trust="internal",
            )
            st.enqueue_message(
                mid="ext-req-1",
                sender="ext-runner/cli",
                fnode="ext-runner",
                target="cat-food/inbox",
                text="ping",
                kind="event",
                corr_id="ext-req-1",
                trust="external",
            )
            st.enqueue_message(
                mid="ext-rep-1",
                sender="cat-food/main",
                fnode="cat-food",
                target="cat-food/inbox",
                text="pong",
                kind="task",
                corr_id="ext-req-1",
                trust="internal",
            )
            polled = st.poll_for_consumer(
                node="ext-runner",
                consumer="ext-runner/cli",
                since=0,
                trust="external",
            )
            self.assertEqual([m["id"] for m in polled], ["ext-rep-1"])
        finally:
            st.close()

    def test_claude_status_detection(self) -> None:
        self.assertEqual(client.detect_claude_status(0, "OK", ""), "ok")
        self.assertEqual(
            client.detect_claude_status(1, "Failed to authenticate: this login reached its time limit", ""),
            "auth_error",
        )
        self.assertEqual(client.detect_claude_status(2, "", "command crashed"), "exec_error")

    def test_ask_claude_allowed_tools_and_extra_args(self) -> None:
        calls = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            res = mock.Mock()
            res.returncode = 0
            res.stdout = "mocked response"
            res.stderr = ""
            return res

        with mock.patch("subprocess.run", side_effect=fake_run):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("BUS_CLAUDE_ALLOWED_TOOLS", None)
                os.environ.pop("BUS_CLAUDE_EXTRA_ARGS", None)
                out, st = client.ask_claude("hi", "test_from", cwd=self.root)
                self.assertEqual(st, "ok")
                self.assertIn("--allowedTools", calls[-1])
                self.assertIn("Read,Grep,Glob,Bash,Edit,Write,WebFetch,mcp__*", calls[-1])

                # Custom tools & extra args
                os.environ["BUS_CLAUDE_ALLOWED_TOOLS"] = "Read,Bash,mcp__custom"
                os.environ["BUS_CLAUDE_EXTRA_ARGS"] = "--dangerously-skip-permissions"
                out, st = client.ask_claude("hi2", "test_from", cwd=self.root)
                self.assertEqual(st, "ok")
                self.assertIn("Read,Bash,mcp__custom", calls[-1])
                self.assertIn("--dangerously-skip-permissions", calls[-1])

    def test_acc_bridge_event_emit(self) -> None:
        acc_repo = self.root / "acc"
        rt_dir = acc_repo / "runtime" / "cattin01-20261106142050"
        (acc_repo / "bin").mkdir(parents=True, exist_ok=True)
        rt_dir.mkdir(parents=True, exist_ok=True)
        (acc_repo / "runtime" / "registry.json").write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "session_name": "cattin01",
                            "runtime_dir": str(rt_dir),
                            "retired": False,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        capture_file = self.root / "emit_capture.json"
        emit_sh = acc_repo / "bin" / "event-emit.sh"
        emit_sh.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"with open({str(capture_file)!r}, 'w') as fh:\n"
            "    json.dump({'env': {'ACC_RUNTIME': os.environ.get('ACC_RUNTIME'), 'SESSION_NAME': os.environ.get('SESSION_NAME')}, 'args': sys.argv[1:]}, fh)\n",
            encoding="utf-8",
        )
        emit_sh.chmod(emit_sh.stat().st_mode | stat.S_IEXEC)

        res = bridge.deliver_to_inbox(
            {
                "seq": 77,
                "id": "abc77",
                "from": "mother-goose/main",
                "to": "cat-food/cattin01",
                "kind": "event",
                "ts": 1763196000.0,
                "text": "build finished cleanly",
            },
            cfg_dir=self.cfg_dir,
            target_session="cattin01",
            acc_repo=acc_repo,
        )
        self.assertTrue(res["acc_emitted"], res)
        self.assertTrue(pathlib.Path(res["detail_path"]).is_file())
        captured = json.loads(capture_file.read_text(encoding="utf-8"))
        self.assertEqual(captured["env"]["ACC_RUNTIME"], str(rt_dir))
        self.assertEqual(captured["env"]["SESSION_NAME"], "cattin01")
        self.assertEqual(captured["args"][0], "external")
        self.assertEqual(captured["args"][1], "done")
        self.assertEqual(captured["args"][2], "bus-77")
        self.assertEqual(captured["args"][4], res["detail_path"])
        self.assertEqual(captured["args"][5], "bus-77-abc77")
        self.assertEqual(captured["args"][6], "bus_msg")

        inbox_items = bridge.read_inbox(self.cfg_dir)
        self.assertEqual(len(inbox_items), 1)
        self.assertEqual(bridge.ack_inbox(self.cfg_dir, "77"), 1)
        self.assertEqual(bridge.read_inbox(self.cfg_dir), [])

    def test_hub_and_gateway_e2e_sync_async_and_outbox(self) -> None:
        ws_port = _free_port()
        gw_port = _free_port()
        tok = auth.create_token(
            self.cfg_dir,
            "runner",
            scopes=["rpc:cat-food/main", "send:cat-food/inbox", "poll:ext-runner"],
            allow_exec=True,
        )

        async def _run_e2e() -> None:
            h = hub.BusHub(
                config_dir=self.cfg_dir,
                database_path=self.data_dir / "bus.db",
                logs_dir=self.log_dir,
            )
            ws_srv = await websockets.serve(
                lambda ws: hub._websockets_adapter(ws, h),
                "127.0.0.1",
                ws_port,
            )
            gw_srv = await gateway.start_gateway_server(h, "127.0.0.1", gw_port)
            try:
                # 1. Start mock worker peer "cat-food/main"
                worker_url = f"ws://127.0.0.1:{ws_port}"

                async def mock_worker() -> None:
                    async with websockets.connect(worker_url) as ws:
                        await ws.send(json.dumps({"label": "main", "since": 0}))
                        await ws.recv()  # connected banner
                        handled = 0
                        while handled < 3:
                            raw = await asyncio.wait_for(ws.recv(), 5.0)
                            m = json.loads(raw)
                            if m.get("op") != "msg":
                                continue
                            handled += 1
                            cid = m.get("corr_id") or m["id"]
                            await ws.send(
                                json.dumps(
                                    {
                                        "op": "progress",
                                        "corr_id": cid,
                                        "to": m["from"],
                                        "status": "running",
                                    }
                                )
                            )
                            await asyncio.sleep(0.05)
                            await ws.send(
                                json.dumps(
                                    {
                                        "v": 2,
                                        "op": "send",
                                        "id": f"rep-{m['id']}",
                                        "corr_id": cid,
                                        "kind": "rpc",
                                        "status": "ok",
                                        "to": m["from"],
                                        "text": f"reply:{m['text']}",
                                        "hop": m.get("hop", 0) + 1,
                                    }
                                )
                            )
                            await ws.send(json.dumps({"op": "ack", "seq": m["seq"]}))

                worker_task = asyncio.create_task(mock_worker())

                # Wait for worker to register deterministically
                for _ in range(40):
                    if "cat-food/main" in h.peers:
                        break
                    await asyncio.sleep(0.02)
                self.assertIn("cat-food/main", h.peers)

                # 2. Two concurrent internal WS RPC callers (no 4002 collision!)
                async def rpc_call(idx: int) -> str:
                    async with websockets.connect(worker_url) as ws:
                        await ws.send(json.dumps({"label": f"cli-test{idx}", "since": 10**9}))
                        await ws.recv()
                        mid = f"req-{idx}"
                        await ws.send(
                            json.dumps(
                                {
                                    "v": 2,
                                    "op": "send",
                                    "id": mid,
                                    "corr_id": mid,
                                    "kind": "rpc",
                                    "to": "cat-food/main",
                                    "text": f"job-{idx}",
                                }
                            )
                        )
                        saw_progress = False
                        while True:
                            frame = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
                            if frame.get("op") == "progress" and frame.get("corr_id") == mid:
                                saw_progress = True
                            elif frame.get("op") == "msg" and frame.get("corr_id") == mid:
                                self.assertTrue(saw_progress)
                                return str(frame["text"])

                r1, r2 = await asyncio.gather(rpc_call(1), rpc_call(2))
                self.assertEqual(r1, "reply:job-1")
                self.assertEqual(r2, "reply:job-2")

                # 3. External HTTP Gateway request with Tailscale-Funnel-Request header
                def _http_call(route: str, method: str = "GET", payload: dict | None = None, extra_headers: dict | None = None):
                    raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
                    hdrs = auth.build_hmac_headers(tok["id"], tok["secret"], raw)
                    hdrs["Tailscale-Funnel-Request"] = "?1"
                    if payload is not None:
                        hdrs["Content-Type"] = "application/json"
                    if extra_headers:
                        hdrs.update(extra_headers)
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{gw_port}{route}",
                        data=raw if method == "POST" else None,
                        headers=hdrs,
                        method=method,
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        return resp.status, json.loads(resp.read().decode("utf-8"))

                # Health works without auth
                def _health_check():
                    with urllib.request.urlopen(f"http://127.0.0.1:{gw_port}/bus/v1/health", timeout=5) as resp:
                        return resp.status, json.loads(resp.read().decode("utf-8"))

                hs, hb = await asyncio.to_thread(_health_check)
                self.assertEqual(hs, 200)
                self.assertTrue(hb["ok"])

                # Unauthenticated Funnel request fails with 401 and logs to stderr
                def _unauth_check():
                    req_unauth = urllib.request.Request(
                        f"http://127.0.0.1:{gw_port}/bus/v1/peers",
                        headers={"Tailscale-Funnel-Request": "?1"},
                    )
                    urllib.request.urlopen(req_unauth, timeout=5)

                stderr_buf = io.StringIO()
                with contextlib.redirect_stderr(stderr_buf):
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        await asyncio.to_thread(_unauth_check)
                    self.assertEqual(ctx.exception.code, 401)
                    ctx.exception.close()
                self.assertIn("[auth-reject]", stderr_buf.getvalue())

                # Send RPC over HTTP Gateway
                sc, send_res = await asyncio.to_thread(
                    _http_call,
                    "/bus/v1/send",
                    "POST",
                    {
                        "v": 2,
                        "id": "ext-rpc-1",
                        "corr_id": "ext-rpc-1",
                        "kind": "rpc",
                        "to": "cat-food/main",
                        "text": "from-funnel",
                        "label": "extcli",
                    },
                )
                self.assertEqual(sc, 200)
                self.assertTrue(send_res["ok"])

                await worker_task

                # Query RPC result over HTTP Gateway
                rc, rpc_res = await asyncio.to_thread(_http_call, "/bus/v1/rpc/ext-rpc-1", "GET")
                self.assertEqual(rc, 200)
                self.assertTrue(rpc_res["found"])
                self.assertEqual(rpc_res["status"], "ok")
                self.assertEqual(rpc_res["text"], "reply:from-funnel")

                # Poll + Ack over HTTP Gateway
                pc, poll_res = await asyncio.to_thread(_http_call, "/bus/v1/poll?label=extcli&since=0", "GET")
                self.assertEqual(pc, 200)
                self.assertTrue(any(m["corr_id"] == "ext-rpc-1" for m in poll_res["messages"]))
                ack_seqs = [m["seq"] for m in poll_res["messages"]]
                ac, ack_res = await asyncio.to_thread(
                    _http_call, "/bus/v1/ack", "POST", {"label": "extcli", "seqs": ack_seqs}
                )
                self.assertEqual(ac, 200)
                self.assertEqual(ack_res["acked"], len(ack_seqs))

                # 4. External WS over Gateway /bus/v1/ws + client outbox flush
                client.append_outbox(
                    {
                        "v": 2,
                        "op": "send",
                        "id": "outbox-1",
                        "kind": "event",
                        "to": "cat-food/inbox",
                        "text": "queued-offline-msg",
                    },
                    self.cfg_dir,
                )
                self.assertEqual(len(client.load_outbox(self.cfg_dir)), 1)

                os.environ["BUS_AUTH_TOKEN"] = tok["id"]
                os.environ["BUS_AUTH_SECRET"] = tok["secret"]
                async with websockets.connect(f"ws://127.0.0.1:{gw_port}/bus/v1/ws") as gws:
                    await gws.send(json.dumps(client.build_ws_hello("extws", 0)))
                    banner = json.loads(await gws.recv())
                    self.assertEqual(banner["you"], "ext-runner/extws")
                    self.assertEqual(banner["trust"], "external")

                    ack_map: dict[str, asyncio.Event] = {}

                    async def _pump_ack() -> None:
                        while True:
                            msg = json.loads(await gws.recv())
                            if msg.get("ack") and msg["ack"] in ack_map:
                                ack_map[msg["ack"]].set()

                    pumper = asyncio.create_task(_pump_ack())
                    try:
                        await client._flush_outbox_over_ws(gws, self.cfg_dir, ack_map)
                    finally:
                        pumper.cancel()

                self.assertEqual(client.load_outbox(self.cfg_dir), [])
            finally:
                gw_srv.close()
                await gw_srv.wait_closed()
                ws_srv.close()
                await ws_srv.wait_closed()
                h.close()

        asyncio.run(_run_e2e())

    def test_shims_help_and_no_hardcoded_tailnet_ips(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(BUS_PKG)
        for cmd in (
            [sys.executable, str(REPO_ROOT / "docs/tailhub-termux/bus/hub.py"), "--help"],
            [sys.executable, str(REPO_ROOT / "docs/tailhub-termux/bus/node.py"), "--help"],
            [sys.executable, "-m", "agent_bus.cli", "--help"],
        ):
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, msg=f"{cmd}: {r.stderr}")

        # Ensure public source code contains no hardcoded 100.x tailnet IPs
        import re

        ip_pat = re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b")
        for pyfile in list((BUS_PKG / "agent_bus").glob("*.py")) + [
            REPO_ROOT / "docs/tailhub-termux/bus/hub.py",
            REPO_ROOT / "docs/tailhub-termux/bus/node.py",
        ]:
            content = pyfile.read_text(encoding="utf-8")
            matches = [m.group(0) for m in ip_pat.finditer(content) if m.group(0) != "100.64.0.0"]
            self.assertEqual(matches, [], str(pyfile))

    def test_live_run_node_worker_auth_error_late_result_and_cli_commands(self) -> None:
        ws_port = _free_port()
        gw_port = _free_port()

        # Mock claude executable
        mock_claude = self.root / "mock_claude.py"
        mock_claude.write_text(
            f"#!{sys.executable}\n"
            "import sys, time\n"
            "prompt = sys.stdin.read()\n"
            "if 'TRIGGER_AUTH_FAIL' in prompt:\n"
            "    sys.stdout.write('Failed to authenticate: this login reached its time limit\\n')\n"
            "    sys.exit(1)\n"
            "if 'TRIGGER_SLOW' in prompt:\n"
            "    time.sleep(0.45)\n"
            "    sys.stdout.write('SLOW_OK\\n')\n"
            "    sys.exit(0)\n"
            "sys.stdout.write('MOCK_OK\\n')\n",
            encoding="utf-8",
        )
        mock_claude.chmod(mock_claude.stat().st_mode | stat.S_IEXEC)
        os.environ["BUS_CLAUDE_CMD"] = str(mock_claude)

        # Mock ACC repo for live inbox bridge inside run_node
        acc_repo = self.root / "acc_live"
        rt_dir = acc_repo / "runtime" / "cattin01-20261106142050"
        (acc_repo / "bin").mkdir(parents=True, exist_ok=True)
        rt_dir.mkdir(parents=True, exist_ok=True)
        (acc_repo / "runtime" / "registry.json").write_text(
            json.dumps({"sessions": [{"session_name": "cattin01", "runtime_dir": str(rt_dir), "retired": False}]}),
            encoding="utf-8",
        )
        capture_file = self.root / "live_emit.json"
        emit_sh = acc_repo / "bin" / "event-emit.sh"
        emit_sh.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"with open({str(capture_file)!r}, 'w') as fh:\n"
            "    json.dump({'ACC_RUNTIME': os.environ.get('ACC_RUNTIME'), 'SESSION_NAME': os.environ.get('SESSION_NAME'), 'args': sys.argv[1:]}, fh)\n",
            encoding="utf-8",
        )
        emit_sh.chmod(emit_sh.stat().st_mode | stat.S_IEXEC)
        os.environ["BUS_ACC_REPO"] = str(acc_repo)

        async def _run() -> None:
            h = hub.BusHub(
                config_dir=self.cfg_dir,
                database_path=self.data_dir / "bus.db",
                logs_dir=self.log_dir,
            )
            ws_srv = await websockets.serve(lambda ws: hub._websockets_adapter(ws, h), "127.0.0.1", ws_port)
            gw_srv = await gateway.start_gateway_server(h, "127.0.0.1", gw_port)
            url = f"ws://127.0.0.1:{ws_port}"

            import argparse
            run_args = argparse.Namespace(
                cmd="run",
                label="main",
                mode="hybrid",
                peers="cat-food",
                url=url,
                cwd=str(self.root),
                no_auto_update=True,
            )
            node_task = asyncio.create_task(client.run_node(run_args))
            try:
                # Wait for cat-food/main to register
                for _ in range(40):
                    if "cat-food/main" in h.peers:
                        break
                    await asyncio.sleep(0.05)
                self.assertIn("cat-food/main", h.peers)

                caller_cfg = self.root / "caller_cfg"
                caller_cfg.mkdir(parents=True, exist_ok=True)

                async def _caller_oneshot(ns: argparse.Namespace) -> int:
                    prev_cfg = os.environ.get("BUS_CONFIG_DIR")
                    os.environ["BUS_CONFIG_DIR"] = str(caller_cfg)
                    try:
                        return await client.oneshot(ns)
                    finally:
                        if prev_cfg is None:
                            os.environ.pop("BUS_CONFIG_DIR", None)
                        else:
                            os.environ["BUS_CONFIG_DIR"] = prev_cfg

                # 1. Normal sync RPC
                buf_ok = io.StringIO()
                with contextlib.redirect_stdout(buf_ok):
                    rc_ok = await _caller_oneshot(
                        argparse.Namespace(
                            cmd="send",
                            to="cat-food/main",
                            text="hello",
                            wait=5.0,
                            hop=0,
                            wait_first=False,
                            update=False,
                            say=False,
                            kind=None,
                            corr_id=None,
                            ttl=86400,
                            url=url,
                            offline_queue=False,
                        )
                    )
                self.assertEqual(rc_ok, 0)
                self.assertIn("MOCK_OK", buf_ok.getvalue())
                self.assertGreaterEqual(client.load_cursor("main", self.cfg_dir), 1)

                # 2. Auth failure detection -> exit code 3
                buf_auth = io.StringIO()
                with contextlib.redirect_stdout(buf_auth):
                    rc_auth = await _caller_oneshot(
                        argparse.Namespace(
                            cmd="send",
                            to="cat-food/main",
                            text="TRIGGER_AUTH_FAIL",
                            wait=5.0,
                            hop=0,
                            wait_first=False,
                            update=False,
                            say=False,
                            kind=None,
                            corr_id=None,
                            ttl=86400,
                            url=url,
                            offline_queue=False,
                        )
                    )
                self.assertEqual(rc_auth, 3)

                # 3. Caller timeout (124) + late reply retrieval via `abus result`
                err_slow = io.StringIO()
                with contextlib.redirect_stderr(err_slow):
                    rc_slow = await _caller_oneshot(
                        argparse.Namespace(
                            cmd="send",
                            to="cat-food/main",
                            text="TRIGGER_SLOW",
                            wait=0.15,
                            hop=0,
                            wait_first=False,
                            update=False,
                            say=False,
                            kind="rpc",
                            corr_id="slow-corr-1",
                            ttl=86400,
                            url=url,
                            offline_queue=False,
                        )
                    )
                self.assertEqual(rc_slow, 124)
                self.assertIn("TIMEOUT_PENDING corr_id=slow-corr-1", err_slow.getvalue())

                env = os.environ.copy()
                env["PYTHONPATH"] = str(BUS_PKG)
                proc_res = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-m", "agent_bus.cli", "result", "slow-corr-1", "--wait", "5", "--url", url, "--json"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(proc_res.returncode, 0, proc_res.stderr)
                res_obj = json.loads(proc_res.stdout)
                self.assertEqual(res_obj["status"], "ok")
                self.assertIn("SLOW_OK", res_obj["text"])

                # 4. Event to cat-food/cattin01 -> ACC bridge event-emit.sh
                rc_evt = await _caller_oneshot(
                    argparse.Namespace(
                        cmd="send",
                        to="cat-food/cattin01",
                        text="wake supervisor",
                        wait=0,
                        hop=0,
                        wait_first=False,
                        update=False,
                        say=True,
                        kind="event",
                        corr_id=None,
                        ttl=86400,
                        url=url,
                        offline_queue=False,
                    )
                )
                self.assertEqual(rc_evt, 0)
                for _ in range(40):
                    if capture_file.exists():
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(capture_file.exists())
                cap = json.loads(capture_file.read_text(encoding="utf-8"))
                self.assertEqual(cap["SESSION_NAME"], "cattin01")
                self.assertEqual(cap["ACC_RUNTIME"], str(rt_dir))
                self.assertEqual(cap["args"][0], "external")
                self.assertEqual(cap["args"][1], "done")

                # 5. CLI subprocess commands: init, token, inbox, ack, doctor
                env = os.environ.copy()
                env["PYTHONPATH"] = str(BUS_PKG)
                for subcmd in (
                    [sys.executable, "-m", "agent_bus.cli", "init", "--url", f"http://127.0.0.1:{gw_port}"],
                    [sys.executable, "-m", "agent_bus.cli", "token", "create", "--node", "ext-test"],
                    [sys.executable, "-m", "agent_bus.cli", "token", "list"],
                    [sys.executable, "-m", "agent_bus.cli", "inbox"],
                    [sys.executable, "-m", "agent_bus.cli", "ack", "all"],
                    [sys.executable, "-m", "agent_bus.cli", "doctor", "--json"],
                ):
                    proc = await asyncio.to_thread(
                        subprocess.run, subcmd, env=env, capture_output=True, text=True, timeout=10
                    )
                    self.assertEqual(proc.returncode, 0, msg=f"{subcmd}: {proc.stderr} {proc.stdout}")
            finally:
                node_task.cancel()
                try:
                    await node_task
                except asyncio.CancelledError:
                    pass
                gw_srv.close()
                await gw_srv.wait_closed()
                ws_srv.close()
                await ws_srv.wait_closed()
                h.close()

        asyncio.run(_run())

    def test_external_progress_tampering_and_frame_injection_blocked(self) -> None:
        ws_port = _free_port()
        gw_port = _free_port()
        tok = auth.create_token(
            self.cfg_dir,
            "attacker",
            scopes=["send:cat-food/inbox", "poll:ext-attacker"],
            allow_exec=False,
        )

        async def _run() -> None:
            h = hub.BusHub(
                config_dir=self.cfg_dir,
                database_path=self.data_dir / "bus.db",
                logs_dir=self.log_dir,
            )
            ws_srv = await websockets.serve(lambda ws: hub._websockets_adapter(ws, h), "127.0.0.1", ws_port)
            gw_srv = await gateway.start_gateway_server(h, "127.0.0.1", gw_port)
            try:
                async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as victim_ws:
                    await victim_ws.send(json.dumps({"label": "cli-victim", "since": 10**9}))
                    await victim_ws.recv()
                    await victim_ws.send(
                        json.dumps(
                            {
                                "v": 2,
                                "op": "send",
                                "id": "internal-corr-99",
                                "corr_id": "internal-corr-99",
                                "kind": "rpc",
                                "to": "cat-food/main",
                                "text": "!ask secret",
                            }
                        )
                    )
                    ack_msg = json.loads(await victim_ws.recv())
                    self.assertIn("ack seq=", ack_msg["text"])

                    os.environ["BUS_AUTH_TOKEN"] = tok["id"]
                    os.environ["BUS_AUTH_SECRET"] = tok["secret"]
                    async with websockets.connect(f"ws://127.0.0.1:{gw_port}/bus/v1/ws") as ext_ws:
                        await ext_ws.send(json.dumps(client.build_ws_hello("ext", 0)))
                        await ext_ws.recv()  # banner

                        # 1. Attempt to forge terminal ok via progress on internal corr_id
                        await ext_ws.send(
                            json.dumps(
                                {
                                    "op": "progress",
                                    "corr_id": "internal-corr-99",
                                    "status": "ok",
                                    "text": "forged",
                                    "to": "cat-food/cli-victim",
                                }
                            )
                        )
                        err1 = json.loads(await asyncio.wait_for(ext_ws.recv(), 3.0))
                        self.assertEqual(err1.get("error"), "forbidden")

                        # 2. Attempt to inject progress on unknown corr_id
                        await ext_ws.send(
                            json.dumps(
                                {
                                    "op": "progress",
                                    "corr_id": "nonexistent-cid",
                                    "status": "ok",
                                    "text": "forged",
                                    "to": "cat-food/cli-victim",
                                }
                            )
                        )
                        err2 = json.loads(await asyncio.wait_for(ext_ws.recv(), 3.0))
                        self.assertEqual(err2.get("error"), "forbidden")

                    # Victim must receive NO injected progress frames, and SQLite rpc_results must stay pending
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(victim_ws.recv(), 0.2)
                    rpc = h.store.get_rpc_result("internal-corr-99")
                    self.assertIsNotNone(rpc)
                    self.assertEqual(rpc["status"], "pending")
                    self.assertEqual(rpc["text"], "")
            finally:
                gw_srv.close()
                await gw_srv.wait_closed()
                ws_srv.close()
                await ws_srv.wait_closed()
                h.close()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
