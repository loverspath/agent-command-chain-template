"""Pytest fixtures and safety guards for `agent_bus` deterministic chaos tests."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import pathlib
import sys
from typing import Any, Iterator

import pytest

from .hub_proc import BUS_ROOT, HubController, write_claude_guard_script
from .oracle import ChaosOracle
from .tcp_proxy import ProxyController

# Guarantee worktree `/home/rerun/abus-wt/bus` takes precedence over everything else.
if sys.path[:1] != [str(BUS_ROOT)]:
    if str(BUS_ROOT) in sys.path:
        sys.path.remove(str(BUS_ROOT))
    sys.path.insert(0, str(BUS_ROOT))

from agent_bus import auth as bus_auth  # noqa: E402
from agent_bus import client as bus_client  # noqa: E402


def _blocked_ask_claude(*args: Any, **kwargs: Any) -> tuple[str, str]:
    raise RuntimeError(
        "Unstubbed agent_bus.client.ask_claude invocation blocked by chaos harness! "
        "Request the `oracle` fixture to install the deterministic ClaudeStub."
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "chaos: deterministic chaos test for agent_bus resilience v2")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Run `async def test_*` functions natively on the per-test `chaos_loop`."""
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    funcargs = pyfuncitem.funcargs
    testargs = {arg: funcargs[arg] for arg in pyfuncitem._fixtureinfo.argnames}
    loop: asyncio.AbstractEventLoop = funcargs["chaos_loop"]
    loop.run_until_complete(pyfuncitem.obj(**testargs))
    return True


@pytest.fixture(autouse=True)
def chaos_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Per-test asyncio event loop torn down AFTER all dependent async fixtures close."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        if not loop.is_closed():
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            asyncio.set_event_loop(None)
            loop.close()


