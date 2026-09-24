#!/usr/bin/env bash
set -euo pipefail
umask 077

worker="${1:-}"
if [[ -z "$worker" ]]; then
  echo "Usage: $0 <agy|codex> [--timeout <seconds>] [--prompt-file <path> | --prompt <text>] [--dry-run]" >&2
  exit 64
fi
shift

case "$worker" in
  agy|codex) ;;
  *) echo "Unsupported worker: $worker (must be agy or codex)" >&2; exit 64 ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ENV_SESSION_NAME="${SESSION_NAME:-}"
ENV_AGY_WINDOW="${AGY_WINDOW:-}"
ENV_CODEX_WINDOW="${CODEX_WINDOW:-}"
ENV_ACC_RUNTIME="${ACC_RUNTIME:-}"

if [[ -f "$HERE/config.env" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/config.env"
fi
CONFIG_SESSION_NAME="${SESSION_NAME:-agentchain}"

# shellcheck disable=SC1091
source "$HERE/lib/session.sh"

AGY_WINDOW="${ENV_AGY_WINDOW:-${AGY_WINDOW:-agy}}"
CODEX_WINDOW="${ENV_CODEX_WINDOW:-${CODEX_WINDOW:-codex}}"

timeout="${DEFAULT_TASK_TIMEOUT:-1800}"
prompt_file=""
prompt_inline=""
dry_run=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      dry_run=true
      shift
      ;;
    --timeout)
      timeout="$2"
      shift 2
      ;;
    --prompt-file)
      prompt_file="$2"
      shift 2
      ;;
    --prompt)
      prompt_inline="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

# Timeout 숫자 및 범위 검증
case "$timeout" in *[!0-9]*|"") echo "Error: --timeout must be a positive integer" >&2; exit 64 ;; esac
if (( timeout < 1 || timeout > 86400 )); then
  echo "Error: --timeout must be between 1 and 86400 seconds" >&2
  exit 64
fi

# 상호 배타성 및 prompt 존재 사전 검증 (락 획득 전 수행)
if [[ -n "$prompt_file" && -n "$prompt_inline" ]]; then
  echo "Error: Cannot specify both --prompt-file and --prompt" >&2
  exit 64
fi
if [[ -n "$prompt_file" && ! -f "$prompt_file" ]]; then
  echo "Error: Prompt file not found: $prompt_file" >&2
  exit 66
fi
if [[ "$dry_run" == "false" && -z "$prompt_file" && -z "$prompt_inline" && -t 0 ]]; then
  echo "Error: No prompt provided (--prompt-file, --prompt, or stdin)" >&2
  exit 64
fi

target_window="$worker"
if [[ "$worker" == "agy" ]]; then
  target_window="${AGY_WINDOW:-agy}"
elif [[ "$worker" == "codex" ]]; then
  target_window="${CODEX_WINDOW:-codex}"
fi

# 세션명 및 런타임 디렉토리 자동 해석 (우선순위: ①환경변수 -> ②런타임마커 -> ③현재tmux -> ④config기본값)
resolve_session_and_runtime "dispatch" "$target_window" false
runtime="$ACC_RUNTIME"

# 세션 및 대상 윈도우 존재 확인
if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "Error: tmux session '$SESSION_NAME' does not exist." >&2
  exit 69
fi

if ! tmux list-windows -t "$SESSION_NAME" -F '#{window_name}' 2>/dev/null | grep -qx "$target_window"; then
  echo "Error: Target window '$target_window' not found in session '$SESSION_NAME'." >&2
  exit 69
fi

# v2 Full-Push 런타임 호환성 fail-closed 검증
if [[ ! -f "$runtime/bootstrap_version" ]] || ! grep -q '^bootstrap_version=2' "$runtime/bootstrap_version"; then
  echo "Error: Session '$SESSION_NAME' is not initialized with v2 Full-Push architecture (missing v2 bootstrap marker). Run bootstrap-v2.sh first." >&2
  exit 70
fi

