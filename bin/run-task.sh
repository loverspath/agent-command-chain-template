#!/usr/bin/env bash
set -uo pipefail
umask 077

worker="${1:-}"
task_id="${2:-}"
prompt_file="${3:-}"
timeout_s="${4:-1800}"

if [[ -z "$worker" || -z "$task_id" || -z "$prompt_file" ]]; then
  echo "Usage: $0 <worker> <task_id> <prompt_file> [timeout_s]" >&2
  exit 64
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$HERE/config.env" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/config.env"
fi

SESSION_NAME="${SESSION_NAME:-agentchain}"
session_safe="$(printf '%s' "$SESSION_NAME" | tr -cd '[:alnum:]_-')"
proj_hash="$(printf '%s' "${PROJECT_DIR:-$PWD}" | md5sum | cut -c1-8)"
default_runtime="$HERE/runtime/${session_safe}-${proj_hash}"
runtime="${ACC_RUNTIME:-$default_runtime}"

task_dir="$runtime/tasks/$task_id"
state="$task_dir/state"
log="$task_dir/output.log"
busy_file="$runtime/workers/$worker.busy"

mkdir -p "$task_dir"

# Busy 소유권 검증 (dispatch가 부여한 lease가 본인 task_id와 일치하는지 확인)
if [[ ! -f "$busy_file" ]] || ! grep -q "^$task_id$" "$busy_file" 2>/dev/null; then
  echo "Error: Worker '$worker' busy lease does not belong to task '$task_id'. Execution rejected." >&2
  exit 75
fi

started_epoch="$(date +%s)"
deadline_epoch=$((started_epoch + timeout_s))

terminal_emitted=false
worker_pid=""
worker_pgid=""
proc_start_token="$started_epoch"

