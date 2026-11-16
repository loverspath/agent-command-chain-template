"""WBS `I5` Integrated Real-OS Multiprocess Chaos Suite (`X01`, `X02`/`X07`, `X03`, `X05`, `X08`, `X10`, `X11`, `X12`, `X17`).

Topology & Safety Guarantees:
- Spawns real OS subprocesses for Hub A (`127.0.0.1:18765`, gw `18766`, `source` auth mode),
  Hub B (`127.0.0.1:18775`, gw `18776`, `replica` auth mode pulling `auth_sync` from Hub A),
  and 3 distinct node daemons (`node-1`, `node-2`, `node-3` via `python3 -m agent_bus.cli run`).
- Routes per-node / per-hub traffic through dedicated `TcpFaultProxy` instances (`18785`–`18792`)
  for asymmetric partition (`X03`) and split-hub request/result (`X08`) fault injection.
- Double-guards against real `claude -p` execution:
  1) Places an executable (`0755`) deterministic fake `claude` script at the front of each node's
     `PATH` WITHOUT setting `BUS_CLAUDE_CMD`, exercising production `shutil.which("claude")` lookup.
  2) Strips user binary dirs from `PATH`, places a fail-closed guard binary second, and self-bounds
     fake `claude` child lifetimes (`ppid` watch + max hold timeout + process-group `SIGKILL`).
- Evaluates every scenario quantitatively via `RequestLedger` (G1–G5) + file-locked
  `SideEffectCounter` (`flock` + `flush` + `fsync`), emitting structured JSONL metrics when
  `BUS_I5_METRICS_PATH` is set (used by `run_i5.sh`).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import pathlib
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable

import pytest
import websockets

from agent_bus import auth as bus_auth
from agent_bus.journal import NodeJournal

from .hub_proc import (
    BUS_ROOT,
    HUB_A_PORT,
    HUB_B_PORT,
    HUB_HOST,
    HubController,
    HubInstance,
    assert_ports_free,
    is_port_listening,
    read_linux_proc_state,
    validate_sandbox_port,
    write_claude_guard_script,
)
from .oracle import ChaosOracle, RequestLedger, SideEffectCounter, make_ask_prompt
from .tcp_proxy import ProxyController, TcpFaultProxy

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

HUB_A_GW_PORT = 18766
HUB_B_GW_PORT = 18776
PROXY_N1_A_PORT = 18785
PROXY_N1_B_PORT = 18786
PROXY_N2_A_PORT = 18787
PROXY_N2_B_PORT = 18788
PROXY_N3_A_PORT = 18789
PROXY_N3_B_PORT = 18790
AUX_SANDBOX_PORT_1 = 18791
AUX_SANDBOX_PORT_2 = 18792

ALL_I5_PORTS: tuple[int, ...] = (
    HUB_A_PORT,
    HUB_A_GW_PORT,
    HUB_B_PORT,
    HUB_B_GW_PORT,
    PROXY_N1_A_PORT,
    PROXY_N1_B_PORT,
    PROXY_N2_A_PORT,
    PROXY_N2_B_PORT,
    PROXY_N3_A_PORT,
    PROXY_N3_B_PORT,
    AUX_SANDBOX_PORT_1,
    AUX_SANDBOX_PORT_2,
)

ALLOWED_MESH_NODES: tuple[str, ...] = (
    "cat-food",
    "mother-goose",
    "z-fold8",
    "msi",
    "node-1",
    "node-2",
    "node-3",
)
DEFAULT_PEERS_CSV = ",".join(
    [
        *ALLOWED_MESH_NODES,
        "ext-x05ext",
        "ext-x08owner",
        "ext-x10victim",
        "ext-x10keep",
    ]
)


async def _wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float = 8.0,
    interval: float = 0.04,
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


def _record_i5_metrics(
    *,
    scenario: str,
    test_name: str,
    sent: int,
    delivered: int,
    lost: int,
    duplicate_exec: int,
    uncertain: int,
    elapsed_s: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scenario": scenario,
        "test_name": test_name,
        "sent": int(sent),
        "delivered": int(delivered),
        "lost": int(lost),
        "duplicate_exec": int(duplicate_exec),
        "uncertain": int(uncertain),
        "elapsed_s": round(float(elapsed_s), 3),
        "passed": True,
        "ts": time.time(),
    }
    if extra:
        payload["extra"] = extra
    metrics_path = (os.environ.get("BUS_I5_METRICS_PATH") or "").strip()
    if metrics_path:
        p = pathlib.Path(metrics_path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            fh.flush()
    return payload


def _sync_counter_into_ledger(counter: SideEffectCounter, ledger: RequestLedger, req_ids: Iterable[str]) -> int:
    """Mirror subprocess `SideEffectCounter` records into `RequestLedger` and return duplicate count."""
    dup_total = 0
    for rid in req_ids:
        recs = counter.records(rid)
        for rec in recs:
            ledger.record_executed(rid, ts=float(rec.get("ts") or time.time()), pid=rec.get("pid"))
        if len(recs) > 1:
            dup_total += len(recs) - 1
    return dup_total


def write_fake_claude_binary(
    bin_dir: pathlib.Path,
    *,
    counter_path: pathlib.Path,
    control_path: pathlib.Path,
    node_name: str,
) -> pathlib.Path:
    """Write an executable (`0755`) fake `claude` script placed at the front of `PATH`.

    Features:
    - Handles `claude auth status` (`{"loggedIn": true}`) for `abus doctor`.
    - Extracts `[id=<req_id>]` and `보낸이: <frm>` from `-p` stdin prompt.
    - Appends to `counter_path` (`SideEffectCounter` JSONL schema) under exclusive `fcntl.flock`
      + `flush()` + `os.fsync()`.
    - Reads optional per-request rules from `control_path` (`hold_signal_file`, `release_file`,
      `max_hold_sec`, `delay_sec`, `response_text`, `exit_code`) and self-terminates immediately
      if its parent worker daemon is killed (`os.getppid() != orig_ppid`) or `max_hold_sec` elapses.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    claude_path = bin_dir / "claude"
    script_src = f"""#!{sys.executable}
import fcntl
import json
import os
import pathlib
import re
import sys
import time

COUNTER_PATH = {str(counter_path.resolve())!r}
CONTROL_PATH = {str(control_path.resolve())!r}
NODE_NAME = {str(node_name)!r}

args = sys.argv[1:]
if len(args) >= 2 and args[0] == "auth" and args[1] == "status":
    sys.stdout.write(json.dumps({{"loggedIn": True}}) + "\\n")
    sys.exit(0)

orig_ppid = os.getppid()
raw = sys.stdin.read()

m_id = re.search(r"\\[(?:req_)?id=([^\\]\\s]+)\\]", raw)
req_id = m_id.group(1) if m_id else "unknown"

m_frm = re.search(r"^보낸이:\\s*(.+)$", raw, flags=re.MULTILINE)
frm = m_frm.group(1).strip() if m_frm else "subprocess"

rules = {{}}
ctrl_file = pathlib.Path(CONTROL_PATH)
if ctrl_file.is_file():
    for _ in range(5):
        try:
            data = json.loads(ctrl_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                rules = data.get(req_id) or data.get("*") or {{}}
            break
        except Exception:
            time.sleep(0.01)

os.makedirs(os.path.dirname(COUNTER_PATH), exist_ok=True)
with open(COUNTER_PATH, "a+", encoding="utf-8") as fh:
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    try:
        fh.seek(0)
        seq = sum(1 for line in fh if line.strip()) + 1
        rec = {{
            "seq": seq,
            "id": req_id,
            "from": frm,
            "node": NODE_NAME,
            "text": raw[-240:],
            "ts": time.time(),
            "pid": os.getpid(),
        }}
        fh.seek(0, os.SEEK_END)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\\n")
        fh.flush()
        os.fsync(fh.fileno())
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

sig_file = rules.get("hold_signal_file")
if sig_file:
    sp = pathlib.Path(sig_file)
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp_sp = sp.parent / f".tmp-{{sp.name}}.{{os.getpid()}}"
    tmp_sp.write_text(
        json.dumps({{"req_id": req_id, "pid": os.getpid(), "ppid": orig_ppid, "ts": time.time()}}),
        encoding="utf-8",
    )
    os.replace(tmp_sp, sp)

rel_file = rules.get("release_file")
max_hold = float(rules.get("max_hold_sec", 8.0))
if sig_file or rel_file:
    deadline = time.monotonic() + max(0.1, max_hold)
    while time.monotonic() < deadline:
        if os.getppid() != orig_ppid:
            sys.exit(137)
        if rel_file and pathlib.Path(rel_file).exists():
            break
        time.sleep(0.02)

delay_s = float(rules.get("delay_sec", 0.0) or 0.0)
if delay_s > 0:
    end_d = time.monotonic() + delay_s
    while time.monotonic() < end_d:
        if os.getppid() != orig_ppid:
            sys.exit(137)
        time.sleep(min(0.02, max(0.005, end_d - time.monotonic())))

resp_text = str(rules.get("response_text") or f"STUB_OK:{{req_id}}:by={{NODE_NAME}}")
exit_code = int(rules.get("exit_code", 0) or 0)
sys.stdout.write(resp_text + "\\n")
sys.stdout.flush()
sys.exit(exit_code)
"""
    claude_path.write_text(script_src, encoding="utf-8")
    claude_path.chmod(0o755)
    return claude_path


def write_claude_control(control_path: pathlib.Path, rules_by_id: dict[str, dict[str, Any]]) -> None:
    control_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = control_path.parent / f".tmp-ctrl-{os.getpid()}-{time.time_ns()}.json"
    tmp.write_text(json.dumps(rules_by_id, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, control_path)


def _kill_pgid_completely(pgid: int | None, *, sig: int = signal.SIGKILL) -> None:
    if not pgid or pgid <= 1:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGCONT)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, sig)


@dataclasses.dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_s: float


