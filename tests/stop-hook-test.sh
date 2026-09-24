#!/usr/bin/env bash
# tests/stop-hook-test.sh: agy-stop-hook.sh 단위 및 fail-closed 검증
# 10개 테스트 케이스:
#   (1) fullyIdle=false -> 0 events emitted
#   (2) resident task running + fullyIdle=true + correct pane -> exactly 1 done event, state=done, busy and terminal.lock released
#   (3) duplicate call with same payload / concurrent 2 calls -> still exactly 1 event (idempotency)
#   (4) oneshot task (task state lacks mode=resident or has mode=oneshot) -> 0 events emitted, state unchanged
#   (5) ACC_RUNTIME unset -> no-op, outputs valid JSON {"decision":"allow"}, modifies zero files
#   (6) TMUX_PANE mismatch with workers/agy.resident -> no-op
#   (7) subagent transcript -> no-op
#   (8) error payload -> exactly 1 error event emitted
#   (9) no busy recorded (/clear scenario) -> no-op
#   (10) broken JSON or empty stdin -> valid JSON response, no-op
#   (11) marker exists but has no pane_id= line or empty value -> no-op
#   (12) marker format corrupted -> no-op

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

# Helper: helper to create task directory and state
create_mock_task() {
  local task_id="$1"
  local mode="${2:-resident}"
  local status="${3:-running}"
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
deadline_epoch=$(( $(date +%s) + 1800 ))
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
# Case 2: resident task running + fullyIdle=true + correct pane -> exactly 1 done event, state=done, busy and terminal.lock released
# ------------------------------------------------------------------
c2_task="task-c2-${RAND_HEX}"
create_mock_task "$c2_task" "resident" "running"
setup_resident_marker "%1"
c2_payload="{\"fullyIdle\": true, \"conversationId\": \"c2\", \"error\": \"\", \"terminationReason\": \"\", \"testmark\": \"$TESTMARK\"}"
c2_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c2_payload" 2>/dev/null || true)
c2_evts=$(find "$TEST_RUNTIME/events" -name "${c2_task}*.evt" 2>/dev/null | wc -l)
c2_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c2_task/state" 2>/dev/null | cut -d= -f2- || true)
c2_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c2_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c2_busy_exists=true
fi
c2_lock_ok=false
if flock -n "$TEST_RUNTIME/tasks/$c2_task/terminal.lock" true 2>/dev/null; then
  c2_lock_ok=true
fi

if [[ "$c2_out" == *'{"decision": "allow"}'* || "$c2_out" == *'{"decision":"allow"}'* ]] && \
   (( c2_evts == 1 )) && [[ "$c2_status" == "done" ]] && [[ "$c2_busy_exists" == "false" ]] && [[ "$c2_lock_ok" == "true" ]]; then
  pass_case 2 "resident task running + fullyIdle=true + correct pane -> 1 done event, state=done, busy/lock released"
else
  fail_case 2 "resident task running + fullyIdle=true + correct pane" "evts=$c2_evts, status=$c2_status, busy_exists=$c2_busy_exists, lock_ok=$c2_lock_ok"
fi

# ------------------------------------------------------------------
# Case 3: duplicate call with same payload / concurrent 2 calls -> still exactly 1 event (idempotency)
# ------------------------------------------------------------------
c3_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c2_payload" 2>/dev/null || true)
c3_evts=$(find "$TEST_RUNTIME/events" -name "${c2_task}*.evt" 2>/dev/null | wc -l)
c3_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c2_task/state" 2>/dev/null | cut -d= -f2- || true)

if [[ "$c3_out" == *'{"decision": "allow"}'* || "$c3_out" == *'{"decision":"allow"}'* ]] && \
   (( c3_evts == 1 )) && [[ "$c3_status" == "done" ]]; then
  pass_case 3 "duplicate call with same payload -> idempotency maintained (still exactly 1 event)"
else
  fail_case 3 "duplicate call idempotency" "evts=$c3_evts, status=$c3_status"
fi

# ------------------------------------------------------------------
# Case 4: oneshot task (task state lacks mode=resident or has mode=oneshot) -> 0 events emitted, state unchanged
# ------------------------------------------------------------------
c4_task="task-c4-${RAND_HEX}"
create_mock_task "$c4_task" "oneshot" "running"
setup_resident_marker "%1"
c4_payload="{\"fullyIdle\": true, \"conversationId\": \"c4\", \"testmark\": \"$TESTMARK\"}"
c4_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c4_payload" 2>/dev/null || true)
c4_evts=$(find "$TEST_RUNTIME/events" -name "${c4_task}*.evt" 2>/dev/null | wc -l)
c4_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c4_task/state" 2>/dev/null | cut -d= -f2- || true)
c4_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c4_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c4_busy_exists=true
fi

if [[ "$c4_out" == *'{"decision": "allow"}'* || "$c4_out" == *'{"decision":"allow"}'* ]] && \
   (( c4_evts == 0 )) && [[ "$c4_status" == "running" ]] && [[ "$c4_busy_exists" == "true" ]]; then
  pass_case 4 "oneshot task -> 0 events emitted, state unchanged (status=running, busy preserved)"
else
  fail_case 4 "oneshot task fail-closed" "evts=$c4_evts, status=$c4_status, busy_exists=$c4_busy_exists"
fi

# ------------------------------------------------------------------
# Case 5: ACC_RUNTIME unset -> no-op, outputs valid JSON {"decision":"allow"}, modifies zero files
# ------------------------------------------------------------------
c5_task="task-c5-${RAND_HEX}"
c5_payload="{\"fullyIdle\": true, \"conversationId\": \"c5\", \"testmark\": \"$TESTMARK\"}"
# Run without ACC_RUNTIME. Check stderr for session resolution fallback.
c5_stderr_file="$TEST_TMP/c5_stderr.log"
c5_out=$(run_clean TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c5_payload" 2>"$c5_stderr_file" || true)
c5_stderr=$(cat "$c5_stderr_file" 2>/dev/null || true)
c5_polluted="$((grep -r "$TESTMARK" "$LIVE_RT" 2>/dev/null || true) | wc -l)"

# Check: must NOT resolve session (no stderr from resolve_session_and_runtime), must return allow, live runtime untouched
c5_pass=true
c5_reason=""
if [[ "$c5_out" != *'{"decision": "allow"}'* && "$c5_out" != *'{"decision":"allow"}'* ]]; then
  c5_pass=false
  c5_reason="invalid output: $c5_out"
fi
if [[ "$c5_stderr" == *"[agy-stop-hook] session="* ]]; then
  c5_pass=false
  c5_reason="fallback session resolution was attempted (fail-open): $c5_stderr"
fi
if (( c5_polluted > 0 )); then
  c5_pass=false
  c5_reason="live runtime polluted with marker count=$c5_polluted"
fi
# Also verify that agy-stop-hook.sh does not call resolve_session_and_runtime
if grep -vE '^\s*#' "$HERE/bin/agy-stop-hook.sh" | grep -q "resolve_session_and_runtime"; then
  c5_pass=false
  c5_reason="agy-stop-hook.sh contains resolve_session_and_runtime fallback"
fi

if [[ "$c5_pass" == "true" ]]; then
  pass_case 5 "ACC_RUNTIME unset -> fail-closed no-op, valid JSON output, zero files modified"
else
  fail_case 5 "ACC_RUNTIME unset fail-closed" "$c5_reason"
fi

# ------------------------------------------------------------------
# Case 6: TMUX_PANE mismatch with workers/agy.resident -> no-op
# ------------------------------------------------------------------
c6_task="task-c6-${RAND_HEX}"
create_mock_task "$c6_task" "resident" "running"
setup_resident_marker "%1"
c6_payload="{\"fullyIdle\": true, \"conversationId\": \"c6\", \"testmark\": \"$TESTMARK\"}"
# Invoke with TMUX_PANE=%99 (mismatched)
c6_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%99" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c6_payload" 2>/dev/null || true)
c6_evts=$(find "$TEST_RUNTIME/events" -name "${c6_task}*.evt" 2>/dev/null | wc -l)
c6_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c6_task/state" 2>/dev/null | cut -d= -f2- || true)
c6_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c6_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c6_busy_exists=true
fi

if [[ "$c6_out" == *'{"decision": "allow"}'* || "$c6_out" == *'{"decision":"allow"}'* ]] && \
   (( c6_evts == 0 )) && [[ "$c6_status" == "running" ]] && [[ "$c6_busy_exists" == "true" ]]; then
  pass_case 6 "TMUX_PANE mismatch with workers/agy.resident -> fail-closed no-op"
else
  fail_case 6 "TMUX_PANE mismatch fail-closed" "evts=$c6_evts, status=$c6_status, busy_exists=$c6_busy_exists"
fi

# ------------------------------------------------------------------
# Case 7: subagent transcript -> no-op
# ------------------------------------------------------------------
c7_task="task-c7-${RAND_HEX}"
create_mock_task "$c7_task" "resident" "running"
setup_resident_marker "%1"
c7_trans="$TEST_TMP/subagent_transcript.jsonl"
printf '{"source": "SYSTEM", "content": "sender=3cff7708-subagent testmark=%s"}\n' "$TESTMARK" > "$c7_trans"
c7_payload="{\"fullyIdle\": true, \"conversationId\": \"c7\", \"transcriptPath\": \"$c7_trans\", \"testmark\": \"$TESTMARK\"}"
c7_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c7_payload" 2>/dev/null || true)
c7_evts=$(find "$TEST_RUNTIME/events" -name "${c7_task}*.evt" 2>/dev/null | wc -l)
c7_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c7_task/state" 2>/dev/null | cut -d= -f2- || true)

if [[ "$c7_out" == *'{"decision": "allow"}'* || "$c7_out" == *'{"decision":"allow"}'* ]] && \
   (( c7_evts == 0 )) && [[ "$c7_status" == "running" ]]; then
  pass_case 7 "subagent transcript -> filtered out, 0 events emitted"
else
  fail_case 7 "subagent transcript filtering" "evts=$c7_evts, status=$c7_status"
fi

# ------------------------------------------------------------------
# Case 8: error payload -> exactly 1 error event emitted
# ------------------------------------------------------------------
c8_task="task-c8-${RAND_HEX}"
create_mock_task "$c8_task" "resident" "running"
setup_resident_marker "%1"
c8_payload="{\"fullyIdle\": true, \"conversationId\": \"c8\", \"error\": \"Execution failed $TESTMARK\", \"terminationReason\": \"error\"}"
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
  pass_case 8 "error payload -> exactly 1 error event emitted, state=error, busy released"
else
  fail_case 8 "error payload handling" "evts=$c8_evts, status=$c8_status, kind=$c8_evt_kind, busy_exists=$c8_busy_exists"
fi

# ------------------------------------------------------------------
# Case 9: no busy recorded (/clear scenario) -> no-op
# ------------------------------------------------------------------
rm -f "$TEST_RUNTIME/workers/agy.busy"
setup_resident_marker "%1"
c9_payload="{\"fullyIdle\": true, \"conversationId\": \"c9\", \"testmark\": \"$TESTMARK\"}"
c9_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c9_payload" 2>/dev/null || true)
c9_evts_after=$(find "$TEST_RUNTIME/events" -name "*-terminal.evt" 2>/dev/null | wc -l)

if [[ "$c9_out" == *'{"decision": "allow"}'* || "$c9_out" == *'{"decision":"allow"}'* ]]; then
  pass_case 9 "no busy recorded (/clear scenario) -> no-op, valid JSON returned"
else
  fail_case 9 "no busy recorded" "out=$c9_out"
fi

# ------------------------------------------------------------------
# Case 10: broken JSON or empty stdin -> valid JSON response, no-op
# ------------------------------------------------------------------
c10_out_empty=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "" 2>/dev/null || true)
c10_out_broken=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< '{"invalid_json' 2>/dev/null || true)

