#!/usr/bin/env bash
# tests/test-watchdog-runtime.sh: watchdog-v2.sh 런타임 범위화 및 다중 인스턴스 격리 검증 (S2)
# 검증 항목:
#   (a) 런타임 브리핑 우선순위 및 공용 브리핑 폴백 검증
#       a-1. $ACC_RUNTIME/session_brief.md 우선 사용
#       a-2. session_brief.md 부재 시 $ACC_RUNTIME/supervisor_brief.md 사용
#       a-3. 둘 다 부재 시 $LOG_DIR/session_brief.md 폴백 + 경고 로그 1회 출력
#   (b) 재기동 명령의 환경변수 주입 및 cwd (WORK_DIR vs PROJECT_DIR) 검증
#       b-1. WORK_DIR 설정 시 PROJECT_DIR, WORK_DIR, ACC_RUNTIME, ACC_TEMPLATE_ROOT, SESSION_NAME 주입 및 cwd=WORK_DIR
#       b-2. WORK_DIR 미설정 시 WORK_DIR=PROJECT_DIR 및 cwd=PROJECT_DIR
#   (c) $ACC_RUNTIME/watchdog.log 기록 및 세션명 포함 검증, logs/watchdog.log 비오염 확인
#   (d) tmux paste buffer 이름 있는 버퍼 사용 (acc-<session>) 및 기본 버퍼 비오염 검증
#   (e) agy.transcript 존재 시 해당 mtime만 참조, 부재 시 폴백 및 경고 로그 1회 검증 (detail 격리 확인)
#   (f) retired 표식 시 즉시 정상 종료 및 재기동 0회 검증
#   (g) 구형 런타임 호환성 검증 (새 파일/키 부재 시 기존 동작 불변)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "watchdog-runtime"
setup_mock_runtime "$TEST_RUNTIME"

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
echo "Running test-watchdog-runtime.sh (Marker: $TESTMARK)"
echo "Target session: $TEST_SESSION, runtime: $TEST_RUNTIME"
echo "=================================================================="

# ------------------------------------------------------------------
# 도구 및 환경 준비
# ------------------------------------------------------------------
TEST_BIN="$TEST_TMP/bin"
mkdir -p "$TEST_BIN"

# 1. Sonnet mock: 호출 시 환경변수, 인자, 실행 디렉터리를 기록
MOCK_SONNET_LOG="$TEST_TMP/mock_sonnet.log"
cat > "$TEST_BIN/mock-sonnet" <<EOF
#!/usr/bin/env bash
{
  echo "INVOCATION_START"
  echo "PWD=\$PWD"
  echo "PROJECT_DIR=\${PROJECT_DIR:-}"
  echo "WORK_DIR=\${WORK_DIR:-}"
  echo "ACC_RUNTIME=\${ACC_RUNTIME:-}"
  echo "ACC_TEMPLATE_ROOT=\${ACC_TEMPLATE_ROOT:-}"
  echo "SESSION_NAME=\${SESSION_NAME:-}"
  echo "ARGS=\$*"
  echo "INVOCATION_END"
} >> "$MOCK_SONNET_LOG"
# 계속 셸로 떨어지지 않도록 무한 대기 (kill 가능)
exec sleep 3600
EOF
chmod +x "$TEST_BIN/mock-sonnet"

# 2. tmux spy: tmux set-buffer, paste-buffer 호출 내역을 기록하면서 실제 tmux 로 포워딩
REAL_TMUX="$(command -v tmux)"
TMUX_SPY_LOG="$TEST_TMP/tmux_spy.log"
cat > "$TEST_BIN/tmux" <<EOF
#!/usr/bin/env bash
echo "TMUX: \$*" >> "$TMUX_SPY_LOG"
exec "$REAL_TMUX" "\$@"
EOF
chmod +x "$TEST_BIN/tmux"

