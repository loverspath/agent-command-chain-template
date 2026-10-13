#!/usr/bin/env bash
# bin/agy-stop-hook.sh: Antigravity CLI Stop 훅 핸들러
# agy 상주 TUI의 완료(fullyIdle=true 및 sentinel 확인) 감지 및 event-emit.sh 연동
# Fail-Closed 설계 원칙:
#   1. ACC_RUNTIME이 명시적으로 설정되어 있고 유효한 디렉토리이며 workers/agy.resident 마커가 존재할 때만 진행
#   2. workers/agy.resident의 pane id와 hook 환경의 TMUX_PANE이 일치할 때만 진행
#   3. 활성 작업 상태가 mode=resident 및 status=running 일 때만 완료 이벤트 발행
#   4. 센티널 기반 완료 판정: 마지막 MODEL 응답에서 [[DONE <task_id>]] 또는 [[BLOCKED <task_id>]] 검증
#   5. 센티널 부재 시 작업을 running 상태로 유지 (마감 시각 초과 시 timeout 발행)
#   6. 그 외 모든 예외/부적합 상황에서는 유효한 JSON {"decision":"allow"}를 출력하고 무작업(no-op) 안전 종료
set -uo pipefail
umask 077

# 1. Stdin JSON 페이로드 수신
raw_payload="$(cat)"
runtime="${ACC_RUNTIME:-}"
res_pane_id=""
mismatch_tag=""

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
  if head -n 5 "$trans_path" 2>/dev/null | grep -qE '<subagent_reminder>|is a subagent|sender=' 2>/dev/null; then
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

# transcript 경로를 상주 워커 파일에 기록 (watchdog 연동용)
if [[ -n "$trans_path" && -f "$trans_path" && -d "$runtime/workers" ]]; then
  printf '%s\n' "$trans_path" > "$runtime/workers/agy.transcript" 2>/dev/null || true
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

# 9. 에러/중단 여부 확인 (terminationReason 또는 error 필드)
is_term_error=false
final_kind="done"
summary="Task completed"
exit_code=0

case "$term_reason" in
  error|interrupted|INTERRUPTED|cancel|cancelled|canceled|user_cancel*|timeout|max_steps_exceeded)
    is_term_error=true
    final_kind="error"
    summary="Agy execution error or interrupted: ${err_msg:-$term_reason}"
    exit_code=1
    ;;
  *)
    if [[ -n "$err_msg" && "$err_msg" != "null" ]]; then
      is_term_error=true
      final_kind="error"
      summary="Agy execution error: $err_msg"
      exit_code=1
    fi
    ;;
esac