if ([[ "$c10_out_empty" == *'{"decision": "allow"}'* || "$c10_out_empty" == *'{"decision":"allow"}'* ]]) && \
   ([[ "$c10_out_broken" == *'{"decision": "allow"}'* || "$c10_out_broken" == *'{"decision":"allow"}'* ]]); then
  pass_case 10 "broken JSON or empty stdin -> valid JSON response, no-op"
else
  fail_case 10 "broken JSON or empty stdin" "out_empty=$c10_out_empty, out_broken=$c10_out_broken"
fi

# ------------------------------------------------------------------
# Case 11: Marker exists but has no pane_id= line or empty value -> no-op
# ------------------------------------------------------------------
c11_task="task-c11-${RAND_HEX}"
create_mock_task "$c11_task" "resident" "running"
# 11a: no pane_id= line
printf 'session=%s\n' "$TEST_SESSION" > "$TEST_RUNTIME/workers/agy.resident"
c11_payload="{\"fullyIdle\": true, \"conversationId\": \"c11\", \"testmark\": \"$TESTMARK\"}"
c11_out1=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c11_payload" 2>/dev/null || true)
c11_evts1=$(find "$TEST_RUNTIME/events" -name "${c11_task}*.evt" 2>/dev/null | wc -l)
c11_status1=$(grep '^status=' "$TEST_RUNTIME/tasks/$c11_task/state" 2>/dev/null | cut -d= -f2- || true)

