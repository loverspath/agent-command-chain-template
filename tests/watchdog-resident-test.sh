#!/usr/bin/env bash
# tests/watchdog-resident-test.sh: watchdog-v2.sh 상주 모드 안전망 및 fail-closed 검증
# 검증 항목:
#   (a) deadline 초과 -> stalled 이벤트 발행
#   (b) SIGSTOP으로 정지된 상주 프로세스 -> stalled 이벤트 발행
#   (c) 상주 프로세스 종료/사망 -> process_exit 이벤트 발행 및 busy 해제
#   (d) ACC_RUNTIME 누락 시 fail-closed 거부 (운영 런타임 절대 미접촉)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "watchdog-resident"
setup_mock_runtime "$TEST_RUNTIME"

# tmux 세션 생성 (windows: sonnet, agy, codex)
tmux new-session -d -s "$TEST_SESSION" -n sonnet -c "$TEST_TMP"
tmux new-window -t "$TEST_SESSION" -n agy -c "$TEST_TMP"
tmux new-window -t "$TEST_SESSION" -n codex -c "$TEST_TMP"

wait_pane_shell "$TEST_SESSION:sonnet" 10
wait_pane_shell "$TEST_SESSION:agy" 10
wait_pane_shell "$TEST_SESSION:codex" 10

tmux set-environment -t "$TEST_SESSION" ACC_BOOTSTRAP_VERSION 2
tmux set-environment -t "$TEST_SESSION" ACC_RUNTIME "$TEST_RUNTIME"
tmux set-environment -t "$TEST_SESSION" SESSION_NAME "$TEST_SESSION"

PASSED=0
FAILED=0

pass_case() {
  local name="$1"
  local desc="$2"
  echo "  [PASS] $name: $desc"
  PASSED=$((PASSED + 1))
}

fail_case() {
  local name="$1"
  local desc="$2"
  local reason="${3:-unknown}"
  echo "  [FAIL] $name: $desc (Reason: $reason)"
  FAILED=$((FAILED + 1))
}

echo "=================================================================="
echo "Running watchdog-resident-test.sh (Marker: $TESTMARK)"
echo "Target session: $TEST_SESSION, runtime: $TEST_RUNTIME"
echo "=================================================================="

# 1. Start stub resident agy process in window 'agy'
stub_bin="$(build_stub_agy "$TEST_TMP/bin")"
tmux send-keys -t "$TEST_SESSION:agy" "$stub_bin" C-m

# Wait for pane_current_command to become 'agy'
p_wait=0
while (( p_wait < 30 )); do
  if [[ "$(tmux display-message -p -t "$TEST_SESSION:agy" '#{pane_current_command}' 2>/dev/null || true)" == "agy" ]]; then
    break
  fi
  sleep 0.1
  p_wait=$((p_wait + 1))
done

pane_pid="$(tmux display-message -p -t "$TEST_SESSION:agy" '#{pane_pid}')"
child_pid="$(pgrep -P "$pane_pid" agy 2>/dev/null | head -n 1 || echo "$pane_pid")"
target_pid="${child_pid:-$pane_pid}"
proc_token="$(awk '{print $22}' "/proc/$target_pid/stat" 2>/dev/null || echo "$$")"
pgid="$(ps -o pgid= -p "$target_pid" 2>/dev/null | tr -d ' ' || echo "$target_pid")"

agy_pane_id="$(tmux display-message -p -t "$TEST_SESSION:agy" '#{pane_id}')"
printf 'pane_id=%s\nsession_name=%s\n' "$agy_pane_id" "$TEST_SESSION" > "$TEST_RUNTIME/workers/agy.resident"

# ------------------------------------------------------------------
# Test A: Deadline 초과 감지 -> stalled 이벤트 발행
# ------------------------------------------------------------------
now=$(date +%s)
dl_task="task-dl-${RAND_HEX}"
dl_dir="$TEST_RUNTIME/tasks/$dl_task"
mkdir -p "$dl_dir"
echo "Deadline test prompt $TESTMARK" > "$dl_dir/prompt.md"

cat > "$dl_dir/state" <<EOF
task_id=$dl_task
worker=agy
status=running
mode=resident
pid=$target_pid
pgid=$pgid
proc_token=$proc_token
started_epoch=$((now - 2000))
deadline_epoch=$((now - 100))
EOF
printf '%s\n%s\n' "$dl_task" "$((now - 2000))" > "$TEST_RUNTIME/workers/agy.busy"

# Run watchdog in oneshot mode with WATCHDOG_AUTO_KILL=false
run_clean ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" WATCHDOG_AUTO_KILL=false \
  -- "$HERE/watchdog-v2.sh" --oneshot >/dev/null 2>&1 || true

dl_evt_file="$TEST_RUNTIME/events/pending/${dl_task}-deadline-warn.evt"
dl_pass=false
if [[ -f "$dl_evt_file" ]] && grep -q '^kind=stalled' "$dl_evt_file"; then
  dl_pass=true
fi

if [[ "$dl_pass" == "true" ]]; then
  pass_case "Case A" "deadline exceeded -> stalled event emitted"
else
  fail_case "Case A" "deadline exceeded" "event file missing or kind!=stalled"
fi

# Clean up task A
rm -f "$dl_evt_file" "$TEST_RUNTIME/workers/agy.busy"

