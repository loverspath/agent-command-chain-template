#!/usr/bin/env bash
# tests/lib-isolated-env.sh: 격리 테스트 환경 공통 하네스
# 모든 테스트 스크립트가 source하여 사용함
# 규칙:
#   1. 깨끗한 환경 실행 (env -i PATH="$PATH" HOME="$TEST_HOME" ...)
#   2. 고유 마커 TESTMARK-<rand> 기반 실시간 운영 런타임 오염 감지 (Plain + Base64)
#   3. 임시 런타임 경로가 LIVE_RT와 같거나 하위이면 즉시 abort
#   4. 테스트 시작/종료 시 LIVE_RT 시그니처 불변 검증 (workers, tasks, events, config)
#   5. tmux 윈도우 생성 후 idle shell 준비 대기 (wait_pane_shell)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE_RT="${LIVE_RT_OVERRIDE:-$HERE/runtime/agentchain-v2-153d25bc}"
REAL_HOME="${REAL_HOME_OVERRIDE:-$HOME}"

# 고유 랜덤 마커 생성
RAND_HEX="$(od -An -N4 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' || echo "$$$RANDOM")"
TESTMARK="TESTMARK-${RAND_HEX}"

# 안전 경로 검증 (Guard check 1)
assert_safe_runtime() {
  local target="${1:-${TEST_RUNTIME:-}}"
  if [[ -z "$target" ]]; then
    echo "[lib-isolated-env] ERROR: Target runtime path is empty!" >&2
    exit 99
  fi

  local real_live real_target
  real_live="$(realpath "$LIVE_RT" 2>/dev/null || echo "$LIVE_RT")"
  real_target="$(realpath "$target" 2>/dev/null || echo "$target")"

  if [[ "$real_target" == "$real_live" || "$real_target" == "$real_live"/* ]]; then
    echo "[lib-isolated-env] FATAL ISOLATION VIOLATION: Target runtime '$real_target' is live runtime or inside live runtime '$real_live'!" >&2
    exit 99
  fi
}

# 셸 프롬프트 준비 대기 헬퍼 (F1: tmux 생성 직후 pane_current_command=tmux 타이밍 실패 방지)
wait_pane_shell() {
  local target="$1"
  local timeout_s="${2:-10}"
  local waited=0
  local cur=""
  while (( waited < timeout_s * 10 )); do
    cur="$(tmux display-message -p -t "$target" '#{pane_current_command}' 2>/dev/null || echo "")"
    case "$cur" in
      bash|zsh|sh|-bash|-zsh|-sh)
        return 0
        ;;
    esac
    sleep 0.1
    waited=$((waited + 1))
  done
  echo "[lib-isolated-env] ERROR: wait_pane_shell timed out after ${timeout_s}s waiting for '$target' to become shell (last command: '$cur')" >&2
  return 1
}

# 운영 런타임 무오염 검증 (Guard check 2: Plain text + Base64 patterns)
assert_no_live_pollution() {
  local target_rt="${1:-$LIVE_RT}"
  if [[ ! -d "$target_rt" ]]; then
    return 0
  fi

  local py_code='
import os, sys, stat, base64, re

rt = sys.argv[1]
mark = sys.argv[2]
mark_b = mark.encode()

findings = []
for root, dirs, files in os.walk(rt):
    for f in files:
        p = os.path.join(root, f)
        try:
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                continue
            with open(p, "rb") as fp:
                data = fp.read()
            # 1. Plain text search
            if mark_b in data:
                findings.append(f"plain in {p}")
                continue
            # 2. b64 fields (_b64=)
            found_b64 = False
            for line in data.split(b"\n"):
                if b"_b64=" in line:
                    val = line.split(b"=", 1)[1].strip()
                    try:
                        dec = base64.b64decode(val)
                        if mark_b in dec:
                            findings.append(f"base64 field in {p}")
                            found_b64 = True
                            break
                    except Exception:
                        pass
            if found_b64:
                continue
            # 3. Arbitrary base64 tokens in content
            for match in re.findall(b"[A-Za-z0-9+/]{16,}={0,2}", data):
                try:
                    dec = base64.b64decode(match)
                    if mark_b in dec:
                        findings.append(f"base64 pattern in {p}")
                        break
                except Exception:
                    pass
        except Exception:
            pass

if findings:
    print("\n".join(findings))
    sys.exit(1)
sys.exit(0)
'

  local output
  if ! output="$(python3 -c "$py_code" "$target_rt" "$TESTMARK" 2>/dev/null)"; then
    echo "[lib-isolated-env] FATAL ISOLATION VIOLATION: Found marker '$TESTMARK' in live runtime '$target_rt':" >&2
    echo "$output" >&2
    return 1
  fi
  return 0
}

# 라이브 런타임 시그니처 기록 (Guard check 3: F2 시그니처 불변 검증)
record_live_signature() {
  local rt="${1:-$LIVE_RT}"
  local out_file="${2:-$TEST_TMP/live_pre_sig.json}"
  if [[ ! -d "$rt" ]]; then
    echo "{}" > "$out_file"
    return 0
  fi

  python3 -c '
import os, sys, stat, hashlib, json

rt = sys.argv[1]
out_path = sys.argv[2]

sig = {
    "workers": {},
    "tasks": {},
    "running_tasks": [],
    "events_active": [],
    "archive_count": 0,
    "config": {}
}

wdir = os.path.join(rt, "workers")
if os.path.isdir(wdir):
    for f in sorted(os.listdir(wdir)):
        p = os.path.join(wdir, f)
        if os.path.isfile(p) and not os.path.islink(p):
            with open(p, "rb") as fp:
                sig["workers"][f] = hashlib.sha256(fp.read()).hexdigest()

tdir = os.path.join(rt, "tasks")
if os.path.isdir(tdir):
    for tid in sorted(os.listdir(tdir)):
        sfile = os.path.join(tdir, tid, "state")
        if os.path.isfile(sfile) and not os.path.islink(sfile):
            with open(sfile, "rb") as fp:
                content = fp.read()
            sig["tasks"][tid] = hashlib.sha256(content).hexdigest()
            for line in content.split(b"\n"):
                if line.strip() == b"status=running":
                    sig["running_tasks"].append(tid)

for sub in ("pending", "inflight"):
    edir = os.path.join(rt, "events", sub)
    if os.path.isdir(edir):
        for ef in sorted(os.listdir(edir)):
            sig["events_active"].append(f"{sub}/{ef}")

adir = os.path.join(rt, "events", "archive")
if os.path.isdir(adir):
    sig["archive_count"] = len([f for f in os.listdir(adir) if os.path.isfile(os.path.join(adir, f))])

for cf in ("bootstrap_version", "session_name", "claude-bridge.settings.json"):
    cp = os.path.join(rt, cf)
    if os.path.isfile(cp) and not os.path.islink(cp):
        with open(cp, "rb") as fp:
            sig["config"][cf] = hashlib.sha256(fp.read()).hexdigest()
    else:
        sig["config"][cf] = "missing"

os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w", encoding="utf-8") as fp:
    json.dump(sig, fp, indent=2)
' "$rt" "$out_file"
}

# 라이브 런타임 시그니처 대조 및 단언 (F2)
verify_live_signature() {
  local rt="${1:-$LIVE_RT}"
  local in_file="${2:-$TEST_TMP/live_pre_sig.json}"
  if [[ ! -d "$rt" || ! -f "$in_file" ]]; then
    return 0
  fi

  python3 -c '
import os, sys, stat, hashlib, json

rt = sys.argv[1]
pre_path = sys.argv[2]

with open(pre_path, "r", encoding="utf-8") as fp:
    pre = json.load(fp)

if not pre:
    sys.exit(0)

post = {
    "workers": {},
    "tasks": {},
    "running_tasks": [],
    "events_active": [],
    "archive_count": 0,
    "config": {}
}

wdir = os.path.join(rt, "workers")
if os.path.isdir(wdir):
    for f in sorted(os.listdir(wdir)):
        p = os.path.join(wdir, f)
        if os.path.isfile(p) and not os.path.islink(p):
            with open(p, "rb") as fp:
                post["workers"][f] = hashlib.sha256(fp.read()).hexdigest()

tdir = os.path.join(rt, "tasks")
if os.path.isdir(tdir):
    for tid in sorted(os.listdir(tdir)):
        sfile = os.path.join(tdir, tid, "state")
        if os.path.isfile(sfile) and not os.path.islink(sfile):
            with open(sfile, "rb") as fp:
                content = fp.read()
            post["tasks"][tid] = hashlib.sha256(content).hexdigest()
            for line in content.split(b"\n"):
                if line.strip() == b"status=running":
                    post["running_tasks"].append(tid)

for sub in ("pending", "inflight"):
    edir = os.path.join(rt, "events", sub)
    if os.path.isdir(edir):
        for ef in sorted(os.listdir(edir)):
            post["events_active"].append(f"{sub}/{ef}")

adir = os.path.join(rt, "events", "archive")
if os.path.isdir(adir):
    post["archive_count"] = len([f for f in os.listdir(adir) if os.path.isfile(os.path.join(adir, f))])

for cf in ("bootstrap_version", "session_name", "claude-bridge.settings.json"):
    cp = os.path.join(rt, cf)
    if os.path.isfile(cp) and not os.path.islink(cp):
        with open(cp, "rb") as fp:
            post["config"][cf] = hashlib.sha256(fp.read()).hexdigest()
    else:
        post["config"][cf] = "missing"

diffs = []
if pre["config"] != post["config"]:
    diffs.append("Config files altered: pre={} post={}".format(pre['config'], post['config']))

pre_running = set(pre["running_tasks"])
for tid, phash in pre["tasks"].items():
    if tid not in post["tasks"]:
        diffs.append(f"Task state missing: {tid}")
    elif post["tasks"][tid] != phash and tid not in pre_running:
        diffs.append(f"Non-running task state altered: {tid}")
for tid in post["tasks"]:
    if tid not in pre["tasks"]:
        diffs.append(f"Unexpected new task created: {tid}")

if pre["workers"] != post["workers"]:
    diffs.append("Workers directory altered: pre={} post={}".format(list(pre['workers'].keys()), list(post['workers'].keys())))

events_diff = (pre["events_active"] != post["events_active"]) or (pre["archive_count"] != post["archive_count"])
running_task_changed = any(post["tasks"].get(tid) != pre["tasks"].get(tid) for tid in pre_running)

if diffs:
    print("[lib-isolated-env] FATAL ISOLATION VIOLATION: " + "; ".join(diffs), file=sys.stderr)
    sys.exit(1)

if (events_diff or running_task_changed):
    if pre_running:
        print("[lib-isolated-env] 경고: 라이브 task 진행 중 — 시그니처 비교 생략(경고 출력)", file=sys.stderr)
        sys.exit(0)
    else:
        print("[lib-isolated-env] FATAL ISOLATION VIOLATION: Events mutated without running tasks (pre_evts={}, post_evts={})".format(pre['events_active'], post['events_active']), file=sys.stderr)
        sys.exit(1)

sys.exit(0)
' "$rt" "$in_file"
}

# 리포 상태 스냅샷 (H1)
snapshot_repo_status() {
  local repo="${1:-${HERE_OVERRIDE:-$HERE}}"
  local out_file="${2:-$TEST_TMP/repo_pre_status.txt}"
  mkdir -p "$(dirname "$out_file")"
  if git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$repo" status --porcelain --ignored=no > "$out_file"
  else
    : > "$out_file"
  fi
}

# 리포 무오염 검증 (H1)
assert_no_repo_pollution() {
  local repo="${1:-${HERE_OVERRIDE:-$HERE}}"
  local in_file="${2:-$TEST_TMP/repo_pre_status.txt}"

  if [[ ! -f "$in_file" ]]; then
    return 0
  fi
  if ! git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return 0
  fi

  local py_code='
import os, sys, subprocess

repo = sys.argv[1]
pre_file = sys.argv[2]

with open(pre_file, "r", encoding="utf-8") as fp:
    pre_lines = set(fp.read().splitlines())

res = subprocess.run(
    ["git", "-C", repo, "status", "--porcelain", "--ignored=no"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False
)
if res.returncode != 0:
    print(f"[lib-isolated-env] ERROR: git status failed in {repo}: {res.stderr.decode()}", file=sys.stderr)
    sys.exit(1)

post_lines = res.stdout.decode("utf-8", errors="replace").splitlines()
new_lines = [line for line in post_lines if line not in pre_lines]

if new_lines:
    print(f"[lib-isolated-env] FATAL ISOLATION VIOLATION: Repository \"{repo}\" was polluted with unexpected files/changes:", file=sys.stderr)
    for line in new_lines:
        print(f"  {line}", file=sys.stderr)
    sys.exit(1)

sys.exit(0)
'
  python3 -c "$py_code" "$repo" "$in_file"
}

# config.env 스냅샷 (H1-config: SHA256 앞 12자 기록, 내용 절대 출력/저장 금지)
snapshot_config_env() {
  local repo="${1:-${HERE_OVERRIDE:-$HERE}}"
  local out_file="${2:-$TEST_TMP/config_env_pre.txt}"
  mkdir -p "$(dirname "$out_file")"
  if [[ -f "$repo/config.env" ]]; then
    python3 -c '
import sys, hashlib
try:
    with open(sys.argv[1], "rb") as f:
        print(hashlib.sha256(f.read()).hexdigest()[:12])
except Exception:
    sys.exit(1)
' "$repo/config.env" > "$out_file"
  else
    rm -f "$out_file" 2>/dev/null || true
  fi
}

# config.env 무오염 검증 (H1-config: SHA256 앞 12자 대조, 변경/삭제 시 FATAL 거부, 내용 절대 미출력)
verify_config_env() {
  local repo="${1:-${HERE_OVERRIDE:-$HERE}}"
  local in_file="${2:-$TEST_TMP/config_env_pre.txt}"

  if [[ ! -f "$in_file" ]]; then
    if [[ -f "$repo/config.env" ]]; then
      local post_sha
      post_sha="$(python3 -c 'import sys, hashlib; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()[:12])' "$repo/config.env" 2>/dev/null || echo "unknown")"
      echo "[lib-isolated-env] FATAL ISOLATION VIOLATION: '$repo/config.env' was unexpectedly created during test! (pre=missing, post_sha12=$post_sha)" >&2
      return 1
    fi
    return 0
  fi

  local pre_sha
  pre_sha="$(tr -d ' \r\n' < "$in_file" 2>/dev/null || echo "")"
  if [[ -z "$pre_sha" ]]; then
    return 0
  fi

  if [[ ! -f "$repo/config.env" ]]; then
    echo "[lib-isolated-env] FATAL ISOLATION VIOLATION: '$repo/config.env' was removed during test! (pre_sha12=$pre_sha, post=missing)" >&2
    return 1
  fi

  local post_sha
  post_sha="$(python3 -c 'import sys, hashlib; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()[:12])' "$repo/config.env" 2>/dev/null || echo "unknown")"

  if [[ "$pre_sha" != "$post_sha" ]]; then
    echo "[lib-isolated-env] FATAL ISOLATION VIOLATION: '$repo/config.env' was modified during test! (pre_sha12=$pre_sha, post_sha12=$post_sha)" >&2
    return 1
  fi

  return 0
}

# 임시 트리를 점유한 프로세스 대기 및 종료 회수 (H2)
terminate_lingering_processes() {
  local target_dir="$1"
  local timeout_s="${2:-10}"

  if [[ ! -d "$target_dir" ]]; then
    return 0
  fi

  python3 -c '
import os, sys, time, signal

target_dir = os.path.realpath(sys.argv[1])
timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
exclude_pids = {os.getpid(), os.getppid()}

def get_matching_pids():
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in exclude_pids:
            continue
        pdir = os.path.join("/proc", entry)
        try:
            cwd = os.path.realpath(os.path.join(pdir, "cwd"))
            if cwd == target_dir or cwd.startswith(target_dir + "/"):
                pids.append(pid)
                continue
        except Exception:
            pass
        try:
            with open(os.path.join(pdir, "environ"), "rb") as fp:
                data = fp.read()
            for item in data.split(b"\0"):
                if item.startswith(b"HOME=") or item.startswith(b"ACC_RUNTIME="):
                    val = item.split(b"=", 1)[1].decode("utf-8", errors="replace")
                    val_r = os.path.realpath(val)
                    if val_r == target_dir or val_r.startswith(target_dir + "/"):
                        pids.append(pid)
                        break
        except Exception:
            pass
    return pids

start = time.time()
while time.time() - start < timeout:
    matching = get_matching_pids()
    if not matching:
        sys.exit(0)
    time.sleep(0.2)

matching = get_matching_pids()
if matching:
    for pid in matching:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(1.0)

matching = get_matching_pids()
if matching:
    for pid in matching:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(0.5)

matching = get_matching_pids()
if matching:
    print(f"[lib-isolated-env] WARNING: Lingering processes refused to terminate: {matching}", file=sys.stderr)
    sys.exit(1)

sys.exit(0)
' "$target_dir" "$timeout_s"
}

# 프로세스 정리 및 디렉토리 완전 삭제 헬퍼 (H2)
cleanup_process_and_tree() {
  local target_dir="$1"
  local session="${2:-}"
  local timeout_s="${3:-10}"

  if [[ -n "$session" ]]; then
    tmux kill-session -t "$session" 2>/dev/null || true
  fi

  if [[ -d "$target_dir" ]]; then
    terminate_lingering_processes "$target_dir" "$timeout_s" || true
    rm -rf "$target_dir" 2>/dev/null || true
    if [[ -e "$target_dir" ]]; then
      sleep 0.5
      rm -rf "$target_dir" 2>/dev/null || true
    fi
  fi

  if [[ -e "$target_dir" ]]; then
    echo "[lib-isolated-env] ERROR: Failed to completely remove '$target_dir'!" >&2
    return 1
  fi
  return 0
}

# 실제 Gemini 설정 스냅샷 (H3)
snapshot_real_gemini_config() {
  local home_dir="${1:-${REAL_HOME_OVERRIDE:-${REAL_HOME:-$HOME}}}"
  local out_file="${2:-$TEST_TMP/real_gemini_pre_sig.json}"
  mkdir -p "$(dirname "$out_file")"

  python3 -c '
import os, sys, hashlib, json

home_dir = sys.argv[1]
out_file = sys.argv[2]

data = {
    "config": {},
    "cli_settings": {},
    "token": {
        "exists": False,
        "is_link": False,
        "link_target": None
    }
}
gemini_dir = os.path.join(home_dir, ".gemini")
cfg_dir = os.path.join(gemini_dir, "config")
cli_dir = os.path.join(gemini_dir, "antigravity-cli")

if os.path.isdir(cfg_dir):
    for root, dirs, files in os.walk(cfg_dir):
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, cfg_dir)
            try:
                with open(p, "rb") as fp:
                    content = fp.read()
                data["config"][rel] = {
                    "size": len(content),
                    "hash": hashlib.sha256(content).hexdigest()
                }
            except Exception:
                pass

token_path = os.path.join(cli_dir, "antigravity-oauth-token")
if os.path.lexists(token_path):
    data["token"]["exists"] = True
    data["token"]["is_link"] = os.path.islink(token_path)
    if data["token"]["is_link"]:
        data["token"]["link_target"] = os.readlink(token_path)

excluded_dirs = {"brain", "conversations", "cache", "log", "presence", "annotations", "crashes", "scratch"}
excluded_exts = {".db", ".db-shm", ".db-wal", ".log", ".pb"}
excluded_files = {"history.jsonl", "last_check.timestamp", "antigravity-oauth-token"}

if os.path.isdir(cli_dir):
    for root, dirs, files in os.walk(cli_dir):
        rel_root = os.path.relpath(root, cli_dir)
        parts = rel_root.split(os.sep) if rel_root != "." else []
        if parts and parts[0] in excluded_dirs:
            continue
        for f in files:
            if f in excluded_files:
                continue
            if any(f.endswith(ext) for ext in excluded_exts):
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, cli_dir)
            try:
                with open(p, "rb") as fp:
                    content = fp.read()
                data["cli_settings"][rel] = {
                    "size": len(content),
                    "hash": hashlib.sha256(content).hexdigest()
                }
            except Exception:
                pass

with open(out_file, "w", encoding="utf-8") as fp:
    json.dump(data, fp, indent=2)
' "$home_dir" "$out_file"
}

# 실제 Gemini 설정 무오염 검증 (H3)
verify_real_gemini_config() {
  local home_dir="${1:-${REAL_HOME_OVERRIDE:-${REAL_HOME:-$HOME}}}"
  local in_file="${2:-$TEST_TMP/real_gemini_pre_sig.json}"

  if [[ ! -f "$in_file" ]]; then
    return 0
  fi

  python3 -c '
import os, sys, hashlib, json

home_dir = sys.argv[1]
in_file = sys.argv[2]

with open(in_file, "r", encoding="utf-8") as fp:
    pre = json.load(fp)

post = {
    "config": {},
    "cli_settings": {},
    "token": {
        "exists": False,
        "is_link": False,
        "link_target": None
    }
}
gemini_dir = os.path.join(home_dir, ".gemini")
cfg_dir = os.path.join(gemini_dir, "config")
cli_dir = os.path.join(gemini_dir, "antigravity-cli")

if os.path.isdir(cfg_dir):
    for root, dirs, files in os.walk(cfg_dir):
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, cfg_dir)
            try:
                with open(p, "rb") as fp:
                    content = fp.read()
                post["config"][rel] = {
                    "size": len(content),
                    "hash": hashlib.sha256(content).hexdigest()
                }
            except Exception:
                pass

token_path = os.path.join(cli_dir, "antigravity-oauth-token")
if os.path.lexists(token_path):
    post["token"]["exists"] = True
    post["token"]["is_link"] = os.path.islink(token_path)
    if post["token"]["is_link"]:
        post["token"]["link_target"] = os.readlink(token_path)

excluded_dirs = {"brain", "conversations", "cache", "log", "presence", "annotations", "crashes", "scratch"}
excluded_exts = {".db", ".db-shm", ".db-wal", ".log", ".pb"}
excluded_files = {"history.jsonl", "last_check.timestamp", "antigravity-oauth-token"}

if os.path.isdir(cli_dir):
    for root, dirs, files in os.walk(cli_dir):
        rel_root = os.path.relpath(root, cli_dir)
        parts = rel_root.split(os.sep) if rel_root != "." else []
        if parts and parts[0] in excluded_dirs:
            continue
        for f in files:
            if f in excluded_files:
                continue
            if any(f.endswith(ext) for ext in excluded_exts):
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, cli_dir)
            try:
                with open(p, "rb") as fp:
                    content = fp.read()
                post["cli_settings"][rel] = {
                    "size": len(content),
                    "hash": hashlib.sha256(content).hexdigest()
                }
            except Exception:
                pass

diffs = []
for f, pinfo in pre["config"].items():
    if f not in post["config"]:
        diffs.append(f"Config file removed: {f}")
    elif post["config"][f] != pinfo:
        diffs.append("Config file modified: {} (pre={}, post={})".format(f, pinfo, post["config"][f]))
for f in post["config"]:
    if f not in pre["config"]:
        diffs.append(f"New config file created: {f}")

for f, pinfo in pre["cli_settings"].items():
    if f not in post["cli_settings"]:
        diffs.append(f"CLI setting removed: {f}")
    elif post["cli_settings"][f] != pinfo:
        diffs.append("CLI setting modified: {} (pre={}, post={})".format(f, pinfo, post["cli_settings"][f]))
for f in post["cli_settings"]:
    if f not in pre["cli_settings"]:
        diffs.append(f"New CLI setting created: {f}")

if pre["token"] != post["token"]:
    diffs.append("Token metadata changed: pre={}, post={}".format(pre["token"], post["token"]))

if diffs:
    print("[lib-isolated-env] FATAL ISOLATION VIOLATION: Real Gemini configuration was altered!", file=sys.stderr)
    for d in diffs:
        print(f"  - {d}", file=sys.stderr)
    sys.exit(1)

cfg_count = len(post["config"])
cli_count = len(post["cli_settings"])
tok_st = "exists=True" if post["token"]["exists"] else "exists=False"
print(f"[lib-isolated-env] Real Gemini config verified: UNCHANGED ({cfg_count} config files, {cli_count} CLI settings, token {tok_st})")
sys.exit(0)
' "$home_dir" "$in_file"
}

# 실제 agy 격리 실행 래퍼 (H3)
run_real_agy_isolated() {
  local target_home="${TEST_HOME:-}"
  if [[ $# -gt 0 ]]; then
    case "$1" in
      --*)
        ;;
      *)
        target_home="$1"
        shift
        ;;
    esac
  fi

  if [[ -z "$target_home" ]]; then
    echo "[lib-isolated-env] FATAL: run_real_agy_isolated called without target HOME!" >&2
    exit 99
  fi

  mkdir -p "$target_home"

  local base_real_home="${REAL_HOME_OVERRIDE:-${REAL_HOME:-$HOME}}"
  local real_home real_target
  real_home="$(realpath "$base_real_home" 2>/dev/null || echo "$base_real_home")"
  real_target="$(realpath "$target_home" 2>/dev/null || echo "$target_home")"

  if [[ "$real_target" == "$real_home" || "$real_target" == "$real_home"/* ]]; then
    echo "[lib-isolated-env] FATAL ISOLATION VIOLATION: Target HOME '$real_target' is real home or inside real home '$real_home'!" >&2
    exit 99
  fi

  local allowed_prefix="${TEST_TMP_BASE:-/tmp}"
  if [[ "$real_target" != "$allowed_prefix"/* && "$real_target" != "${TEST_TMP:-/nonexistent}"* ]]; then
    echo "[lib-isolated-env] FATAL ISOLATION VIOLATION: Target HOME '$real_target' is not under temporary path '$allowed_prefix'!" >&2
    exit 99
  fi

  # Symlink auth token (Symlink ONLY, NO copy, NO content printing)
  local real_token="$real_home/.gemini/antigravity-cli/antigravity-oauth-token"
  if [[ -f "$real_token" || -L "$real_token" ]]; then
    mkdir -p "$real_target/.gemini/antigravity-cli"
    local dest_token="$real_target/.gemini/antigravity-cli/antigravity-oauth-token"
    rm -f "$dest_token" 2>/dev/null || true
    ln -s "$real_token" "$dest_token"
  fi

  # Ensure basic config (onboarding bypass & settings)
  mkdir -p "$real_target/.gemini/antigravity-cli/cache" "$real_target/.gemini/config"
  if [[ ! -f "$real_target/.gemini/antigravity-cli/cache/onboarding.json" ]]; then
    cat > "$real_target/.gemini/antigravity-cli/cache/onboarding.json" <<'EOF'
{
  "consumerOnboardingComplete": true,
  "enterpriseOnboardingComplete": false,
  "onboardingComplete": true
}
EOF
  fi
  if [[ ! -f "$real_target/.gemini/antigravity-cli/settings.json" ]]; then
    cat > "$real_target/.gemini/antigravity-cli/settings.json" <<'EOF'
{
  "agentMode": "accept-edits",
  "colorScheme": "terminal",
  "toolPermission": "always-proceed"
}
EOF
  fi

  if [[ $# -gt 0 ]]; then
    if [[ "$1" == "--" ]]; then
      shift
    fi
    env -i \
      PATH="$PATH" \
      HOME="$real_target" \
      TESTMARK="${TESTMARK:-}" \
      "$@"
  fi
}

# 격리 환경 초기화
init_isolated_env() {
  local suite_name="${1:-test}"
  TEST_TMP="$(mktemp -d "/tmp/acc-t0924-11-${suite_name}-${RAND_HEX}-XXXXXX")"
  TEST_HOME="$TEST_TMP/home"
  mkdir -p "$TEST_HOME"
  TEST_RUNTIME="$TEST_TMP/runtime"
  TEST_SESSION="acc-t0924-11-${RAND_HEX}"

  assert_safe_runtime "$TEST_RUNTIME"

  # 시작 전 LIVE_RT 내 마커 확인
  if ! assert_no_live_pollution; then
    echo "[lib-isolated-env] Pre-test check failed: Live runtime already contains $TESTMARK!" >&2
    exit 99
  fi

  # 시작 전 LIVE_RT 시그니처 기록
  record_live_signature "$LIVE_RT" "$TEST_TMP/live_pre_sig.json"

  # 시작 전 리포 상태 기록 (H1)
  snapshot_repo_status "$HERE" "$TEST_TMP/repo_pre_status.txt"

  # 시작 전 실제 config.env 스냅샷 (H1-config: SHA256 앞 12자 기록)
  snapshot_config_env "$HERE" "$TEST_TMP/config_env_pre.txt"

  # 임시 격리 config.env 생성 (모든 테스트가 실제 config.env 대신 사용)
  TEST_CONFIG_ENV="$TEST_TMP/config.env"
  if [[ -f "$HERE/config.env.example" ]]; then
    cp "$HERE/config.env.example" "$TEST_CONFIG_ENV"
  else
    printf 'SESSION_NAME=agentchain\nWORKER_MODE=oneshot\nBRIDGE_MODE=push\n' > "$TEST_CONFIG_ENV"
  fi
  export ACC_CONFIG_ENV="$TEST_CONFIG_ENV"

  # 시작 전 실제 Gemini 설정 스냅샷 (H3)
  snapshot_real_gemini_config "$REAL_HOME" "$TEST_TMP/real_gemini_pre_sig.json"

  # trap 등록 (종료 시 자동 정리 및 오염 검사)
  trap 'cleanup_isolated_env' EXIT INT TERM
}

# 격리 환경 정리 (H2 수정 순서 준수)
cleanup_isolated_env() {
  local exit_code=$?
  trap - EXIT INT TERM

  # (1) 임시 tmux 세션 종료
  if [[ -n "${TEST_SESSION:-}" ]]; then
    tmux kill-session -t "$TEST_SESSION" 2>/dev/null || true
  fi

  # (2) 임시 트리를 HOME/cwd/ACC_RUNTIME 로 가진 프로세스 종료 대기 및 회수 (SIGTERM -> SIGKILL)
  if [[ -n "${TEST_TMP:-}" && -d "$TEST_TMP" ]]; then
    terminate_lingering_processes "$TEST_TMP" 10 || true
  fi

  # (3) 종료 후 LIVE_RT 시그니처 검증 (불변 단언)
  if [[ -f "${TEST_TMP:-}/live_pre_sig.json" ]]; then
    if ! verify_live_signature "$LIVE_RT" "$TEST_TMP/live_pre_sig.json"; then
      echo "[lib-isolated-env] Post-test check FAILED: Live runtime signature was altered!" >&2
      exit_code=99
    fi
  fi

  # 종료 후 LIVE_RT 내 마커 검증 (0건 단언, plain & base64)
  if ! assert_no_live_pollution; then
    echo "[lib-isolated-env] Post-test check FAILED: Live runtime was polluted with marker $TESTMARK!" >&2
    exit_code=99
  fi

  # 종료 후 리포 무오염 검증 (H1)
  if [[ -f "${TEST_TMP:-}/repo_pre_status.txt" ]]; then
    if ! assert_no_repo_pollution "$HERE" "$TEST_TMP/repo_pre_status.txt"; then
      echo "[lib-isolated-env] Post-test check FAILED: Repository was polluted during test!" >&2
      exit_code=99
    fi
  fi

  # 종료 후 실제 config.env 무오염 검증 (H1-config: SHA256 앞 12자 불변 단언)
  if [[ -f "${TEST_TMP:-}/config_env_pre.txt" ]]; then
    if ! verify_config_env "$HERE" "$TEST_TMP/config_env_pre.txt"; then
      echo "[lib-isolated-env] Post-test check FAILED: config.env was altered or removed during test!" >&2
      exit_code=99
    fi
  fi

  # 종료 후 실제 Gemini 설정 무오염 검증 (H3)
  if [[ -f "${TEST_TMP:-}/real_gemini_pre_sig.json" ]]; then
    if ! verify_real_gemini_config "$REAL_HOME" "$TEST_TMP/real_gemini_pre_sig.json"; then
      echo "[lib-isolated-env] Post-test check FAILED: Real Gemini config was altered during test!" >&2
      exit_code=99
    fi
  fi

  # (4) 임시 디렉토리 완전 삭제 및 부재 확인 (H2)
  if [[ -n "${TEST_TMP:-}" && -e "$TEST_TMP" ]]; then
    rm -rf "$TEST_TMP" 2>/dev/null || true
    if [[ -e "$TEST_TMP" ]]; then
      sleep 0.5
      rm -rf "$TEST_TMP" 2>/dev/null || true
      if [[ -e "$TEST_TMP" ]]; then
        echo "[lib-isolated-env] Post-test check FAILED: Temporary directory '$TEST_TMP' still exists after cleanup!" >&2
        exit_code=99
      fi
    fi
  fi

  exit "$exit_code"
}

# 깨끗한 환경 명령 실행 래퍼 (env -i)
# 사용법: run_clean VAR1=VAL1 VAR2=VAL2 ... -- command [args...]
run_clean() {
  local env_vars=()
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--" ]]; then
      shift
      break
    fi
    env_vars+=("$1")
    shift
  done

  env -i \
    PATH="$PATH" \
    HOME="$TEST_HOME" \
    TESTMARK="$TESTMARK" \
    ACC_CONFIG_ENV="${ACC_CONFIG_ENV:-${TEST_CONFIG_ENV:-$TEST_TMP/config.env}}" \
    "${env_vars[@]}" \
    "$@"
}

# 임시 런타임 디렉토리 구조 생성
setup_mock_runtime() {
  local rt="${1:-$TEST_RUNTIME}"
  assert_safe_runtime "$rt"
  mkdir -p "$rt"/{events/{pending,inflight,archive,quarantine},tasks,workers}
  [[ -p "$rt/event.fifo" ]] || mkfifo -m 600 "$rt/event.fifo"
  printf 'bootstrap_version=2\ncreated_epoch=%s\nworker_mode=resident\nagy_mode=resident\ncodex_mode=oneshot\nbridge_mode=push\n' "$(date +%s)" > "$rt/bootstrap_version"
  printf '%s\n' "$TEST_SESSION" > "$rt/session_name"
}

# stub agy ELF 바이너리 빌드 헬퍼 (tmux pane_current_command=agy 인식용)
build_stub_agy() {
  local target_dir="${1:-$TEST_TMP/bin}"
  mkdir -p "$target_dir"
  local c_src="$target_dir/stub_agy.c"
  cat > "$c_src" <<'EOF'
#include <stdio.h>
#include <unistd.h>
#include <signal.h>
#include <stdlib.h>

void handle_term(int sig) { _exit(0); }

int main() {
    signal(SIGTERM, handle_term);
    signal(SIGINT, handle_term);
    printf(">\n");
    fflush(stdout);
    while (1) { sleep(60); }
    return 0;
}
EOF
  gcc -O2 "$c_src" -o "$target_dir/agy"
  echo "$target_dir/agy"
}