# 11b: empty pane_id= value
printf 'pane_id=\nsession=%s\n' "$TEST_SESSION" > "$TEST_RUNTIME/workers/agy.resident"
c11_out2=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c11_payload" 2>/dev/null || true)
c11_evts2=$(find "$TEST_RUNTIME/events" -name "${c11_task}*.evt" 2>/dev/null | wc -l)
c11_status2=$(grep '^status=' "$TEST_RUNTIME/tasks/$c11_task/state" 2>/dev/null | cut -d= -f2- || true)

if ([[ "$c11_out1" == *'{"decision": "allow"}'* || "$c11_out1" == *'{"decision":"allow"}'* ]]) && \
   ([[ "$c11_out2" == *'{"decision": "allow"}'* || "$c11_out2" == *'{"decision":"allow"}'* ]]) && \
   (( c11_evts1 == 0 )) && (( c11_evts2 == 0 )) && \
   [[ "$c11_status1" == "running" ]] && [[ "$c11_status2" == "running" ]]; then
  pass_case 11 "marker exists but has no pane_id= line or empty value -> no-op, state unchanged"
else
  fail_case 11 "no or empty pane_id in marker" "out1=$c11_out1, out2=$c11_out2, evts1=$c11_evts1, evts2=$c11_evts2"
fi

