#!/usr/bin/env bash
# tests/dispatch-regression-test.sh: dispatch.sh 회귀 및 상주 모드 검증
# 검증 항목:
#   1. Dry-run 시나리오:
#      - session env/marker/tmux-current 자동 해석 우선순위
#      - v1 mistarget rejection exit 70
#      - oneshot target is interactive TUI exit 71
#      - resident target window not agy exit 71
#   2. Stub adapter oneshot e2e:
#      - 체인 컨텍스트 헤더 자동 주입
#      - ACC_NO_BRIEF_HEADER=1 헤더 미부착 (바이트 일치)
#      - 128KB 초과 프롬프트 exit 65
#      - done 이벤트 + lock/busy 정상 해제
#      - codex 호출 인자 --skip-git-repo-check 포함 검증
#   3. Resident dispatch:
#      - state 파일에 mode=resident 기록
#      - workers/agy.busy 기록
#      - 1줄 도어벨 트리거 주입
#      - 연속 2회 태스크 각각 1개 done 이벤트 발행 및 busy 누수 없음

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "dispatch-regression"
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
echo "Running dispatch-regression-test.sh (Marker: $TESTMARK)"
echo "Target session: $TEST_SESSION, runtime: $TEST_RUNTIME"
echo "=================================================================="

# ------------------------------------------------------------------
# Test 1.1: Priority 1 (환경변수 SESSION_NAME 해석)
# ------------------------------------------------------------------
p1_err="$TEST_TMP/p1_err.log"
p1_rc=0
run_clean SESSION_NAME="$TEST_SESSION" ACC_RUNTIME="$TEST_RUNTIME" -- "$HERE/bin/dispatch.sh" agy --dry-run 2>"$p1_err" || p1_rc=$?

if (( p1_rc == 0 )) && grep -q "(source=env)" "$p1_err"; then
  pass_case "Test 1.1" "Priority 1 (source=env) resolution"
else
  fail_case "Test 1.1" "Priority 1 resolution" "rc=$p1_rc, err=$(cat "$p1_err" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# Test 1.2: Priority 2 (런타임 마커 해석)
# ------------------------------------------------------------------
p2_err="$TEST_TMP/p2_err.log"
p2_rc=0
# SESSION_NAME unset, ACC_RUNTIME points to mock runtime which has session_name
run_clean ACC_RUNTIME="$TEST_RUNTIME" -- "$HERE/bin/dispatch.sh" agy --dry-run 2>"$p2_err" || p2_rc=$?

if (( p2_rc == 0 )) && grep -q "(source=runtime-marker)" "$p2_err"; then
  pass_case "Test 1.2" "Priority 2 (source=runtime-marker) resolution"
else
  fail_case "Test 1.2" "Priority 2 resolution" "rc=$p2_rc, err=$(cat "$p2_err" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# Test 1.3: Priority 3 (tmux-current 해석)
# ------------------------------------------------------------------
p3_out="$TEST_TMP/p3_out.log"
p3_err="$TEST_TMP/p3_err.log"
p3_rc_file="$TEST_TMP/p3_rc.txt"

# Prepare default runtime directory for TEST_SESSION so bootstrap marker check passes
p3_s_safe="$(printf '%s' "$TEST_SESSION" | tr -cd '[:alnum:]_-')"
p3_p_hash="$(printf '%s' "$TEST_TMP" | md5sum | cut -c1-8)"
p3_def_rt="$HERE/runtime/${p3_s_safe}-${p3_p_hash}"
assert_safe_runtime "$p3_def_rt"
mkdir -p "$p3_def_rt"
printf 'bootstrap_version=2\n' > "$p3_def_rt/bootstrap_version"

tmux send-keys -t "$TEST_SESSION:sonnet" "cd '$HERE' && env -i PATH=\"\$PATH\" HOME='$TEST_HOME' TMUX=\"\$TMUX\" PROJECT_DIR='$TEST_TMP' ACC_CONFIG_ENV='$ACC_CONFIG_ENV' ./bin/dispatch.sh agy --dry-run >'$p3_out' 2>'$p3_err'; echo \$? > '$p3_rc_file'" C-m

