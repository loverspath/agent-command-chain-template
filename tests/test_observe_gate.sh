#!/usr/bin/env bash
# tests/test_observe_gate.sh: Candidate 6 (observe mode & deterministic gate) verification
# Verifies:
#   1. acc_gate_evaluate_event logic (PASS on task done/error, DROP_SELF on self-echo, DROP_DEDUPE on duplicate, mode branching)
#   2. acc_gate_archive_observed schema and file archiving
#   3. sonnet-event-wait.sh in observe mode: proactive event archived, audit logged, no supervisor wake
#   4. sonnet-event-wait.sh with done/error event: supervisor wakes immediately
#   5. sonnet-event-wait.sh in notify mode: proactive event wakes supervisor with [PROACTIVE_TRIGGER_NOTICE]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "observe-gate"
setup_mock_runtime "$TEST_RUNTIME"
export ACC_RUNTIME="$TEST_RUNTIME"

# shellcheck disable=SC1091
source "$HERE/lib/gate.sh"

echo "=== Case 1: acc_gate_evaluate_event deterministic rules ==="
evt_pass="$TEST_TMP/task-done.evt"
cat > "$evt_pass" <<EOF
id=evt-1
source=agy
kind=done
task_id=task-1
EOF
res="$(acc_gate_evaluate_event "$evt_pass" "$TEST_RUNTIME")"
if [[ "$res" == "PASS" ]]; then
  echo "PASS: done event yields PASS"
else
  echo "FAIL: done event yielded $res" >&2
  exit 1
fi

evt_err="$TEST_TMP/task-err.evt"
cat > "$evt_err" <<EOF
id=evt-2
source=agy
kind=error
task_id=task-2
EOF
res="$(acc_gate_evaluate_event "$evt_err" "$TEST_RUNTIME")"
if [[ "$res" == "PASS" ]]; then
  echo "PASS: error event yields PASS"
else
  echo "FAIL: error event yielded $res" >&2
  exit 1
fi

# Self-actor echo drop
evt_self="$TEST_TMP/proactive-self.evt"
cat > "$evt_self" <<EOF
id=evt-3
source=poller
kind=proactive
task_id=task-3
actor=agentchain
EOF
res="$(acc_gate_evaluate_event "$evt_self" "$TEST_RUNTIME")"
if [[ "$res" == "DROP_SELF" ]]; then
  echo "PASS: self-actor event yields DROP_SELF"
else
  echo "FAIL: self-actor event yielded $res" >&2
  exit 1
fi

# Deduplication
evt_dedupe="$TEST_TMP/proactive-dedupe.evt"
cat > "$evt_dedupe" <<EOF
id=evt-4
source=poller
kind=proactive
task_id=task-4
dedupe_key=dup-key-1
EOF
mkdir -p "$TEST_RUNTIME/events/dedupe"
touch "$TEST_RUNTIME/events/dedupe/dup-key-1"
res="$(acc_gate_evaluate_event "$evt_dedupe" "$TEST_RUNTIME")"
if [[ "$res" == "DROP_DEDUPE" ]]; then
  echo "PASS: duplicate key yields DROP_DEDUPE"
else
  echo "FAIL: duplicate key yielded $res" >&2
  exit 1
fi
rm -f "$TEST_RUNTIME/events/dedupe/dup-key-1"

# Observe mode default
evt_pro="$TEST_TMP/proactive-normal.evt"
cat > "$evt_pro" <<EOF
id=evt-5
source=poller
kind=proactive
task_id=task-5
actor=external_user
EOF
res="$(ACC_PROACTIVE_MODE=observe acc_gate_evaluate_event "$evt_pro" "$TEST_RUNTIME")"
if [[ "$res" == "OBSERVE_ONLY" ]]; then
  echo "PASS: observe mode yields OBSERVE_ONLY"
else
  echo "FAIL: observe mode yielded $res" >&2
  exit 1
fi

# Notify mode
res="$(ACC_PROACTIVE_MODE=notify acc_gate_evaluate_event "$evt_pro" "$TEST_RUNTIME")"
if [[ "$res" == "PASS" ]]; then
  echo "PASS: notify mode yields PASS"
else
  echo "FAIL: notify mode yielded $res" >&2
  exit 1
fi

# Off mode
res="$(ACC_PROACTIVE_MODE=off acc_gate_evaluate_event "$evt_pro" "$TEST_RUNTIME")"
if [[ "$res" == "DROP_OFF" ]]; then
  echo "PASS: off mode yields DROP_OFF"
else
  echo "FAIL: off mode yielded $res" >&2
  exit 1
fi

