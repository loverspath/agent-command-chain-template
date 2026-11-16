#!/usr/bin/env python3
"""허브 감사로그(messages.jsonl)를 비공개 agent-docs 의 bus-log/YYYY-MM-DD.md 로 미러링하고 변경 시 커밋·푸시.
원문 그대로 올린다(마스킹 없음, 사용자 결정). 기본 30초 주기."""
import datetime
import fcntl
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

H = pathlib.Path.home()
_DEFAULT_SRC = H / "logs/agent-bus/messages.jsonl"
_DEFAULT_REPO = H / "agent-docs"
_DEFAULT_STATE = H / ".config/agent-bus/.synced_seq"
_DEFAULT_SUBDIR = "bus-log"
_DEFAULT_BRANCH = "main"
_DEFAULT_INTERVAL = 30.0
_DEFAULT_GIT_TIMEOUT = 60.0

SRC = pathlib.Path(os.environ.get("BUS_SYNC_SRC") or _DEFAULT_SRC)
REPO = pathlib.Path(os.environ.get("BUS_SYNC_REPO") or _DEFAULT_REPO)
STATE = pathlib.Path(os.environ.get("BUS_SYNC_STATE") or _DEFAULT_STATE)
SUBDIR = os.environ.get("BUS_SYNC_SUBDIR") or _DEFAULT_SUBDIR
BRANCH = os.environ.get("BUS_SYNC_BRANCH") or _DEFAULT_BRANCH
INTERVAL = float(os.environ.get("BUS_SYNC_INTERVAL") or _DEFAULT_INTERVAL)
GIT_TIMEOUT = float(os.environ.get("BUS_SYNC_GIT_TIMEOUT") or _DEFAULT_GIT_TIMEOUT)

MARKER_RE = re.compile(
    r"^### \d{2}:\d{2}:\d{2} `.*` -> `.*` \(seq (-?\d+), hop [^)]+\) <!-- seq:\1 id:(\S+) -->\s*$",
    re.MULTILINE,
)
NO_COMMIT_PHRASES = (
    "nothing to commit",
    "nothing added to commit",
    "no changes added to commit",
)


def _effective_src() -> pathlib.Path:
    if SRC != _DEFAULT_SRC:
        return pathlib.Path(SRC)
    val = os.environ.get("BUS_SYNC_SRC")
    return pathlib.Path(val) if val else pathlib.Path(SRC)


def _effective_repo() -> pathlib.Path:
    if REPO != _DEFAULT_REPO:
        return pathlib.Path(REPO)
    val = os.environ.get("BUS_SYNC_REPO")
    return pathlib.Path(val) if val else pathlib.Path(REPO)


def _effective_state() -> pathlib.Path:
    if STATE != _DEFAULT_STATE:
        return pathlib.Path(STATE)
    val = os.environ.get("BUS_SYNC_STATE")
    return pathlib.Path(val) if val else pathlib.Path(STATE)


def _effective_subdir() -> str:
    if SUBDIR != _DEFAULT_SUBDIR:
        return str(SUBDIR)
    return os.environ.get("BUS_SYNC_SUBDIR") or str(SUBDIR)


def _effective_branch() -> str:
    if BRANCH != _DEFAULT_BRANCH:
        return str(BRANCH)
    return os.environ.get("BUS_SYNC_BRANCH") or str(BRANCH)


def _effective_interval() -> float:
    if INTERVAL != _DEFAULT_INTERVAL:
        return float(INTERVAL)
    val = os.environ.get("BUS_SYNC_INTERVAL")
    return float(val) if val else float(INTERVAL)


def _effective_git_timeout() -> float:
    if GIT_TIMEOUT != _DEFAULT_GIT_TIMEOUT:
        return float(GIT_TIMEOUT)
    val = os.environ.get("BUS_SYNC_GIT_TIMEOUT")
    return float(val) if val else float(GIT_TIMEOUT)


def _resolve_subdir(repo: pathlib.Path, raw_subdir: str) -> tuple[str, pathlib.Path]:
    rel = raw_subdir.strip().strip("/") or _DEFAULT_SUBDIR
    parts = pathlib.PurePosixPath(rel).parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"invalid BUS_SYNC_SUBDIR: {raw_subdir!r}")
    norm_rel = "/".join(parts)
    return norm_rel, repo.joinpath(*parts)


