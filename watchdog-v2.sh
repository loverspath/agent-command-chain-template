#!/usr/bin/env bash
# watchdog-v2.sh: 4대 비정상 상태(세션/Sonnet, PID증발, Stall/Deadline, 브리지 정체/ACK timeout) 전담 안전망
set -uo pipefail
umask 077

INVOKED_DIR="$PWD"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_ENV_PROJECT_DIR="${PROJECT_DIR:-}"
_ENV_ACC_RUNTIME="${ACC_RUNTIME:-}"
_ENV_SESSION_NAME="${SESSION_NAME:-}"
_ENV_WATCHDOG_AUTO_KILL="${WATCHDOG_AUTO_KILL:-}"

cd "$HERE"

if [[ -f config.env ]]; then
  # shellcheck disable=SC1091
  source config.env
fi
CONFIG_SESSION_NAME="${SESSION_NAME:-agentchain}"

# shellcheck disable=SC1091
source "$HERE/lib/session.sh"

[[ -n "$_ENV_PROJECT_DIR" ]] && PROJECT_DIR="$_ENV_PROJECT_DIR"
[[ -n "$_ENV_ACC_RUNTIME" ]] && ACC_RUNTIME="$_ENV_ACC_RUNTIME"
[[ -n "$_ENV_SESSION_NAME" ]] && SESSION_NAME="$_ENV_SESSION_NAME"
[[ -n "$_ENV_WATCHDOG_AUTO_KILL" ]] && WATCHDOG_AUTO_KILL="$_ENV_WATCHDOG_AUTO_KILL"
WATCHDOG_AUTO_KILL="${WATCHDOG_AUTO_KILL:-false}"

PROJECT_DIR="${PROJECT_DIR:-$INVOKED_DIR}"
if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "PROJECT_DIR '$PROJECT_DIR' 가 존재하지 않는다." >&2
  exit 1
fi
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"

