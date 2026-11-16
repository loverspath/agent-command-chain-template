"""Deterministic sandbox hub process controller for agent_bus chaos tests.

Safety & Isolation Guarantees:
- Dynamically resolves PYTHONPATH to the worktree's `bus/` directory
  (`Path(__file__).resolve().parents[2]`, i.e., `/home/rerun/abus-wt/bus`).
- Refuses to bind or touch production ports (:8765, :8766) or peer worker port (:18769).
- Isolates HOME, BUS_CONFIG_DIR, BUS_DATA_DIR, BUS_DB_PATH, BUS_LOG_DIR, and BUS_MODE_FILE
  under per-test / per-instance temporary directories.
- Injects a fail-closed `BUS_CLAUDE_CMD` guard script so child hub/CLI processes can
  never invoke the real `claude` CLI.
- Tracks exact `subprocess.Popen.pid` handles (never uses pkill/killall) and guarantees
  `SIGCONT` recovery before termination if a hub was paused with `SIGSTOP`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any, Iterable

BUS_ROOT = pathlib.Path(__file__).resolve().parents[2]
if not (BUS_ROOT / "agent_bus" / "__init__.py").is_file():
    raise RuntimeError(f"Expected agent_bus package under BUS_ROOT={BUS_ROOT}")

if str(BUS_ROOT) not in sys.path:
    sys.path.insert(0, str(BUS_ROOT))

from agent_bus import auth as bus_auth  # noqa: E402

HUB_HOST = "127.0.0.1"
HUB_A_PORT = 18765
HUB_B_PORT = 18775
FORBIDDEN_PORTS = frozenset({8765, 8766, 18769})
_RESERVED_SANDBOX_PORTS = frozenset({HUB_A_PORT, HUB_B_PORT, 18785, 18786, 18799})
_RECENT_PICKED_PORTS: list[int] = []


def validate_sandbox_port(port: int, *, allow_zero: bool = False) -> None:
    if port == 0 and allow_zero:
        return
    if port in FORBIDDEN_PORTS:
        raise ValueError(f"Port {port} is strictly forbidden in chaos tests")
    if not (1024 <= port <= 65535):
        raise ValueError(f"Port {port} is outside unprivileged sandbox range")


def pick_free_port(host: str = HUB_HOST) -> int:
    for _ in range(60):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, 0))
            port = int(sock.getsockname()[1])
        if (
            port not in FORBIDDEN_PORTS
            and port not in _RESERVED_SANDBOX_PORTS
            and port not in _RECENT_PICKED_PORTS
        ):
            _RECENT_PICKED_PORTS.append(port)
            if len(_RECENT_PICKED_PORTS) > 64:
                del _RECENT_PICKED_PORTS[:-64]
            return port
    raise RuntimeError("Could not allocate a free non-forbidden loopback port")


def is_port_listening(host: str, port: int, timeout: float = 0.15) -> bool:
    if port <= 0:
        return False
    validate_sandbox_port(port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def assert_ports_free(
    ports: Iterable[int],
    context: str = "preflight",
    host: str = HUB_HOST,
    *,
    wait_timeout: float = 0.8,
) -> None:
    port_list = [p for p in ports if p > 0]
    deadline = time.monotonic() + max(0.0, wait_timeout)
    while True:
        busy = [p for p in port_list if is_port_listening(host, p, timeout=0.05)]
        if not busy or time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    if busy:
        raise RuntimeError(f"[{context}] Sandbox port(s) {busy} on {host} are already listening")


def write_claude_guard_script(guard_dir: pathlib.Path) -> pathlib.Path:
    """Create an executable fail-closed `claude` guard script to block real CLI calls."""
    guard_dir.mkdir(parents=True, exist_ok=True)
    guard_path = guard_dir / "claude"
    guard_path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.stderr.write('FATAL: Unstubbed claude CLI execution blocked by chaos guard!\\n')\n"
        "sys.exit(99)\n",
        encoding="utf-8",
    )
    guard_path.chmod(guard_path.stat().st_mode | stat.S_IRWXU)
    return guard_path


def read_linux_proc_state(pid: int) -> str:
    """Read 1-letter process state ('S', 'R', 'T', 'Z', ...) from /proc/<pid>/stat."""
    stat_file = pathlib.Path(f"/proc/{pid}/stat")
    if not stat_file.exists():
        return ""
    try:
        raw = stat_file.read_text(encoding="utf-8", errors="replace")
        after_comm = raw.rsplit(")", 1)[1].strip()
        return after_comm.split()[0]
    except Exception:
        return ""


@dataclasses.dataclass
class HubInstance:
    """Controls a single isolated sandbox `agent_bus` hub subprocess by exact PID."""

    name: str
    base_dir: pathlib.Path
    port: int = HUB_A_PORT
    gateway_port: int = 0
    host: str = HUB_HOST
    local_node: str = "cat-food"
    allowed: tuple[str, ...] = ("cat-food", "mother-goose", "z-fold8", "sbxnode")
    extra_env: dict[str, str] = dataclasses.field(default_factory=dict)

    proc: subprocess.Popen[str] | None = dataclasses.field(default=None, init=False, repr=False)
    _stdout_fh: Any = dataclasses.field(default=None, init=False, repr=False)
    _stderr_fh: Any = dataclasses.field(default=None, init=False, repr=False)
    _stopped: bool = dataclasses.field(default=False, init=False)
    last_returncode: int | None = dataclasses.field(default=None, init=False)
    stopped_via: str = dataclasses.field(default="", init=False)

    def __post_init__(self) -> None:
        validate_sandbox_port(self.port)
        validate_sandbox_port(self.gateway_port, allow_zero=True)
        self.base_dir = pathlib.Path(self.base_dir).resolve()
        self.home_dir = self.base_dir / "home"
        self.cfg_dir = self.base_dir / "cfg"
        self.data_dir = self.base_dir / "data"
        self.logs_dir = self.base_dir / "logs"
        self.guard_dir = self.base_dir / "_guard_bin"
        self.db_path = self.data_dir / "bus.db"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for d in (self.home_dir, self.cfg_dir, self.data_dir, self.logs_dir, self.guard_dir):
            d.mkdir(parents=True, exist_ok=True)
        allow_file = self.cfg_dir / "allow.txt"
        if not allow_file.exists():
            self.write_allow(self.allowed)

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def gateway_http_url(self) -> str:
        if self.gateway_port <= 0:
            raise RuntimeError(f"Hub {self.name} was started with gateway_port=0")
        return f"http://{self.host}:{self.gateway_port}/bus"

    @property
    def gateway_ws_url(self) -> str:
        if self.gateway_port <= 0:
            raise RuntimeError(f"Hub {self.name} was started with gateway_port=0")
        return f"ws://{self.host}:{self.gateway_port}/bus/v1/ws"

    def write_allow(self, nodes: Iterable[str]) -> pathlib.Path:
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        items = [str(n).strip().lower() for n in nodes if str(n).strip()]
        p = self.cfg_dir / "allow.txt"
        p.write_text("\n".join(items) + ("\n" if items else ""), encoding="utf-8")
        return p

    def create_token(
        self,
        node_name: str,
        *,
        scopes: list[str] | None = None,
        allow_exec: bool = False,
    ) -> dict[str, Any]:
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        return bus_auth.create_token(self.cfg_dir, node_name, scopes=scopes, allow_exec=allow_exec)

    def revoke_token(self, token_id: str) -> bool:
        return bus_auth.revoke_token(self.cfg_dir, token_id)

    def copy_auth_from(self, other: "HubInstance") -> None:
        """Copy `allow.txt` and `tokens.json` from another sandbox hub instance."""
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        for fname in ("allow.txt", "tokens.json"):
            src = other.cfg_dir / fname
            if src.exists():
                shutil.copy2(src, self.cfg_dir / fname)

    def build_env(self) -> dict[str, str]:
        guard_claude = write_claude_guard_script(self.guard_dir)
        orig_path = os.environ.get("PATH", "/usr/bin:/bin")
        env: dict[str, str] = {}
        for key in ("USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "SYSTEMROOT"):
            if key in os.environ:
                env[key] = os.environ[key]
        env.update(
            {
                "HOME": str(self.home_dir),
                "PATH": f"{self.guard_dir}{os.pathsep}{orig_path}",
                "PYTHONPATH": str(BUS_ROOT),
                "PYTHONUSERBASE": "/home/rerun/.local",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
                "BUS_CONFIG_DIR": str(self.cfg_dir),
                "BUS_DATA_DIR": str(self.data_dir),
                "BUS_DB_PATH": str(self.db_path),
                "BUS_LOG_DIR": str(self.logs_dir),
                "BUS_MODE_FILE": str(self.cfg_dir / "mode"),
                "BUS_TEST_ALLOW_LOCALHOST": "1",
                "BUS_TEST_LOCAL_NODE": self.local_node,
                "BUS_CLAUDE_CMD": str(guard_claude),
            }
        )
        env.update(self.extra_env)
        return env

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def proc_state(self) -> str:
        if self.proc is None or self.proc.poll() is not None:
            return "dead"
        return read_linux_proc_state(self.proc.pid) or "alive"

    def start(
        self,
        *,
        timeout: float = 8.0,
        wipe_db: bool = False,
        wipe_logs: bool = False,
    ) -> "HubInstance":
        if self.is_running():
            raise RuntimeError(f"Hub {self.name} (pid={self.pid}) is already running")

        if wipe_db and self.data_dir.exists():
            shutil.rmtree(self.data_dir)
        if wipe_logs and self.logs_dir.exists():
            shutil.rmtree(self.logs_dir)
        self._ensure_dirs()

        ports_to_check = [self.port] + ([self.gateway_port] if self.gateway_port > 0 else [])
        assert_ports_free(ports_to_check, context=f"start:{self.name}", host=self.host)

        env = self.build_env()
        stdout_path = self.logs_dir / f"{self.name}.stdout.log"
        stderr_path = self.logs_dir / f"{self.name}.stderr.log"
        self._stdout_fh = open(stdout_path, "a", encoding="utf-8")
        self._stderr_fh = open(stderr_path, "a", encoding="utf-8")

        cmd = [
            sys.executable,
            "-m",
            "agent_bus.cli",
            "hub",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--gateway-host",
            self.host,
            "--gateway-port",
            str(self.gateway_port),
        ]
        self.proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(self.base_dir),
            stdout=self._stdout_fh,
            stderr=self._stderr_fh,
            text=True,
        )
        self._stopped = False
        self.last_returncode = None
        self.stopped_via = ""

        self.wait_healthy(timeout=timeout)
        return self

    def wait_healthy(self, *, timeout: float = 8.0) -> None:
        if self.proc is None:
            raise RuntimeError(f"Hub {self.name} has not been started")
        stdout_path = self.logs_dir / f"{self.name}.stdout.log"
        stderr_path = self.logs_dir / f"{self.name}.stderr.log"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc = self.proc.poll()
            if rc is not None:
                self._close_log_handles()
                out_txt = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
                err_txt = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
                raise RuntimeError(
                    f"Hub {self.name} (pid={self.proc.pid}) exited early with rc={rc}.\n"
                    f"stdout:\n{out_txt}\nstderr:\n{err_txt}"
                )
            ws_ok = is_port_listening(self.host, self.port, timeout=0.05)
            gw_ok = self.gateway_port <= 0 or is_port_listening(self.host, self.gateway_port, timeout=0.05)
            if ws_ok and gw_ok:
                return
            time.sleep(0.015)

        raise TimeoutError(
            f"Hub {self.name} (pid={self.proc.pid}) failed health check on "
            f"{self.host}:{self.port} (gw={self.gateway_port}) within {timeout}s"
        )

    def restart(
        self,
        *,
        timeout: float = 8.0,
        wipe_db: bool = False,
        wipe_logs: bool = False,
    ) -> "HubInstance":
        self.stop()
        return self.start(timeout=timeout, wipe_db=wipe_db, wipe_logs=wipe_logs)

    def sigstop(self, *, wait_state: bool = True, timeout: float = 2.0) -> None:
        """Freeze the hub process deterministically via SIGSTOP."""
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError(f"Cannot SIGSTOP non-running hub {self.name}")
        os.kill(self.proc.pid, signal.SIGSTOP)
        self._stopped = True
        if wait_state:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                st = read_linux_proc_state(self.proc.pid)
                if st in ("T", "t") or st == "":
                    break
                time.sleep(0.01)

    def sigcont(self, *, wait_state: bool = True, timeout: float = 2.0) -> None:
        """Resume a SIGSTOPped hub process via SIGCONT."""
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError(f"Cannot SIGCONT non-running hub {self.name}")
        os.kill(self.proc.pid, signal.SIGCONT)
        self._stopped = False
        if wait_state:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                st = read_linux_proc_state(self.proc.pid)
                if st not in ("T", "t"):
                    break
                time.sleep(0.01)

    def sigkill(self, *, wait_timeout: float = 5.0) -> int:
        """Immediately kill the hub process via SIGKILL (safe even when SIGSTOPped)."""
        if self.proc is None:
            return -9
        pid = self.proc.pid
        if self.proc.poll() is None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if self._stopped:
                try:
                    os.kill(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            rc = self.proc.wait(timeout=wait_timeout)
            self.last_returncode = rc
            self.stopped_via = "SIGKILL"
        else:
            self.last_returncode = self.proc.returncode
            if not self.stopped_via:
                self.stopped_via = "already_exited"
        self._stopped = False
        self._close_log_handles()
        self._wait_ports_closed()
        return int(self.last_returncode if self.last_returncode is not None else -9)

    def stop(self, *, timeout: float = 4.0) -> dict[str, Any]:
        """Gracefully terminate the hub (resuming from SIGSTOP first to prevent wait() deadlock)."""
        if self.proc is None:
            return {"name": self.name, "pid": None, "returncode": None, "stopped_via": "not_started"}

        pid = self.proc.pid
        if self.proc.poll() is None:
            st = read_linux_proc_state(pid)
            if self._stopped or st in ("T", "t"):
                try:
                    os.kill(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                self._stopped = False

            self.proc.terminate()
            try:
                rc = self.proc.wait(timeout=timeout)
                self.stopped_via = "SIGTERM"
            except subprocess.TimeoutExpired:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                rc = self.proc.wait(timeout=4.0)
                self.stopped_via = "SIGKILL_FALLBACK"
            self.last_returncode = rc
        else:
            self.last_returncode = self.proc.returncode
            if not self.stopped_via:
                self.stopped_via = "already_exited"

        self._stopped = False
        self._close_log_handles()
        self._wait_ports_closed()
        return {
            "name": self.name,
            "pid": pid,
            "port": self.port,
            "gateway_port": self.gateway_port,
            "returncode": self.last_returncode,
            "stopped_via": self.stopped_via,
            "port_listening": is_port_listening(self.host, self.port),
        }

    def _close_log_handles(self) -> None:
        for fh_attr in ("_stdout_fh", "_stderr_fh"):
            fh = getattr(self, fh_attr, None)
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
                setattr(self, fh_attr, None)

    def _wait_ports_closed(self, timeout: float = 3.0) -> None:
        ports = [self.port] + ([self.gateway_port] if self.gateway_port > 0 else [])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(is_port_listening(self.host, p, timeout=0.05) for p in ports):
                return
            time.sleep(0.02)

    def inspect_db(self, consumer: str | None = None) -> dict[str, Any]:
        """Read-only snapshot of hub SQLite state (`messages`, `deliveries`, `rpc_results`)."""
        if not self.db_path.exists():
            return {"exists": False, "messages": [], "deliveries": [], "rpc_results": []}
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            messages = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY seq ASC")]
            if consumer is not None:
                deliveries = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM deliveries WHERE consumer = ? ORDER BY seq ASC",
                        (consumer,),
                    )
                ]
            else:
                deliveries = [dict(r) for r in conn.execute("SELECT * FROM deliveries ORDER BY seq ASC")]
            rpc_results = [dict(r) for r in conn.execute("SELECT * FROM rpc_results ORDER BY ts ASC")]
            return {
                "exists": True,
                "messages": messages,
                "deliveries": deliveries,
                "rpc_results": rpc_results,
            }
        finally:
            conn.close()


class HubController:
    """Manages multiple `HubInstance`s per test and guarantees PID cleanup on teardown."""

    def __init__(self, root_dir: pathlib.Path, *, default_local_node: str = "cat-food") -> None:
        self.root_dir = pathlib.Path(root_dir).resolve()
        self.default_local_node = default_local_node
        self._hubs: list[HubInstance] = []

    @property
    def hubs(self) -> list[HubInstance]:
        return list(self._hubs)

    def create_hub(
        self,
        name: str,
        *,
        port: int,
        gateway_port: int = 0,
        local_node: str | None = None,
        allowed: Iterable[str] = ("cat-food", "mother-goose", "z-fold8", "sbxnode"),
        base_dir: pathlib.Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> HubInstance:
        inst = HubInstance(
            name=name,
            base_dir=base_dir or (self.root_dir / name),
            port=port,
            gateway_port=gateway_port,
            local_node=local_node or self.default_local_node,
            allowed=tuple(allowed),
            extra_env=dict(extra_env or {}),
        )
        self._hubs.append(inst)
        return inst

    def start_hub(
        self,
        name: str,
        *,
        port: int,
        gateway_port: int = 0,
        local_node: str | None = None,
        allowed: Iterable[str] = ("cat-food", "mother-goose", "z-fold8", "sbxnode"),
        base_dir: pathlib.Path | None = None,
        extra_env: dict[str, str] | None = None,
        timeout: float = 8.0,
    ) -> HubInstance:
        inst = self.create_hub(
            name,
            port=port,
            gateway_port=gateway_port,
            local_node=local_node,
            allowed=allowed,
            base_dir=base_dir,
            extra_env=extra_env,
        )
        return inst.start(timeout=timeout)

    def start_hub_a(
        self,
        *,
        port: int = HUB_A_PORT,
        gateway_port: int = 0,
        local_node: str | None = None,
        timeout: float = 8.0,
    ) -> HubInstance:
        return self.start_hub("hub_a", port=port, gateway_port=gateway_port, local_node=local_node, timeout=timeout)

    def start_hub_b(
        self,
        *,
        port: int = HUB_B_PORT,
        gateway_port: int = 0,
        local_node: str | None = None,
        timeout: float = 8.0,
    ) -> HubInstance:
        return self.start_hub("hub_b", port=port, gateway_port=gateway_port, local_node=local_node, timeout=timeout)

    def close_all(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for inst in reversed(self._hubs):
            try:
                summaries.append(inst.stop())
            except Exception as exc:
                if inst.proc is not None and inst.proc.poll() is None:
                    try:
                        os.kill(inst.proc.pid, signal.SIGKILL)
                        inst.proc.wait(timeout=3.0)
                    except Exception:
                        pass
                inst._close_log_handles()
                summaries.append({"name": inst.name, "pid": inst.pid, "error": str(exc)})
        return summaries
