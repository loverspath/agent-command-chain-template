"""Real-Git integration tests for docs/tailhub-termux/bus/bus-sync.py (WBS I6 / F24 / R13).

All git operations, HOME/gitconfig, source logs, checkpoints, and bare remotes
are 100% confined to pytest's tmp_path. No operational paths, ports, or sessions
are ever accessed.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import pathlib
import signal
import stat
import subprocess
import sys
import time
from typing import Any

import pytest

WORKTREE_ROOT = pathlib.Path(__file__).resolve().parents[2]
BUS_SYNC_SCRIPT = WORKTREE_ROOT / "docs" / "tailhub-termux" / "bus" / "bus-sync.py"
FIXED_TS = 1794816000.0  # Deterministic timestamp for stable YYYY-MM-DD.md checks


def _isolated_git_env(base_dir: pathlib.Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    home_dir = base_dir / "isolated_home"
    xdg_dir = home_dir / ".config"
    home_dir.mkdir(parents=True, exist_ok=True)
    xdg_dir.mkdir(parents=True, exist_ok=True)
    global_gitconfig = home_dir / ".gitconfig"
    if not global_gitconfig.exists():
        global_gitconfig.write_text(
            "[user]\n\tname = BusSync Test\n\temail = bus-sync-test@example.invalid\n"
            "[init]\n\tdefaultBranch = main\n",
            encoding="utf-8",
        )

    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
        "XDG_CONFIG_HOME": str(xdg_dir),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(global_gitconfig),
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if extra:
        env.update(extra)
    return env


def _git(cwd: pathlib.Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert res.returncode == 0, f"git {args} failed ({res.returncode}): {res.stdout}\n{res.stderr}"
    return res


def _init_remote_and_clone(
    tmp_path: pathlib.Path,
    env: dict[str, str],
    clone_name: str = "agent-docs",
) -> tuple[pathlib.Path, pathlib.Path]:
    bare = tmp_path / "remote-agent-docs.git"
    if not bare.exists():
        bare.mkdir(parents=True)
        _git(bare, env, "init", "--bare")
        _git(bare, env, "symbolic-ref", "HEAD", "refs/heads/main")

        seed = tmp_path / "_seed_clone"
        subprocess.run(
            ["git", "clone", "-q", str(bare), str(seed)],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        _git(seed, env, "checkout", "-B", "main")
        (seed / "README.md").write_text("# Sandbox agent-docs\n", encoding="utf-8")
        _git(seed, env, "add", "README.md")
        _git(seed, env, "commit", "-q", "-m", "initial commit")
        _git(seed, env, "push", "-q", "-u", "origin", "main")

    clone = tmp_path / clone_name
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(bare), str(clone)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return bare, clone


def _append_msg(
    src: pathlib.Path,
    *,
    seq: int,
    mid: str | None,
    text: str,
    sender: str = "cat-food/main",
    target: str = "mother-goose/main",
    hop: int = 0,
    ts: float = FIXED_TS,
    op: str = "msg",
) -> None:
    src.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "v": 2,
        "op": op,
        "seq": seq,
        "from": sender,
        "to": target,
        "hop": hop,
        "ts": ts,
        "text": text,
    }
    if mid is not None:
        record["id"] = mid
    with open(src, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _run_sync_once(
    env: dict[str, str],
    *,
    src: pathlib.Path,
    repo: pathlib.Path,
    state: pathlib.Path,
    subdir: str | None = None,
    git_timeout: str | None = None,
    timeout_proc: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    run_env = env.copy()
    run_env.update(
        {
            "BUS_SYNC_SRC": str(src),
            "BUS_SYNC_REPO": str(repo),
            "BUS_SYNC_STATE": str(state),
            "BUS_SYNC_ONCE": "1",
        }
    )
    if subdir is not None:
        run_env["BUS_SYNC_SUBDIR"] = subdir
    if git_timeout is not None:
        run_env["BUS_SYNC_GIT_TIMEOUT"] = git_timeout
    return subprocess.run(
        [sys.executable, str(BUS_SYNC_SCRIPT)],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=timeout_proc,
    )


def _remote_files_on_main(bare: pathlib.Path, env: dict[str, str]) -> list[str]:
    res = _git(bare, env, "ls-tree", "-r", "--name-only", "refs/heads/main")
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]


def _remote_read_file(bare: pathlib.Path, env: dict[str, str], rel_path: str) -> str:
    res = _git(bare, env, "show", f"refs/heads/main:{rel_path}")
    return res.stdout


def _write_executable_hook(hook_path: pathlib.Path, body: str) -> None:
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(body, encoding="utf-8")
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_a_normal_cycle_pushes_markdown_with_markers_and_advances_state(
    tmp_path: pathlib.Path,
) -> None:
    """(a) Normal BUS_SYNC_ONCE=1 cycle mirrors messages with seq+id markers to remote and updates STATE."""
    env = _isolated_git_env(tmp_path)
    bare, repo = _init_remote_and_clone(tmp_path, env)
    src = tmp_path / "logs" / "messages.jsonl"
    state = tmp_path / "cfg" / ".synced_seq"

    _append_msg(src, seq=1, mid="msg-alpha-001", text="first normal payload")
    # Duplicate inside the exact same batch + non-msg op + malformed JSON line
    _append_msg(src, seq=1, mid="msg-alpha-001", text="first normal payload")
    with open(src, "a", encoding="utf-8") as fh:
        fh.write("{corrupt-json-line\n")
    _append_msg(src, seq=99, mid="sys-ignored", text="ignored sys frame", op="sys")
    _append_msg(src, seq=2, mid="msg-beta-002", text="second normal payload", hop=1, ts=FIXED_TS + 5)

    res = _run_sync_once(env, src=src, repo=repo, state=state)
    assert res.returncode == 0, f"stderr={res.stderr}"
    assert state.exists()
    assert state.read_text(encoding="utf-8").strip() == "2"

    remote_files = _remote_files_on_main(bare, env)
    md_files = [p for p in remote_files if p.startswith("bus-log/") and p.endswith(".md")]
    assert len(md_files) == 1

    remote_md = _remote_read_file(bare, env, md_files[0])
    assert remote_md.startswith("# Bus log ")
    assert "`cat-food/main` -> `mother-goose/main` (seq 1, hop 0) <!-- seq:1 id:msg-alpha-001 -->" in remote_md
    assert "`cat-food/main` -> `mother-goose/main` (seq 2, hop 1) <!-- seq:2 id:msg-beta-002 -->" in remote_md
    assert remote_md.count("first normal payload") == 1
    assert remote_md.count("second normal payload") == 1
    assert "ignored sys frame" not in remote_md

    # Second cycle with no new messages must be a clean no-op
    rev_before = _git(bare, env, "rev-parse", "refs/heads/main").stdout.strip()
    res2 = _run_sync_once(env, src=src, repo=repo, state=state)
    assert res2.returncode == 0
    rev_after = _git(bare, env, "rev-parse", "refs/heads/main").stdout.strip()
    assert rev_after == rev_before
    assert state.read_text(encoding="utf-8").strip() == "2"


def test_b_commit_failure_keeps_state_and_recovers_without_loss_or_dup(
    tmp_path: pathlib.Path,
) -> None:
    """(b) R13 first half: commit failure (pre-commit hook & empty user.email) never advances STATE;
    subsequent recovery commits pending changes with 0 lost and 0 duplicated messages."""
    env = _isolated_git_env(tmp_path)
    bare, repo = _init_remote_and_clone(tmp_path, env)
    src = tmp_path / "logs" / "messages.jsonl"
    state = tmp_path / "cfg" / ".synced_seq"

    # Sub-case b1: pre-commit hook rejects commit
    pre_commit = repo / ".git" / "hooks" / "pre-commit"
    _write_executable_hook(pre_commit, "#!/bin/sh\necho 'simulated pre-commit failure' >&2\nexit 1\n")

    _append_msg(src, seq=1, mid="r13-commit-1", text="commit-fail-sentinel-one")
    _append_msg(src, seq=2, mid="r13-commit-2", text="commit-fail-sentinel-two")

    # Run two failing cycles to prove neither advances STATE nor duplicates markdown entries
    r1 = _run_sync_once(env, src=src, repo=repo, state=state)
    assert r1.returncode == 0
    assert "git commit failed" in r1.stderr
    assert not state.exists()

    r2 = _run_sync_once(env, src=src, repo=repo, state=state)
    assert r2.returncode == 0
    assert not state.exists()
    assert [p for p in _remote_files_on_main(bare, env) if p.startswith("bus-log/")] == []

    # Remove failing hook -> next cycle must commit dirty tree, rebase, push, and advance STATE
    pre_commit.unlink()
    r3 = _run_sync_once(env, src=src, repo=repo, state=state)
    assert r3.returncode == 0, f"stderr={r3.stderr}"
    assert state.read_text(encoding="utf-8").strip() == "2"

    md_rel = next(p for p in _remote_files_on_main(bare, env) if p.startswith("bus-log/"))
    remote_md = _remote_read_file(bare, env, md_rel)
    assert remote_md.count("commit-fail-sentinel-one") == 1
    assert remote_md.count("commit-fail-sentinel-two") == 1
    assert remote_md.count("<!-- seq:1 id:r13-commit-1 -->") == 1
    assert remote_md.count("<!-- seq:2 id:r13-commit-2 -->") == 1

    # Sub-case b2: empty user.email / missing git author identity
    no_ident_cfg = tmp_path / "empty.gitconfig"
    no_ident_cfg.write_text("", encoding="utf-8")
    broken_ident_env = env.copy()
    broken_ident_env["GIT_CONFIG_GLOBAL"] = str(no_ident_cfg)
    broken_ident_env["EMAIL"] = ""
    broken_ident_env["GIT_AUTHOR_EMAIL"] = ""
    broken_ident_env["GIT_COMMITTER_EMAIL"] = ""

    _append_msg(src, seq=3, mid="r13-commit-3", text="commit-fail-empty-email-three")
    r4 = _run_sync_once(broken_ident_env, src=src, repo=repo, state=state)
    assert r4.returncode == 0
    assert "git commit failed" in r4.stderr
    # STATE must remain at previous checkpoint "2"
    assert state.read_text(encoding="utf-8").strip() == "2"

    # Restore valid identity -> commits & pushes seq 3 without duplicating 1..3
    r5 = _run_sync_once(env, src=src, repo=repo, state=state)
    assert r5.returncode == 0, f"stderr={r5.stderr}"
    assert state.read_text(encoding="utf-8").strip() == "3"

    remote_md_after = _remote_read_file(bare, env, md_rel)
    assert remote_md_after.count("commit-fail-sentinel-one") == 1
    assert remote_md_after.count("commit-fail-sentinel-two") == 1
    assert remote_md_after.count("commit-fail-empty-email-three") == 1


def test_c_push_failure_keeps_state_and_retries_unpushed_commit_without_dup(
    tmp_path: pathlib.Path,
) -> None:
    """(c) R13 second half: remote pre-receive rejection keeps STATE unchanged after local commit;
    retry after hook removal pushes unpushed local commit ('nothing to commit' allowed) with 0 duplicates."""
    env = _isolated_git_env(tmp_path)
    bare, repo = _init_remote_and_clone(tmp_path, env)
    src = tmp_path / "logs" / "messages.jsonl"
    state = tmp_path / "cfg" / ".synced_seq"

    pre_receive = bare / "hooks" / "pre-receive"
    _write_executable_hook(pre_receive, "#!/bin/sh\necho 'remote pre-receive reject' >&2\nexit 1\n")

    _append_msg(src, seq=1, mid="r13-push-1", text="push-fail-sentinel-one")
    _append_msg(src, seq=2, mid="r13-push-2", text="push-fail-sentinel-two")

    # Cycle 1: local commit succeeds, remote push fails -> STATE must NOT advance
    r1 = _run_sync_once(env, src=src, repo=repo, state=state)
    assert r1.returncode == 0
    assert "git push failed" in r1.stderr
    assert not state.exists()
    assert [p for p in _remote_files_on_main(bare, env) if p.startswith("bus-log/")] == []

    # Cycle 2 (still blocked): markers prevent duplicate append, push fails again -> STATE still absent
    r2 = _run_sync_once(env, src=src, repo=repo, state=state)
    assert r2.returncode == 0
    assert "git push failed" in r2.stderr
    assert not state.exists()

    # Remove pre-receive hook -> Cycle 3 hits 'nothing to commit', runs pull --rebase + push, advances STATE
    pre_receive.unlink()
    r3 = _run_sync_once(env, src=src, repo=repo, state=state)
    assert r3.returncode == 0, f"stderr={r3.stderr}"
    assert state.read_text(encoding="utf-8").strip() == "2"

    md_rel = next(p for p in _remote_files_on_main(bare, env) if p.startswith("bus-log/"))
    remote_md = _remote_read_file(bare, env, md_rel)
    assert remote_md.count("push-fail-sentinel-one") == 1
    assert remote_md.count("push-fail-sentinel-two") == 1

    # Mixed sub-case: seq 3 committed while push fails, then seq 4 arrives before push recovers
    _write_executable_hook(pre_receive, "#!/bin/sh\nexit 1\n")
    _append_msg(src, seq=3, mid="r13-push-3", text="push-fail-sentinel-three")
    r4 = _run_sync_once(env, src=src, repo=repo, state=state)
    assert r4.returncode == 0
    assert state.read_text(encoding="utf-8").strip() == "2"

    _append_msg(src, seq=4, mid="r13-push-4", text="push-fail-sentinel-four")
    pre_receive.unlink()
    r5 = _run_sync_once(env, src=src, repo=repo, state=state)
    assert r5.returncode == 0, f"stderr={r5.stderr}"
    assert state.read_text(encoding="utf-8").strip() == "4"

    remote_md_final = _remote_read_file(bare, env, md_rel)
    for expected in (
        "push-fail-sentinel-one",
        "push-fail-sentinel-two",
        "push-fail-sentinel-three",
        "push-fail-sentinel-four",
    ):
        assert remote_md_final.count(expected) == 1


def test_d_concurrent_two_processes_flock_skips_cleanly_without_conflict_or_dup(
    tmp_path: pathlib.Path,
) -> None:
    """(d) Concurrent writer lock (<repo>/.git/bus-sync.lock) causes contender to skip cleanly with 0 conflict/dup."""
    env = _isolated_git_env(tmp_path)
    bare, repo = _init_remote_and_clone(tmp_path, env)
    src = tmp_path / "logs" / "messages.jsonl"
    state = tmp_path / "cfg" / ".synced_seq"

    _append_msg(src, seq=1, mid="lock-msg-1", text="concurrent-lock-payload-1")

    # Part 1: Direct flock held on <repo>/.git/bus-sync.lock
    lock_file = repo / ".git" / "bus-sync.lock"
    with open(lock_file, "a+", encoding="utf-8") as held_fh:
        fcntl.flock(held_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            skipped = _run_sync_once(env, src=src, repo=repo, state=state)
            assert skipped.returncode == 0
            assert skipped.stderr == ""
            assert not state.exists()
            assert [p for p in _remote_files_on_main(bare, env) if p.startswith("bus-log/")] == []
        finally:
            fcntl.flock(held_fh.fileno(), fcntl.LOCK_UN)

    # Part 2: Two simultaneous bus-sync.py subprocesses racing while Process 1 is in slow pre-push
    hook_entered = tmp_path / "pre_push_entered"
    hook_count_file = tmp_path / "pre_push_runs.log"
    _write_executable_hook(
        repo / ".git" / "hooks" / "pre-push",
        f"#!/bin/sh\necho $$ >> '{hook_count_file}'\ntouch '{hook_entered}'\nsleep 0.7\nexit 0\n",
    )

    run_env = env.copy()
    run_env.update(
        {
            "BUS_SYNC_SRC": str(src),
            "BUS_SYNC_REPO": str(repo),
            "BUS_SYNC_STATE": str(state),
            "BUS_SYNC_ONCE": "1",
        }
    )
    p1 = subprocess.Popen(
        [sys.executable, str(BUS_SYNC_SCRIPT)],
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not hook_entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert hook_entered.exists(), "Process 1 did not enter pre-push hook in time"

        # Launch Process 2 while Process 1 holds .git/bus-sync.lock inside pre-push
        p2_res = _run_sync_once(env, src=src, repo=repo, state=state)
        assert p2_res.returncode == 0
        assert p2_res.stderr == ""

        out1, err1 = p1.communicate(timeout=10)
        assert p1.returncode == 0, f"p1 failed: {out1}\n{err1}"
    finally:
        if p1.poll() is None:
            p1.kill()
            p1.communicate()

    assert state.read_text(encoding="utf-8").strip() == "1"
    hook_runs = [ln for ln in hook_count_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(hook_runs) == 1

    md_rel = next(p for p in _remote_files_on_main(bare, env) if p.startswith("bus-log/"))
    remote_md = _remote_read_file(bare, env, md_rel)
    assert remote_md.count("concurrent-lock-payload-1") == 1


def test_e_bus_sync_subdir_multi_hub_isolated_sync_into_shared_remote(
    tmp_path: pathlib.Path,
) -> None:
    """(e) BUS_SYNC_SUBDIR separates multi-hub mirrors ('bus-log' vs 'bus-log/mother-goose') in one remote."""
    env = _isolated_git_env(tmp_path)
    bare, clone_a = _init_remote_and_clone(tmp_path, env, clone_name="clone-cat-food")
    _, clone_b = _init_remote_and_clone(tmp_path, env, clone_name="clone-mother-goose")

    src_a = tmp_path / "hub-a" / "messages.jsonl"
    state_a = tmp_path / "hub-a" / ".synced_seq"
    src_b = tmp_path / "hub-b" / "messages.jsonl"
    state_b = tmp_path / "hub-b" / ".synced_seq"

    # Both hubs independently start at seq=1,2 on the same date
    _append_msg(src_a, seq=1, mid="hubA-id-1", text="cat-food log entry 1", sender="cat-food/main")
    _append_msg(src_a, seq=2, mid="hubA-id-2", text="cat-food log entry 2", sender="cat-food/main")

    _append_msg(src_b, seq=1, mid="hubB-id-1", text="mother-goose log entry 1", sender="mother-goose/main")
    _append_msg(src_b, seq=2, mid="hubB-id-2", text="mother-goose log entry 2", sender="mother-goose/main")

    # Hub A pushes default 'bus-log' first; Hub B (unpulled clone) pushes 'bus-log/mother-goose' and auto-rebases
    ra1 = _run_sync_once(env, src=src_a, repo=clone_a, state=state_a)
    assert ra1.returncode == 0, f"stderr={ra1.stderr}"
    rb1 = _run_sync_once(
        env,
        src=src_b,
        repo=clone_b,
        state=state_b,
        subdir="bus-log/mother-goose",
    )
    assert rb1.returncode == 0, f"stderr={rb1.stderr}"

    # Hub A now behind origin/main appends seq=3 and syncs cleanly via pull --rebase
    _append_msg(src_a, seq=3, mid="hubA-id-3", text="cat-food log entry 3", sender="cat-food/main")
    ra2 = _run_sync_once(env, src=src_a, repo=clone_a, state=state_a, subdir="bus-log")
    assert ra2.returncode == 0, f"stderr={ra2.stderr}"

    assert state_a.read_text(encoding="utf-8").strip() == "3"
    assert state_b.read_text(encoding="utf-8").strip() == "2"

    remote_files = _remote_files_on_main(bare, env)
    root_logs = [p for p in remote_files if p.count("/") == 1 and p.startswith("bus-log/")]
    mg_logs = [p for p in remote_files if p.startswith("bus-log/mother-goose/")]
    assert len(root_logs) == 1
    assert len(mg_logs) == 1

    md_a = _remote_read_file(bare, env, root_logs[0])
    md_b = _remote_read_file(bare, env, mg_logs[0])
    assert md_a.count("cat-food log entry 1") == 1
    assert md_a.count("cat-food log entry 2") == 1
    assert md_a.count("cat-food log entry 3") == 1
    assert "mother-goose log entry" not in md_a

    assert md_b.count("mother-goose log entry 1") == 1
    assert md_b.count("mother-goose log entry 2") == 1
    assert "cat-food log entry" not in md_b


def test_f_pull_rebase_conflict_auto_aborts_rebase_and_preserves_state(
    tmp_path: pathlib.Path,
) -> None:
    """(f) Conflicting remote commit causing pull --rebase failure triggers git rebase --abort and keeps STATE."""
    env = _isolated_git_env(tmp_path)
    bare, repo_primary = _init_remote_and_clone(tmp_path, env, clone_name="primary-clone")
    _, repo_other = _init_remote_and_clone(tmp_path, env, clone_name="conflicting-clone")

    src = tmp_path / "logs" / "messages.jsonl"
    state = tmp_path / "cfg" / ".synced_seq"

    # Determine the exact date filename that FIXED_TS produces
    import datetime

    date_str = datetime.datetime.fromtimestamp(FIXED_TS).strftime("%Y-%m-%d")
    rel_md = f"bus-log/{date_str}.md"

    # Other clone pushes a conflicting file at the exact same path on origin/main
    conflict_md = repo_other / rel_md
    conflict_md.parent.mkdir(parents=True, exist_ok=True)
    conflict_md.write_text("# Conflicting header line from external writer\n\ndivergent text\n", encoding="utf-8")
    _git(repo_other, env, "add", rel_md)
    _git(repo_other, env, "commit", "-q", "-m", "conflicting upstream edit")
    _git(repo_other, env, "push", "-q", "origin", "main")

    # Primary clone (still on initial commit) tries to sync a message into the same date file
    _append_msg(src, seq=1, mid="conflict-msg-1", text="primary local payload")
    res = _run_sync_once(env, src=src, repo=repo_primary, state=state)
    assert res.returncode == 0
    assert "git pull --rebase failed" in res.stderr
    assert not state.exists()

    # Verify rebase was automatically aborted and working tree is completely clean
    assert not (repo_primary / ".git" / "rebase-merge").exists()
    assert not (repo_primary / ".git" / "rebase-apply").exists()
    porcelain = _git(repo_primary, env, "status", "--porcelain").stdout.strip()
    assert porcelain == ""
    local_md = (repo_primary / rel_md).read_text(encoding="utf-8")
    assert "<<<<<<<" not in local_md
    assert local_md.count("primary local payload") == 1


def test_g_git_subprocess_timeout_handled_safely_with_zero_orphan_processes(
    tmp_path: pathlib.Path,
) -> None:
    """(g) BUS_SYNC_GIT_TIMEOUT terminates hanging git/hook process group, preserves STATE, and recovers cleanly."""
    env = _isolated_git_env(tmp_path)
    bare, repo = _init_remote_and_clone(tmp_path, env)
    src = tmp_path / "logs" / "messages.jsonl"
    state = tmp_path / "cfg" / ".synced_seq"

    pid_file = tmp_path / "hanging_hook.pid"
    pre_push = repo / ".git" / "hooks" / "pre-push"
    _write_executable_hook(
        pre_push,
        f"#!/bin/sh\necho $$ > '{pid_file}'\nsleep 30\n",
    )

    _append_msg(src, seq=1, mid="timeout-msg-1", text="timeout-recovery-payload")

    t0 = time.monotonic()
    res = _run_sync_once(env, src=src, repo=repo, state=state, git_timeout="0.4", timeout_proc=10.0)
    elapsed = time.monotonic() - t0

    assert res.returncode == 0
    assert elapsed < 5.0
    assert "timed out" in res.stderr
    assert not state.exists()
    assert not (repo / ".git" / "index.lock").exists()

    # Hook child process must have been killed with the git process group (zero orphan processes)
    assert pid_file.exists()
    hook_pid = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            os.kill(hook_pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            break
    with pytest.raises(ProcessLookupError):
        os.kill(hook_pid, 0)

    # Remove slow hook -> next cycle recovers, pushes unpushed commit without duplicate, advances STATE
    pre_push.unlink()
    res_ok = _run_sync_once(env, src=src, repo=repo, state=state, git_timeout="10")
    assert res_ok.returncode == 0, f"stderr={res_ok.stderr}"
    assert state.read_text(encoding="utf-8").strip() == "1"

    md_rel = next(p for p in _remote_files_on_main(bare, env) if p.startswith("bus-log/"))
    remote_md = _remote_read_file(bare, env, md_rel)
    assert remote_md.count("timeout-recovery-payload") == 1


def test_h_default_env_paths_interval_loop_and_invalid_subdir_guard(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(h) Unset BUS_SYNC_* preserves exact legacy defaults; daemon loop & traversal guard work cleanly."""
    env = _isolated_git_env(tmp_path)
    fake_home = pathlib.Path(env["HOME"])

    for key in (
        "BUS_SYNC_SRC",
        "BUS_SYNC_REPO",
        "BUS_SYNC_STATE",
        "BUS_SYNC_SUBDIR",
        "BUS_SYNC_INTERVAL",
        "BUS_SYNC_GIT_TIMEOUT",
        "BUS_SYNC_ONCE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(fake_home))

    spec = importlib.util.spec_from_file_location("bus_sync_under_test", BUS_SYNC_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.SRC == fake_home / "logs/agent-bus/messages.jsonl"
    assert mod.REPO == fake_home / "agent-docs"
    assert mod.STATE == fake_home / ".config/agent-bus/.synced_seq"
    assert mod.SUBDIR == "bus-log"
    assert mod.INTERVAL == 30.0
    assert mod.GIT_TIMEOUT == 60.0

    # Path traversal in BUS_SYNC_SUBDIR must be rejected before touching outside repo
    bare, repo = _init_remote_and_clone(tmp_path, env, clone_name="guard-clone")
    src = tmp_path / "guard" / "messages.jsonl"
    state = tmp_path / "guard" / ".synced_seq"
    _append_msg(src, seq=1, mid="guard-1", text="traversal test")

    bad = _run_sync_once(env, src=src, repo=repo, state=state, subdir="../outside")
    assert bad.returncode != 0
    assert not (tmp_path / "outside").exists()
    assert not state.exists()

    # Continuous daemon loop mode (BUS_SYNC_ONCE unset, BUS_SYNC_INTERVAL=0.15)
    loop_env = env.copy()
    loop_env.update(
        {
            "BUS_SYNC_SRC": str(src),
            "BUS_SYNC_REPO": str(repo),
            "BUS_SYNC_STATE": str(state),
            "BUS_SYNC_INTERVAL": "0.15",
        }
    )
    daemon = subprocess.Popen(
        [sys.executable, str(BUS_SYNC_SCRIPT)],
        env=loop_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if state.exists() and state.read_text(encoding="utf-8").strip() == "1":
                break
            time.sleep(0.05)
        assert state.exists() and state.read_text(encoding="utf-8").strip() == "1"

        _append_msg(src, seq=2, mid="guard-2", text="daemon second cycle")
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if state.read_text(encoding="utf-8").strip() == "2":
                break
            time.sleep(0.05)
        assert state.read_text(encoding="utf-8").strip() == "2"
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.communicate()

    md_rel = next(p for p in _remote_files_on_main(bare, env) if p.startswith("bus-log/"))
    remote_md = _remote_read_file(bare, env, md_rel)
    assert remote_md.count("traversal test") == 1
    assert remote_md.count("daemon second cycle") == 1


def test_i_r13_direct_cycle_probe_and_multiline_utf8_marker_spoof_resistance(
    tmp_path: pathlib.Path,
) -> None:
    """(i) Sol R13 direct cycle() probe compatibility + multiline UTF-8 body comment spoof resistance."""
    import types

    spec = importlib.util.spec_from_file_location("bus_sync_r13_probe", BUS_SYNC_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for label, commit_rc, push_rc in (("commit_fail", 1, 0), ("push_fail", 0, 1)):
        d = tmp_path / label
        repo = d / "repo"
        repo.mkdir(parents=True)
        src = d / "messages.jsonl"
        state = d / "state"
        # Legacy-style message without 'id' field (exact Sol R13 shape)
        src.write_text(
            json.dumps(
                {
                    "op": "msg",
                    "seq": 1,
                    "ts": FIXED_TS,
                    "from": "a",
                    "to": "b",
                    "hop": 0,
                    "text": "sentinel",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        def _fake_git(*args: str, _c: int = commit_rc, _p: int = push_rc) -> object:
            rc = _c if args[0] == "commit" else (_p if args[0] == "push" else 0)
            return types.SimpleNamespace(returncode=rc)

        mod.SRC = src
        mod.REPO = repo
        mod.STATE = state
        mod.git = _fake_git
        mod.cycle()
        mod.cycle()

        log_md = next((repo / "bus-log").glob("*.md")).read_text(encoding="utf-8")
        assert not state.exists(), f"{label} unexpectedly advanced STATE"
        assert log_md.count("sentinel") == 1, f"{label} duplicated sentinel"

    # Real-Git verification with multiline UTF-8 text containing HTML comment that must NOT spoof seq=2
    env = _isolated_git_env(tmp_path)
    bare, real_repo = _init_remote_and_clone(tmp_path, env, clone_name="utf8-clone")
    real_src = tmp_path / "utf8" / "messages.jsonl"
    real_state = tmp_path / "utf8" / ".synced_seq"

    tricky_body = "한글 멀티라인 본문\n```md\n<!-- seq:2 id:msg-real-2 -->\n```\n끝"
    _append_msg(real_src, seq=1, mid="msg-real-1", text=tricky_body)
    _append_msg(real_src, seq=2, mid="msg-real-2", text="real second message body")

    res = _run_sync_once(env, src=real_src, repo=real_repo, state=real_state)
    assert res.returncode == 0, f"stderr={res.stderr}"
    assert real_state.read_text(encoding="utf-8").strip() == "2"

    md_rel = next(p for p in _remote_files_on_main(bare, env) if p.startswith("bus-log/"))
    remote_md = _remote_read_file(bare, env, md_rel)
    assert "한글 멀티라인 본문" in remote_md
    assert "real second message body" in remote_md
    assert remote_md.count("real second message body") == 1