# ------------------------------------------------------------------
# Case 12: Marker format corrupted -> no-op
# ------------------------------------------------------------------
c12_task="task-c12-${RAND_HEX}"
create_mock_task "$c12_task" "resident" "running"
printf 'CORRUPTED_GARBAGE_LINE_###\nINVALID_FORMAT\n' > "$TEST_RUNTIME/workers/agy.resident"
c12_payload="{\"fullyIdle\": true, \"conversationId\": \"c12\", \"testmark\": \"$TESTMARK\"}"
c12_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c12_payload" 2>/dev/null || true)
c12_evts=$(find "$TEST_RUNTIME/events" -name "${c12_task}*.evt" 2>/dev/null | wc -l)
c12_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c12_task/state" 2>/dev/null | cut -d= -f2- || true)

if ([[ "$c12_out" == *'{"decision": "allow"}'* || "$c12_out" == *'{"decision":"allow"}'* ]]) && \
   (( c12_evts == 0 )) && [[ "$c12_status" == "running" ]]; then
  pass_case 12 "marker format corrupted -> no-op, valid JSON response, state unchanged"
else
  fail_case 12 "marker format corrupted" "out=$c12_out, evts=$c12_evts, status=$c12_status"
fi

# ------------------------------------------------------------------
# Case 13: terminationReason="interrupted" -> exactly 1 error event, 0 done events, state=error, busy released
# ------------------------------------------------------------------
c13_task="task-c13-${RAND_HEX}"
create_mock_task "$c13_task" "resident" "running"
setup_resident_marker "%1"
c13_payload="{\"fullyIdle\": true, \"conversationId\": \"c13\", \"terminationReason\": \"interrupted\", \"error\": \"\", \"testmark\": \"$TESTMARK\"}"
c13_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c13_payload" 2>/dev/null || true)
c13_evts=$(find "$TEST_RUNTIME/events" -name "${c13_task}*.evt" 2>/dev/null | wc -l)
c13_done_evts=$(grep -rn 'kind=done' "$TEST_RUNTIME/events" 2>/dev/null | grep "$c13_task" | wc -l || true)
c13_err_evts=$(grep -rn 'kind=error' "$TEST_RUNTIME/events" 2>/dev/null | grep "$c13_task" | wc -l || true)
c13_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c13_task/state" 2>/dev/null | cut -d= -f2- || true)
c13_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c13_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c13_busy_exists=true
fi

