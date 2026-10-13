#!/usr/bin/env bash
# tests/stop-hook-test.sh: agy-stop-hook.sh 단위 및 fail-closed 검증 (센티널 기반 완료 판정 포함)
# 19개 테스트 케이스:
#   (1) fullyIdle=false -> 0 events emitted
#   (2) resident running + fullyIdle=true + NO sentinel (서브에이전트 위임 직후) -> 0 events, state=running, busy 유지 (Scenario ①)
#   (3) resident running + fullyIdle=true + sentinel [[DONE <task_id>]] -> exactly 1 done event, state=done, busy 삭제 (Scenario ②)
#   (4) duplicate call with same completed task -> still exactly 1 event (idempotency) (Scenario ③)
#   (5) 지시서 본문 인용 [[DONE <task_id>]] (USER_INPUT) -> 0 events, state=running, busy 유지 (Scenario ④)
#   (6) [[BLOCKED <task_id>]] -> exactly 1 error event, state=error, busy 삭제 (Scenario ⑤)
#   (7) error / interrupted terminationReason -> exactly 1 error event without sentinel (Scenario ⑥)
#   (8) deadline 초과 후 센티널 없는 Stop -> timeout error 발행, state=error, busy 삭제 (Scenario ⑦)
#   (9) subagent transcript -> 0 events, allow (Scenario ⑧)
#   (10) TMUX_PANE mismatch with workers/agy.resident -> 0 events, allow (Scenario ⑧)
#   (11) marker missing, empty pane_id, or corrupted -> 0 events, allow (Scenario ⑧)
#   (12) ACC_RUNTIME unset -> no-op, outputs valid JSON {"decision":"allow"}, modifies zero files
#   (13) oneshot task -> 0 events emitted, state unchanged
#   (14) no busy recorded (/clear scenario) -> no-op, valid JSON returned
#   (15) broken JSON or empty stdin -> valid JSON response, no-op
#   (16) ACC_HOOK_DEBUG=1 -> appends debug trace to $ACC_RUNTIME/hook-debug.log
#   (17) 이름 ID 센티널 ([[DONE some-named-task]]) -> done 1회 발행, state=done, busy 삭제, mismatch 태그 확인
#   (18) 이름 ID BLOCKED ([[BLOCKED named-task]]) -> error 1회 발행, state=error, busy 삭제, mismatch 태그 확인
#   (19) 이전 작업의 오래된 센티널 (created_at < started_epoch) -> 미발행, running 유지, busy 유지

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "stop-hook"
setup_mock_runtime "$TEST_RUNTIME"

PASSED=0
FAILED=0

pass_case() {
  local num="$1"
  local desc="$2"
  echo "  [PASS] Case $num: $desc"
  PASSED=$((PASSED + 1))
}

fail_case() {
  local num="$1"
  local desc="$2"
  local reason="${3:-unknown}"
  echo "  [FAIL] Case $num: $desc (Reason: $reason)"
  FAILED=$((FAILED + 1))
}

echo "=================================================================="
echo "Running stop-hook-test.sh (Marker: $TESTMARK)"
echo "Target runtime: $TEST_RUNTIME"
echo "=================================================================="

# Helper: create task directory and state
create_mock_task() {
  local task_id="$1"
  local mode="${2:-resident}"
  local status="${3:-running}"
  local deadline_offset="${4:-1800}"
  local t_dir="$TEST_RUNTIME/tasks/$task_id"
  mkdir -p "$t_dir"
  cat > "$t_dir/state" <<EOF
task_id=$task_id
worker=agy
status=$status
mode=$mode
pid=$$
pgid=$$
proc_token=$$
started_epoch=$(date +%s)
deadline_epoch=$(( $(date +%s) + deadline_offset ))
EOF
  printf '%s\n%s\n' "$task_id" "$(date +%s)" > "$TEST_RUNTIME/workers/agy.busy"
}

# Helper: setup resident marker
setup_resident_marker() {
  local pane_id="${1:-%1}"
  printf 'pane_id=%s\nsession_name=%s\n' "$pane_id" "$TEST_SESSION" > "$TEST_RUNTIME/workers/agy.resident"
}

# ------------------------------------------------------------------
# Case 1: fullyIdle=false -> 0 events emitted
# ------------------------------------------------------------------
c1_task="task-c1-${RAND_HEX}"
create_mock_task "$c1_task" "resident" "running"
setup_resident_marker "%1"
c1_payload="{\"fullyIdle\": false, \"conversationId\": \"c1\", \"testmark\": \"$TESTMARK\"}"
c1_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c1_payload" 2>/dev/null || true)
c1_evts=$(find "$TEST_RUNTIME/events" -name "${c1_task}*.evt" 2>/dev/null | wc -l)
c1_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c1_task/state" 2>/dev/null | cut -d= -f2- || true)

