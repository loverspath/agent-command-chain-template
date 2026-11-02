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
ENV_WORKER_MODE="${WORKER_MODE:-}"
ENV_AGY_MODE="${AGY_MODE:-}"
ENV_CODEX_MODE="${CODEX_MODE:-}"

# shellcheck disable=SC1091
source "$HERE/lib/config.sh"
acc_load_config "$HERE" || exit $?
CONFIG_SESSION_NAME="${SESSION_NAME:-agentchain}"

[[ -n "$ENV_WORKER_MODE" ]] && WORKER_MODE="$ENV_WORKER_MODE"
[[ -n "$ENV_AGY_MODE" ]] && AGY_MODE="$ENV_AGY_MODE"
[[ -n "$ENV_CODEX_MODE" ]] && CODEX_MODE="$ENV_CODEX_MODE"

# shellcheck disable=SC1091
source "$HERE/lib/session.sh"

AGY_WINDOW="${ENV_AGY_WINDOW:-${AGY_WINDOW:-agy}}"
CODEX_WINDOW="${ENV_CODEX_WINDOW:-${CODEX_WINDOW:-codex}}"

timeout="${DEFAULT_TASK_TIMEOUT:-1800}"
prompt_file=""
prompt_inline=""
dry_run=false
recover_from=""
no_recovery_note=false
use_worktree=false
worktree_branch=""

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
    --recover-from)
      recover_from="$2"
      shift 2
      ;;
    --no-recovery-note)
      no_recovery_note=true
      shift
      ;;
    --worktree|--isolate)
      use_worktree=true
      shift
      ;;
    --worktree-branch)
      worktree_branch="$2"
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

# Git worktree isolation 사전 검증
if [[ "$use_worktree" == "true" ]]; then
  orig_project_dir="${PROJECT_DIR:-$PWD}"
  if ! git -C "$orig_project_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Error: --worktree requires PROJECT_DIR to be a valid git repository" >&2
    exit 65
  fi
  if [[ -n "$worktree_branch" ]]; then
    if ! git check-ref-format --branch "$worktree_branch" 2>/dev/null; then
      echo "Error: Invalid branch name '$worktree_branch'" >&2
      exit 64
    fi
  fi
fi

# 세션명 및 런타임 디렉토리 자동 해석 (우선순위: ①환경변수 -> ②런타임마커 -> ③현재tmux -> ④config기본값)
resolve_session_and_runtime "dispatch" "$target_window" false
runtime="$ACC_RUNTIME"

# 세션 및 대상 윈도우 존재 확인
if ! term_session_exists "$SESSION_NAME"; then
  echo "Error: tmux session '$SESSION_NAME' does not exist." >&2
  exit 69
fi

if ! term_has_window "$SESSION_NAME" "$target_window"; then
  echo "Error: Target window '$target_window' not found in session '$SESSION_NAME'." >&2
  exit 69
fi

# v2 Full-Push 런타임 호환성 fail-closed 검증
if [[ ! -f "$runtime/bootstrap_version" ]] || ! grep -q '^bootstrap_version=2' "$runtime/bootstrap_version"; then
  echo "Error: Session '$SESSION_NAME' is not initialized with v2 Full-Push architecture (missing v2 bootstrap marker). Run bootstrap-v2.sh first." >&2
  exit 70
fi

case "$worker" in
  agy) worker_mode="${AGY_MODE:-${WORKER_MODE:-oneshot}}" ;;
  codex) worker_mode="${CODEX_MODE:-${WORKER_MODE:-oneshot}}" ;;
  *) worker_mode="${WORKER_MODE:-oneshot}" ;;
esac

case "$worker_mode" in
  oneshot) ;;
  resident)
    if [[ "$worker" == "codex" ]]; then
      echo "Error: Stage 1 does not implement resident mode for codex (completion hooks/watchdog are agy-only). Use CODEX_MODE=oneshot. Dispatch rejected." >&2
      exit 64
    fi
    ;;
  *)
    echo "Error: Unsupported worker mode '$worker_mode' for $worker (must be oneshot or resident)" >&2
    exit 64
    ;;
esac