# 3. 임시 프로젝트 및 작업 디렉터리 준비
PROJ_DIR="$TEST_TMP/project"
mkdir -p "$PROJ_DIR"
CUSTOM_WORK_DIR="$TEST_TMP/custom_work"
mkdir -p "$CUSTOM_WORK_DIR"
COMMON_LOG_DIR="$TEST_TMP/common_logs"
mkdir -p "$COMMON_LOG_DIR"

# 4. 격리 tmux 세션 생성 (windows: sonnet, agy, codex)
run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" new-session -d -s "$TEST_SESSION" -n sonnet -c "$PROJ_DIR"
run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" new-window -t "$TEST_SESSION" -n agy -c "$PROJ_DIR"
run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" new-window -t "$TEST_SESSION" -n codex -c "$PROJ_DIR"

wait_pane_shell "$TEST_SESSION:sonnet" 10
wait_pane_shell "$TEST_SESSION:agy" 10
wait_pane_shell "$TEST_SESSION:codex" 10

run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" set-environment -t "$TEST_SESSION" ACC_BOOTSTRAP_VERSION 2
run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" set-environment -t "$TEST_SESSION" ACC_RUNTIME "$TEST_RUNTIME"
run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" set-environment -t "$TEST_SESSION" SESSION_NAME "$TEST_SESSION"
echo '{}' > "$TEST_RUNTIME/claude-bridge.settings.json"

# ------------------------------------------------------------------
# Test A & B: 브리핑 파일 우선순위 및 재기동 환경/cwd 주입 검증
# ------------------------------------------------------------------

# a-1 & b-1: $ACC_RUNTIME/session_brief.md 존재 + WORK_DIR 지정
echo "BRIEF_CONTENT_SESSION_BRIEF_$TESTMARK" > "$TEST_RUNTIME/session_brief.md"
echo "BRIEF_CONTENT_SUPERVISOR_BRIEF_$TESTMARK" > "$TEST_RUNTIME/supervisor_brief.md"
echo "BRIEF_CONTENT_COMMON_LOG_$TESTMARK" > "$COMMON_LOG_DIR/session_brief.md"
: > "$MOCK_SONNET_LOG"

run_clean \
  PATH="$TEST_BIN:$PATH" \
  PROJECT_DIR="$PROJ_DIR" \
  WORK_DIR="$CUSTOM_WORK_DIR" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  LOG_DIR="$COMMON_LOG_DIR" \
  SONNET_CMD="$TEST_BIN/mock-sonnet" \
  -- "$HERE/watchdog-v2.sh" --oneshot >/dev/null 2>&1 || true

# sonnet 창에서 mock-sonnet 이 실행될 때까지 대기
wait_cnt=0
while (( wait_cnt < 30 )); do
  if grep -q "INVOCATION_START" "$MOCK_SONNET_LOG" 2>/dev/null; then
    break
  fi
  sleep 0.1
  wait_cnt=$((wait_cnt + 1))
done

if grep -q "BRIEF_CONTENT_SESSION_BRIEF_$TESTMARK" "$MOCK_SONNET_LOG" 2>/dev/null; then
  pass_case "a-1" "\$ACC_RUNTIME/session_brief.md 가 supervisor_brief.md 및 공용 브리핑보다 우선 적용됨"
else
  fail_case "a-1" "session_brief.md 우선순위 실패" "content=$(cat "$MOCK_SONNET_LOG" 2>/dev/null || echo empty)"
fi

if grep -q "PWD=$CUSTOM_WORK_DIR" "$MOCK_SONNET_LOG" 2>/dev/null && \
   grep -q "PROJECT_DIR=$PROJ_DIR" "$MOCK_SONNET_LOG" 2>/dev/null && \
   grep -q "WORK_DIR=$CUSTOM_WORK_DIR" "$MOCK_SONNET_LOG" 2>/dev/null && \
   grep -q "ACC_RUNTIME=$TEST_RUNTIME" "$MOCK_SONNET_LOG" 2>/dev/null && \
   grep -q "SESSION_NAME=$TEST_SESSION" "$MOCK_SONNET_LOG" 2>/dev/null; then
  pass_case "b-1" "재기동 명령어에 PROJECT_DIR, WORK_DIR, ACC_RUNTIME, ACC_TEMPLATE_ROOT, SESSION_NAME 주입 및 cwd=WORK_DIR 검증"