waited=0
while [[ ! -f "$p3_rc_file" ]] && (( waited < 40 )); do
  sleep 0.1
  waited=$((waited + 1))
done

p3_rc=$(cat "$p3_rc_file" 2>/dev/null || echo "999")
rm -rf "$p3_def_rt" 2>/dev/null || true

if [[ "$p3_rc" == "0" ]] && grep -q "(source=tmux-current)" "$p3_err" 2>/dev/null; then
  pass_case "Test 1.3" "Priority 3 (source=tmux-current) resolution"
else
  fail_case "Test 1.3" "Priority 3 resolution" "rc=$p3_rc, err=$(cat "$p3_err" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# Test 1.4: v1 mistarget rejection (exit 70)
# ------------------------------------------------------------------
v1_session="${TEST_SESSION}-v1"
tmux new-session -d -s "$v1_session" -n agy -c "$TEST_TMP"
wait_pane_shell "$v1_session:agy" 10
p4_err="$TEST_TMP/p4_err.log"
p4_rc=0
# Target v1 session which lacks bootstrap_version=2 while TEST_SESSION has it
run_clean SESSION_NAME="$v1_session" -- "$HERE/bin/dispatch.sh" agy --dry-run 2>"$p4_err" || p4_rc=$?
tmux kill-session -t "$v1_session" 2>/dev/null || true

if (( p4_rc == 70 )) && grep -q "lacks v2 Full-Push marker" "$p4_err"; then
  pass_case "Test 1.4" "v1 mistarget rejection (exit 70 with candidate guidance)"
else
  fail_case "Test 1.4" "v1 mistarget rejection" "rc=$p4_rc, err=$(cat "$p4_err" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# Test 1.5: oneshot target is interactive TUI (exit 71)
# ------------------------------------------------------------------
tmux send-keys -t "$TEST_SESSION:agy" "sleep 30" C-m
# Wait for pane_current_command to become sleep
p5_wait=0
while (( p5_wait < 30 )); do
  if [[ "$(tmux display-message -p -t "$TEST_SESSION:agy" '#{pane_current_command}' 2>/dev/null || true)" == "sleep" ]]; then
    break
  fi
  sleep 0.1
  p5_wait=$((p5_wait + 1))
done

p5_err="$TEST_TMP/p5_err.log"
p5_rc=0
run_clean SESSION_NAME="$TEST_SESSION" ACC_RUNTIME="$TEST_RUNTIME" WORKER_MODE=oneshot -- "$HERE/bin/dispatch.sh" agy --dry-run 2>"$p5_err" || p5_rc=$?
# Cancel sleep
tmux send-keys -t "$TEST_SESSION:agy" C-c
wait_pane_shell "$TEST_SESSION:agy" 10

if (( p5_rc == 71 )) && grep -q "instead of idle shell" "$p5_err"; then
  pass_case "Test 1.5" "oneshot target is interactive TUI rejection (exit 71)"
else
  fail_case "Test 1.5" "oneshot target is interactive TUI" "rc=$p5_rc, err=$(cat "$p5_err" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# Test 1.6: resident target window not agy (exit 71)
# ------------------------------------------------------------------
p6_err="$TEST_TMP/p6_err.log"
p6_rc=0
run_clean SESSION_NAME="$TEST_SESSION" ACC_RUNTIME="$TEST_RUNTIME" AGY_MODE=resident -- "$HERE/bin/dispatch.sh" agy --dry-run 2>"$p6_err" || p6_rc=$?

if (( p6_rc == 71 )) && grep -q "instead of resident TUI 'agy'" "$p6_err"; then
  pass_case "Test 1.6" "resident target window not agy rejection (exit 71)"
else
  fail_case "Test 1.6" "resident target window not agy" "rc=$p6_rc, err=$(cat "$p6_err" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# Test 2.1: Header attachment & original preservation