# 대상 pane의 현재 프로세스 검증 (상주 모드는 worker TUI, oneshot은 idle shell 요구)
cur_cmd="$(term_fg_cmd "$SESSION_NAME:$target_window")"
cur_cmd="${cur_cmd:-}"
if [[ "$worker_mode" == "resident" ]]; then
  if [[ "$cur_cmd" != "$worker" ]]; then
    echo "Error: Target window '$target_window' in session '$SESSION_NAME' is running '$cur_cmd' instead of resident TUI '$worker'. Dispatch rejected." >&2
    echo "Hint: Make sure the resident TUI for $worker is running in '$SESSION_NAME:$target_window' or set ${worker^^}_MODE=oneshot." >&2
    exit 71
  fi
else
  case "$cur_cmd" in
    bash|zsh|sh|-bash|-zsh|-sh) ;;
    *)
      echo "Error: Target window '$target_window' in session '$SESSION_NAME' is running interactive TUI '$cur_cmd' instead of idle shell. Dispatch rejected." >&2
      echo "Hint: If this is a v1 session, re-run with SESSION_NAME=<v2-session> (e.g. SESSION_NAME=agentchain-v2) to target the v2 idle worker shell." >&2
      exit 71
      ;;
  esac
fi

# 드라이런 요청 시 실제 작업 디스패치 없이 성공 종료
if [[ "$dry_run" == "true" ]]; then
  echo "[dry-run] Target window '$target_window' in session '$SESSION_NAME' is ready (mode: $worker_mode, current command: '$cur_cmd')."
  echo "[dry-run] Task would be dispatched to $worker with timeout ${timeout}s."
  if [[ "$use_worktree" == "true" ]]; then
    echo "[dry-run] Isolated git worktree would be created under $runtime/worktrees/<task_id> (base: $(git -C "${PROJECT_DIR:-$PWD}" rev-parse --short HEAD 2>/dev/null || echo "HEAD"))."
  fi
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

wt_dir=""
wt_branch=""
if [[ "$use_worktree" == "true" ]]; then
  orig_project_dir="${PROJECT_DIR:-$PWD}"
  wt_root="$runtime/worktrees"
  wt_dir="$wt_root/$task_id"
  wt_branch="${worktree_branch:-acc/$task_id}"
  mkdir -p "$wt_root"
  base_sha="$(git -C "$orig_project_dir" rev-parse HEAD 2>/dev/null || echo "")"
  if ! timeout 120 git -C "$orig_project_dir" worktree add -b "$wt_branch" "$wt_dir" "$base_sha" >"$task_dir/worktree.log" 2>&1; then
    echo "Error: Failed to prepare isolated git worktree at $wt_dir (see $task_dir/worktree.log)" >&2
    git -C "$orig_project_dir" worktree remove --force "$wt_dir" >/dev/null 2>&1 || rm -rf "$wt_dir" 2>/dev/null || true
    git -C "$orig_project_dir" branch -D "$wt_branch" >/dev/null 2>&1 || true
    rm -rf "$task_dir"
    exit 73
  fi
  {
    printf 'worktree_dir=%s\n' "$wt_dir"
    printf 'worktree_branch=%s\n' "$wt_branch"
    printf 'base_project_dir=%s\n' "$orig_project_dir"
    printf 'base_sha=%s\n' "$base_sha"
  } > "$task_dir/worktree.meta"
fi

rollback_worktree_on_abort() {
  if [[ "$use_worktree" == "true" && -n "$wt_dir" ]]; then
    git -C "${PROJECT_DIR:-$PWD}" worktree remove --force "$wt_dir" >/dev/null 2>&1 || rm -rf "$wt_dir" 2>/dev/null || true
    git -C "${PROJECT_DIR:-$PWD}" branch -D "$wt_branch" >/dev/null 2>&1 || true
  fi
}

brief_header=""
if [[ "${ACC_NO_BRIEF_HEADER:-0}" != "1" ]]; then
  target_project_dir="${PROJECT_DIR:-$PWD}"
  if [[ "$use_worktree" == "true" ]]; then
    brief_header="[CHAIN CONTEXT]
TEMPLATE_ROOT: $HERE
CHAIN_WIKI_INDEX: $HERE/.llmwiki/INDEX.md
ROLES: $HERE/ROLES.md
PROJECT_DIR: $wt_dir
BASE_PROJECT_DIR: $target_project_dir
WORKTREE_DIR: $wt_dir
WORKTREE_BRANCH: $wt_branch
WORKTREE_ISOLATION: active (반드시 WORKTREE_DIR 내부에서만 파일을 수정하고 BASE_PROJECT_DIR은 직접 수정하지 마라)
SESSION_NAME: $SESSION_NAME
TASK_ID: $task_id
WORKER_RULES:
- agy: oneshot router mode. Do not directly execute with Read/Edit/Bash. Delegate using invoke_subagent. If delegation fails, report [[BLOCKED <id>]].
- codex: Project convention files (e.g. AGENTS.md) take precedence over chain rules for coding conventions.
---
"
  else
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
fi

