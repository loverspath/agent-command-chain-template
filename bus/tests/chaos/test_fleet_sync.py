"""Real-OS subprocess integration & chaos tests for `abus-fleet-sync` (`18900`–`18999` ports).

Covers all mandatory Fleet specification scenarios:
1. `rev` advance -> `.bak` backup, unknown custom `config.json` keys preserved, `urls` &
   `hub_peers` updated atomically, `fleet-applied.json` written, `fleet-ack` persisted on hub,
   clean `exit 75`, and stable idempotent re-entry on restart without restart loops.
2. `commit` fast-forward (`BUS_FLEET_REPO_DIR` + bare `origin` in `tmp_path`) -> `git fetch` +
   `git merge --ff-only` converges `HEAD == <new_sha>` and exits `75`; `--no-auto-update` skips
   commit step (`result == 'ok:no_auto_update'`, `commit_skipped == True`) while still applying
   config updates cleanly.
3. Graceful drain while busy (`inbox running == 1`) -> node detects new `fleet` `rev` during a
   held fake `claude` execution, never dies mid-task, delivers `status == 'ok'` RPC result to
   caller, and immediately afterwards converges and exits with `75`.
4. Safety guards -> dirty tracked file (`blocked:dirty_worktree`), diverged branch
   (`blocked:non_fast_forward`), unsatisfied `min_caps` (`blocked:missing_caps`), 3-attempt
   failure guard (`blocked:commit_not_on_origin` with `attempts == 3` and retry halt until higher
   `rev`), and corrupt/downgraded/empty-URL manifest rejection without `git reset` or `git stash`.
5. Replica hub (`auth_mode='replica'`) `fleet.json` replication (`18905` source -> `18915`
   replica) -> node connected ONLY to replica `18915` converges identically; direct `abus fleet set`
   on replica `cfg_dir` fails closed (`ReplicaReadOnlyError`); removing `fleet.json` on source
   disables `fleet` on replica.
6. Legacy `e7d0da2` client compatibility -> unmodified `e7d0da2` `run_node` + `peers` + `send`
   (sync RPC, async task, `--say` event) + `result` + `doctor` operate seamlessly against a v2
   hub serving `"fleet"` banners.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import pathlib
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

from agent_bus.config import FLEET_EXIT_CODE
from agent_bus.journal import NodeJournal

from .hub_proc import (
    BUS_ROOT,
    HubController,
    assert_ports_free,
    is_port_listening,
    validate_sandbox_port,
    write_claude_guard_script,
)
from .oracle import SideEffectCounter

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


def _create_origin_and_clones(
    root: pathlib.Path,
    clone_names: list[str],
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
    _git(publisher, git_env, "add", "README.md")
    _git(publisher, git_env, "commit", "-q", "-m", "v1 initial")
    _git(publisher, git_env, "push", "-q", "-u", "origin", "main")
    sha_v1 = _git(publisher, git_env, "rev-parse", "HEAD")

    clones: dict[str, pathlib.Path] = {}
    for name in clone_names:
        cpath = root / f"clone_{name}"
        subprocess.run(
            ["git", "clone", "-q", "-b", "main", str(origin), str(cpath)],
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        assert _git(cpath, git_env, "rev-parse", "HEAD") == sha_v1
        clones[name] = cpath

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
        "        fh.write(json.dumps({'seq': seq, 'id': req_id, 'ts': time.time(), 'pid': os.getpid()}) + '\\n')\n"
        "        fh.flush()\n"
        "        os.fsync(fh.fileno())\n"
        "        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)\n"
        f"sig_p = {sig_str}\n"
        f"rel_p = {rel_str}\n"
        "if sig_p:\n"
        "    sp = pathlib.Path(sig_p)\n"
        "    sp.parent.mkdir(parents=True, exist_ok=True)\n"
        "    sp.write_text(json.dumps({'id': req_id, 'pid': os.getpid(), 'ts': time.time()}), encoding='utf-8')\n"
        "if rel_p:\n"
        "    deadline = time.monotonic() + 10.0\n"
        "    while time.monotonic() < deadline:\n"
        "        if os.getppid() != orig_ppid:\n"
        "            sys.exit(137)\n"
        "        if pathlib.Path(rel_p).exists():\n"
        "            break\n"
        "        time.sleep(0.02)\n"
        f"sys.stdout.write('{response_prefix}:' + req_id + '\\n')\n"
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
        extra_env: dict[str, str] | None = None,
        counter_path: pathlib.Path | None = None,
        hold_signal_file: pathlib.Path | None = None,
        release_file: pathlib.Path | None = None,
        response_prefix: str = "FLEET_STUB_OK",
    ) -> None:
        self.node_name = node_name
        self.base_dir = pathlib.Path(base_dir).resolve()
        self.home_dir = self.base_dir / "home"
        self.cfg_dir = self.base_dir / "cfg"
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

        self.proc = subprocess.Popen(
            cmd,
            env=self.build_env(),
            cwd=str(self.home_dir),
            stdout=self._stdout_fh,
            stderr=self._stderr_fh,
            text=True,
            start_new_session=True,
        )
        return self.proc

    async def wait_exit(self, *, timeout: float = 8.0) -> int:
        assert self.proc is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc = self.proc.poll()
            if rc is not None:
                self._close_logs()
                return int(rc)
            await asyncio.sleep(0.02)
        out = self.read_stdout()
        err = self.read_stderr()
        raise AssertionError(
            f"Node {self.node_name} (pid={self.proc.pid}) did not exit within {timeout}s.\n"
            f"stdout:\n{out}\nstderr:\n{err}"
        )

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
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                with contextlib.suppress(Exception):
                    p.kill()
                with contextlib.suppress(Exception):
                    p.wait(timeout=3.0)
        for n in reversed(nodes):
            n.stop()


# ======================================================================
# 1. `rev` advance -> atomic `config.json` + `.bak` + custom keys + exit 75
# ======================================================================


@pytest.mark.chaos
async def test_fleet_01_rev_advance_atomic_config_update_exit75_and_idempotent_restart(
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
    node1 = ManagedNodeProc(
        node_name="node-1",
        base_dir=tmp_path / "node1",
        repo_dir=clones["node1"],
        extra_env={"BUS_FLEET_POLL_INTERVAL": "0.3"},
    )
    nodes.append(node1)
    orig_cfg_bytes = node1.write_config(initial_cfg).read_bytes()
    node1.start(label="main", mode="hybrid", peers="cat-food")

    # Wait until `node-1/main` is connected to `hub_1`.
    async def _node1_on_hub1() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        return "node-1/main" in (b.get("peers") or [])

    await _wait_until(_node1_on_hub1, timeout=5.0, desc="node-1/main connected to hub_1")

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

    # Banner immediately includes `fleet` without hub restart.
    b_on = await _ws_banner(hub_1.ws_url)
    assert b_on["fleet"]["rev"] == 1
    assert b_on["fleet"]["urls"] == [hub_1.ws_url, hub_2.ws_url]
    assert b_on["fleet"]["hub_peers"] == ["mother-goose", "z-fold8"]

    # `node-1` converges, flushes `fleet-ack`, and exits with code 75!
    rc1 = await node1.wait_exit(timeout=6.0)
    assert rc1 == FLEET_EXIT_CODE == 75

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
    assert applied_doc["commit"] == sha_v1
    assert applied_doc["target_commit"] == sha_v1
    assert applied_doc["result"] == "ok"
    assert applied_doc["config_changed"] is True
    assert applied_doc["code_changed"] is False
    assert applied_doc["attempts"] == 1

    # Verify `abus fleet status` (both `--json` over WS and table over HTTP Gateway) AND hub restart persistence!
    st_ws = await asyncio.to_thread(
        _run_cli, hub_1.build_env(), "fleet", "status", "--url", hub_1.ws_url, "--json"
    )
    assert st_ws.returncode == 0, st_ws.stderr
    st_doc = json.loads(st_ws.stdout)
    assert st_doc["ok"] is True
    assert st_doc["all_converged"] is True
    assert st_doc["nodes"]["node-1"]["rev"] == 1
    assert st_doc["nodes"]["node-1"]["result"] == "ok"
    assert st_doc["nodes"]["node-1"]["converged"] is True

    st_tbl = await asyncio.to_thread(
        _run_cli, hub_1.build_env(), "fleet", "status", "--url", hub_1.gateway_http_url
    )
    assert st_tbl.returncode == 0
    assert "Target Fleet: rev=1" in st_tbl.stdout
    assert "node-1" in st_tbl.stdout

    # Restart `hub_1` -> `fleet_status` persists in SQLite!
    hub_1.restart()
    _, h_after_restart = await asyncio.to_thread(_http_get_json, f"{hub_1.gateway_http_url}/v1/health")
    assert h_after_restart["fleet"]["rev"] == 1
    assert h_after_restart["fleet_nodes"]["node-1"]["rev"] == 1

    # Restart `node-1` (simulating supervisor restart after exit 75):
    # Must connect to BOTH `hub_1` and `hub_2` and stay running stably without exiting 75 again!
    node1.start(label="main", mode="hybrid", peers="cat-food")

    async def _node1_dual_connected() -> bool:
        b1 = await _ws_banner(hub_1.ws_url)
        b2 = await _ws_banner(hub_2.ws_url)
        return "node-1/main" in (b1.get("peers") or []) and "node-1/main" in (b2.get("peers") or [])

    await _wait_until(_node1_dual_connected, timeout=5.0, desc="restarted node-1 dual-connected")
    await asyncio.sleep(0.4)
    assert node1.proc is not None and node1.proc.poll() is None

    # Verify live RPC works on restarted `node-1` AND `hub_2` also received `node-1`'s `fleet-ack`!
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
    assert "FLEET_STUB_OK:s1-rpc-check" in rpc_res.stdout

    await _wait_until(
        lambda: _http_get_json(f"{hub_2.gateway_http_url}/v1/health")[1]
        .get("fleet_nodes", {})
        .get("node-1", {})
        .get("rev")
        == 1,
        timeout=4.0,
        desc="hub_2 received fleet-ack from restarted node-1",
    )

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

    node_auto.start(label="main", mode="hybrid", no_auto_update=False)
    node_manual.start(label="main", mode="hybrid", no_auto_update=True)

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

    # Both nodes exit 75 (`node-auto` for code+config, `node-manual` for config change only)
    rc_auto, rc_manual = await asyncio.gather(
        node_auto.wait_exit(timeout=8.0),
        node_manual.wait_exit(timeout=8.0),
    )
    assert rc_auto == 75
    assert rc_manual == 75

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

    # Restart `node-manual` with `--no-auto-update`, then push `sha_v3` with IDENTICAL `urls` (`rev=2`):
    # Since `config_changed == False` and `code_changed == False`, `node-manual` records `rev=2` WITHOUT exiting 75!
    node_manual.start(label="main", mode="hybrid", no_auto_update=True)
    await _wait_until(
        lambda: _ws_banner(hub_1.ws_url),
        timeout=4.0,
        desc="hub_1 responsive",
    )
    await _wait_until(
        lambda: (
            "node-manual/main"
            in (_http_get_json(f"{hub_1.gateway_http_url}/v1/health") and [])  # dummy fallback
        )
        or True,
        timeout=1.0,
    )

    async def _man_reconnected() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        return "node-manual/main" in (b.get("peers") or [])

    await _wait_until(_man_reconnected, timeout=5.0, desc="node-manual reconnected")

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
        lambda: (node_manual.cfg_dir / "fleet-applied.json").is_file()
        and json.loads((node_manual.cfg_dir / "fleet-applied.json").read_text(encoding="utf-8")).get("rev") == 2,
        timeout=5.0,
        desc="node-manual recorded rev=2 without restart",
    )
    await asyncio.sleep(0.3)
    assert node_manual.proc is not None and node_manual.proc.poll() is None
    assert _git(clones["manual"], git_env, "rev-parse", "HEAD") == sha_v1

    node_manual.stop()
    node_auto.stop()
    hub_2.stop()
    hub_1.stop()
    assert_ports_free(ports, context="fleet_02_post")


# ======================================================================
# 3. Graceful Drain while Worker Busy (`inbox running == 1`) before Exit `75`
# ======================================================================


@pytest.mark.chaos
async def test_fleet_03_graceful_drain_while_worker_busy_before_exit75(
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
    node_busy.start(label="main", mode="hybrid", peers="cat-food")

    async def _busy_connected() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        return "node-busy/main" in (b.get("peers") or [])

    await _wait_until(_busy_connected, timeout=5.0, desc="node-busy connected")

    # Launch synchronous RPC caller (`abus send --wait 15`) in background OS subprocess
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

    # Wait until fake `claude` is actively executing AND `journal.db` `inbox` shows `running == 1`
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

    # Wait until `node-busy` explicitly logs detecting `rev=1` while busy!
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

    # Now release the slow `claude` job!
    release_sig.write_text("go\n", encoding="utf-8")

    # 1) RPC caller receives normal terminal `status='ok'` response!
    caller_out, caller_err = await asyncio.to_thread(caller_proc.communicate, timeout=8.0)
    assert caller_proc.returncode == 0, f"caller failed: {caller_err}\n{caller_out}"
    assert "DRAIN_RPC_OK:drain-job-01" in caller_out

    # 2) Immediately after becoming idle & flushing reply, `node-busy` converges and exits with code 75!
    rc_busy = await node_busy.wait_exit(timeout=6.0)
    assert rc_busy == 75
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

    # (4a) Malformed `fleet.json` (`corrupt json`, `rev <= 0`, `urls == []`, `http://` URL) on Hub ->
    # logged as warning and ignored (never served in banner or `/v1/health`)!
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

    # CLI `abus fleet set` also rejects empty/HTTP URLs cleanly
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

    # Prepare local dirty tracked modification on `clones['dirty']` and diverged commit on `clones['diverged']`
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

    async def _all_three_connected() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        ps = set(b.get("peers") or [])
        return {"node-dirty/main", "node-diverged/main", "node-strike/main"} <= ps

    await _wait_until(_all_three_connected, timeout=6.0, desc="all 3 guard nodes connected")

    # (4b & 4c) Push `sha_v2` to `origin` and set `rev=1` ->
    # `node-strike` converges & exits 75; `node-dirty` -> `blocked:dirty_worktree`;
    # `node-diverged` -> `blocked:non_fast_forward` (both stay running with 0 local data loss!)
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

    # `node-strike` (clean) converges to `sha_v2` and exits 75
    assert (await node_strike.wait_exit(timeout=6.0)) == 75
    assert _git(clones["strike"], git_env, "rev-parse", "HEAD") == sha_v2

    # Wait until `hub_1` receives `blocked` fleet-acks from `node-dirty` and `node-diverged`
    async def _dirty_and_diverged_blocked() -> bool:
        _, h = await asyncio.to_thread(_http_get_json, f"{hub_1.gateway_http_url}/v1/health")
        fn = h.get("fleet_nodes") or {}
        return (
            fn.get("node-dirty", {}).get("result") == "blocked:dirty_worktree"
            and fn.get("node-diverged", {}).get("result") == "blocked:non_fast_forward"
        )

    await _wait_until(_dirty_and_diverged_blocked, timeout=6.0, desc="dirty and diverged blocked reports")

    # Verify zero `git reset`/`git stash`, 100% preserved local state, and zero restart loop!
    assert dirty_readme.read_text(encoding="utf-8") == " PRECIOUS LOCAL UNCOMMITTED EDIT \n"
    assert _git(clones["dirty"], git_env, "rev-parse", "HEAD") == sha_v1
    assert _git(clones["dirty"], git_env, "stash", "list") == ""
    assert _git(clones["diverged"], git_env, "rev-parse", "HEAD") == sha_local_diverged
    assert (clones["diverged"] / "local_only.txt").read_text(encoding="utf-8") == "diverged branch work\n"
    assert _git(clones["diverged"], git_env, "stash", "list") == ""
    assert node_dirty.proc is not None and node_dirty.proc.poll() is None
    assert node_diverged.proc is not None and node_diverged.proc.poll() is None

    # Restart `node-strike` so we can test (4d) `min_caps` and (4e) 3-attempt strike guard!
    node_strike.start(label="main", mode="hybrid")
    await _wait_until(
        lambda: _ws_banner(hub_1.ws_url),
        timeout=3.0,
    )

    async def _strike_reconnected() -> bool:
        b = await _ws_banner(hub_1.ws_url)
        return "node-strike/main" in (b.get("peers") or [])

    await _wait_until(_strike_reconnected, timeout=5.0, desc="node-strike reconnected")

    # Verify CLI `--rev` regression is rejected!
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
    assert node_strike.proc is not None and node_strike.proc.poll() is None

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

    # Wait 0.45s (> 4 retry intervals): node must NOT retry rev=3 further or exit!
    await asyncio.sleep(0.45)
    assert node_strike.proc is not None and node_strike.proc.poll() is None
    assert node_strike.read_stderr().count("rev=3 transient failure") == 2  # attempts 1 & 2 retried; 3rd blocked!

    # Also test node-side ignore on `rev` downgrade (`rev < local_rev`) via in-process convergence check
    from agent_bus.fleet import converge_fleet_once

    downgrade_out = converge_fleet_once(
        node_strike.cfg_dir,
        {"rev": 2, "commit": sha_v2, "urls": [hub_1.ws_url]},
    )
    assert downgrade_out.action == "ignored"
    empty_urls_out = converge_fleet_once(
        node_strike.cfg_dir,
        {"rev": 99, "commit": sha_v2, "urls": []},
    )
    assert empty_urls_out.action == "ignored"

    # Now publish valid `sha_v3` at `rev=4` -> `node-strike` unblocks, fast-forwards to `sha_v3`, and exits 75!
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
    assert (await node_strike.wait_exit(timeout=6.0)) == 75
    assert _git(clones["strike"], git_env, "rev-parse", "HEAD") == sha_v3

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
    # Mandatory ports per spec: Source Hub A = 18905, Replica Hub B = 18915!
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

    # Wait for initial auth_sync (gen 1, fleet=None)
    await _wait_until(
        lambda: _http_get_json(f"{hub_b.gateway_http_url}/v1/health")[1].get("auth_generation", 0) >= 1,
        timeout=5.0,
        desc="Hub B initial auth_sync",
    )

    # Single-writer guard: direct `abus fleet set` on Replica Hub B MUST fail closed!
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

    # Start `z-fold8` connected ONLY to Replica Hub B (`18915`)!
    # Note: Hub B runs with `BUS_TEST_ALLOW_LOCALHOST=0` + `BUS_WHOIS_CMD`, where `127.0.0.1` -> `cat-food`
    # and `127.0.0.3` -> `z-fold8`; binding loopback or running as `cat-food` (`127.0.0.1`) works seamlessly.
    node_rep = ManagedNodeProc(
        node_name="cat-food",
        base_dir=tmp_path / "node_on_replica",
        repo_dir=clones["zfold"],
        extra_env={"BUS_FLEET_POLL_INTERVAL": "0.25"},
    )
    nodes.append(node_rep)
    node_rep.write_config({"urls": [hub_b.ws_url]})
    node_rep.start(label="main", mode="hybrid")

    await _wait_until(
        lambda: _ws_banner(hub_b.ws_url, local_ip="127.0.0.3"),
        timeout=4.0,
    )

    async def _node_on_hub_b() -> bool:
        b = await _ws_banner(hub_b.ws_url, local_ip="127.0.0.3")
        return "cat-food/main" in (b.get("peers") or [])

    await _wait_until(_node_on_hub_b, timeout=5.0, desc="node connected to replica Hub B")

    # Push `sha_v2` and run `abus fleet set` on Source Hub A (`18905`)!
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

    # Replica Hub B replicates `fleet.json` into `auth-gen/<gen>/fleet.json` and pushes/serves it ->
    # `node_rep` (connected ONLY to Hub B `18915`) converges `HEAD == sha_v2` + `config.json` and exits 75!
    rc_rep = await node_rep.wait_exit(timeout=8.0)
    assert rc_rep == 75

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

    # Verify removing `fleet.json` on Source Hub A advances generation and disables `fleet` on Replica Hub B!
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

    # Activate `fleet.json` on new v2 Hub and confirm `"fleet"` is present in banner!
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

    # Start unmodified `e7d0da2` `run_node` daemon (`cat-food/main` via `BUS_TEST_LOCAL_NODE=cat-food`)
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

    legacy_caller_dir = tmp_path / "legacy_caller"
    legacy_caller = ManagedNodeProc(
        node_name="cat-food",
        base_dir=legacy_caller_dir,
        bus_root=legacy_bus_root,
    )
    caller_env = legacy_caller.build_env()

    # 1) Legacy `peers` CLI sees `cat-food/main`
    async def _legacy_peers_ready() -> bool:
        r = await asyncio.to_thread(
            _run_cli, caller_env, "peers", "--url", hub_v2.ws_url
        )
        return r.returncode == 0 and "cat-food/main" in r.stdout

    await _wait_until(_legacy_peers_ready, timeout=6.0, desc="legacy e7d0da2 worker registered")

    # 2) Legacy sync RPC (`send --wait`) succeeds despite `"fleet"` in banner!
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

    # 3) Legacy async `send` + `result` retrieval succeeds!
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

    # 4) Legacy `--say` event to inbox + legacy `doctor` succeed!
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

    # Ensure legacy worker stayed alive without crashing on `"fleet"` banner field
    assert legacy_worker.proc is not None and legacy_worker.proc.poll() is None

    legacy_worker.stop()
    hub_v2.stop()
    assert_ports_free(ports, context="fleet_06_post")