if [[ "$c1_out" == *'{"decision": "allow"}'* || "$c1_out" == *'{"decision":"allow"}'* ]] && \
   (( c1_evts == 0 )) && [[ "$c1_status" == "running" ]]; then
  pass_case 1 "fullyIdle=false -> 0 events emitted, state unchanged"
else
  fail_case 1 "fullyIdle=false" "evts=$c1_evts, status=$c1_status, out=$c1_out"
fi

# ------------------------------------------------------------------
# Case 2: Scenario ① 센티널 없는 Stop (서브에이전트 위임 직후) -> 이벤트 미발행, running 유지, busy 유지
# ------------------------------------------------------------------
c2_task="task-c2-${RAND_HEX}"
create_mock_task "$c2_task" "resident" "running"
setup_resident_marker "%1"
c2_trans="$TEST_TMP/c2_transcript.jsonl"
cat > "$c2_trans" <<EOF
{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Start work on task"}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "I am invoking a subagent now."}
EOF
c2_payload="{\"fullyIdle\": true, \"conversationId\": \"c2\", \"transcriptPath\": \"$c2_trans\", \"testmark\": \"$TESTMARK\"}"
c2_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c2_payload" 2>/dev/null || true)
c2_evts=$(find "$TEST_RUNTIME/events" -name "${c2_task}*.evt" 2>/dev/null | wc -l)
c2_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c2_task/state" 2>/dev/null | cut -d= -f2- || true)
c2_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c2_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c2_busy_exists=true
fi

if [[ "$c2_out" == *'{"decision": "allow"}'* || "$c2_out" == *'{"decision":"allow"}'* ]] && \
   (( c2_evts == 0 )) && [[ "$c2_status" == "running" ]] && [[ "$c2_busy_exists" == "true" ]]; then
  pass_case 2 "센티널 없는 Stop (서브에이전트 위임 직후) -> 이벤트 미발행, running 유지, busy 유지 (Scenario ①)"
else
  fail_case 2 "센티널 없는 Stop" "evts=$c2_evts, status=$c2_status, busy_exists=$c2_busy_exists, out=$c2_out"
fi

# ------------------------------------------------------------------
# Case 3: Scenario ② 센티널 있는 최종 Stop -> done 이벤트 1회만, state done, busy 삭제
# ------------------------------------------------------------------
c3_trans="$TEST_TMP/c3_transcript.jsonl"
cat > "$c3_trans" <<EOF
{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Start work on task"}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Working on it."}
{"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Final report here.\n[[DONE $c2_task]] result=all tests pass | learning=none"}
EOF
c3_payload="{\"fullyIdle\": true, \"conversationId\": \"c2\", \"transcriptPath\": \"$c3_trans\", \"testmark\": \"$TESTMARK\"}"
c3_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c3_payload" 2>/dev/null || true)
c3_evts=$(find "$TEST_RUNTIME/events" -name "${c2_task}*.evt" 2>/dev/null | wc -l)
c3_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c2_task/state" 2>/dev/null | cut -d= -f2- || true)
c3_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c2_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c3_busy_exists=true
fi
c3_lock_ok=false
if flock -n "$TEST_RUNTIME/tasks/$c2_task/terminal.lock" true 2>/dev/null; then
  c3_lock_ok=true
fi

if [[ "$c3_out" == *'{"decision": "allow"}'* || "$c3_out" == *'{"decision":"allow"}'* ]] && \
   (( c3_evts == 1 )) && [[ "$c3_status" == "done" ]] && [[ "$c3_busy_exists" == "false" ]] && [[ "$c3_lock_ok" == "true" ]]; then
  pass_case 3 "센티널 있는 최종 Stop -> done 이벤트 1회만, state done, busy 삭제 (Scenario ②)"
else
  fail_case 3 "센티널 있는 최종 Stop" "evts=$c3_evts, status=$c3_status, busy_exists=$c3_busy_exists, lock_ok=$c3_lock_ok"
fi

# ------------------------------------------------------------------
# Case 4: Scenario ③ 같은 task 중복 Stop -> 멱등성 (1회만 발행)
# ------------------------------------------------------------------
c4_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c3_payload" 2>/dev/null || true)
c4_evts=$(find "$TEST_RUNTIME/events" -name "${c2_task}*.evt" 2>/dev/null | wc -l)
c4_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c2_task/state" 2>/dev/null | cut -d= -f2- || true)

if [[ "$c4_out" == *'{"decision": "allow"}'* || "$c4_out" == *'{"decision":"allow"}'* ]] && \
   (( c4_evts == 1 )) && [[ "$c4_status" == "done" ]]; then
  pass_case 4 "같은 task 중복 Stop -> 멱등성 (여전히 1회만 발행) (Scenario ③)"
else
  fail_case 4 "중복 Stop 멱등성" "evts=$c4_evts, status=$c4_status"
fi

# ------------------------------------------------------------------
# Case 5: Scenario ④ 지시서 본문 인용 [[DONE <task_id>]] (USER_INPUT) -> 미발행
# ------------------------------------------------------------------
c5_task="task-c5-${RAND_HEX}"
create_mock_task "$c5_task" "resident" "running"
setup_resident_marker "%1"
c5_trans="$TEST_TMP/c5_transcript.jsonl"
cat > "$c5_trans" <<EOF
{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "주의사항: 작업 완료 시 반드시 [[DONE $c5_task]] result=...를 출력해야 함."}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "작업을 시작합니다. 지시서를 확인했습니다."}
EOF
c5_payload="{\"fullyIdle\": true, \"conversationId\": \"c5\", \"transcriptPath\": \"$c5_trans\", \"testmark\": \"$TESTMARK\"}"
c5_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c5_payload" 2>/dev/null || true)
c5_evts=$(find "$TEST_RUNTIME/events" -name "${c5_task}*.evt" 2>/dev/null | wc -l)
c5_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c5_task/state" 2>/dev/null | cut -d= -f2- || true)
c5_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c5_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c5_busy_exists=true
fi