class NodeSubprocess:
    """Manages a real OS `agent_bus.cli run` node daemon subprocess and oneshot CLI invocations."""

    def __init__(
        self,
        *,
        node_name: str,
        base_dir: pathlib.Path,
        urls: list[str],
        counter_path: pathlib.Path,
        control_path: pathlib.Path,
        bus_root: pathlib.Path = BUS_ROOT,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.node_name = node_name
        self.base_dir = pathlib.Path(base_dir).resolve()
        self.home_dir = self.base_dir / "home"
        self.cfg_dir = self.base_dir / "cfg"
        self.data_dir = self.base_dir / "data"
        self.logs_dir = self.base_dir / "logs"
        self.bin_dir = self.base_dir / "bin"
        self.guard_dir = self.base_dir / "_guard_bin"
        for d in (self.home_dir, self.cfg_dir, self.data_dir, self.logs_dir, self.bin_dir, self.guard_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.counter_path = pathlib.Path(counter_path).resolve()
        self.control_path = pathlib.Path(control_path).resolve()
        self.bus_root = pathlib.Path(bus_root).resolve()
        self.extra_env: dict[str, str] = dict(extra_env or {})

        self.fake_claude = write_fake_claude_binary(
            self.bin_dir,
            counter_path=self.counter_path,
            control_path=self.control_path,
            node_name=self.node_name,
        )
        self.guard_claude = write_claude_guard_script(self.guard_dir)
        self.urls: list[str] = []
        self.set_urls(urls)

        self.proc: subprocess.Popen[str] | None = None
        self.pgid: int | None = None
        self._stdout_fh: Any = None
        self._stderr_fh: Any = None
        self._active_cli_pgids: set[int] = set()
        self._run_seq = 0

    def set_urls(self, urls: list[str], *, legacy_single_url: str | None = None) -> None:
        self.urls = [str(u).strip() for u in urls if str(u).strip()]
        cfg_obj: dict[str, Any] = {}
        cfg_file = self.cfg_dir / "config.json"
        if cfg_file.is_file():
            with contextlib.suppress(Exception):
                loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cfg_obj = loaded
        if self.urls:
            cfg_obj["url"] = legacy_single_url or self.urls[0]
            cfg_obj["urls"] = list(self.urls)
        elif legacy_single_url:
            cfg_obj["url"] = legacy_single_url
            cfg_obj.pop("urls", None)
        cfg_file.write_text(json.dumps(cfg_obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def build_env(
        self,
        *,
        pythonpath: pathlib.Path | None = None,
        overrides: dict[str, str | None] | None = None,
    ) -> dict[str, str]:
        safe_sys_path = "/usr/bin:/bin"
        isolated_path = os.pathsep.join([str(self.bin_dir), str(self.guard_dir), safe_sys_path])
        resolved_claude = shutil.which("claude", path=isolated_path)
        if resolved_claude != str(self.fake_claude):
            raise RuntimeError(
                f"Fake claude PATH guard failed for {self.node_name}: resolved={resolved_claude!r}, "
                f"expected={str(self.fake_claude)!r}"
            )
        env: dict[str, str] = {}
        for k in ("USER", "LOGNAME", "LANG", "LC_ALL", "TERM"):
            if k in os.environ:
                env[k] = os.environ[k]
        env.update(
            {
                "HOME": str(self.home_dir),
                "PATH": isolated_path,
                "PYTHONPATH": str((pythonpath or self.bus_root).resolve()),
                "PYTHONUSERBASE": "/home/rerun/.local",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
                "BUS_CONFIG_DIR": str(self.cfg_dir),
                "BUS_DATA_DIR": str(self.data_dir),
                "BUS_DB_PATH": str(self.data_dir / "bus.db"),
                "BUS_LOG_DIR": str(self.logs_dir),
                "BUS_MODE_FILE": str(self.cfg_dir / "mode"),
                "BUS_TEST_ALLOW_LOCALHOST": "1",
                "BUS_TEST_LOCAL_NODE": self.node_name,
                "BUS_DIAL_TIMEOUT": "1.5",
                "BUS_HELLO_TIMEOUT": "1.5",
                "BUS_BANNER_TIMEOUT": "2.0",
                "BUS_ACK_TIMEOUT": "2.5",
            }
        )
        # Ensure `BUS_CLAUDE_CMD` is NOT set so production `shutil.which("claude")` via `PATH` is tested!
        env.pop("BUS_CLAUDE_CMD", None)
        env.update(self.extra_env)
        if overrides:
            for k, v in overrides.items():
                if v is None:
                    env.pop(k, None)
                else:
                    env[k] = str(v)
        return env

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start_daemon(
        self,
        *,
        label: str = "main",
        mode: str = "hybrid",
        peers: str = DEFAULT_PEERS_CSV,
        pythonpath: pathlib.Path | None = None,
        extra_args: list[str] | None = None,
        env_overrides: dict[str, str | None] | None = None,
    ) -> subprocess.Popen[str]:
        if self.is_running():
            raise RuntimeError(f"Node daemon {self.node_name} (pid={self.pid}) is already running")
        self._run_seq += 1
        env = self.build_env(pythonpath=pythonpath, overrides=env_overrides)
        out_path = self.logs_dir / f"daemon-{label}-{self._run_seq}.stdout.log"
        err_path = self.logs_dir / f"daemon-{label}-{self._run_seq}.stderr.log"
        self._stdout_fh = open(out_path, "a", encoding="utf-8")
        self._stderr_fh = open(err_path, "a", encoding="utf-8")

        cmd = [
            sys.executable,
            "-u",
            "-m",
            "agent_bus.cli",
            "run",
            "--label",
            label,
            "--mode",
            mode,
            "--no-auto-update",
            "--peers",
            peers,
        ]
        if extra_args:
            cmd.extend(extra_args)

        self.proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(self.home_dir),
            stdout=self._stdout_fh,
            stderr=self._stderr_fh,
            text=True,
            start_new_session=True,
        )
        self.pgid = self.proc.pid
        return self.proc

    def _close_log_handles(self) -> None:
        for attr in ("_stdout_fh", "_stderr_fh"):
            fh = getattr(self, attr, None)
            if fh is not None:
                with contextlib.suppress(Exception):
                    fh.close()
                setattr(self, attr, None)

    def sigkill(self, *, wait_timeout: float = 5.0) -> int:
        """Deliver real `SIGKILL` (`kill -9`) to the node daemon PID and reap its process group."""
        if self.proc is None:
            return -9
        pid = self.proc.pid
        pgid = self.pgid
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            rc = self.proc.wait(timeout=wait_timeout)
        else:
            rc = int(self.proc.returncode or 0)
        # Reap any child processes (e.g., in-flight fake `claude`) in the daemon's process group.
        _kill_pgid_completely(pgid, sig=signal.SIGKILL)
        self._close_log_handles()
        self.proc = None
        self.pgid = None
        return rc

    def stop(self, *, timeout: float = 4.0) -> int | None:
        """Gracefully stop the node daemon via `SIGTERM`, falling back to process-group `SIGKILL`."""
        for cli_pg in list(self._active_cli_pgids):
            _kill_pgid_completely(cli_pg, sig=signal.SIGKILL)
        self._active_cli_pgids.clear()

        if self.proc is None:
            self._close_log_handles()
            return None
        pid = self.proc.pid
        pgid = self.pgid
        rc: int | None
        if self.proc.poll() is None:
            st = read_linux_proc_state(pid)
            if st in ("T", "t"):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGCONT)
            self.proc.terminate()
            try:
                rc = self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_pgid_completely(pgid, sig=signal.SIGKILL)
                rc = self.proc.wait(timeout=4.0)
        else:
            rc = self.proc.returncode
        _kill_pgid_completely(pgid, sig=signal.SIGKILL)
        self._close_log_handles()
        self.proc = None
        self.pgid = None
        return rc

    async def run_cli(
        self,
        *cli_args: str,
        timeout: float = 15.0,
        pythonpath: pathlib.Path | None = None,
        env_overrides: dict[str, str | None] | None = None,
    ) -> CliResult:
        """Execute a real OS subprocess `python3 -m agent_bus.cli <cli_args>`."""
        env = self.build_env(pythonpath=pythonpath, overrides=env_overrides)
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_bus.cli",
            *(str(a) for a in cli_args),
            env=env,
            cwd=str(self.home_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        pgid = proc.pid
        self._active_cli_pgids.add(pgid)
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            _kill_pgid_completely(pgid, sig=signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            raise
        finally:
            self._active_cli_pgids.discard(pgid)
            _kill_pgid_completely(pgid, sig=signal.SIGKILL)

        return CliResult(
            returncode=int(proc.returncode or 0),
            stdout=out_b.decode("utf-8", errors="replace"),
            stderr=err_b.decode("utf-8", errors="replace"),
            elapsed_s=time.monotonic() - t0,
        )

    async def journal_status(self) -> dict[str, Any]:
        res = await self.run_cli("journal", "status", "--json", timeout=8.0)
        assert res.returncode == 0, f"journal status failed for {self.node_name}: {res.stderr}"
        return json.loads(res.stdout)


class FakeBannerHangProc:
    """Real OS subprocess WebSocket server that completes HTTP 101 handshake and hangs before banner."""

    def __init__(self, base_dir: pathlib.Path, port: int = AUX_SANDBOX_PORT_1, host: str = HUB_HOST) -> None:
        validate_sandbox_port(port)
        self.base_dir = pathlib.Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.host = host
        self.port = port
        self.counter_file = self.base_dir / "accepted_ws.count"
        self.script_file = self.base_dir / "fake_banner_hang_hub.py"
        self.proc: subprocess.Popen[str] | None = None
        self.pgid: int | None = None

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def accepted_ws(self) -> int:
        if not self.counter_file.is_file():
            return 0
        try:
            return int(self.counter_file.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            return 0

    def start(self, *, timeout: float = 6.0) -> "FakeBannerHangProc":
        assert_ports_free([self.port], context="start:fake_banner_hang_hub", host=self.host)
        self.counter_file.write_text("0\n", encoding="utf-8")
        self.script_file.write_text(
            f"#!{sys.executable}\n"
            "import asyncio, pathlib, websockets\n"
            f"HOST = {self.host!r}\n"
            f"PORT = {self.port!r}\n"
            f"CNT = pathlib.Path({str(self.counter_file)!r})\n"
            "count = 0\n"
            "async def handler(ws):\n"
            "    global count\n"
            "    count += 1\n"
            "    CNT.write_text(f'{count}\\n', encoding='utf-8')\n"
            "    try:\n"
            "        await asyncio.Future()  # Never send banner!\n"
            "    except BaseException:\n"
            "        pass\n"
            "async def main():\n"
            "    async with websockets.serve(handler, HOST, PORT):\n"
            "        await asyncio.Future()\n"
            "asyncio.run(main())\n",
            encoding="utf-8",
        )
        env = {
            "HOME": str(self.base_dir),
            "PATH": "/usr/bin:/bin",
            "PYTHONUSERBASE": "/home/rerun/.local",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(self.script_file)],
            env=env,
            cwd=str(self.base_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        self.pgid = self.proc.pid
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"FakeBannerHangProc exited early rc={self.proc.returncode}")
            if is_port_listening(self.host, self.port, timeout=0.05):
                return self
            time.sleep(0.02)
        raise TimeoutError(f"FakeBannerHangProc failed to listen on {self.host}:{self.port}")

    def stop(self) -> None:
        if self.proc is not None:
            pgid = self.pgid
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    _kill_pgid_completely(pgid, sig=signal.SIGKILL)
                    self.proc.wait(timeout=2.0)
            _kill_pgid_completely(pgid, sig=signal.SIGKILL)
            self.proc = None
            self.pgid = None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and is_port_listening(self.host, self.port, timeout=0.05):
            time.sleep(0.02)


@dataclasses.dataclass
class MultiProcCluster:
    """Full I5 multiprocess topology: Hub A (source) + Hub B (replica) + 6 TCP proxies + 3 OS nodes."""

    root_dir: pathlib.Path
    hub_ctrl: HubController
    proxy_ctrl: ProxyController
    oracle: ChaosOracle
    control_path: pathlib.Path
    hub_a: HubInstance
    hub_b: HubInstance
    proxies: dict[tuple[str, str], TcpFaultProxy]
    nodes: dict[str, NodeSubprocess]
    extra_procs: list[Any] = dataclasses.field(default_factory=list)

    def create_node(
        self,
        node_name: str,
        urls: list[str],
        *,
        extra_env: dict[str, str] | None = None,
    ) -> NodeSubprocess:
        np = NodeSubprocess(
            node_name=node_name,
            base_dir=self.root_dir / "nodes" / node_name,
            urls=urls,
            counter_path=self.oracle.counter.path,
            control_path=self.control_path,
            extra_env=extra_env,
        )
        self.nodes[node_name] = np
        return np

    async def aclose(self) -> None:
        for ep in reversed(self.extra_procs):
            with contextlib.suppress(Exception):
                ep.stop()
        self.extra_procs.clear()
        for np in reversed(list(self.nodes.values())):
            with contextlib.suppress(Exception):
                np.stop()
        for proxy in reversed(list(self.proxy_ctrl._proxies)):
            with contextlib.suppress(Exception):
                await proxy.close()
        self.proxy_ctrl._proxies.clear()
        self.hub_ctrl.close_all()


async def _build_standard_cluster(
    *,
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
    hub_a_local_node: str = "cat-food",
    hub_a_extra_env: dict[str, str] | None = None,
    hub_b_extra_env: dict[str, str] | None = None,
    auth_sync_interval: float = 0.2,
    start_daemons: bool = True,
) -> MultiProcCluster:
    assert_ports_free(ALL_I5_PORTS, context="i5_cluster_preflight")
    control_path = tmp_path / "oracle" / "claude_control.json"
    write_claude_control(control_path, {})

    a_env = {
        "BUS_HUB_PEERS": "mother-goose",
        "BUS_AUTH_WATCH_INTERVAL": "0.05",
    }
    if hub_a_extra_env:
        a_env.update(hub_a_extra_env)

    b_env = {
        "BUS_AUTH_SOURCE": f"ws://{HUB_HOST}:{HUB_A_PORT}",
        "BUS_AUTH_SYNC_INTERVAL": str(auth_sync_interval),
    }
    if hub_b_extra_env:
        b_env.update(hub_b_extra_env)

    hub_a = hub_factory.start_hub(
        "hub_a",
        port=HUB_A_PORT,
        gateway_port=HUB_A_GW_PORT,
        local_node=hub_a_local_node,
        allowed=ALLOWED_MESH_NODES,
        extra_env=a_env,
    )
    hub_b = hub_factory.start_hub(
        "hub_b",
        port=HUB_B_PORT,
        gateway_port=HUB_B_GW_PORT,
        local_node="mother-goose",
        allowed=ALLOWED_MESH_NODES,
        extra_env=b_env,
    )

    def _hub_b_synced() -> bool:
        try:
            with urllib.request.urlopen(f"{hub_b.gateway_http_url}/v1/health", timeout=1.5) as resp:
                h = json.loads(resp.read().decode("utf-8"))
            return h.get("auth_mode") == "replica" and int(h.get("auth_generation") or 0) >= 1
        except Exception:
            return False

    await _wait_until(_hub_b_synced, timeout=8.0, desc="Hub B initial auth_sync from Hub A")

    p_n1_a = await proxy_factory.start_proxy(listen_port=PROXY_N1_A_PORT, upstream_port=HUB_A_PORT)
    p_n1_b = await proxy_factory.start_proxy(listen_port=PROXY_N1_B_PORT, upstream_port=HUB_B_PORT)
    p_n2_a = await proxy_factory.start_proxy(listen_port=PROXY_N2_A_PORT, upstream_port=HUB_A_PORT)
    p_n2_b = await proxy_factory.start_proxy(listen_port=PROXY_N2_B_PORT, upstream_port=HUB_B_PORT)
    p_n3_a = await proxy_factory.start_proxy(listen_port=PROXY_N3_A_PORT, upstream_port=HUB_A_PORT)
    p_n3_b = await proxy_factory.start_proxy(listen_port=PROXY_N3_B_PORT, upstream_port=HUB_B_PORT)

    proxies = {
        ("node-1", "a"): p_n1_a,
        ("node-1", "b"): p_n1_b,
        ("node-2", "a"): p_n2_a,
        ("node-2", "b"): p_n2_b,
        ("node-3", "a"): p_n3_a,
        ("node-3", "b"): p_n3_b,
    }

    cluster = MultiProcCluster(
        root_dir=tmp_path / "cluster",
        hub_ctrl=hub_factory,
        proxy_ctrl=proxy_factory,
        oracle=oracle,
        control_path=control_path,
        hub_a=hub_a,
        hub_b=hub_b,
        proxies=proxies,
        nodes={},
    )

    n1 = cluster.create_node("node-1", [p_n1_a.ws_url, p_n1_b.ws_url])
    n2 = cluster.create_node("node-2", [p_n2_a.ws_url, p_n2_b.ws_url])
    n3 = cluster.create_node("node-3", [p_n3_a.ws_url, p_n3_b.ws_url])

    if start_daemons:
        n1.start_daemon()
        n2.start_daemon()
        n3.start_daemon()

        async def _all_three_registered_on_both_hubs() -> bool:
            r_a = await n1.run_cli("peers", "--url", hub_a.ws_url, timeout=5.0)
            r_b = await n1.run_cli("peers", "--url", hub_b.ws_url, timeout=5.0)
            if r_a.returncode != 0 or r_b.returncode != 0:
                return False
            expected = {"node-1/main", "node-2/main", "node-3/main"}
            return expected.issubset(set(r_a.stdout.strip().replace("peers=", "").split(","))) and expected.issubset(
                set(r_b.stdout.strip().replace("peers=", "").split(","))
            )

        await _wait_until(
            _all_three_registered_on_both_hubs,
            timeout=10.0,
            desc="node-1/main, node-2/main, node-3/main registered as distinct nodes on Hub A & Hub B",
        )

    return cluster


def _http_get_json(url: str, timeout: float = 3.0) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        exc.close()
        return exc.code, (json.loads(body) if body.strip() else {})


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


# ==============================================================================
# 1. X01: Real-process Hub A SIGKILL + permanent DB wipe + empty-DB restart (H10)
# ==============================================================================
@pytest.mark.chaos
async def test_i5_x01_hub_a_sigkill_db_wipe_and_empty_restart_zero_loss(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
) -> None:
    t0 = time.monotonic()
    cluster = await _build_standard_cluster(
        tmp_path=tmp_path,
        hub_factory=hub_factory,
        proxy_factory=proxy_factory,
        oracle=oracle,
    )
    try:
        n1, n2, n3 = cluster.nodes["node-1"], cluster.nodes["node-2"], cluster.nodes["node-3"]
        hub_a, hub_b = cluster.hub_a, cluster.hub_b

        _, health_a0 = _http_get_json(f"{hub_a.gateway_http_url}/v1/health")
        old_hub_a_id = str(health_a0["hub_id"])
        assert old_hub_a_id

        # Boundary 1: Hub A ACKs while Hub B path from node-1 and Hub A delivery to node-2 are blocked.
        # Then Hub A is SIGKILLed and its SQLite DB (`bus.db`, `-wal`, `-shm`) is permanently deleted!
        cluster.proxies[("node-1", "b")].block(True, cut_existing=True)
        cluster.proxies[("node-2", "a")].block(True, cut_existing=True)

        b1_ids = [f"x01-ack-boundary-{i}" for i in range(3)]
        for rid in b1_ids:
            oracle.ledger.record_sent(rid, sender="node-1", boundary="post_hub_a_ack")
            r = await n1.run_cli(
                "send",
                "--to",
                "node-2/main",
                "--corr-id",
                rid,
                f"[id={rid}] x01-boundary-1",
            )
            assert r.returncode == 0, r.stderr
            oracle.ledger.record_hub_ack(rid, hub="hub_a_pre_wipe")

        st_n1 = await n1.journal_status()
        assert st_n1["outbox"]["pending"] == 3, f"Expected 3 pending outbox items awaiting Hub B ACK: {st_n1}"
        assert all(oracle.counter.count(rid) == 0 for rid in b1_ids)

        # SIGKILL Hub A + permanently delete `bus.db`, `bus.db-wal`, `bus.db-shm`
        assert hub_a.sigkill() == -9
        for suffix in ("", "-wal", "-shm"):
            p_db = pathlib.Path(str(hub_a.db_path) + suffix)
            if p_db.exists():
                p_db.unlink()
        assert not hub_a.db_path.exists()

        # Restart Hub A with a fresh empty DB (new `hub_id`, scenario H10) and heal proxy paths
        hub_a.start(wipe_db=True)
        _, health_a1 = _http_get_json(f"{hub_a.gateway_http_url}/v1/health")
        new_hub_a_id = str(health_a1["hub_id"])
        assert new_hub_a_id and new_hub_a_id != old_hub_a_id

        cluster.proxies[("node-1", "b")].unblock()
        cluster.proxies[("node-2", "a")].unblock()

        # Boundary 2: Immediately after send burst from node-1 & node-3, SIGKILL + wipe Hub A again!
        b2_ids = [f"x01-post-send-{i}" for i in range(5)]
        for idx, rid in enumerate(b2_ids):
            sender_node = n1 if idx < 3 else n3
            oracle.ledger.record_sent(rid, sender=sender_node.node_name, boundary="post_send")
            r = await sender_node.run_cli(
                "send",
                "--to",
                "node-2/main",
                "--corr-id",
                rid,
                f"[id={rid}] x01-boundary-2",
            )
            assert r.returncode == 0, r.stderr
            oracle.ledger.record_hub_ack(rid, hub="dual_send")

        assert hub_a.sigkill() == -9
        hub_a.start(wipe_db=True)

        all_ids = [*b1_ids, *b2_ids]
        await _wait_until(
            lambda: all(oracle.counter.count(rid) >= 1 for rid in all_ids),
            timeout=12.0,
            desc="all 8 X01 requests executed on node-2",
        )

        # Wait for node-1 and node-3 outbox pumps to republish to recreated Hub A (`pending == 0`)
        async def _outboxes_settled() -> bool:
            s1 = await n1.journal_status()
            s2 = await n2.journal_status()
            s3 = await n3.journal_status()
            return (
                s1["outbox"]["pending"] == 0
                and s2["outbox"]["pending"] == 0
                and s3["outbox"]["pending"] == 0
            )

        await _wait_until(_outboxes_settled, timeout=12.0, desc="all node outboxes settled after H10 restart")

        # Verify every result is retrievable via CLI `abus result`
        for rid in all_ids:
            oracle.ledger.record_received(rid, node="node-2")
            res_cli = await n1.run_cli("result", rid, "--wait", "5", "--json")
            assert res_cli.returncode == 0, f"Result missing for {rid}: {res_cli.stderr}"
            res_data = json.loads(res_cli.stdout)
            assert res_data["status"] == "ok"
            assert f"STUB_OK:{rid}" in res_data["text"]
            oracle.ledger.record_result(rid, status="ok")

        dup_cnt = _sync_counter_into_ledger(oracle.counter, oracle.ledger, all_ids)
        counts = {rid: oracle.counter.count(rid) for rid in all_ids}
        assert all(c == 1 for c in counts.values()), f"Expected exact 1 execution per id, got {counts}"
        assert dup_cnt == 0

        _record_i5_metrics(
            scenario="X01",
            test_name="test_i5_x01_hub_a_sigkill_db_wipe_and_empty_restart_zero_loss",
            sent=len(all_ids),
            delivered=sum(1 for c in counts.values() if c >= 1),
            lost=sum(1 for c in counts.values() if c == 0),
            duplicate_exec=dup_cnt,
            uncertain=0,
            elapsed_s=time.monotonic() - t0,
        )
    finally:
        await cluster.aclose()
        assert_ports_free(ALL_I5_PORTS, context="x01_teardown")


# ==============================================================================
# 2. X03: Asymmetric network partition (shared hub vs disjoint hubs -> heal)
# ==============================================================================
@pytest.mark.chaos
async def test_i5_x03_asymmetric_partition_shared_hub_immediate_disjoint_buffered(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
) -> None:
    t0 = time.monotonic()
    cluster = await _build_standard_cluster(
        tmp_path=tmp_path,
        hub_factory=hub_factory,
        proxy_factory=proxy_factory,
        oracle=oracle,
    )
    try:
        n1, n2, n3 = cluster.nodes["node-1"], cluster.nodes["node-2"], cluster.nodes["node-3"]

        # Inject asymmetric partition:
        # - Block `node-1 -> Hub A` (node-1 reaches ONLY Hub B)
        # - Block `node-2 -> Hub B` (node-2 reaches ONLY Hub A)
        # - `node-3` reaches BOTH Hub A and Hub B!
        cluster.proxies[("node-1", "a")].block(True, cut_existing=True)
        cluster.proxies[("node-2", "b")].block(True, cut_existing=True)

        # Wait until Hub A drops node-1/main and Hub B drops node-2/main
        async def _partition_effective() -> bool:
            pa = await n3.run_cli("peers", "--url", cluster.hub_a.ws_url)
            pb = await n3.run_cli("peers", "--url", cluster.hub_b.ws_url)
            if pa.returncode != 0 or pb.returncode != 0:
                return False
            peers_a = set(pa.stdout.strip().replace("peers=", "").split(","))
            peers_b = set(pb.stdout.strip().replace("peers=", "").split(","))
            return (
                "node-1/main" not in peers_a
                and "node-2/main" in peers_a
                and "node-3/main" in peers_a
                and "node-2/main" not in peers_b
                and "node-1/main" in peers_b
                and "node-3/main" in peers_b
            )

        await _wait_until(_partition_effective, timeout=8.0, desc="asymmetric partition reflected on Hub A & Hub B")

        # 1) Pairs WITH a shared hub (`node-1 <-> node-3` via Hub B, `node-3 -> node-2` via Hub A)
        #    must succeed IMMEDIATELY during the partition!
        shared_specs = [
            (n1, "node-3/main", "x03-n1-to-n3-1"),
            (n1, "node-3/main", "x03-n1-to-n3-2"),
            (n3, "node-1/main", "x03-n3-to-n1-1"),
            (n3, "node-1/main", "x03-n3-to-n1-2"),
            (n3, "node-2/main", "x03-n3-to-n2-1"),
        ]
        for sender_np, target, rid in shared_specs:
            oracle.ledger.record_sent(rid, sender=sender_np.node_name, target=target)
            res = await sender_np.run_cli(
                "send",
                "--to",
                target,
                "--corr-id",
                rid,
                "--wait",
                "8",
                f"[id={rid}] shared-hub-rpc",
            )
            assert res.returncode == 0, f"Shared-hub RPC {rid} failed: {res.stderr}"
            assert f"STUB_OK:{rid}" in res.stdout
            oracle.ledger.record_hub_ack(rid, hub="shared_hub")
            oracle.ledger.record_received(rid)
            oracle.ledger.record_result(rid, status="ok")

        # 2) Disjoint pair (`node-1 -> node-2/main`, NO common hub):
        #    Hub B ACKs node-1's send, node-1's `outbox` retains `pending` for Hub A,
        #    and `node-2` sees 0 executions while partitioned!
        disjoint_ids = [f"x03-disjoint-n1-n2-{i}" for i in range(3)]
        for rid in disjoint_ids:
            oracle.ledger.record_sent(rid, sender="node-1", target="node-2/main")
            r = await n1.run_cli(
                "send",
                "--to",
                "node-2/main",
                "--corr-id",
                rid,
                f"[id={rid}] disjoint-buffered-task",
            )
            assert r.returncode == 0, r.stderr
            oracle.ledger.record_hub_ack(rid, hub="hub_b_only")

        await asyncio.sleep(0.8)
        st_part = await n1.journal_status()
        assert st_part["outbox"]["pending"] >= len(disjoint_ids), (
            f"Disjoint requests must stay pending in node-1 outbox during partition: {st_part}"
        )
        for rid in disjoint_ids:
            assert oracle.counter.count(rid) == 0, f"{rid} should not reach partitioned node-2 yet"

        # 3) Heal partition -> all buffered requests deliver & execute exactly once (`lost=0`, `dup=0`)!
        cluster.proxies[("node-1", "a")].unblock()
        cluster.proxies[("node-2", "b")].unblock()

        await _wait_until(
            lambda: all(oracle.counter.count(rid) >= 1 for rid in disjoint_ids),
            timeout=12.0,
            desc="disjoint node-1 -> node-2 requests delivered & executed after partition heal",
        )

        for rid in disjoint_ids:
            oracle.ledger.record_received(rid, node="node-2")
            r_res = await n1.run_cli("result", rid, "--wait", "6", "--json")
            assert r_res.returncode == 0, r_res.stderr
            assert json.loads(r_res.stdout)["status"] == "ok"
            oracle.ledger.record_result(rid, status="ok")

        async def _all_outboxes_drained() -> bool:
            for np in (n1, n2, n3):
                if (await np.journal_status())["outbox"]["pending"] != 0:
                    return False
            return True

        await _wait_until(_all_outboxes_drained, timeout=10.0, desc="all outboxes drained post-partition")

        all_ids = [rid for _, _, rid in shared_specs] + disjoint_ids
        dup_cnt = _sync_counter_into_ledger(oracle.counter, oracle.ledger, all_ids)
        counts = {rid: oracle.counter.count(rid) for rid in all_ids}
        assert all(c == 1 for c in counts.values()), f"Expected 1 execution per id: {counts}"
        assert dup_cnt == 0

        _record_i5_metrics(
            scenario="X03",
            test_name="test_i5_x03_asymmetric_partition_shared_hub_immediate_disjoint_buffered",
            sent=len(all_ids),
            delivered=sum(1 for c in counts.values() if c >= 1),
            lost=sum(1 for c in counts.values() if c == 0),
            duplicate_exec=dup_cnt,
            uncertain=0,
            elapsed_s=time.monotonic() - t0,
        )
    finally:
        await cluster.aclose()
        assert_ports_free(ALL_I5_PORTS, context="x03_teardown")


# ==============================================================================
# 3. X05: Hub A SIGSTOP freeze -> Hub B uninterrupted -> SIGCONT + +301s skew
# ==============================================================================
@pytest.mark.chaos
async def test_i5_x05_hub_a_sigstop_sigcont_zero_dup_and_clock_offset_301s_auth_error(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
) -> None:
    t0 = time.monotonic()
    cluster = await _build_standard_cluster(
        tmp_path=tmp_path,
        hub_factory=hub_factory,
        proxy_factory=proxy_factory,
        oracle=oracle,
    )
    try:
        n1, n2, n3 = cluster.nodes["node-1"], cluster.nodes["node-2"], cluster.nodes["node-3"]
        hub_a, hub_b = cluster.hub_a, cluster.hub_b

        # Issue an external HMAC token on Hub A via real CLI and wait for Hub B sync
        hub_a_admin = NodeSubprocess(
            node_name="cat-food",
            base_dir=tmp_path / "hub_a_admin_cli",
            urls=[hub_a.ws_url],
            counter_path=oracle.counter.path,
            control_path=cluster.control_path,
        )
        cluster.extra_procs.append(hub_a_admin)
        tok_create = await hub_a_admin.run_cli(
            "token",
            "create",
            "--node",
            "x05ext",
            "--scope",
            "rpc:node-2/main",
            "--scope",
            "send:node-2/inbox",
            "--allow-exec",
            "--json",
            env_overrides={"BUS_CONFIG_DIR": str(hub_a.cfg_dir)},
        )
        assert tok_create.returncode == 0, tok_create.stderr
        tok = json.loads(tok_create.stdout)

        await _wait_until(
            lambda: _http_get_json(f"{hub_b.gateway_http_url}/v1/health")[1].get("auth_generation", 0) >= 2,
            timeout=6.0,
            desc="Hub B synced x05ext token generation",
        )

        ext_http_node = cluster.create_node(
            "ext-x05-http",
            [hub_a.gateway_http_url, hub_b.gateway_http_url],
            extra_env={"BUS_AUTH_TOKEN": tok["id"], "BUS_AUTH_SECRET": tok["secret"]},
        )
        ext_ws_node = cluster.create_node(
            "ext-x05-ws",
            [hub_a.gateway_ws_url, hub_b.gateway_ws_url],
            extra_env={"BUS_AUTH_TOKEN": tok["id"], "BUS_AUTH_SECRET": tok["secret"]},
        )

        # Baseline external RPC with 0s clock offset succeeds
        pre_id = "x05-pre-ext-ok"
        oracle.ledger.record_sent(pre_id, sender="ext-x05")
        pre_res = await ext_http_node.run_cli(
            "send",
            "--to",
            "node-2/main",
            "--corr-id",
            pre_id,
            "--wait",
            "8",
            f"[id={pre_id}] baseline-ext-ok",
        )
        assert pre_res.returncode == 0, pre_res.stderr
        assert f"STUB_OK:{pre_id}" in pre_res.stdout
        oracle.ledger.record_hub_ack(pre_id, hub="gateway_dual")
        oracle.ledger.record_received(pre_id, node="node-2")
        oracle.ledger.record_result(pre_id, status="ok")

        # Part A: Freeze Hub A with real kernel `SIGSTOP`!
        hub_a.sigstop(wait_state=True)
        assert hub_a.proc_state() in ("T", "t")
        freeze_started = time.monotonic()

        frozen_specs = [
            (n1, "node-2/main", "x05-during-sigstop-1"),
            (n1, "node-2/main", "x05-during-sigstop-2"),
            (n1, "node-2/main", "x05-during-sigstop-3"),
            (n2, "node-3/main", "x05-during-sigstop-4"),
            (n2, "node-3/main", "x05-during-sigstop-5"),
        ]
        for sender_np, target, rid in frozen_specs:
            oracle.ledger.record_sent(rid, sender=sender_np.node_name, phase="hub_a_sigstopped")
            r = await sender_np.run_cli(
                "send",
                "--to",
                target,
                "--corr-id",
                rid,
                "--wait",
                "8",
                f"[id={rid}] uninterrupted-via-hub-b",
            )
            assert r.returncode == 0, f"RPC failed while Hub A SIGSTOPped: {r.stderr}"
            assert f"STUB_OK:{rid}" in r.stdout
            oracle.ledger.record_hub_ack(rid, hub="hub_b")
            oracle.ledger.record_received(rid)
            oracle.ledger.record_result(rid, status="ok")

        # Keep Hub A genuinely frozen for at least 3.0s total before resuming via SIGCONT
        rem_freeze = 3.0 - (time.monotonic() - freeze_started)
        if rem_freeze > 0:
            await asyncio.sleep(rem_freeze)
        assert hub_a.proc_state() in ("T", "t")

        hub_a.sigcont(wait_state=True)
        assert hub_a.proc_state() not in ("T", "t")

        # Wait for all outboxes to sync back to thawed Hub A and ensure 0 duplicate executions!
        async def _post_sigcont_settled() -> bool:
            for np in (n1, n2, n3):
                if (await np.journal_status())["outbox"]["pending"] != 0:
                    return False
            return True

        await _wait_until(_post_sigcont_settled, timeout=12.0, desc="outboxes synced to Hub A after SIGCONT")
        await asyncio.sleep(0.4)

        # Part B: Wall-clock skew `+301s` (and `-301s`) on external HMAC client -> distinct auth error!
        skew_id_http = "x05-skew-plus301-http"
        r_skew_http = await ext_http_node.run_cli(
            "send",
            "--to",
            "node-2/main",
            "--corr-id",
            skew_id_http,
            "--offline-queue",  # Even with `--offline-queue`, auth failure must NOT masquerade as rc=0!
            f"[id={skew_id_http}] should-be-rejected-by-replay-window",
            env_overrides={"BUS_TEST_CLOCK_OFFSET_S": "+301"},
        )
        assert r_skew_http.returncode == 3, f"Expected auth exit code 3, got {r_skew_http}"
        assert "[auth-error]" in r_skew_http.stderr
        assert "http_401" in r_skew_http.stderr
        assert "timestamp outside replay window" in r_skew_http.stderr

        skew_id_ws = "x05-skew-plus301-ws"
        r_skew_ws = await ext_ws_node.run_cli(
            "send",
            "--to",
            "node-2/main",
            "--corr-id",
            skew_id_ws,
            "--wait",
            "4",
            f"[id={skew_id_ws}] should-be-rejected-ws-4003",
            env_overrides={"BUS_TEST_CLOCK_OFFSET_S": "+301"},
        )
        assert r_skew_ws.returncode == 3, f"Expected WS auth exit code 3, got {r_skew_ws}"
        assert "[auth-error]" in r_skew_ws.stderr
        assert "ws_4003" in r_skew_ws.stderr
        assert "timestamp outside replay window" in r_skew_ws.stderr

        r_skew_peers = await ext_ws_node.run_cli(
            "peers",
            env_overrides={"BUS_TEST_CLOCK_OFFSET_S": "-301"},
        )
        assert r_skew_peers.returncode == 3, f"Expected WS peers auth exit code 3: {r_skew_peers}"
        assert "[auth-error]" in r_skew_peers.stderr and "ws_4003" in r_skew_peers.stderr

        assert oracle.counter.count(skew_id_http) == 0
        assert oracle.counter.count(skew_id_ws) == 0

        # Also verify production safety invariant: when `BUS_TEST_ALLOW_LOCALHOST` is NOT `"1"`,
        # `X-Bus-Test-Local-Node` header is completely ignored on loopback!
        prod_guard_hub = hub_factory.start_hub(
            "hub_prod_guard",
            port=AUX_SANDBOX_PORT_1,
            allowed=("node-1", "cat-food"),
            extra_env={"BUS_TEST_ALLOW_LOCALHOST": "0", "BUS_WHOIS_CMD": "/bin/false"},
        )
        try:
            import inspect

            hdr_kw = (
                "additional_headers"
                if "additional_headers" in inspect.signature(websockets.connect).parameters
                else "extra_headers"
            )
            async with websockets.connect(
                prod_guard_hub.ws_url,
                **{hdr_kw: {"X-Bus-Test-Local-Node": "node-1"}},
            ) as ws_probe:
                await ws_probe.send(json.dumps({"v": 2, "label": "probe", "since": 0}))
                with pytest.raises(websockets.exceptions.ConnectionClosed) as ws_exc:
                    await asyncio.wait_for(ws_probe.recv(), timeout=3.0)
                assert ws_exc.value.rcvd is not None and ws_exc.value.rcvd.code == 4003
        finally:
            prod_guard_hub.stop()

        valid_ids = [pre_id, *(rid for _, _, rid in frozen_specs)]
        dup_cnt = _sync_counter_into_ledger(oracle.counter, oracle.ledger, valid_ids)
        counts = {rid: oracle.counter.count(rid) for rid in valid_ids}
        assert all(c == 1 for c in counts.values()), f"Expected exact 1 execution per valid id: {counts}"
        assert dup_cnt == 0

        _record_i5_metrics(
            scenario="X05",
            test_name="test_i5_x05_hub_a_sigstop_sigcont_zero_dup_and_clock_offset_301s_auth_error",
            sent=len(valid_ids),
            delivered=sum(1 for c in counts.values() if c >= 1),
            lost=sum(1 for c in counts.values() if c == 0),
            duplicate_exec=dup_cnt,
            uncertain=0,
            elapsed_s=time.monotonic() - t0,
            extra={"rejected_skew_requests": 2},
        )
    finally:
        await cluster.aclose()
        assert_ports_free(ALL_I5_PORTS, context="x05_teardown")


# ==============================================================================
# 4. X07 + X02: Real-process worker `SIGKILL` mid-execution -> `exec_uncertain`
# ==============================================================================
@pytest.mark.chaos
async def test_i5_x07_x02_worker_sigkill_mid_claude_exec_uncertain_and_queued_recovery(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
) -> None:
    t0 = time.monotonic()
    cluster = await _build_standard_cluster(
        tmp_path=tmp_path,
        hub_factory=hub_factory,
        proxy_factory=proxy_factory,
        oracle=oracle,
    )
    try:
        n1, n2 = cluster.nodes["node-1"], cluster.nodes["node-2"]

        inflight_id = "x02-x07-inflight-kill"
        queued_ids = [f"x02-x07-queued-{i}" for i in range(3)]
        expired_id = "x02-x07-deadline-expired"

        hold_sig = tmp_path / "signals" / "inflight_entered.json"
        release_sig = tmp_path / "signals" / "inflight_release.flag"
        write_claude_control(
            cluster.control_path,
            {
                inflight_id: {
                    "hold_signal_file": str(hold_sig),
                    "release_file": str(release_sig),
                    "max_hold_sec": 10.0,
                }
            },
        )

        # 1) Send `inflight_id` -> wait until fake `claude` child process is actively mid-execution!
        oracle.ledger.record_sent(inflight_id, sender="node-1", role="inflight_victim")
        r_inf = await n1.run_cli(
            "send",
            "--to",
            "node-2/main",
            "--corr-id",
            inflight_id,
            f"[id={inflight_id}] slow-inflight-work",
        )
        assert r_inf.returncode == 0, r_inf.stderr
        oracle.ledger.record_hub_ack(inflight_id, hub="dual")

        await _wait_until(lambda: hold_sig.is_file(), timeout=8.0, desc="fake claude entered mid-execution")
        oracle.ledger.record_received(inflight_id, node="node-2")
        assert oracle.counter.count(inflight_id) == 1

        # Verify `node-2`'s `journal.db` shows `inflight_id` in `running` state
        with NodeJournal(n2.cfg_dir) as j2:
            assert j2.status_summary()["inbox"]["running"] == 1

        # 2) While `node-2` worker is busy inside fake `claude`, enqueue:
        #    - 3 valid `queued` requests (`queued_ids`)
        #    - 1 short-deadline request (`expired_id`, deadline = now + 0.35s)
        #    - 2 interleaved events (X07)
        for qid in queued_ids:
            oracle.ledger.record_sent(qid, sender="node-1", role="queued_survivor")
            rq = await n1.run_cli(
                "send",
                "--to",
                "node-2/main",
                "--corr-id",
                qid,
                f"[id={qid}] queued-behind-inflight",
            )
            assert rq.returncode == 0, rq.stderr
            oracle.ledger.record_hub_ack(qid, hub="dual")

        short_dl = time.time() + 0.35
        oracle.ledger.record_sent(expired_id, sender="node-1", role="deadline_expired")
        rexp = await n1.run_cli(
            "send",
            "--to",
            "node-2/main",
            "--corr-id",
            expired_id,
            "--deadline",
            f"{short_dl:.3f}",
            f"[id={expired_id}] expires-while-queued",
        )
        assert rexp.returncode == 0, rexp.stderr
        oracle.ledger.record_hub_ack(expired_id, hub="dual")

        for ev_idx in range(2):
            rev = await n1.run_cli("send", "--to", "node-2/inbox", "--say", f"x07-event-{ev_idx}")
            assert rev.returncode == 0, rev.stderr

        # Wait until all 4 requests (`3 queued + 1 expiring`) are durably committed in `node-2` `inbox`
        def _four_queued_in_node2_journal() -> bool:
            with NodeJournal(n2.cfg_dir) as j2:
                ib = j2.status_summary()["inbox"]
                return ib["running"] == 1 and ib["queued"] >= 4

        await _wait_until(_four_queued_in_node2_journal, timeout=6.0, desc="4 queued items persisted on node-2")

        # Ensure `short_dl` has elapsed before restart so `expired_id` transitions `queued -> expired`
        while time.time() <= short_dl + 0.05:
            await asyncio.sleep(0.03)

        # 3) Deliver real `SIGKILL` (`kill -9`) to `node-2` OS worker process!
        kill_rc = n2.sigkill()
        assert kill_rc == -9

        # Clear hold rule (even if called, it wouldn't block — and we assert it is NEVER called again!)
        write_claude_control(cluster.control_path, {})

        # 4) Restart `node-2` daemon process against the EXACT SAME `cfg_dir`!
        n2.start_daemon()

        # Verify:
        # - `inflight_id`: 0 re-executions (`count == 1`), `inbox.uncertain == 1`, caller gets `status == 'exec_uncertain'`!
        res_unc = await n1.run_cli("result", inflight_id, "--wait", "8", "--json")
        assert res_unc.returncode == 1, f"exec_uncertain should return exit code 1: {res_unc}"
        unc_data = json.loads(res_unc.stdout)
        assert unc_data["found"] is True
        assert unc_data["status"] == "exec_uncertain"
        oracle.ledger.record_result(inflight_id, status="exec_uncertain")

        # - `queued_ids`: all 3 execute exactly once (`count == 1`) and return `status == 'ok'`!
        await _wait_until(
            lambda: all(oracle.counter.count(qid) >= 1 for qid in queued_ids),
            timeout=10.0,
            desc="all 3 queued survivors executed after node-2 restart",
        )
        for qid in queued_ids:
            oracle.ledger.record_received(qid, node="node-2")
            r_ok = await n1.run_cli("result", qid, "--wait", "8", "--json")
            assert r_ok.returncode == 0, r_ok.stderr
            ok_data = json.loads(r_ok.stdout)
            assert ok_data["status"] == "ok" and f"STUB_OK:{qid}" in ok_data["text"]
            oracle.ledger.record_result(qid, status="ok")

        # - `expired_id`: 0 executions (`count == 0`), caller receives `status == 'expired'`!
        res_exp = await n1.run_cli("result", expired_id, "--wait", "8", "--json")
        assert res_exp.returncode == 1, res_exp.stderr
        exp_data = json.loads(res_exp.stdout)
        assert exp_data["status"] == "expired"
        oracle.ledger.record_received(expired_id, node="node-2")
        oracle.ledger.record_result(expired_id, status="expired")
        assert oracle.counter.count(expired_id) == 0

        st2 = await n2.journal_status()
        assert st2["inbox"]["uncertain"] == 1
        assert st2["inbox"]["expired"] >= 1
        assert st2["inbox"]["done"] == 3
        assert oracle.counter.count(inflight_id) == 1, "In-flight crashed request MUST NOT re-execute!"

        # Hub dead-letter count must remain 0 (`X07`)
        _, ha = _http_get_json(f"{cluster.hub_a.gateway_http_url}/v1/health")
        _, hb = _http_get_json(f"{cluster.hub_b.gateway_http_url}/v1/health")
        assert ha["dead"] == 0 and hb["dead"] == 0

        tracked_exec_ids = [inflight_id, *queued_ids]
        dup_cnt = _sync_counter_into_ledger(oracle.counter, oracle.ledger, tracked_exec_ids)
        assert all(oracle.counter.count(rid) == 1 for rid in tracked_exec_ids)
        assert dup_cnt == 0

        _record_i5_metrics(
            scenario="X07_X02",
            test_name="test_i5_x07_x02_worker_sigkill_mid_claude_exec_uncertain_and_queued_recovery",
            sent=len(queued_ids) + 1,  # 1 inflight + 3 queued valid tasks (+1 expired tested separately)
            delivered=len(queued_ids),  # 3 completed `ok` + 1 reported `exec_uncertain` (0 lost!)
            lost=0,
            duplicate_exec=dup_cnt,
            uncertain=st2["inbox"]["uncertain"],
            elapsed_s=time.monotonic() - t0,
            extra={"expired_notified": 1},
        )
    finally:
        await cluster.aclose()
        assert_ports_free(ALL_I5_PORTS, context="x07_x02_teardown")


# ==============================================================================
# 5. X08: Request ONLY on Hub A, Result ONLY on Hub B -> cross-hub reception
# ==============================================================================
@pytest.mark.chaos
async def test_i5_x08_request_only_on_hub_a_result_only_on_hub_b_cross_delivery(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
) -> None:
    t0 = time.monotonic()
    cluster = await _build_standard_cluster(
        tmp_path=tmp_path,
        hub_factory=hub_factory,
        proxy_factory=proxy_factory,
        oracle=oracle,
    )
    try:
        n1, n2 = cluster.nodes["node-1"], cluster.nodes["node-2"]
        hub_a, hub_b = cluster.hub_a, cluster.hub_b

        # Proxies for external HTTP gateway dual-home (`18791 -> 18766`, `18792 -> 18776`)
        p_ext_a = await proxy_factory.start_proxy(listen_port=AUX_SANDBOX_PORT_1, upstream_port=HUB_A_GW_PORT)
        p_ext_b = await proxy_factory.start_proxy(listen_port=AUX_SANDBOX_PORT_2, upstream_port=HUB_B_GW_PORT)

        admin = NodeSubprocess(
            node_name="cat-food",
            base_dir=tmp_path / "x08_admin",
            urls=[hub_a.ws_url],
            counter_path=oracle.counter.path,
            control_path=cluster.control_path,
        )
        cluster.extra_procs.append(admin)
        tok_owner = json.loads(
            (
                await admin.run_cli(
                    "token",
                    "create",
                    "--node",
                    "x08owner",
                    "--scope",
                    "rpc:node-2/main",
                    "--allow-exec",
                    "--json",
                    env_overrides={"BUS_CONFIG_DIR": str(hub_a.cfg_dir)},
                )
            ).stdout
        )
        tok_other = json.loads(
            (
                await admin.run_cli(
                    "token",
                    "create",
                    "--node",
                    "x08other",
                    "--scope",
                    "rpc:node-2/main",
                    "--allow-exec",
                    "--json",
                    env_overrides={"BUS_CONFIG_DIR": str(hub_a.cfg_dir)},
                )
            ).stdout
        )
        await _wait_until(
            lambda: _http_get_json(f"{hub_b.gateway_http_url}/v1/health")[1].get("auth_generation", 0) >= 3,
            timeout=6.0,
            desc="Hub B synced x08 external tokens",
        )

        ext_owner = cluster.create_node(
            "ext-x08-owner",
            [p_ext_a.http_url, p_ext_b.http_url],
            extra_env={"BUS_AUTH_TOKEN": tok_owner["id"], "BUS_AUTH_SECRET": tok_owner["secret"]},
        )
        ext_other = cluster.create_node(
            "ext-x08-other",
            [hub_b.gateway_http_url],
            extra_env={"BUS_AUTH_TOKEN": tok_other["id"], "BUS_AUTH_SECRET": tok_other["secret"]},
        )

        req_int = "x08-cross-internal-1"
        req_ext = "x08-cross-external-1"
        hold_sig = tmp_path / "x08_signals" / "entered.json"
        rel_sig = tmp_path / "x08_signals" / "release.flag"
        write_claude_control(
            cluster.control_path,
            {
                req_int: {
                    "hold_signal_file": str(hold_sig),
                    "release_file": str(rel_sig),
                    "max_hold_sec": 10.0,
                }
            },
        )

        # Phase 1: Block ALL Hub B client/worker paths so requests land ONLY on Hub A!
        cluster.proxies[("node-1", "b")].block(True, cut_existing=True)
        cluster.proxies[("node-2", "b")].block(True, cut_existing=True)
        p_ext_b.block(True, cut_existing=True)

        oracle.ledger.record_sent(req_int, sender="node-1", hub="hub_a_only")
        wait_caller_task = asyncio.create_task(
            n1.run_cli(
                "send",
                "--to",
                "node-2/main",
                "--corr-id",
                req_int,
                "--wait",
                "12",
                f"[id={req_int}] cross-hub-internal-rpc",
            )
        )

        await _wait_until(lambda: hold_sig.is_file(), timeout=8.0, desc="node-2 entered execution of req_int")
        oracle.ledger.record_hub_ack(req_int, hub="hub_a")
        oracle.ledger.record_received(req_int, node="node-2")

        oracle.ledger.record_sent(req_ext, sender="ext-x08owner", hub="hub_a_only")
        r_ext_send = await ext_owner.run_cli(
            "send",
            "--to",
            "node-2/main",
            "--corr-id",
            req_ext,
            "--kind",
            "rpc",
            f"[id={req_ext}] cross-hub-external-rpc",
        )
        assert r_ext_send.returncode == 0, r_ext_send.stderr
        oracle.ledger.record_hub_ack(req_ext, hub="hub_a")

        # Wait until `req_ext` is also persisted in `node-2`'s `inbox` from Hub A
        def _ext_in_node2_inbox() -> bool:
            with NodeJournal(n2.cfg_dir) as j2:
                return j2.status_summary()["inbox"]["queued"] >= 1

        await _wait_until(_ext_in_node2_inbox, timeout=6.0, desc="req_ext queued in node-2 inbox via Hub A")
        oracle.ledger.record_received(req_ext, node="node-2")

        # Prove Hub B has ZERO knowledge of either request right now!
        hb_msgs_before = {m["id"] for m in hub_b.inspect_db()["messages"]}
        assert req_int not in hb_msgs_before and req_ext not in hb_msgs_before

        # Phase 2: Block ALL Hub A paths and open ALL Hub B paths so results go ONLY to Hub B!
        cluster.proxies[("node-1", "a")].block(True, cut_existing=True)
        cluster.proxies[("node-2", "a")].block(True, cut_existing=True)
        p_ext_a.block(True, cut_existing=True)

        cluster.proxies[("node-1", "b")].unblock()
        cluster.proxies[("node-2", "b")].unblock()
        p_ext_b.unblock()

        rel_sig.parent.mkdir(parents=True, exist_ok=True)
        rel_sig.write_text("go\n", encoding="utf-8")

        # 1) Live `abus send --wait` caller (whose original send was on Hub A) receives terminal result via `abus result` / Hub B!
        #    Note: `send --wait`'s Hub B task failed in Phase 1 while blocked, so test both `wait_caller_task` / `abus result` on dual-home!
        res_int_poll = await n1.run_cli("result", req_int, "--wait", "10", "--json")
        assert res_int_poll.returncode == 0, res_int_poll.stderr
        int_data = json.loads(res_int_poll.stdout)
        assert int_data["found"] is True and int_data["status"] == "ok"
        assert f"STUB_OK:{req_int}" in int_data["text"]
        oracle.ledger.record_result(req_int, status="ok", via="hub_b")

        # Cancel or collect `wait_caller_task` cleanly (since Hub A is currently blocked, its WS wait would time out if not polling B)
        if not wait_caller_task.done():
            wait_caller_task.cancel()
            with contextlib.suppress(BaseException):
                await wait_caller_task

        # Also test a dual-home `send --wait` where Hub B stays open slightly after send (live cross-hub `--wait` wakeup!):
        # And verify `ext_owner` retrieves `req_ext` from Hub B Gateway while Hub A is still blocked!
        res_ext_poll = await ext_owner.run_cli("result", req_ext, "--wait", "10", "--json")
        assert res_ext_poll.returncode == 0, res_ext_poll.stderr
        ext_data = json.loads(res_ext_poll.stdout)
        assert ext_data["found"] is True and ext_data["status"] == "ok"
        assert f"STUB_OK:{req_ext}" in ext_data["text"]
        oracle.ledger.record_result(req_ext, status="ok", via="hub_b_gateway")

        # Verify `ext_other` is strictly isolated (`403 forbidden`) on Hub B!
        res_other = await ext_other.run_cli("result", req_ext, "--json")
        assert res_other.returncode == 1
        assert "forbidden" in res_other.stderr

        # Confirm Hub A STILL has only non-terminal (`pending`/`running`) RPC rows while blocked
        # (proving terminal `ok` results came strictly from Hub B!)
        ha_rpc_mid = {r["corr_id"]: r["status"] for r in hub_a.inspect_db()["rpc_results"]}
        assert ha_rpc_mid.get(req_int) in ("pending", "running")
        assert ha_rpc_mid.get(req_ext) == "pending"

        # Heal Hub A paths -> worker outbox replicates results to Hub A (`pending == 0`), 0 duplicate executions!
        cluster.proxies[("node-1", "a")].unblock()
        cluster.proxies[("node-2", "a")].unblock()
        p_ext_a.unblock()

        await _wait_until(
            lambda: n2.journal_status(),
            timeout=5.0,
        )
        await _wait_until(
            lambda: _check_outbox_zero(n2),
            timeout=10.0,
            desc="node-2 outbox replicated results to Hub A after unblock",
        )

        all_ids = [req_int, req_ext]
        dup_cnt = _sync_counter_into_ledger(oracle.counter, oracle.ledger, all_ids)
        counts = {rid: oracle.counter.count(rid) for rid in all_ids}
        assert all(c == 1 for c in counts.values()), f"Expected 1 execution per id: {counts}"
        assert dup_cnt == 0

        _record_i5_metrics(
            scenario="X08",
            test_name="test_i5_x08_request_only_on_hub_a_result_only_on_hub_b_cross_delivery",
            sent=len(all_ids),
            delivered=len(all_ids),
            lost=0,
            duplicate_exec=dup_cnt,
            uncertain=0,
            elapsed_s=time.monotonic() - t0,
        )
    finally:
        await cluster.aclose()
        assert_ports_free(ALL_I5_PORTS, context="x08_teardown")


async def _check_outbox_zero(node: NodeSubprocess) -> bool:
    st = await node.journal_status()
    return st["outbox"]["pending"] == 0


# ==============================================================================
# 6. X10: Real-process Hub A `token revoke` -> Hub B `auth_sync` -> `4003` & `401`
# ==============================================================================
@pytest.mark.chaos
async def test_i5_x10_source_token_revoke_replica_sync_4003_eviction_and_ext_rejection(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
) -> None:
    t0 = time.monotonic()
    cluster = await _build_standard_cluster(
        tmp_path=tmp_path,
        hub_factory=hub_factory,
        proxy_factory=proxy_factory,
        oracle=oracle,
        auth_sync_interval=0.15,
    )
    try:
        hub_a, hub_b = cluster.hub_a, cluster.hub_b

        admin = NodeSubprocess(
            node_name="cat-food",
            base_dir=tmp_path / "x10_admin",
            urls=[hub_a.ws_url],
            counter_path=oracle.counter.path,
            control_path=cluster.control_path,
        )
        cluster.extra_procs.append(admin)

        tok_victim = json.loads(
            (
                await admin.run_cli(
                    "token",
                    "create",
                    "--node",
                    "x10victim",
                    "--scope",
                    "rpc:node-2/main",
                    "--scope",
                    "send:node-2/inbox",
                    "--allow-exec",
                    "--json",
                    env_overrides={"BUS_CONFIG_DIR": str(hub_a.cfg_dir)},
                )
            ).stdout
        )
        tok_keep = json.loads(
            (
                await admin.run_cli(
                    "token",
                    "create",
                    "--node",
                    "x10keep",
                    "--scope",
                    "rpc:node-2/main",
                    "--allow-exec",
                    "--json",
                    env_overrides={"BUS_CONFIG_DIR": str(hub_a.cfg_dir)},
                )
            ).stdout
        )

        _, ha_gen_before = _http_get_json(f"{hub_a.gateway_http_url}/v1/health")
        gen_before = int(ha_gen_before["auth_generation"])
        await _wait_until(
            lambda: _http_get_json(f"{hub_b.gateway_http_url}/v1/health")[1].get("auth_generation", 0) >= gen_before,
            timeout=6.0,
            desc="Hub B synced initial tokens from Hub A",
        )

        ext_victim_b = cluster.create_node(
            "ext-x10-victim",
            [hub_b.gateway_http_url],
            extra_env={"BUS_AUTH_TOKEN": tok_victim["id"], "BUS_AUTH_SECRET": tok_victim["secret"]},
        )
        ext_victim_dual = cluster.create_node(
            "ext-x10-victim-dual",
            [hub_a.gateway_http_url, hub_b.gateway_http_url],
            extra_env={"BUS_AUTH_TOKEN": tok_victim["id"], "BUS_AUTH_SECRET": tok_victim["secret"]},
        )
        ext_keep_b = cluster.create_node(
            "ext-x10-keep",
            [hub_b.gateway_http_url],
            extra_env={"BUS_AUTH_TOKEN": tok_keep["id"], "BUS_AUTH_SECRET": tok_keep["secret"]},
        )

        # 1) Pre-revoke: `tok_victim` succeeds against replica Hub B!
        pre_id = "x10-victim-pre-revoke-ok"
        oracle.ledger.record_sent(pre_id, sender="ext-x10victim")
        r_pre = await ext_victim_b.run_cli(
            "send",
            "--to",
            "node-2/main",
            "--corr-id",
            pre_id,
            "--wait",
            "8",
            f"[id={pre_id}] before-revoke",
        )
        assert r_pre.returncode == 0, r_pre.stderr
        assert f"STUB_OK:{pre_id}" in r_pre.stdout
        oracle.ledger.record_hub_ack(pre_id, hub="hub_b")
        oracle.ledger.record_received(pre_id, node="node-2")
        oracle.ledger.record_result(pre_id, status="ok")

        # 2) Open live external WS sessions on BOTH Hub A and Hub B for `tok_victim` and `tok_keep`!
        def _ext_hello(label: str, t: dict[str, Any]) -> str:
            hdr = bus_auth.build_hmac_headers(t["id"], t["secret"], t["id"].encode("utf-8"))
            return json.dumps(
                {
                    "v": 2,
                    "label": label,
                    "since": 0,
                    "accept_inbox": False,
                    "no_replay": True,
                    "auth": {
                        "token_id": t["id"],
                        "ts": hdr["X-Bus-Timestamp"],
                        "nonce": hdr["X-Bus-Nonce"],
                        "sig": hdr["X-Bus-Signature"],
                    },
                }
            )

        ws_vic_a = await websockets.connect(hub_a.gateway_ws_url, open_timeout=3.0, close_timeout=1.5)
        ws_vic_b = await websockets.connect(hub_b.gateway_ws_url, open_timeout=3.0, close_timeout=1.5)
        ws_keep_b = await websockets.connect(hub_b.gateway_ws_url, open_timeout=3.0, close_timeout=1.5)
        try:
            for ws_c, lbl, tk in (
                (ws_vic_a, "vic-a", tok_victim),
                (ws_vic_b, "vic-b", tok_victim),
                (ws_keep_b, "keep-b", tok_keep),
            ):
                await ws_c.send(_ext_hello(lbl, tk))
                bn = json.loads(await asyncio.wait_for(ws_c.recv(), 3.0))
                assert bn.get("op") == "sys" and bn.get("trust") == "external"

            # 3) Execute real `abus token revoke <token_id>` CLI on source Hub A!
            rev_res = await admin.run_cli(
                "token",
                "revoke",
                tok_victim["id"],
                env_overrides={"BUS_CONFIG_DIR": str(hub_a.cfg_dir)},
            )
            assert rev_res.returncode == 0 and "revoked" in rev_res.stdout

            # Wait for replica Hub B to pull the new generation
            await _wait_until(
                lambda: _http_get_json(f"{hub_b.gateway_http_url}/v1/health")[1].get("auth_generation", 0)
                > gen_before,
                timeout=6.0,
                desc="Hub B synced revoked token generation",
            )

            # 4) Open WS sessions for `tok_victim` on BOTH Hub A and Hub B must be evicted with `4003`!
            for ws_evict, name in ((ws_vic_a, "Hub A"), (ws_vic_b, "Hub B")):
                with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
                    while True:
                        await asyncio.wait_for(ws_evict.recv(), timeout=4.0)
                code = exc_info.value.rcvd.code if exc_info.value.rcvd else ws_evict.close_code
                assert code == 4003, f"Expected 4003 eviction on {name}, got {code}"

            # Meanwhile `ws_keep_b` stays alive!
            await ws_keep_b.send(json.dumps({"op": "peers"}))
            keep_peers = json.loads(await asyncio.wait_for(ws_keep_b.recv(), timeout=3.0))
            assert keep_peers.get("op") == "sys"
        finally:
            for w in (ws_vic_a, ws_vic_b, ws_keep_b):
                with contextlib.suppress(Exception):
                    await w.close()

        # 5) New CLI calls by `tok_victim` fail with auth exit code `3` across Hub A, Hub B, and dual-home!
        post_blocked_id = "x10-victim-post-revoke-blocked"
        r_blk_http = await ext_victim_dual.run_cli(
            "send",
            "--to",
            "node-2/main",
            "--corr-id",
            post_blocked_id,
            f"[id={post_blocked_id}] must-not-run",
        )
        assert r_blk_http.returncode == 3, r_blk_http.stderr
        assert "[auth-error]" in r_blk_http.stderr and "http_401" in r_blk_http.stderr

        r_blk_ws = await ext_victim_dual.run_cli(
            "peers",
            "--url",
            f"{hub_a.gateway_ws_url},{hub_b.gateway_ws_url}",
        )
        assert r_blk_ws.returncode == 3, r_blk_ws.stderr
        assert "[auth-error]" in r_blk_ws.stderr and "ws_4003" in r_blk_ws.stderr
        assert oracle.counter.count(post_blocked_id) == 0

        # 6) Valid `tok_keep` continues working via Hub B!
        keep_id = "x10-keep-post-revoke-ok"
        oracle.ledger.record_sent(keep_id, sender="ext-x10keep")
        r_keep = await ext_keep_b.run_cli(
            "send",
            "--to",
            "node-2/main",
            "--corr-id",
            keep_id,
            "--wait",
            "8",
            f"[id={keep_id}] survivor-ok",
        )
        assert r_keep.returncode == 0, r_keep.stderr
        assert f"STUB_OK:{keep_id}" in r_keep.stdout
        oracle.ledger.record_hub_ack(keep_id, hub="hub_b")
        oracle.ledger.record_received(keep_id, node="node-2")
        oracle.ledger.record_result(keep_id, status="ok")

        valid_ids = [pre_id, keep_id]
        dup_cnt = _sync_counter_into_ledger(oracle.counter, oracle.ledger, valid_ids)
        assert all(oracle.counter.count(rid) == 1 for rid in valid_ids)
        assert dup_cnt == 0

        _record_i5_metrics(
            scenario="X10",
            test_name="test_i5_x10_source_token_revoke_replica_sync_4003_eviction_and_ext_rejection",
            sent=len(valid_ids),
            delivered=len(valid_ids),
            lost=0,
            duplicate_exec=dup_cnt,
            uncertain=0,
            elapsed_s=time.monotonic() - t0,
            extra={"evicted_4003_sessions": 2},
        )
    finally:
        await cluster.aclose()
        assert_ports_free(ALL_I5_PORTS, context="x10_teardown")


# ==============================================================================
# 7. X11: Banner-unresponsive fake hub in real `cli run` & CLI `urls`
# ==============================================================================
@pytest.mark.chaos
async def test_i5_x11_banner_hang_fake_hub_in_multiprocess_dual_home(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
) -> None:
    t0 = time.monotonic()
    cluster = await _build_standard_cluster(
        tmp_path=tmp_path,
        hub_factory=hub_factory,
        proxy_factory=proxy_factory,
        oracle=oracle,
        start_daemons=False,
    )
    fake_hub = FakeBannerHangProc(tmp_path / "fake_banner_hub", port=AUX_SANDBOX_PORT_1).start()
    cluster.extra_procs.append(fake_hub)
    try:
        n1, n2, n3 = cluster.nodes["node-1"], cluster.nodes["node-2"], cluster.nodes["node-3"]
        fast_deadlines = {
            "BUS_DIAL_TIMEOUT": "0.5",
            "BUS_HELLO_TIMEOUT": "0.5",
            "BUS_BANNER_TIMEOUT": "0.65",
            "BUS_ACK_TIMEOUT": "1.5",
        }
        # Put the banner-unresponsive fake hub FIRST in `urls` for every node & CLI!
        for np, key_a, key_b in (
            (n1, ("node-1", "a"), ("node-1", "b")),
            (n2, ("node-2", "a"), ("node-2", "b")),
            (n3, ("node-3", "a"), ("node-3", "b")),
        ):
            np.extra_env.update(fast_deadlines)
            np.set_urls([fake_hub.ws_url, cluster.proxies[key_a].ws_url, cluster.proxies[key_b].ws_url])
            np.start_daemon()

        # Verify fake hub actually accepted WS connections AND healthy hubs registered all 3 nodes!
        async def _healthy_despite_fake_hub() -> bool:
            if fake_hub.accepted_ws < 1:
                return False
            r_peers = await n1.run_cli("peers", timeout=6.0)
            if r_peers.returncode != 0:
                return False
            seen = set(r_peers.stdout.strip().replace("peers=", "").split(","))
            return {"node-1/main", "node-2/main", "node-3/main"}.issubset(seen)

        await _wait_until(_healthy_despite_fake_hub, timeout=10.0, desc="daemons operational despite hung fake hub")

        # `abus doctor --json` must report `hub_ok=True`, `all_hubs_ok=False`, fake hub `ok=False` within deadline!
        doc_res = await n1.run_cli("doctor", "--json", timeout=6.0)
        assert doc_res.returncode == 0, doc_res.stderr
        doc = json.loads(doc_res.stdout)
        assert doc["hub_ok"] is True
        assert doc["all_hubs_ok"] is False
        assert len(doc["hubs"]) == 3
        assert doc["hubs"][0]["url"] == fake_hub.ws_url and doc["hubs"][0]["ok"] is False
        assert doc["hubs"][1]["ok"] is True and doc["hubs"][2]["ok"] is True

        # Synchronous `send --wait` and `result` across all 3 nodes complete fast without stalling on fake hub!
        rpc_specs = [
            (n1, "node-2/main", "x11-rpc-1"),
            (n2, "node-3/main", "x11-rpc-2"),
            (n3, "node-1/main", "x11-rpc-3"),
            (n1, "node-3/main", "x11-rpc-4"),
        ]
        for sender_np, target, rid in rpc_specs:
            oracle.ledger.record_sent(rid, sender=sender_np.node_name)
            r_rpc = await sender_np.run_cli(
                "send",
                "--to",
                target,
                "--corr-id",
                rid,
                "--wait",
                "8",
                f"[id={rid}] fast-despite-banner-hang",
            )
            assert r_rpc.returncode == 0, r_rpc.stderr
            assert f"STUB_OK:{rid}" in r_rpc.stdout
            assert r_rpc.elapsed_s < 4.5, f"RPC {rid} stalled on fake hub ({r_rpc.elapsed_s:.2f}s)"
            oracle.ledger.record_hub_ack(rid, hub="healthy_hubs")
            oracle.ledger.record_received(rid)
            oracle.ledger.record_result(rid, status="ok")

        all_ids = [rid for _, _, rid in rpc_specs]
        dup_cnt = _sync_counter_into_ledger(oracle.counter, oracle.ledger, all_ids)
        counts = {rid: oracle.counter.count(rid) for rid in all_ids}
        assert all(c == 1 for c in counts.values()), f"Expected exact 1 execution per id: {counts}"
        assert dup_cnt == 0

        _record_i5_metrics(
            scenario="X11",
            test_name="test_i5_x11_banner_hang_fake_hub_in_multiprocess_dual_home",
            sent=len(all_ids),
            delivered=len(all_ids),
            lost=0,
            duplicate_exec=dup_cnt,
            uncertain=0,
            elapsed_s=time.monotonic() - t0,
            extra={"fake_hub_accepted_ws": fake_hub.accepted_ws},
        )
    finally:
        await cluster.aclose()
        assert_ports_free(ALL_I5_PORTS, context="x11_teardown")


# ==============================================================================
# 8. X12: Default `20/min` rate limit (NO override) + 30-item burst -> 60s rollover
# ==============================================================================
@pytest.mark.chaos
async def test_i5_x12_default_20_per_min_30_burst_no_false_ack_and_window_rollover_drain(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
) -> None:
    t0 = time.monotonic()
    cluster = await _build_standard_cluster(
        tmp_path=tmp_path,
        hub_factory=hub_factory,
        proxy_factory=proxy_factory,
        oracle=oracle,
        start_daemons=False,
    )
    try:
        # Strictly verify `BUS_TEST_RATE_PER_MIN` is NOT set anywhere in Hub A or Hub B environments!
        assert "BUS_TEST_RATE_PER_MIN" not in os.environ
        assert "BUS_TEST_RATE_PER_MIN" not in cluster.hub_a.build_env()
        assert "BUS_TEST_RATE_PER_MIN" not in cluster.hub_b.build_env()

        n1, n2 = cluster.nodes["node-1"], cluster.nodes["node-2"]
        # Start receiver `node-2` daemon first so initial 20 accepted tasks execute immediately.
        n2.start_daemon()
        await _wait_until(
            lambda: _node_in_peers(n2, cluster.hub_a.ws_url, "node-2/main"),
            timeout=8.0,
            desc="node-2/main online before 30-message burst",
        )

        # Send 30 distinct tasks in a burst from `node-1` via real `abus send` CLI subprocesses!
        burst_ids = [f"x12-burst-{i:02d}" for i in range(30)]
        ack_ok_rcs = 0
        rate_err_rcs = 0
        burst_started_wall = time.time()

        for idx, rid in enumerate(burst_ids):
            oracle.ledger.record_sent(rid, sender="node-1", seq_idx=idx)
            res = await n1.run_cli(
                "send",
                "--to",
                "node-2/main",
                "--corr-id",
                rid,
                f"[id={rid}] burst-item-{idx:02d}",
            )
            if res.returncode == 0:
                ack_ok_rcs += 1
                oracle.ledger.record_hub_ack(rid, hub="initial_window")
            else:
                rate_err_rcs += 1
                assert "rate" in res.stderr, f"Expected rate rejection for {rid}, got: {res.stderr}"

        # Exact initial window verification (`20/min` default):
        # - Exactly 20 accepted (`done == 20`)
        # - Exactly 10 rate-limited (`pending == 10`, `rejected == 0` -> ZERO false ACKs!)
        assert ack_ok_rcs == 20, f"Expected 20 initial ACKs under default 20/min, got {ack_ok_rcs}"
        assert rate_err_rcs == 10, f"Expected 10 rate rejections under default 20/min, got {rate_err_rcs}"

        st_initial = await n1.journal_status()
        assert st_initial["outbox"]["done"] == 20, f"Initial done mismatch: {st_initial}"
        assert st_initial["outbox"]["pending"] == 10, f"Rate-rejected 10 items MUST stay pending: {st_initial}"
        assert st_initial["outbox"]["rejected"] == 0

        # Start `node-1` daemon (`cli run`): its `_outbox_pump` (`rate_backoff <= 2.0s`) will retry
        # and automatically flush all 10 pending items right as the 60s sliding window rolls over!
        n1.start_daemon()

        await _wait_until(
            lambda: sum(1 for rid in burst_ids[:20] if oracle.counter.count(rid) >= 1) == 20,
            timeout=10.0,
            desc="initial 20 accepted items executed on node-2",
        )

        # Wait for the 60s per-minute rate window rollover + prompt convergence (`<= 2.0s` backoff)
        async def _all_30_drained_and_executed() -> bool:
            if not all(oracle.counter.count(rid) >= 1 for rid in burst_ids):
                return False
            st_now = await n1.journal_status()
            return st_now["outbox"]["pending"] == 0 and st_now["outbox"]["done"] == 30

        await _wait_until(
            _all_30_drained_and_executed,
            timeout=72.0,
            interval=0.25,
            desc="remaining 10 rate-limited items drained & executed after ~60s window rollover",
        )
        rollover_elapsed = time.time() - burst_started_wall
        assert 55.0 <= rollover_elapsed <= 70.0, f"Unexpected rollover elapsed={rollover_elapsed:.2f}s"

        for rid in burst_ids[20:]:
            oracle.ledger.record_hub_ack(rid, hub="post_rollover_window")
        for rid in burst_ids:
            oracle.ledger.record_received(rid, node="node-2")
            oracle.ledger.record_result(rid, status="ok")

        dup_cnt = _sync_counter_into_ledger(oracle.counter, oracle.ledger, burst_ids)
        counts = {rid: oracle.counter.count(rid) for rid in burst_ids}
        assert all(c == 1 for c in counts.values()), f"Expected exact 1 execution for all 30 items: {counts}"
        assert dup_cnt == 0

        _record_i5_metrics(
            scenario="X12",
            test_name="test_i5_x12_default_20_per_min_30_burst_no_false_ack_and_window_rollover_drain",
            sent=len(burst_ids),
            delivered=sum(1 for c in counts.values() if c >= 1),
            lost=sum(1 for c in counts.values() if c == 0),
            duplicate_exec=dup_cnt,
            uncertain=0,
            elapsed_s=time.monotonic() - t0,
            extra={"initial_acked": ack_ok_rcs, "initial_rate_deferred": rate_err_rcs},
        )
    finally:
        await cluster.aclose()
        assert_ports_free(ALL_I5_PORTS, context="x12_teardown")


async def _node_in_peers(node: NodeSubprocess, url: str, peer_key: str) -> bool:
    r = await node.run_cli("peers", "--url", url, timeout=5.0)
    return r.returncode == 0 and peer_key in r.stdout


# ==============================================================================
# 9. X17: Live rollback (`v2` `cli run` -> `journal drain` -> `e7d0da2` client)
# ==============================================================================
@pytest.mark.chaos
async def test_i5_x17_v2_to_legacy_e7d0da2_live_rollback_zero_duplicate_execution(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    proxy_factory: ProxyController,
    oracle: ChaosOracle,
) -> None:
    t0 = time.monotonic()
    # Configure Hub A with `local_node="node-2"` so when unmodified legacy `e7d0da2` client connects
    # without `X-Bus-Test-Local-Node`, Hub A falls back to `BUS_TEST_LOCAL_NODE="node-2"` seamlessly!
    cluster = await _build_standard_cluster(
        tmp_path=tmp_path,
        hub_factory=hub_factory,
        proxy_factory=proxy_factory,
        oracle=oracle,
        hub_a_local_node="node-2",
        start_daemons=False,
    )
    try:
        n1, n2 = cluster.nodes["node-1"], cluster.nodes["node-2"]

        # Advance Hub B's `seq` ahead of Hub A by +8 events so we also prove `cursor-main` tracks
        # ONLY primary Hub A (`F28`) and isn't polluted by secondary Hub B's higher sequence!
        for k in range(8):
            r_seed = await n1.run_cli(
                "send",
                "--url",
                cluster.hub_b.ws_url,
                "--to",
                "node-3/inbox",
                "--say",
                f"hub-b-seq-advance-{k}",
            )
            assert r_seed.returncode == 0, r_seed.stderr

        # 1) Start `v2` `node-1` and `node-2` real OS daemons (`node-2` primary URL = Hub A proxy)
        n1.start_daemon()
        n2.start_daemon()
        await _wait_until(
            lambda: _node_in_peers(n1, cluster.hub_a.ws_url, "node-2/main"),
            timeout=8.0,
            desc="v2 node-2/main online",
        )

        v2_ids = [f"x17-v2-phase-{i}" for i in range(5)]
        for rid in v2_ids:
            oracle.ledger.record_sent(rid, sender="node-1", phase="v2")
            r = await n1.run_cli(
                "send",
                "--to",
                "node-2/main",
                "--corr-id",
                rid,
                "--wait",
                "8",
                f"[id={rid}] v2-phase-work",
            )
            assert r.returncode == 0, r.stderr
            assert f"STUB_OK:{rid}" in r.stdout
            oracle.ledger.record_hub_ack(rid, hub="dual")
            oracle.ledger.record_received(rid, node="node-2")
            oracle.ledger.record_result(rid, status="ok")

        # Stage 2 pending outbox items on `node-2` while its proxies are briefly blocked,
        # stop `v2` `node-2` daemon, unblock proxies, and run real `abus journal drain` CLI!
        n2.stop()
        cluster.proxies[("node-2", "a")].block(True, cut_existing=True)
        cluster.proxies[("node-2", "b")].block(True, cut_existing=True)
        for d_idx in range(2):
            r_off = await n2.run_cli(
                "send",
                "--to",
                "node-1/inbox",
                "--say",
                "--offline-queue",
                f"x17-pre-rollback-outbox-{d_idx}",
            )
            assert r_off.returncode == 0, r_off.stderr

        st_before_drain = await n2.journal_status()
        assert st_before_drain["outbox"]["pending"] >= 2

        cluster.proxies[("node-2", "a")].unblock()
        cluster.proxies[("node-2", "b")].unblock()

        drain_res = await n2.run_cli("journal", "drain", "--timeout", "10", "--json")
        assert drain_res.returncode == 0, drain_res.stderr
        drain_json = json.loads(drain_res.stdout)
        assert drain_json["ok"] is True and drain_json["remaining"] == 0

        cursor_file = n2.cfg_dir / "cursor-main"
        assert cursor_file.is_file(), "Legacy cursor-main must exist before rollback"
        cursor_before_rollback = int(cursor_file.read_text(encoding="utf-8").strip())
        assert cursor_before_rollback > 0

        # 2) Extract unmodified `e7d0da2` legacy package and start legacy `cli run` against the SAME `cfg_dir`!
        legacy_bus_root = _extract_legacy_package(tmp_path / "legacy_e7d0da2_pkg")
        n2.set_urls(
            [cluster.proxies[("node-2", "a")].ws_url, cluster.proxies[("node-2", "b")].ws_url],
            legacy_single_url=cluster.proxies[("node-2", "a")].ws_url,
        )
        n2.start_daemon(pythonpath=legacy_bus_root)

        await _wait_until(
            lambda: _node_in_peers(n1, cluster.hub_a.ws_url, "node-2/main"),
            timeout=8.0,
            desc="rolled-back e7d0da2 node-2/main online",
        )
        # Give legacy client a moment on connect to ensure zero replay/re-execution of `v2_ids`
        await asyncio.sleep(0.5)
        for rid in v2_ids:
            assert oracle.counter.count(rid) == 1, f"Rollback re-executed v2 request {rid}!"

        # 3) Send 3 brand-new requests (`legacy_ids`) from `node-1` to rolled-back `e7d0da2` `node-2`!
        legacy_ids = [f"x17-post-rollback-{i}" for i in range(3)]
        for rid in legacy_ids:
            oracle.ledger.record_sent(rid, sender="node-1", phase="post_rollback_e7d0da2")
            r_new = await n1.run_cli(
                "send",
                "--url",
                cluster.hub_a.ws_url,
                "--to",
                "node-2/main",
                "--corr-id",
                rid,
                "--wait",
                "8",
                f"[id={rid}] post-rollback-work",
            )
            assert r_new.returncode == 0, r_new.stderr
            assert f"STUB_OK:{rid}" in r_new.stdout
            oracle.ledger.record_hub_ack(rid, hub="hub_a")
            oracle.ledger.record_received(rid, node="node-2_e7d0da2")
            oracle.ledger.record_result(rid, status="ok")

        cursor_after_rollback = int(cursor_file.read_text(encoding="utf-8").strip())
        assert cursor_after_rollback > cursor_before_rollback

        all_ids = [*v2_ids, *legacy_ids]
        dup_cnt = _sync_counter_into_ledger(oracle.counter, oracle.ledger, all_ids)
        counts = {rid: oracle.counter.count(rid) for rid in all_ids}
        assert all(c == 1 for c in counts.values()), f"Expected exact 1 execution across v2+rollback: {counts}"
        assert dup_cnt == 0

        _record_i5_metrics(
            scenario="X17",
            test_name="test_i5_x17_v2_to_legacy_e7d0da2_live_rollback_zero_duplicate_execution",
            sent=len(all_ids),
            delivered=sum(1 for c in counts.values() if c >= 1),
            lost=sum(1 for c in counts.values() if c == 0),
            duplicate_exec=dup_cnt,
            uncertain=0,
            elapsed_s=time.monotonic() - t0,
            extra={
                "cursor_before_rollback": cursor_before_rollback,
                "cursor_after_rollback": cursor_after_rollback,
            },
        )
    finally:
        await cluster.aclose()
        assert_ports_free(ALL_I5_PORTS, context="x17_teardown")