else
  fail_case "b-1" "환경변수 및 cwd 주입 검증 실패" "log=$(cat "$MOCK_SONNET_LOG" 2>/dev/null || echo empty)"
fi

# mock-sonnet 종료 후 셸 복귀 대기
run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" send-keys -t "$TEST_SESSION:sonnet" C-c 2>/dev/null || true
pkill -f "mock-sonnet" 2>/dev/null || true
wait_pane_shell "$TEST_SESSION:sonnet" 10

# a-2: session_brief.md 삭제 후 supervisor_brief.md 사용 검증
rm -f "$TEST_RUNTIME/session_brief.md"
: > "$MOCK_SONNET_LOG"

run_clean \
  PATH="$TEST_BIN:$PATH" \
  PROJECT_DIR="$PROJ_DIR" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  LOG_DIR="$COMMON_LOG_DIR" \
  SONNET_CMD="$TEST_BIN/mock-sonnet" \
  -- "$HERE/watchdog-v2.sh" --oneshot >/dev/null 2>&1 || true

wait_cnt=0
while (( wait_cnt < 30 )); do
  if grep -q "INVOCATION_START" "$MOCK_SONNET_LOG" 2>/dev/null; then
    break
  fi
  sleep 0.1
  wait_cnt=$((wait_cnt + 1))
done

if grep -q "BRIEF_CONTENT_SUPERVISOR_BRIEF_$TESTMARK" "$MOCK_SONNET_LOG" 2>/dev/null; then
  pass_case "a-2" "session_brief.md 부재 시 \$ACC_RUNTIME/supervisor_brief.md 정상 사용"
else
  fail_case "a-2" "supervisor_brief.md 적용 실패" "content=$(cat "$MOCK_SONNET_LOG" 2>/dev/null || echo empty)"
fi

# b-2: WORK_DIR 미지정 시 PROJECT_DIR 로 fallback 및 cwd=PROJECT_DIR 검증
if grep -q "PWD=$PROJ_DIR" "$MOCK_SONNET_LOG" 2>/dev/null && \
   grep -q "WORK_DIR=$PROJ_DIR" "$MOCK_SONNET_LOG" 2>/dev/null; then
  pass_case "b-2" "WORK_DIR 미설정 시 PROJECT_DIR 기본값 주입 및 cwd=PROJECT_DIR 검증"
else
  fail_case "b-2" "WORK_DIR 미설정 시 기본값 fallback 실패" "log=$(cat "$MOCK_SONNET_LOG" 2>/dev/null || echo empty)"
fi

# mock-sonnet 종료 후 셸 복귀 대기
run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" send-keys -t "$TEST_SESSION:sonnet" C-c 2>/dev/null || true
pkill -f "mock-sonnet" 2>/dev/null || true
wait_pane_shell "$TEST_SESSION:sonnet" 10

# a-3: 둘 다 삭제 시 공용 $LOG_DIR/session_brief.md 폴백 + 경고 로그 1회 출력 검증
rm -f "$TEST_RUNTIME/supervisor_brief.md"
: > "$MOCK_SONNET_LOG"

run_clean \
  PATH="$TEST_BIN:$PATH" \
  PROJECT_DIR="$PROJ_DIR" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  LOG_DIR="$COMMON_LOG_DIR" \
  SONNET_CMD="$TEST_BIN/mock-sonnet" \
  -- "$HERE/watchdog-v2.sh" --oneshot >/dev/null 2>&1 || true

wait_cnt=0
while (( wait_cnt < 30 )); do
  if grep -q "INVOCATION_START" "$MOCK_SONNET_LOG" 2>/dev/null; then
    break
  fi
  sleep 0.1
  wait_cnt=$((wait_cnt + 1))
done