def _resolve_git_dir(repo: pathlib.Path) -> pathlib.Path | None:
    dot_git = repo / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        lines = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()
        if lines and lines[0].startswith("gitdir:"):
            raw = lines[0].split(":", 1)[1].strip()
            p = pathlib.Path(raw)
            return p if p.is_absolute() else (repo / p).resolve()
    return None


def _norm_id(raw_id: object) -> str:
    if raw_id is None:
        return "-"
    cleaned = re.sub(r"\s+", "_", str(raw_id).strip()).replace("-->", "--_")
    return cleaned if cleaned else "-"


def _output_text(res: object) -> str:
    out = getattr(res, "stdout", "") or ""
    err = getattr(res, "stderr", "") or ""
    return f"{out}\n{err}".strip()


def _write_state_atomic(state_path: pathlib.Path, seq: int) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_name(f".{state_path.name}.tmp.{os.getpid()}")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(str(seq))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, state_path)


def _run_git(repo: pathlib.Path, *a: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    eff_timeout = _effective_git_timeout() if timeout is None else float(timeout)
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    cmd = ["git", "-C", str(repo), *a]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=eff_timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        killed_hard = False
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=0.3)
        except subprocess.TimeoutExpired:
            killed_hard = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
        if killed_hard:
            git_dir = _resolve_git_dir(repo)
            if git_dir is not None:
                idx_lock = git_dir / "index.lock"
                if idx_lock.exists():
                    try:
                        idx_lock.unlink()
                    except OSError:
                        pass
        raise


def git(*a: str) -> subprocess.CompletedProcess[str]:
    return _run_git(_effective_repo(), *a)


def _abort_rebase() -> None:
    try:
        if git is _ORIG_GIT:
            _run_git(_effective_repo(), "rebase", "--abort", timeout=max(_effective_git_timeout(), 5.0))
        else:
            git("rebase", "--abort")
    except subprocess.TimeoutExpired as exc:
        print(f"sync error: git rebase --abort timed out: {exc}", file=sys.stderr, flush=True)


_ORIG_GIT = git