recovery_note=""
recovered_task=""
if [[ "${ACC_NO_BRIEF_HEADER:-0}" != "1" && "${ACC_NO_RECOVERY_NOTE:-0}" != "1" && "$no_recovery_note" == "false" ]]; then
  prev_tid=""
  prev_status=""
  prev_reason=""
  if [[ -n "$recover_from" ]]; then
    if [[ ! -f "$runtime/tasks/$recover_from/state" ]]; then
      echo "Error: Task to recover from not found: $recover_from" >&2
      rollback_worktree_on_abort
      exit 66
    fi
    prev_tid="$recover_from"
    prev_status="$(grep '^status=' "$runtime/tasks/$recover_from/state" 2>/dev/null | cut -d= -f2- || true)"
    prev_reason="$(grep '^reason_code=' "$runtime/tasks/$recover_from/state" 2>/dev/null | cut -d= -f2- || true)"
  elif [[ -f "$runtime/workers/$worker.last_interrupted" ]]; then
    i_epoch="$(grep '^interrupted_epoch=' "$runtime/workers/$worker.last_interrupted" 2>/dev/null | cut -d= -f2- || echo 0)"
    ttl="${RECOVERY_NOTE_TTL:-86400}"
    now_epoch="$(date +%s)"
    if (( now_epoch - i_epoch < ttl )); then
      prev_tid="$(grep '^task_id=' "$runtime/workers/$worker.last_interrupted" 2>/dev/null | cut -d= -f2- || true)"
      prev_status="$(grep '^status=' "$runtime/workers/$worker.last_interrupted" 2>/dev/null | cut -d= -f2- || true)"
      prev_reason="$(grep '^reason_code=' "$runtime/workers/$worker.last_interrupted" 2>/dev/null | cut -d= -f2- || true)"
    fi
  fi

  if [[ -n "$prev_tid" ]]; then
    recovered_task="$prev_tid"
    prev_reason_display="${prev_reason:-interrupted}"
    recovery_note="[RECOVERY NOTE]