if [[ "$c5_out" == *'{"decision": "allow"}'* || "$c5_out" == *'{"decision":"allow"}'* ]] && \
   (( c5_evts == 0 )) && [[ "$c5_status" == "running" ]] && [[ "$c5_busy_exists" == "true" ]]; then
  pass_case 5 "지시서 본문 인용 [[DONE <task_id>]] (USER_INPUT) -> 미발행, running 유지 (Scenario ④)"
else
  fail_case 5 "지시서 본문 인용 오탐 방지" "evts=$c5_evts, status=$c5_status, busy_exists=$c5_busy_exists"
fi

# ------------------------------------------------------------------
# Case 6: Scenario ⑤ [[BLOCKED ...]] -> 적절한 종류(error) 1회 발행
# ------------------------------------------------------------------
c6_task="task-c6-${RAND_HEX}"
create_mock_task "$c6_task" "resident" "running"
setup_resident_marker "%1"
c6_trans="$TEST_TMP/c6_transcript.jsonl"
cat > "$c6_trans" <<EOF
{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "작업 수행"}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "외부 API 접근 불가로 진행할 수 없음.\n[[BLOCKED $c6_task]] reason=API 인증 실패"}
EOF
c6_payload="{\"fullyIdle\": true, \"conversationId\": \"c6\", \"transcriptPath\": \"$c6_trans\", \"testmark\": \"$TESTMARK\"}"
c6_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c6_payload" 2>/dev/null || true)
c6_evts=$(find "$TEST_RUNTIME/events" -name "${c6_task}*.evt" 2>/dev/null | wc -l)
c6_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c6_task/state" 2>/dev/null | cut -d= -f2- || true)
c6_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c6_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c6_busy_exists=true
fi
c6_evt_kind=""
for ef in "$TEST_RUNTIME"/events/pending/${c6_task}*.evt; do
  if [[ -f "$ef" ]]; then
    c6_evt_kind=$(grep '^kind=' "$ef" | cut -d= -f2- || true)
    break
  fi
done

if [[ "$c6_out" == *'{"decision": "allow"}'* || "$c6_out" == *'{"decision":"allow"}'* ]] && \
   (( c6_evts == 1 )) && [[ "$c6_status" == "error" ]] && [[ "$c6_evt_kind" == "error" ]] && [[ "$c6_busy_exists" == "false" ]]; then
  pass_case 6 "[[BLOCKED ...]] -> error 이벤트 1회 발행, state=error, busy 삭제 (Scenario ⑤)"
else
  fail_case 6 "[[BLOCKED]] handling" "evts=$c6_evts, status=$c6_status, kind=$c6_evt_kind, busy_exists=$c6_busy_exists"
fi