# 10. 정상 종료 시: 트랜스크립트 센티널 검사
if [[ "$is_term_error" == "false" ]]; then
  # trans_path 폴백 탐색 (payload에 없거나 유효하지 않은 경우)
  if [[ -z "$trans_path" || ! -f "$trans_path" ]]; then
    if [[ -n "$conv_id" ]]; then
      for cand in \
        "$HOME/.gemini/antigravity-cli/brain/$conv_id/.system_generated/logs/transcript_full.jsonl" \
        "$HOME/.gemini/antigravity-cli/brain/$conv_id/.system_generated/logs/transcript.jsonl" \
        "${REAL_HOME:-$HOME}/.gemini/antigravity-cli/brain/$conv_id/.system_generated/logs/transcript_full.jsonl" \
        "${REAL_HOME:-$HOME}/.gemini/antigravity-cli/brain/$conv_id/.system_generated/logs/transcript.jsonl"; do
        if [[ -f "$cand" ]]; then
          trans_path="$cand"
          break
        fi
      done
    fi
    if [[ -z "$trans_path" || ! -f "$trans_path" ]] && [[ -f "$runtime/workers/agy.transcript" ]]; then
      cand="$(head -n 1 "$runtime/workers/agy.transcript" 2>/dev/null | tr -d '\r\n')"
      if [[ -f "$cand" ]]; then
        trans_path="$cand"
      fi
    fi
  fi

  # started_epoch 추출 (state_file 우선, 없으면 busy_file 2번째 줄, 실패 시 0)
  started_epoch="$(sed -n 's/^started_epoch=//p' "$state_file" 2>/dev/null | head -n 1 | tr -d '\r\n')"
  if [[ -z "$started_epoch" || ! "$started_epoch" =~ ^[0-9]+$ ]]; then
    started_epoch="$(sed -n '2p' "$busy_file" 2>/dev/null | tr -d '\r\n')"
  fi
  if [[ ! "$started_epoch" =~ ^[0-9]+$ ]]; then
    started_epoch="0"
  fi

  # 센티널 파싱 (python3)
  sentinel_type="NONE"
  sentinel_rest=""
  sentinel_found_id=""
  if [[ -n "$trans_path" && -f "$trans_path" ]]; then
    {
      read -r sentinel_type || sentinel_type="NONE"
      read -r sentinel_rest || sentinel_rest=""
      read -r sentinel_found_id || sentinel_found_id=""
    } < <(python3 -c '
import sys, json, os, re
from datetime import datetime, timezone

def parse_created_at(ts_str):
    if not ts_str or not isinstance(ts_str, str):
        return None
    ts = ts_str.strip()
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None

trans_path = sys.argv[1]
task_id = sys.argv[2]
started_epoch = 0.0
if len(sys.argv) > 3:
    try:
        started_epoch = float(sys.argv[3])
    except Exception:
        started_epoch = 0.0

if not trans_path or not os.path.isfile(trans_path):
    print("NONE\n")
    sys.exit(0)

full_path = trans_path
if trans_path.endswith("transcript.jsonl"):
    cand = trans_path[:-len("transcript.jsonl")] + "transcript_full.jsonl"
    if os.path.isfile(cand):
        full_path = cand

last_model_text = None

try:
    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
        # Check first 5 lines for subagent indicator
        for _ in range(5):
            line = f.readline()
            if not line:
                break
            if any(k in line for k in ("<subagent_reminder>", "is a subagent", "sender=")):
                print("SUBAGENT\n")
                sys.exit(0)
        f.seek(0)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            stype = obj.get("type")
            source = obj.get("source")
            if stype in ("USER_INPUT", "SYSTEM_MESSAGE", "CHECKPOINT", "GENERIC"):
                continue
            if source == "MODEL" or stype == "PLANNER_RESPONSE":
                c = obj.get("content")
                if c and isinstance(c, str) and c.strip():
                    created_at_raw = obj.get("created_at")
                    created_epoch = parse_created_at(created_at_raw)
                    # 타임스탬프 가드: started_epoch 이전 생성된 모델 응답은 이전 작업 잔여물이므로 배제
                    if created_epoch is not None and started_epoch > 0 and created_epoch < started_epoch:
                        continue
                    last_model_text = c
except Exception:
    print("NONE\n")
    sys.exit(0)

if not last_model_text:
    print("NONE\n")
    sys.exit(0)

pattern = re.compile(r"^\s*\[\[(DONE|BLOCKED)\s+([a-zA-Z0-9_\-\.]+)\s*\]\](.*)$")
matched_type = None
matched_rest = ""
matched_id = ""

for line in reversed(last_model_text.splitlines()):
    m = pattern.match(line)
    if m:
        stype, tid, rest = m.group(1), m.group(2), m.group(3).strip()
        matched_type = stype
        matched_id = tid
        matched_rest = rest
        break

if matched_type:
    print(matched_type)
    print(matched_rest)
    print(matched_id)
else:
    print("NONE\n")
' "$trans_path" "$task_id" "$started_epoch" 2>/dev/null || echo "NONE")
  fi

  mismatch_tag=""
  if [[ "$sentinel_type" == "DONE" || "$sentinel_type" == "BLOCKED" ]]; then
    if [[ -n "$sentinel_found_id" && "$sentinel_found_id" != "$task_id" ]]; then
      mismatch_tag="sentinel_id_mismatch_accepted (found: $sentinel_found_id, current: $task_id)"
      if [[ "${ACC_HOOK_DEBUG:-0}" == "1" && -n "$runtime" && -d "$runtime" ]]; then
        printf '[%s] tag=%s\n' "$(date +%s)" "$mismatch_tag" >> "$runtime/hook-debug.log" 2>/dev/null || true
      fi
    fi
  fi

  case "$sentinel_type" in
    SUBAGENT)
      flock -u "$term_fd" 2>/dev/null || true
      [[ -n "$term_fd" ]] && exec {term_fd}>&-
      respond_and_exit "allow" "subagent_transcript"
      ;;
    DONE)
      final_kind="done"
      if [[ -n "$sentinel_rest" ]]; then
        summary="Task completed: $sentinel_rest"
      else
        summary="Task completed"
      fi
      exit_code=0
      ;;
    BLOCKED)
      final_kind="error"
      if [[ -n "$sentinel_rest" ]]; then
        summary="Task blocked: $sentinel_rest"
      else
        summary="Task blocked"
      fi
      exit_code=1
      ;;
    *)
      # 센티널 부재: deadline 확인
      now="$(date +%s)"
      deadline_epoch="$(sed -n 's/^deadline_epoch=//p' "$state_file" 2>/dev/null | head -n 1 | tr -d '\r\n')"
      if [[ -n "$deadline_epoch" && "$deadline_epoch" =~ ^[0-9]+$ ]] && (( now > deadline_epoch )); then
        # 기한 초과: timeout 에러 발행 및 종료
        final_kind="error"
        summary="Task deadline exceeded (${now} > ${deadline_epoch}): timeout"
        exit_code=124
      else
        # 기한 내 센티널 부재: 작업을 running 상태로 유지하고 정상 대기
        flock -u "$term_fd" 2>/dev/null || true
        [[ -n "$term_fd" ]] && exec {term_fd}>&-
        respond_and_exit "allow" "sentinel_missing_keep_running"
      fi
      ;;
  esac