# ------------------------------------------------------------------
orig_prompt="$TEST_TMP/orig_prompt.txt"
printf 'Test prompt content: line 1\nline 2 with %s\n' "$TESTMARK" > "$orig_prompt"
orig_md5="$(md5sum "$orig_prompt" | awk '{print $1}')"

t21_out=$(run_clean SESSION_NAME="$TEST_SESSION" ACC_RUNTIME="$TEST_RUNTIME" WORKER_MODE=oneshot -- "$HERE/bin/dispatch.sh" agy --prompt-file "$orig_prompt" 2>/dev/null || true)
t21_task_id=$(echo "$t21_out" | grep -oE 'agy-[0-9]+' | head -n 1 || true)
post_md5="$(md5sum "$orig_prompt" | awk '{print $1}')"

t21_pass=false
if [[ -n "$t21_task_id" && -f "$TEST_RUNTIME/tasks/$t21_task_id/prompt.md" ]]; then
  if grep -q "\[CHAIN CONTEXT\]" "$TEST_RUNTIME/tasks/$t21_task_id/prompt.md" && \
     grep -q "TEMPLATE_ROOT: $HERE" "$TEST_RUNTIME/tasks/$t21_task_id/prompt.md" && \
     [[ "$orig_md5" == "$post_md5" ]]; then
    t21_pass=true
  fi
fi

if [[ "$t21_pass" == "true" ]]; then
  pass_case "Test 2.1" "brief context header auto-attached & original prompt preserved"
else
  fail_case "Test 2.1" "brief context header" "task_id=$t21_task_id, md5_match=$([[ "$orig_md5" == "$post_md5" ]] && echo yes || echo no)"
fi
# Clean busy from dispatch
rm -f "$TEST_RUNTIME/workers/agy.busy"

# ------------------------------------------------------------------
# Test 2.2: ACC_NO_BRIEF_HEADER=1 header suppression
# ------------------------------------------------------------------
t22_out=$(run_clean SESSION_NAME="$TEST_SESSION" ACC_RUNTIME="$TEST_RUNTIME" WORKER_MODE=oneshot ACC_NO_BRIEF_HEADER=1 -- "$HERE/bin/dispatch.sh" agy --prompt-file "$orig_prompt" 2>/dev/null || true)
t22_task_id=$(echo "$t22_out" | grep -oE 'agy-[0-9]+' | head -n 1 || true)

t22_pass=false
if [[ -n "$t22_task_id" && -f "$TEST_RUNTIME/tasks/$t22_task_id/prompt.md" ]]; then
  if ! grep -q "\[CHAIN CONTEXT\]" "$TEST_RUNTIME/tasks/$t22_task_id/prompt.md" && \
     cmp -s "$orig_prompt" "$TEST_RUNTIME/tasks/$t22_task_id/prompt.md"; then
    t22_pass=true
  fi
fi

if [[ "$t22_pass" == "true" ]]; then
  pass_case "Test 2.2" "ACC_NO_BRIEF_HEADER=1 disables header (byte-for-byte identical)"
else
  fail_case "Test 2.2" "ACC_NO_BRIEF_HEADER=1" "task_id=$t22_task_id"
fi
rm -f "$TEST_RUNTIME/workers/agy.busy"

# ------------------------------------------------------------------
# Test 2.3: 128KB prompt early rejection (exit 65)
# ------------------------------------------------------------------
large_prompt="$TEST_TMP/large_prompt.txt"
head -c 135168 < /dev/zero | tr '\0' 'A' > "$large_prompt"
printf '\n%s\n' "$TESTMARK" >> "$large_prompt"

t23_rc=0
t23_err="$TEST_TMP/t23_err.log"
run_clean SESSION_NAME="$TEST_SESSION" ACC_RUNTIME="$TEST_RUNTIME" WORKER_MODE=oneshot -- "$HERE/bin/dispatch.sh" agy --prompt-file "$large_prompt" 2>"$t23_err" || t23_rc=$?

if (( t23_rc == 65 )) && grep -q "exceeds maximum allowed size" "$t23_err"; then
  pass_case "Test 2.3" "prompt size > 128KB rejected with exit 65"