# ------------------------------------------------------------------
# Case 7: Scenario ⑥ 에러/중단 terminationReason -> 센티널 없이도 error 즉시 발행
# ------------------------------------------------------------------
c7_task="task-c7-${RAND_HEX}"
create_mock_task "$c7_task" "resident" "running"
setup_resident_marker "%1"
c7_payload="{\"fullyIdle\": true, \"conversationId\": \"c7\", \"error\": \"Crash detected $TESTMARK\", \"terminationReason\": \"error\"}"
c7_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c7_payload" 2>/dev/null || true)
c7_evts=$(find "$TEST_RUNTIME/events" -name "${c7_task}*.evt" 2>/dev/null | wc -l)
c7_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c7_task/state" 2>/dev/null | cut -d= -f2- || true)
c7_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c7_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c7_busy_exists=true
fi

if [[ "$c7_out" == *'{"decision": "allow"}'* || "$c7_out" == *'{"decision":"allow"}'* ]] && \
   (( c7_evts == 1 )) && [[ "$c7_status" == "error" ]] && [[ "$c7_busy_exists" == "false" ]]; then
  pass_case 7 "에러/중단 terminationReason -> 센티널 없이도 error 즉시 발행 (Scenario ⑥)"
else
  fail_case 7 "terminationReason=error handling" "evts=$c7_evts, status=$c7_status, busy_exists=$c7_busy_exists"
fi

# ------------------------------------------------------------------
# Case 8: Scenario ⑦ deadline 초과 후 센티널 없는 Stop -> timeout error 발행
# ------------------------------------------------------------------
c8_task="task-c8-${RAND_HEX}"
# deadline_offset = -60 (이미 60초 전 초과)
create_mock_task "$c8_task" "resident" "running" "-60"
setup_resident_marker "%1"
c8_trans="$TEST_TMP/c8_transcript.jsonl"
cat > "$c8_trans" <<EOF
{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "작업 지시"}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "아직 진행 중..."}
EOF
c8_payload="{\"fullyIdle\": true, \"conversationId\": \"c8\", \"transcriptPath\": \"$c8_trans\", \"testmark\": \"$TESTMARK\"}"
c8_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c8_payload" 2>/dev/null || true)
c8_evts=$(find "$TEST_RUNTIME/events" -name "${c8_task}*.evt" 2>/dev/null | wc -l)
c8_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c8_task/state" 2>/dev/null | cut -d= -f2- || true)
c8_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c8_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c8_busy_exists=true
fi
c8_evt_kind=""
for ef in "$TEST_RUNTIME"/events/pending/${c8_task}*.evt; do
  if [[ -f "$ef" ]]; then
    c8_evt_kind=$(grep '^kind=' "$ef" | cut -d= -f2- || true)
    break
  fi
done

if [[ "$c8_out" == *'{"decision": "allow"}'* || "$c8_out" == *'{"decision":"allow"}'* ]] && \
   (( c8_evts == 1 )) && [[ "$c8_status" == "error" ]] && [[ "$c8_evt_kind" == "error" ]] && [[ "$c8_busy_exists" == "false" ]]; then
  pass_case 8 "deadline 초과 후 센티널 없는 Stop -> timeout error 발행 (Scenario ⑦)"
else
  fail_case 8 "deadline timeout handling" "evts=$c8_evts, status=$c8_status, kind=$c8_evt_kind, busy_exists=$c8_busy_exists"
fi

# ------------------------------------------------------------------
# Case 9: Scenario ⑧-A 서브에이전트 transcript -> 0 events, allow
# ------------------------------------------------------------------
c9_task="task-c9-${RAND_HEX}"
create_mock_task "$c9_task" "resident" "running"
setup_resident_marker "%1"
c9_trans="$TEST_TMP/subagent_transcript.jsonl"
printf '{"source": "SYSTEM", "content": "sender=3cff7708-subagent testmark=%s"}\n' "$TESTMARK" > "$c9_trans"
c9_payload="{\"fullyIdle\": true, \"conversationId\": \"c9\", \"transcriptPath\": \"$c9_trans\", \"testmark\": \"$TESTMARK\"}"
c9_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c9_payload" 2>/dev/null || true)
c9_evts=$(find "$TEST_RUNTIME/events" -name "${c9_task}*.evt" 2>/dev/null | wc -l)
c9_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c9_task/state" 2>/dev/null | cut -d= -f2- || true)

if [[ "$c9_out" == *'{"decision": "allow"}'* || "$c9_out" == *'{"decision":"allow"}'* ]] && \
   (( c9_evts == 0 )) && [[ "$c9_status" == "running" ]]; then
  pass_case 9 "서브에이전트 transcript -> 무작업 allow, 0 events (Scenario ⑧-A)"
