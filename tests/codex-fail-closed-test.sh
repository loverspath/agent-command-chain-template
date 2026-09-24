#!/usr/bin/env bash
# tests/codex-fail-closed-test.sh: Codex resident fail-closed 검증 (Task F7)
# 검증 항목:
#   1. bootstrap-v2.sh exits 64 when CODEX_MODE=resident
#   2. bootstrap-v2.sh exits 64 when WORKER_MODE=resident (un-overridden CODEX_MODE)
#   3. dispatch.sh codex exits 64 when CODEX_MODE=resident
#   4. dispatch.sh codex exits 64 when WORKER_MODE=resident
#   5. watchdog-v2.sh exits 64 when CODEX_MODE=resident
#   6. watchdog-v2.sh exits 64 when WORKER_MODE=resident
#   7. AGY_MODE=resident with CODEX_MODE=oneshot is accepted (no exit 64)
#   8. WORKER_MODE=resident with CODEX_MODE=oneshot is accepted (no exit 64)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "codex-fail-closed"
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
echo "Running codex-fail-closed-test.sh (Marker: $TESTMARK)"
echo "Target runtime: $TEST_RUNTIME"
echo "=================================================================="

PROJ_DIR="$TEST_TMP/proj"
mkdir -p "$PROJ_DIR"
TEST_BIN="$TEST_TMP/bin"
mkdir -p "$TEST_BIN"
stub_agy="$(build_stub_agy "$TEST_BIN")"

# ------------------------------------------------------------------
# Case 1: bootstrap-v2.sh exits 64 when CODEX_MODE=resident
# ------------------------------------------------------------------
c1_out=""
c1_rc=0
c1_out=$(run_clean \
  PROJECT_DIR="$PROJ_DIR" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  CODEX_MODE=resident \
  -- "$HERE/bootstrap-v2.sh" 2>&1) || c1_rc=$?

if (( c1_rc == 64 )) && [[ "$c1_out" == *"codex"* ]]; then
  pass_case 1 "bootstrap-v2.sh exits 64 when CODEX_MODE=resident"
else
  fail_case 1 "bootstrap-v2.sh CODEX_MODE=resident" "rc=$c1_rc, out=$c1_out"
fi

# ------------------------------------------------------------------
# Case 2: bootstrap-v2.sh exits 64 when WORKER_MODE=resident (un-overridden)
# ------------------------------------------------------------------
c2_out=""
c2_rc=0
c2_out=$(run_clean \
  PROJECT_DIR="$PROJ_DIR" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  WORKER_MODE=resident \
  -- "$HERE/bootstrap-v2.sh" 2>&1) || c2_rc=$?

if (( c2_rc == 64 )) && [[ "$c2_out" == *"codex"* ]]; then
  pass_case 2 "bootstrap-v2.sh exits 64 when WORKER_MODE=resident"
else
  fail_case 2 "bootstrap-v2.sh WORKER_MODE=resident" "rc=$c2_rc, out=$c2_out"
fi

# ------------------------------------------------------------------
# Case 3: dispatch.sh codex exits 64 when CODEX_MODE=resident
# ------------------------------------------------------------------
tmux new-session -d -s "$TEST_SESSION" -n sonnet -c "$PROJ_DIR"
tmux new-window -t "$TEST_SESSION" -n codex -c "$PROJ_DIR"
wait_pane_shell "$TEST_SESSION:codex" 5

c3_out=""
c3_rc=0
c3_out=$(run_clean \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  CODEX_MODE=resident \
  -- "$HERE/bin/dispatch.sh" codex --dry-run 2>&1) || c3_rc=$?

if (( c3_rc == 64 )) && [[ "$c3_out" == *"codex"* ]]; then
  pass_case 3 "dispatch.sh codex exits 64 when CODEX_MODE=resident"
else
  fail_case 3 "dispatch.sh codex CODEX_MODE=resident" "rc=$c3_rc, out=$c3_out"
fi

# ------------------------------------------------------------------
# Case 4: dispatch.sh codex exits 64 when WORKER_MODE=resident
# ------------------------------------------------------------------
c4_out=""
c4_rc=0
c4_out=$(run_clean \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  WORKER_MODE=resident \
  -- "$HERE/bin/dispatch.sh" codex --dry-run 2>&1) || c4_rc=$?

if (( c4_rc == 64 )) && [[ "$c4_out" == *"codex"* ]]; then
  pass_case 4 "dispatch.sh codex exits 64 when WORKER_MODE=resident"
else
  fail_case 4 "dispatch.sh codex WORKER_MODE=resident" "rc=$c4_rc, out=$c4_out"
fi

