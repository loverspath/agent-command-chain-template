#!/usr/bin/env python3
"""ACC Dashboard Backend Server.

Lightweight read-only HTTP server providing real-time system state monitoring
for the agent-command-chain-template architecture.
Python 3 standard library ONLY.
"""

import argparse
import base64
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.parse


# ==============================================================================
# Sensitive Data Masking Patterns
# ==============================================================================
# All patterns are masked with '***'. Masking is ALWAYS performed BEFORE truncation.
MASK_PATTERNS = [
    # 1. Private keys (multi-line)
    (re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----"), "***"),
    # 2. JWT (JSON Web Tokens)
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "***"),
    # 3. Known vendor tokens
    (re.compile(r"\bgho_[A-Za-z0-9_]{10,}"), "***"),
    (re.compile(r"\bghp_[A-Za-z0-9_]{10,}"), "***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}"), "***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{10,}"), "***"),
    (re.compile(r"\bAIza[0-9A-Za-z-_]{35}"), "***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+"), "***"),
    # 4. Bearer & Authorization headers
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+"), "***"),
    (re.compile(r"(?i)\bAuthorization:\s+[^\r\n]+"), "***"),
    # 5. Key / Token / Secret / Password / API_KEY assignments (case-insensitive)
    (re.compile(r"(?i)\b[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|API_KEY)[A-Za-z0-9_]*\s*[:=]\s*(?:'[^']*'|\"[^\"]*\"|[^\s'\";]+)"), "***"),
    # 6. OAuth paths and tokens
    (re.compile(r"(?i)(?:https?://\S*oauth\S*|/\S*oauth\S*|\S*oauth\S*/\S*|[A-Za-z0-9_.-]*oauth[A-Za-z0-9_.-]*=[^\s'\";]+|\b[A-Za-z0-9_-]*oauth[A-Za-z0-9_-]{5,}\b)"), "***"),
    # 7. Hex blocks >= 40 chars
    (re.compile(r"\b[a-fA-F0-9]{40,}\b"), "***"),
    # 8. Base64 blocks >= 40 chars
    (re.compile(r"(?:[A-Za-z0-9+/]{40,}={0,2}|[A-Za-z0-9+/]{38,}={1,2})"), "***"),
]

# ANSI escape sequence regex
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def mask_sensitive(text):
    """Single-point masking function applied to ALL text before truncation."""
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    for pattern, repl in MASK_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def get_default_host():
    """Returns tailscale IPv4 if available, otherwise 127.0.0.1."""
    try:
        res = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=2
        )
        if res.returncode == 0:
            ip = res.stdout.strip()
            parts = ip.split(".")
            if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                return ip
    except Exception:
        pass
    return "127.0.0.1"


def get_default_runtime(repo_dir):
    """Finds runtime from ACC_RUNTIME env var or newest directory under repo runtime/."""
    env_runtime = os.environ.get("ACC_RUNTIME")
    if env_runtime and os.path.isdir(env_runtime):
        return os.path.abspath(env_runtime)

    runtime_base = os.path.join(repo_dir, "runtime")
    if not os.path.isdir(runtime_base):
        return None

    candidates = []
    try:
        for entry in os.listdir(runtime_base):
            full_path = os.path.join(runtime_base, entry)
            if os.path.isdir(full_path):
                has_session = os.path.isfile(os.path.join(full_path, "session_name"))
                has_bootstrap = os.path.isfile(os.path.join(full_path, "bootstrap_version"))
                if has_session or has_bootstrap:
                    try:
                        mtime = os.path.getmtime(full_path)
                        candidates.append((mtime, full_path))
                    except Exception:
                        pass
    except Exception:
        pass

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    return None


def clean_summary(b64_str):
    """Decodes base64 summary, strips control characters, masks sensitive tokens, and truncates to 160 chars."""
    if not b64_str:
        return ""
    try:
        raw = base64.b64decode(b64_str.strip().encode("utf-8", errors="ignore"), validate=False)
        decoded = raw.decode("utf-8", errors="replace")
    except Exception:
        return ""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", decoded).strip()
    masked = mask_sensitive(cleaned)
    return masked[:160]


def check_daemon(pid_file, pattern):
    """Checks if a daemon process is alive using PID file or pgrep."""
    if pid_file and os.path.isfile(pid_file):
        try:
            with open(pid_file, "r", encoding="utf-8", errors="replace") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            try:
                with open(f"/proc/{pid}/cmdline", "r", encoding="utf-8", errors="ignore") as f:
                    cmd = f.read()
                    if re.search(pattern, cmd):
                        return {"alive": True, "pid": pid}
            except Exception:
                return {"alive": True, "pid": pid}
        except Exception:
            pass

    try:
        res = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=1
        )
        if res.returncode == 0:
            pids = [int(p) for p in res.stdout.split() if p.isdigit()]
            if pids:
                return {"alive": True, "pid": pids[-1]}
    except Exception:
        pass

    return {"alive": False, "pid": None}


def parse_evt_file(filepath):
    """Parses single .evt file securely without exposing detail_path or reading unauthorized files."""
    kv = {}
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip()
    except Exception:
        return None

    created_epoch = None
    if "created_epoch" in kv and kv["created_epoch"]:
        try:
            created_epoch = int(float(kv["created_epoch"]))
        except Exception:
            pass
    if created_epoch is None:
        try:
            created_epoch = int(os.path.getmtime(filepath))
        except Exception:
            created_epoch = 0

    exit_code = None
    if "exit_code" in kv and kv["exit_code"]:
        try:
            exit_code = int(kv["exit_code"])
        except Exception:
            pass

    evt_id = kv.get("id")
    if not evt_id:
        base = os.path.basename(filepath)
        evt_id = base[:-4] if base.endswith(".evt") else base

    return {
        "id": evt_id,
        "source": kv.get("source", "unknown"),
        "kind": kv.get("kind", "unknown"),
        "task_id": kv.get("task_id") or None,
        "created_epoch": created_epoch,
        "exit_code": exit_code,
        "summary": clean_summary(kv.get("summary_b64", ""))
    }