else
  fail_case 9 "서브에이전트 transcript filtering" "evts=$c9_evts, status=$c9_status"
fi

# ------------------------------------------------------------------
# Case 10: Scenario ⑧-B TMUX_PANE mismatch with workers/agy.resident -> 0 events, allow
# ------------------------------------------------------------------
c10_task="task-c10-${RAND_HEX}"
create_mock_task "$c10_task" "resident" "running"
setup_resident_marker "%1"
c10_payload="{\"fullyIdle\": true, \"conversationId\": \"c10\", \"testmark\": \"$TESTMARK\"}"
c10_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%99" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c10_payload" 2>/dev/null || true)
c10_evts=$(find "$TEST_RUNTIME/events" -name "${c10_task}*.evt" 2>/dev/null | wc -l)
c10_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c10_task/state" 2>/dev/null | cut -d= -f2- || true)
c10_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c10_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c10_busy_exists=true
fi

if [[ "$c10_out" == *'{"decision": "allow"}'* || "$c10_out" == *'{"decision":"allow"}'* ]] && \
   (( c10_evts == 0 )) && [[ "$c10_status" == "running" ]] && [[ "$c10_busy_exists" == "true" ]]; then
  pass_case 10 "TMUX_PANE mismatch -> fail-closed no-op allow (Scenario ⑧-B)"
else
  fail_case 10 "TMUX_PANE mismatch fail-closed" "evts=$c10_evts, status=$c10_status, busy_exists=$c10_busy_exists"
fi

# ------------------------------------------------------------------
# Case 11: Scenario ⑧-C 상주 마커 부재, pane_id 누락, 포맷 오염 -> 0 events, allow
# ------------------------------------------------------------------
c11_task="task-c11-${RAND_HEX}"
create_mock_task "$c11_task" "resident" "running"
# 11a: 마커 파일 부재
rm -f "$TEST_RUNTIME/workers/agy.resident"
c11_payload="{\"fullyIdle\": true, \"conversationId\": \"c11\", \"testmark\": \"$TESTMARK\"}"
c11_out1=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c11_payload" 2>/dev/null || true)
c11_evts1=$(find "$TEST_RUNTIME/events" -name "${c11_task}*.evt" 2>/dev/null | wc -l)

# 11b: pane_id 값 누락
printf 'pane_id=\nsession=%s\n' "$TEST_SESSION" > "$TEST_RUNTIME/workers/agy.resident"
c11_out2=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c11_payload" 2>/dev/null || true)
c11_evts2=$(find "$TEST_RUNTIME/events" -name "${c11_task}*.evt" 2>/dev/null | wc -l)

# 11c: 포맷 오염
printf 'CORRUPTED_GARBAGE_LINE_###\nINVALID_FORMAT\n' > "$TEST_RUNTIME/workers/agy.resident"
c11_out3=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c11_payload" 2>/dev/null || true)
c11_evts3=$(find "$TEST_RUNTIME/events" -name "${c11_task}*.evt" 2>/dev/null | wc -l)
c11_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c11_task/state" 2>/dev/null | cut -d= -f2- || true)

if ([[ "$c11_out1" == *'{"decision": "allow"}'* || "$c11_out1" == *'{"decision":"allow"}'* ]]) && \
   ([[ "$c11_out2" == *'{"decision": "allow"}'* || "$c11_out2" == *'{"decision":"allow"}'* ]]) && \
   ([[ "$c11_out3" == *'{"decision": "allow"}'* || "$c11_out3" == *'{"decision":"allow"}'* ]]) && \
   (( c11_evts1 == 0 )) && (( c11_evts2 == 0 )) && (( c11_evts3 == 0 )) && \
   [[ "$c11_status" == "running" ]]; then
  pass_case 11 "마커 부재, pane_id 누락, 포맷 오염 -> 0 events, allow (Scenario ⑧-C)"
else
  fail_case 11 "마커 이상 fail-closed" "evts1=$c11_evts1, evts2=$c11_evts2, evts3=$c11_evts3, status=$c11_status"
fi

