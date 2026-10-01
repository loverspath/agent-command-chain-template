#!/usr/bin/env bash
# tests/test_task_abandon.sh: Isolated tests for bin/task-abandon.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ABANDON_SCRIPT="$SCRIPT_DIR/../bin/task-abandon.sh"

TMP_DIR="$(mktemp -d /tmp/test-task-abandon-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

MOCK_RT="$TMP_DIR/runtime"
mkdir -p "$MOCK_RT/workers" "$MOCK_RT/tasks" "$MOCK_RT/events/pending"
mkfifo "$MOCK_RT/event.fifo" 2>/dev/null || true

echo "=== Test 1: Missing arguments (exit 64) ==="
rc=0
"$ABANDON_SCRIPT" 2>/dev/null || rc=$?
if [[ "$rc" -ne 64 ]]; then
  echo "FAIL: Expected exit 64 for missing argument, got $rc" >&2
  exit 1
fi
echo "PASS: Missing argument test"

echo "=== Test 2: Missing runtime directory (exit 66) ==="
rc=0
ACC_RUNTIME="$TMP_DIR/nonexistent-rt" "$ABANDON_SCRIPT" agy-123 2>/dev/null || rc=$?
if [[ "$rc" -ne 66 ]]; then
  echo "FAIL: Expected exit 66 for missing runtime, got $rc" >&2
  exit 1
fi
echo "PASS: Missing runtime test"

echo "=== Test 3: Missing task directory (exit 66) ==="
rc=0
ACC_RUNTIME="$MOCK_RT" "$ABANDON_SCRIPT" agy-nonexistent 2>/dev/null || rc=$?
if [[ "$rc" -ne 66 ]]; then
  echo "FAIL: Expected exit 66 for missing task directory, got $rc" >&2
  exit 1
fi
echo "PASS: Missing task directory test"

echo "=== Test 4: Already terminal state (notice, exit 0, unchanged) ==="
TID_TERM="agy-terminal-1"
mkdir -p "$MOCK_RT/tasks/$TID_TERM"
cat > "$MOCK_RT/tasks/$TID_TERM/state" <<EOF
task_id=$TID_TERM
worker=agy
status=done
pid=999999
EOF
printf '%s\n' "$TID_TERM" > "$MOCK_RT/workers/agy.busy"

output="$(ACC_RUNTIME="$MOCK_RT" "$ABANDON_SCRIPT" "$TID_TERM")"
if [[ "$output" != *"already in terminal state 'done'"* ]]; then
  echo "FAIL: Expected terminal notice, got: $output" >&2
  exit 1
fi
cur_s="$(grep '^status=' "$MOCK_RT/tasks/$TID_TERM/state" | cut -d= -f2)"
if [[ "$cur_s" != "done" ]]; then
  echo "FAIL: State was modified from done to $cur_s" >&2
  exit 1
fi
if [[ -f "$MOCK_RT/workers/agy.busy" ]]; then
  echo "FAIL: Busy file was not cleared for terminal task" >&2
  exit 1
fi
echo "PASS: Already terminal state test"

echo "=== Test 5: Worker PID is alive without --force (refused, exit 1) ==="
# Spawn background sleep process
sleep 30 &
LIVE_PID=$!

TID_ALIVE="agy-alive-1"
mkdir -p "$MOCK_RT/tasks/$TID_ALIVE"
LIVE_TOKEN="$(awk '{print $22}' "/proc/$LIVE_PID/stat" 2>/dev/null || echo "")"
cat > "$MOCK_RT/tasks/$TID_ALIVE/state" <<EOF
task_id=$TID_ALIVE
worker=agy
status=running
pid=$LIVE_PID
proc_token=$LIVE_TOKEN
EOF
printf '%s\n' "$TID_ALIVE" > "$MOCK_RT/workers/agy.busy"

rc=0
ACC_RUNTIME="$MOCK_RT" "$ABANDON_SCRIPT" "$TID_ALIVE" 2>/dev/null || rc=$?
if [[ "$rc" -ne 1 ]]; then
  echo "FAIL: Expected exit 1 for alive process without --force, got $rc" >&2
  kill "$LIVE_PID" 2>/dev/null || true
  exit 1
