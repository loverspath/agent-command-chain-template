#!/usr/bin/env bash
# bin/agy-stop-hook.sh: Antigravity CLI Stop 훅 핸들러
# agy 상주 TUI의 완료(fullyIdle=true) 감지 및 event-emit.sh 연동
# Fail-Closed 설계 원칙:
#   1. ACC_RUNTIME이 명시적으로 설정되어 있고 유효한 디렉토리이며 workers/agy.resident 마커가 존재할 때만 진행
#   2. workers/agy.resident의 pane id와 hook 환경의 TMUX_PANE이 일치할 때만 진행
#   3. 활성 작업 상태가 mode=resident 및 status=running 일 때만 완료 이벤트 발행
#   4. 그 외 모든 예외/부적합 상황에서는 유효한 JSON {"decision":"allow"}를 출력하고 무작업(no-op) 안전 종료
set -uo pipefail
umask 077

# 1. Stdin JSON 페이로드 수신
raw_payload="$(cat)"
runtime="${ACC_RUNTIME:-}"
res_pane_id=""

debug_log() {
  local decision="${1:-allow}"
  local reason_tag="${2:-none}"
  if [[ "${ACC_HOOK_DEBUG:-0}" == "1" && -n "$runtime" && -d "$runtime" ]]; then
    printf '[%s] pane=%s res_pane=%s fullyIdle=%s termReason=%s decision=%s tag=%s trans=%s\n' \
      "$(date +%s)" "${TMUX_PANE:-}" "${res_pane_id:-none}" "${fully_idle:-unknown}" "${term_reason:-none}" "$decision" "$reason_tag" "${trans_path:-none}" >> "$runtime/hook-debug.log" 2>/dev/null || true
  fi
}

# 항상 유효한 JSON 응답을 stdout으로 출력해야 함
respond_and_exit() {
  local decision="${1:-allow}"
  local reason_tag="${2:-none}"
  debug_log "$decision" "$reason_tag"
  printf '{"decision": "%s"}\n' "$decision"
  exit 0
}

# 빈 페이로드 방어
if [[ -z "$raw_payload" ]]; then
  respond_and_exit "allow" "empty_payload"
fi