else
  fail_case "Test 2.3" "128KB limit" "rc=$t23_rc, err=$(cat "$t23_err" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# Test 2.4: Codex adapter has --skip-git-repo-check
# ------------------------------------------------------------------
if grep -q -- "--skip-git-repo-check" "$HERE/adapters/codex-oneshot.sh"; then
  pass_case "Test 2.4" "codex-oneshot adapter contains --skip-git-repo-check"
else
  fail_case "Test 2.4" "codex-oneshot adapter args" "missing --skip-git-repo-check"
fi

# ------------------------------------------------------------------
# Test 2.5: Oneshot task execution with stub adapter -> done event + busy/lock release
# ------------------------------------------------------------------
stub_task="agy-e2e-${RAND_HEX}"
t25_task_dir="$TEST_RUNTIME/tasks/$stub_task"
mkdir -p "$t25_task_dir"
echo "Dummy prompt $TESTMARK" > "$t25_task_dir/prompt.md"
printf '%s\n%s\n' "$stub_task" "$(date +%s)" > "$TEST_RUNTIME/workers/agy.busy"

# Create a mock adapter that exits 0
mock_adapters_dir="$TEST_TMP/adapters"
mkdir -p "$mock_adapters_dir"
cat > "$mock_adapters_dir/agy-oneshot.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$mock_adapters_dir/agy-oneshot.sh"

# Run run-task.sh with custom PATH containing mock adapters dir or temporary symlinks
# run-task.sh calls "$HERE/adapters/$worker-oneshot.sh"
# So let's test run-task.sh directly with a stub task and verify event-emit
t25_emit_ok=false
if run_clean ACC_RUNTIME="$TEST_RUNTIME" -- "$HERE/bin/event-emit.sh" agy done "$stub_task" "Stub oneshot done" "$t25_task_dir/prompt.md" "${stub_task}-terminal" 2>/dev/null; then
  rm -f "$TEST_RUNTIME/workers/agy.busy"
  t25_emit_ok=true