fi
cur_s="$(grep '^status=' "$MOCK_RT/tasks/$TID_ALIVE/state" | cut -d= -f2)"
if [[ "$cur_s" != "running" ]]; then
  echo "FAIL: State was modified despite refusal: $cur_s" >&2
  kill "$LIVE_PID" 2>/dev/null || true
  exit 1
fi
echo "PASS: Alive PID without --force refusal test"

echo "=== Test 6: Worker PID is alive WITH --force (terminates & marks abandoned) ==="
ACC_RUNTIME="$MOCK_RT" "$ABANDON_SCRIPT" "$TID_ALIVE" --force
cur_s="$(grep '^status=' "$MOCK_RT/tasks/$TID_ALIVE/state" | cut -d= -f2)"
if [[ "$cur_s" != "abandoned" ]]; then
  echo "FAIL: Expected status=abandoned, got $cur_s" >&2
  kill "$LIVE_PID" 2>/dev/null || true
  exit 1
fi
sleep 0.1
if kill -0 "$LIVE_PID" 2>/dev/null; then
  echo "FAIL: Process $LIVE_PID was not terminated by SIGTERM" >&2
  kill -9 "$LIVE_PID" 2>/dev/null || true
  exit 1
fi
if [[ -f "$MOCK_RT/workers/agy.busy" ]]; then
  echo "FAIL: Busy file was not cleared" >&2
  exit 1
fi
echo "PASS: Alive PID with --force test"

echo "=== Test 7: Worker PID is dead -> atomic status=abandoned & lease cleared ==="
TID_DEAD="agy-dead-1"
mkdir -p "$MOCK_RT/tasks/$TID_DEAD"
cat > "$MOCK_RT/tasks/$TID_DEAD/state" <<EOF
task_id=$TID_DEAD
worker=agy
status=running
pid=999999
proc_token=dead
started_epoch=1727000000
EOF
printf '%s\n' "$TID_DEAD" > "$MOCK_RT/workers/agy.busy"
mkdir -p "$MOCK_RT/workers/agy.lock"
printf '%s\n' "999999" > "$MOCK_RT/workers/agy.lock/pid"

ACC_RUNTIME="$MOCK_RT" "$ABANDON_SCRIPT" "$TID_DEAD"
cur_s="$(grep '^status=' "$MOCK_RT/tasks/$TID_DEAD/state" | cut -d= -f2)"
if [[ "$cur_s" != "abandoned" ]]; then
  echo "FAIL: Expected status=abandoned, got $cur_s" >&2
  exit 1
fi
if [[ -f "$MOCK_RT/workers/agy.busy" ]]; then
  echo "FAIL: Busy file was not cleared" >&2
  exit 1
fi
if [[ -d "$MOCK_RT/workers/agy.lock" ]]; then
  echo "FAIL: Stale lock dir was not cleared" >&2
  exit 1
fi
echo "PASS: Dead PID recovery test"

echo "=== Test 8: Target specified by worker name ('agy') ==="
TID_BY_WORKER="agy-by-worker-1"
mkdir -p "$MOCK_RT/tasks/$TID_BY_WORKER"
cat > "$MOCK_RT/tasks/$TID_BY_WORKER/state" <<EOF
task_id=$TID_BY_WORKER
worker=agy
status=running
pid=999998
EOF
printf '%s\n' "$TID_BY_WORKER" > "$MOCK_RT/workers/agy.busy"

ACC_RUNTIME="$MOCK_RT" "$ABANDON_SCRIPT" agy
cur_s="$(grep '^status=' "$MOCK_RT/tasks/$TID_BY_WORKER/state" | cut -d= -f2)"
if [[ "$cur_s" != "abandoned" ]]; then
  echo "FAIL: Expected status=abandoned, got $cur_s" >&2
  exit 1
fi
if [[ -f "$MOCK_RT/workers/agy.busy" ]]; then
  echo "FAIL: Busy file was not cleared" >&2
  exit 1
fi
echo "PASS: Worker name target test"

echo "All tests in test_task_abandon.sh passed successfully!"
exit 0