# ------------------------------------------------------------------
# Test B: SIGSTOP 일시 정지 감지 -> stalled 이벤트 발행
# ------------------------------------------------------------------
now=$(date +%s)
stop_task="task-stop-${RAND_HEX}"
stop_dir="$TEST_RUNTIME/tasks/$stop_task"
mkdir -p "$stop_dir"
echo "Stop test prompt $TESTMARK" > "$stop_dir/prompt.md"

cat > "$stop_dir/state" <<EOF
task_id=$stop_task
worker=agy
status=running
mode=resident
pid=$target_pid
pgid=$pgid
proc_token=$proc_token
started_epoch=$now
deadline_epoch=$((now + 1800))
EOF
printf '%s\n%s\n' "$stop_task" "$now" > "$TEST_RUNTIME/workers/agy.busy"

# Stop the stub process with SIGSTOP
kill -STOP "$target_pid"
sleep 0.1

run_clean ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" \
  -- "$HERE/watchdog-v2.sh" --oneshot >/dev/null 2>&1 || true

# Resume the stub process
kill -CONT "$target_pid" 2>/dev/null || true
sleep 0.1

stop_evt_file="$TEST_RUNTIME/events/pending/${stop_task}-stop-warn.evt"
stop_pass=false
if [[ -f "$stop_evt_file" ]] && grep -q '^kind=stalled' "$stop_evt_file"; then
  stop_pass=true
fi

if [[ "$stop_pass" == "true" ]]; then
  pass_case "Case B" "stopped with SIGSTOP -> stalled event emitted"
else
  fail_case "Case B" "stopped with SIGSTOP" "event file missing or kind!=stalled (found: $(ls "$TEST_RUNTIME/events/pending/" 2>/dev/null || true))"
fi

# Clean up task B
rm -f "$stop_evt_file" "$TEST_RUNTIME/workers/agy.busy"

# ------------------------------------------------------------------
# Test C: 프로세스 비정상 종료 (crash / kill) 감지 -> process_exit 이벤트 발행 및 busy 해제
# ------------------------------------------------------------------
now=$(date +%s)
term_task="task-term-${RAND_HEX}"
term_dir="$TEST_RUNTIME/tasks/$term_task"
mkdir -p "$term_dir"
echo "Term test prompt $TESTMARK" > "$term_dir/prompt.md"

cat > "$term_dir/state" <<EOF
task_id=$term_task
worker=agy
status=running
mode=resident
pid=$target_pid
pgid=$pgid
proc_token=$proc_token
started_epoch=$now
deadline_epoch=$((now + 1800))
EOF
printf '%s\n%s\n' "$term_task" "$now" > "$TEST_RUNTIME/workers/agy.busy"

# Kill target process
kill -9 "$target_pid" 2>/dev/null || true

# Wait for pane command to return to bash/sh
w_term=0
while (( w_term < 30 )); do
  cur_cmd="$(tmux display-message -p -t "$TEST_SESSION:agy" '#{pane_current_command}' 2>/dev/null || true)"
  if [[ "$cur_cmd" != "agy" ]]; then
    break
  fi
  sleep 0.1
  w_term=$((w_term + 1))
done

run_clean ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" \
  -- "$HERE/watchdog-v2.sh" --oneshot >/dev/null 2>&1 || true

term_evt_file="$TEST_RUNTIME/events/pending/${term_task}-terminal.evt"
term_status=$(grep '^status=' "$term_dir/state" 2>/dev/null | cut -d= -f2- || true)
term_busy_exists=false
[[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && term_busy_exists=true

term_pass=false
if [[ -f "$term_evt_file" ]] && grep -q '^kind=process_exit' "$term_evt_file" && \
   [[ "$term_status" == "error" ]] && [[ "$term_busy_exists" == "false" ]]; then
  term_pass=true
fi

if [[ "$term_pass" == "true" ]]; then
  pass_case "Case C" "process termination -> process_exit event emitted, status=error, busy released"
else
  fail_case "Case C" "process termination" "evt_exists=$([[ -f "$term_evt_file" ]] && echo yes || echo no), status=$term_status, busy_exists=$term_busy_exists"
fi

# ------------------------------------------------------------------
# Test D: ACC_RUNTIME 누락 시 watchdog-v2 fail-closed 거부
# ------------------------------------------------------------------
d_rc=0
d_err="$TEST_TMP/d_err.log"
run_clean -- "$HERE/watchdog-v2.sh" --oneshot >/dev/null 2>"$d_err" || d_rc=$?

d_pass=false
if (( d_rc != 0 )) && grep -qi "requires explicit ACC_RUNTIME" "$d_err"; then
  d_pass=true
fi

if [[ "$d_pass" == "true" ]]; then
  pass_case "Case D" "watchdog-v2 fail-closed when ACC_RUNTIME unset (rejects without touching runtime)"
else
  fail_case "Case D" "watchdog-v2 fail-closed" "rc=$d_rc, err=$(cat "$d_err" 2>/dev/null || true)"
fi

echo "=================================================================="
echo "watchdog-resident-test.sh Summary: PASSED=$PASSED, FAILED=$FAILED"
echo "=================================================================="

if (( FAILED > 0 )); then
  exit 1
fi
exit 0