write_state() {
  local status="$1"
  local term_evt_id="${2:-}"
  local tmp="$state.tmp.$$"
  {
    printf 'task_id=%s\n' "$task_id"
    printf 'worker=%s\n' "$worker"
    printf 'status=%s\n' "$status"
    printf 'pid=%s\n' "${worker_pid:-$$}"
    printf 'pgid=%s\n' "${worker_pgid:-$$}"
    printf 'proc_token=%s\n' "$proc_start_token"
    printf 'started_epoch=%s\n' "$started_epoch"
    printf 'deadline_epoch=%s\n' "$deadline_epoch"
    if [[ -n "$term_evt_id" ]]; then
      printf 'terminal_event_id=%s\n' "$term_evt_id"
    fi
  } >"$tmp"

  local close_fds=()
  [[ -n "${terminal_fd:-}" ]] && close_fds+=("${terminal_fd}>&-")
  [[ -n "${sig_term_fd:-}" ]] && close_fds+=("${sig_term_fd}>&-")
  if [[ ${#close_fds[@]} -gt 0 ]]; then
    eval "mv \"\$tmp\" \"\$state\" ${close_fds[*]}"
  else
    mv "$tmp" "$state"
  fi
}

LAST_EMITTED_EVENT_ID=""

# Durable Outbox 기반 원자적 이벤트 기록
emit_durable_event() {
  local kind="$1"
  local exit_code="$2"
  local summary="$3"

  LAST_EMITTED_EVENT_ID=""

  local pending="$runtime/events/pending"
  local fifo="$runtime/event.fifo"
  mkdir -p "$pending"

  local id="$(date +%s%N)-${worker}-$$"
  local tmp="$pending/.event.$id.tmp"

  local clean_summary
  clean_summary="$(printf '%s' "$summary" | tr -d '\000-\010\013\014\016-\037\177' | tr '\r\n\t' '   ' | tr '<>' '[]' | tr -s ' ' | cut -c 1-120)"
  local summary_b64="$(printf '%s' "$clean_summary" | base64 -w0)"
  local path_b64="$(printf '%s' "$log" | base64 -w0)"

  {
    printf 'version=1\n'
    printf 'id=%s\n' "$id"
    printf 'source=%s\n' "$worker"
    printf 'kind=%s\n' "$kind"
    printf 'task_id=%s\n' "$task_id"
    printf 'created_epoch=%s\n' "$(date +%s)"
    printf 'exit_code=%s\n' "$exit_code"
    printf 'summary_b64=%s\n' "$summary_b64"
    printf 'detail_path_b64=%s\n' "$path_b64"
  } >"$tmp"

  if ! mv "$tmp" "$pending/$id.evt"; then
    echo "Error: Failed to move $tmp to $pending/$id.evt" >&2
    return 1
  fi

  if [[ -p "$fifo" ]]; then
    timeout 0.2 bash -c 'printf ".\n" >"$1"' _ "$fifo" >/dev/null 2>&1 || true
  fi

  LAST_EMITTED_EVENT_ID="$id"
  printf '%s' "$id"
  return 0
}

cleanup_on_signal() {
  local sig="$1"
  if [[ -f "$busy_file" ]] && grep -q "^$task_id$" "$busy_file" 2>/dev/null; then
    rm -f "$busy_file"
  fi
  if [[ -n "$worker_pgid" ]]; then
    kill -TERM -- "-$worker_pgid" 2>/dev/null || true
    sleep 0.2
    kill -KILL -- "-$worker_pgid" 2>/dev/null || true
  fi
  if [[ "$terminal_emitted" == "false" ]]; then
    local sig_term_id="${task_id}-terminal"
    local sig_pending="$runtime/events/pending"
    local sig_fifo="$runtime/event.fifo"
    mkdir -p "$sig_pending"

    # 모든 외부 명령어(base64, log 등)를 락 획득 전에 미리 수행
    local clean_sig_summary="Worker interrupted by signal $sig"
    local sig_summary_b64
    sig_summary_b64="$(printf '%s' "$clean_sig_summary" | base64 -w0)"
    local sig_path_b64
    sig_path_b64="$(printf '%s' "$log" | base64 -w0)"
    local sig_tmp="$sig_pending/.event.$sig_term_id.tmp"

    {
      printf 'version=1\n'
      printf 'id=%s\n' "$sig_term_id"
      printf 'source=%s\n' "$worker"
      printf 'kind=canceled\n'
      printf 'task_id=%s\n' "$task_id"
      printf 'created_epoch=%s\n' "$(date +%s)"
      printf 'exit_code=130\n'
      printf 'summary_b64=%s\n' "$sig_summary_b64"
      printf 'detail_path_b64=%s\n' "$sig_path_b64"
    } >"$sig_tmp"

    local sig_term_fd=""
    exec {sig_term_fd}>"$task_dir/terminal.lock"
    if flock -n "$sig_term_fd"; then
      local is_terminal=false
      if [[ -f "$state" ]]; then
        while IFS= read -r s_line; do
          case "$s_line" in
            status=done*|status=error*|status=canceled*|terminal_event_id=*|killed_by=*)
              is_terminal=true
              break
              ;;
          esac
        done < "$state"
      fi

      if [[ "$is_terminal" == "false" ]]; then
        # 순수 bash 멱등성 검사 (fork/pipeline 없이 FD 상속 누수 원천 차단)
        local sig_evt_exists=false
        if [[ -f "$sig_pending/$sig_term_id.evt" ]]; then
          sig_evt_exists=true
        else
          for f in "$runtime/events/inflight"/*/"$sig_term_id.evt" "$runtime/events/archive"/*/"$sig_term_id.evt"; do
            if [[ -f "$f" ]]; then
              sig_evt_exists=true
              break
            fi
          done
        fi

        if [[ "$sig_evt_exists" == "false" ]]; then
          if eval "mv \"\$sig_tmp\" \"\$sig_pending/\$sig_term_id.evt\" ${sig_term_fd}>&-"; then
            write_state "canceled" "$sig_term_id"
          else
            write_state notification_error ""
          fi
        else
          write_state "canceled" "$sig_term_id"
        fi
      fi
      flock -u "$sig_term_fd" 2>/dev/null || true
    fi
    [[ -n "$sig_term_fd" ]] && exec {sig_term_fd}>&-
    sig_term_fd=""
    terminal_emitted=true
    rm -f "$sig_tmp" 2>/dev/null || true

    if [[ -p "$sig_fifo" ]]; then
      timeout 0.2 bash -c 'printf ".\n" >"$1"' _ "$sig_fifo" >/dev/null 2>&1 || true
    fi
  fi
  exit 130
}

trap 'cleanup_on_signal INT' INT
trap 'cleanup_on_signal TERM' TERM

case "$worker" in
  agy) adapter_cmd=("$HERE/adapters/agy-oneshot.sh" "$prompt_file") ;;
  codex) adapter_cmd=("$HERE/adapters/codex-oneshot.sh" "$prompt_file") ;;
  *)
    echo "Unknown worker: $worker" | tee -a "$log"
    exit 64
    ;;
esac

# setsid를 통해 완전히 격리된 새 프로세스 세션/그룹 리더로 worker 실행
setsid timeout --foreground --kill-after=10s "${timeout_s}s" "${adapter_cmd[@]}" > "$log" 2>&1 &
worker_pid=$!
worker_pgid="$(ps -o pgid= -p "$worker_pid" 2>/dev/null | tr -d ' ' || echo "$worker_pid")"
proc_start_token="$(awk '{print $22}' "/proc/$worker_pid/stat" 2>/dev/null || echo "$started_epoch")"

write_state running
emit_durable_event "started" "" "worker process started (PID $worker_pid, PGID $worker_pgid)" >/dev/null || true

set +e
wait "$worker_pid"
rc=$?
set -e

# Busy 마커 안전 해제 (본인 task_id 일치 시에만 삭제)
if [[ -f "$busy_file" ]] && grep -q "^$task_id$" "$busy_file" 2>/dev/null; then
  rm -f "$busy_file"
fi

# 1. 락 획득 전에 모든 요약 추출, base64 변환, payload .tmp 생성을 완전히 마침 (flock FD 자식 상속 원천 차단)
term_kind=""
term_code=0
term_summary=""

if (( rc == 0 )); then
  term_kind="done"
  term_code=0
  term_summary="$(tail -n 10 "$log" 2>/dev/null || echo "ok")"
