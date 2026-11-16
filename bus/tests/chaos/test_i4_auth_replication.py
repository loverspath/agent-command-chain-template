"""WBS I4 deterministic chaos & counterexample tests (`X10` a–e + `R12`).

Covers:
- `X10(a)`: Token `revoke` + `allow.txt` removal + manual file edit hash bump + deleted-token
  cumulative `tombstones` on source Hub A -> replica Hub B syncs next tick -> immediate `4003`
  eviction of open revoked-token WS sessions (both `/bus/v1/ws` and hub WS) and removed-allow
  internal WS sessions on BOTH source & replica, while valid sessions stay uninterrupted.
- `X10(b)`: Source Hub A permanent loss (`SIGKILL`) -> replica Hub B serves LKG within
  `auth_max_stale`, then after `auth_max_stale` rejects `ext` HTTP/WS auth (and evicts open
  `ext` WS with `4003`) while preserving internal `whois` `allow` LKG availability.
- `X10(c)`: Crash/exception after writing `auth-gen/<gen>/` files right before `auth-current`
  atomic pointer swap (both in-process hook and subprocess `os._exit(91)` crash) -> zero
  generation mixing, previous generation stays intact, next sync transitions cleanly; plus
  downgrade (`gen <= current`) and corrupted `sha256` rejection + initial unsynced fail-closed.
- `X10(d)`: `op: 'auth_sync'` rejected with `error == 'forbidden'` and zero secret leakage for
  internal nodes not in `hub_peers`, external `ext` tokens, and replica hubs; plus single-writer
  enforcement rejecting `abus token create/revoke` CLI and Python mutation APIs in replica mode.
- `X10(e)`: Default constants (`60s` sync, `86400s` max stale), source-mode 100% compatibility,
  and accurate `auth_mode`, `auth_generation`, `auth_age_s` on both WS `op: 'health'` and
  unauthenticated Gateway `GET /v1/health`.
- `R12`: Lead/Sol counterexample (`R12_revoke_stale`: old `A=False, B=True`) resolved -> after
  revoke + sync, BOTH Hub A and Hub B reject the token across HTTP & WS (`A=False, B=False`)
  and close open sessions with `4003`.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

import pytest
import websockets

from .hub_proc import BUS_ROOT, HubController, pick_free_port

from agent_bus import auth as bus_auth  # noqa: E402
from agent_bus import client as bus_client  # noqa: E402
from agent_bus import config as bus_config  # noqa: E402
from agent_bus import gateway as bus_gateway  # noqa: E402
from agent_bus import hub as bus_hub  # noqa: E402


WHOIS_IP_MAP = {
    "127.0.0.1": "cat-food",
    "127.0.0.2": "mother-goose",
    "127.0.0.3": "z-fold8",
    "127.0.0.4": "rogue-peer",
    "127.0.0.5": "new-peer",
}


def _write_whois_script(dir_path: pathlib.Path) -> pathlib.Path:
    """Create a deterministic `BUS_WHOIS_CMD` mapping loopback IPs `127.0.0.1..5` to node names."""
    dir_path.mkdir(parents=True, exist_ok=True)
    script = dir_path / "mock_whois.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"MAPPING = {WHOIS_IP_MAP!r}\n"
        "ip = (sys.argv[1] if len(sys.argv) > 1 else '').strip()\n"
        "node = MAPPING.get(ip, '')\n"
        "if not node:\n"
        "    sys.exit(1)\n"
        "sys.stdout.write(json.dumps({'Node': {'ComputedName': node, 'Name': f'{node}.tailnet.ts.net'}}))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IRWXU)
    return script


async def _wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float = 5.0,
    interval: float = 0.02,
    desc: str = "condition",
) -> Any:
    deadline = time.monotonic() + timeout
    last_val: Any = None
    while time.monotonic() < deadline:
        res = predicate()
        if asyncio.iscoroutine(res):
            res = await res
        last_val = res
        if res:
            return res
        await asyncio.sleep(interval)
    raise AssertionError(f"Timed out after {timeout}s waiting for {desc} (last={last_val!r})")


def _http_get_unauth(gw_http_url: str, route: str, timeout: float = 3.0) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(f"{gw_http_url}{route}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        err.close()
        return err.code, (json.loads(raw) if raw.strip() else {})


def _http_ext_call(
    gw_http_url: str,
    route: str,
    tok: dict[str, Any],
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 3.0,
) -> tuple[int, dict[str, Any]]:
    raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
    hdrs = bus_auth.build_hmac_headers(tok["id"], tok["secret"], raw)
    hdrs["Tailscale-Funnel-Request"] = "?1"
    if payload is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{gw_http_url}{route}",
        data=(raw if method.upper() == "POST" else None),
        headers=hdrs,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        err.close()
        return err.code, (json.loads(body) if body.strip() else {})


def _build_ext_ws_hello(label: str, tok: dict[str, Any]) -> dict[str, Any]:
    ts = str(int(time.time()))
    nonce = f"wsn-{time.time_ns()}"
    sig = bus_auth.compute_hmac_signature(tok["secret"], ts, tok["id"].encode("utf-8"), nonce=nonce)
    return {
        "v": 2,
        "label": label,
        "since": 0,
        "accept_inbox": False,
        "no_replay": True,
        "auth": {
            "token_id": tok["id"],
            "ts": ts,
            "nonce": nonce,
            "sig": sig,
        },
    }


async def _open_internal_ws(ws_url: str, *, source_ip: str, label: str) -> Any:
    ws = await websockets.connect(ws_url, local_addr=(source_ip, 0), open_timeout=3.0, close_timeout=1.5)
    await ws.send(
        json.dumps(
            {
                "v": 2,
                "label": label,
                "since": 0,
                "accept_inbox": False,
                "no_replay": True,
            }
        )
    )
    banner = json.loads(await asyncio.wait_for(ws.recv(), 3.0))
    assert banner.get("op") == "sys" and "you" in banner
    return ws, banner


async def _open_ext_ws(ws_url: str, *, tok: dict[str, Any], label: str) -> Any:
    ws = await websockets.connect(ws_url, open_timeout=3.0, close_timeout=1.5)
    await ws.send(json.dumps(_build_ext_ws_hello(label, tok)))
    banner = json.loads(await asyncio.wait_for(ws.recv(), 3.0))
    assert banner.get("op") == "sys" and banner.get("trust") == "external"
    return ws, banner


async def _assert_ws_closed_4003(ws: Any, *, timeout: float = 4.0) -> int:
    """Assert that an already-open WebSocket session is closed by the hub with code `4003`."""
    try:
        while True:
            await asyncio.wait_for(ws.recv(), timeout=timeout)
    except websockets.exceptions.ConnectionClosed as exc:
        code = exc.rcvd.code if exc.rcvd is not None else ws.close_code
        assert code == 4003, f"Expected close code 4003, got {code!r} (exc={exc!r})"
        return int(code)
    raise AssertionError("Expected WebSocket to close with 4003, but it stayed open")


async def _assert_new_ws_rejected_4003(
    ws_url: str,
    hello: dict[str, Any],
    *,
    local_ip: str | None = None,
) -> None:
    kw: dict[str, Any] = {"open_timeout": 3.0, "close_timeout": 1.5}
    if local_ip:
        kw["local_addr"] = (local_ip, 0)
    async with websockets.connect(ws_url, **kw) as ws:
        await ws.send(json.dumps(hello))
        await _assert_ws_closed_4003(ws, timeout=3.0)


@pytest.mark.chaos
async def test_x10a_revoke_allow_removal_sync_and_immediate_4003_session_eviction(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
) -> None:
    """`X10(a)`: Token revoke + direct `tokens.json` deletion + `allow.txt` node removal on
    source Hub A -> replica Hub B syncs next cycle -> revoked/removed sessions on BOTH source &
    replica are immediately closed with `4003`, valid sessions remain open, and new attempts fail."""
    whois_cmd = _write_whois_script(tmp_path / "whois_bin")

    port_a, gw_a = pick_free_port(), pick_free_port()
    port_b, gw_b = pick_free_port(), pick_free_port()

    hub_a = hub_factory.create_hub(
        "hub_a_src",
        port=port_a,
        gateway_port=gw_a,
        allowed=("cat-food", "mother-goose", "z-fold8"),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_HUB_PEERS": "mother-goose",
            "BUS_AUTH_WATCH_INTERVAL": "0.05",
        },
    )
    tok_victim = hub_a.create_token(
        "victim",
        scopes=["send:cat-food/inbox", "poll:ext-victim"],
        allow_exec=False,
    )
    tok_keep = hub_a.create_token(
        "keep",
        scopes=["send:cat-food/inbox", "poll:ext-keep"],
        allow_exec=False,
    )
    tok_to_delete = hub_a.create_token(
        "ephemeral",
        scopes=["send:cat-food/inbox", "poll:ext-ephemeral"],
        allow_exec=False,
    )
    tok_expiring = bus_auth.create_token(
        hub_a.cfg_dir,
        "expiring",
        scopes=["send:cat-food/inbox", "poll:ext-expiring"],
        allow_exec=False,
        expires_at=time.time() + 120.0,
        explicit_auth_source="",
    )
    hub_a.start()

    hub_b = hub_factory.start_hub(
        "hub_b_rep",
        port=port_b,
        gateway_port=gw_b,
        allowed=(),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_AUTH_SOURCE": hub_a.ws_url,
            "BUS_AUTH_SYNC_BIND_HOST": "127.0.0.2",
            "BUS_AUTH_SYNC_INTERVAL": "0.08",
        },
    )

    # Wait until Hub B completes initial pull from Hub A.
    async def _synced_gen(min_gen: int) -> bool:
        ca, ha = await asyncio.to_thread(_http_get_unauth, hub_a.gateway_http_url, "/v1/health")
        cb, hb = await asyncio.to_thread(_http_get_unauth, hub_b.gateway_http_url, "/v1/health")
        return (
            ca == 200
            and cb == 200
            and ha["auth_generation"] >= min_gen
            and hb["auth_generation"] == ha["auth_generation"]
        )

    await _wait_until(lambda: _synced_gen(1), timeout=5.0, desc="initial auth_sync to Hub B")
    _, health_init = await asyncio.to_thread(_http_get_unauth, hub_b.gateway_http_url, "/v1/health")
    gen0 = int(health_init["auth_generation"])
    assert gen0 >= 1

    # Check 0600 permissions on replica generation files (`tokens.json`, `manifest.json`).
    rep_gdir0 = hub_b.cfg_dir / "auth-gen" / str(gen0)
    assert (rep_gdir0 / "tokens.json").stat().st_mode & 0o777 == 0o600
    assert (rep_gdir0 / "manifest.json").stat().st_mode & 0o777 == 0o600

    # Open active WS sessions on BOTH Hub A (source) and Hub B (replica):
    # - Victim external token on Gateway `/bus/v1/ws` (A & B) + internal port WS (B)
    # - Survivor external token on Gateway `/bus/v1/ws` (A & B)
    # - Internal `z-fold8` (`127.0.0.3`, to be removed from allow.txt) on A & B
    # - Internal `cat-food` (`127.0.0.1`, stays allowed) on A & B
    open_sockets: list[Any] = []
    try:
        ws_a_ext_victim, _ = await _open_ext_ws(hub_a.gateway_ws_url, tok=tok_victim, label="vic-a")
        ws_b_ext_victim_gw, _ = await _open_ext_ws(hub_b.gateway_ws_url, tok=tok_victim, label="vic-b-gw")
        ws_b_ext_victim_hub, _ = await _open_ext_ws(hub_b.ws_url, tok=tok_victim, label="vic-b-hub")
        ws_b_ext_deleted, _ = await _open_ext_ws(hub_b.gateway_ws_url, tok=tok_to_delete, label="del-b")
        ws_b_ext_expiring, _ = await _open_ext_ws(hub_b.gateway_ws_url, tok=tok_expiring, label="exp-b")
        ws_a_ext_keep, _ = await _open_ext_ws(hub_a.gateway_ws_url, tok=tok_keep, label="keep-a")
        ws_b_ext_keep, _ = await _open_ext_ws(hub_b.gateway_ws_url, tok=tok_keep, label="keep-b")
        ws_a_int_zfold, b_az = await _open_internal_ws(hub_a.ws_url, source_ip="127.0.0.3", label="zf-a")
        ws_b_int_zfold, b_bz = await _open_internal_ws(hub_b.ws_url, source_ip="127.0.0.3", label="zf-b")
        ws_a_int_cat, _ = await _open_internal_ws(hub_a.ws_url, source_ip="127.0.0.1", label="cf-a")
        ws_b_int_cat, _ = await _open_internal_ws(hub_b.ws_url, source_ip="127.0.0.1", label="cf-b")
        open_sockets.extend(
            [
                ws_a_ext_victim,
                ws_b_ext_victim_gw,
                ws_b_ext_victim_hub,
                ws_b_ext_deleted,
                ws_b_ext_expiring,
                ws_a_ext_keep,
                ws_b_ext_keep,
                ws_a_int_zfold,
                ws_b_int_zfold,
                ws_a_int_cat,
                ws_b_int_cat,
            ]
        )
        assert b_az["you"] == "z-fold8/zf-a"
        assert b_bz["you"] == "z-fold8/zf-b"

        # 1) Revoke `tok_victim` via CLI on Hub A -> generation +1
        cli_env = hub_a.build_env()
        rev_proc = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "agent_bus.cli", "token", "revoke", tok_victim["id"]],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        assert rev_proc.returncode == 0, rev_proc.stderr

        # 2) Directly edit `tokens.json` to delete `tok_to_delete` entirely AND expire `tok_expiring`
        #    (verifies SHA-256 direct-edit detection + cumulative `tombstones` + expired token eviction).
        raw_tok_data = json.loads((hub_a.cfg_dir / "tokens.json").read_text(encoding="utf-8"))
        updated_tokens = []
        for t in raw_tok_data["tokens"]:
            if t.get("id") == tok_to_delete["id"]:
                continue
            if t.get("id") == tok_expiring["id"]:
                t = dict(t, expires_at=time.time() - 5.0)
            updated_tokens.append(t)
        raw_tok_data["tokens"] = updated_tokens
        (hub_a.cfg_dir / "tokens.json").write_text(
            json.dumps(raw_tok_data, indent=2) + "\n",
            encoding="utf-8",
        )
        await _wait_until(
            lambda: _synced_gen(gen0 + 2),
            timeout=5.0,
            desc="token revoke + direct edit generation sync",
        )

        # 3) Directly edit `allow.txt` on Hub A to remove `z-fold8` -> generation +1 again!
        (hub_a.cfg_dir / "allow.txt").write_text("cat-food\nmother-goose\n", encoding="utf-8")
        await _wait_until(
            lambda: _synced_gen(gen0 + 3),
            timeout=5.0,
            desc="allow.txt direct edit generation sync",
        )

        # Verify cumulative tombstones on Hub B include BOTH `tok_victim` and deleted `tok_to_delete`.
        cur_b_gen = bus_auth.read_replica_current_generation(hub_b.cfg_dir)
        assert cur_b_gen >= gen0 + 3
        b_manifest = json.loads(
            (hub_b.cfg_dir / "auth-gen" / str(cur_b_gen) / "manifest.json").read_text(encoding="utf-8")
        )
        assert tok_victim["id"] in b_manifest["tombstones"]
        assert tok_to_delete["id"] in b_manifest["tombstones"]
        assert tok_keep["id"] not in b_manifest["tombstones"]

        # Verify immediate `4003` close on ALL revoked, deleted, expired external & removed internal
        # sessions across BOTH Hub A (source) and Hub B (replica)!
        await asyncio.gather(
            _assert_ws_closed_4003(ws_a_ext_victim),
            _assert_ws_closed_4003(ws_b_ext_victim_gw),
            _assert_ws_closed_4003(ws_b_ext_victim_hub),
            _assert_ws_closed_4003(ws_b_ext_deleted),
            _assert_ws_closed_4003(ws_b_ext_expiring),
            _assert_ws_closed_4003(ws_a_int_zfold),
            _assert_ws_closed_4003(ws_b_int_zfold),
        )

        # Verify valid sessions (`tok_keep` external + `cat-food` internal) stayed alive & functional!
        for alive_ws in (ws_a_ext_keep, ws_b_ext_keep, ws_a_int_cat, ws_b_int_cat):
            await alive_ws.send(json.dumps({"op": "peers"}))
            reply = json.loads(await asyncio.wait_for(alive_ws.recv(), 3.0))
            assert reply["op"] == "sys" and isinstance(reply.get("peers"), list)

        # Verify new HTTP & WS requests for `tok_victim`, `tok_to_delete`, and `z-fold8` are rejected!
        for dead_tok in (tok_victim, tok_to_delete):
            code_http_b, _ = await asyncio.to_thread(
                _http_ext_call, hub_b.gateway_http_url, "/v1/peers", dead_tok
            )
            assert code_http_b == 401
            await _assert_new_ws_rejected_4003(
                hub_b.gateway_ws_url, _build_ext_ws_hello("retry-ext", dead_tok)
            )
        await _assert_new_ws_rejected_4003(
            hub_b.ws_url,
            {"v": 2, "label": "retry-zf", "since": 0, "no_replay": True},
            local_ip="127.0.0.3",
        )

        # Meanwhile `tok_keep` HTTP still succeeds (`200`) on Hub B.
        code_keep, body_keep = await asyncio.to_thread(
            _http_ext_call, hub_b.gateway_http_url, "/v1/peers", tok_keep
        )
        assert code_keep == 200 and body_keep["ok"] is True
    finally:
        for s in open_sockets:
            try:
                await s.close()
            except Exception:
                pass


@pytest.mark.chaos
async def test_x10b_source_sigkill_lkg_then_auth_max_stale_blocks_ext_preserves_internal(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
) -> None:
    """`X10(b)`: Source Hub A permanently killed (`SIGKILL`) -> Hub B keeps LKG before
    `auth_max_stale`, then after `auth_max_stale` rejects `ext` tokens (HTTP `401`, new WS `4003`,
    open `ext` WS evicted with `4003`) while internal `whois` `allow` peers stay fully operational."""
    whois_cmd = _write_whois_script(tmp_path / "whois_bin")
    port_a, gw_a = pick_free_port(), pick_free_port()
    port_b, gw_b = pick_free_port(), pick_free_port()

    hub_a = hub_factory.create_hub(
        "hub_a_src_b",
        port=port_a,
        gateway_port=gw_a,
        allowed=("cat-food", "mother-goose", "z-fold8"),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_HUB_PEERS": "mother-goose",
        },
    )
    tok_ext = hub_a.create_token(
        "mobile",
        scopes=["send:cat-food/inbox", "poll:ext-mobile"],
        allow_exec=False,
    )
    hub_a.start()

    # Short `auth_max_stale=0.45s` for deterministic test transition.
    hub_b = hub_factory.start_hub(
        "hub_b_rep_b",
        port=port_b,
        gateway_port=gw_b,
        allowed=(),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_AUTH_SOURCE": hub_a.ws_url,
            "BUS_AUTH_SYNC_BIND_HOST": "127.0.0.2",
            "BUS_AUTH_SYNC_INTERVAL": "0.10",
            "BUS_AUTH_MAX_STALE": "0.45",
        },
    )

    await _wait_until(
        lambda: bus_auth.read_replica_current_generation(hub_b.cfg_dir) >= 1,
        timeout=5.0,
        desc="Hub B initial sync",
    )

    ws_ext_open: Any = None
    ws_int_zfold: Any = None
    ws_int_cat: Any = None
    try:
        ws_ext_open, _ = await _open_ext_ws(hub_b.gateway_ws_url, tok=tok_ext, label="ext-live")
        ws_int_zfold, _ = await _open_internal_ws(hub_b.ws_url, source_ip="127.0.0.3", label="main")

        # Permanently kill source Hub A (`SIGKILL`) and revoke on disk (unreplicated) to model
        # source loss around a revocation boundary.
        hub_a.sigkill()
        hub_a.revoke_token(tok_ext["id"])

        # Immediately after Hub A dies (before `0.45s` max stale), LKG still serves `tok_ext`.
        code_fresh, _ = await asyncio.to_thread(
            _http_ext_call, hub_b.gateway_http_url, "/v1/peers", tok_ext
        )
        assert code_fresh == 200

        # Wait until `auth_age_s` on Hub B exceeds `0.45s`.
        async def _is_stale_on_health() -> bool:
            c, h = await asyncio.to_thread(_http_get_unauth, hub_b.gateway_http_url, "/v1/health")
            return c == 200 and h.get("auth_age_s") is not None and float(h["auth_age_s"]) > 0.50

        await _wait_until(_is_stale_on_health, timeout=4.0, desc="Hub B auth_age_s > auth_max_stale")

        # 1) Open `ext` WS session must be evicted with `4003` due to staleness!
        await _assert_ws_closed_4003(ws_ext_open, timeout=3.0)

        # 2) New `ext` HTTP requests (`/v1/peers`, `/v1/send`) and new `/bus/v1/ws` are rejected!
        code_stale_peers, body_stale = await asyncio.to_thread(
            _http_ext_call, hub_b.gateway_http_url, "/v1/peers", tok_ext
        )
        assert code_stale_peers == 401
        assert "stale" in str(body_stale.get("error") or "").lower()

        code_stale_send, _ = await asyncio.to_thread(
            _http_ext_call,
            hub_b.gateway_http_url,
            "/v1/send",
            tok_ext,
            method="POST",
            payload={"to": "cat-food/inbox", "kind": "event", "text": "blocked-when-stale"},
        )
        assert code_stale_send == 401

        await _assert_new_ws_rejected_4003(
            hub_b.gateway_ws_url, _build_ext_ws_hello("ext-stale-new", tok_ext)
        )

        # 3) Meanwhile internal `whois` `allow` peers (`z-fold8` & `cat-food`) remain 100% allowed
        #    and keep routing messages over Hub B using the LKG `allow.txt`!
        ws_int_cat, _ = await _open_internal_ws(hub_b.ws_url, source_ip="127.0.0.1", label="cli-tx")
        await ws_int_cat.send(
            json.dumps(
                {
                    "v": 2,
                    "op": "send",
                    "id": "x10b-int-msg-1",
                    "kind": "event",
                    "to": "z-fold8/main",
                    "text": "internal-lkg-survives-stale",
                }
            )
        )
        ack_frame = json.loads(await asyncio.wait_for(ws_int_cat.recv(), 3.0))
        assert ack_frame.get("ack") == "x10b-int-msg-1"

        delivered_msg = json.loads(await asyncio.wait_for(ws_int_zfold.recv(), 3.0))
        assert delivered_msg["id"] == "x10b-int-msg-1"
        assert delivered_msg["text"] == "internal-lkg-survives-stale"
    finally:
        for s in (ws_ext_open, ws_int_zfold, ws_int_cat):
            if s is not None:
                try:
                    await s.close()
                except Exception:
                    pass


@pytest.mark.chaos
async def test_x10c_crash_before_pointer_swap_no_generation_mixing_and_clean_recovery(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    hub_factory: HubController,
) -> None:
    """`X10(c)`: Fault/crash after writing `auth-gen/<gen>/` right before `auth-current` atomic
    pointer swap (both in-process exception hook AND real subprocess `os._exit(91)` crash),
    plus initial fail-closed, corrupted `sha256` rejection, and `gen <= current` downgrade guard."""
    whois_cmd = _write_whois_script(tmp_path / "whois_bin")
    monkeypatch.setenv("BUS_TEST_ALLOW_LOCALHOST", "0")
    monkeypatch.setenv("BUS_WHOIS_CMD", str(whois_cmd))
    bus_auth.clear_whois_cache()

    # --- Part A: In-process fail-closed before first sync + crash hook + downgrade/hash guards ---
    src_cfg = tmp_path / "inproc_src" / "cfg"
    src_data = tmp_path / "inproc_src" / "data"
    src_logs = tmp_path / "inproc_src" / "logs"
    rep_cfg = tmp_path / "inproc_rep" / "cfg"
    rep_data = tmp_path / "inproc_rep" / "data"
    rep_logs = tmp_path / "inproc_rep" / "logs"
    for d in (src_cfg, src_data, src_logs, rep_cfg, rep_data, rep_logs):
        d.mkdir(parents=True, exist_ok=True)

    # Plant a decoy root `allow.txt` in `rep_cfg` BEFORE initial sync to prove replica mode
    # strictly ignores root `allow.txt` and fails closed when `auth-current` is missing!
    (rep_cfg / "allow.txt").write_text("cat-food\nmother-goose\nz-fold8\n", encoding="utf-8")

    (src_cfg / "allow.txt").write_text("cat-food\nmother-goose\nz-fold8\n", encoding="utf-8")
    tok_v1 = bus_auth.create_token(
        src_cfg,
        "v1client",
        scopes=["send:cat-food/inbox", "poll:ext-v1client"],
        explicit_auth_source="",
    )

    src_port = pick_free_port()
    rep_port, rep_gw_port = pick_free_port(), pick_free_port()

    h_src = bus_hub.BusHub(
        config_dir=src_cfg,
        database_path=src_data / "bus.db",
        logs_dir=src_logs,
        auth_source="",
        hub_peers=["mother-goose"],
    )
    src_ws_srv = await websockets.serve(
        lambda ws: bus_hub._websockets_adapter(ws, h_src), "127.0.0.1", src_port
    )
    src_url = f"ws://127.0.0.1:{src_port}"
    (rep_cfg / "config.json").write_text(
        json.dumps({"auth_source": src_url}),
        encoding="utf-8",
    )

    h_rep = bus_hub.BusHub(
        config_dir=rep_cfg,
        database_path=rep_data / "bus.db",
        logs_dir=rep_logs,
        auth_source=src_url,
        bind_host="127.0.0.2",
    )
    rep_ws_srv = await websockets.serve(
        lambda ws: bus_hub._websockets_adapter(ws, h_rep), "127.0.0.1", rep_port
    )
    rep_gw_srv = await bus_gateway.start_gateway_server(h_rep, "127.0.0.1", rep_gw_port)
    rep_ws_url = f"ws://127.0.0.1:{rep_port}"
    rep_gw_http = f"http://127.0.0.1:{rep_gw_port}/bus"

    try:
        # 1) Before first sync (`auth-current` missing): replica MUST fail closed (`gen == 0`)
        #    even though a decoy `rep_cfg/allow.txt` exists!
        assert h_rep.auth_generation == 0
        assert bus_auth.allowed_nodes(rep_cfg) == set()
        assert bus_auth.load_tokens(rep_cfg) == {}
        await _assert_new_ws_rejected_4003(
            rep_ws_url,
            {"v": 2, "label": "pre-sync", "since": 0, "no_replay": True},
            local_ip="127.0.0.1",
        )

        # 2) Initial sync -> generation `gen1` active (`z-fold8` + `tok_v1` allowed).
        r1 = await h_rep.sync_auth_once()
        gen1 = r1.generation
        assert gen1 >= 1 and r1.updated is True
        code_v1_ok, _ = await asyncio.to_thread(_http_ext_call, rep_gw_http, "/v1/peers", tok_v1)
        assert code_v1_ok == 200

        # 3) Advance source to a new generation: revoke `tok_v1`, create `tok_v2`, replace
        #    `z-fold8` with `new-peer` (`127.0.0.5`) in `allow.txt`.
        assert bus_auth.revoke_token(src_cfg, tok_v1["id"], explicit_auth_source="") is True
        tok_v2 = bus_auth.create_token(
            src_cfg,
            "v2client",
            scopes=["send:cat-food/inbox", "poll:ext-v2client"],
            explicit_auth_source="",
        )
        bus_auth.write_allow_nodes(
            src_cfg,
            ["cat-food", "mother-goose", "new-peer"],
            explicit_auth_source="",
        )
        target_gen = h_src.health_snapshot()["auth_generation"]
        assert target_gen > gen1

        # 4) Inject crash hook right after staging `auth-gen/<target_gen>/` files and right
        #    BEFORE swapping `auth-current`!
        staged_seen: list[int] = []

        def _crash_before_swap(staged_gen: int, cdir: pathlib.Path) -> None:
            gdir = cdir / "auth-gen" / str(staged_gen)
            assert (gdir / "allow.txt").is_file()
            assert (gdir / "tokens.json").is_file()
            assert (gdir / "manifest.json").is_file()
            staged_seen.append(staged_gen)
            raise RuntimeError("simulated power-loss before auth-current os.replace")

        bus_auth.set_auth_pointer_crash_hook(_crash_before_swap)
        with pytest.raises(RuntimeError, match="simulated power-loss"):
            await h_rep.sync_auth_once()
        bus_auth.set_auth_pointer_crash_hook(None)

        assert staged_seen == [target_gen]
        # `auth-gen/<target_gen>` exists on disk, YET `auth-current` MUST still point to `gen1`!
        assert (rep_cfg / "auth-gen" / str(target_gen) / "manifest.json").is_file()
        assert bus_auth.read_replica_current_generation(rep_cfg) == gen1
        assert h_rep.auth_generation == gen1

        # Verify ZERO generation mixing: replica behaves 100% as `gen1`:
        # - `tok_v1` and `z-fold8` still work;
        # - `tok_v2` and `new-peer` (`127.0.0.5`) are NOT prematurely accepted!
        c_v1_still, _ = await asyncio.to_thread(_http_ext_call, rep_gw_http, "/v1/peers", tok_v1)
        assert c_v1_still == 200
        c_v2_not_yet, _ = await asyncio.to_thread(_http_ext_call, rep_gw_http, "/v1/peers", tok_v2)
        assert c_v2_not_yet == 401
        ws_zf, _ = await _open_internal_ws(rep_ws_url, source_ip="127.0.0.3", label="still-v1")
        await ws_zf.close()
        await _assert_new_ws_rejected_4003(
            rep_ws_url,
            {"v": 2, "label": "not-yet", "since": 0, "no_replay": True},
            local_ip="127.0.0.5",
        )

        # 5) Re-sync without crash hook -> cleanly transitions to `target_gen`!
        r2 = await h_rep.sync_auth_once()
        assert r2.generation == target_gen and r2.updated is True
        assert bus_auth.read_replica_current_generation(rep_cfg) == target_gen

        c_v1_now, _ = await asyncio.to_thread(_http_ext_call, rep_gw_http, "/v1/peers", tok_v1)
        c_v2_now, _ = await asyncio.to_thread(_http_ext_call, rep_gw_http, "/v1/peers", tok_v2)
        assert c_v1_now == 401
        assert c_v2_now == 200
        ws_np, b_np = await _open_internal_ws(rep_ws_url, source_ip="127.0.0.5", label="now-v2")
        assert b_np["you"] == "new-peer/now-v2"
        await ws_np.close()

        # 6) Verify corrupted `sha256` manifest AND downgrade (`generation < target_gen`) are rejected!
        good_m = bus_auth.build_auth_sync_manifest(src_cfg, explicit_auth_source="")
        tampered_m = dict(good_m, allow=["cat-food"])  # sha256 no longer matches!
        with pytest.raises(bus_auth.AuthSyncError, match="sha256 mismatch"):
            bus_auth.apply_replica_manifest(rep_cfg, tampered_m)

        downgrade_gen = 1
        downgrade_sha = bus_auth.compute_manifest_sha256(
            generation=downgrade_gen,
            allow=["cat-food"],
            tokens=[],
            tombstones=[],
        )
        downgrade_m = {
            "generation": downgrade_gen,
            "sha256": downgrade_sha,
            "allow": ["cat-food"],
            "tokens": [],
            "tombstones": [],
            "issued_at": time.time(),
        }
        with pytest.raises(bus_auth.AuthSyncError, match="downgrade"):
            bus_auth.apply_replica_manifest(rep_cfg, downgrade_m)
        assert bus_auth.read_replica_current_generation(rep_cfg) == target_gen
    finally:
        bus_auth.set_auth_pointer_crash_hook(None)
        rep_gw_srv.close()
        await rep_gw_srv.wait_closed()
        rep_ws_srv.close()
        await rep_ws_srv.wait_closed()
        src_ws_srv.close()
        await src_ws_srv.wait_closed()
        h_rep.close()
        h_src.close()

    # --- Part B: Real subprocess `os._exit(91)` crash before pointer swap + restart recovery ---
    sp_a_port, sp_a_gw = pick_free_port(), pick_free_port()
    sp_b_port, sp_b_gw = pick_free_port(), pick_free_port()
    crash_marker = tmp_path / "subproc_pointer_crash.marker"

    sub_a = hub_factory.start_hub(
        "hub_a_subcrash",
        port=sp_a_port,
        gateway_port=sp_a_gw,
        allowed=("cat-food", "mother-goose"),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_HUB_PEERS": "mother-goose",
        },
    )
    sub_b = hub_factory.start_hub(
        "hub_b_subcrash",
        port=sp_b_port,
        gateway_port=sp_b_gw,
        allowed=(),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_AUTH_SOURCE": sub_a.ws_url,
            "BUS_AUTH_SYNC_BIND_HOST": "127.0.0.2",
            "BUS_AUTH_SYNC_INTERVAL": "0.08",
        },
    )
    await _wait_until(
        lambda: bus_auth.read_replica_current_generation(sub_b.cfg_dir) == 1,
        timeout=5.0,
        desc="subprocess Hub B gen 1 sync",
    )

    # Restart Hub B armed with `exit_once:<marker>`, then bump Hub A to generation 2 -> Hub B
    # writes `auth-gen/2/` and abruptly exits with code `91` BEFORE updating `auth-current`!
    sub_b.stop()
    sub_b.extra_env["BUS_TEST_CRASH_BEFORE_AUTH_POINTER"] = f"exit_once:{crash_marker}"
    sub_b.start()
    tok_after = sub_a.create_token("aftercrash")

    await _wait_until(lambda: crash_marker.exists(), timeout=5.0, desc="crash marker written")
    await _wait_until(lambda: not sub_b.is_running(), timeout=5.0, desc="Hub B exited with 91")
    assert sub_b.proc is not None and sub_b.proc.returncode == 91
    assert (sub_b.cfg_dir / "auth-gen" / "2" / "manifest.json").is_file()
    assert bus_auth.read_replica_current_generation(sub_b.cfg_dir) == 1

    # Restart Hub B cleanly -> marker already exists so sync completes and transitions to gen 2!
    sub_b.stop()
    sub_b.start()
    await _wait_until(
        lambda: bus_auth.read_replica_current_generation(sub_b.cfg_dir) >= 2,
        timeout=5.0,
        desc="Hub B recovered to generation 2 after restart",
    )
    code_after, _ = await asyncio.to_thread(
        _http_ext_call, sub_b.gateway_http_url, "/v1/peers", tok_after
    )
    assert code_after == 200


@pytest.mark.chaos
async def test_x10d_unauthorized_auth_sync_forbidden_no_secret_leak_and_replica_readonly_guard(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
) -> None:
    """`X10(d)`: Internal nodes not in `hub_peers`, external `ext` tokens, and replica hubs all
    reject `op: 'auth_sync'` with `error == 'forbidden'` and leak zero secrets; replica `cfg_dir`
    rejects `abus token create/revoke` CLI and Python edit APIs."""
    whois_cmd = _write_whois_script(tmp_path / "whois_bin")
    port_a, gw_a = pick_free_port(), pick_free_port()
    port_b, gw_b = pick_free_port(), pick_free_port()

    hub_a = hub_factory.create_hub(
        "hub_a_d",
        port=port_a,
        gateway_port=gw_a,
        allowed=("cat-food", "mother-goose", "z-fold8"),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_HUB_PEERS": "mother-goose",
        },
    )
    tok_secret_holder = hub_a.create_token(
        "topsecret",
        scopes=["send:cat-food/inbox", "poll:ext-topsecret"],
        allow_exec=False,
    )
    sensitive_secret = tok_secret_holder["secret"]
    sensitive_id = tok_secret_holder["id"]
    hub_a.start()

    hub_b = hub_factory.start_hub(
        "hub_b_d",
        port=port_b,
        gateway_port=gw_b,
        allowed=(),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_AUTH_SOURCE": hub_a.ws_url,
            "BUS_AUTH_SYNC_BIND_HOST": "127.0.0.2",
            "BUS_AUTH_SYNC_INTERVAL": "0.10",
        },
    )
    await _wait_until(
        lambda: bus_auth.read_replica_current_generation(hub_b.cfg_dir) >= 1,
        timeout=5.0,
        desc="Hub B initial sync",
    )

    def _assert_forbidden_no_leak(resp: dict[str, Any]) -> None:
        raw_dump = json.dumps(resp)
        assert resp.get("ok") is False
        assert resp.get("error") == "forbidden"
        assert sensitive_secret not in raw_dump
        assert sensitive_id not in raw_dump
        for key in ("tokens", "allow", "tombstones", "sha256"):
            assert key not in resp

    # 1) Internal node `z-fold8` (`127.0.0.3`: in `allow.txt`, NOT in `hub_peers`) -> forbidden!
    ws_zf, _ = await _open_internal_ws(hub_a.ws_url, source_ip="127.0.0.3", label="probe-sync")
    try:
        await ws_zf.send(json.dumps({"op": "auth_sync"}))
        resp_zf = json.loads(await asyncio.wait_for(ws_zf.recv(), 3.0))
        _assert_forbidden_no_leak(resp_zf)
    finally:
        await ws_zf.close()

    # 2) Internal node `cat-food` (`127.0.0.1`: also NOT in `hub_peers=["mother-goose"]`) -> forbidden!
    ws_cf, _ = await _open_internal_ws(hub_a.ws_url, source_ip="127.0.0.1", label="probe-cf")
    try:
        await ws_cf.send(json.dumps({"op": "auth_sync"}))
        resp_cf = json.loads(await asyncio.wait_for(ws_cf.recv(), 3.0))
        _assert_forbidden_no_leak(resp_cf)
    finally:
        await ws_cf.close()

    # 3) External `ext` token session (both Gateway `/bus/v1/ws` and Hub WS) -> forbidden!
    for ext_url in (hub_a.gateway_ws_url, hub_a.ws_url):
        ws_ext, _ = await _open_ext_ws(ext_url, tok=tok_secret_holder, label="probe-ext")
        try:
            await ws_ext.send(json.dumps({"op": "auth_sync"}))
            resp_ext = json.loads(await asyncio.wait_for(ws_ext.recv(), 3.0))
            _assert_forbidden_no_leak(resp_ext)
        finally:
            await ws_ext.close()

    # 4) Replica Hub B itself MUST reject `op: 'auth_sync'` even from `mother-goose` (`127.0.0.2`)!
    ws_on_rep, _ = await _open_internal_ws(hub_b.ws_url, source_ip="127.0.0.2", label="probe-rep")
    try:
        await ws_on_rep.send(json.dumps({"op": "auth_sync"}))
        resp_rep = json.loads(await asyncio.wait_for(ws_on_rep.recv(), 3.0))
        _assert_forbidden_no_leak(resp_rep)
    finally:
        await ws_on_rep.close()

    # 5) Authorized `hub_peers` member `mother-goose` (`127.0.0.2`) on Source Hub A DOES succeed!
    ws_mg, _ = await _open_internal_ws(hub_a.ws_url, source_ip="127.0.0.2", label="valid-peer")
    try:
        await ws_mg.send(json.dumps({"op": "auth_sync"}))
        resp_mg = json.loads(await asyncio.wait_for(ws_mg.recv(), 3.0))
        assert resp_mg["ok"] is True
        assert set(resp_mg.keys()) >= {
            "op",
            "ok",
            "generation",
            "sha256",
            "allow",
            "tokens",
            "tombstones",
            "issued_at",
        }
    finally:
        await ws_mg.close()

    # 6) Replica `cfg_dir` single-writer guard: CLI `abus token create` & `abus token revoke`
    #    and Python mutation APIs MUST fail with clear error!
    rep_env = hub_b.build_env()
    for bad_cmd in (
        [sys.executable, "-m", "agent_bus.cli", "token", "create", "--node", "illegal"],
        [sys.executable, "-m", "agent_bus.cli", "token", "revoke", sensitive_id],
    ):
        proc = await asyncio.to_thread(
            subprocess.run,
            bad_cmd,
            env=rep_env,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        assert proc.returncode != 0
        assert "replica mode" in proc.stderr.lower()
    assert not (hub_b.cfg_dir / "tokens.json").exists()

    # Also test via `config.json` `"auth_source"` without env var
    rep_cfg_only = tmp_path / "rep_cfg_json_only"
    rep_cfg_only.mkdir(parents=True, exist_ok=True)
    (rep_cfg_only / "config.json").write_text(
        json.dumps({"auth_source": hub_a.ws_url}),
        encoding="utf-8",
    )
    with pytest.raises(bus_auth.ReplicaReadOnlyError):
        bus_auth.create_token(rep_cfg_only, "forbidden-create")
    with pytest.raises(bus_auth.ReplicaReadOnlyError):
        bus_auth.revoke_token(rep_cfg_only, sensitive_id)
    with pytest.raises(bus_auth.ReplicaReadOnlyError):
        bus_auth.write_allow_nodes(rep_cfg_only, ["cat-food"])


@pytest.mark.chaos
async def test_x10e_default_constants_source_compat_and_ws_http_health_fields(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
) -> None:
    """`X10(e)`: Verify default `60s` sync / `86400s` max-stale constants, initial unsynced replica
    health (`auth_mode='replica'`, `auth_generation=0`, `auth_age_s=None`), synced WS + HTTP health
    fields on both source & replica, and unchanged source-hub behavior."""
    assert bus_config.DEFAULT_AUTH_SYNC_INTERVAL == 60.0
    assert bus_config.DEFAULT_AUTH_MAX_STALE == 86400.0
    assert bus_config.get_auth_sync_interval() == 60.0
    assert bus_config.get_auth_max_stale() == 86400.0

    # Verify unsynced initial replica `BusHub` health fields before any pull succeeds.
    unsynced_cfg = tmp_path / "unsynced_rep" / "cfg"
    unsynced_data = tmp_path / "unsynced_rep" / "data"
    unsynced_logs = tmp_path / "unsynced_rep" / "logs"
    h_unsynced = bus_hub.BusHub(
        config_dir=unsynced_cfg,
        database_path=unsynced_data / "bus.db",
        logs_dir=unsynced_logs,
        auth_source="ws://127.0.0.1:19999",
    )
    try:
        assert h_unsynced.auth_sync_interval == 60.0
        assert h_unsynced.auth_max_stale == 86400.0
        snap0 = h_unsynced.health_snapshot()
        assert snap0["auth_mode"] == "replica"
        assert snap0["auth_generation"] == 0
        assert snap0["auth_age_s"] is None
        assert snap0["auth"] == {"mode": "replica", "generation": 0, "age_s": None}
    finally:
        h_unsynced.close()

    # Live source + replica health check over BOTH unauthenticated `GET /v1/health` and WS `op: 'health'`.
    whois_cmd = _write_whois_script(tmp_path / "whois_bin")
    port_a, gw_a = pick_free_port(), pick_free_port()
    port_b, gw_b = pick_free_port(), pick_free_port()

    hub_a = hub_factory.create_hub(
        "hub_a_e",
        port=port_a,
        gateway_port=gw_a,
        allowed=("cat-food", "mother-goose", "z-fold8"),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_HUB_PEERS": "mother-goose",
        },
    )
    tok_a = hub_a.create_token("healthcheck")
    hub_a.start()

    hub_b = hub_factory.start_hub(
        "hub_b_e",
        port=port_b,
        gateway_port=gw_b,
        allowed=(),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_AUTH_SOURCE": hub_a.ws_url,
            "BUS_AUTH_SYNC_BIND_HOST": "127.0.0.2",
            "BUS_AUTH_SYNC_INTERVAL": "0.10",
        },
    )

    await _wait_until(
        lambda: bus_auth.read_replica_current_generation(hub_b.cfg_dir) >= 1,
        timeout=5.0,
        desc="Hub B synced with Hub A",
    )

    ca, ha_http = await asyncio.to_thread(_http_get_unauth, hub_a.gateway_http_url, "/v1/health")
    cb, hb_http = await asyncio.to_thread(_http_get_unauth, hub_b.gateway_http_url, "/v1/health")
    assert ca == 200 and cb == 200

    assert ha_http["auth_mode"] == "source"
    assert hb_http["auth_mode"] == "replica"
    assert isinstance(ha_http["auth_generation"], int) and ha_http["auth_generation"] >= 1
    assert hb_http["auth_generation"] == ha_http["auth_generation"]
    assert isinstance(ha_http["auth_age_s"], (int, float)) and 0.0 <= float(ha_http["auth_age_s"]) < 10.0
    assert isinstance(hb_http["auth_age_s"], (int, float)) and 0.0 <= float(hb_http["auth_age_s"]) < 10.0

    for h_doc in (ha_http, hb_http):
        for secret_key in ("tokens", "secret", "allow", "tombstones", "peers"):
            assert secret_key not in h_doc
        assert tok_a["secret"] not in json.dumps(h_doc)

    # Check WS `op: 'health'` on both hubs matches HTTP `/v1/health`.
    for target_hub, expected_mode in ((hub_a, "source"), (hub_b, "replica")):
        ws, _ = await _open_internal_ws(target_hub.ws_url, source_ip="127.0.0.1", label="ws-hlth")
        try:
            await ws.send(json.dumps({"op": "health"}))
            ws_h = json.loads(await asyncio.wait_for(ws.recv(), 3.0))
            assert ws_h["op"] == "health"
            assert ws_h["auth_mode"] == expected_mode
            assert ws_h["auth_generation"] == ha_http["auth_generation"]
            assert isinstance(ws_h["auth_age_s"], (int, float)) and float(ws_h["auth_age_s"]) >= 0.0
        finally:
            await ws.close()


@pytest.mark.chaos
async def test_r12_revoked_token_rejected_on_both_hub_a_and_hub_b_after_sync(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
) -> None:
    """`R12` acceptance test (resolving lead/Sol counterexample `R12_revoke_stale: {A: False, B: True}`):
    Issue token on Hub A -> replicate to Hub B -> verify accepted on BOTH -> revoke on Hub A ->
    after sync, BOTH Hub A and Hub B reject the token across HTTP & WS (`{A: False, B: False}`)
    and immediately evict already-open sessions with `4003`."""
    whois_cmd = _write_whois_script(tmp_path / "whois_bin")
    port_a, gw_a = pick_free_port(), pick_free_port()
    port_b, gw_b = pick_free_port(), pick_free_port()

    hub_a = hub_factory.create_hub(
        "hub_a_r12",
        port=port_a,
        gateway_port=gw_a,
        allowed=("cat-food", "mother-goose"),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_HUB_PEERS": "mother-goose",
            "BUS_AUTH_WATCH_INTERVAL": "0.05",
        },
    )
    tok_r12 = hub_a.create_token(
        "r12client",
        scopes=["send:cat-food/inbox", "poll:ext-r12client"],
        allow_exec=False,
    )
    hub_a.start()

    hub_b = hub_factory.start_hub(
        "hub_b_r12",
        port=port_b,
        gateway_port=gw_b,
        allowed=(),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_AUTH_SOURCE": hub_a.ws_url,
            "BUS_AUTH_SYNC_BIND_HOST": "127.0.0.2",
            "BUS_AUTH_SYNC_INTERVAL": "0.08",
        },
    )

    await _wait_until(
        lambda: bus_auth.read_replica_current_generation(hub_b.cfg_dir) >= 1,
        timeout=5.0,
        desc="R12 initial sync",
    )
    gen_before = bus_auth.read_replica_current_generation(hub_b.cfg_dir)

    ws_a, _ = await _open_ext_ws(hub_a.gateway_ws_url, tok=tok_r12, label="r12-a")
    ws_b, _ = await _open_ext_ws(hub_b.gateway_ws_url, tok=tok_r12, label="r12-b")
    try:
        # Before revoke: BOTH Hub A and Hub B accept `tok_r12` (`{A: True, B: True}`).
        ca_pre, _ = await asyncio.to_thread(
            _http_ext_call,
            hub_a.gateway_http_url,
            "/v1/send",
            tok_r12,
            method="POST",
            payload={"to": "cat-food/inbox", "kind": "event", "text": "r12-pre-a"},
        )
        cb_pre, _ = await asyncio.to_thread(
            _http_ext_call,
            hub_b.gateway_http_url,
            "/v1/send",
            tok_r12,
            method="POST",
            payload={"to": "cat-food/inbox", "kind": "event", "text": "r12-pre-b"},
        )
        assert {"A": ca_pre == 200, "B": cb_pre == 200} == {"A": True, "B": True}

        # Revoke token on Hub A and wait for replica Hub B to sync the next generation.
        assert hub_a.revoke_token(tok_r12["id"]) is True
        await _wait_until(
            lambda: bus_auth.read_replica_current_generation(hub_b.cfg_dir) > gen_before,
            timeout=5.0,
            desc="R12 revocation synced to Hub B",
        )

        # Open WS sessions on BOTH Hub A and Hub B must immediately close with `4003`!
        await asyncio.gather(
            _assert_ws_closed_4003(ws_a),
            _assert_ws_closed_4003(ws_b),
        )

        # After revoke + sync: BOTH Hub A and Hub B reject HTTP (`/v1/send`, `/v1/peers`) AND WS!
        ca_post, _ = await asyncio.to_thread(
            _http_ext_call,
            hub_a.gateway_http_url,
            "/v1/send",
            tok_r12,
            method="POST",
            payload={"to": "cat-food/inbox", "kind": "event", "text": "r12-post-a"},
        )
        cb_post, _ = await asyncio.to_thread(
            _http_ext_call,
            hub_b.gateway_http_url,
            "/v1/send",
            tok_r12,
            method="POST",
            payload={"to": "cat-food/inbox", "kind": "event", "text": "r12-post-b"},
        )
        r12_post_acceptance = {"A": ca_post == 200, "B": cb_post == 200}
        assert r12_post_acceptance == {"A": False, "B": False}
        assert ca_post == 401 and cb_post == 401

        await _assert_new_ws_rejected_4003(
            hub_a.gateway_ws_url, _build_ext_ws_hello("r12-retry-a", tok_r12)
        )
        await _assert_new_ws_rejected_4003(
            hub_b.gateway_ws_url, _build_ext_ws_hello("r12-retry-b", tok_r12)
        )
    finally:
        for s in (ws_a, ws_b):
            try:
                await s.close()
            except Exception:
                pass
