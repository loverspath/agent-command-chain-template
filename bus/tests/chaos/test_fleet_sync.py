"""Real-OS subprocess integration, chaos, & portability tests for `abus-fleet-sync` (`18900`–`18999`).

Covers all mandatory Fleet specification scenarios:
1. `rev` advance -> `.bak` backup, unknown custom `config.json` keys preserved, `urls` &
   `hub_peers` updated atomically, `fleet-applied.json` (`checkout` + `commit`) written,
   `fleet-ack` persisted on hub, in-place `os.execv` (`pid_before == pid_after`, `orig_argv` &
   `cwd` preserved), automatic dual-hub connection, and zero infinite `execv` loops.
2. `commit` fast-forward (`BUS_FLEET_REPO_DIR` + bare `origin` in `tmp_path`) -> `git fetch` +
   `git merge --ff-only` converges `HEAD == <new_sha>` and `os.execv`s in-place; `--no-auto-update`
   skips commit step (`result == 'ok:no_auto_update'`, `commit_skipped == True`) while applying
   config updates cleanly.
3. Graceful drain while busy (`inbox running == 1`) -> node detects new `fleet` `rev` during a
   held fake `claude` execution, never dies mid-task, delivers `status == 'ok'` RPC result to
   caller, and immediately afterwards converges and `os.execv`s in-place (`pid_before == pid_after`).
4. Safety guards -> dirty tracked file (`blocked:dirty_worktree`), diverged branch
   (`blocked:non_fast_forward`), unsatisfied `min_caps` (`blocked:missing_caps`), 3-attempt
   failure guard (`blocked:commit_not_on_origin` with `attempts == 3` and retry halt until higher
   `rev`), and corrupt/downgraded/empty-URL manifest rejection without `git reset` or `git stash`.
5. Replica hub (`auth_mode='replica'`) `fleet.json` replication (`18905` source -> `18915`
   replica) -> node connected ONLY to replica `18915` converges identically via `os.execv`; direct
   `abus fleet set` on replica `cfg_dir` fails closed (`ReplicaReadOnlyError`); removing
   `fleet.json` on source disables `fleet` on replica.
6. Legacy `e7d0da2` client compatibility -> unmodified `e7d0da2` `run_node` + `peers` + `send`
   (sync RPC, async task, `--say` event) + `result` + `doctor` operate seamlessly against a v2
   hub serving `"fleet"` banners.
7. `run_supervise` (`abus supervise`) coexistence with `os.execv` (zero duplicate child spawns,
   identical child PID across convergence) AND `os.execv` `OSError` fault injection (`blocked:execv-failed:*`
   recorded & reported while current process stays alive serving RPCs).
8. Non-Git checkout (`not (repo_dir / ".git").exists()`, isolated even under a parent directory
   containing `.git`) -> `commit` step skipped with `blocked:not-a-git-repo` while `config.json`
   updates normally and `os.execv` runs once without restart loops; git worktree `.git` file
   detection also verified.
9. Multi-checkout `__file__` auto-detection (`agent-command-chain-template` vs `act`) WITHOUT
   `BUS_FLEET_REPO_DIR` -> only the running checkout fast-forwards while sibling checkout stays
   untouched; shared `cfg_dir` `fleet-applied.json` preserves per-checkout entries and
   `abus fleet status` exposes `checkout`, `commit`, and multi-checkout mismatch diagnostics.
10. Hub `SIGSTOP` (sleep / network freeze) -> WS disconnect -> `SIGCONT` wake -> automatic
    reconnect, manifest convergence, and in-place `os.execv` (`pid_before == pid_after`).
11. Static AST/regex audit across `bus/agent_bus/*.py` verifying zero non-stdlib external
    dependencies and proper guards on all POSIX-specific imports, syscalls, and signal constants,
    plus cross-platform (`Termux` / `Windows`) unit checks.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import json
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

import pytest
import websockets

import agent_bus.config as bus_config
import agent_bus.journal as bus_journal
from agent_bus.client import build_exec_argv
from agent_bus.fleet import converge_fleet_once, has_git_checkout
from agent_bus.journal import NodeJournal

from .hub_proc import (
    BUS_ROOT,
    HubController,
    assert_ports_free,
    validate_sandbox_port,
    write_claude_guard_script,
)
from .oracle import SideEffectCounter

WORKTREE_ROOT = BUS_ROOT.parent
PACKAGE_DIR = BUS_ROOT / "agent_bus"
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

WHOIS_IP_MAP = {
    "127.0.0.1": "cat-food",
    "127.0.0.2": "mother-goose",
    "127.0.0.3": "z-fold8",
}


async def _wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float = 8.0,
    interval: float = 0.03,
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


def _http_get_json(url: str, timeout: float = 3.0) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        exc.close()
        return exc.code, (json.loads(body) if body.strip() else {})


def _isolated_git_env(base_dir: pathlib.Path) -> dict[str, str]:
    git_home = base_dir / "_git_home"
    git_home.mkdir(parents=True, exist_ok=True)
    gitconfig = git_home / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text(
            "[user]\n\tname = Fleet Test\n\temail = fleet-test@example.invalid\n"
            "[init]\n\tdefaultBranch = main\n",
            encoding="utf-8",
        )
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(git_home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


def _git(repo: pathlib.Path, git_env: dict[str, str], *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=git_env,
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    assert res.returncode == 0, (
        f"git {' '.join(args)} in {repo} failed ({res.returncode}):\n"
        f"stdout={res.stdout}\nstderr={res.stderr}"
    )
    return res.stdout.strip()


def _copy_agent_bus_tree(dest_checkout_root: pathlib.Path) -> pathlib.Path:
    """Copy current `bus/agent_bus/*.py` into `<dest_checkout_root>/bus/agent_bus/`."""
    dest_pkg = dest_checkout_root / "bus" / "agent_bus"
    dest_pkg.mkdir(parents=True, exist_ok=True)
    for src_py in sorted(PACKAGE_DIR.glob("*.py")):
        shutil.copy2(src_py, dest_pkg / src_py.name)
    return dest_checkout_root / "bus"


def _create_origin_and_clones(
    root: pathlib.Path,
    clone_names: list[str],
    *,
    with_bus_package: bool = False,
    exact_clone_paths: dict[str, pathlib.Path] | None = None,
) -> tuple[pathlib.Path, pathlib.Path, dict[str, pathlib.Path], dict[str, str], str]:
    """Create a bare `origin.git`, a `publisher` clone, and named node clones at `sha_v1`."""
    git_env = _isolated_git_env(root)
    origin = root / "origin.git"
    origin.mkdir(parents=True, exist_ok=True)
    _git(origin, git_env, "init", "--bare")
    _git(origin, git_env, "symbolic-ref", "HEAD", "refs/heads/main")

    publisher = root / "publisher"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(publisher)],
        env=git_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    _git(publisher, git_env, "checkout", "-B", "main")
    (publisher / "README.md").write_text("fleet repo v1\n", encoding="utf-8")
    if with_bus_package:
        _copy_agent_bus_tree(publisher)
    _git(publisher, git_env, "add", ".")
    _git(publisher, git_env, "commit", "-q", "-m", "v1 initial")
    _git(publisher, git_env, "push", "-q", "-u", "origin", "main")
    sha_v1 = _git(publisher, git_env, "rev-parse", "HEAD")

    clones: dict[str, pathlib.Path] = {}
    for name in clone_names:
        cpath = (
            exact_clone_paths[name]
            if (exact_clone_paths and name in exact_clone_paths)
            else (root / f"clone_{name}")
        )
        cpath.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", "-b", "main", str(origin), str(cpath)],
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        assert _git(cpath, git_env, "rev-parse", "HEAD") == sha_v1
        clones[name] = cpath.resolve()

    return origin, publisher, clones, git_env, sha_v1


def _push_new_commit(
    publisher: pathlib.Path,
    git_env: dict[str, str],
    rel_file: str,
    content: str,
    msg: str,
) -> str:
    fpath = publisher / rel_file
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(content, encoding="utf-8")
    _git(publisher, git_env, "add", rel_file)
    _git(publisher, git_env, "commit", "-q", "-m", msg)
    _git(publisher, git_env, "push", "-q", "origin", "main")
    return _git(publisher, git_env, "rev-parse", "HEAD")


def _write_fake_claude(
    bin_dir: pathlib.Path,
    *,
    counter_path: pathlib.Path | None = None,
    hold_signal_file: pathlib.Path | None = None,
    release_file: pathlib.Path | None = None,
    response_prefix: str = "FLEET_STUB_OK",
) -> pathlib.Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    cpath = bin_dir / "claude"
    cnt_str = repr(str(counter_path.resolve())) if counter_path is not None else "None"
    sig_str = repr(str(hold_signal_file.resolve())) if hold_signal_file is not None else "None"
    rel_str = repr(str(release_file.resolve())) if release_file is not None else "None"
    cpath.write_text(
        f"#!{sys.executable}\n"
        "import fcntl, json, os, pathlib, re, sys, time\n"
        "args = sys.argv[1:]\n"
        "if len(args) >= 2 and args[0] == 'auth' and args[1] == 'status':\n"
        "    sys.stdout.write(json.dumps({'loggedIn': True}) + '\\n')\n"
        "    sys.exit(0)\n"
        "orig_ppid = os.getppid()\n"
        "raw = sys.stdin.read()\n"
        "m = re.search(r'\\[(?:req_)?id=([^\\]\\s]+)\\]', raw)\n"
        "req_id = m.group(1) if m else 'job'\n"
        f"counter_p = {cnt_str}\n"
        "if counter_p:\n"
        "    os.makedirs(os.path.dirname(counter_p), exist_ok=True)\n"
        "    with open(counter_p, 'a+', encoding='utf-8') as fh:\n"
        "        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
        "        fh.seek(0)\n"
        "        seq = sum(1 for ln in fh if ln.strip()) + 1\n"
        "        fh.seek(0, os.SEEK_END)\n"
        "        fh.write(json.dumps({'seq': seq, 'id': req_id, 'ts': time.time(), 'pid': os.getpid(), 'ppid': orig_ppid}) + '\\n')\n"
        "        fh.flush()\n"
        "        os.fsync(fh.fileno())\n"
        "        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)\n"
        f"sig_p = {sig_str}\n"
        f"rel_p = {rel_str}\n"
        "if sig_p:\n"
        "    sp = pathlib.Path(sig_p)\n"
        "    sp.parent.mkdir(parents=True, exist_ok=True)\n"
        "    sp.write_text(json.dumps({'id': req_id, 'pid': os.getpid(), 'ppid': orig_ppid, 'ts': time.time()}), encoding='utf-8')\n"
        "if rel_p:\n"
        "    deadline = time.monotonic() + 10.0\n"
        "    while time.monotonic() < deadline:\n"
        "        if os.getppid() != orig_ppid:\n"
        "            sys.exit(137)\n"
        "        if pathlib.Path(rel_p).exists():\n"
        "            break\n"
        "        time.sleep(0.02)\n"
        f"sys.stdout.write('{response_prefix}:' + req_id + ':ppid=' + str(orig_ppid) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    cpath.chmod(0o755)
    return cpath


def _write_whois_script(dir_path: pathlib.Path) -> pathlib.Path:
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
            timeout=10.0,
        )
        (pkg_dir / fname).write_text(res.stdout, encoding="utf-8")
    return dest_bus_root


class ManagedNodeProc:
    """Real OS `python3 -m agent_bus.cli run` subprocess controller with deterministic cleanup."""

    def __init__(
        self,
        *,
        node_name: str,
        base_dir: pathlib.Path,
        repo_dir: pathlib.Path | None = None,
        bus_root: pathlib.Path = BUS_ROOT,
        cfg_dir_override: pathlib.Path | None = None,
        extra_env: dict[str, str] | None = None,
        counter_path: pathlib.Path | None = None,
        hold_signal_file: pathlib.Path | None = None,
        release_file: pathlib.Path | None = None,
        response_prefix: str = "FLEET_STUB_OK",
    ) -> None:
        self.node_name = node_name
        self.base_dir = pathlib.Path(base_dir).resolve()
        self.home_dir = self.base_dir / "home"
        self.cfg_dir = pathlib.Path(cfg_dir_override).resolve() if cfg_dir_override else (self.base_dir / "cfg")
        self.data_dir = self.base_dir / "data"
        self.logs_dir = self.base_dir / "logs"
        self.fake_bin = self.base_dir / "_fake_bin"
        self.guard_bin = self.base_dir / "_guard_bin"
        for d in (
            self.home_dir,
            self.cfg_dir,
            self.data_dir,
            self.logs_dir,
            self.fake_bin,
            self.guard_bin,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self.repo_dir = repo_dir
        self.bus_root = pathlib.Path(bus_root).resolve()
        self.extra_env = dict(extra_env or {})
        self.claude_bin = _write_fake_claude(
            self.fake_bin,
            counter_path=counter_path,
            hold_signal_file=hold_signal_file,
            release_file=release_file,
            response_prefix=response_prefix,
        )
        write_claude_guard_script(self.guard_bin)

        self.proc: subprocess.Popen[str] | None = None
        self._stdout_fh: Any = None
        self._stderr_fh: Any = None
        self.stdout_path = self.logs_dir / f"{self.node_name}.stdout.log"
        self.stderr_path = self.logs_dir / f"{self.node_name}.stderr.log"

    def build_env(self) -> dict[str, str]:
        gitconfig = self.home_dir / ".gitconfig"
        if not gitconfig.exists():
            gitconfig.write_text(
                "[user]\n\tname = Node Worker\n\temail = node@example.invalid\n",
                encoding="utf-8",
            )
        sys_paths = [
            p
            for p in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep)
            if p and "/.local" not in p and "/.npm" not in p and "/.nvm" not in p
        ]
        safe_path = os.pathsep.join([str(self.fake_bin), str(self.guard_bin), *sys_paths])
        env: dict[str, str] = {
            "HOME": str(self.home_dir),
            "PATH": safe_path,
            "PYTHONPATH": str(self.bus_root),
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
            "BUS_CLAUDE_CMD": str(self.claude_bin),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
        if self.repo_dir is not None:
            env["BUS_FLEET_REPO_DIR"] = str(self.repo_dir.resolve())
        env.update(self.extra_env)
        return env

    def write_config(self, doc: dict[str, Any]) -> pathlib.Path:
        p = self.cfg_dir / "config.json"
        p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return p

    def start(
        self,
        *,
        label: str = "main",
        mode: str = "hybrid",
        peers: str = "",
        url: str | None = None,
        no_auto_update: bool = False,
        launch_cwd: pathlib.Path | None = None,
    ) -> subprocess.Popen[str]:
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError(f"Node {self.node_name} is already running")
        self._stdout_fh = open(self.stdout_path, "a", encoding="utf-8")
        self._stderr_fh = open(self.stderr_path, "a", encoding="utf-8")
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
            "--cwd",
            str(self.home_dir),
        ]
        if peers:
            cmd.extend(["--peers", peers])
        if url:
            cmd.extend(["--url", url])
        if no_auto_update:
            cmd.append("--no-auto-update")

        eff_cwd = pathlib.Path(launch_cwd).resolve() if launch_cwd is not None else self.home_dir
        eff_cwd.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            cmd,
            env=self.build_env(),
            cwd=str(eff_cwd),
            stdout=self._stdout_fh,
            stderr=self._stderr_fh,
            text=True,
            start_new_session=True,
        )
        return self.proc

    def read_stdout(self) -> str:
        return self.stdout_path.read_text(encoding="utf-8", errors="replace") if self.stdout_path.exists() else ""

    def read_stderr(self) -> str:
        return self.stderr_path.read_text(encoding="utf-8", errors="replace") if self.stderr_path.exists() else ""

    def _close_logs(self) -> None:
        for attr in ("_stdout_fh", "_stderr_fh"):
            fh = getattr(self, attr, None)
            if fh is not None:
                with contextlib.suppress(Exception):
                    fh.close()
                setattr(self, attr, None)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            pgid: int | None = None
            with contextlib.suppress(Exception):
                pgid = os.getpgid(self.proc.pid)
            try:
                if pgid and pgid > 1:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    self.proc.terminate()
                self.proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    if pgid and pgid > 1:
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        self.proc.kill()
                with contextlib.suppress(Exception):
                    self.proc.wait(timeout=3.0)
            except ProcessLookupError:
                pass
        self._close_logs()


def _run_cli(
    env: dict[str, str],
    *args: str,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_bus.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


async def _ws_banner(ws_url: str, *, node_name: str = "cat-food", local_ip: str | None = None) -> dict[str, Any]:
    kw: dict[str, Any] = {"open_timeout": 3.0, "close_timeout": 1.5}
    if local_ip:
        kw["local_addr"] = (local_ip, 0)
    else:
        hdr_kw = (
            "additional_headers"
            if "additional_headers" in inspect.signature(websockets.connect).parameters
            else "extra_headers"
        )
        kw[hdr_kw] = {"X-Bus-Test-Local-Node": node_name}
    async with websockets.connect(ws_url, **kw) as ws:
        await ws.send(
            json.dumps(
                {
                    "v": 2,
                    "label": "cli-probe",
                    "since": 10**9,
                    "accept_inbox": False,
                    "no_replay": True,
                }
            )
        )
        return json.loads(await asyncio.wait_for(ws.recv(), 3.0))


def _read_worker_lock_pids(cfg_dir: pathlib.Path) -> list[int]:
    locks_dir = cfg_dir / "locks"
    if not locks_dir.is_dir():
        return []
    pids: list[int] = []
    for p in sorted(locks_dir.glob("worker-*.lock")):
        try:
            raw = p.read_text(encoding="utf-8").strip()
            if raw:
                pids.append(int(raw.split()[0]))
        except Exception:
            continue
    return pids


@pytest.fixture
def node_proc_registry() -> Any:
    nodes: list[ManagedNodeProc] = []
    raw_procs: list[subprocess.Popen[Any]] = []
    try:
        yield nodes, raw_procs
    finally:
        for p in reversed(raw_procs):
            if p.poll() is None:
                with contextlib.suppress(Exception):
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                with contextlib.suppress(Exception):
                    p.terminate()
                try:
                    p.wait(timeout=2.5)
                except Exception:
                    with contextlib.suppress(Exception):
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    with contextlib.suppress(Exception):
                        p.kill()
                    with contextlib.suppress(Exception):
                        p.wait(timeout=2.5)
        for n in reversed(nodes):
            n.stop()


# ======================================================================
# 1. `rev` advance -> atomic `config.json` + `.bak` + `os.execv` same PID
# ======================================================================


@pytest.mark.chaos
async def test_fleet_01_rev_advance_atomic_config_update_execv_same_pid_and_idempotent(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    node_proc_registry: Any,
) -> None:
    nodes, _ = node_proc_registry
    ports = (18901, 18902, 18903, 18904)
    for p in ports:
        validate_sandbox_port(p)
    assert_ports_free(ports, context="fleet_01_pre")

    _, _, clones, _, sha_v1 = _create_origin_and_clones(tmp_path / "git_s1", ["node1"])

    hub_1 = hub_factory.start_hub(
        "fleet_s1_hub1",
        port=18901,
        gateway_port=18902,
        allowed=("cat-food", "node-1", "mother-goose", "z-fold8"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )
    hub_2 = hub_factory.start_hub(
        "fleet_s1_hub2",
        port=18903,
        gateway_port=18904,
        allowed=("cat-food", "node-1", "mother-goose", "z-fold8"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )

    # Without `fleet.json`, banner has NO `fleet` key and `/v1/health` reports `fleet: None`.
    b_off = await _ws_banner(hub_1.ws_url)
    assert "fleet" not in b_off
    c_off, h_off = await asyncio.to_thread(_http_get_json, f"{hub_1.gateway_http_url}/v1/health")
    assert c_off == 200 and h_off["fleet"] is None and h_off["fleet_nodes"] == {}

    initial_cfg = {
        "url": hub_1.ws_url,
        "urls": [hub_1.ws_url],
        "custom_user_key": {"nested": [10, 20, 30], "tag": "preserve-me"},
        "custom_flag": True,
        "peers": "cat-food",
    }
    custom_launch_dir = tmp_path / "node1_custom_launch_cwd"
    node1 = ManagedNodeProc(
        node_name="node-1",
        base_dir=tmp_path / "node1",
        repo_dir=clones["node1"],
        extra_env={"BUS_FLEET_POLL_INTERVAL": "0.3"},
    )
    nodes.append(node1)
    orig_cfg_bytes = node1.write_config(initial_cfg).read_bytes()
    proc1 = node1.start(
        label="main",
        mode="hybrid",
        peers="cat-food",
        launch_cwd=custom_launch_dir,
    )
    pid_before = proc1.pid

    # Wait until `node-1/main` is connected to `hub_1` (and NOT connected to `hub_2` yet).
    async def _node1_on_hub1() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        return "node-1/main" in (b.get("peers") or [])

    await _wait_until(_node1_on_hub1, timeout=5.0, desc="node-1/main connected to hub_1")
    cmdline_before = pathlib.Path(f"/proc/{pid_before}/cmdline").read_bytes()
    cwd_before = os.readlink(f"/proc/{pid_before}/cwd")
    assert cwd_before == str(custom_launch_dir.resolve())
    assert b"-m\x00agent_bus.cli\x00run" in cmdline_before
    assert "node-1/main" not in ((await _ws_banner(hub_2.ws_url)).get("peers") or [])

    # Live `abus fleet set` on already-running `hub_1` (no hub restart!).
    set_res = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v1,
        "--urls",
        f"{hub_1.ws_url},{hub_2.ws_url}",
        "--hub-peers",
        "mother-goose,z-fold8",
        "--json",
    )
    assert set_res.returncode == 0, set_res.stderr
    set_doc = json.loads(set_res.stdout)
    assert set_doc["fleet"]["rev"] == 1
    assert set_doc["fleet"]["urls"] == [hub_1.ws_url, hub_2.ws_url]
    assert set_doc["fleet"]["hub_peers"] == ["mother-goose", "z-fold8"]

    # `node-1` converges, flushes `fleet-ack`, and re-executes IN-PLACE via `os.execv`
    # without exiting (`pid_before == pid_after`), automatically connecting to BOTH `hub_1` & `hub_2`!
    async def _node1_dual_connected_after_execv() -> bool:
        b1 = await _ws_banner(hub_1.ws_url)
        b2 = await _ws_banner(hub_2.ws_url)
        return (
            "node-1/main" in (b1.get("peers") or [])
            and "node-1/main" in (b2.get("peers") or [])
            and "re-executing in-place via os.execv" in node1.read_stdout()
        )

    await _wait_until(
        _node1_dual_connected_after_execv,
        timeout=8.0,
        desc="node-1 dual-connected via in-place os.execv",
    )

    assert proc1.poll() is None
    assert proc1.pid == pid_before
    assert pathlib.Path(f"/proc/{pid_before}/cmdline").read_bytes() == cmdline_before
    assert os.readlink(f"/proc/{pid_before}/cwd") == cwd_before
    assert _read_worker_lock_pids(node1.cfg_dir) == [pid_before]

    # Check `.bak` exact bytes, preserved custom keys, updated `urls`/`hub_peers`, and `fleet-applied.json`.
    bak_path = node1.cfg_dir / "config.json.bak"
    assert bak_path.is_file()
    assert bak_path.read_bytes() == orig_cfg_bytes

    updated_cfg = json.loads((node1.cfg_dir / "config.json").read_text(encoding="utf-8"))
    assert updated_cfg["urls"] == [hub_1.ws_url, hub_2.ws_url]
    assert updated_cfg["url"] == hub_1.ws_url
    assert updated_cfg["hub_peers"] == ["mother-goose", "z-fold8"]
    assert updated_cfg["custom_user_key"] == {"nested": [10, 20, 30], "tag": "preserve-me"}
    assert updated_cfg["custom_flag"] is True
    assert updated_cfg["peers"] == "cat-food"

    applied_doc = json.loads((node1.cfg_dir / "fleet-applied.json").read_text(encoding="utf-8"))
    assert applied_doc["rev"] == 1
    assert applied_doc["checkout"] == str(clones["node1"])
    assert applied_doc["commit"] == sha_v1
    assert applied_doc["target_commit"] == sha_v1
    assert applied_doc["result"] == "ok"
    assert applied_doc["config_changed"] is True
    assert applied_doc["code_changed"] is False
    assert applied_doc["attempts"] == 1
    assert str(clones["node1"]) in applied_doc["checkouts"]

    # Wait until `hub_2` also received `node-1`'s `fleet-ack`!
    await _wait_until(
        lambda: _http_get_json(f"{hub_2.gateway_http_url}/v1/health")[1]
        .get("fleet_nodes", {})
        .get("node-1", {})
        .get("rev")
        == 1,
        timeout=4.0,
        desc="hub_2 received fleet-ack from re-executed node-1",
    )

    # Verify `abus fleet status` (`--json` over WS & table over HTTP Gateway) AND hub restart persistence!
    st_ws = await asyncio.to_thread(
        _run_cli, hub_1.build_env(), "fleet", "status", "--url", hub_1.ws_url, "--json"
    )
    assert st_ws.returncode == 0, st_ws.stderr
    st_doc = json.loads(st_ws.stdout)
    assert st_doc["ok"] is True
    assert st_doc["all_converged"] is True
    assert st_doc["nodes"]["node-1"]["rev"] == 1
    assert st_doc["nodes"]["node-1"]["checkout"] == str(clones["node1"])
    assert st_doc["nodes"]["node-1"]["result"] == "ok"
    assert st_doc["nodes"]["node-1"]["converged"] is True

    st_tbl = await asyncio.to_thread(
        _run_cli, hub_1.build_env(), "fleet", "status", "--url", hub_1.gateway_http_url
    )
    assert st_tbl.returncode == 0
    assert "Target Fleet: rev=1" in st_tbl.stdout
    assert "node-1" in st_tbl.stdout
    assert str(clones["node1"]) in st_tbl.stdout

    # Restart `hub_1` -> `fleet_status` (`checkout` + `commit`) persists in SQLite!
    hub_1.restart()
    _, h_after_restart = await asyncio.to_thread(_http_get_json, f"{hub_1.gateway_http_url}/v1/health")
    assert h_after_restart["fleet"]["rev"] == 1
    assert h_after_restart["fleet_nodes"]["node-1"]["rev"] == 1
    assert h_after_restart["fleet_nodes"]["node-1"]["checkout"] == str(clones["node1"])

    # Verify live RPC over `hub_2` proves the re-executed worker's OS PID (`ppid`) is `pid_before`
    # and there is ZERO infinite `execv` loop!
    await asyncio.sleep(0.35)
    assert node1.read_stdout().count("re-executing in-place via os.execv") == 1
    rpc_res = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "send",
        "--to",
        "node-1/main",
        "--wait",
        "5",
        "--url",
        hub_2.ws_url,
        "[id=s1-rpc-check] ping",
    )
    assert rpc_res.returncode == 0, rpc_res.stderr
    assert f"FLEET_STUB_OK:s1-rpc-check:ppid={pid_before}" in rpc_res.stdout

    node1.stop()
    hub_2.stop()
    hub_1.stop()
    assert_ports_free(ports, context="fleet_01_post")


# ======================================================================
# 2. Git `commit` Fast-Forward (`BUS_FLEET_REPO_DIR`) + `--no-auto-update`
# ======================================================================


@pytest.mark.chaos
async def test_fleet_02_commit_fast_forward_and_no_auto_update_skip(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    node_proc_registry: Any,
) -> None:
    nodes, _ = node_proc_registry
    ports = (18911, 18912, 18913, 18914)
    for p in ports:
        validate_sandbox_port(p)
    assert_ports_free(ports, context="fleet_02_pre")

    _, publisher, clones, git_env, sha_v1 = _create_origin_and_clones(
        tmp_path / "git_s2", ["auto", "manual"]
    )

    hub_1 = hub_factory.start_hub(
        "fleet_s2_hub1",
        port=18911,
        gateway_port=18912,
        allowed=("cat-food", "node-auto", "node-manual"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )
    hub_2 = hub_factory.start_hub(
        "fleet_s2_hub2",
        port=18913,
        gateway_port=18914,
        allowed=("cat-food", "node-auto", "node-manual"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )

    node_auto = ManagedNodeProc(
        node_name="node-auto",
        base_dir=tmp_path / "node_auto",
        repo_dir=clones["auto"],
    )
    node_manual = ManagedNodeProc(
        node_name="node-manual",
        base_dir=tmp_path / "node_manual",
        repo_dir=clones["manual"],
    )
    nodes.extend([node_auto, node_manual])

    node_auto.write_config({"urls": [hub_1.ws_url]})
    node_manual.write_config({"urls": [hub_1.ws_url]})

    pid_auto = node_auto.start(label="main", mode="hybrid", no_auto_update=False).pid
    pid_man = node_manual.start(label="main", mode="hybrid", no_auto_update=True).pid

    async def _both_connected() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        ps = set(b.get("peers") or [])
        return {"node-auto/main", "node-manual/main"} <= ps

    await _wait_until(_both_connected, timeout=5.0, desc="node-auto and node-manual connected")

    # Push `sha_v2` to `origin` and publish `rev=1` (changes BOTH `commit` AND `urls`)
    sha_v2 = _push_new_commit(publisher, git_env, "feature_v2.txt", "v2 payload\n", "release v2")
    assert sha_v2 != sha_v1

    set_r1 = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v2,
        "--urls",
        f"{hub_1.ws_url},{hub_2.ws_url}",
    )
    assert set_r1.returncode == 0, set_r1.stderr

    # Both nodes re-execute via `os.execv` keeping identical PIDs and connecting to `hub_2`!
    async def _both_on_hub2() -> bool:
        b2 = await _ws_banner(hub_2.ws_url)
        ps2 = set(b2.get("peers") or [])
        return {"node-auto/main", "node-manual/main"} <= ps2

    await _wait_until(_both_on_hub2, timeout=8.0, desc="both nodes connected to hub_2 via execv")
    assert node_auto.proc is not None and node_auto.proc.poll() is None and node_auto.proc.pid == pid_auto
    assert node_manual.proc is not None and node_manual.proc.poll() is None and node_manual.proc.pid == pid_man

    # Verify `node-auto` fast-forwarded `HEAD == sha_v2`!
    assert _git(clones["auto"], git_env, "rev-parse", "HEAD") == sha_v2
    assert (clones["auto"] / "feature_v2.txt").read_text(encoding="utf-8") == "v2 payload\n"
    app_auto = json.loads((node_auto.cfg_dir / "fleet-applied.json").read_text(encoding="utf-8"))
    assert app_auto["rev"] == 1
    assert app_auto["commit"] == sha_v2
    assert app_auto["result"] == "ok"
    assert app_auto["code_changed"] is True
    assert app_auto["config_changed"] is True

    # Verify `node-manual` (`--no-auto-update`) skipped git commit (`HEAD == sha_v1`) and recorded `ok:no_auto_update`!
    assert _git(clones["manual"], git_env, "rev-parse", "HEAD") == sha_v1
    assert not (clones["manual"] / "feature_v2.txt").exists()
    app_man = json.loads((node_manual.cfg_dir / "fleet-applied.json").read_text(encoding="utf-8"))
    assert app_man["rev"] == 1
    assert app_man["commit"] == sha_v1
    assert app_man["target_commit"] == sha_v2
    assert app_man["result"] == "ok:no_auto_update"
    assert app_man["commit_skipped"] is True
    assert app_man["code_changed"] is False
    assert app_man["config_changed"] is True

    # Push `sha_v3` with IDENTICAL `urls` (`rev=2`):
    # `node-auto` fast-forwards to `sha_v3` & `execv`s (still `pid_auto`), while `node-manual`
    # (`config_changed == False, code_changed == False`) records `rev=2` WITHOUT calling `os.execv`!
    man_execv_before = node_manual.read_stdout().count("re-executing in-place via os.execv")
    assert man_execv_before == 1

    sha_v3 = _push_new_commit(publisher, git_env, "feature_v3.txt", "v3 payload\n", "release v3")
    set_r2 = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v3,
        "--urls",
        f"{hub_1.ws_url},{hub_2.ws_url}",
    )
    assert set_r2.returncode == 0, set_r2.stderr

    await _wait_until(
        lambda: _git(clones["auto"], git_env, "rev-parse", "HEAD") == sha_v3
        and node_auto.read_stdout().count("re-executing in-place via os.execv") == 2,
        timeout=6.0,
        desc="node-auto converged rev=2 via second in-place execv",
    )
    await _wait_until(
        lambda: (node_manual.cfg_dir / "fleet-applied.json").is_file()
        and json.loads((node_manual.cfg_dir / "fleet-applied.json").read_text(encoding="utf-8")).get("rev") == 2,
        timeout=5.0,
        desc="node-manual recorded rev=2 without restart",
    )
    await asyncio.sleep(0.3)
    assert node_auto.proc.poll() is None and node_auto.proc.pid == pid_auto
    assert node_manual.proc.poll() is None and node_manual.proc.pid == pid_man
    assert node_manual.read_stdout().count("re-executing in-place via os.execv") == 1
    assert _git(clones["manual"], git_env, "rev-parse", "HEAD") == sha_v1

    node_manual.stop()
    node_auto.stop()
    hub_2.stop()
    hub_1.stop()
    assert_ports_free(ports, context="fleet_02_post")


# ======================================================================
# 3. Graceful Drain while Worker Busy (`inbox running == 1`) before `os.execv`
# ======================================================================


@pytest.mark.chaos
async def test_fleet_03_graceful_drain_while_worker_busy_before_execv(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    node_proc_registry: Any,
) -> None:
    nodes, raw_procs = node_proc_registry
    ports = (18921, 18922, 18923, 18924)
    for p in ports:
        validate_sandbox_port(p)
    assert_ports_free(ports, context="fleet_03_pre")

    _, publisher, clones, git_env, sha_v1 = _create_origin_and_clones(tmp_path / "git_s3", ["busy"])

    hub_1 = hub_factory.start_hub(
        "fleet_s3_hub1",
        port=18921,
        gateway_port=18922,
        allowed=("cat-food", "node-busy"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )
    hub_2 = hub_factory.start_hub(
        "fleet_s3_hub2",
        port=18923,
        gateway_port=18924,
        allowed=("cat-food", "node-busy"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )

    counter = SideEffectCounter(tmp_path / "s3_counter.jsonl")
    hold_sig = tmp_path / "s3_hold.signal"
    release_sig = tmp_path / "s3_release.signal"

    node_busy = ManagedNodeProc(
        node_name="node-busy",
        base_dir=tmp_path / "node_busy",
        repo_dir=clones["busy"],
        counter_path=counter.path,
        hold_signal_file=hold_sig,
        release_file=release_sig,
        response_prefix="DRAIN_RPC_OK",
    )
    nodes.append(node_busy)
    node_busy.write_config({"urls": [hub_1.ws_url]})
    pid_busy = node_busy.start(label="main", mode="hybrid", peers="cat-food").pid

    async def _busy_connected() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        return "node-busy/main" in (b.get("peers") or [])

    await _wait_until(_busy_connected, timeout=5.0, desc="node-busy connected")

    caller_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_bus.cli",
            "send",
            "--to",
            "node-busy/main",
            "--wait",
            "15",
            "--corr-id",
            "drain-corr-01",
            "--url",
            hub_1.ws_url,
            "[id=drain-job-01] slow computation",
        ],
        env=hub_1.build_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    raw_procs.append(caller_proc)

    await _wait_until(lambda: hold_sig.is_file(), timeout=5.0, desc="fake claude entered hold")

    def _running_count() -> int:
        with NodeJournal(node_busy.cfg_dir) as j:
            return j.count_inbox_running()

    assert _running_count() == 1

    # Push `sha_v2` and announce `fleet` `rev=1` WHILE `inbox running == 1`!
    sha_v2 = _push_new_commit(publisher, git_env, "drained_v2.txt", "after drain\n", "v2")
    set_res = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v2,
        "--urls",
        f"{hub_1.ws_url},{hub_2.ws_url}",
    )
    assert set_res.returncode == 0, set_res.stderr

    await _wait_until(
        lambda: "detected while worker busy" in node_busy.read_stdout(),
        timeout=4.0,
        desc="node-busy detected fleet rev=1 while busy",
    )

    # Hold for 0.35s while task is still running -> node MUST stay alive and NOT mutate git worktree mid-job!
    await asyncio.sleep(0.35)
    assert node_busy.proc is not None and node_busy.proc.poll() is None
    assert _running_count() == 1
    assert _git(clones["busy"], git_env, "rev-parse", "HEAD") == sha_v1

    # Release the slow `claude` job!
    release_sig.write_text("go\n", encoding="utf-8")

    # 1) RPC caller receives normal terminal `status='ok'` response!
    caller_out, caller_err = await asyncio.to_thread(caller_proc.communicate, timeout=8.0)
    assert caller_proc.returncode == 0, f"caller failed: {caller_err}\n{caller_out}"
    assert f"DRAIN_RPC_OK:drain-job-01:ppid={pid_busy}" in caller_out

    # 2) Immediately after becoming idle & flushing reply, `node-busy` converges and `os.execv`s
    #    in-place keeping `pid_busy` and connecting to `hub_2`!
    async def _busy_dual_connected() -> bool:
        b2 = await _ws_banner(hub_2.ws_url)
        return "node-busy/main" in (b2.get("peers") or [])

    await _wait_until(_busy_dual_connected, timeout=8.0, desc="node-busy connected to hub_2 after drain+execv")
    assert node_busy.proc.poll() is None and node_busy.proc.pid == pid_busy
    assert _git(clones["busy"], git_env, "rev-parse", "HEAD") == sha_v2
    assert json.loads((node_busy.cfg_dir / "config.json").read_text(encoding="utf-8"))["urls"] == [
        hub_1.ws_url,
        hub_2.ws_url,
    ]

    # 3) Verify at-most-once execution (`count == 1`), `inbox done == 1, uncertain == 0`, and hub `fleet_status`!
    assert counter.count("drain-job-01") == 1
    with NodeJournal(node_busy.cfg_dir) as j:
        summary = j.status_summary()
    assert summary["inbox"]["done"] == 1
    assert summary["inbox"]["uncertain"] == 0
    assert summary["inbox"]["running"] == 0

    _, h_snap = await asyncio.to_thread(_http_get_json, f"{hub_1.gateway_http_url}/v1/health")
    assert h_snap["fleet_nodes"]["node-busy"]["rev"] == 1
    assert h_snap["fleet_nodes"]["node-busy"]["commit"] == sha_v2
    assert h_snap["fleet_nodes"]["node-busy"]["result"] == "ok"

    node_busy.stop()
    hub_2.stop()
    hub_1.stop()
    assert_ports_free(ports, context="fleet_03_post")


# ======================================================================
# 4. Safety Guards (`blocked:*`, 3-Strike Retry Halt, Corrupt/Downgrade Ignore)
# ======================================================================


@pytest.mark.chaos
async def test_fleet_04_safety_guards_dirty_nonff_missing_caps_3strike_and_invalid_inputs(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    node_proc_registry: Any,
) -> None:
    nodes, _ = node_proc_registry
    ports = (18931, 18932, 18933, 18934)
    for p in ports:
        validate_sandbox_port(p)
    assert_ports_free(ports, context="fleet_04_pre")

    _, publisher, clones, git_env, sha_v1 = _create_origin_and_clones(
        tmp_path / "git_s4", ["dirty", "diverged", "strike"]
    )

    hub_1 = hub_factory.start_hub(
        "fleet_s4_hub1",
        port=18931,
        gateway_port=18932,
        allowed=("cat-food", "node-dirty", "node-diverged", "node-strike"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )
    hub_2 = hub_factory.start_hub(
        "fleet_s4_hub2",
        port=18933,
        gateway_port=18934,
        allowed=("cat-food", "node-dirty", "node-diverged", "node-strike"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )

    # (4a) Malformed `fleet.json` (`corrupt json`, `rev <= 0`, `urls == []`, `http://` URL) on Hub -> ignored!
    fleet_p = hub_1.cfg_dir / "fleet.json"
    for bad_raw in (
        "{not-valid-json",
        json.dumps({"rev": 0, "commit": sha_v1, "urls": [hub_1.ws_url]}),
        json.dumps({"rev": -3, "commit": sha_v1, "urls": [hub_1.ws_url]}),
        json.dumps({"rev": 1, "commit": sha_v1, "urls": []}),
        json.dumps({"rev": 1, "commit": sha_v1, "urls": ["http://127.0.0.1:18932"]}),
    ):
        fleet_p.write_text(bad_raw, encoding="utf-8")
        b_bad = await _ws_banner(hub_1.ws_url)
        assert "fleet" not in b_bad
        _, h_bad = await asyncio.to_thread(_http_get_json, f"{hub_1.gateway_http_url}/v1/health")
        assert h_bad["fleet"] is None
    fleet_p.unlink()

    bad_cli = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v1,
        "--urls",
        "http://127.0.0.1:18932",
    )
    assert bad_cli.returncode != 0
    assert not fleet_p.exists()

    dirty_readme = clones["dirty"] / "README.md"
    dirty_readme.write_text(" PRECIOUS LOCAL UNCOMMITTED EDIT \n", encoding="utf-8")

    (clones["diverged"] / "local_only.txt").write_text("diverged branch work\n", encoding="utf-8")
    _git(clones["diverged"], git_env, "add", "local_only.txt")
    _git(clones["diverged"], git_env, "commit", "-q", "-m", "local diverged commit")
    sha_local_diverged = _git(clones["diverged"], git_env, "rev-parse", "HEAD")

    node_dirty = ManagedNodeProc(
        node_name="node-dirty",
        base_dir=tmp_path / "node_dirty",
        repo_dir=clones["dirty"],
        extra_env={"BUS_FLEET_RETRY_INTERVAL": "0.1"},
    )
    node_diverged = ManagedNodeProc(
        node_name="node-diverged",
        base_dir=tmp_path / "node_diverged",
        repo_dir=clones["diverged"],
        extra_env={"BUS_FLEET_RETRY_INTERVAL": "0.1"},
    )
    node_strike = ManagedNodeProc(
        node_name="node-strike",
        base_dir=tmp_path / "node_strike",
        repo_dir=clones["strike"],
        extra_env={"BUS_FLEET_RETRY_INTERVAL": "0.1"},
    )
    nodes.extend([node_dirty, node_diverged, node_strike])

    for n in (node_dirty, node_diverged, node_strike):
        n.write_config({"urls": [hub_1.ws_url], "keep_me": "safe"})
        n.start(label="main", mode="hybrid")
    pid_strike = node_strike.proc.pid  # type: ignore[union-attr]

    async def _all_three_connected() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        ps = set(b.get("peers") or [])
        return {"node-dirty/main", "node-diverged/main", "node-strike/main"} <= ps

    await _wait_until(_all_three_connected, timeout=6.0, desc="all 3 guard nodes connected")

    # (4b & 4c) Push `sha_v2` to `origin` and set `rev=1` ->
    # `node-strike` converges & `os.execv`s in-place; `node-dirty` -> `blocked:dirty_worktree`;
    # `node-diverged` -> `blocked:non_fast_forward`!
    sha_v2 = _push_new_commit(publisher, git_env, "upstream_v2.txt", "upstream\n", "v2")
    r1 = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v2,
        "--urls",
        f"{hub_1.ws_url},{hub_2.ws_url}",
    )
    assert r1.returncode == 0, r1.stderr

    async def _strike_on_hub2() -> bool:
        b2 = await _ws_banner(hub_2.ws_url)
        return "node-strike/main" in (b2.get("peers") or [])

    await _wait_until(_strike_on_hub2, timeout=6.0, desc="node-strike connected to hub_2 via execv")
    assert node_strike.proc is not None and node_strike.proc.poll() is None and node_strike.proc.pid == pid_strike
    assert _git(clones["strike"], git_env, "rev-parse", "HEAD") == sha_v2

    async def _dirty_and_diverged_blocked() -> bool:
        _, h = await asyncio.to_thread(_http_get_json, f"{hub_1.gateway_http_url}/v1/health")
        fn = h.get("fleet_nodes") or {}
        return (
            fn.get("node-dirty", {}).get("result") == "blocked:dirty_worktree"
            and fn.get("node-diverged", {}).get("result") == "blocked:non_fast_forward"
        )

    await _wait_until(_dirty_and_diverged_blocked, timeout=6.0, desc="dirty and diverged blocked reports")

    assert dirty_readme.read_text(encoding="utf-8") == " PRECIOUS LOCAL UNCOMMITTED EDIT \n"
    assert _git(clones["dirty"], git_env, "rev-parse", "HEAD") == sha_v1
    assert _git(clones["dirty"], git_env, "stash", "list") == ""
    assert _git(clones["diverged"], git_env, "rev-parse", "HEAD") == sha_local_diverged
    assert (clones["diverged"] / "local_only.txt").read_text(encoding="utf-8") == "diverged branch work\n"
    assert _git(clones["diverged"], git_env, "stash", "list") == ""
    assert node_dirty.proc is not None and node_dirty.proc.poll() is None
    assert node_diverged.proc is not None and node_diverged.proc.poll() is None

    reg_cli = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--rev",
        "1",
        "--commit",
        sha_v2,
        "--urls",
        hub_1.ws_url,
    )
    assert reg_cli.returncode != 0

    # (4d) `min_caps` unsatisfied -> `blocked:missing_caps` (`rev=2`)
    r2 = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v2,
        "--urls",
        f"{hub_1.ws_url},{hub_2.ws_url}",
        "--min-caps",
        "v2,nonexistent_future_cap_v99",
    )
    assert r2.returncode == 0, r2.stderr

    await _wait_until(
        lambda: _http_get_json(f"{hub_1.gateway_http_url}/v1/health")[1]
        .get("fleet_nodes", {})
        .get("node-strike", {})
        .get("result")
        == "blocked:missing_caps",
        timeout=5.0,
        desc="node-strike blocked:missing_caps",
    )
    assert node_strike.proc.poll() is None and node_strike.proc.pid == pid_strike

    # (4e) 3-Strike Retry Guard on Nonexistent Commit (`rev=3` -> `attempts == 3` -> `blocked:commit_not_on_origin`)
    missing_sha = "deadbeef" * 5
    r3 = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--commit",
        missing_sha,
        "--urls",
        f"{hub_1.ws_url},{hub_2.ws_url}",
    )
    assert r3.returncode == 0, r3.stderr

    async def _strike_blocked_3_attempts() -> bool:
        _, h = await asyncio.to_thread(_http_get_json, f"{hub_1.gateway_http_url}/v1/health")
        st = (h.get("fleet_nodes") or {}).get("node-strike") or {}
        return st.get("rev") == 3 and st.get("result") == "blocked:commit_not_on_origin"

    await _wait_until(_strike_blocked_3_attempts, timeout=6.0, desc="3-strike blocked:commit_not_on_origin")
    strike_applied = json.loads((node_strike.cfg_dir / "fleet-applied.json").read_text(encoding="utf-8"))
    assert strike_applied["rev"] == 3
    assert strike_applied["result"] == "blocked:commit_not_on_origin"
    assert strike_applied["attempts"] == 3

    await asyncio.sleep(0.45)
    assert node_strike.proc.poll() is None and node_strike.proc.pid == pid_strike
    assert node_strike.read_stderr().count("rev=3 transient failure") == 2

    downgrade_out = converge_fleet_once(
        node_strike.cfg_dir,
        {"rev": 2, "commit": sha_v2, "urls": [hub_1.ws_url]},
        repo_dir=clones["strike"],
    )
    assert downgrade_out.action == "ignored"
    empty_urls_out = converge_fleet_once(
        node_strike.cfg_dir,
        {"rev": 99, "commit": sha_v2, "urls": []},
        repo_dir=clones["strike"],
    )
    assert empty_urls_out.action == "ignored"

    # Publish valid `sha_v3` at `rev=4` -> `node-strike` unblocks, fast-forwards to `sha_v3`, and `os.execv`s!
    sha_v3 = _push_new_commit(publisher, git_env, "upstream_v3.txt", "recovered\n", "v3")
    r4 = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v3,
        "--urls",
        f"{hub_1.ws_url},{hub_2.ws_url}",
    )
    assert r4.returncode == 0, r4.stderr

    await _wait_until(
        lambda: _git(clones["strike"], git_env, "rev-parse", "HEAD") == sha_v3
        and node_strike.read_stdout().count("re-executing in-place via os.execv") == 2,
        timeout=6.0,
        desc="node-strike unblocked and re-executed via os.execv at rev=4",
    )
    assert node_strike.proc.poll() is None and node_strike.proc.pid == pid_strike

    for n in (node_strike, node_diverged, node_dirty):
        n.stop()
    hub_2.stop()
    hub_1.stop()
    assert_ports_free(ports, context="fleet_04_post")


# ======================================================================
# 5. Replica Hub (`18905` -> `18915`) Manifest Sync & Single-Writer Guard
# ======================================================================


@pytest.mark.chaos
async def test_fleet_05_replica_hub_manifest_sync_and_single_writer_guard(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    node_proc_registry: Any,
) -> None:
    nodes, _ = node_proc_registry
    ports = (18905, 18906, 18915, 18916)
    for p in ports:
        validate_sandbox_port(p)
    assert_ports_free(ports, context="fleet_05_pre")

    whois_cmd = _write_whois_script(tmp_path / "whois_bin")
    _, publisher, clones, git_env, sha_v1 = _create_origin_and_clones(tmp_path / "git_s5", ["zfold"])

    hub_a = hub_factory.start_hub(
        "fleet_s5_src_a",
        port=18905,
        gateway_port=18906,
        allowed=("cat-food", "mother-goose", "z-fold8"),
        extra_env={
            "BUS_TEST_ALLOW_LOCALHOST": "0",
            "BUS_WHOIS_CMD": str(whois_cmd),
            "BUS_HUB_PEERS": "mother-goose",
            "BUS_AUTH_WATCH_INTERVAL": "0.05",
        },
    )
    hub_b = hub_factory.start_hub(
        "fleet_s5_rep_b",
        port=18915,
        gateway_port=18916,
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
        lambda: _http_get_json(f"{hub_b.gateway_http_url}/v1/health")[1].get("auth_generation", 0) >= 1,
        timeout=5.0,
        desc="Hub B initial auth_sync",
    )

    rep_set = await asyncio.to_thread(
        _run_cli,
        hub_b.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v1,
        "--urls",
        hub_b.ws_url,
    )
    assert rep_set.returncode != 0
    assert "replica mode" in rep_set.stderr.lower()
    assert not (hub_b.cfg_dir / "fleet.json").exists()

    node_rep = ManagedNodeProc(
        node_name="cat-food",
        base_dir=tmp_path / "node_on_replica",
        repo_dir=clones["zfold"],
        extra_env={"BUS_FLEET_POLL_INTERVAL": "0.25"},
    )
    nodes.append(node_rep)
    node_rep.write_config({"urls": [hub_b.ws_url]})
    pid_rep = node_rep.start(label="main", mode="hybrid").pid

    async def _node_on_hub_b() -> bool:
        b = await _ws_banner(hub_b.ws_url, local_ip="127.0.0.3")
        return "cat-food/main" in (b.get("peers") or [])

    await _wait_until(_node_on_hub_b, timeout=5.0, desc="node connected to replica Hub B")

    sha_v2 = _push_new_commit(publisher, git_env, "replica_synced.txt", "via replica\n", "v2")
    src_set = await asyncio.to_thread(
        _run_cli,
        hub_a.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v2,
        "--urls",
        f"{hub_a.ws_url},{hub_b.ws_url}",
        "--hub-peers",
        "mother-goose",
    )
    assert src_set.returncode == 0, src_set.stderr

    # `node_rep` converges via Replica Hub B and `os.execv`s in-place (`pid_rep`), connecting to Hub A too!
    async def _rep_node_dual_connected() -> bool:
        ba = await _ws_banner(hub_a.ws_url, local_ip="127.0.0.3")
        bb = await _ws_banner(hub_b.ws_url, local_ip="127.0.0.3")
        return "cat-food/main" in (ba.get("peers") or []) and "cat-food/main" in (bb.get("peers") or [])

    await _wait_until(_rep_node_dual_connected, timeout=8.0, desc="replica node dual-connected via execv")
    assert node_rep.proc is not None and node_rep.proc.poll() is None and node_rep.proc.pid == pid_rep

    assert _git(clones["zfold"], git_env, "rev-parse", "HEAD") == sha_v2
    rep_node_cfg = json.loads((node_rep.cfg_dir / "config.json").read_text(encoding="utf-8"))
    assert rep_node_cfg["urls"] == [hub_a.ws_url, hub_b.ws_url]
    assert rep_node_cfg["hub_peers"] == ["mother-goose"]

    _, hb_health = await asyncio.to_thread(_http_get_json, f"{hub_b.gateway_http_url}/v1/health")
    assert hb_health["fleet"]["rev"] == 1
    assert hb_health["fleet"]["commit"] == sha_v2
    assert hb_health["fleet_nodes"]["cat-food"]["rev"] == 1
    assert hb_health["fleet_nodes"]["cat-food"]["commit"] == sha_v2
    assert hb_health["fleet_nodes"]["cat-food"]["converged"] is True

    cur_gen = int((hub_b.cfg_dir / "auth-current").read_text(encoding="utf-8").strip())
    assert (hub_b.cfg_dir / "auth-gen" / str(cur_gen) / "fleet.json").is_file()

    (hub_a.cfg_dir / "fleet.json").unlink()
    await _wait_until(
        lambda: _http_get_json(f"{hub_b.gateway_http_url}/v1/health")[1].get("fleet") is None,
        timeout=5.0,
        desc="Replica Hub B disabled fleet after source fleet.json removal",
    )
    b_after_del = await _ws_banner(hub_b.ws_url, local_ip="127.0.0.3")
    assert "fleet" not in b_after_del

    node_rep.stop()
    hub_b.stop()
    hub_a.stop()
    assert_ports_free(ports, context="fleet_05_post")


# ======================================================================
# 6. Legacy `e7d0da2` Client Backward Compatibility with Active `fleet.json`
# ======================================================================


@pytest.mark.chaos
async def test_fleet_06_legacy_e7d0da2_client_backward_compatibility(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    node_proc_registry: Any,
) -> None:
    nodes, _ = node_proc_registry
    ports = (18961, 18962)
    for p in ports:
        validate_sandbox_port(p)
    assert_ports_free(ports, context="fleet_06_pre")

    legacy_bus_root = _extract_legacy_package(tmp_path / "legacy_e7d0da2_bus")

    hub_v2 = hub_factory.start_hub(
        "fleet_s6_hub",
        port=18961,
        gateway_port=18962,
        local_node="cat-food",
        allowed=("cat-food", "mother-goose"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )

    set_res = await asyncio.to_thread(
        _run_cli,
        hub_v2.build_env(),
        "fleet",
        "set",
        "--commit",
        "c05b690",
        "--urls",
        hub_v2.ws_url,
        "--hub-peers",
        "mother-goose",
        "--min-caps",
        "v2,journal",
    )
    assert set_res.returncode == 0, set_res.stderr
    b_check = await _ws_banner(hub_v2.ws_url)
    assert b_check["fleet"]["rev"] == 1

    legacy_worker = ManagedNodeProc(
        node_name="cat-food",
        base_dir=tmp_path / "legacy_worker",
        bus_root=legacy_bus_root,
        response_prefix="LEGACY_E7_OK",
    )
    nodes.append(legacy_worker)
    legacy_worker.start(
        label="main",
        mode="hybrid",
        peers="cat-food",
        url=hub_v2.ws_url,
        no_auto_update=True,
    )

    legacy_caller = ManagedNodeProc(
        node_name="cat-food",
        base_dir=tmp_path / "legacy_caller",
        bus_root=legacy_bus_root,
    )
    caller_env = legacy_caller.build_env()

    async def _legacy_peers_ready() -> bool:
        r = await asyncio.to_thread(
            _run_cli, caller_env, "peers", "--url", hub_v2.ws_url
        )
        return r.returncode == 0 and "cat-food/main" in r.stdout

    await _wait_until(_legacy_peers_ready, timeout=6.0, desc="legacy e7d0da2 worker registered")

    rpc_sync = await asyncio.to_thread(
        _run_cli,
        caller_env,
        "send",
        "--to",
        "cat-food/main",
        "--wait",
        "8",
        "--url",
        hub_v2.ws_url,
        "[id=leg-sync-1] hello from e7d0da2",
    )
    assert rpc_sync.returncode == 0, f"{rpc_sync.stderr}\n{rpc_sync.stdout}"
    assert "LEGACY_E7_OK:leg-sync-1" in rpc_sync.stdout

    async_cid = "leg-async-cid-1"
    send_async = await asyncio.to_thread(
        _run_cli,
        caller_env,
        "send",
        "--to",
        "cat-food/main",
        "--kind",
        "rpc",
        "--corr-id",
        async_cid,
        "--url",
        hub_v2.ws_url,
        f"[id={async_cid}] async job",
    )
    assert send_async.returncode == 0, send_async.stderr

    res_async = await asyncio.to_thread(
        _run_cli,
        caller_env,
        "result",
        async_cid,
        "--wait",
        "8",
        "--json",
        "--url",
        hub_v2.ws_url,
    )
    assert res_async.returncode == 0, res_async.stderr
    res_doc = json.loads(res_async.stdout)
    assert res_doc["status"] == "ok"
    assert "LEGACY_E7_OK:leg-async-cid-1" in res_doc["text"]

    say_res = await asyncio.to_thread(
        _run_cli,
        caller_env,
        "send",
        "--to",
        "cat-food/inbox",
        "--say",
        "--url",
        hub_v2.ws_url,
        "legacy event note",
    )
    assert say_res.returncode == 0, say_res.stderr

    doc_res = await asyncio.to_thread(
        _run_cli,
        caller_env,
        "doctor",
        "--url",
        hub_v2.ws_url,
    )
    assert doc_res.returncode == 0, doc_res.stderr
    doc_json = json.loads(doc_res.stdout)
    assert doc_json["hub_ok"] is True

    assert legacy_worker.proc is not None and legacy_worker.proc.poll() is None

    legacy_worker.stop()
    hub_v2.stop()
    assert_ports_free(ports, context="fleet_06_post")


# ======================================================================
# 7. `run_supervise` Coexistence (No Double Spawn) & `os.execv` Failure Recovery
# ======================================================================


@pytest.mark.chaos
async def test_fleet_07_execv_under_supervise_no_double_spawn_and_execv_failure_recovery(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    node_proc_registry: Any,
) -> None:
    nodes, raw_procs = node_proc_registry
    ports = (18941, 18942, 18943, 18944)
    for p in ports:
        validate_sandbox_port(p)
    assert_ports_free(ports, context="fleet_07_pre")

    _, publisher, clones, git_env, sha_v1 = _create_origin_and_clones(
        tmp_path / "git_s7", ["sup", "execfail"]
    )

    hub_1 = hub_factory.start_hub(
        "fleet_s7_hub1",
        port=18941,
        gateway_port=18942,
        allowed=("cat-food", "node-sup", "node-execfail"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )
    hub_2 = hub_factory.start_hub(
        "fleet_s7_hub2",
        port=18943,
        gateway_port=18944,
        allowed=("cat-food", "node-sup", "node-execfail"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )

    # --- Part A: `abus supervise` child `os.execv` keeps same child PID and spawns 0 duplicate workers ---
    sup_helper = ManagedNodeProc(
        node_name="node-sup",
        base_dir=tmp_path / "node_sup",
        repo_dir=clones["sup"],
        extra_env={"BUS_FLEET_POLL_INTERVAL": "0.3"},
    )
    sup_helper.write_config({"urls": [hub_1.ws_url]})
    sup_log_file = tmp_path / "node_sup" / "supervise.log"
    sup_cwd = tmp_path / "node_sup_cwd"
    sup_cwd.mkdir(parents=True, exist_ok=True)

    sup_proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "agent_bus.cli",
            "supervise",
            "--label",
            "main",
            "--mode",
            "hybrid",
            "--peers",
            "cat-food",
            "--log-file",
            str(sup_log_file),
        ],
        env=sup_helper.build_env(),
        cwd=str(sup_cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    raw_procs.append(sup_proc)

    async def _sup_child_connected() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        return (
            "node-sup/main" in (b.get("peers") or [])
            and len(_read_worker_lock_pids(sup_helper.cfg_dir)) == 1
        )

    await _wait_until(_sup_child_connected, timeout=6.0, desc="supervised child connected to hub_1")
    child_pid_before = _read_worker_lock_pids(sup_helper.cfg_dir)[0]
    assert child_pid_before != sup_proc.pid

    # --- Part B: `node-execfail` configured with invalid `BUS_TEST_EXECV_EXECUTABLE` so `os.execv` raises `OSError` ---
    node_fail = ManagedNodeProc(
        node_name="node-execfail",
        base_dir=tmp_path / "node_execfail",
        repo_dir=clones["execfail"],
        extra_env={
            "BUS_FLEET_POLL_INTERVAL": "0.3",
            "BUS_TEST_EXECV_EXECUTABLE": str(tmp_path / "nonexistent-python-binary-for-execv-fail"),
        },
    )
    nodes.append(node_fail)
    node_fail.write_config({"urls": [hub_1.ws_url]})
    pid_fail_before = node_fail.start(label="main", mode="hybrid", peers="cat-food").pid

    async def _fail_node_connected() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        return "node-execfail/main" in (b.get("peers") or [])

    await _wait_until(_fail_node_connected, timeout=5.0, desc="node-execfail connected to hub_1")

    # Push `sha_v2` and set `fleet` `rev=1` (`urls=[hub_1, hub_2]`)!
    sha_v2 = _push_new_commit(publisher, git_env, "sup_v2.txt", "v2\n", "v2")
    set_r1 = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v2,
        "--urls",
        f"{hub_1.ws_url},{hub_2.ws_url}",
    )
    assert set_r1.returncode == 0, set_r1.stderr

    # Verify Part A (`node-sup`): child `os.execv`s in-place (`child_pid_after == child_pid_before`),
    # connects to `hub_2`, and supervisor log shows `"starting child:"` ONLY ONCE!
    async def _sup_child_on_hub2() -> bool:
        b2 = await _ws_banner(hub_2.ws_url)
        return (
            "node-sup/main" in (b2.get("peers") or [])
            and sup_log_file.is_file()
            and "re-executing in-place via os.execv" in sup_log_file.read_text(encoding="utf-8")
        )

    await _wait_until(_sup_child_on_hub2, timeout=8.0, desc="supervised child re-executed and joined hub_2")
    await asyncio.sleep(0.3)
    assert sup_proc.poll() is None
    sup_log_txt = sup_log_file.read_text(encoding="utf-8")
    assert sup_log_txt.count("starting child:") == 1
    assert "child exited with code=" not in sup_log_txt
    assert _read_worker_lock_pids(sup_helper.cfg_dir) == [child_pid_before]
    assert _git(clones["sup"], git_env, "rev-parse", "HEAD") == sha_v2

    sup_rpc = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "send",
        "--to",
        "node-sup/main",
        "--wait",
        "5",
        "--url",
        hub_2.ws_url,
        "[id=s7-sup-rpc] verify supervised pid",
    )
    assert sup_rpc.returncode == 0, sup_rpc.stderr
    assert f"FLEET_STUB_OK:s7-sup-rpc:ppid={child_pid_before}" in sup_rpc.stdout

    # Verify Part B (`node-execfail`): `os.execv` fails with `FileNotFoundError`, records
    # `blocked:execv-failed:FileNotFoundError` in `fleet-applied.json` AND hub `fleet_nodes`,
    # and the current `node-execfail` process stays alive (`pid_fail_before`) and answers RPCs!
    async def _execfail_reported_blocked() -> bool:
        _, h = await asyncio.to_thread(_http_get_json, f"{hub_1.gateway_http_url}/v1/health")
        res = (h.get("fleet_nodes") or {}).get("node-execfail", {}).get("result") or ""
        return res.startswith("blocked:execv-failed:")

    await _wait_until(
        _execfail_reported_blocked,
        timeout=8.0,
        desc="node-execfail reported blocked:execv-failed:*",
    )
    assert node_fail.proc is not None and node_fail.proc.poll() is None
    assert node_fail.proc.pid == pid_fail_before

    fail_applied = json.loads((node_fail.cfg_dir / "fleet-applied.json").read_text(encoding="utf-8"))
    assert fail_applied["rev"] == 1
    assert fail_applied["result"] == "blocked:execv-failed:FileNotFoundError"

    fail_rpc = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "send",
        "--to",
        "node-execfail/main",
        "--wait",
        "5",
        "--url",
        hub_1.ws_url,
        "[id=s7-fail-rpc] still alive after execv error",
    )
    assert fail_rpc.returncode == 0, fail_rpc.stderr
    assert f"FLEET_STUB_OK:s7-fail-rpc:ppid={pid_fail_before}" in fail_rpc.stdout

    node_fail.stop()
    if sup_proc.poll() is None:
        os.killpg(os.getpgid(sup_proc.pid), signal.SIGTERM)
        sup_proc.wait(timeout=4.0)
    hub_2.stop()
    hub_1.stop()
    assert_ports_free(ports, context="fleet_07_post")


# ======================================================================
# 8. Non-Git Checkout (`blocked:not-a-git-repo`) + Config Apply + Worktree `.git` File
# ======================================================================


@pytest.mark.chaos
async def test_fleet_08_non_git_checkout_blocks_commit_and_applies_config(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    node_proc_registry: Any,
) -> None:
    nodes, _ = node_proc_registry
    ports = (18951, 18952, 18953, 18954)
    for p in ports:
        validate_sandbox_port(p)
    assert_ports_free(ports, context="fleet_08_pre")

    # Verify `has_git_checkout` handles normal clone (`.git` dir), git worktree (`.git` file), and plain dir!
    _, _, clones, git_env, sha_v1 = _create_origin_and_clones(tmp_path / "git_s8", ["base"])
    wt_dir = tmp_path / "git_s8" / "wt_linked"
    _git(clones["base"], git_env, "worktree", "add", "-b", "wt-branch", str(wt_dir), sha_v1)
    assert (clones["base"] / ".git").is_dir() and has_git_checkout(clones["base"]) is True
    assert (wt_dir / ".git").is_file() and has_git_checkout(wt_dir) is True

    # Create a parent directory that HAS `.git`, and inside it a child checkout WITHOUT `.git`!
    # This proves `not (repo_dir / ".git").exists()` never climbs into a parent `.git`!
    outer_parent = tmp_path / "outer_parent_with_git"
    outer_parent.mkdir(parents=True, exist_ok=True)
    _git(outer_parent, git_env, "init")
    (outer_parent / "dummy.txt").write_text("parent repo\n", encoding="utf-8")
    _git(outer_parent, git_env, "add", "dummy.txt")
    _git(outer_parent, git_env, "commit", "-q", "-m", "parent commit")

    plain_checkout = (outer_parent / "plain_tarball_checkout").resolve()
    plain_bus_root = _copy_agent_bus_tree(plain_checkout)
    assert not (plain_checkout / ".git").exists()
    assert has_git_checkout(plain_checkout) is False

    hub_1 = hub_factory.start_hub(
        "fleet_s8_hub1",
        port=18951,
        gateway_port=18952,
        allowed=("cat-food", "node-nogit", "mother-goose"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )
    hub_2 = hub_factory.start_hub(
        "fleet_s8_hub2",
        port=18953,
        gateway_port=18954,
        allowed=("cat-food", "node-nogit", "mother-goose"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )

    # Launch `node-nogit` WITHOUT `BUS_FLEET_REPO_DIR` (`repo_dir=None`), running from `plain_bus_root`!
    node_nogit = ManagedNodeProc(
        node_name="node-nogit",
        base_dir=tmp_path / "node_nogit_state",
        repo_dir=None,
        bus_root=plain_bus_root,
        extra_env={"BUS_FLEET_POLL_INTERVAL": "0.3"},
    )
    nodes.append(node_nogit)
    assert "BUS_FLEET_REPO_DIR" not in node_nogit.build_env()
    node_nogit.write_config({"urls": [hub_1.ws_url], "custom_note": "keep-nogit"})
    pid_nogit = node_nogit.start(label="main", mode="hybrid", peers="cat-food").pid

    async def _nogit_on_hub1() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        return "node-nogit/main" in (b.get("peers") or [])

    await _wait_until(_nogit_on_hub1, timeout=5.0, desc="node-nogit connected to hub_1")

    set_res = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v1,
        "--urls",
        f"{hub_1.ws_url},{hub_2.ws_url}",
        "--hub-peers",
        "mother-goose",
    )
    assert set_res.returncode == 0, set_res.stderr

    # `node-nogit` skips `commit` (`blocked:not-a-git-repo`), applies `config.json`, `os.execv`s ONCE
    # (`pid_nogit` preserved), connects to `hub_2`, and does NOT loop!
    async def _nogit_on_hub2() -> bool:
        b2 = await _ws_banner(hub_2.ws_url)
        return "node-nogit/main" in (b2.get("peers") or [])

    await _wait_until(_nogit_on_hub2, timeout=8.0, desc="node-nogit connected to hub_2 after config update")
    await asyncio.sleep(0.4)
    assert node_nogit.proc is not None and node_nogit.proc.poll() is None
    assert node_nogit.proc.pid == pid_nogit
    assert node_nogit.read_stdout().count("re-executing in-place via os.execv") == 1

    ng_cfg = json.loads((node_nogit.cfg_dir / "config.json").read_text(encoding="utf-8"))
    assert ng_cfg["urls"] == [hub_1.ws_url, hub_2.ws_url]
    assert ng_cfg["hub_peers"] == ["mother-goose"]
    assert ng_cfg["custom_note"] == "keep-nogit"
    assert (node_nogit.cfg_dir / "config.json.bak").is_file()

    ng_applied = json.loads((node_nogit.cfg_dir / "fleet-applied.json").read_text(encoding="utf-8"))
    assert ng_applied["rev"] == 1
    assert ng_applied["checkout"] == str(plain_checkout)
    assert ng_applied["commit"] == ""
    assert ng_applied["result"] == "blocked:not-a-git-repo"
    assert ng_applied["commit_skipped"] is True
    assert ng_applied["config_changed"] is True
    assert ng_applied["code_changed"] is False

    st_res = await asyncio.to_thread(
        _run_cli, hub_1.build_env(), "fleet", "status", "--url", hub_1.ws_url, "--json"
    )
    assert st_res.returncode == 0
    st_json = json.loads(st_res.stdout)
    assert st_json["nodes"]["node-nogit"]["result"] == "blocked:not-a-git-repo"
    assert st_json["nodes"]["node-nogit"]["checkout"] == str(plain_checkout)
    assert st_json["nodes"]["node-nogit"]["converged"] is False

    node_nogit.stop()
    hub_2.stop()
    hub_1.stop()
    assert_ports_free(ports, context="fleet_08_post")


# ======================================================================
# 9. Multi-Checkout `__file__` Auto-Detection (`agent-command-chain-template` vs `act`)
# ======================================================================


@pytest.mark.chaos
async def test_fleet_09_multi_checkout_dunder_file_detection_and_status_mismatch(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    node_proc_registry: Any,
) -> None:
    nodes, _ = node_proc_registry
    ports = (18971, 18972, 18973, 18974)
    for p in ports:
        validate_sandbox_port(p)
    assert_ports_free(ports, context="fleet_09_pre")

    checkout_a_path = tmp_path / "sim_zfold8" / "agent-command-chain-template"
    checkout_b_path = tmp_path / "sim_zfold8" / "act"
    _, publisher, clones, git_env, sha_v1 = _create_origin_and_clones(
        tmp_path / "git_s9",
        ["acct", "act"],
        with_bus_package=True,
        exact_clone_paths={"acct": checkout_a_path, "act": checkout_b_path},
    )
    checkout_a = clones["acct"]
    checkout_b = clones["act"]
    assert (checkout_a / "bus" / "agent_bus" / "fleet.py").is_file()
    assert (checkout_b / "bus" / "agent_bus" / "fleet.py").is_file()

    hub_1 = hub_factory.start_hub(
        "fleet_s9_hub1",
        port=18971,
        gateway_port=18972,
        allowed=("cat-food", "z-fold8"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )
    hub_2 = hub_factory.start_hub(
        "fleet_s9_hub2",
        port=18973,
        gateway_port=18974,
        allowed=("cat-food", "z-fold8"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )

    shared_cfg_dir = tmp_path / "sim_zfold8" / "shared_cfg"
    shared_cfg_dir.mkdir(parents=True, exist_ok=True)

    # Start `z-fold8` (`--label main`) running from `checkout_a / "bus"` with NO `BUS_FLEET_REPO_DIR`!
    proc_a = ManagedNodeProc(
        node_name="z-fold8",
        base_dir=tmp_path / "proc_a",
        repo_dir=None,
        bus_root=checkout_a / "bus",
        cfg_dir_override=shared_cfg_dir,
        extra_env={"BUS_FLEET_POLL_INTERVAL": "0.3"},
    )
    nodes.append(proc_a)
    assert "BUS_FLEET_REPO_DIR" not in proc_a.build_env()
    proc_a.write_config({"urls": [hub_1.ws_url]})
    pid_a = proc_a.start(label="main", mode="hybrid", peers="cat-food").pid

    async def _zfold_main_connected() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        return "z-fold8/main" in (b.get("peers") or [])

    await _wait_until(_zfold_main_connected, timeout=5.0, desc="z-fold8/main connected from checkout_a")

    # Push `sha_v2` to `origin` and publish `rev=1` (`[hub_1, hub_2]`)
    sha_v2 = _push_new_commit(
        publisher,
        git_env,
        "dunder_file_v2.txt",
        "updated via __file__\n",
        "v2 for multi-checkout",
    )
    set_res = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "fleet",
        "set",
        "--commit",
        sha_v2,
        "--urls",
        f"{hub_1.ws_url},{hub_2.ws_url}",
    )
    assert set_res.returncode == 0, set_res.stderr

    async def _zfold_main_on_hub2() -> bool:
        b2 = await _ws_banner(hub_2.ws_url)
        return "z-fold8/main" in (b2.get("peers") or [])

    await _wait_until(_zfold_main_on_hub2, timeout=8.0, desc="checkout_a converged & execv'd to hub_2")
    assert proc_a.proc is not None and proc_a.proc.poll() is None and proc_a.proc.pid == pid_a

    # 1) ONLY `checkout_a` (`agent-command-chain-template`) was fast-forwarded via `__file__`;
    #    `checkout_b` (`act`) was NOT touched at all (`HEAD == sha_v1`)!
    assert _git(checkout_a, git_env, "rev-parse", "HEAD") == sha_v2
    assert (checkout_a / "dunder_file_v2.txt").is_file()
    assert _git(checkout_b, git_env, "rev-parse", "HEAD") == sha_v1
    assert not (checkout_b / "dunder_file_v2.txt").exists()

    applied_after_a = json.loads((shared_cfg_dir / "fleet-applied.json").read_text(encoding="utf-8"))
    assert applied_after_a["checkout"] == str(checkout_a)
    assert applied_after_a["commit"] == sha_v2
    assert str(checkout_a) in applied_after_a["checkouts"]

    # 2) Start second process on the same device (`z-fold8`, `--label funnel`, `--no-auto-update`)
    #    from `checkout_b / "bus"` (also NO `BUS_FLEET_REPO_DIR`), sharing `shared_cfg_dir`!
    proc_b = ManagedNodeProc(
        node_name="z-fold8",
        base_dir=tmp_path / "proc_b",
        repo_dir=None,
        bus_root=checkout_b / "bus",
        cfg_dir_override=shared_cfg_dir,
        extra_env={"BUS_FLEET_POLL_INTERVAL": "0.3"},
    )
    nodes.append(proc_b)
    assert "BUS_FLEET_REPO_DIR" not in proc_b.build_env()
    proc_b.start(label="funnel", mode="watch", no_auto_update=True)

    # Wait until `shared_cfg_dir/fleet-applied.json` records BOTH checkouts AND hub reports mismatch!
    async def _both_checkouts_reported_on_hub() -> bool:
        _, h = await asyncio.to_thread(_http_get_json, f"{hub_1.gateway_http_url}/v1/health")
        zf = (h.get("fleet_nodes") or {}).get("z-fold8") or {}
        cks = zf.get("checkouts") or {}
        return str(checkout_a) in cks and str(checkout_b) in cks

    await _wait_until(
        _both_checkouts_reported_on_hub,
        timeout=8.0,
        desc="both checkouts reported on hub for z-fold8",
    )

    applied_both = json.loads((shared_cfg_dir / "fleet-applied.json").read_text(encoding="utf-8"))
    assert applied_both["checkouts"][str(checkout_a)]["commit"] == sha_v2
    assert applied_both["checkouts"][str(checkout_a)]["result"] == "ok"
    assert applied_both["checkouts"][str(checkout_b)]["commit"] == sha_v1
    assert applied_both["checkouts"][str(checkout_b)]["result"] == "ok:no_auto_update"
    assert _git(checkout_b, git_env, "rev-parse", "HEAD") == sha_v1

    # 3) Verify `abus fleet status` (`--json` & table) highlights multi-checkout mismatch on `z-fold8`!
    st_json_res = await asyncio.to_thread(
        _run_cli, hub_1.build_env(), "fleet", "status", "--url", hub_1.ws_url, "--json"
    )
    assert st_json_res.returncode == 0, st_json_res.stderr
    st_doc = json.loads(st_json_res.stdout)
    zf_node = st_doc["nodes"]["z-fold8"]
    assert zf_node["checkout_mismatch"] is True
    assert zf_node["target_mismatch"] is True
    assert zf_node["converged"] is False
    assert st_doc["all_converged"] is False
    assert st_doc["mismatch_count"] == 1
    assert zf_node["checkouts"][str(checkout_a)]["commit"] == sha_v2
    assert zf_node["checkouts"][str(checkout_a)]["converged"] is True
    assert zf_node["checkouts"][str(checkout_b)]["commit"] == sha_v1
    assert zf_node["checkouts"][str(checkout_b)]["converged"] is False

    st_tbl_res = await asyncio.to_thread(
        _run_cli, hub_1.build_env(), "fleet", "status", "--url", hub_1.gateway_http_url
    )
    assert st_tbl_res.returncode == 0
    tbl_txt = st_tbl_res.stdout
    assert str(checkout_a) in tbl_txt
    assert str(checkout_b) in tbl_txt
    assert "[MISMATCH]" in tbl_txt
    assert "Mismatches:" in tbl_txt
    assert "multi-checkout drift" in tbl_txt
    assert "mismatches=1" in tbl_txt

    proc_b.stop()
    proc_a.stop()
    hub_2.stop()
    hub_1.stop()
    assert_ports_free(ports, context="fleet_09_post")


# ======================================================================
# 10. Hub `SIGSTOP` (Sleep / Network Freeze) -> `SIGCONT` Reconnect & Converge
# ======================================================================


@pytest.mark.chaos
async def test_fleet_10_hub_sigstop_sleep_disconnect_sigcont_reconnect_convergence(
    tmp_path: pathlib.Path,
    hub_factory: HubController,
    node_proc_registry: Any,
) -> None:
    nodes, _ = node_proc_registry
    ports = (18981, 18982, 18983, 18984)
    for p in ports:
        validate_sandbox_port(p)
    assert_ports_free(ports, context="fleet_10_pre")

    _, publisher, clones, git_env, sha_v1 = _create_origin_and_clones(
        tmp_path / "git_s10", ["sleepnode"]
    )

    hub_1 = hub_factory.start_hub(
        "fleet_s10_hub1",
        port=18981,
        gateway_port=18982,
        allowed=("cat-food", "node-sleep"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )
    hub_2 = hub_factory.start_hub(
        "fleet_s10_hub2",
        port=18983,
        gateway_port=18984,
        allowed=("cat-food", "node-sleep"),
        extra_env={"BUS_AUTH_WATCH_INTERVAL": "0.05"},
    )

    node_sleep = ManagedNodeProc(
        node_name="node-sleep",
        base_dir=tmp_path / "node_sleep",
        repo_dir=clones["sleepnode"],
        extra_env={
            "BUS_WS_PING_INTERVAL": "0.25",
            "BUS_WS_PING_TIMEOUT": "0.35",
            "BUS_DIAL_TIMEOUT": "0.5",
            "BUS_HELLO_TIMEOUT": "0.5",
            "BUS_BANNER_TIMEOUT": "0.8",
            "BUS_FLEET_POLL_INTERVAL": "0.25",
        },
    )
    nodes.append(node_sleep)
    node_sleep.write_config({"urls": [hub_1.ws_url]})
    pid_sleep = node_sleep.start(label="main", mode="hybrid", peers="cat-food").pid

    async def _sleep_node_connected() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        return "node-sleep/main" in (b.get("peers") or [])

    await _wait_until(_sleep_node_connected, timeout=5.0, desc="node-sleep initially connected")

    # Push `sha_v2` to `origin`, then freeze `hub_1` with `SIGSTOP`!
    sha_v2 = _push_new_commit(publisher, git_env, "after_sleep.txt", "woke up\n", "v2")
    assert sha_v2 != sha_v1

    hub_1.sigstop()
    try:
        # While `hub_1` is frozen, stage `rev=1` (`sha_v2`, `[hub_1, hub_2]`) onto `hub_1`'s `cfg_dir`
        # via `abus fleet set` (runs purely against files in `hub_1.cfg_dir`) AND wait until
        # `node-sleep` detects the frozen socket via WS ping timeout (`[disconnected:...]`)!
        set_while_frozen = await asyncio.to_thread(
            _run_cli,
            hub_1.build_env(),
            "fleet",
            "set",
            "--commit",
            sha_v2,
            "--urls",
            f"{hub_1.ws_url},{hub_2.ws_url}",
        )
        assert set_while_frozen.returncode == 0, set_while_frozen.stderr

        await _wait_until(
            lambda: "[disconnected:" in node_sleep.read_stdout(),
            timeout=5.0,
            desc="node-sleep detected WS disconnect while hub_1 was SIGSTOPped",
        )
        assert node_sleep.proc is not None and node_sleep.proc.poll() is None
    finally:
        # Wake `hub_1` with `SIGCONT`!
        hub_1.sigcont()

    # Upon `SIGCONT`, `node-sleep` reconnects to `hub_1`, receives `rev=1`, fast-forwards to `sha_v2`,
    # updates `config.json`, `os.execv`s in-place (`pid_sleep`), and connects to BOTH `hub_1` & `hub_2`!
    async def _sleep_node_recovered_and_converged() -> bool:
        b1 = await _ws_banner(hub_1.ws_url)
        b2 = await _ws_banner(hub_2.ws_url)
        return (
            "node-sleep/main" in (b1.get("peers") or [])
            and "node-sleep/main" in (b2.get("peers") or [])
            and _git(clones["sleepnode"], git_env, "rev-parse", "HEAD") == sha_v2
        )

    await _wait_until(
        _sleep_node_recovered_and_converged,
        timeout=10.0,
        desc="node-sleep reconnected after SIGCONT and converged via os.execv",
    )
    assert node_sleep.proc is not None and node_sleep.proc.poll() is None
    assert node_sleep.proc.pid == pid_sleep

    await _wait_until(
        lambda: _http_get_json(f"{hub_1.gateway_http_url}/v1/health")[1]
        .get("fleet_nodes", {})
        .get("node-sleep", {})
        .get("converged")
        is True,
        timeout=5.0,
        desc="hub_1 marked node-sleep converged after SIGCONT",
    )

    rpc_after = await asyncio.to_thread(
        _run_cli,
        hub_1.build_env(),
        "send",
        "--to",
        "node-sleep/main",
        "--wait",
        "5",
        "--url",
        hub_2.ws_url,
        "[id=s10-wake-rpc] ping after wake",
    )
    assert rpc_after.returncode == 0, rpc_after.stderr
    assert f"FLEET_STUB_OK:s10-wake-rpc:ppid={pid_sleep}" in rpc_after.stdout

    node_sleep.stop()
    hub_2.stop()
    hub_1.stop()
    assert_ports_free(ports, context="fleet_10_post")


# ======================================================================
# 11. Static AST/Grep POSIX Guard Audit & Termux/Windows Unit Checks
# ======================================================================


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _has_enclosing_guard(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Return True if `node` sits inside a `try` block, `with contextlib.suppress(...)`, or platform `if`."""
    cur: ast.AST | None = node
    while cur is not None:
        parent = parents.get(cur)
        if isinstance(parent, ast.Try) and cur in parent.body:
            return True
        if isinstance(parent, ast.With):
            for item in parent.items:
                txt = ast.unparse(item.context_expr)
                if "suppress" in txt:
                    return True
        if isinstance(parent, ast.If):
            cond_txt = ast.unparse(parent.test)
            if any(
                tok in cond_txt
                for tok in (
                    "os.name",
                    "sys.platform",
                    "hasattr",
                    "getattr",
                    "fcntl is not None",
                    "msvcrt is not None",
                    "sigterm_num is not None",
                )
            ):
                return True
        cur = parent
    return False