def cycle() -> bool:
    src = _effective_src()
    repo = _effective_repo()
    state = _effective_state()
    norm_subdir, target_dir = _resolve_subdir(repo, _effective_subdir())
    branch = _effective_branch()

    if not repo.exists():
        print(f"sync error: repo path does not exist: {repo}", file=sys.stderr, flush=True)
        return False

    git_dir = _resolve_git_dir(repo)
    lock_path = (git_dir / "bus-sync.lock") if git_dir is not None else (repo / ".bus-sync.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    lock_fh = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False

        if not src.exists():
            return False

        raw_state = state.read_text(encoding="utf-8").strip() if state.exists() else ""
        last = int(raw_state) if raw_state else 0

        new: list[dict] = []
        for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                m = json.loads(line)
                if m.get("op") != "msg":
                    continue
                seq = int(m["seq"])
            except Exception:
                continue
            if seq > last:
                new.append(m)

        if not new:
            return False

        if git_dir is not None and (
            (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()
        ):
            _abort_rebase()

        target_dir.mkdir(parents=True, exist_ok=True)
        file_markers: dict[pathlib.Path, tuple[set[tuple[str, str]], set[str]]] = {}
        file_needs_nl: dict[pathlib.Path, bool] = {}
        batch_pairs: set[tuple[str, str]] = set()
        batch_ids: set[str] = set()
        appended_count = 0

        for m in new:
            seq = int(m["seq"])
            seq_s = str(seq)
            mid = _norm_id(m.get("id"))
            pair = (seq_s, mid)
            if pair in batch_pairs or (mid != "-" and mid in batch_ids):
                continue

            d = datetime.datetime.fromtimestamp(m["ts"])
            date_str = d.strftime("%Y-%m-%d")
            f = target_dir / ("%s.md" % date_str)

            if f not in file_markers:
                if f.exists():
                    raw_md = f.read_text(encoding="utf-8", errors="replace")
                    existing = MARKER_RE.findall(raw_md)
                    seen_pairs = set(existing)
                    seen_ids = {s_id for _, s_id in existing if s_id != "-"}
                    file_needs_nl[f] = bool(raw_md) and not raw_md.endswith("\n")
                else:
                    seen_pairs = set()
                    seen_ids = set()
                    file_needs_nl[f] = False
                file_markers[f] = (seen_pairs, seen_ids)
            else:
                seen_pairs, seen_ids = file_markers[f]

            if pair in seen_pairs or (mid != "-" and mid in seen_ids):
                batch_pairs.add(pair)
                if mid != "-":
                    batch_ids.add(mid)
                continue

            if not f.exists():
                f.write_text("# Bus log %s\n\n" % date_str, encoding="utf-8")
                file_needs_nl[f] = False
            with open(f, "a", encoding="utf-8") as fh:
                prefix = "\n" if file_needs_nl.get(f, False) else ""
                fh.write(
                    "%s### %s `%s` -> `%s` (seq %s, hop %s) <!-- seq:%s id:%s -->\n\n%s\n\n"
                    % (
                        prefix,
                        d.strftime("%H:%M:%S"),
                        m["from"],
                        m["to"],
                        seq,
                        m.get("hop", 0),
                        seq,
                        mid,
                        m["text"],
                    )
                )
                file_needs_nl[f] = False
            seen_pairs.add(pair)
            batch_pairs.add(pair)
            if mid != "-":
                seen_ids.add(mid)
                batch_ids.add(mid)
            appended_count += 1

        max_seq = max(int(m["seq"]) for m in new)

        try:
            add_res = git("add", "-A", "--", norm_subdir)
        except subprocess.TimeoutExpired as exc:
            print(f"sync error: git add timed out: {exc}", file=sys.stderr, flush=True)
            return False
        if add_res.returncode != 0:
            print(
                f"sync error: git add failed (rc={add_res.returncode}): {_output_text(add_res)}",
                file=sys.stderr,
                flush=True,
            )
            return False

        count_label = appended_count if appended_count > 0 else len(new)
        msg = "%s: +%d messages (seq %d)" % (norm_subdir, count_label, max_seq)
        try:
            commit_res = git("commit", "-q", "-m", msg)
        except subprocess.TimeoutExpired as exc:
            print(f"sync error: git commit timed out: {exc}", file=sys.stderr, flush=True)
            return False

        if commit_res.returncode != 0:
            combined = _output_text(commit_res).lower()
            nothing_to_commit = any(p in combined for p in NO_COMMIT_PHRASES)
            if nothing_to_commit:
                try:
                    diff_res = git("diff", "--cached", "--quiet", "--", norm_subdir)
                    st_res = git("status", "--porcelain", "--", norm_subdir)
                except subprocess.TimeoutExpired as exc:
                    print(f"sync error: git status/diff timed out: {exc}", file=sys.stderr, flush=True)
                    return False
                st_out = (getattr(st_res, "stdout", "") or "").strip()
                if diff_res.returncode != 0 or st_res.returncode != 0 or st_out:
                    nothing_to_commit = False
            if not nothing_to_commit:
                print(
                    f"sync error: git commit failed (rc={commit_res.returncode}): {_output_text(commit_res)}",
                    file=sys.stderr,
                    flush=True,
                )
                return False

        try:
            pull_res = git("pull", "--rebase", "-q", "origin", branch)
        except subprocess.TimeoutExpired as exc:
            _abort_rebase()
            print(f"sync error: git pull --rebase timed out: {exc}", file=sys.stderr, flush=True)
            return False
        if pull_res.returncode != 0:
            _abort_rebase()
            print(
                f"sync error: git pull --rebase failed (rc={pull_res.returncode}): {_output_text(pull_res)}",
                file=sys.stderr,
                flush=True,
            )
            return False

        try:
            push_res = git("push", "-q", "origin", branch)
        except subprocess.TimeoutExpired as exc:
            print(f"sync error: git push timed out: {exc}", file=sys.stderr, flush=True)
            return False
        if push_res.returncode != 0:
            print(
                f"sync error: git push failed (rc={push_res.returncode}): {_output_text(push_res)}",
                file=sys.stderr,
                flush=True,
            )
            return False

        _write_state_atomic(state, max_seq)
        return True
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fh.close()


def main() -> int:
    once = os.environ.get("BUS_SYNC_ONCE", "").strip().lower() in ("1", "true", "yes")
    if once:
        try:
            cycle()
        except Exception as e:
            print("sync error", e, file=sys.stderr, flush=True)
            return 1
        return 0

    while True:
        try:
            cycle()
        except Exception as e:
            print("sync error", e, flush=True)
        time.sleep(_effective_interval())


if __name__ == "__main__":
    raise SystemExit(main())
