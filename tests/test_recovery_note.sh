#!/usr/bin/env bash
# tests/test_recovery_note.sh: Tests for Candidate 2 (RECOVERY_NOTE automatic injection)
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "recovery-note"
setup_mock_runtime "$TEST_RUNTIME"
export TEST_TMP

echo "=================================================================="
echo "Running test_recovery_note.sh (Marker: $TESTMARK)"
echo "Target runtime: $TEST_RUNTIME"
echo "=================================================================="

# Create mock tmux session
tmux new-session -d -s "$TEST_SESSION" -n agy -c "$TEST_TMP"
tmux new-window -t "$TEST_SESSION" -n codex -c "$TEST_TMP"
wait_pane_shell "$TEST_SESSION:agy"
wait_pane_shell "$TEST_SESSION:codex"

trap_cleanup() {
  tmux kill-session -t "$TEST_SESSION" 2>/dev/null || true
  cleanup_isolated_env
}
trap trap_cleanup EXIT

# Case 1: task-abandon.sh -> auto RECOVERY_NOTE injection & 1-time consumption
echo "--- Case 1: task-abandon.sh -> RECOVERY_NOTE & 1-time consumption ---"
old_task="agy-old-abandon-1"
old_dir="$TEST_RUNTIME/tasks/$old_task"
mkdir -p "$old_dir"
echo "Original old task prompt" > "$old_dir/prompt.md"
cat > "$old_dir/state" <<EOF
task_id=$old_task
worker=agy
status=running
pid=999999
proc_token=999999
started_epoch=$(date +%s)
EOF
printf '%s\n' "$old_task" > "$TEST_RUNTIME/workers/agy.busy"

# Abandon the task
ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" \
  "$TEMPLATE_ROOT/bin/task-abandon.sh" "$old_task" >/dev/null

interrupted_file="$TEST_RUNTIME/workers/agy.last_interrupted"
if [[ ! -f "$interrupted_file" ]]; then
  echo "FAIL: Expected $interrupted_file to be created by task-abandon.sh" >&2
  exit 1
fi

# Dispatch next task
ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" \
  "$TEMPLATE_ROOT/bin/dispatch.sh" agy --prompt "Continue work after abandon" >/dev/null

# Find newly created task
new_task="$(ls -1td "$TEST_RUNTIME"/tasks/agy-* | grep -v "$old_task" | head -n 1)"
new_prompt="$new_task/prompt.md"

if ! grep -q "\[RECOVERY NOTE\]" "$new_prompt"; then
  echo "FAIL: Expected [RECOVERY NOTE] in $new_prompt" >&2
  exit 1
fi
if ! grep -q "PREV_TASK_ID: $old_task" "$new_prompt"; then
  echo "FAIL: Expected PREV_TASK_ID: $old_task in $new_prompt" >&2
  exit 1
fi

# Check that marker is consumed (deleted)
if [[ -f "$interrupted_file" ]]; then
  echo "FAIL: Expected $interrupted_file to be consumed and deleted" >&2
  exit 1
fi

# Clean busy file of new_task so we can dispatch again
rm -f "$TEST_RUNTIME/workers/agy.busy"

# Third dispatch: should NOT have [RECOVERY NOTE]
ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" \
  "$TEMPLATE_ROOT/bin/dispatch.sh" agy --prompt "Normal third task" >/dev/null

third_task="$(ls -1td "$TEST_RUNTIME"/tasks/agy-* | grep -v "$old_task" | grep -v "$(basename "$new_task")" | head -n 1)"
third_prompt="$third_task/prompt.md"

if grep -q "\[RECOVERY NOTE\]" "$third_prompt"; then
  echo "FAIL: Third task should not have [RECOVERY NOTE] (marker was already consumed)" >&2
  exit 1
fi
echo "  [PASS] Case 1: task-abandon.sh created marker, dispatched task consumed it, 3rd task was clean"

# Clean busy
rm -f "$TEST_RUNTIME/workers/agy.busy"