# ------------------------------------------------------------------
# Case 12: ACC_RUNTIME unset -> no-op, outputs valid JSON {"decision":"allow"}, modifies zero files
# ------------------------------------------------------------------
c12_payload="{\"fullyIdle\": true, \"conversationId\": \"c12\", \"testmark\": \"$TESTMARK\"}"
c12_stderr_file="$TEST_TMP/c12_stderr.log"
c12_out=$(run_clean TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c12_payload" 2>"$c12_stderr_file" || true)
c12_stderr=$(cat "$c12_stderr_file" 2>/dev/null || true)
c12_polluted="$((grep -r "$TESTMARK" "$LIVE_RT" 2>/dev/null || true) | wc -l)"

c12_pass=true
c12_reason=""
if [[ "$c12_out" != *'{"decision": "allow"}'* && "$c12_out" != *'{"decision":"allow"}'* ]]; then
  c12_pass=false
  c12_reason="invalid output: $c12_out"
fi
if [[ "$c12_stderr" == *"[agy-stop-hook] session="* ]]; then
  c12_pass=false
  c12_reason="fallback session resolution was attempted: $c12_stderr"
fi
if (( c12_polluted > 0 )); then
  c12_pass=false
  c12_reason="live runtime polluted with marker count=$c12_polluted"
fi
if grep -vE '^\s*#' "$HERE/bin/agy-stop-hook.sh" | grep -q "resolve_session_and_runtime"; then
  c12_pass=false
  c12_reason="agy-stop-hook.sh contains resolve_session_and_runtime fallback"
fi

if [[ "$c12_pass" == "true" ]]; then
  pass_case 12 "ACC_RUNTIME unset -> fail-closed no-op, valid JSON output, zero files modified"
else
  fail_case 12 "ACC_RUNTIME unset fail-closed" "$c12_reason"
fi

# ------------------------------------------------------------------
# Case 13: oneshot task -> 0 events emitted, state unchanged
# ------------------------------------------------------------------
c13_task="task-c13-${RAND_HEX}"
create_mock_task "$c13_task" "oneshot" "running"
setup_resident_marker "%1"
c13_payload="{\"fullyIdle\": true, \"conversationId\": \"c13\", \"testmark\": \"$TESTMARK\"}"
c13_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c13_payload" 2>/dev/null || true)
c13_evts=$(find "$TEST_RUNTIME/events" -name "${c13_task}*.evt" 2>/dev/null | wc -l)
c13_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c13_task/state" 2>/dev/null | cut -d= -f2- || true)
c13_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c13_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c13_busy_exists=true
fi

if [[ "$c13_out" == *'{"decision": "allow"}'* || "$c13_out" == *'{"decision":"allow"}'* ]] && \
   (( c13_evts == 0 )) && [[ "$c13_status" == "running" ]] && [[ "$c13_busy_exists" == "true" ]]; then
  pass_case 13 "oneshot task -> 0 events emitted, state unchanged (status=running, busy preserved)"
else
  fail_case 13 "oneshot task fail-closed" "evts=$c13_evts, status=$c13_status, busy_exists=$c13_busy_exists"
fi

# ------------------------------------------------------------------
# Case 14: no busy recorded (/clear scenario) -> no-op
# ------------------------------------------------------------------
rm -f "$TEST_RUNTIME/workers/agy.busy"
setup_resident_marker "%1"
c14_payload="{\"fullyIdle\": true, \"conversationId\": \"c14\", \"testmark\": \"$TESTMARK\"}"
c14_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c14_payload" 2>/dev/null || true)

if [[ "$c14_out" == *'{"decision": "allow"}'* || "$c14_out" == *'{"decision":"allow"}'* ]]; then
  pass_case 14 "no busy recorded (/clear scenario) -> no-op, valid JSON returned"
else
  fail_case 14 "no busy recorded" "out=$c14_out"
fi

# ------------------------------------------------------------------
# Case 15: broken JSON or empty stdin -> valid JSON response, no-op
# ------------------------------------------------------------------
c15_out_empty=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "" 2>/dev/null || true)
c15_out_broken=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< '{"invalid_json' 2>/dev/null || true)

if ([[ "$c15_out_empty" == *'{"decision": "allow"}'* || "$c15_out_empty" == *'{"decision":"allow"}'* ]]) && \
   ([[ "$c15_out_broken" == *'{"decision": "allow"}'* || "$c15_out_broken" == *'{"decision":"allow"}'* ]]); then
  pass_case 15 "broken JSON or empty stdin -> valid JSON response, no-op"
else
  fail_case 15 "broken JSON or empty stdin" "out_empty=$c15_out_empty, out_broken=$c15_out_broken"
fi