echo "=== Case 2: acc_gate_archive_observed schema and file move ==="
evt_archive="$TEST_TMP/proactive-archive.evt"
cat > "$evt_archive" <<EOF
id=evt-arch-1
source=cron
kind=proactive
task_id=none
rule_id=rule-backup-check
actor=system
summary_b64=$(printf 'Scheduled health audit alert' | base64)
EOF
acc_gate_archive_observed "$evt_archive" "$TEST_RUNTIME" "rule-backup-check"

date_str="$(date +%Y%m%d)"
audit_log="$TEST_RUNTIME/events/archive/observed-$date_str/observed-audit.jsonl"
if [[ -f "$audit_log" ]]; then
  echo "PASS: observed-audit.jsonl created"
else
  echo "FAIL: observed-audit.jsonl not found at $audit_log" >&2
  exit 1
fi

audit_line="$(head -n 1 "$audit_log")"
audit_ok=$(python3 -c '
import sys, json
d = json.loads(sys.argv[1])
assert d.get("event_id") == "evt-arch-1"
assert d.get("kind") == "proactive"
assert d.get("mode") == "observe"
assert d.get("would_accept") is True
assert d.get("rule_id") == "rule-backup-check"
assert "health audit alert" in d.get("summary", "")
print("ok")
' "$audit_line" 2>/dev/null || echo "invalid")

if [[ "$audit_ok" == "ok" ]]; then
  echo "PASS: observed-audit.jsonl record schema validated"
else
  echo "FAIL: observed-audit.jsonl invalid line: $audit_line" >&2
  exit 1
fi

echo "=== Case 3: sonnet-event-wait.sh in observe mode (drains proactive, no supervisor wake) ==="
# Setup runtime for event-wait
mkdir -p "$TEST_RUNTIME/events/pending" "$TEST_RUNTIME/events/inflight" "$TEST_RUNTIME/events/archive"
fifo="$TEST_RUNTIME/event.fifo"
[[ -p "$fifo" ]] || mkfifo "$fifo"

# Emit proactive event into pending
"$HERE/bin/event-emit.sh" "cron" "proactive" "none" "Scheduled cron heartbeat" "" "" "none"

export ACC_RUNTIME="$TEST_RUNTIME"
export ACC_PROACTIVE_MODE="observe"

out=""
out="$(timeout 1 "$HERE/bin/sonnet-event-wait.sh" run "$TEST_RUNTIME" 2>&1 || true)"

# Verify no batch was delivered to supervisor
if [[ "$out" == *"[ACC_EVENT_BATCH"* ]]; then
  echo "FAIL: supervisor received proactive event batch in observe mode!" >&2
  exit 1
else
  echo "PASS: supervisor was NOT woken up in observe mode"
fi

# Verify proactive event was archived to observed-*
arch_evts=$(find "$TEST_RUNTIME/events/archive"/observed-* -name "*.evt" 2>/dev/null | wc -l)
if (( arch_evts > 0 )); then
  echo "PASS: proactive event was cleanly moved to observed-* archive ($arch_evts events)"
else
  echo "FAIL: proactive event was not archived to observed-*" >&2
  exit 1
fi

echo "=== Case 4: sonnet-event-wait.sh wakes immediately for done/error events ==="
"$HERE/bin/event-emit.sh" "agy" "done" "task-c4-ok" "Task 4 completed normally" "" "" "ok"
c4_out="$(timeout 2 "$HERE/bin/sonnet-event-wait.sh" run "$TEST_RUNTIME" 2>&1 || true)"
if [[ "$c4_out" == *"[ACC_EVENT_BATCH"* && "$c4_out" == *"task-c4-ok"* ]]; then
  echo "PASS: done event woke supervisor immediately with batch"
else
  echo "FAIL: done event did not wake supervisor (out: $c4_out)" >&2
  exit 1
fi

echo "=== Case 5: sonnet-event-wait.sh in notify mode wakes with proactive notice ==="
# Emit proactive event with ACC_PROACTIVE_MODE=notify
export ACC_PROACTIVE_MODE="notify"
"$HERE/bin/event-emit.sh" "poller" "proactive" "none" "High priority user ping" "" "" "none"
c5_out="$(timeout 2 "$HERE/bin/sonnet-event-wait.sh" run "$TEST_RUNTIME" 2>&1 || true)"
if [[ "$c5_out" == *"[ACC_EVENT_BATCH"* && "$c5_out" == *"[PROACTIVE_TRIGGER_NOTICE]"* ]]; then
  echo "PASS: notify mode woke supervisor with [PROACTIVE_TRIGGER_NOTICE]"
else
  echo "FAIL: notify mode failed (out: $c5_out)" >&2
  exit 1
fi

echo "=================================================================="
echo "test_observe_gate.sh: ALL TESTS PASSED (100% OK)"
echo "=================================================================="