# Case 2: watchdog-v2.sh PID reap -> auto RECOVERY_NOTE injection
echo "--- Case 2: watchdog-v2.sh PID reap -> RECOVERY_NOTE ---"
dead_task="codex-dead-pid-1"
dead_dir="$TEST_RUNTIME/tasks/$dead_task"
mkdir -p "$dead_dir"
echo "Dead worker prompt" > "$dead_dir/prompt.md"
cat > "$dead_dir/state" <<EOF
task_id=$dead_task
worker=codex
status=running
pid=999998
proc_token=999998
started_epoch=$(( $(date +%s) - 60 ))
deadline_epoch=$(( $(date +%s) + 1800 ))
EOF
printf '%s\n' "$dead_task" > "$TEST_RUNTIME/workers/codex.busy"

# Run watchdog oneshot
ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" \
  "$TEMPLATE_ROOT/watchdog-v2.sh" --oneshot >/dev/null 2>&1 || true

c_interrupted="$TEST_RUNTIME/workers/codex.last_interrupted"
if [[ ! -f "$c_interrupted" ]]; then
  echo "FAIL: Expected $c_interrupted after watchdog reap" >&2
  exit 1
fi
if ! grep -q "reason_code=worker_crash" "$c_interrupted"; then
  echo "FAIL: Expected reason_code=worker_crash in $c_interrupted" >&2
  exit 1
fi

# Dispatch next task
ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" \
  "$TEMPLATE_ROOT/bin/dispatch.sh" codex --prompt "Re-run after crash" >/dev/null

re_task="$(ls -1td "$TEST_RUNTIME"/tasks/codex-* | grep -v "$dead_task" | head -n 1)"
if ! grep -q "reason_code: worker_crash" "$re_task/prompt.md"; then
  echo "FAIL: Expected 'reason_code: worker_crash' in prompt of recovered task" >&2
  exit 1
fi
echo "  [PASS] Case 2: watchdog reap set worker_crash and dispatch injected it"

# Clean busy
rm -f "$TEST_RUNTIME/workers/codex.busy"

# Case 3: --recover-from <task_id> explicit specification
echo "--- Case 3: --recover-from explicit specification ---"
custom_prev="codex-prev-custom"
custom_dir="$TEST_RUNTIME/tasks/$custom_prev"
mkdir -p "$custom_dir"
echo "Custom prev prompt" > "$custom_dir/prompt.md"
cat > "$custom_dir/state" <<EOF
task_id=$custom_prev
worker=codex
status=error
reason_code=timeout
started_epoch=$(date +%s)
EOF

ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" \
  "$TEMPLATE_ROOT/bin/dispatch.sh" codex --recover-from "$custom_prev" --prompt "Task recovering from custom" >/dev/null

custom_new="$(ls -1td "$TEST_RUNTIME"/tasks/codex-* | grep -v "$dead_task" | grep -v "$custom_prev" | head -n 1)"
if ! grep -q "PREV_TASK_ID: $custom_prev" "$custom_new/prompt.md"; then
  echo "FAIL: Expected PREV_TASK_ID: $custom_prev in prompt" >&2
  exit 1
fi
if ! grep -q "recovered_from=$custom_prev" "$custom_new/recovery.meta"; then
  echo "FAIL: Expected recovered_from=$custom_prev in recovery.meta" >&2
  exit 1
fi
echo "  [PASS] Case 3: --recover-from explicitly bound and injected"

# Clean busy
rm -f "$TEST_RUNTIME/workers/codex.busy"

# Case 4: --no-recovery-note & ACC_NO_BRIEF_HEADER=1 suppression
echo "--- Case 4: Suppression options ---"
# Set marker
printf 'task_id=suppress-target\nstatus=abandoned\nreason_code=interrupted\ninterrupted_epoch=%s\n' "$(date +%s)" > "$TEST_RUNTIME/workers/codex.last_interrupted"

ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" \
  "$TEMPLATE_ROOT/bin/dispatch.sh" codex --no-recovery-note --prompt "Suppressed note task" >/dev/null

sup_task="$(ls -1td "$TEST_RUNTIME"/tasks/codex-* | head -n 1)"
if grep -q "\[RECOVERY NOTE\]" "$sup_task/prompt.md"; then
  echo "FAIL: Expected [RECOVERY NOTE] to be suppressed by --no-recovery-note" >&2
  exit 1
fi
echo "  [PASS] Case 4: --no-recovery-note successfully suppressed note"

echo "=================================================================="
echo "test_recovery_note.sh: ALL CASES PASSED"
echo "=================================================================="
exit 0