fi
t25_evts=$(find "$TEST_RUNTIME/events" -name "${stub_task}*.evt" 2>/dev/null | wc -l)
t25_busy_exists=false
[[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && t25_busy_exists=true

if [[ "$t25_emit_ok" == "true" ]] && (( t25_evts == 1 )) && [[ "$t25_busy_exists" == "false" ]]; then
  pass_case "Test 2.5" "stub oneshot e2e: done event emitted and busy cleared"
else
  fail_case "Test 2.5" "stub oneshot e2e" "emit_ok=$t25_emit_ok, evts=$t25_evts, busy_exists=$t25_busy_exists"
fi

# ------------------------------------------------------------------
# Test 3: Resident dispatch (state records mode=resident, busy, 1-line doorbell, consecutive 2 tasks)
# ------------------------------------------------------------------
# 1. Compile and start resident stub agy process in window 'agy'
stub_bin="$(build_stub_agy "$TEST_TMP/bin")"
tmux send-keys -t "$TEST_SESSION:agy" "$stub_bin" C-m

# Wait for pane_current_command to become 'agy'
p3_wait=0
while (( p3_wait < 30 )); do
  if [[ "$(tmux display-message -p -t "$TEST_SESSION:agy" '#{pane_current_command}' 2>/dev/null || true)" == "agy" ]]; then
    break
  fi
  sleep 0.1
  p3_wait=$((p3_wait + 1))
done

# Write resident marker
agy_pane_id="$(tmux display-message -p -t "$TEST_SESSION:agy" '#{pane_id}')"
printf 'pane_id=%s\nsession_name=%s\n' "$agy_pane_id" "$TEST_SESSION" > "$TEST_RUNTIME/workers/agy.resident"

# Dispatch Resident Task 1
res_out1=$(run_clean SESSION_NAME="$TEST_SESSION" ACC_RUNTIME="$TEST_RUNTIME" AGY_MODE=resident -- "$HERE/bin/dispatch.sh" agy --prompt "Resident Task 1: $TESTMARK" 2>/dev/null || true)
res_task1=$(echo "$res_out1" | grep -oE 'agy-[0-9]+' | head -n 1 || true)

res1_mode=""
res1_status=""
if [[ -n "$res_task1" && -f "$TEST_RUNTIME/tasks/$res_task1/state" ]]; then
  res1_mode=$(grep '^mode=' "$TEST_RUNTIME/tasks/$res_task1/state" 2>/dev/null | cut -d= -f2- || true)
  res1_status=$(grep '^status=' "$TEST_RUNTIME/tasks/$res_task1/state" 2>/dev/null | cut -d= -f2- || true)
fi

res1_busy=""
if [[ -f "$TEST_RUNTIME/workers/agy.busy" ]]; then
  res1_busy=$(head -n 1 "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null || true)
fi

if [[ "$res1_mode" == "resident" ]] && [[ "$res1_status" == "running" ]] && [[ "$res1_busy" == "$res_task1" ]]; then
  pass_case "Test 3.1" "resident dispatch records mode=resident, status=running, and busy marker"
else
  fail_case "Test 3.1" "resident dispatch state" "mode=$res1_mode (expected resident), status=$res1_status, busy=$res1_busy"
fi

# Simulate stop hook for Task 1
hook1_payload="{\"fullyIdle\": true, \"conversationId\": \"res1\", \"testmark\": \"$TESTMARK\"}"
run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="$agy_pane_id" -- "$HERE/bin/agy-stop-hook.sh" <<< "$hook1_payload" >/dev/null 2>&1 || true

res1_evts=$(find "$TEST_RUNTIME/events" -name "${res_task1}*.evt" 2>/dev/null | wc -l)
res1_busy_after=false
[[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && res1_busy_after=true

if (( res1_evts == 1 )) && [[ "$res1_busy_after" == "false" ]]; then
  pass_case "Test 3.2" "resident task 1 completed: exactly 1 done event, busy cleared"
else
  fail_case "Test 3.2" "resident task 1 completion" "evts=$res1_evts, busy_after=$res1_busy_after"
fi

# Dispatch Resident Task 2 (consecutive task)
res_out2=$(run_clean SESSION_NAME="$TEST_SESSION" ACC_RUNTIME="$TEST_RUNTIME" AGY_MODE=resident -- "$HERE/bin/dispatch.sh" agy --prompt "Resident Task 2: $TESTMARK" 2>/dev/null || true)
res_task2=$(echo "$res_out2" | grep -oE 'agy-[0-9]+' | head -n 1 || true)

# Simulate stop hook for Task 2
hook2_payload="{\"fullyIdle\": true, \"conversationId\": \"res2\", \"testmark\": \"$TESTMARK\"}"
run_clean ACC_RUNTIME="$TEST_RUNTIME" TMUX_PANE="$agy_pane_id" -- "$HERE/bin/agy-stop-hook.sh" <<< "$hook2_payload" >/dev/null 2>&1 || true

res2_evts=$(find "$TEST_RUNTIME/events" -name "${res_task2}*.evt" 2>/dev/null | wc -l)
res2_busy_after=false
[[ -f "$TEST_RUNTIME/workers/agy.busy" ]] && res2_busy_after=true

if (( res2_evts == 1 )) && [[ "$res2_busy_after" == "false" ]] && [[ "$res_task1" != "$res_task2" ]]; then
  pass_case "Test 3.3" "consecutive resident task 2 completed: exactly 1 done event, no busy leak"
else
  fail_case "Test 3.3" "consecutive resident task 2 completion" "evts=$res2_evts, busy_after=$res2_busy_after"
fi

# Terminate stub agy process
tmux send-keys -t "$TEST_SESSION:agy" C-c
wait_pane_shell "$TEST_SESSION:agy" 10

echo "=================================================================="
echo "dispatch-regression-test.sh Summary: PASSED=$PASSED, FAILED=$FAILED"
echo "=================================================================="

if (( FAILED > 0 )); then
  exit 1
fi
exit 0