def test_fleet_11_static_ast_posix_guards_and_cross_platform_unit_checks(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    py_files = sorted(PACKAGE_DIR.glob("*.py"))
    assert len(py_files) >= 10

    stdlib_names = set(sys.stdlib_module_names) | {"__future__"}
    allowed_external_top = {"websockets"}
    allowed_optional_fn_imports = {"fastapi"}
    posix_only_modules = {"fcntl", "pwd", "grp", "termios", "pty", "resource", "syslog", "posix"}
    guarded_os_attrs = {
        "fork",
        "forkpty",
        "setsid",
        "killpg",
        "getpgid",
        "setpgid",
        "chown",
        "fchown",
        "lchown",
        "chmod",
        "kill",
        "O_DIRECTORY",
    }

    for py_path in py_files:
        src = py_path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(py_path))
        parents = _build_parent_map(tree)

        # 1) Verify imports: stdlib + `websockets` only; any POSIX-only / `msvcrt` import MUST be inside `try`!
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_mod = alias.name.split(".", 1)[0]
                    if top_mod in posix_only_modules or top_mod == "msvcrt":
                        assert _has_enclosing_guard(node, parents), (
                            f"Unguarded platform-specific import `{alias.name}` in {py_path.name}:{node.lineno}"
                        )
                    elif top_mod not in stdlib_names and top_mod not in allowed_external_top:
                        assert top_mod in allowed_optional_fn_imports, (
                            f"Disallowed external dependency `{alias.name}` in {py_path.name}:{node.lineno}"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue
                top_mod = (node.module or "").split(".", 1)[0]
                if not top_mod:
                    continue
                if top_mod in posix_only_modules or top_mod == "msvcrt":
                    assert _has_enclosing_guard(node, parents), (
                        f"Unguarded platform-specific from-import `{top_mod}` in {py_path.name}:{node.lineno}"
                    )
                elif top_mod not in stdlib_names and top_mod not in allowed_external_top:
                    # Optional `fastapi` allowed strictly inside `create_fastapi_app` function scope
                    in_fn = False
                    cur_p = parents.get(node)
                    while cur_p is not None:
                        if isinstance(cur_p, ast.FunctionDef):
                            in_fn = True
                            break
                        cur_p = parents.get(cur_p)
                    assert top_mod in allowed_optional_fn_imports and in_fn, (
                        f"Disallowed top-level external import `{top_mod}` in {py_path.name}:{node.lineno}"
                    )

        # 2) Verify POSIX-specific attribute accesses (`os.*`, `fcntl.*`, `signal.SIG*`, `add_signal_handler`)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            attr = node.attr
            owner = ast.unparse(node.value)
            if owner == "fcntl":
                assert _has_enclosing_guard(node, parents), (
                    f"Unguarded `fcntl.{attr}` in {py_path.name}:{node.lineno}"
                )
            elif owner == "os" and attr in guarded_os_attrs:
                assert _has_enclosing_guard(node, parents), (
                    f"Unguarded `os.{attr}` in {py_path.name}:{node.lineno}"
                )
            elif owner == "os" and attr.startswith("execv"):
                # `_exec_self_in_place` checks `if not hasattr(os, "execv"): raise OSError(...)` immediately above
                fn_parent = parents.get(node)
                while fn_parent is not None and not isinstance(fn_parent, ast.FunctionDef):
                    fn_parent = parents.get(fn_parent)
                fn_src = ast.unparse(fn_parent) if fn_parent is not None else ""
                assert _has_enclosing_guard(node, parents) or 'hasattr(os, "execv")' in fn_src or "hasattr(os, 'execv')" in fn_src, (
                    f"Unguarded `os.{attr}` in {py_path.name}:{node.lineno}"
                )
            elif owner == "signal" and attr.startswith("SIG"):
                assert False, (
                    f"Direct `signal.{attr}` attribute access found in {py_path.name}:{node.lineno}; "
                    "use `getattr(signal, 'SIG...', None)` instead"
                )
            elif attr in ("add_signal_handler", "remove_signal_handler"):
                assert _has_enclosing_guard(node, parents), (
                    f"Unguarded `{owner}.{attr}` in {py_path.name}:{node.lineno}"
                )

        # 3) Static regex check: zero `os.fork`, `os.setsid`, `os.killpg` in `agent_bus/*.py`
        assert not re.search(r"\bos\.(fork|setsid|killpg)\b", src), (
            f"Forbidden unportable process call in {py_path.name}"
        )

    # 4) Unit test `build_exec_argv()` (`sys.orig_argv` preferred, `sys.argv` fallback)
    monkeypatch.setattr(
        sys,
        "orig_argv",
        ["python3", "-u", "-m", "agent_bus.cli", "run", "--label", "main"],
    )
    assert build_exec_argv("/usr/bin/python3") == [
        "/usr/bin/python3",
        "-u",
        "-m",
        "agent_bus.cli",
        "run",
        "--label",
        "main",
    ]
    monkeypatch.setattr(sys, "orig_argv", [])
    monkeypatch.setattr(sys, "argv", ["/opt/bus/cli.py", "run", "--label", "fallback"])
    assert build_exec_argv("/usr/bin/python3") == [
        "/usr/bin/python3",
        "/opt/bus/cli.py",
        "run",
        "--label",
        "fallback",
    ]

    # 5) Unit test `home_dir()` on simulated Termux and Windows environments
    monkeypatch.setenv("TERMUX_VERSION", "0.118.1")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    assert bus_config.is_termux() is True
    assert str(bus_config.home_dir()) == "/data/data/com.termux/files/home"

    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.setattr(bus_config, "is_windows", lambda: True)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "win_user"))
    assert bus_config.home_dir() == tmp_path / "win_user"

    # 6) Unit test `journal.py` `WorkerSession` fallback when both `fcntl` and `msvcrt` are `None`
    monkeypatch.setattr(bus_journal, "fcntl", None)
    monkeypatch.setattr(bus_journal, "msvcrt", None)
    ws_cfg = tmp_path / "nofcntl_cfg"
    sess = bus_journal.register_worker_session(ws_cfg, "main")
    try:
        assert bus_journal.is_worker_owner_alive(ws_cfg, sess.worker_id, sess.pid) is True
        assert bus_journal.is_worker_owner_alive(ws_cfg, "dead-worker-999999", 99999999) is False
    finally:
        sess.close()
    assert bus_journal.is_worker_owner_alive(ws_cfg, sess.worker_id, sess.pid) is False