# ------------------------------------------------------------------
# Case 16: ACC_HOOK_DEBUG=1 -> appends debug trace to $ACC_RUNTIME/hook-debug.log
# ------------------------------------------------------------------
rm -f "$TEST_RUNTIME/hook-debug.log"
c16_payload="{\"fullyIdle\": false, \"conversationId\": \"c16\", \"testmark\": \"$TESTMARK\"}"
c16_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" ACC_HOOK_DEBUG="1" TMUX_PANE="%99" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c16_payload" 2>/dev/null || true)
debug_log_content="$(cat "$TEST_RUNTIME/hook-debug.log" 2>/dev/null || echo "")"

if [[ -f "$TEST_RUNTIME/hook-debug.log" ]] && [[ "$debug_log_content" == *"pane=%99"* ]] && [[ "$debug_log_content" == *"fullyIdle=false"* ]]; then
  pass_case 16 "ACC_HOOK_DEBUG=1 -> writes pane and fullyIdle to hook-debug.log"
else
  fail_case 16 "ACC_HOOK_DEBUG=1 logging" "log_content=$debug_log_content"
fi

# ------------------------------------------------------------------
# Case 17: 이름 ID 센티널 ([[DONE some-named-task]]) -> done 1회 발행, state=done, busy 삭제, mismatch 태그 로깅
# ------------------------------------------------------------------
c17_task="task-c17-${RAND_HEX}"
create_mock_task "$c17_task" "resident" "running"
setup_resident_marker "%1"
rm -f "$TEST_RUNTIME/hook-debug.log"
c17_trans="$TEST_TMP/c17_transcript.jsonl"
cat > "$c17_trans" <<EOF
{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Start work on named task"}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Named report here.\n[[DONE some-named-task]] result=all tests pass | learning=none"}
EOF
c17_payload="{\"fullyIdle\": true, \"conversationId\": \"c17\", \"transcriptPath\": \"$c17_trans\", \"testmark\": \"$TESTMARK\"}"
c17_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" ACC_HOOK_DEBUG="1" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c17_payload" 2>/dev/null || true)
c17_evts=$(find "$TEST_RUNTIME/events" -name "${c17_task}*.evt" 2>/dev/null | wc -l)
c17_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c17_task/state" 2>/dev/null | cut -d= -f2- || true)
c17_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c17_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c17_busy_exists=true
fi
c17_debug_log="$(cat "$TEST_RUNTIME/hook-debug.log" 2>/dev/null || echo "")"
c17_mismatch_logged=false
if [[ "$c17_debug_log" == *"sentinel_id_mismatch_accepted (found: some-named-task, current: $c17_task)"* ]]; then
  c17_mismatch_logged=true
fi
c17_summary_ok=false
for ef in "$TEST_RUNTIME"/events/pending/${c17_task}*.evt; do
  if [[ -f "$ef" ]]; then
    c17_b64=$(grep '^summary_b64=' "$ef" | cut -d= -f2- || true)
    c17_decoded=$(printf '%s' "$c17_b64" | base64 -d 2>/dev/null || true)
    if [[ "$c17_decoded" == *"result=all tests pass | learning=none"* ]]; then
      c17_summary_ok=true
    fi
    break
  fi
done

if [[ "$c17_out" == *'{"decision": "allow"}'* || "$c17_out" == *'{"decision":"allow"}'* ]] && \
   (( c17_evts == 1 )) && [[ "$c17_status" == "done" ]] && [[ "$c17_busy_exists" == "false" ]] && \
   [[ "$c17_mismatch_logged" == "true" ]] && [[ "$c17_summary_ok" == "true" ]]; then
  pass_case 17 "이름 ID 센티널 ([[DONE some-named-task]]) -> done 1회 발행, state=done, busy 삭제, mismatch 태그 확인"
else
  fail_case 17 "이름 ID 센티널 처리 실패" "evts=$c17_evts, status=$c17_status, busy=$c17_busy_exists, mismatch_logged=$c17_mismatch_logged, summary_ok=$c17_summary_ok"
fi

