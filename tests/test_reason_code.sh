#!/usr/bin/env bash
# tests/test_reason_code.sh: Tests for Candidate 1 (reason_code elaboration)
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "reason-code"
setup_mock_runtime "$TEST_RUNTIME"
export TEST_TMP

TEST_BIN="$TEST_HOME/.local/bin"
mkdir -p "$TEST_BIN"
export PATH="$TEST_BIN:$PATH"
export CODEX_MODEL="mock-sol"

echo "=================================================================="
echo "Running test_reason_code.sh (Marker: $TESTMARK)"
echo "Target runtime: $TEST_RUNTIME"
echo "=================================================================="

# Create flexible mock codex binary
cat > "$TEST_BIN/codex" <<'EOF'
#!/usr/bin/env bash
if [[ -f "$TEST_TMP/mock_output.txt" ]]; then
  cat "$TEST_TMP/mock_output.txt"
fi
exit "${MOCK_RC:-1}"
EOF
chmod +x "$TEST_BIN/codex"

# Case 1: Quota 429 automatic classification in run-task.sh
echo "--- Case 1: Quota 429 automatic classification ---"
task1="codex-quota-1"
t1_dir="$TEST_RUNTIME/tasks/$task1"
mkdir -p "$t1_dir"
echo "Test prompt" > "$t1_dir/prompt.md"
printf '%s\n' "$task1" > "$TEST_RUNTIME/workers/codex.busy"

echo "Error: You have hit your usage limit. Please try again at 14:00." > "$TEST_TMP/mock_output.txt"
export MOCK_RC=1

rc=0
SESSION_NAME="$TEST_SESSION" ACC_RUNTIME="$TEST_RUNTIME" HOME="$TEST_HOME" \
  "$TEMPLATE_ROOT/bin/run-task.sh" codex "$task1" "$t1_dir/prompt.md" 10 || rc=$?

evt_file="$TEST_RUNTIME/events/pending/${task1}-terminal.evt"
if [[ ! -f "$evt_file" ]]; then
  echo "FAIL: Expected terminal event file $evt_file to exist" >&2
  exit 1
fi
rc_in_evt="$(grep '^reason_code=' "$evt_file" | cut -d= -f2-)"
if [[ "$rc_in_evt" != "transient_quota" ]]; then
  echo "FAIL: Expected reason_code=transient_quota in .evt, got '$rc_in_evt'" >&2
  exit 1
fi

rc_in_state="$(grep '^reason_code=' "$t1_dir/state" | cut -d= -f2-)"
if [[ "$rc_in_state" != "transient_quota" ]]; then
  echo "FAIL: Expected reason_code=transient_quota in state, got '$rc_in_state'" >&2
  exit 1
fi
echo "  [PASS] Case 1: Quota 429 auto-classified as transient_quota"

# Case 2: Transient network error classification
echo "--- Case 2: Network error classification ---"
task2="codex-net-1"
t2_dir="$TEST_RUNTIME/tasks/$task2"
mkdir -p "$t2_dir"
echo "Test prompt" > "$t2_dir/prompt.md"
printf '%s\n' "$task2" > "$TEST_RUNTIME/workers/codex.busy"

echo "503 Service Unavailable: ECONNRESET while streaming" > "$TEST_TMP/mock_output.txt"
export MOCK_RC=1

rc=0
SESSION_NAME="$TEST_SESSION" ACC_RUNTIME="$TEST_RUNTIME" HOME="$TEST_HOME" \
  "$TEMPLATE_ROOT/bin/run-task.sh" codex "$task2" "$t2_dir/prompt.md" 10 || rc=$?

evt_file2="$TEST_RUNTIME/events/pending/${task2}-terminal.evt"
rc_in_evt2="$(grep '^reason_code=' "$evt_file2" | cut -d= -f2-)"
if [[ "$rc_in_evt2" != "transient_network" ]]; then
  echo "FAIL: Expected reason_code=transient_network in .evt, got '$rc_in_evt2'" >&2
  exit 1
fi
echo "  [PASS] Case 2: Network error auto-classified as transient_network"