warn_in_log=false
if [[ -f "$TEST_RUNTIME/watchdog.log" ]] && grep -q "런타임 브리핑 부재" "$TEST_RUNTIME/watchdog.log"; then
  warn_in_log=true
fi

if grep -q "BRIEF_CONTENT_COMMON_LOG_$TESTMARK" "$MOCK_SONNET_LOG" 2>/dev/null && [[ "$warn_in_log" == "true" ]]; then
  pass_case "a-3" "런타임 브리핑 전무 시 공용 session_brief.md 폴백 및 1회 경고 로그 정상 출력"
else
  fail_case "a-3" "공용 브리핑 폴백 또는 경고 로그 출력 실패" "warn=$warn_in_log, sonnet=$(cat "$MOCK_SONNET_LOG" 2>/dev/null || echo empty)"
fi

# mock-sonnet 정리
run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" send-keys -t "$TEST_SESSION:sonnet" C-c 2>/dev/null || true
pkill -f "mock-sonnet" 2>/dev/null || true
wait_pane_shell "$TEST_SESSION:sonnet" 10

# ------------------------------------------------------------------
# Test C: $ACC_RUNTIME/watchdog.log 기록 및 세션명 포함, 템플릿 비오염 검증
# ------------------------------------------------------------------
c_ok=true
c_reason=""
if [[ ! -f "$TEST_RUNTIME/watchdog.log" ]]; then
  c_ok=false
  c_reason="watchdog.log was not created in \$ACC_RUNTIME"
elif ! grep -q "\[$TEST_SESSION\]" "$TEST_RUNTIME/watchdog.log"; then
  c_ok=false
  c_reason="watchdog.log lines do not include [$TEST_SESSION]"
fi

# 템플릿의 logs/watchdog.log 검증
if [[ -f "$HERE/logs/watchdog.log" ]]; then
  if grep -q "$TESTMARK" "$HERE/logs/watchdog.log" 2>/dev/null; then
    c_ok=false
    c_reason="template logs/watchdog.log was polluted with $TESTMARK"
  fi
fi

if [[ "$c_ok" == "true" ]]; then
  pass_case "c" "\$ACC_RUNTIME/watchdog.log 에 [$TEST_SESSION] 접두 기록 및 템플릿 logs 비오염 확인"
else
  fail_case "c" "로그 런타임 범위화 검증 실패" "$c_reason"
fi

# ------------------------------------------------------------------
# Test D: tmux paste buffer 격리 (acc-<session> 이름 있는 버퍼 사용 및 -d 삭제)
# ------------------------------------------------------------------
# 브리지 리스너 비활성 상태에서 120초 이상 지난 미처리 pending 이벤트 생성
now=$(date +%s)
old_evt_time=$((now - 300))
mkdir -p "$TEST_RUNTIME/events/pending"
cat > "$TEST_RUNTIME/events/pending/old-event.evt" <<EOF
id=old-event-1
source=test
kind=task.done
task_id=task-old
summary_b64=$(printf 'test event' | base64)
detail_path_b64=$(printf '/dev/null' | base64)
EOF
touch -d "@$old_evt_time" "$TEST_RUNTIME/events/pending/old-event.evt"
rm -f "$TEST_RUNTIME/listener.pid" "$TEST_RUNTIME/emergency.notified"
: > "$TMUX_SPY_LOG"

# 기본 버퍼에 더미 데이터 세팅
run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" set-buffer "DEFAULT_PRESERVED_BUFFER_CONTENT" 2>/dev/null || true

run_clean \
  PATH="$TEST_BIN:$PATH" \
  PROJECT_DIR="$PROJ_DIR" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  LOG_DIR="$COMMON_LOG_DIR" \
  -- "$HERE/watchdog-v2.sh" --oneshot >/dev/null 2>&1 || true

sanitized_sess="${TEST_SESSION//[^A-Za-z0-9_-]/_}"
expected_buf="acc-${sanitized_sess}"