# 대상 pane의 현재 프로세스가 idle shell인지 검증 (v1 상시 TUI 오염 원천 차단)
cur_cmd="$(tmux display-message -p -t "$SESSION_NAME:$target_window" '#{pane_current_command}' 2>/dev/null || echo "")"
case "$cur_cmd" in
  bash|zsh|sh|-bash|-zsh|-sh) ;;
  *)
    echo "Error: Target window '$target_window' in session '$SESSION_NAME' is running interactive TUI '$cur_cmd' instead of idle shell. Dispatch rejected." >&2
    echo "Hint: If this is a v1 session, re-run with SESSION_NAME=<v2-session> (e.g. SESSION_NAME=agentchain-v2) to target the v2 idle worker shell." >&2
    exit 71
    ;;
esac

# 드라이런 요청 시 실제 작업 디스패치 없이 성공 종료
if [[ "$dry_run" == "true" ]]; then
  echo "[dry-run] Target window '$target_window' in session '$SESSION_NAME' is ready (current command: '$cur_cmd')."
  echo "[dry-run] Task would be dispatched to $worker with timeout ${timeout}s."
  exit 0
fi

# 워커 단일 활성 작업 원자적 락 획득 (이중 dispatch 방지)
mkdir -p "$runtime/workers"
lock_dir="$runtime/workers/$worker.lock"
busy_file="$runtime/workers/$worker.busy"

my_proc_token="$(awk '{print $22}' /proc/$$/stat 2>/dev/null || echo "$$")"
owner_token="$$:${my_proc_token}:$(date +%s%N):$RANDOM"
lease_acquired=false

cleanup_lease() {
  local cur_owner cur_pid
  cur_owner="$(cat "$lock_dir/owner" 2>/dev/null || true)"
  cur_pid="$(cat "$lock_dir/pid" 2>/dev/null || true)"
  if [[ -n "$owner_token" && "$cur_owner" == "$owner_token" ]] || [[ "$lease_acquired" == "true" && -z "$cur_owner" && ( -z "$cur_pid" || "$cur_pid" == "$$" ) ]]; then
    rm -rf "$lock_dir" 2>/dev/null || true
  fi
}
trap cleanup_lease EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

acquire_worker_lease() {
  local retries=0
  local max_retries=40
  while ! mkdir "$lock_dir" 2>/dev/null; do
    local lock_pid lock_ts lock_proc_token dir_mtime now
    now="$(date +%s)"
    lock_pid="$(cat "$lock_dir/pid" 2>/dev/null || echo "")"
    lock_ts="$(cat "$lock_dir/ts" 2>/dev/null || echo "")"
    lock_proc_token="$(cat "$lock_dir/proc_token" 2>/dev/null || echo "")"
    dir_mtime="$(stat -c %Y "$lock_dir" 2>/dev/null || stat -f %m "$lock_dir" 2>/dev/null || echo 0)"

    if [[ -n "$lock_pid" ]]; then
      if kill -0 "$lock_pid" 2>/dev/null; then
        local cur_proc_token
        cur_proc_token="$(awk '{print $22}' "/proc/$lock_pid/stat" 2>/dev/null || echo "")"
        if [[ -n "$lock_proc_token" && -n "$cur_proc_token" && "$cur_proc_token" != "$lock_proc_token" ]]; then
          # PID 재사용 감지 -> 원래 소유자 사망으로 판정하여 stale 회수
          rm -rf "$lock_dir" 2>/dev/null || true
        fi
        # PID가 살아 있고 proc_token이 일치하면 작업 시간과 무관하게 회수 금지
      else
        # PID 프로세스가 이미 사망함 -> stale 회수
        rm -rf "$lock_dir" 2>/dev/null || true
      fi
    else
      # PID가 없거나 metadata가 비어있는 경우 (3초 이상 방치 시 stale 회수)
      if (( now - dir_mtime >= 3 )); then
        rm -rf "$lock_dir" 2>/dev/null || true
      fi
    fi

    retries=$(( retries + 1 ))
    if (( retries > max_retries )); then
      echo "Error: Worker '$worker' lock acquisition conflict. Dispatch rejected." >&2
      exit 75
    fi
    sleep 0.05
  done

  lease_acquired=true
  printf '%s\n' "$owner_token" > "$lock_dir/owner"
  printf '%s\n' "$$" > "$lock_dir/pid"
  printf '%s\n' "$my_proc_token" > "$lock_dir/proc_token"
  printf '%s\n' "$(date +%s)" > "$lock_dir/ts"

  # busy 파일 검사
  if [[ -f "$busy_file" ]]; then
    local active_task active_ts
    active_task="$(sed -n '1p' "$busy_file" || true)"
    active_ts="$(sed -n '2p' "$busy_file" || true)"
    local now
    now="$(date +%s)"
    local state_file="$runtime/tasks/$active_task/state"

    if [[ -f "$state_file" ]] && grep -q '^status=running' "$state_file"; then
      echo "Error: Worker '$worker' is currently busy executing task '$active_task'. Dispatch rejected." >&2
      exit 75
    fi

    if [[ -n "$active_ts" ]] && (( now - active_ts < 30 )); then
      echo "Error: Worker '$worker' is currently launching task '$active_task' (lease active). Dispatch rejected." >&2
      exit 75
    fi

    rm -f "$busy_file"
  fi
}