PREV_TASK_ID: $prev_tid
PREV_STATUS: ${prev_status:-unknown} (reason_code: $prev_reason_display)
PREV_LOG: $runtime/tasks/$prev_tid/output.log
INSTRUCTION: 직전 작업(${prev_tid})이 완료되지 못하고 중단되었습니다. 파일 수정, 외부 명령 실행, git commit을 반복하기 전에 반드시 현재 작업 트리(\`git status\`, \`git diff\`, \`git log -n 5\`)와 직전 작업 로그/산출물을 먼저 확인하십시오. 이미 반영된 변경사항은 중복 실행하지 말고 남은 단계만 이어서 완료하십시오.
---
"
    printf 'recovered_from=%s\n' "$prev_tid" > "$task_dir/recovery.meta"
  fi
fi

if [[ -n "$brief_header" || -n "$recovery_note" ]]; then
  {
    [[ -n "$brief_header" ]] && printf '%s\n' "$brief_header"
    [[ -n "$recovery_note" ]] && printf '%s\n' "$recovery_note"
  } >"$task_dir/prompt.md"
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

# 규약 D 주입 (agy 작업이고 brief header가 활성화된 경우, 중복 주입 방지)
if [[ "$worker" == "agy" && "${ACC_NO_BRIEF_HEADER:-0}" != "1" ]]; then
  if ! grep -qE '\[\[DONE (<task_id>|'"$task_id"')\]\]|\[PROTOCOL D\]|규약 D' "$task_dir/prompt.md" 2>/dev/null; then
    cat >> "$task_dir/prompt.md" <<EOF

[PROTOCOL D]
서브에이전트를 쓰더라도 본체는 모든 서브에이전트가 끝나 결과를 받을 때까지 턴을 종료하지 말고, 최종 보고 후 마지막 줄에 [[DONE $task_id]] result=… | learning=…(막히면 [[BLOCKED $task_id]] reason=…)를 출력한 뒤에만 종료하라. 센티널은 최종 응답에만 쓰고 중간 응답·서브에이전트 지시에는 쓰지 마라.
EOF
  fi
fi

# ARG_MAX 및 CLI 입력 방어를 위한 프롬프트 크기 사전 검증 (헤더 포함 128KB 제한)
prompt_size="$(wc -c < "$task_dir/prompt.md")"
if (( prompt_size > 131072 )); then
  echo "Error: Prompt file exceeds maximum allowed size (128KB)" >&2
  rollback_worktree_on_abort
  rm -rf "$task_dir"
  exit 65
fi

if [[ "$worker_mode" == "resident" ]]; then
  started_epoch="$(date +%s)"
  deadline_epoch=$((started_epoch + timeout))
  pane_pid="$(term_pane_pid "$SESSION_NAME:$target_window")"
  pane_pid="${pane_pid:-$$}"
  child_pid="$(pgrep -P "$pane_pid" "$worker" 2>/dev/null | head -n 1 || echo "")"
  target_pid="${child_pid:-$pane_pid}"
  proc_token="$(awk '{print $22}' "/proc/$target_pid/stat" 2>/dev/null || echo "$started_epoch")"
  pgid="$(ps -o pgid= -p "$target_pid" 2>/dev/null | tr -d ' ' || echo "$target_pid")"

  # 상주 모드: 작업 상태 파일 직접 기록
  cat > "$task_dir/state" <<EOF
task_id=$task_id
worker=$worker
status=running
mode=resident
pid=$target_pid
pgid=$pgid
proc_token=$proc_token
started_epoch=$started_epoch
deadline_epoch=$deadline_epoch
EOF
  [[ -n "$recovered_task" ]] && printf 'recovered_from=%s\n' "$recovered_task" >> "$task_dir/state"
  [[ -n "$wt_dir" ]] && printf 'worktree_dir=%s\nworktree_branch=%s\n' "$wt_dir" "$wt_branch" >> "$task_dir/state"

  # 작업 예약 마커 설정 및 락 해제
  printf '%s\n%s\n' "$task_id" "$started_epoch" > "$busy_file"
  lease_acquired=false
  if [[ "$(cat "$lock_dir/owner" 2>/dev/null || true)" == "$owner_token" ]]; then
    rm -rf "$lock_dir" 2>/dev/null || true
  fi

  # started 이벤트 발행
  "$HERE/bin/event-emit.sh" "$worker" started "$task_id" "Resident task started (target: $SESSION_NAME:$target_window)" >/dev/null 2>&1 || true

  # 1줄 트리거 주입 (개행 폭주 방지)
  trigger="Read and execute task prompt: $task_dir/prompt.md"
  if ! term_send_line "$SESSION_NAME:$target_window" "$trigger"; then
    echo "Error: Failed to inject command into tmux window '$target_window'." >&2
    rollback_worktree_on_abort
    if grep -q "^$task_id$" "$busy_file" 2>/dev/null; then
      rm -f "$busy_file"
    fi
    exit 72
  fi
  # 주입 성공 후 이전 중단 마커 정리
  rm -f "$runtime/workers/$worker.last_interrupted" 2>/dev/null || true
else
  # oneshot 모드: 작업 예약 마커 설정 및 락 해제
  printf '%s\n%s\n' "$task_id" "$(date +%s)" > "$busy_file"
  lease_acquired=false
  if [[ "$(cat "$lock_dir/owner" 2>/dev/null || true)" == "$owner_token" ]]; then
    rm -rf "$lock_dir" 2>/dev/null || true
  fi

  # 안전하게 인자 이스케이프 후 literal 모드로 명령 주입
  if [[ "$use_worktree" == "true" ]]; then
    run_cmd=$(printf '%q %q %q %q %q %q' "$HERE/bin/run-task.sh" "$worker" "$task_id" "$task_dir/prompt.md" "$timeout" "$wt_dir")
  else
    run_cmd=$(printf '%q %q %q %q %q' "$HERE/bin/run-task.sh" "$worker" "$task_id" "$task_dir/prompt.md" "$timeout")
  fi
  if ! term_run_cmd "$SESSION_NAME:$target_window" "$run_cmd"; then
    echo "Error: Failed to inject command into tmux window '$target_window'." >&2
    rollback_worktree_on_abort
    if grep -q "^$task_id$" "$busy_file" 2>/dev/null; then
      rm -f "$busy_file"
    fi
    exit 72
  fi
  # 주입 성공 후 이전 중단 마커 정리
  rm -f "$runtime/workers/$worker.last_interrupted" 2>/dev/null || true
fi

echo "Dispatched task $task_id to $worker (target: $SESSION_NAME:$target_window, mode: $worker_mode)"