elif (( rc == 124 || rc == 137 )); then
  term_kind="error"
  term_code="$rc"
  term_summary="timed out after ${timeout_s}s; $(tail -n 5 "$log" 2>/dev/null || echo "")"
else
  term_kind="error"
  term_code="$rc"
  term_summary="exit code $rc; $(tail -n 5 "$log" 2>/dev/null || echo "")"
fi

term_event_id="${task_id}-${term_kind}"
pending="$runtime/events/pending"
fifo="$runtime/event.fifo"
mkdir -p "$pending"

# 단일 안정 결정적 ID 규격: ${task_id}-terminal (모든 terminal producer가 동일 ID를 공유하여 중복 발행 원천 방지)
term_event_id="${task_id}-terminal"
pending="$runtime/events/pending"
fifo="$runtime/event.fifo"
mkdir -p "$pending"

clean_summary="$(printf '%s' "$term_summary" | tr -d '\000-\010\013\014\016-\037\177' | tr '\r\n\t' '   ' | tr '<>' '[]' | tr -s ' ' | cut -c 1-120)"
clean_path="$(printf '%s' "$log" | tr -d '\r\n' | tr '<>' '[]')"
summary_b64="$(printf '%s' "$clean_summary" | base64 -w0)"
path_b64="$(printf '%s' "$clean_path" | base64 -w0)"
tmp_evt="$pending/.event.$term_event_id.tmp"

{
  printf 'version=1\n'
  printf 'id=%s\n' "$term_event_id"
  printf 'source=%s\n' "$worker"
  printf 'kind=%s\n' "$term_kind"
  printf 'task_id=%s\n' "$task_id"
  printf 'created_epoch=%s\n' "$(date +%s)"
  printf 'exit_code=%s\n' "$term_code"
  printf 'summary_b64=%s\n' "$summary_b64"
  printf 'detail_path_b64=%s\n' "$path_b64"
} >"$tmp_evt"

# 2. 원자적 terminal ownership 획득 시도 (flock 기반: 프로세스 비정상 종료 시 커널이 자동 해제하여 stale lock 방지)
terminal_fd=""
exec {terminal_fd}>"$task_dir/terminal.lock"
if ! flock -n "$terminal_fd"; then
  [[ -n "$terminal_fd" ]] && exec {terminal_fd}>&-
  terminal_fd=""
  terminal_emitted=true
  rm -f "$tmp_evt" 2>/dev/null || true
  exit "$rc"
fi

# 3. state 재확인 (watchdog이 이미 terminal 처리를 완료했는지 순수 bash 루프로 검사)
is_already_terminal=false
if [[ -f "$state" ]]; then
  while IFS= read -r s_line; do
    case "$s_line" in
      status=done*|status=error*|status=canceled*|terminal_event_id=*|killed_by=*)
        is_already_terminal=true
        break
        ;;
    esac
  done < "$state"
fi

if [[ "$is_already_terminal" == "true" ]]; then
  flock -u "$terminal_fd" 2>/dev/null || true
  [[ -n "$terminal_fd" ]] && exec {terminal_fd}>&-
  terminal_fd=""
  terminal_emitted=true
  rm -f "$tmp_evt" 2>/dev/null || true
  exit "$rc"
fi

# 4. 순수 bash 기반 멱등성 검사 (fork/pipeline/find/grep 없이 순수 빌트인으로 검사하여 자식 FD 누수 원천 차단)
evt_already_exists=false
if [[ -f "$pending/$term_event_id.evt" ]]; then
  evt_already_exists=true
else
  for f in "$runtime/events/inflight"/*/"$term_event_id.evt" "$runtime/events/archive"/*/"$term_event_id.evt"; do
    if [[ -f "$f" ]]; then
      evt_already_exists=true
      break
    fi
  done
fi

if [[ "$evt_already_exists" == "false" ]]; then
  if ! eval "mv \"\$tmp_evt\" \"\$pending/\$term_event_id.evt\" ${terminal_fd}>&-"; then
    echo "Error: Failed to publish terminal event $term_kind for task $task_id" >&2
    write_state notification_error ""
    flock -u "$terminal_fd" 2>/dev/null || true
    [[ -n "$terminal_fd" ]] && exec {terminal_fd}>&-
    terminal_fd=""
    rm -f "$tmp_evt" 2>/dev/null || true
    exit 74
  fi
fi

write_state "$term_kind" "$term_event_id"
terminal_emitted=true

# 5. Lock 즉시 해제 (모든 임계구역 작업 완료 후 즉시 락 해제)
flock -u "$terminal_fd" 2>/dev/null || true
[[ -n "$terminal_fd" ]] && exec {terminal_fd}>&-
terminal_fd=""

# 6. Lock 해제 후 임시 파일 정리 및 FIFO 펄스 전송
rm -f "$tmp_evt" 2>/dev/null || true

if [[ -p "$fifo" ]]; then
  timeout 0.2 bash -c 'printf ".\n" >"$1"' _ "$fifo" >/dev/null 2>&1 || true
fi

exit "$rc"