# ------------------------------------------------------------------
# Case 5: watchdog-v2.sh exits 64 when CODEX_MODE=resident
# ------------------------------------------------------------------
c5_out=""
c5_rc=0
c5_out=$(run_clean \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  PROJECT_DIR="$PROJ_DIR" \
  CODEX_MODE=resident \
  -- "$HERE/watchdog-v2.sh" 2>&1) || c5_rc=$?

if (( c5_rc == 64 )) && [[ "$c5_out" == *"codex"* ]]; then
  pass_case 5 "watchdog-v2.sh exits 64 when CODEX_MODE=resident"
else
  fail_case 5 "watchdog-v2.sh CODEX_MODE=resident" "rc=$c5_rc, out=$c5_out"
fi

# ------------------------------------------------------------------
# Case 6: watchdog-v2.sh exits 64 when WORKER_MODE=resident
# ------------------------------------------------------------------
c6_out=""
c6_rc=0
c6_out=$(run_clean \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  PROJECT_DIR="$PROJ_DIR" \
  WORKER_MODE=resident \
  -- "$HERE/watchdog-v2.sh" 2>&1) || c6_rc=$?

if (( c6_rc == 64 )) && [[ "$c6_out" == *"codex"* ]]; then
  pass_case 6 "watchdog-v2.sh exits 64 when WORKER_MODE=resident"
else
  fail_case 6 "watchdog-v2.sh WORKER_MODE=resident" "rc=$c6_rc, out=$c6_out"
fi

# ------------------------------------------------------------------
# Case 7: AGY_MODE=resident with CODEX_MODE=oneshot is accepted
# ------------------------------------------------------------------
tmux new-window -t "$TEST_SESSION" -n agy -c "$PROJ_DIR"
wait_pane_shell "$TEST_SESSION:agy" 5
tmux send-keys -t "$TEST_SESSION:agy" "$stub_agy" C-m
# Wait for stub agy to start
wait_agy=0
while (( wait_agy < 30 )); do
  cur_cmd="$(tmux display-message -p -t "$TEST_SESSION:agy" '#{pane_current_command}' 2>/dev/null || true)"
  if [[ "$cur_cmd" == "agy" ]]; then
    break
  fi
  sleep 0.1
  wait_agy=$((wait_agy + 1))
done

# Test dispatch agy with resident mode
c7_agy_rc=0
c7_agy_out=$(run_clean \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  AGY_MODE=resident \
  CODEX_MODE=oneshot \
  -- "$HERE/bin/dispatch.sh" agy --dry-run 2>&1) || c7_agy_rc=$?

# Test dispatch codex with oneshot mode
c7_cdx_rc=0
c7_cdx_out=$(run_clean \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  AGY_MODE=resident \
  CODEX_MODE=oneshot \
  -- "$HERE/bin/dispatch.sh" codex --dry-run 2>&1) || c7_cdx_rc=$?

if (( c7_agy_rc == 0 )) && (( c7_cdx_rc == 0 )); then
  pass_case 7 "AGY_MODE=resident with CODEX_MODE=oneshot permitted (agy dry-run rc=0, codex dry-run rc=0)"
else
  fail_case 7 "AGY_MODE=resident CODEX_MODE=oneshot" "agy_rc=$c7_agy_rc, cdx_rc=$c7_cdx_rc, agy_out=$c7_agy_out, cdx_out=$c7_cdx_out"
fi

# ------------------------------------------------------------------
# Case 8: WORKER_MODE=resident with explicit CODEX_MODE=oneshot is accepted
# ------------------------------------------------------------------
c8_agy_rc=0
c8_agy_out=$(run_clean \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  WORKER_MODE=resident \
  CODEX_MODE=oneshot \
  -- "$HERE/bin/dispatch.sh" agy --dry-run 2>&1) || c8_agy_rc=$?

c8_cdx_rc=0
c8_cdx_out=$(run_clean \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  WORKER_MODE=resident \
  CODEX_MODE=oneshot \
  -- "$HERE/bin/dispatch.sh" codex --dry-run 2>&1) || c8_cdx_rc=$?

if (( c8_agy_rc == 0 )) && (( c8_cdx_rc == 0 )); then
  pass_case 8 "WORKER_MODE=resident with CODEX_MODE=oneshot permitted (agy rc=0, codex rc=0)"
else
  fail_case 8 "WORKER_MODE=resident CODEX_MODE=oneshot" "agy_rc=$c8_agy_rc, cdx_rc=$c8_cdx_rc"
fi

echo "=================================================================="
echo "codex-fail-closed-test.sh Summary: PASSED=$PASSED, FAILED=$FAILED"
echo "=================================================================="

if (( FAILED > 0 )); then
  exit 1
fi
exit 0