@pytest.fixture(autouse=True)
def sandbox_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, pathlib.Path]]:
    """Strictly isolate client HOME/BUS_* paths and double-block real `claude` execution."""
    client_base = tmp_path / "client"
    client_home = client_base / "home"
    client_cfg = client_base / "cfg"
    client_data = client_base / "data"
    client_logs = client_base / "logs"
    guard_dir = client_base / "_guard_bin"
    for d in (client_home, client_cfg, client_data, client_logs, guard_dir):
        d.mkdir(parents=True, exist_ok=True)

    guard_claude = write_claude_guard_script(guard_dir)
    orig_path = os.environ.get("PATH", "/usr/bin:/bin")

    monkeypatch.setenv("HOME", str(client_home))
    monkeypatch.setenv("PATH", f"{guard_dir}{os.pathsep}{orig_path}")
    monkeypatch.setenv("PYTHONPATH", str(BUS_ROOT))
    monkeypatch.setenv("PYTHONUSERBASE", "/home/rerun/.local")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("BUS_CONFIG_DIR", str(client_cfg))
    monkeypatch.setenv("BUS_DATA_DIR", str(client_data))
    monkeypatch.setenv("BUS_DB_PATH", str(client_data / "bus.db"))
    monkeypatch.setenv("BUS_LOG_DIR", str(client_logs))
    monkeypatch.setenv("BUS_MODE_FILE", str(client_cfg / "mode"))
    monkeypatch.setenv("BUS_TEST_ALLOW_LOCALHOST", "1")
    monkeypatch.setenv("BUS_TEST_LOCAL_NODE", "cat-food")
    monkeypatch.setenv("BUS_CLAUDE_CMD", str(guard_claude))
    monkeypatch.delenv("BUS_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("BUS_AUTH_SECRET", raising=False)
    monkeypatch.delenv("BUS_URL", raising=False)
    monkeypatch.delenv("BUS_URLS", raising=False)
    monkeypatch.delenv("BUS_AUTH_SOURCE", raising=False)
    monkeypatch.delenv("BUS_HUB_PEERS", raising=False)
    monkeypatch.delenv("BUS_AUTH_SYNC_INTERVAL", raising=False)
    monkeypatch.delenv("BUS_AUTH_MAX_STALE", raising=False)
    monkeypatch.delenv("BUS_TEST_CRASH_BEFORE_AUTH_POINTER", raising=False)

    # Layer 1 in-process guard: fail closed if `oracle` fixture was not installed.
    monkeypatch.setattr(bus_client, "ask_claude", _blocked_ask_claude)
    monkeypatch.setattr(bus_client, "update_repo", lambda: False)
    bus_auth.clear_whois_cache()
    bus_auth.set_auth_pointer_crash_hook(None)

    try:
        yield {
            "base": client_base,
            "home": client_home,
            "cfg": client_cfg,
            "data": client_data,
            "logs": client_logs,
            "guard_claude": guard_claude,
        }
    finally:
        bus_auth.set_auth_pointer_crash_hook(None)
        bus_auth.clear_whois_cache()


@pytest.fixture
def hub_factory(tmp_path: pathlib.Path, sandbox_env: dict[str, pathlib.Path]) -> Iterator[HubController]:
    """Provide a `HubController` that spawns isolated hubs (18765/18775) and cleans up PIDs."""
    ctrl = HubController(tmp_path / "hubs", default_local_node="cat-food")
    try:
        yield ctrl
    finally:
        ctrl.close_all()


@pytest.fixture
def proxy_factory(chaos_loop: asyncio.AbstractEventLoop) -> Iterator[ProxyController]:
    """Provide a `ProxyController` for `127.0.0.1:18785` family TCP fault proxies."""
    ctrl = ProxyController(chaos_loop)
    try:
        yield ctrl
    finally:
        ctrl.close_all()


@pytest.fixture
def oracle(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    chaos_loop: asyncio.AbstractEventLoop,
    sandbox_env: dict[str, pathlib.Path],
) -> Iterator[ChaosOracle]:
    """Provide `ChaosOracle` (`ledger`, `counter`, `stub`) and install `ask_claude` stub."""
    ora = ChaosOracle(tmp_path / "oracle", chaos_loop)
    ora.install(monkeypatch)
    try:
        yield ora
    finally:
        ora.close()


class NodeRunner:
    """Launches in-process `agent_bus.client.run_node` tasks and cancels them cleanly on teardown."""

    def __init__(self, loop: asyncio.AbstractEventLoop, default_cwd: pathlib.Path) -> None:
        self.loop = loop
        self.default_cwd = default_cwd
        self._tasks: list[asyncio.Task[None]] = []

    def start(
        self,
        *,
        url: str | None = None,
        label: str = "main",
        mode: str = "hybrid",
        peers: str = "cat-food",
        cwd: pathlib.Path | None = None,
        cfg_dir_override: pathlib.Path | None = None,
        dial_timeout: float | None = None,
        hello_timeout: float | None = None,
        banner_timeout: float | None = None,
        ack_timeout: float | None = None,
    ) -> asyncio.Task[None]:
        if cfg_dir_override is not None:
            cfg_dir_override.mkdir(parents=True, exist_ok=True)
        args = argparse.Namespace(
            cmd="run",
            label=label,
            mode=mode,
            peers=peers,
            url=url,
            cwd=str(cwd or self.default_cwd),
            config_dir=str(cfg_dir_override) if cfg_dir_override is not None else None,
            dial_timeout=dial_timeout,
            hello_timeout=hello_timeout,
            banner_timeout=banner_timeout,
            ack_timeout=ack_timeout,
            no_auto_update=True,
        )

        async def _entry() -> None:
            await bus_client.run_node(args)

        task = self.loop.create_task(_entry())
        self._tasks.append(task)
        return task

    async def stop_task(self, task: asyncio.Task[None]) -> None:
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def close_all(self) -> None:
        if not self._tasks:
            return
        if not self.loop.is_closed():
            for t in reversed(self._tasks):
                if not t.done():
                    t.cancel()
            self.loop.run_until_complete(asyncio.gather(*self._tasks, return_exceptions=True))
        self._tasks.clear()


@pytest.fixture
def node_runner(
    chaos_loop: asyncio.AbstractEventLoop,
    sandbox_env: dict[str, pathlib.Path],
) -> Iterator[NodeRunner]:
    runner = NodeRunner(chaos_loop, sandbox_env["home"])
    try:
        yield runner
    finally:
        runner.close_all()