LOG_DIR="${LOG_DIR:-./logs}"
[[ "$LOG_DIR" = /* ]] || LOG_DIR="$HERE/$LOG_DIR"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/watchdog.log"

ENV_SESSION_NAME="$_ENV_SESSION_NAME"
ENV_ACC_RUNTIME="$_ENV_ACC_RUNTIME"
resolve_session_and_runtime "watchdog-v2" "" false

mkdir -p "$ACC_RUNTIME/events"/{pending,inflight,archive} "$ACC_RUNTIME/tasks" "$ACC_RUNTIME/workers"

# 워치독 단일 인스턴스 락 (자식 프로세스 상속 방지를 위해 flock --close 래퍼 및 내부 전용 인자 사용)
export PROJECT_DIR ACC_RUNTIME SESSION_NAME
if [[ "${1:-}" != "--lock-held" ]]; then
  target_self="$HERE/${BASH_SOURCE[0]##*/}"
  [[ -f "$target_self" ]] || target_self="$0"
  exec flock -n -E 0 --close "$ACC_RUNTIME/watchdog.lock" "$BASH" "$target_self" --lock-held "$@"
fi
shift # 내부 전용 --lock-held 인자 즉시 소비 (자식 프로세스 전파 원천 차단)

printf '%s\n' "$$" > "$ACC_RUNTIME/watchdog.pid"
trap 'rm -f "$ACC_RUNTIME/watchdog.pid"' EXIT

WATCHDOG_INTERVAL="${WATCHDOG_INTERVAL:-60}"
DEFAULT_TASK_TIMEOUT="${DEFAULT_TASK_TIMEOUT:-1800}"
NO_OUTPUT_WARN_SECONDS="${NO_OUTPUT_WARN_SECONDS:-900}"
EVENT_DELIVERY_GRACE="${EVENT_DELIVERY_GRACE:-120}"
EVENT_ACK_TIMEOUT="${EVENT_ACK_TIMEOUT:-600}"
if [[ "${WORKER_MODE:-oneshot}" != "oneshot" ]]; then
  echo "Error: watchdog-v2는 WORKER_MODE=oneshot 만 지원합니다." >&2
  exit 64
fi

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "워치독 v2 시작 (세션=$SESSION_NAME, 주기=${WATCHDOG_INTERVAL}s, 런타임=$ACC_RUNTIME)"

check_session_and_sonnet() {
  if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    log "세션 '$SESSION_NAME' 부재 — bootstrap-v2.sh 재실행"
    PROJECT_DIR="$PROJECT_DIR" SESSION_NAME="$SESSION_NAME" ACC_RUNTIME="$ACC_RUNTIME" "$HERE/bootstrap-v2.sh" >> "$LOG_FILE" 2>&1 || true
    return
  fi

  local cur
  cur="$(tmux display-message -p -t "${SESSION_NAME}:${SONNET_WINDOW}" '#{pane_current_command}' 2>/dev/null || echo "MISSING")"
  case "$cur" in
    MISSING)
      log "Sonnet 윈도우 부재 — 재생성"
      tmux new-window -t "$SESSION_NAME" -n "$SONNET_WINDOW" -c "$PROJECT_DIR"
      local bridge_settings="$ACC_RUNTIME/claude-bridge.settings.json"
      local brief_file="$LOG_DIR/session_brief.md"
      local sonnet_launch="export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 ACC_RUNTIME=\"$ACC_RUNTIME\"; $SONNET_CMD --settings \"$bridge_settings\" --add-dir \"$HERE\" --append-system-prompt \"\$(cat '$brief_file' 2>/dev/null || echo '')\""
      tmux send-keys -t "${SESSION_NAME}:${SONNET_WINDOW}" "$sonnet_launch" C-m
      ;;
    bash|zsh|sh|-bash|-zsh|-sh)
      log "Sonnet 프로세스 종료 감지(현재: $cur) — 재기동"
      local bridge_settings="$ACC_RUNTIME/claude-bridge.settings.json"
      local brief_file="$LOG_DIR/session_brief.md"
      local sonnet_launch="export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 ACC_RUNTIME=\"$ACC_RUNTIME\"; $SONNET_CMD --settings \"$bridge_settings\" --add-dir \"$HERE\" --append-system-prompt \"\$(cat '$brief_file' 2>/dev/null || echo '')\""
      tmux send-keys -t "${SESSION_NAME}:${SONNET_WINDOW}" "$sonnet_launch" C-m
      ;;
    *)
      ;;
  esac
}

write_task_state() {
  local target_state="$1"
  local new_status="$2"
  local new_killed_by="${3:-}"
  local new_term_evt_id="${4:-}"
  local close_fd="${5:-}"

  local t_dir="${target_state%/*}"
  local tmp_state="$t_dir/.state.tmp.$$"

  local t_id="" w_name="" p_id="" pg_id="" p_tok="" s_epoch="" d_epoch="" old_term_id=""
  if [[ -f "$target_state" ]]; then
    while IFS= read -r line; do
      case "$line" in
        task_id=*) t_id="${line#task_id=}" ;;
        worker=*) w_name="${line#worker=}" ;;
        pid=*) p_id="${line#pid=}" ;;
        pgid=*) pg_id="${line#pgid=}" ;;
        proc_token=*) p_tok="${line#proc_token=}" ;;
        started_epoch=*) s_epoch="${line#started_epoch=}" ;;
        deadline_epoch=*) d_epoch="${line#deadline_epoch=}" ;;
        terminal_event_id=*) old_term_id="${line#terminal_event_id=}" ;;
      esac
    done < "$target_state"
  fi

  local effective_term_id="${new_term_evt_id:-$old_term_id}"

  {
    printf 'task_id=%s\n' "$t_id"
    printf 'worker=%s\n' "$w_name"
    printf 'status=%s\n' "$new_status"
    printf 'pid=%s\n' "$p_id"
    printf 'pgid=%s\n' "$pg_id"
    printf 'proc_token=%s\n' "$p_tok"
    printf 'started_epoch=%s\n' "$s_epoch"
    printf 'deadline_epoch=%s\n' "$d_epoch"
    if [[ -n "$new_killed_by" ]]; then
      printf 'killed_by=%s\n' "$new_killed_by"
    fi
    if [[ -n "$effective_term_id" && "$new_status" != "notification_error" ]]; then
      printf 'terminal_event_id=%s\n' "$effective_term_id"
    fi
  } > "$tmp_state"

  if [[ -n "$close_fd" ]]; then
    eval "mv \"\$tmp_state\" \"\$target_state\" ${close_fd}>&-"
  else
    mv "$tmp_state" "$target_state"
  fi
}