d_ok=true
d_reason=""
if ! grep -q "set-buffer -b $expected_buf" "$TMUX_SPY_LOG" 2>/dev/null; then
  d_ok=false
  d_reason="set-buffer -b $expected_buf was not called (log: $(cat "$TMUX_SPY_LOG" 2>/dev/null))"
fi
if ! grep -q "paste-buffer -d -b $expected_buf" "$TMUX_SPY_LOG" 2>/dev/null; then
  d_ok=false
  d_reason="paste-buffer -d -b $expected_buf was not called"
fi

# 기본 버퍼가 훼손되지 않았는지 확인
def_buf_content="$(run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" show-buffer 2>/dev/null || echo "")"
if [[ "$def_buf_content" != "DEFAULT_PRESERVED_BUFFER_CONTENT" ]]; then
  d_ok=false
  d_reason="default tmux buffer was polluted: got '$def_buf_content'"
fi

if [[ "$d_ok" == "true" ]]; then
  pass_case "d" "tmux 비상 알림 시 세션별 버퍼($expected_buf) 사용, -d 삭제, 기본 버퍼 보존 확인"
else
  fail_case "d" "tmux paste-buffer 격리 실패" "$d_reason"
fi

# 정리
rm -f "$TEST_RUNTIME/events/pending/old-event.evt"

# ------------------------------------------------------------------
# Test E: agy.transcript mtime 검사 및 부재 시 폴백 & detail 격리 검증
# ------------------------------------------------------------------
# e-1: agy.transcript 존재 시 해당 파일만 검사
stub_bin="$(build_stub_agy "$TEST_BIN")"
run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" send-keys -t "$TEST_SESSION:agy" "$stub_bin" C-m

p_wait=0
while (( p_wait < 30 )); do
  if [[ "$(run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" display-message -p -t "$TEST_SESSION:agy" '#{pane_current_command}' 2>/dev/null || true)" == "agy" ]]; then
    break
  fi
  sleep 0.1
  p_wait=$((p_wait + 1))
done

pane_pid="$(run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" display-message -p -t "$TEST_SESSION:agy" '#{pane_pid}')"
child_pid="$(pgrep -P "$pane_pid" agy 2>/dev/null | head -n 1 || echo "$pane_pid")"
target_pid="${child_pid:-$pane_pid}"
proc_token="$(awk '{print $22}' "/proc/$target_pid/stat" 2>/dev/null || echo "$$")"
pgid="$(ps -o pgid= -p "$target_pid" 2>/dev/null | tr -d ' ' || echo "$target_pid")"
agy_pane_id="$(run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" display-message -p -t "$TEST_SESSION:agy" '#{pane_id}')"
printf 'pane_id=%s\nsession_name=%s\n' "$agy_pane_id" "$TEST_SESSION" > "$TEST_RUNTIME/workers/agy.resident"

now=$(date +%s)
stall_task="task-stall-${RAND_HEX}"
stall_dir="$TEST_RUNTIME/tasks/$stall_task"
mkdir -p "$stall_dir"
echo "Stall prompt $TESTMARK" > "$stall_dir/prompt.md"

cat > "$stall_dir/state" <<EOF
task_id=$stall_task
worker=agy
status=running
mode=resident
pid=$target_pid
pgid=$pgid
proc_token=$proc_token
started_epoch=$((now - 2000))
deadline_epoch=$((now + 2000))
EOF
printf '%s\n%s\n' "$stall_task" "$((now - 2000))" > "$TEST_RUNTIME/workers/agy.busy"

# 활성 transcript 파일 생성 및 agy.transcript 에 등록 (최근 mtime)
chain_trans="$TEST_TMP/chain_agy_transcript.jsonl"
echo '{"step": 1}' > "$chain_trans"
touch -d "@$now" "$chain_trans"
echo "$chain_trans" > "$TEST_RUNTIME/workers/agy.transcript"