# 2. 페이로드 파싱 (python3) - IFS 빈 필드 축약 버그 방지를 위해 줄 단위 read
# protojson 필드: conversationId, fullyIdle, terminationReason, error, transcriptPath, artifactDirectoryPath
{
  read -r fully_idle || fully_idle="false"
  read -r term_reason || term_reason=""
  read -r err_msg || err_msg=""
  read -r trans_path || trans_path=""
  read -r conv_id || conv_id=""
} < <(python3 -c '
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print("true" if d.get("fullyIdle") is True else "false")
    print(str(d.get("terminationReason", "") or ""))
    print(str(d.get("error", "") or ""))
    print(str(d.get("transcriptPath", "") or ""))
    print(str(d.get("conversationId", "") or ""))
except Exception:
    print("false\n\n\n\n")
' <<< "$raw_payload")

# 3. fullyIdle 검사: 백그라운드 서브에이전트 구동 중이면 false -> 대기
if [[ "$fully_idle" != "true" ]]; then
  respond_and_exit "allow" "not_fully_idle"
fi

# 4. 서브에이전트 트랜스크립트인지 검사 (메인 에이전트만 완료 이벤트 발행)
# 서브에이전트 트랜스크립트는 첫머리(시스템 프롬프트, Step 0)에 <subagent_reminder> 또는 sender=를 포함함
if [[ -n "$trans_path" && -f "$trans_path" ]]; then
  if head -n 1 "$trans_path" 2>/dev/null | grep -qE '<subagent_reminder>|is a subagent|sender=' 2>/dev/null; then
    respond_and_exit "allow" "subagent_transcript"
  fi
fi

# 5. 런타임 및 상주 마커 fail-closed 검증 (Requirement 2.1a)
# Stop hook은 런타임을 절대 추측하지 않음 (세션/런타임 자동 해석 폴백 제거)
runtime="${ACC_RUNTIME:-}"
if [[ -z "$runtime" || ! -d "$runtime" ]]; then
  respond_and_exit "allow" "no_runtime"
fi

resident_marker="$runtime/workers/agy.resident"
if [[ ! -f "$resident_marker" ]]; then
  respond_and_exit "allow" "no_resident_marker"
fi

# 6. TMUX_PANE 일치 검증 (Requirement 2.1b)
# 상주 윈도우 agy 프로세스에서 발행된 Stop hook인지 검증
res_pane_id="$(sed -n 's/^pane_id=//p' "$resident_marker" 2>/dev/null | head -n 1 | tr -d '\r\n')"
res_session="$(sed -n -E 's/^(session|session_name)=//p' "$resident_marker" 2>/dev/null | head -n 1 | tr -d '\r\n')"
cur_pane="${TMUX_PANE:-}"
if [[ -z "$cur_pane" || -z "$res_pane_id" || "$cur_pane" != "$res_pane_id" ]]; then
  respond_and_exit "allow" "pane_mismatch"
fi

busy_file="$runtime/workers/agy.busy"
if [[ ! -f "$busy_file" ]]; then
  # 현재 할당된 작업이 없음 (/clear 또는 직접 대화 등) -> fake done 방지
  respond_and_exit "allow" "no_busy_file"
fi

task_id="$(head -n 1 "$busy_file" 2>/dev/null | tr -d '\r\n')"
if [[ -z "$task_id" ]]; then
  respond_and_exit "allow" "empty_task_id"
fi

task_dir="$runtime/tasks/$task_id"
state_file="$task_dir/state"
if [[ ! -f "$state_file" ]]; then
  respond_and_exit "allow" "no_state_file"
fi

# 7. 작업 모드 및 상태 확인 (Requirement 2.1c: mode=resident 및 status=running)
# oneshot 작업이나 이미 완료/종료된 작업에 대한 오반응 방지
if ! grep -q '^mode=resident' "$state_file" 2>/dev/null; then
  respond_and_exit "allow" "not_resident_mode"
fi

if ! grep -q '^status=running' "$state_file" 2>/dev/null; then
  respond_and_exit "allow" "not_running_status"
fi

# 8. 원자적 멱등성 보장 (terminal.lock)
term_fd=""
exec {term_fd}>"$task_dir/terminal.lock"
if ! flock -n "$term_fd"; then
  # 다른 프로세스나 중복 훅이 이미 커밋 중
  [[ -n "$term_fd" ]] && exec {term_fd}>&-
  respond_and_exit "allow" "lock_conflict"
fi

# 락 획득 후 재검사
if ! grep -q '^mode=resident' "$state_file" 2>/dev/null || ! grep -q '^status=running' "$state_file" 2>/dev/null; then
  flock -u "$term_fd" 2>/dev/null || true
  [[ -n "$term_fd" ]] && exec {term_fd}>&-
  respond_and_exit "allow" "status_changed_under_lock"
fi

# 9. 성공/에러 판정 및 이벤트 발행
final_kind="done"
summary="Task completed"
exit_code=0

case "$term_reason" in
  error|interrupted|INTERRUPTED|cancel|cancelled|canceled|user_cancel*|timeout|max_steps_exceeded)
    final_kind="error"
    summary="Agy execution error or interrupted: ${err_msg:-$term_reason}"
    exit_code=1
    ;;
  *)
    if [[ -n "$err_msg" && "$err_msg" != "null" ]]; then
      final_kind="error"
      summary="Agy execution error: $err_msg"
      exit_code=1
    fi
    ;;
esac

detail_path="${trans_path:-$task_dir/output.log}"
term_evt_id="${task_id}-terminal"

export ACC_RUNTIME="$runtime"
export ACC_EXIT_CODE="$exit_code"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# event-emit.sh 호출 (Durable Outbox 및 FIFO 펄스)
if eval "\"$HERE/bin/event-emit.sh\" agy \"$final_kind\" \"$task_id\" \"$summary\" \"$detail_path\" \"$term_evt_id\" ${term_fd}>&-"; then
  # 10. 작업 상태 갱신
  completed_epoch="$(date +%s)"
  tmp_state="$state_file.tmp.$$"
  {
    grep -vE '^(status|completed_epoch|terminal_event_id)=' "$state_file" 2>/dev/null || true
    printf 'status=%s\n' "$final_kind"
    printf 'completed_epoch=%s\n' "$completed_epoch"
    printf 'terminal_event_id=%s\n' "$term_evt_id"
  } > "$tmp_state"
  mv "$tmp_state" "$state_file"

  # 11. busy 파일 및 락 해제
  if [[ -f "$busy_file" ]] && grep -q "^$task_id$" "$busy_file" 2>/dev/null; then
    rm -f "$busy_file"
  fi
  rm -rf "$runtime/workers/agy.lock" 2>/dev/null || true
fi

flock -u "$term_fd" 2>/dev/null || true
[[ -n "$term_fd" ]] && exec {term_fd}>&-

respond_and_exit "allow" "event_emitted"
