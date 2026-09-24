#!/usr/bin/env bash
# tests/bootstrap-resident-test.sh: bootstrap-v2.sh resident 모드 및 회귀 방지 격리 검증 (F5)
# 검증 항목:
#   (a) workers/agy.resident 존재 및 pane_id 일치
#   (b) session_name 마커 일치
#   (c) 스텁 agy /proc/<pid>/environ 에 ACC_RUNTIME 이 임시 런타임과 일치 (라이브 런타임 값 아님)
#   (d) 1줄 재무장 안내문 <= 500바이트 주입 확인
#   (e) AGY_MODE 미지정(기본 oneshot) 시 마커 및 상주 프로세스 기동 없음 (무회귀)
#   (f) bootstrap 실행 중 라이브 agentchain* 세션 무간섭 검증

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "bootstrap-resident"

PASSED=0
FAILED=0

pass_case() {
  local code="$1"
  local desc="$2"
  echo "  [PASS] ($code): $desc"
  PASSED=$((PASSED + 1))
}

fail_case() {
  local code="$1"
  local desc="$2"
  local reason="${3:-unknown}"
  echo "  [FAIL] ($code): $desc (Reason: $reason)"
  FAILED=$((FAILED + 1))
}

echo "=================================================================="
echo "Running bootstrap-resident-test.sh (Marker: $TESTMARK)"
echo "Target session: $TEST_SESSION, runtime: $TEST_RUNTIME"
echo "=================================================================="

# 1. 스텁 바이너리 준비 (PATH 격리)
TEST_BIN="$TEST_TMP/bin"
mkdir -p "$TEST_BIN"
stub_agy="$(build_stub_agy "$TEST_BIN")"

cat > "$TEST_BIN/claude" <<'EOF'
#!/bin/sh
while true; do sleep 60; done
EOF
chmod +x "$TEST_BIN/claude"

cat > "$TEST_BIN/codex" <<'EOF'
#!/bin/sh
while true; do sleep 60; done
EOF
chmod +x "$TEST_BIN/codex"

# 2. 사전 라이브 세션 윈도우 상태 기록
LIVE_PRE_WINDOWS="$(tmux list-windows -t agentchain-v2 -F '#{window_name} #{pane_current_command}' 2>/dev/null || true)"

# 3. 임시 프로젝트 및 로그 디렉토리 준비
PROJ_DIR="$TEST_TMP/project"
mkdir -p "$PROJ_DIR"
LOG_DIR="$TEST_TMP/logs"
mkdir -p "$LOG_DIR"

# 4. bootstrap-v2.sh 실행 (AGY_MODE=resident)
boot_log="$TEST_TMP/boot_resident.log"
run_clean \
  PATH="$TEST_BIN:$PATH" \
  PROJECT_DIR="$PROJ_DIR" \
  LOG_DIR="$LOG_DIR" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  AGY_MODE=resident \
  SONNET_CMD="$TEST_BIN/claude" \
  AGY_CMD="$stub_agy" \
  CODEX_CMD="$TEST_BIN/codex" \
  START_WATCHDOG=false \
  AUTO_CONFIRM_TRUST=false \
  -- "$HERE/bootstrap-v2.sh" >"$boot_log" 2>&1 || true

# agy 창 준비 대기 (stub_agy 구동 확인)
wait_agy=0
while (( wait_agy < 30 )); do
  cur_cmd="$(tmux display-message -p -t "$TEST_SESSION:agy" '#{pane_current_command}' 2>/dev/null || true)"
  if [[ "$cur_cmd" == "agy" ]]; then
    break
  fi
  sleep 0.1
  wait_agy=$((wait_agy + 1))
done

# (a) workers/agy.resident 존재 및 pane_id 대조
resident_marker="$TEST_RUNTIME/workers/agy.resident"
actual_pane_id="$(tmux display-message -p -t "$TEST_SESSION:agy" '#{pane_id}')"
res_pane_id="$(sed -n 's/^pane_id=//p' "$resident_marker" 2>/dev/null | head -n 1 | tr -d '\r\n')"

if [[ -f "$resident_marker" ]] && [[ "$res_pane_id" == "$actual_pane_id" ]]; then
  pass_case "a" "workers/agy.resident exists and pane_id matches ($res_pane_id == $actual_pane_id)"
else
  fail_case "a" "workers/agy.resident pane_id check" "marker_exists=$([[ -f "$resident_marker" ]] && echo yes || echo no), res_pane_id=$res_pane_id, actual=$actual_pane_id"
fi

# (b) session_name 마커 대조
file_session="$(cat "$TEST_RUNTIME/session_name" 2>/dev/null | tr -d '\r\n')"
marker_session="$(sed -n -E 's/^(session|session_name)=//p' "$resident_marker" 2>/dev/null | head -n 1 | tr -d '\r\n')"

if [[ "$file_session" == "$TEST_SESSION" ]] && [[ "$marker_session" == "$TEST_SESSION" ]]; then
  pass_case "b" "session_name marker matches temp session ($TEST_SESSION)"