def collect_system_state(repo_dir, runtime_dir):
    """Collects aggregated system state conforming strictly to schema. Read-only."""
    now_dt = datetime.now(timezone.utc)
    server_time = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    runtime_name = os.path.basename(runtime_dir) if runtime_dir else "unknown"

    # 1. Server & Git info
    git_head = "unknown"
    git_tags = []
    if repo_dir and os.path.isdir(repo_dir):
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=2
            )
            if res.returncode == 0:
                git_head = res.stdout.strip()
        except Exception:
            pass
        try:
            res = subprocess.run(
                ["git", "tag", "--points-at", "HEAD"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=2
            )
            if res.returncode == 0:
                git_tags = [t.strip() for t in res.stdout.splitlines() if t.strip()]
        except Exception:
            pass

    server_info = {
        "time": server_time,
        "runtime_name": runtime_name,
        "git": {
            "head": git_head,
            "tags": git_tags
        }
    }

    # 2. Session info
    session_name = "agentchain"
    if runtime_dir:
        sn_file = os.path.join(runtime_dir, "session_name")
        if os.path.isfile(sn_file):
            try:
                with open(sn_file, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read().strip()
                    if content:
                        session_name = content
            except Exception:
                pass

    windows = []
    if session_name and session_name != "unknown":
        try:
            cmd = [
                "tmux", "list-windows", "-t", session_name,
                "-F", "#{window_id}\t#{window_name}\t#{window_active}\t#{pane_current_command}"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if res.returncode == 0:
                for line in res.stdout.strip().splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 4:
                        windows.append({
                            "id": parts[0],
                            "name": parts[1],
                            "active": parts[2] == "1",
                            "pane_command": parts[3]
                        })
        except Exception:
            pass

    session_info = {
        "name": session_name,
        "windows": windows
    }

    # 3. Workers info
    workers = {
        "agy": {"mode": "oneshot", "busy_task_id": None},
        "codex": {"mode": "oneshot", "busy_task_id": None},
        "sonnet": {"mode": "supervisor", "busy_task_id": None}
    }
    if runtime_dir:
        for w in ["agy", "codex"]:
            res_file = os.path.join(runtime_dir, "workers", f"{w}.resident")
            if os.path.isfile(res_file):
                workers[w]["mode"] = "resident"
            busy_file = os.path.join(runtime_dir, "workers", f"{w}.busy")
            if os.path.isfile(busy_file):
                try:
                    with open(busy_file, "r", encoding="utf-8", errors="replace") as f:
                        line = f.readline().strip()
                        if line:
                            workers[w]["busy_task_id"] = line
                except Exception:
                    pass

    # 4. Daemons info
    watchdog_pid_file = os.path.join(runtime_dir, "watchdog.pid") if runtime_dir else None
    listener_pid_file = os.path.join(runtime_dir, "listener.pid") if runtime_dir else None
    daemons = {
        "watchdog": check_daemon(watchdog_pid_file, r"watchdog-v2\.sh"),
        "listener": check_daemon(listener_pid_file, r"sonnet-event-wait\.sh")
    }

    # 5. Events info
    counts = {"pending": 0, "inflight": 0, "archive": 0}
    evt_files = []
    if runtime_dir:
        for cat in ["pending", "inflight", "archive"]:
            cdir = os.path.join(runtime_dir, "events", cat)
            if os.path.isdir(cdir):
                try:
                    for root, _, files in os.walk(cdir):
                        for f in files:
                            if f.endswith(".evt"):
                                counts[cat] += 1
                                fp = os.path.join(root, f)
                                try:
                                    evt_files.append((os.path.getmtime(fp), fp))
                                except Exception:
                                    pass
                except Exception:
                    pass

    evt_files.sort(key=lambda x: x[0], reverse=True)
    parsed_events = []
    terminal_events_by_id = {}
    terminal_events_by_task = {}

    for _, fp in evt_files:
        pe = parse_evt_file(fp)
        if pe:
            parsed_events.append(pe)
            if pe["id"].endswith("-terminal") or pe["kind"] in ("done", "error", "canceled", "timeout"):
                terminal_events_by_id[pe["id"]] = pe
                if pe["task_id"]:
                    terminal_events_by_task[pe["task_id"]] = pe

    parsed_events.sort(key=lambda x: (x["created_epoch"] or 0, x["id"]), reverse=True)
    recent_events = parsed_events[:40]

    events_info = {
        "pending_count": counts["pending"],
        "inflight_count": counts["inflight"],
        "archive_count": counts["archive"],
        "recent": recent_events
    }

    # 6. Tasks info
    tasks = []
    if runtime_dir:
        tdir = os.path.join(runtime_dir, "tasks")
        if os.path.isdir(tdir):
            try:
                for t_entry in os.listdir(tdir):
                    task_path = os.path.join(tdir, t_entry)
                    if not os.path.isdir(task_path):
                        continue
                    state_file = os.path.join(task_path, "state")
                    kv = {}
                    if os.path.isfile(state_file):
                        try:
                            with open(state_file, "r", encoding="utf-8", errors="replace") as f:
                                for line in f:
                                    line = line.strip()
                                    if "=" in line:
                                        k, v = line.split("=", 1)
                                        kv[k.strip()] = v.strip()
                        except Exception:
                            pass

                    task_id = kv.get("task_id", t_entry)
                    worker = kv.get("worker")
                    if not worker:
                        worker = t_entry.split("-")[0] if "-" in t_entry else "unknown"

                    mode = kv.get("mode")
                    if not mode:
                        worker_res = os.path.join(runtime_dir, "workers", f"{worker}.resident")
                        mode = "resident" if os.path.isfile(worker_res) else "oneshot"

                    status = kv.get("status", "unknown")

                    started_at = None
                    if "started_epoch" in kv:
                        try:
                            started_at = int(kv["started_epoch"])
                        except Exception:
                            pass
                    if started_at is None:
                        try:
                            started_at = int(os.path.getmtime(task_path))
                        except Exception:
                            pass

                    deadline_at = None
                    if "deadline_epoch" in kv:
                        try:
                            deadline_at = int(kv["deadline_epoch"])
                        except Exception:
                            pass

                    finished_at = None
                    if "completed_epoch" in kv:
                        try:
                            finished_at = int(kv["completed_epoch"])
                        except Exception:
                            pass

                    exit_code = None
                    if "exit_code" in kv and kv["exit_code"]:
                        try:
                            exit_code = int(kv["exit_code"])
                        except Exception:
                            pass

                    term_evt_id = kv.get("terminal_event_id")
                    term_evt = (
                        terminal_events_by_id.get(term_evt_id)
                        if term_evt_id else terminal_events_by_task.get(task_id)
                    )
                    if term_evt:
                        if finished_at is None and term_evt.get("created_epoch"):
                            finished_at = term_evt["created_epoch"]
                        if exit_code is None and term_evt.get("exit_code") is not None:
                            exit_code = term_evt["exit_code"]

                    if exit_code is None and status == "done":
                        exit_code = 0

                    now_epoch = int(time.time())
                    duration_s = None
                    if started_at is not None:
                        if finished_at is not None:
                            duration_s = max(0, finished_at - started_at)
                        elif status == "running":
                            duration_s = max(0, now_epoch - started_at)

                    tasks.append({
                        "id": task_id,
                        "worker": worker,
                        "mode": mode,
                        "status": status,
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "duration_s": duration_s,
                        "exit_code": exit_code,
                        "deadline_at": deadline_at
                    })
            except Exception:
                pass

    tasks.sort(key=lambda x: (x["started_at"] or 0, x["id"]), reverse=True)
    tasks = tasks[:30]

    # 7. Queue info
    busy_starts = []
    if runtime_dir:
        for w in ["agy", "codex"]:
            if workers[w]["busy_task_id"]:
                busy_file = os.path.join(runtime_dir, "workers", f"{w}.busy")
                ts = None
                if os.path.isfile(busy_file):
                    try:
                        with open(busy_file, "r", encoding="utf-8", errors="replace") as f:
                            lines = f.read().splitlines()
                            if len(lines) >= 2 and lines[1].strip().isdigit():
                                ts = int(lines[1].strip())
                    except Exception:
                        pass
                if ts is None:
                    tid = workers[w]["busy_task_id"]
                    st_file = os.path.join(runtime_dir, "tasks", tid, "state")
                    if os.path.isfile(st_file):
                        try:
                            with open(st_file, "r", encoding="utf-8", errors="replace") as f:
                                for line in f:
                                    if line.startswith("started_epoch="):
                                        ts = int(line.split("=", 1)[1].strip())
                                        break
                        except Exception:
                            pass
                if ts is None and os.path.isfile(busy_file):
                    try:
                        ts = int(os.path.getmtime(busy_file))
                    except Exception:
                        pass
                if ts:
                    busy_starts.append(ts)

    busy_elapsed_s = None
    if busy_starts:
        busy_elapsed_s = max(0, int(time.time() - min(busy_starts)))

    queue_info = {
        "busy_elapsed_s": busy_elapsed_s
    }

    return {
        "server": server_info,
        "session": session_info,
        "workers": workers,
        "daemons": daemons,
        "tasks": tasks,
        "events": events_info,
        "queue": queue_info
    }


# ==============================================================================
# Whitelist Validation & Detail Helpers
# ==============================================================================

def validate_transcript_path(path, transcript_root):
    r"""Validates transcript path against strict whitelist.

    Rules:
    - Path must normalize via realpath inside os.path.realpath(transcript_root) + "/brain/"
    - Must contain "/.system_generated/logs/"
    - Filename must match r"^transcript.*\.jsonl$"
    - Target must be a regular existing file
    - Rejects path traversal, symlink escapes, or foreign directories.
    Returns (is_valid: bool, realpath: str or None).
    """
    if not path or not transcript_root:
        return False, None
    try:
        real_root = os.path.realpath(transcript_root)
        brain_root = os.path.join(real_root, "brain")
        brain_prefix = brain_root if brain_root.endswith(os.sep) else brain_root + os.sep

        real_target = os.path.realpath(path)

        if not real_target.startswith(brain_prefix):
            return False, None

        logs_segment = f"{os.sep}.system_generated{os.sep}logs{os.sep}"
        if logs_segment not in real_target:
            return False, None

        basename = os.path.basename(real_target)
        if not re.match(r"^transcript.*\.jsonl$", basename):
            return False, None

        if not os.path.isfile(real_target):
            return False, None

        return True, real_target
    except Exception:
        return False, None


def extract_prompt_title(prompt_path):
    """Extracts first heading (#, ##) or first meaningful line skipping [CHAIN CONTEXT]...--- block.

    Max 200 chars. Masked. Never full prompt.
    """
    if not prompt_path or not os.path.isfile(prompt_path):
        return ""
    try:
        with open(prompt_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return ""

    in_chain_context = False
    in_frontmatter = False
    candidate_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped == "[CHAIN CONTEXT]":
            in_chain_context = True
            continue

        if in_chain_context:
            if stripped == "---":
                in_chain_context = False
            continue

        if stripped == "---":
            if not in_frontmatter and not candidate_lines:
                in_frontmatter = True
                continue
            elif in_frontmatter:
                in_frontmatter = False
                continue

        if in_frontmatter:
            continue

        candidate_lines.append(stripped)

    chosen_line = ""
    for c in candidate_lines:
        if c.startswith("#"):
            chosen_line = c
            break

    if not chosen_line and candidate_lines:
        chosen_line = candidate_lines[0]

    masked = mask_sensitive(chosen_line)
    return masked[:200]


def extract_output_tail(output_path):
    """Extracts the last 8KB of output.log if exists, ANSI stripped, masked.

    Masking is performed before truncation to avoid boundary leakage.
    """
    if not output_path or not os.path.isfile(output_path):
        return ""
    try:
        size = os.path.getsize(output_path)
        with open(output_path, "rb") as f:
            read_size = min(size, 65536)
            if size > read_size:
                f.seek(size - read_size)
            raw_bytes = f.read()
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        ansi_clean = ANSI_ESCAPE_RE.sub("", raw_text)
        masked = mask_sensitive(ansi_clean)
        if len(masked) > 8192:
            return masked[-8192:]
        return masked
    except Exception:
        return ""


TASK_PROMPT_RE = re.compile(r"tasks/([A-Za-z0-9._-]+)/prompt\.md")

META_PREFIXES = (
    "created at:",
    "completed at:",
    "file path:",
    "step id:",
    "total lines:",
    "total bytes:",
    "showing lines",
)

PATH_KEYS = {
    "absolutepath",
    "targetfile",
    "path",
    "filepath",
    "targetdir",
    "cwd",
    "targetdirectory",
    "file_path",
    "target_file",
    "absolute_path",
}


def strip_meta_prefix(text):
    """Strips leading lines that start with metadata prefixes."""
    if not text:
        return ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        line_lower = line.lower()
        if any(line_lower.startswith(p) for p in META_PREFIXES) or not line:
            i += 1
        else:
            break
    return "\n".join(lines[i:]).strip()


def abbreviate_path(val):
    """Abbreviates a path string to .../<parent>/<basename>."""
    if not isinstance(val, str):
        return str(val)
    s = val.strip().strip("\"'")
    if s.startswith("file://"):
        s = s[7:]
    norm = os.path.normpath(s).replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    if len(parts) >= 2:
        return f".../{parts[-2]}/{parts[-1]}"
    elif len(parts) == 1:
        return parts[0]
    return s


def is_path_key(k):
    kl = str(k).lower()
    return kl in PATH_KEYS or kl.endswith("path") or kl.endswith("file") or kl.endswith("dir")


def format_compact_args(args):
    """Formats tool call arguments concisely, abbreviating path arguments."""
    if not isinstance(args, dict):
        if isinstance(args, str):
            if "/" in args or "\\" in args:
                return abbreviate_path(args)
            return args
        return json.dumps(args, ensure_ascii=False)

    # Common tool shortcuts
    if "CommandLine" in args:
        return str(args["CommandLine"])
    if "AbsolutePath" in args:
        return abbreviate_path(args["AbsolutePath"])
    if "TargetFile" in args:
        return abbreviate_path(args["TargetFile"])
    if "path" in args:
        return abbreviate_path(args["path"])
    if "query" in args:
        return str(args["query"])
    if "Url" in args:
        return str(args["Url"])

    items = []
    for k, v in args.items():
        if isinstance(v, str):
            if is_path_key(k) or (("/" in v or "\\" in v) and not v.startswith("{")):
                v_str = abbreviate_path(v)
            else:
                v_str = v
        elif isinstance(v, (dict, list)):
            v_str = json.dumps(v, ensure_ascii=False)
        else:
            v_str = str(v)
        items.append(f"{k}: {v_str}")
    return ", ".join(items)


def parse_step_epoch(ts):
    """Parses timestamp string or number into unix epoch seconds."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, str):
        try:
            return int(float(ts))
        except ValueError:
            pass
        try:
            clean_ts = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_ts)
            return int(dt.timestamp())
        except Exception:
            pass
    return None


def is_user_input_step(step):
    """Checks if step is a user input step."""
    source = str(step.get("source") or "")
    stype = str(step.get("type") or "")
    if source in ("USER", "USER_EXPLICIT") or stype in ("USER_INPUT", "USER_REQUEST"):
        return True
    content = step.get("content")
    if isinstance(content, str) and ("<USER_REQUEST>" in content or "Read and execute task prompt:" in content):
        return True
    return False


def get_other_task_id_from_user_input(step, current_task_id):
    """Returns other task ID if this step is a user input containing prompt of another task."""
    if not is_user_input_step(step):
        return None
    content = step.get("content")
    if isinstance(content, str):
        matches = TASK_PROMPT_RE.findall(content)
        for m in matches:
            if m != current_task_id:
                return m
    try:
        s = json.dumps(step)
        matches = TASK_PROMPT_RE.findall(s)
        for m in matches:
            if m != current_task_id:
                return m
    except Exception:
        pass
    return None


def step_contains_task_prompt(step, tid):
    """Checks if step contains tasks/<tid>/prompt.md in content or tool call args."""
    target = f"tasks/{tid}/prompt.md"
    try:
        return target in json.dumps(step)
    except Exception:
        return False


def slice_task_transcript(transcript_path, task_id, started_at=None, candidate_from_event=False):
    """Reads last 2MB of transcript, slices steps for task_id, and formats timeline.

    Returns:
        (timeline, linked, truncated_before)
        where linked is 'exact' | 'partial' | 'inferred' | 'none'
        and truncated_before is bool.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return [], "none", False

    seek_offset = 0
    raw_lines = []
    try:
        file_size = os.path.getsize(transcript_path)
        seek_offset = max(0, file_size - 2 * 1024 * 1024)
        with open(transcript_path, "rb") as f:
            if seek_offset > 0:
                f.seek(seek_offset)
                f.readline()  # Discard partial first line
            for raw_line in f:
                line_str = raw_line.decode("utf-8", errors="replace").strip()
                if line_str:
                    raw_lines.append(line_str)
    except Exception:
        return [], "none", False

    if not raw_lines:
        return [], "none", False

    parsed_raw_steps = []
    for line_idx, line in enumerate(raw_lines):
        try:
            d = json.loads(line)
            if isinstance(d, dict):
                if "step_index" not in d or not isinstance(d.get("step_index"), int):
                    d["step_index"] = line_idx
                parsed_raw_steps.append(d)
        except Exception:
            continue

    if not parsed_raw_steps:
        return [], "none", False

    # Check if there are any tasks/<id>/prompt.md markers in the window
    has_any_task_markers = any(bool(TASK_PROMPT_RE.search(l)) for l in raw_lines)

    start_idx = None
    if task_id:
        for idx, step in enumerate(parsed_raw_steps):
            if step_contains_task_prompt(step, task_id):
                start_idx = idx
                break

    # If no start marker found, but this is a dedicated transcript from an event
    # with NO multi-task markers at all in the file (legacy/mock unit test case):
    if start_idx is None and not has_any_task_markers and candidate_from_event:
        start_idx = 0

    task_slice = []
    linked = "none"
    truncated_before = False

    if start_idx is not None:
        # Task start marker found at start_idx
        end_idx = None
        for idx in range(start_idx + 1, len(parsed_raw_steps)):
            step = parsed_raw_steps[idx]
            other_id = get_other_task_id_from_user_input(step, task_id)
            if other_id:
                end_idx = idx
                break

        if end_idx is not None:
            task_slice = parsed_raw_steps[start_idx:end_idx]
        else:
            task_slice = parsed_raw_steps[start_idx:]

        linked = "exact"
        truncated_before = bool(seek_offset > 0 and start_idx == 0)

    else:
        # Start marker NOT found in window
        earliest_epoch = parse_step_epoch(parsed_raw_steps[0].get("created_at"))
        if seek_offset > 0 and started_at is not None and earliest_epoch is not None and started_at <= earliest_epoch:
            # Task started before or at earliest step in 2MB window
            end_idx = None
            for idx, step in enumerate(parsed_raw_steps):
                other_id = get_other_task_id_from_user_input(step, task_id)
                if other_id:
                    end_idx = idx
                    break

            if end_idx is not None:
                task_slice = parsed_raw_steps[:end_idx]
            else:
                task_slice = parsed_raw_steps[:]

            linked = "partial"
            truncated_before = True
        else:
            linked = "inferred"
            task_slice = []
            truncated_before = False

    formatted_steps_rev = []
    for step in reversed(task_slice):
        step_idx = step.get("step_index")
        if step_idx is None or not isinstance(step_idx, int):
            step_idx = 0

        timestamp = str(step.get("created_at") or "")
        tool_calls = step.get("tool_calls")
        source = str(step.get("source") or "")
        entry_type = str(step.get("type") or "")
        content = step.get("content")
        thinking = step.get("thinking")

        step_type = "system"
        raw_summary = ""

        if isinstance(tool_calls, list) and len(tool_calls) > 0:
            tc = tool_calls[0]
            if isinstance(tc, dict):
                fn_name = tc.get("name") or tc.get("function") or "tool"
                args = tc.get("args") or tc.get("arguments") or tc.get("parameters") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass

                if "subagent" in fn_name.lower() or fn_name in ("invoke_subagent", "manage_subagents", "send_message", "define_subagent"):
                    step_type = "subagent"
                    if fn_name == "invoke_subagent":
                        sub_list = args.get("Subagents") if isinstance(args, dict) else None
                        if isinstance(sub_list, str):
                            try:
                                sub_list = json.loads(sub_list)
                            except Exception:
                                pass
                        if isinstance(sub_list, list) and len(sub_list) > 0 and isinstance(sub_list[0], dict):
                            s0 = sub_list[0]
                            role = s0.get("Role") or s0.get("TypeName") or "subagent"
                            prompt_snip = s0.get("Prompt") or ""
                            raw_summary = f"{role}: {prompt_snip}"
                        else:
                            raw_summary = f"invoke_subagent: {format_compact_args(args)}"
                    elif fn_name == "send_message":
                        recip = args.get("Recipient") if isinstance(args, dict) else ""
                        msg = args.get("Message") if isinstance(args, dict) else ""
                        raw_summary = f"send_message to {recip}: {msg}"
                    else:
                        raw_summary = f"{fn_name}: {format_compact_args(args)}"
                else:
                    step_type = "tool_call"
                    compact = format_compact_args(args)
                    raw_summary = f"{fn_name}: {compact}"
            else:
                step_type = "tool_call"
                raw_summary = str(tc)
        else:
            c_str = content if isinstance(content, str) else (json.dumps(content, ensure_ascii=False) if content is not None else "")
            if source in ("SUBAGENT", "AGENT") or entry_type in ("SUBAGENT_MESSAGE", "SUBAGENT"):
                step_type = "subagent"
                raw_summary = c_str
            elif isinstance(content, str) and ("[Message]" in content and "sender=" in content):
                step_type = "subagent"
                raw_summary = c_str
            elif source == "MODEL" or entry_type in ("PLANNER_RESPONSE", "GENERIC", "MODEL"):
                step_type = "model"
                raw_summary = c_str if c_str else (thinking if isinstance(thinking, str) else "")
                # Omit empty model responses
                if not raw_summary or not raw_summary.strip():
                    continue
            else:
                step_type = "system"
                raw_summary = c_str

        # 1. Meta prefix cleanup
        raw_summary = strip_meta_prefix(raw_summary)

        # Re-check empty model response after meta prefix stripping
        if step_type == "model" and not raw_summary.strip():
            continue

        # 2. Control characters cleanup
        cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", raw_summary).strip()

        # 3. Single-point masking BEFORE truncation
        masked = mask_sensitive(cleaned)

        # 4. Truncate summary to max 300 chars
        final_summary = masked[:300]

        formatted_steps_rev.append({
            "step_index": step_idx,
            "timestamp": timestamp,
            "type": step_type,
            "summary": final_summary
        })
        if len(formatted_steps_rev) >= 60:
            break

    formatted_steps_rev.reverse()
    timeline = formatted_steps_rev
    return timeline, linked, truncated_before


def parse_transcript_timeline(transcript_path, task_id=None, started_at=None, candidate_from_event=False):
    """Backwards-compatible wrapper returning only the timeline list."""
    tl, _, _ = slice_task_transcript(transcript_path, task_id, started_at, candidate_from_event)
    return tl


def find_task_events(runtime_dir, task_id):
    """Finds all events associated with task_id across pending, inflight, archive."""
    events = []
    raw_events = []
    if not runtime_dir:
        return events, raw_events

    for cat in ("pending", "inflight", "archive"):
        cdir = os.path.join(runtime_dir, "events", cat)
        if not os.path.isdir(cdir):
            continue
        try:
            for root, _, files in os.walk(cdir):
                for f in files:
                    if f.endswith(".evt"):
                        fp = os.path.join(root, f)
                        try:
                            pe = parse_evt_file(fp)
                            if pe and (pe.get("task_id") == task_id or pe.get("id", "").startswith(f"{task_id}-")):
                                events.append(pe)
                                kv = {}
                                with open(fp, "r", encoding="utf-8", errors="replace") as rf:
                                    for line in rf:
                                        line = line.strip()
                                        if "=" in line:
                                            k, v = line.split("=", 1)
                                            kv[k.strip()] = v.strip()
                                raw_events.append(kv)
                        except Exception:
                            pass
        except Exception:
            pass

    paired = list(zip(events, raw_events))
    paired.sort(key=lambda x: (x[0]["created_epoch"] or 0, x[0]["id"]), reverse=True)
    events = [p[0] for p in paired]
    raw_events = [p[1] for p in paired]
    return events, raw_events


def collect_task_detail(runtime_dir, task_id, transcript_root):
    """Collects detailed state for GET /api/task/<id>. Read-only."""
    if not runtime_dir:
        return None

    task_dir = os.path.join(runtime_dir, "tasks", task_id)
    if not os.path.isdir(task_dir):
        return None

    # 1. Parse task state
    state_file = os.path.join(task_dir, "state")
    kv = {}
    if os.path.isfile(state_file):
        try:
            with open(state_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line:
                        k, v = line.split("=", 1)
                        kv[k.strip()] = v.strip()
        except Exception:
            pass

    worker = kv.get("worker")
    if not worker:
        worker = task_id.split("-")[0] if "-" in task_id else "unknown"

    mode = kv.get("mode")
    if not mode:
        worker_res = os.path.join(runtime_dir, "workers", f"{worker}.resident")
        mode = "resident" if os.path.isfile(worker_res) else "oneshot"

    status = kv.get("status", "unknown")

    started_at = None
    if "started_epoch" in kv:
        try:
            started_at = int(kv["started_epoch"])
        except Exception:
            pass
    if started_at is None:
        try:
            started_at = int(os.path.getmtime(task_dir))
        except Exception:
            pass

    deadline_at = None
    if "deadline_epoch" in kv:
        try:
            deadline_at = int(kv["deadline_epoch"])
        except Exception:
            pass

    finished_at = None
    if "completed_epoch" in kv:
        try:
            finished_at = int(kv["completed_epoch"])
        except Exception:
            pass

    exit_code = None
    if "exit_code" in kv and kv["exit_code"]:
        try:
            exit_code = int(kv["exit_code"])
        except Exception:
            pass

    # 2. Connected events
    events, raw_events = find_task_events(runtime_dir, task_id)

    # Check terminal event among connected events
    term_evt_id = kv.get("terminal_event_id")
    for pe in events:
        if (term_evt_id and pe["id"] == term_evt_id) or pe["id"].endswith("-terminal") or pe["kind"] in ("done", "error", "canceled", "timeout"):
            if finished_at is None and pe.get("created_epoch"):
                finished_at = pe["created_epoch"]
            if exit_code is None and pe.get("exit_code") is not None:
                exit_code = pe["exit_code"]
            break

    if exit_code is None and status == "done":
        exit_code = 0

    now_epoch = int(time.time())
    duration_s = None
    if started_at is not None:
        if finished_at is not None:
            duration_s = max(0, finished_at - started_at)
        elif status == "running":
            duration_s = max(0, now_epoch - started_at)

    meta = {
        "id": task_id,
        "worker": worker,
        "mode": mode,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": duration_s,
        "exit_code": exit_code,
        "deadline_at": deadline_at
    }

    # 3. Prompt title
    prompt_path = os.path.join(task_dir, "prompt.md")
    prompt_title = extract_prompt_title(prompt_path)

    # 4. Output tail
    output_path = os.path.join(task_dir, "output.log")
    output_tail = extract_output_tail(output_path)

    # 5. Transcript linking & Whitelist validation
    transcript_path = None
    candidate_from_event = False

    # Exact link from connected events (detail_path_b64 / detail_path)
    for revt in raw_events:
        candidate_path = None
        if "detail_path_b64" in revt and revt["detail_path_b64"]:
            try:
                candidate_path = base64.b64decode(
                    revt["detail_path_b64"].strip().encode("utf-8", errors="ignore"),
                    validate=False
                ).decode("utf-8", errors="replace").strip()
            except Exception:
                candidate_path = None
        elif "detail_path" in revt and revt["detail_path"]:
            candidate_path = revt["detail_path"].strip()

        if candidate_path:
            is_valid, real_p = validate_transcript_path(candidate_path, transcript_root)
            if is_valid:
                transcript_path = real_p
                candidate_from_event = True
                break

    # Inferred link if not found in events and task is running or resident
    if not transcript_path and (status == "running" or mode == "resident"):
        brain_dir = os.path.join(transcript_root, "brain")
        if os.path.isdir(brain_dir):
            started_epoch = started_at or 0
            candidates = []
            try:
                for uuid_entry in os.listdir(brain_dir):
                    logs_dir = os.path.join(brain_dir, uuid_entry, ".system_generated", "logs")
                    if os.path.isdir(logs_dir):
                        try:
                            for fname in os.listdir(logs_dir):
                                if re.match(r"^transcript.*\.jsonl$", fname):
                                    fpath = os.path.join(logs_dir, fname)
                                    is_valid, real_p = validate_transcript_path(fpath, transcript_root)
                                    if is_valid:
                                        try:
                                            mtime = os.path.getmtime(real_p)
                                            if mtime >= started_epoch:
                                                candidates.append((mtime, real_p))
                                        except Exception:
                                            pass
                        except Exception:
                            pass
            except Exception:
                pass

            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                chosen = None
                for _, cp in candidates:
                    try:
                        with open(cp, "r", encoding="utf-8", errors="replace") as cf:
                            first_line = cf.readline()
                            fl = first_line.lower()
                            is_sub = ("subagent_reminder" in fl or "caller agent" in fl or
                                      "sender=" in fl or "[message]" in fl or
                                      ("subagent" in fl and ("recipient" in fl or "system_message" in fl)))
                            if not is_sub:
                                chosen = cp
                                break
                    except Exception:
                        pass
                if not chosen:
                    chosen = candidates[0][1]
                transcript_path = chosen
                candidate_from_event = False

    # 6. Parse timeline and task slice from validated transcript
    timeline, linked, truncated_before = slice_task_transcript(
        transcript_path,
        task_id=task_id,
        started_at=started_at,
        candidate_from_event=candidate_from_event
    )

    formatted_events = [
        {
            "id": e["id"],
            "kind": e["kind"],
            "created_epoch": e["created_epoch"],
            "summary": e["summary"]
        }
        for e in events[:50]
    ]

    return {
        "meta": meta,
        "prompt_title": prompt_title,
        "output_tail": output_tail,
        "timeline": timeline,
        "events": formatted_events,
        "linked": linked,
        "truncated_before": truncated_before
    }


def find_event_file(runtime_dir, event_id):
    """Searches for event file in runtime/events/pending, inflight, archive."""
    if not runtime_dir:
        return None
    for cat in ("pending", "inflight", "archive"):
        cdir = os.path.join(runtime_dir, "events", cat)
        if not os.path.isdir(cdir):
            continue
        direct = os.path.join(cdir, f"{event_id}.evt")
        if os.path.isfile(direct):
            return direct
        try:
            for root, _, files in os.walk(cdir):
                for f in files:
                    if f == f"{event_id}.evt" or f == event_id:
                        return os.path.join(root, f)
        except Exception:
            pass

    for cat in ("pending", "inflight", "archive"):
        cdir = os.path.join(runtime_dir, "events", cat)
        if not os.path.isdir(cdir):
            continue
        try:
            for root, _, files in os.walk(cdir):
                for f in files:
                    if f.endswith(".evt"):
                        fp = os.path.join(root, f)
                        try:
                            with open(fp, "r", encoding="utf-8", errors="replace") as ef:
                                for line in ef:
                                    if line.startswith("id="):
                                        if line.split("=", 1)[1].strip() == event_id:
                                            return fp
                                        break
                        except Exception:
                            pass
        except Exception:
            pass
    return None


def collect_event_detail(runtime_dir, event_id):
    """Collects detailed state for GET /api/event/<id>. Read-only."""
    fp = find_event_file(runtime_dir, event_id)
    if not fp:
        return None
    kv = {}
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip()
    except Exception:
        return None

    created_epoch = None
    if "created_epoch" in kv and kv["created_epoch"]:
        try:
            created_epoch = int(float(kv["created_epoch"]))
        except Exception:
            pass
    if created_epoch is None:
        try:
            created_epoch = int(os.path.getmtime(fp))
        except Exception:
            created_epoch = 0

    exit_code = None
    if "exit_code" in kv and kv["exit_code"]:
        try:
            exit_code = int(kv["exit_code"])
        except Exception:
            pass

    actual_id = kv.get("id")
    if not actual_id:
        base = os.path.basename(fp)
        actual_id = base[:-4] if base.endswith(".evt") else base

    raw_summary = ""
    if "summary_b64" in kv and kv["summary_b64"]:
        try:
            raw_b = base64.b64decode(kv["summary_b64"].strip().encode("utf-8", errors="ignore"), validate=False)
            raw_summary = raw_b.decode("utf-8", errors="replace")
        except Exception:
            raw_summary = ""
    elif "summary" in kv:
        raw_summary = kv["summary"]

    cleaned_summary = re.sub(r"[\x00-\x1f\x7f]", " ", raw_summary).strip()
    masked_summary = mask_sensitive(cleaned_summary)
    final_summary = masked_summary[:500]

    return {
        "id": actual_id,
        "kind": kv.get("kind", "unknown"),
        "source": kv.get("source", "unknown"),
        "created_epoch": created_epoch,
        "exit_code": exit_code,
        "task_id": kv.get("task_id") or None,
        "summary": final_summary
    }


# ==============================================================================
# HTTP Server and Handler
# ==============================================================================

class DualStackThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, repo_dir=None, runtime_dir=None, transcript_root=None):
        host = server_address[0]
        if ":" in host:
            self.address_family = socket.AF_INET6
        self.repo_dir = repo_dir
        self.runtime_dir = runtime_dir
        self.transcript_root = transcript_root or os.path.expanduser("~/.gemini/antigravity-cli")
        self._cache_lock = threading.Lock()
        self._state_cache = {"timestamp": 0.0, "data": b""}
        self._task_cache_lock = threading.Lock()
        self._task_cache = {}
        self._event_cache_lock = threading.Lock()
        self._event_cache = {}
        super().__init__(server_address, RequestHandlerClass)

    def get_cached_state(self):
        """Returns in-memory cached state JSON (1.5s TTL) to prevent disk thrashing."""
        now = time.time()
        with self._cache_lock:
            if now - self._state_cache["timestamp"] < 1.5 and self._state_cache["data"]:
                return self._state_cache["data"]

            state = collect_system_state(self.repo_dir, self.runtime_dir)
            json_bytes = json.dumps(state, indent=2).encode("utf-8")
            self._state_cache["timestamp"] = time.time()
            self._state_cache["data"] = json_bytes
            return json_bytes

    def get_cached_task(self, task_id):
        """Returns in-memory cached task detail JSON (2.0s TTL). Total size capped <= 200KB."""
        now = time.time()
        with self._task_cache_lock:
            if task_id in self._task_cache:
                ts, data = self._task_cache[task_id]
                if now - ts < 2.0:
                    return data

        data_dict = collect_task_detail(self.runtime_dir, task_id, self.transcript_root)
        if data_dict is None:
            return None

        json_bytes = json.dumps(data_dict, indent=2, ensure_ascii=False).encode("utf-8")
        if len(json_bytes) > 200 * 1024:
            if "output_tail" in data_dict and len(data_dict["output_tail"]) > 2048:
                data_dict["output_tail"] = data_dict["output_tail"][-2048:]
            if "timeline" in data_dict and len(data_dict["timeline"]) > 30:
                data_dict["timeline"] = data_dict["timeline"][-30:]
            json_bytes = json.dumps(data_dict, indent=2, ensure_ascii=False).encode("utf-8")
        if len(json_bytes) > 200 * 1024:
            if "timeline" in data_dict and len(data_dict["timeline"]) > 10:
                data_dict["timeline"] = data_dict["timeline"][-10:]
            json_bytes = json.dumps(data_dict, separators=(',', ':'), ensure_ascii=False).encode("utf-8")

        with self._task_cache_lock:
            if len(self._task_cache) > 200:
                self._task_cache = {k: v for k, v in self._task_cache.items() if now - v[0] < 60.0}
            self._task_cache[task_id] = (time.time(), json_bytes)
        return json_bytes

    def get_cached_event(self, event_id):
        """Returns in-memory cached event detail JSON (2.0s TTL)."""
        now = time.time()
        with self._event_cache_lock:
            if event_id in self._event_cache:
                ts, data = self._event_cache[event_id]
                if now - ts < 2.0:
                    return data

        data_dict = collect_event_detail(self.runtime_dir, event_id)
        if data_dict is None:
            return None

        json_bytes = json.dumps(data_dict, indent=2, ensure_ascii=False).encode("utf-8")
        with self._event_cache_lock:
            if len(self._event_cache) > 200:
                self._event_cache = {k: v for k, v in self._event_cache.items() if now - v[0] < 60.0}
            self._event_cache[event_id] = (time.time(), json_bytes)
        return json_bytes


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        if os.environ.get("ACC_DEBUG"):
            super().log_message(format, *args)

    def send_json_response(self, status_code, body_bytes, cache_control="no-cache"):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def send_error_json(self, status_code, message):
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_json_response(status_code, body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        # Path traversal defense
        if ".." in path or "\\" in path:
            self.send_error_json(400, "Bad Request")
            return

        if path in ("/", "/index.html"):
            index_path = os.path.join(self.server.repo_dir, "dashboard", "index.html")
            if os.path.isfile(index_path):
                try:
                    with open(index_path, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                except Exception:
                    self.send_error_json(500, "Internal Server Error")
            else:
                self.send_error_json(404, "index.html Not Found")
            return

        if path == "/healthz":
            body = b'{"status": "ok"}\n'
            self.send_json_response(200, body)
            return

        if path == "/api/state":
            try:
                body = self.server.get_cached_state()
                self.send_json_response(200, body)
            except Exception as e:
                self.send_error_json(500, f"Error collecting state: {str(e)}")
            return

        if path.startswith("/api/task/"):
            task_id = path[len("/api/task/"):]
            if not task_id or "/" in task_id or "\\" in task_id or ".." in task_id or not re.fullmatch(r"^[A-Za-z0-9._-]{1,80}$", task_id):
                self.send_error_json(400, "Invalid task ID")
                return

            if not self.server.runtime_dir:
                self.send_error_json(404, "Task not found")
                return

            task_dir = os.path.join(self.server.runtime_dir, "tasks", task_id)
            if not os.path.isdir(task_dir):
                self.send_error_json(404, "Task not found")
                return

            try:
                body = self.server.get_cached_task(task_id)
                if body is None:
                    self.send_error_json(404, "Task not found")
                    return
                self.send_json_response(200, body)
            except Exception as e:
                self.send_error_json(500, f"Error getting task detail: {str(e)}")
            return

        if path.startswith("/api/event/"):
            event_id = path[len("/api/event/"):]
            if not event_id or "/" in event_id or "\\" in event_id or ".." in event_id or not re.fullmatch(r"^[A-Za-z0-9._-]{1,80}$", event_id):
                self.send_error_json(400, "Invalid event ID")
                return

            if not self.server.runtime_dir:
                self.send_error_json(404, "Event not found")
                return

            try:
                body = self.server.get_cached_event(event_id)
                if body is None:
                    self.send_error_json(404, "Event not found")
                    return
                self.send_json_response(200, body)
            except Exception as e:
                self.send_error_json(500, f"Error getting event detail: {str(e)}")
            return

        self.send_error_json(404, "Not Found")

    # Reject non-GET HTTP methods
    def do_POST(self):
        self.send_error_json(405, "Method Not Allowed")

    def do_PUT(self):
        self.send_error_json(405, "Method Not Allowed")

    def do_DELETE(self):
        self.send_error_json(405, "Method Not Allowed")

    def do_PATCH(self):
        self.send_error_json(405, "Method Not Allowed")

    def do_HEAD(self):
        self.send_error_json(405, "Method Not Allowed")

    def do_OPTIONS(self):
        self.send_error_json(405, "Method Not Allowed")


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_repo = os.path.dirname(script_dir)

    parser = argparse.ArgumentParser(description="ACC Dashboard Server")
    parser.add_argument("--host", default=None, help="Host to bind to (default: tailscale ip -4 or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to (default: 8765)")
    parser.add_argument("--runtime", default=None, help="Runtime directory")
    parser.add_argument("--repo", default=default_repo, help="Repository root directory")
    parser.add_argument("--allow-any-host", action="store_true", help="Allow binding to 0.0.0.0 or ::")
    parser.add_argument(
        "--transcript-root",
        default=os.path.expanduser("~/.gemini/antigravity-cli"),
        help="Directory for transcripts (default: ~/.gemini/antigravity-cli)"
    )

    args = parser.parse_args()

    host = args.host
    if not host:
        host = get_default_host()

    if host in ("0.0.0.0", "::", "0:0:0:0:0:0:0:0") and not args.allow_any_host:
        sys.stderr.write(f"Error: Binding to {host} requires --allow-any-host flag.\n")
        sys.exit(1)

    repo_dir = os.path.abspath(args.repo)
    runtime_dir = os.path.abspath(args.runtime) if args.runtime else get_default_runtime(repo_dir)
    transcript_root = os.path.abspath(args.transcript_root)

    sys.stdout.write(f"Starting ACC Dashboard on http://{host}:{args.port}\n")
    sys.stdout.write(f"Repo: {repo_dir}\n")
    sys.stdout.write(f"Runtime: {runtime_dir}\n")
    sys.stdout.write(f"Transcript root: {transcript_root}\n")
    sys.stdout.flush()

    server = DualStackThreadingHTTPServer(
        (host, args.port),
        DashboardHandler,
        repo_dir=repo_dir,
        runtime_dir=runtime_dir,
        transcript_root=transcript_root
    )

    def shutdown_signal(signum, frame):
        sys.stderr.write("\nShutting down dashboard server...\n")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_signal)
    signal.signal(signal.SIGTERM, shutdown_signal)

    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