if [[ "$c13_out" == *'{"decision": "allow"}'* || "$c13_out" == *'{"decision":"allow"}'* ]] && \
   (( c13_evts == 1 )) && (( c13_done_evts == 0 )) && (( c13_err_evts == 1 )) && \
   [[ "$c13_status" == "error" ]] && [[ "$c13_busy_exists" == "false" ]]; then
  pass_case 13 "terminationReason=interrupted -> 1 error event (0 done events), state=error, busy released (no fake done)"
else
  fail_case 13 "terminationReason=interrupted" "evts=$c13_evts, done_evts=$c13_done_evts, err_evts=$c13_err_evts, status=$c13_status, busy=$c13_busy_exists"
fi

# ------------------------------------------------------------------
# Case 14: terminationReason="cancelled" -> exactly 1 error event, 0 done events, state=error, busy released
# ------------------------------------------------------------------
c14_task="task-c14-${RAND_HEX}"
create_mock_task "$c14_task" "resident" "running"
setup_resident_marker "%1"
c14_payload="{\"fullyIdle\": true, \"conversationId\": \"c14\", \"terminationReason\": \"cancelled\", \"error\": \"User cancelled\", \"testmark\": \"$TESTMARK\"}"
c14_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c14_payload" 2>/dev/null || true)
c14_evts=$(find "$TEST_RUNTIME/events" -name "${c14_task}*.evt" 2>/dev/null | wc -l)
c14_done_evts=$(grep -rn 'kind=done' "$TEST_RUNTIME/events" 2>/dev/null | grep "$c14_task" | wc -l || true)
c14_err_evts=$(grep -rn 'kind=error' "$TEST_RUNTIME/events" 2>/dev/null | grep "$c14_task" | wc -l || true)
c14_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$c14_task/state" 2>/dev/null | cut -d= -f2- || true)
c14_busy_exists=false
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && grep -q "$c14_task" "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null; then
  c14_busy_exists=true
fi

if [[ "$c14_out" == *'{"decision": "allow"}'* || "$c14_out" == *'{"decision":"allow"}'* ]] && \
   (( c14_evts == 1 )) && (( c14_done_evts == 0 )) && (( c14_err_evts == 1 )) && \
   [[ "$c14_status" == "error" ]] && [[ "$c14_busy_exists" == "false" ]]; then
  pass_case 14 "terminationReason=cancelled -> 1 error event (0 done events), state=error, busy released (no fake done)"
else
  fail_case 14 "terminationReason=cancelled" "evts=$c14_evts, done_evts=$c14_done_evts, err_evts=$c14_err_evts, status=$c14_status, busy=$c14_busy_exists"
fi

# ------------------------------------------------------------------
# Case 15: ACC_HOOK_DEBUG=1 -> appends debug trace to $ACC_RUNTIME/hook-debug.log
# ------------------------------------------------------------------
rm -f "$TEST_RUNTIME/hook-debug.log"
c15_payload="{\"fullyIdle\": false, \"conversationId\": \"c15\", \"testmark\": \"$TESTMARK\"}"
c15_out=$(run_clean ACC_RUNTIME="$TEST_RUNTIME" ACC_HOOK_DEBUG="1" TMUX_PANE="%99" -- "$HERE/bin/agy-stop-hook.sh" <<< "$c15_payload" 2>/dev/null || true)
debug_log_content="$(cat "$TEST_RUNTIME/hook-debug.log" 2>/dev/null || echo "")"

if [[ -f "$TEST_RUNTIME/hook-debug.log" ]] && [[ "$debug_log_content" == *"pane=%99"* ]] && [[ "$debug_log_content" == *"fullyIdle=false"* ]]; then
  pass_case 15 "ACC_HOOK_DEBUG=1 -> writes pane and fullyIdle to hook-debug.log"
else
  fail_case 15 "ACC_HOOK_DEBUG=1 logging" "log_content=$debug_log_content"
fi

echo "=================================================================="
echo "stop-hook-test.sh Summary: PASSED=$PASSED, FAILED=$FAILED"
echo "=================================================================="

if (( FAILED > 0 )); then
  exit 1
fi
exit 0