# ------------------------------------------------------------------
# Case 18: 이름 ID BLOCKED ([[BLOCKED named-task]]) -> error 1회 발행, state=error, busy 삭제, mismatch 태그 확인
# ------------------------------------------------------------------
c18_task="task-c18-${RAND_HEX}"
create_mock_task "$c18_task" "resident" "running"
setup_resident_marker "%1"
rm -f "$TEST_RUNTIME/hook-debug.log"
c18_trans="$TEST_TMP/c18_transcript.jsonl"
cat > "$c18_trans" <<EOF
{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "작업 수행"}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "외부 인증 실패로 차단됨.\n[[BLOCKED named-task]] reason=auth failure"}
EOF
c18_payload="{\"fullyIdle\": true, \"conversationId\": \"c18\", \"transcriptPath\": \"$c18_trans\", \"testmark\": \"$TESTMARK\"}"
c18_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" ACC_HOOK_DEBUG="1" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c18_payload" 2>/dev/null || true)
c18_evts=$(find "$TEST_RUNTIME/events" -name "${c18_task}*.evt" 2>/dev/null | wc -l)
c18_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c18_task/state" 2>/dev/null | cut -d= -f2- || true)
c18_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c18_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c18_busy_exists=true
fi
c18_evt_kind=""
c18_summary_ok=false
for ef in "$TEST_RUNTIME"/events/pending/${c18_task}*.evt; do
  if [[ -f "$ef" ]]; then
    c18_evt_kind=$(grep '^kind=' "$ef" | cut -d= -f2- || true)
    c18_b64=$(grep '^summary_b64=' "$ef" | cut -d= -f2- || true)
    c18_decoded=$(printf '%s' "$c18_b64" | base64 -d 2>/dev/null || true)
    if [[ "$c18_decoded" == *"reason=auth failure"* ]]; then
      c18_summary_ok=true
    fi
    break
  fi
done
c18_debug_log="$(cat "$TEST_RUNTIME/hook-debug.log" 2>/dev/null || echo "")"
c18_mismatch_logged=false
if [[ "$c18_debug_log" == *"sentinel_id_mismatch_accepted (found: named-task, current: $c18_task)"* ]]; then
  c18_mismatch_logged=true
fi

if [[ "$c18_out" == *'{"decision": "allow"}'* || "$c18_out" == *'{"decision":"allow"}'* ]] && \
   (( c18_evts == 1 )) && [[ "$c18_status" == "error" ]] && [[ "$c18_evt_kind" == "error" ]] && \
   [[ "$c18_busy_exists" == "false" ]] && [[ "$c18_mismatch_logged" == "true" ]] && [[ "$c18_summary_ok" == "true" ]]; then
  pass_case 18 "이름 ID BLOCKED ([[BLOCKED named-task]]) -> error 1회 발행, state=error, busy 삭제, mismatch 태그 확인"
else
  fail_case 18 "이름 ID BLOCKED 처리 실패" "evts=$c18_evts, status=$c18_status, kind=$c18_evt_kind, busy=$c18_busy_exists, mismatch_logged=$c18_mismatch_logged"
fi

# ------------------------------------------------------------------
# Case 19: 이전 작업의 오래된 센티널 (created_at < started_epoch) -> 미발행, running 유지, busy 유지
# ------------------------------------------------------------------
c19_task="task-c19-${RAND_HEX}"
create_mock_task "$c19_task" "resident" "running"
setup_resident_marker "%1"
c19_now=$(date +%s)
c19_old_ts=$(python3 -c 'from datetime import datetime, timezone; print(datetime.fromtimestamp('"$c19_now"' - 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))')
c19_trans="$TEST_TMP/c19_transcript.jsonl"
cat > "$c19_trans" <<EOF
{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": "$c19_old_ts", "content": "Old task input"}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": "$c19_old_ts", "content": "Old completion.\n[[DONE old-task-id]] result=old result"}
EOF
c19_payload="{\"fullyIdle\": true, \"conversationId\": \"c19\", \"transcriptPath\": \"$c19_trans\", \"testmark\": \"$TESTMARK\"}"
c19_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c19_payload" 2>/dev/null || true)
c19_evts=$(find "$TEST_RUNTIME/events" -name "${c19_task}*.evt" 2>/dev/null | wc -l)
c19_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c19_task/state" 2>/dev/null | cut -d= -f2- || true)
c19_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c19_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c19_busy_exists=true
fi

if [[ "$c19_out" == *'{"decision": "allow"}'* || "$c19_out" == *'{"decision":"allow"}'* ]] && \
   (( c19_evts == 0 )) && [[ "$c19_status" == "running" ]] && [[ "$c19_busy_exists" == "true" ]]; then
  pass_case 19 "이전 작업의 오래된 센티널 (created_at < started_epoch) -> 미발행, running 유지, busy 유지"
else
  fail_case 19 "오래된 센티널 오탐 방지 가드 실패" "evts=$c19_evts, status=$c19_status, busy=$c19_busy_exists, out=$c19_out"
fi

echo "=================================================================="
echo "stop-hook-test.sh Summary: PASSED=$PASSED, FAILED=$FAILED"
echo "=================================================================="

if (( FAILED > 0 )); then
  exit 1
fi
exit 0