else
  fail_case "b" "session_name marker check" "file_session=$file_session, marker_session=$marker_session, expected=$TEST_SESSION"
fi

# (c) stub agy /proc/<pid>/environ ACC_RUNTIME 대조 (임시 런타임 일치, 라이브 런타임 아님)
pane_pid="$(tmux display-message -p -t "$TEST_SESSION:agy" '#{pane_pid}')"
child_pid="$(pgrep -P "$pane_pid" agy 2>/dev/null | head -n 1 || echo "$pane_pid")"
target_pid="${child_pid:-$pane_pid}"
proc_rt="$(tr '\0' '\n' < "/proc/$target_pid/environ" 2>/dev/null | grep '^ACC_RUNTIME=' | cut -d= -f2- || true)"

real_test_rt="$(realpath "$TEST_RUNTIME" 2>/dev/null || echo "$TEST_RUNTIME")"
real_proc_rt="$(realpath "$proc_rt" 2>/dev/null || echo "$proc_rt")"
real_live_rt="$(realpath "$LIVE_RT" 2>/dev/null || echo "$LIVE_RT")"

if [[ -n "$real_proc_rt" ]] && [[ "$real_proc_rt" == "$real_test_rt" ]] && [[ "$real_proc_rt" != "$real_live_rt" ]]; then
  pass_case "c" "stub process /proc/$target_pid/environ contains temp ACC_RUNTIME ($proc_rt) and not live"
else
  fail_case "c" "stub environ ACC_RUNTIME check" "proc_rt=$proc_rt, test_rt=$TEST_RUNTIME, live_rt=$LIVE_RT"
fi

# (d) 1줄 재무장 안내문 <= 500바이트 주입 검증
pane_content="$(tmux capture-pane -p -t "$TEST_SESSION:agy" 2>/dev/null || true)"
rearm_expected="ACC_ROLE: You are the resident router."
rearm_raw="ACC_ROLE: You are the resident router. Do not do file writing or coding directly. Always delegate via invoke_subagent and wait."
rearm_len="$(printf '%s' "$rearm_raw" | wc -c)"

if [[ "$pane_content" == *"$rearm_expected"* ]] && (( rearm_len <= 500 )); then
  pass_case "d" "1-line re-arm instruction (bytes: $rearm_len <= 500) injected into resident pane"
else
  fail_case "d" "re-arm instruction injection" "len=$rearm_len, found=$([[ "$pane_content" == *"$rearm_expected"* ]] && echo yes || echo no)"
fi

# 상주 임시 세션 종료
tmux kill-session -t "$TEST_SESSION" 2>/dev/null || true

# (e) AGY_MODE 미지정(기본 oneshot) 시 무회귀 검증: 상주 마커 및 프로세스 없음
oneshot_session="${TEST_SESSION}-oneshot"
oneshot_rt="$TEST_TMP/runtime-oneshot"
oneshot_log="$TEST_TMP/boot_oneshot.log"

run_clean \
  PATH="$TEST_BIN:$PATH" \
  PROJECT_DIR="$PROJ_DIR" \
  LOG_DIR="$LOG_DIR" \
  ACC_RUNTIME="$oneshot_rt" \
  SESSION_NAME="$oneshot_session" \
  WORKER_MODE=oneshot \
  SONNET_CMD="$TEST_BIN/claude" \
  AGY_CMD="$stub_agy" \
  CODEX_CMD="$TEST_BIN/codex" \
  START_WATCHDOG=false \
  AUTO_CONFIRM_TRUST=false \
  -- "$HERE/bootstrap-v2.sh" >"$oneshot_log" 2>&1 || true

wait_pane_shell "$oneshot_session:agy" 10
oneshot_cmd="$(tmux display-message -p -t "$oneshot_session:agy" '#{pane_current_command}' 2>/dev/null || true)"
oneshot_marker_exists=false
[[ -f "$oneshot_rt/workers/agy.resident" ]] && oneshot_marker_exists=true

tmux kill-session -t "$oneshot_session" 2>/dev/null || true

if [[ "$oneshot_marker_exists" == "false" ]] && [[ "$oneshot_cmd" != "agy" ]]; then
  pass_case "e" "oneshot mode produces NO resident marker and NO resident launch"
else
  fail_case "e" "oneshot regression check" "marker_exists=$oneshot_marker_exists, cmd=$oneshot_cmd"
fi

# (f) 라이브 agentchain* 세션 무간섭 검증
LIVE_POST_WINDOWS="$(tmux list-windows -t agentchain-v2 -F '#{window_name} #{pane_current_command}' 2>/dev/null || true)"
if [[ "$LIVE_PRE_WINDOWS" == "$LIVE_POST_WINDOWS" ]]; then
  pass_case "f" "live agentchain* session was completely untouched during bootstrap"
else
  fail_case "f" "live session touched" "pre=$LIVE_PRE_WINDOWS, post=$LIVE_POST_WINDOWS"
fi

echo "=================================================================="
echo "bootstrap-resident-test.sh Summary: PASSED=$PASSED, FAILED=$FAILED"
echo "=================================================================="

if (( FAILED > 0 )); then
  exit 1
fi
exit 0