# Case 3: agy-stop-hook.sh BLOCKED reason parsing
echo "--- Case 3: agy-stop-hook.sh BLOCKED reason parsing ---"
task3="agy-blocked-1"
t3_dir="$TEST_RUNTIME/tasks/$task3"
mkdir -p "$t3_dir"
echo "Test prompt" > "$t3_dir/prompt.md"
cat > "$t3_dir/state" <<EOF
task_id=$task3
worker=agy
status=running
mode=resident
pid=$$
pgid=$$
proc_token=$$
started_epoch=$(date +%s)
deadline_epoch=$(( $(date +%s) + 1800 ))
EOF
printf '%s\n%s\n' "$task3" "$(date +%s)" > "$TEST_RUNTIME/workers/agy.busy"

# Prepare resident marker
cat > "$TEST_RUNTIME/workers/agy.resident" <<EOF
pane_id=%1
session=$TEST_SESSION
EOF

# Fake transcript with [[BLOCKED task3]] reason=subagent-unavailable
trans_file="$TEST_TMP/c3_transcript.jsonl"
cat > "$trans_file" <<EOF
{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "작업 수행"}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "에러 발생: 서브에이전트 사용 불가\n[[BLOCKED $task3]] reason=subagent-unavailable"}
EOF

payload=$(cat <<EOF
{
  "conversationId": "conv-$task3",
  "fullyIdle": true,
  "transcriptPath": "$trans_file"
}
EOF
)

ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="%1" \
  "$TEMPLATE_ROOT/bin/agy-stop-hook.sh" <<<"$payload" >/dev/null

evt_file3="$TEST_RUNTIME/events/pending/${task3}-terminal.evt"
if [[ ! -f "$evt_file3" ]]; then
  echo "FAIL: Expected terminal event $evt_file3 to exist" >&2
  exit 1
fi
rc_in_evt3="$(grep '^reason_code=' "$evt_file3" | cut -d= -f2-)"
if [[ "$rc_in_evt3" != "subagent_unavailable" ]]; then
  echo "FAIL: Expected reason_code=subagent_unavailable, got '$rc_in_evt3'" >&2
  exit 1
fi
echo "  [PASS] Case 3: agy-stop-hook extracted BLOCKED reason_code"

# Case 4: sonnet-event-wait.sh batch format and hint
echo "--- Case 4: sonnet-event-wait.sh batch format with reason_code ---"
# Clear pending and add one quota event and one old event without reason_code
rm -rf "$TEST_RUNTIME/events/pending"/* "$TEST_RUNTIME/events/inflight"/*
cat > "$TEST_RUNTIME/events/pending/evt-with-reason.evt" <<EOF
version=1
id=evt-with-reason
source=codex
kind=error
reason_code=transient_quota
task_id=task-q1
created_epoch=$(date +%s)
exit_code=1
summary_b64=$(printf 'Quota exceeded' | base64 -w0)
detail_path_b64=$(printf '/tmp/log.txt' | base64 -w0)
EOF

cat > "$TEST_RUNTIME/events/pending/evt-legacy.evt" <<EOF
version=1
id=evt-legacy
source=codex
kind=done
task_id=task-legacy
created_epoch=$(date +%s)
exit_code=0
summary_b64=$(printf 'Done legacy' | base64 -w0)
detail_path_b64=$(printf '/tmp/log2.txt' | base64 -w0)
EOF

wait_err="$TEST_TMP/wait_err.txt"
rc=0
ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" HOME="$TEST_HOME" \
  "$TEMPLATE_ROOT/bin/sonnet-event-wait.sh" wait "$TEST_RUNTIME" 2>"$wait_err" || rc=$?

if [[ "$rc" -ne 2 ]]; then
  echo "FAIL: Expected exit 2 from sonnet-event-wait, got $rc" >&2
  exit 1
fi

if ! grep -q "reason=transient_quota" "$wait_err"; then
  echo "FAIL: Expected 'reason=transient_quota' in event batch output" >&2
  exit 1
fi

if ! grep -q "\[HINT\] reason=transient_quota 감지됨" "$wait_err"; then
  echo "FAIL: Expected quota hint in batch output" >&2
  exit 1
fi

if ! grep -q "kind=done worker=codex task=task-legacy" "$wait_err"; then
  echo "FAIL: Expected legacy event format preserved for event without reason_code" >&2
  exit 1
fi
echo "  [PASS] Case 4: sonnet-event-wait batch output formatted properly"

echo "=================================================================="
echo "test_reason_code.sh: ALL CASES PASSED"
echo "=================================================================="
exit 0