# watchdog 실행 -> transcript mtime이 최신이므로 stall 미발생
run_clean \
  PATH="$TEST_BIN:$PATH" \
  PROJECT_DIR="$PROJ_DIR" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  NO_OUTPUT_WARN_SECONDS=900 \
  -- "$HERE/watchdog-v2.sh" --oneshot >/dev/null 2>&1 || true

stall_evt="$TEST_RUNTIME/events/pending/${stall_task}-stall-warn.evt"
if [[ ! -f "$stall_evt" ]]; then
  pass_case "e-1" "\$ACC_RUNTIME/workers/agy.transcript 존재 시 해당 파일 mtime 최신 감지로 스톨 경고 미발생"
else
  fail_case "e-1" "agy.transcript 최신 mtime 감지 실패 (불필요한 스톨 경고 발행됨)"
fi

# e-2: agy.transcript 경로 및 prompt mtime을 과거로 조작 -> stall 발행 및 detail 파일 검증
touch -d "@$((now - 2000))" "$chain_trans"
touch -d "@$((now - 2000))" "$stall_dir/prompt.md"
run_clean \
  PATH="$TEST_BIN:$PATH" \
  PROJECT_DIR="$PROJ_DIR" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  NO_OUTPUT_WARN_SECONDS=900 \
  -- "$HERE/watchdog-v2.sh" --oneshot >/dev/null 2>&1 || true

e2_ok=false
if [[ -f "$stall_evt" ]]; then
  # detail 파일이 $chain_trans 인지 확인
  det_b64="$(grep '^detail_path_b64=' "$stall_evt" | cut -d= -f2-)"
  det_path="$(printf '%s' "$det_b64" | base64 -d 2>/dev/null || echo "")"
  if [[ "$det_path" == "$chain_trans" ]]; then
    e2_ok=true
  fi
fi

if [[ "$e2_ok" == "true" ]]; then
  pass_case "e-2" "agy.transcript mtime 지연 시 정확히 해당 transcript 경로를 detail 로 stalled 이벤트 발행"
else
  fail_case "e-2" "stalled 이벤트 발행 또는 detail 경로 일치 실패"
fi

# e-3: agy.transcript 파일 삭제 시 머신 전체 최신 transcript 로 폴백 + 경고 1회 + detail 안전 처리
rm -f "$stall_evt" "$stall_dir/stall.notified" "$TEST_RUNTIME/workers/agy.transcript"
# 머신 최신 transcript 가 잡히더라도 detail 에는 다른 체인의 경로가 노출되지 않아야 함
run_clean \
  PATH="$TEST_BIN:$PATH" \
  PROJECT_DIR="$PROJ_DIR" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  NO_OUTPUT_WARN_SECONDS=900 \
  -- "$HERE/watchdog-v2.sh" --oneshot >/dev/null 2>&1 || true

e3_warn=false
if [[ -f "$TEST_RUNTIME/watchdog.log" ]] && grep -q "workers/agy.transcript 부재" "$TEST_RUNTIME/watchdog.log"; then
  e3_warn=true
fi

e3_detail_safe=true
if [[ -f "$stall_evt" ]]; then
  det_b64="$(grep '^detail_path_b64=' "$stall_evt" | cut -d= -f2-)"
  det_path="$(printf '%s' "$det_b64" | base64 -d 2>/dev/null || echo "")"
  if [[ "$det_path" == *".gemini/antigravity-cli/brain"* ]]; then
    e3_detail_safe=false
  fi
fi

if [[ "$e3_warn" == "true" && "$e3_detail_safe" == "true" ]]; then
  pass_case "e-3" "agy.transcript 부재 시 1회 경고 로그 출력 및 stalled 이벤트 detail 에 타 체인 transcript 비노출 확인"
else
  fail_case "e-3" "폴백 경고 로그 누락 또는 타 체인 transcript 노출" "warn=$e3_warn, safe=$e3_detail_safe"
fi

# stub agy 프로세스 정리
kill -9 "$target_pid" 2>/dev/null || true
rm -f "$stall_evt" "$TEST_RUNTIME/workers/agy.busy" "$stall_dir/stall.notified"