fi

# 11. 완료 또는 에러 이벤트 발행
detail_path="${trans_path:-$task_dir/output.log}"
term_evt_id="${task_id}-terminal"

export ACC_RUNTIME="$runtime"
export ACC_EXIT_CODE="$exit_code"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# event-emit.sh 호출 (Durable Outbox 및 FIFO 펄스)
if eval "\"$HERE/bin/event-emit.sh\" agy \"$final_kind\" \"$task_id\" \"$summary\" \"$detail_path\" \"$term_evt_id\" ${term_fd}>&-"; then
  # 12. 작업 상태 갱신
  completed_epoch="$(date +%s)"
  tmp_state="$state_file.tmp.$$"
  {
    grep -vE '^(status|completed_epoch|terminal_event_id)=' "$state_file" 2>/dev/null || true
    printf 'status=%s\n' "$final_kind"
    printf 'completed_epoch=%s\n' "$completed_epoch"
    printf 'terminal_event_id=%s\n' "$term_evt_id"
  } > "$tmp_state"
  mv "$tmp_state" "$state_file"

  # 13. busy 파일 및 락 해제
  if [[ -f "$busy_file" ]] && grep -q "^$task_id$" "$busy_file" 2>/dev/null; then
    rm -f "$busy_file"
  fi
  rm -rf "$runtime/workers/agy.lock" 2>/dev/null || true
fi

flock -u "$term_fd" 2>/dev/null || true
[[ -n "$term_fd" ]] && exec {term_fd}>&-

if [[ -n "$mismatch_tag" ]]; then
  respond_and_exit "allow" "$mismatch_tag"
else
  respond_and_exit "allow" "event_emitted"
fi