acquire_worker_lease

task_id="${worker}-$(date +%s%N)"
task_dir="$runtime/tasks/$task_id"
mkdir -p "$task_dir"

brief_header=""
if [[ "${ACC_NO_BRIEF_HEADER:-0}" != "1" ]]; then
  target_project_dir="${PROJECT_DIR:-$PWD}"
  brief_header="[CHAIN CONTEXT]
TEMPLATE_ROOT: $HERE
CHAIN_WIKI_INDEX: $HERE/.llmwiki/INDEX.md
ROLES: $HERE/ROLES.md
PROJECT_DIR: $target_project_dir
SESSION_NAME: $SESSION_NAME
TASK_ID: $task_id
WORKER_RULES:
- agy: oneshot router mode. Do not directly execute with Read/Edit/Bash. Delegate using invoke_subagent. If delegation fails, report [[BLOCKED <id>]].
- codex: Project convention files (e.g. AGENTS.md) take precedence over chain rules for coding conventions.
---
"
fi

if [[ -n "$brief_header" ]]; then
  printf '%s\n' "$brief_header" >"$task_dir/prompt.md"
  if [[ -n "$prompt_file" ]]; then
    cat "$prompt_file" >>"$task_dir/prompt.md"
  elif [[ -n "$prompt_inline" ]]; then
    printf '%s\n' "$prompt_inline" >>"$task_dir/prompt.md"
  elif [[ ! -t 0 ]]; then
    cat >>"$task_dir/prompt.md"
  fi
else
  if [[ -n "$prompt_file" ]]; then
    cp "$prompt_file" "$task_dir/prompt.md"
  elif [[ -n "$prompt_inline" ]]; then
    printf '%s\n' "$prompt_inline" >"$task_dir/prompt.md"
  elif [[ ! -t 0 ]]; then
    cat >"$task_dir/prompt.md"
  fi
fi

# ARG_MAX 및 CLI 입력 방어를 위한 프롬프트 크기 사전 검증 (헤더 포함 128KB 제한)
prompt_size="$(wc -c < "$task_dir/prompt.md")"
if (( prompt_size > 131072 )); then
  echo "Error: Prompt file exceeds maximum allowed size (128KB)" >&2
  rm -rf "$task_dir"
  exit 65
fi

# 작업 예약 마커 설정 및 락 해제
printf '%s\n%s\n' "$task_id" "$(date +%s)" > "$busy_file"
lease_acquired=false
if [[ "$(cat "$lock_dir/owner" 2>/dev/null || true)" == "$owner_token" ]]; then
  rm -rf "$lock_dir" 2>/dev/null || true
fi

# 안전하게 인자 이스케이프 후 literal 모드로 명령 주입
run_cmd=$(printf '%q %q %q %q %q' "$HERE/bin/run-task.sh" "$worker" "$task_id" "$task_dir/prompt.md" "$timeout")
if ! tmux send-keys -l -t "$SESSION_NAME:$target_window" "$run_cmd" || ! tmux send-keys -t "$SESSION_NAME:$target_window" C-m; then
  echo "Error: Failed to inject command into tmux window '$target_window'." >&2
  if grep -q "^$task_id$" "$busy_file" 2>/dev/null; then
    rm -f "$busy_file"
  fi
  exit 72
fi

echo "Dispatched task $task_id to $worker (target: $SESSION_NAME:$target_window)"