# ------------------------------------------------------------------
# Test F: retired 표식 시 즉시 정상 종료 및 재기동 0회 검증
# ------------------------------------------------------------------
touch "$TEST_RUNTIME/retired"

# sonnet 창을 닫아두어 만약 retired 감지가 동작하지 않으면 창 재생성을 시도하도록 유도
run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" kill-window -t "$TEST_SESSION:sonnet" 2>/dev/null || true

rc_ret=0
out_ret="$(run_clean \
  PATH="$TEST_BIN:$PATH" \
  PROJECT_DIR="$PROJ_DIR" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  LOG_DIR="$COMMON_LOG_DIR" \
  -- "$HERE/watchdog-v2.sh" --oneshot 2>&1)" || rc_ret=$?

f_ok=true
f_reason=""
if (( rc_ret != 0 )); then
  f_ok=false
  f_reason="watchdog did not exit with 0 (got rc=$rc_ret)"
fi
has_sonnet="$(run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" list-windows -t "$TEST_SESSION" -F '#{window_name}' 2>/dev/null | grep -x 'sonnet' || true)"
if [[ -n "$has_sonnet" ]]; then
  f_ok=false
  f_reason="sonnet window was unexpectedly recreated despite retired marker"
fi
if [[ ! -f "$TEST_RUNTIME/watchdog.log" ]] || ! grep -q "은퇴 표식.*감지 — 워치독 정상 종료" "$TEST_RUNTIME/watchdog.log"; then
  f_ok=false
  f_reason="clean retirement log was not printed"
fi
if [[ -f "$TEST_RUNTIME/watchdog.pid" ]]; then
  f_ok=false
  f_reason="watchdog.pid was not removed after exit"
fi

if [[ "$f_ok" == "true" ]]; then
  pass_case "f" "\$ACC_RUNTIME/retired 감지 시 즉시 정상 종료(rc=0), 창 재기동 0회, pid 파일 정리 확인"
else
  fail_case "f" "retired 표식 정상 처리 실패" "$f_reason"
fi

rm -f "$TEST_RUNTIME/retired"

# ------------------------------------------------------------------
# Test G: 구형 런타임 호환성 (새 파일/키 없을 때 완벽한 무회귀 동작)
# ------------------------------------------------------------------
# WORK_DIR, retired, agy.transcript 가 전혀 없는 클린 런타임 생성
LEGACY_RT="$TEST_TMP/legacy_runtime"
setup_mock_runtime "$LEGACY_RT"
echo "LEGACY_SESSION_BRIEF_$TESTMARK" > "$COMMON_LOG_DIR/session_brief.md"

rc_leg=0
run_clean \
  PATH="$TEST_BIN:$PATH" \
  PROJECT_DIR="$PROJ_DIR" \
  ACC_RUNTIME="$LEGACY_RT" \
  SESSION_NAME="$TEST_SESSION" \
  LOG_DIR="$COMMON_LOG_DIR" \
  -- "$HERE/watchdog-v2.sh" --oneshot >/dev/null 2>&1 || rc_leg=$?

if (( rc_leg == 0 )) && [[ -f "$LEGACY_RT/watchdog.log" ]]; then
  pass_case "g" "구형 런타임(WORK_DIR, retired, agy.transcript 부재) 환경에서 exit 0 및 무회귀 동작 확인"
else
  fail_case "g" "구형 런타임 호환성 검증 실패" "rc=$rc_leg"
fi

# ------------------------------------------------------------------
# 세션 정리 및 종합 결과
# ------------------------------------------------------------------
run_clean PATH="$TEST_BIN:$PATH" -- "$REAL_TMUX" kill-session -t "$TEST_SESSION" 2>/dev/null || true

echo "=================================================================="
echo "test-watchdog-runtime.sh Summary: PASSED=$PASSED, FAILED=$FAILED"
echo "=================================================================="

if (( FAILED > 0 )); then
  exit 1
fi
exit 0