check_running_tasks() {
  local now
  now="$(date +%s)"

  for state_file in "$ACC_RUNTIME/tasks"/*/state; do
    [[ -f "$state_file" ]] || continue
    local task_dir="${state_file%/*}"
    local task_id="" worker="" status="" pid="" pgid="" proc_token="" started_epoch="" deadline_epoch=""
    while IFS= read -r line; do
      case "$line" in
        task_id=*) task_id="${line#task_id=}" ;;
        worker=*) worker="${line#worker=}" ;;
        status=*) status="${line#status=}" ;;
        pid=*) pid="${line#pid=}" ;;
        pgid=*) pgid="${line#pgid=}" ;;
        proc_token=*) proc_token="${line#proc_token=}" ;;
        started_epoch=*) started_epoch="${line#started_epoch=}" ;;
        deadline_epoch=*) deadline_epoch="${line#deadline_epoch=}" ;;
      esac
    done < "$state_file"
    [[ -n "$pgid" ]] || pgid="$pid"

    case "$status" in
      running|notification_error) ;;
      *) continue ;;
    esac
    [[ -n "$pid" ]] || continue

    # 1. OS PID 생존 및 PID 재사용 검증
    local pid_alive=false
    if kill -0 "$pid" 2>/dev/null; then
      if [[ -n "$proc_token" ]]; then
        local cur_token
        cur_token="$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || echo "")"
        if [[ "$cur_token" == "$proc_token" ]]; then
          pid_alive=true
        fi
      else
        pid_alive=true
      fi
    fi

    # PID가 증발했거나, 이전 알림 실패(notification_error) 상태인 경우 reap 처리
    if [[ "$pid_alive" == "false" || "$status" == "notification_error" ]]; then
      if [[ "$pid_alive" == "false" ]]; then
        log "Task $task_id (PID $pid) 비정상 증발 감지 (PID dead or reused)"
      else
        log "Task $task_id 알림 에러 복구 시도 (status=$status)"
      fi
      local term_fd=""
      exec {term_fd}>"$task_dir/terminal.lock"
      if flock -n "$term_fd"; then
        local is_term=false
        local state_committed=false
        local emit_ok=false
        local should_pulse_fifo=false
        local existing_term_id=""
        if [[ -f "$state_file" ]]; then
          while IFS= read -r s_line; do
            case "$s_line" in
              status=done*|status=error*|status=canceled*) is_term=true ;;
              terminal_event_id=*) existing_term_id="${s_line#terminal_event_id=}" ;;
            esac
          done < "$state_file"
        fi

        # Auto-repair: status가 running인데 terminal_event_id가 이미 존재하는 비정상 상태 복구
        if [[ "$is_term" == "false" && -n "$existing_term_id" ]]; then
          if write_task_state "$state_file" "error" "watchdog_reap" "$existing_term_id" "$term_fd"; then
            is_term=true
            state_committed=true
          else
            is_term=true
            state_committed=false
          fi
        fi

        if [[ "$is_term" == "false" ]]; then
          # 단일 안정 결정적 ID: ${task_id}-terminal (모든 terminal producer가 동일 ID를 공유)
          local term_evt_id="${task_id}-terminal"

          # Pure bash로 이미 run-task가 동일 ID 이벤트를 발행했는지 확인
          local evt_already_exists=false
          local existing_evt_file=""
          if [[ -f "$ACC_RUNTIME/events/pending/$term_evt_id.evt" ]]; then
            evt_already_exists=true
            existing_evt_file="$ACC_RUNTIME/events/pending/$term_evt_id.evt"
          else
            for f in "$ACC_RUNTIME/events/inflight"/*/"$term_evt_id.evt" "$ACC_RUNTIME/events/archive"/*/"$term_evt_id.evt"; do
              if [[ -f "$f" ]]; then
                evt_already_exists=true
                existing_evt_file="$f"
                break
              fi
            done
          fi

          local final_status="error"
          local killed_by="watchdog_reap"

          if [[ "$evt_already_exists" == "true" ]]; then
            emit_ok=true
            should_pulse_fifo=true
            local existing_evt_kind=""
            if [[ -f "$existing_evt_file" ]]; then
              while IFS= read -r k_line; do
                case "$k_line" in
                  kind=*) existing_evt_kind="${k_line#kind=}"; break ;;
                esac
              done < "$existing_evt_file"
            fi
            if [[ "$existing_evt_kind" == "done" || "$existing_evt_kind" == "canceled" ]]; then
              final_status="$existing_evt_kind"
              killed_by=""
            fi
          else
            if eval "\"$HERE/bin/event-emit.sh\" watchdog process_exit \"$task_id\" \
              \"Worker PID $pid disappeared unexpectedly\" \"$task_dir/output.log\" \"$term_evt_id\" ${term_fd}>&-"; then
              emit_ok=true
            fi
          fi

          if [[ "$emit_ok" == "true" ]]; then
            if write_task_state "$state_file" "$final_status" "$killed_by" "$term_evt_id" "$term_fd"; then
              state_committed=true
            fi
          else
            write_task_state "$state_file" "notification_error" "" "" "$term_fd" || true
          fi
        fi

        # Close terminal lock BEFORE pulse, logging, and busy marker cleanup
        flock -u "$term_fd" 2>/dev/null || true
        [[ -n "$term_fd" ]] && exec {term_fd}>&-
        term_fd=""

        if [[ "$state_committed" == "true" ]]; then
          if [[ "$should_pulse_fifo" == "true" && -p "$ACC_RUNTIME/event.fifo" ]]; then
            timeout 0.2 bash -c 'printf ".\n" >"$1"' _ "$ACC_RUNTIME/event.fifo" >/dev/null 2>&1 || true
          fi
          if [[ -f "$ACC_RUNTIME/workers/$worker.busy" ]]; then
            local b_line="" is_busy_match=false
            while IFS= read -r b_line; do
              if [[ "$b_line" == "$task_id" ]]; then
                is_busy_match=true
                break
              fi
            done < "$ACC_RUNTIME/workers/$worker.busy"
            if [[ "$is_busy_match" == "true" ]]; then
              rm -f "$ACC_RUNTIME/workers/$worker.busy"
            fi
          fi
        elif [[ "$is_term" == "false" || "$emit_ok" == "false" ]]; then
          log "Task $task_id process_exit 알림 발행 또는 상태 커밋 실패 — status=notification_error 유지"
        fi
      else
        [[ -n "$term_fd" ]] && exec {term_fd}>&-
      fi
      continue
    fi

    # 2. Deadline 초과 검사
    if [[ -n "$deadline_epoch" ]] && (( now > deadline_epoch )); then
      if [[ ! -f "$task_dir/deadline.notified" ]]; then
        log "Task $task_id 마감시간 초과 (started: $started_epoch, deadline: $deadline_epoch)"
        if [[ "$WATCHDOG_AUTO_KILL" == "true" ]]; then
          local term_fd=""
          exec {term_fd}>"$task_dir/terminal.lock"
          if flock -n "$term_fd"; then
            local is_term=false
            local state_committed=false
            local emit_ok=false
            local should_pulse_fifo=false
            local existing_term_id=""
            if [[ -f "$state_file" ]]; then
              while IFS= read -r s_line; do
                case "$s_line" in
                  status=done*|status=error*|status=canceled*) is_term=true ;;
                  terminal_event_id=*) existing_term_id="${s_line#terminal_event_id=}" ;;
                esac
              done < "$state_file"
            fi

            # Auto-repair: status가 running인데 terminal_event_id가 이미 존재하는 비정상 상태 복구
            if [[ "$is_term" == "false" && -n "$existing_term_id" ]]; then
              if write_task_state "$state_file" "error" "watchdog" "$existing_term_id" "$term_fd"; then
                is_term=true
                state_committed=true
              else
                is_term=true
                state_committed=false
              fi
            fi

            if [[ "$is_term" == "false" ]]; then
              # Worker group 강제 종료
              kill -9 -- "-$pgid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true

              local term_evt_id="${task_id}-terminal"
              local evt_already_exists=false
              if [[ -f "$ACC_RUNTIME/events/pending/$term_evt_id.evt" ]]; then
                evt_already_exists=true
              else
                for f in "$ACC_RUNTIME/events/inflight"/*/"$term_evt_id.evt" "$ACC_RUNTIME/events/archive"/*/"$term_evt_id.evt"; do
                  if [[ -f "$f" ]]; then
                    evt_already_exists=true
                    break
                  fi
                done
              fi

              if [[ "$evt_already_exists" == "true" ]]; then
                emit_ok=true
                should_pulse_fifo=true
              else
                if eval "\"$HERE/bin/event-emit.sh\" watchdog error \"$task_id\" \
                  \"Task exceeded deadline ($DEFAULT_TASK_TIMEOUT s) and was terminated\" \"$task_dir/output.log\" \"$term_evt_id\" ${term_fd}>&-"; then
                  emit_ok=true
                fi
              fi

              if [[ "$emit_ok" == "true" ]]; then
                if write_task_state "$state_file" "error" "watchdog" "$term_evt_id" "$term_fd"; then
                  state_committed=true
                fi
              else
                write_task_state "$state_file" "notification_error" "" "" "$term_fd" || true
              fi
            fi

            # Close terminal lock BEFORE pulse, logging, and busy marker cleanup
            flock -u "$term_fd" 2>/dev/null || true
            [[ -n "$term_fd" ]] && exec {term_fd}>&-
            term_fd=""

            if [[ "$state_committed" == "true" ]]; then
              if [[ "$should_pulse_fifo" == "true" && -p "$ACC_RUNTIME/event.fifo" ]]; then
                timeout 0.2 bash -c 'printf ".\n" >"$1"' _ "$ACC_RUNTIME/event.fifo" >/dev/null 2>&1 || true
              fi
              touch "$task_dir/deadline.notified"
              if [[ -f "$ACC_RUNTIME/workers/$worker.busy" ]]; then
                local b_line="" is_busy_match=false
                while IFS= read -r b_line; do
                  if [[ "$b_line" == "$task_id" ]]; then
                    is_busy_match=true
                    break
                  fi
                done < "$ACC_RUNTIME/workers/$worker.busy"
                if [[ "$is_busy_match" == "true" ]]; then
                  rm -f "$ACC_RUNTIME/workers/$worker.busy"
                fi
              fi
            elif [[ "$is_term" == "true" ]]; then
              # 이전에 이미 정상적으로 terminal 상태에 도달해 있던 경우에만 marker 기록
              local cur_s=""
              if [[ -f "$state_file" ]]; then
                while IFS= read -r chk_l; do
                  case "$chk_l" in
                    status=done*|status=error*|status=canceled*) cur_s="term"; break ;;
                  esac
                done < "$state_file"
              fi
              if [[ "$cur_s" == "term" ]]; then
                touch "$task_dir/deadline.notified"
              fi
            else
              log "Task $task_id deadline 알림 발행 또는 상태 커밋 실패 — status=notification_error 유지"
            fi
          else
            [[ -n "$term_fd" ]] && exec {term_fd}>&-
          fi
        else
          if "$HERE/bin/event-emit.sh" watchdog stalled "$task_id" \
            "Task exceeded deadline ($DEFAULT_TASK_TIMEOUT s) but remains running" "$task_dir/output.log" "${task_id}-deadline-warn"; then
            touch "$task_dir/deadline.notified"
          fi
        fi
      fi
    fi

    # 3. No-output Stall 검사 (출력 정체)
    local log_file="$task_dir/output.log"
    if [[ -f "$log_file" && ! -f "$task_dir/stall.notified" ]]; then
      local log_mtime
      log_mtime="$(stat -c %Y "$log_file" 2>/dev/null || echo "$now")"
      local idle_seconds=$(( now - log_mtime ))
      if (( idle_seconds >= NO_OUTPUT_WARN_SECONDS )); then
        log "Task $task_id 출력 장기 정체 감지 (${idle_seconds}s >= ${NO_OUTPUT_WARN_SECONDS}s)"
        if "$HERE/bin/event-emit.sh" watchdog stalled "$task_id" \
          "No output produced for ${idle_seconds}s (exceeds ${NO_OUTPUT_WARN_SECONDS}s threshold)" "$log_file" "${task_id}-stall-warn"; then
          touch "$task_dir/stall.notified"
        fi
      fi
    fi
  done
}

# 4. ACK Timeout & Inflight 재전달 (claiming 및 awaiting_ack 분리 처리)
check_inflight_ack_timeouts() {
  local now
  now="$(date +%s)"
  local claim_timeout="${EVENT_CLAIM_TIMEOUT:-30}"

  for batch_dir in "$ACC_RUNTIME/events/inflight"/*; do
    [[ -d "$batch_dir" ]] || continue
    local batch_id claimed_at age
    batch_id="${batch_dir##*/}"
    local status="awaiting_ack"
    if [[ -f "$batch_dir/.metadata" ]]; then
      status="$(grep '^status=' "$batch_dir/.metadata" | cut -d= -f2- || echo "awaiting_ack")"
      claimed_at="$(grep '^claimed_at=' "$batch_dir/.metadata" | cut -d= -f2- || echo "$now")"
    else
      claimed_at="$(stat -c %Y "$batch_dir" 2>/dev/null || echo "$now")"
    fi
    age=$(( now - claimed_at ))

    local should_requeue=false
    if [[ "$status" == "claiming" ]] && (( age > claim_timeout )); then
      log "Inflight 배치 $batch_id claiming 타임아웃 (${age}s > ${claim_timeout}s) — pending 복귀"
      should_requeue=true
    elif [[ "$status" == "awaiting_ack" ]] && (( age > EVENT_ACK_TIMEOUT )); then
      log "Inflight 배치 $batch_id ACK 타임아웃 (${age}s > ${EVENT_ACK_TIMEOUT}s) — pending 재전달"
      should_requeue=true
    fi

    if [[ "$should_requeue" == "true" ]]; then
      local requeued=()
      while IFS= read -r -d '' evt; do
        requeued+=("$evt")
      done < <(find "$batch_dir" -maxdepth 1 -name '*.evt' -print0 2>/dev/null || true)

      if (( ${#requeued[@]} > 0 )); then
        mv "${requeued[@]}" "$ACC_RUNTIME/events/pending/"
        if [[ -p "$ACC_RUNTIME/event.fifo" ]]; then
          timeout 0.2 bash -c 'printf ".\n" >"$1"' _ "$ACC_RUNTIME/event.fifo" >/dev/null 2>&1 || true
        fi
      fi
      rm -rf "$batch_dir"
      rm -f "$ACC_RUNTIME/events/batch-$batch_id.txt"
    fi
  done

  # 고립된 outbox 임시 파일 복구 및 유효성 검증
  mkdir -p "$ACC_RUNTIME/events/quarantine"
  for tmp_evt in "$ACC_RUNTIME/events/pending"/.event.*.tmp; do
    [[ -f "$tmp_evt" ]] || continue
    local tmp_mtime
    tmp_mtime="$(stat -c %Y "$tmp_evt" 2>/dev/null || echo "$now")"
    if (( now - tmp_mtime > 10 )); then
      local name="${tmp_evt##*/}"
      local id="${name#.event.}"
      id="${id%.tmp}"

      # 필수 필드 존재 여부 검사
      local valid=true
      for field in id= source= kind= task_id= summary_b64= detail_path_b64=; do
        if ! grep -q "^$field" "$tmp_evt" 2>/dev/null; then
          valid=false
          break
        fi
      done

      # Base64 필드 디코딩 유효성 검사
      if [[ "$valid" == "true" ]]; then
        local sum_b64 path_b64
        sum_b64="$(sed -n 's/^summary_b64=//p' "$tmp_evt")"
        path_b64="$(sed -n 's/^detail_path_b64=//p' "$tmp_evt")"
        if ! printf '%s' "$sum_b64" | base64 -d >/dev/null 2>&1 || ! printf '%s' "$path_b64" | base64 -d >/dev/null 2>&1; then
          valid=false
        fi
      fi

      if [[ "$valid" == "true" ]]; then
        if mv "$tmp_evt" "$ACC_RUNTIME/events/pending/$id.evt" 2>/dev/null; then
          log "Recovered and promoted orphan event $id.evt to pending"
          if [[ -p "$ACC_RUNTIME/event.fifo" ]]; then
            timeout 0.2 bash -c 'printf ".\n" >"$1"' _ "$ACC_RUNTIME/event.fifo" >/dev/null 2>&1 || true
          fi
        else
          log "경고: orphan event $id.evt 승격 mv 실패"
        fi
      else
        if mv "$tmp_evt" "$ACC_RUNTIME/events/quarantine/$name" 2>/dev/null; then
          log "Quarantined corrupt or truncated orphan event file $name"
        fi
      fi
    fi
  done
}

# 5. 브리지 리스너 장애 및 비상 폴백 (자기증폭 방지 쿨다운 포함)
check_bridge_health() {
  local now
  now="$(date +%s)"
  local pending_dir="$ACC_RUNTIME/events/pending"

  # bridge_degraded 이벤트 제외한 일반 작업 이벤트 중 오래된 것 탐색
  local oldest_evt
  oldest_evt="$(find "$pending_dir" -maxdepth 1 -name '*.evt' ! -name '*-bridge-*' -type f -printf '%T@ %p\n' 2>/dev/null | sort -n | head -n 1)"
  if [[ -n "$oldest_evt" ]]; then
    local evt_time evt_file
    evt_time="${oldest_evt%%.*}"
    evt_file="${oldest_evt#* }"
    local age=$(( now - evt_time ))

    if (( age > EVENT_DELIVERY_GRACE )); then
      local listener_pid_file="$ACC_RUNTIME/listener.pid"
      local listener_alive=false
      if [[ -f "$listener_pid_file" ]]; then
        local l_pid
        l_pid="$(<"$listener_pid_file")"
        if kill -0 "$l_pid" 2>/dev/null; then
          listener_alive=true
        fi
      fi

      if [[ "$listener_alive" == "false" ]]; then
        # 쿨다운 검사 (최소 15분마다 1회만 주입하여 자기증폭 방지)
        local marker="$ACC_RUNTIME/emergency.notified"
        local should_notify=true
        if [[ -f "$marker" ]]; then
          local marker_time
          marker_time="$(<"$marker")"
          if (( now - marker_time < 900 )); then
            should_notify=false
          fi
        fi

        if [[ "$should_notify" == "true" ]]; then
          printf '%s\n' "$now" > "$marker"
          log "경고: 브리지 리스너 비활성 및 pending 이벤트 고립 (${age}s) — 비상 폴백 발동"
          local emergency_msg="[ACC_WATCHDOG_EMERGENCY] Push listener degraded. Pending events isolated for ${age}s. Check $pending_dir"
          tmux set-buffer "$emergency_msg" 2>/dev/null || true
          tmux paste-buffer -t "${SESSION_NAME}:${SONNET_WINDOW}" 2>/dev/null || true
          tmux send-keys -t "${SESSION_NAME}:${SONNET_WINDOW}" C-m 2>/dev/null || true

          "$HERE/bin/event-emit.sh" bridge bridge_degraded "system" \
            "Listener inactive, fallback paste-buffer triggered" "" || true
        fi
      fi
    fi
  fi
}

oneshot=false
for arg in "$@"; do
  case "$arg" in
    --oneshot) oneshot=true ;;
  esac
done

if [[ "$oneshot" == "true" ]]; then
  check_session_and_sonnet
  check_running_tasks
  check_inflight_ack_timeouts
  check_bridge_health
  exit 0
fi

while true; do
  check_session_and_sonnet
  check_running_tasks
  check_inflight_ack_timeouts
  check_bridge_health
  sleep "$WATCHDOG_INTERVAL"
done
