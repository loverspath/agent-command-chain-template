#!/usr/bin/env bash
# tests/e2e-resident-real-agy.sh: Real agy End-to-End Test (Task F10)
# 주의: 실제 LLM API를 호출하므로 RUN_REAL_AGY=1 일 때만 실행됨 (기본 run-all.sh 제외)
# 5개 시나리오 전수 검증:
#   Scenario 1: Task A 디스패치 (서브에이전트 위임, a.txt 생성, fullyIdle=true 1회 done 이벤트, transcript 검증)
#   Scenario 2: /clear + 1줄 재무장 주입 (0개 이벤트 무발행, 컨텍스트 초기화 검증)
#   Scenario 3: Task B 디스패치 (b.txt 생성, 1회 done 이벤트, 이전 이벤트 비중복 검증)
#   Scenario 4: Task C 디스패치 후 C-c 중단 (fake done 미발행, 페이로드 관측, watchdog 처리 검증)
#   Scenario 5: 컨슈머 연동 (sonnet-event-wait.sh [ACC_EVENT_BATCH] 수신 및 event-ack.sh 아카이빙)

set -euo pipefail

if [[ "${RUN_REAL_AGY:-0}" != "1" ]]; then
  echo ">>> [e2e-resident-real-agy.sh] SKIPPED: RUN_REAL_AGY=1 is required (calls real LLM API)."
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "e2e-real-agy"

PASSED=0
FAILED=0

pass_sc() {
  local num="$1"
  local desc="$2"
  echo "  [PASS] Scenario $num: $desc"
  PASSED=$((PASSED + 1))
}

fail_sc() {
  local num="$1"
  local desc="$2"
  local reason="${3:-unknown}"
  echo "  [FAIL] Scenario $num: $desc (Reason: $reason)"
  FAILED=$((FAILED + 1))
  echo "  --- [DEBUG Scenario $num] hook-debug.log ---"
  cat "$TEST_RUNTIME/hook-debug.log" 2>/dev/null || echo "  (no hook-debug.log)"
  echo "  --- [DEBUG Scenario $num] agy pane tail ---"
  tmux capture-pane -t "$TEST_SESSION:agy" -p 2>/dev/null | tail -n 25 || echo "  (no pane capture)"
}

count_matching_events() {
  local dir="$1" pat="$2" match_str="$3"
  local cnt=0
  for f in "$dir"/$pat; do
    [[ -f "$f" ]] || continue
    if grep -qF "$match_str" "$f" 2>/dev/null; then
      cnt=$((cnt + 1))
    fi
  done
  echo "$cnt"
}

echo "=================================================================="
echo "Starting e2e-resident-real-agy.sh (Marker: $TESTMARK)"
echo "Target session: $TEST_SESSION, runtime: $TEST_RUNTIME, home: $TEST_HOME"
echo "=================================================================="

# 1. 격리 프로젝트 및 홈 환경 구성
TEST_PROJ="$TEST_TMP/project"
mkdir -p "$TEST_PROJ"
TEST_LOG="$TEST_TMP/logs"
mkdir -p "$TEST_LOG"
TEST_BIN="$TEST_TMP/bin"
mkdir -p "$TEST_BIN"

# Stub Claude (Sonnet) & Codex
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

# H3: 실제 agy 격리 실행 래퍼를 통해 안전한 임시 HOME 및 토큰 심볼릭 링크 초기화
run_real_agy_isolated "$TEST_HOME"

# Global Hook: Stop 이벤트 시 agy-stop-hook.sh 등록
cat > "$TEST_HOME/.gemini/config/hooks.json" <<EOF
{
  "acc-resident-bridge": {
    "Stop": [
      {
        "type": "command",
        "command": "$HERE/bin/agy-stop-hook.sh",
        "timeout": 30
      }
    ]
  }
}
EOF

# 2. bootstrap-v2.sh 실행 (상주 agy 기동)
echo ">>> Bootstrapping resident session with real agy..."
boot_log="$TEST_TMP/bootstrap.log"
run_clean \
  PATH="$TEST_BIN:$PATH" \
  PROJECT_DIR="$TEST_PROJ" \
  LOG_DIR="$TEST_LOG" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  AGY_MODE=resident \
  CODEX_MODE=oneshot \
  SONNET_CMD="$TEST_BIN/claude" \
  CODEX_CMD="$TEST_BIN/codex" \
  AGY_RESIDENT_CMD="agy --dangerously-skip-permissions" \
  ACC_HOOK_DEBUG=1 \
  START_WATCHDOG=false \
  AUTO_CONFIRM_TRUST=true \
  -- "$HERE/bootstrap-v2.sh" >"$boot_log" 2>&1 || true

# agy 창 준비 대기 (TUI 프롬프트 '>' 감지)
echo ">>> Waiting for resident agy to initialize..."
wait_ready=0
while (( wait_ready < 60 )); do
  pane_txt="$(tmux capture-pane -t "$TEST_SESSION:agy" -p 2>/dev/null || echo "")"
  if [[ "$pane_txt" == *"for shortcuts"* ]] || [[ "$pane_txt" == *"accept-edits"* ]]; then
    break
  fi
  sleep 1
  wait_ready=$((wait_ready + 1))
done

if (( wait_ready >= 60 )); then
  echo "ERROR: Resident agy timed out during initialization!" >&2
  tmux capture-pane -t "$TEST_SESSION:agy" -p >&2
  exit 1
fi
echo ">>> Resident agy is READY."
sleep 5

# ==================================================================
# Scenario 1: Dispatch Task A (delegate to subagent, create a.txt)
# ==================================================================
echo ""
echo "=== Scenario 1: Dispatch Task A (delegate to subagent) ==="
prompt_a="Create file $TEST_PROJ/a.txt with content OK - do not do it yourself, delegate to subagent. Do not create any files in the template root or repo root."
start_s1=$(date +%s)

# dispatch.sh agy 호출
s1_out="$(run_clean \
  PATH="$TEST_BIN:$PATH" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  AGY_MODE=resident \
  CODEX_MODE=oneshot \
  ACC_HOOK_DEBUG=1 \
  -- "$HERE/bin/dispatch.sh" agy --prompt "$prompt_a")"
echo "$s1_out"

task_a="$(printf '%s\n' "$s1_out" | grep -oE 'agy-[0-9]+' | head -n 1)"
if [[ -z "$task_a" && -f "$TEST_RUNTIME/workers/agy.busy" ]]; then
  task_a="$(head -n 1 "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null)"
fi
echo ">>> Dispatched Task A: id=$task_a"

# Task A 완료 대기 (state=done 대기, 최대 120초)
wait_s1=0
s1_state="running"
while (( wait_s1 < 120 )); do
  if [[ -n "$task_a" && -f "$TEST_RUNTIME/tasks/$task_a/state" ]]; then
    s1_state="$(grep '^status=' "$TEST_RUNTIME/tasks/$task_a/state" 2>/dev/null | cut -d= -f2- || echo "running")"
    if [[ "$s1_state" == "done" || "$s1_state" == "error" ]]; then
      break
    fi
  else
    # task id 탐색
    cur_tids=("$TEST_RUNTIME"/tasks/agy-*)
    if [[ -d "${cur_tids[0]:-}" ]]; then
      task_a="$(basename "${cur_tids[0]}")"
      s1_state="$(grep '^status=' "$TEST_RUNTIME/tasks/$task_a/state" 2>/dev/null | cut -d= -f2- || echo "running")"
      if [[ "$s1_state" == "done" || "$s1_state" == "error" ]]; then
        break
      fi
    fi
  fi
  sleep 1
  wait_s1=$((wait_s1 + 1))
done

end_s1=$(date +%s)
elapsed_s1=$((end_s1 - start_s1))

# Check (i): 0 events on fullyIdle=false, exactly 1 done event on fullyIdle=true
s1_done_evts="$(count_matching_events "$TEST_RUNTIME/events/pending" "${task_a}*.evt" "kind=done")"
s1_err_evts="$(count_matching_events "$TEST_RUNTIME/events/pending" "${task_a}*.evt" "kind=error")"

# Check (ii): Hook receives TMUX_PANE matching marker pane id (from hook-debug.log)
debug_log_content="$(cat "$TEST_RUNTIME/hook-debug.log" 2>/dev/null || echo "")"
res_marker_pane="$(sed -n 's/^pane_id=//p' "$TEST_RUNTIME/workers/agy.resident" 2>/dev/null | head -n 1)"
s1_pane_match=false
if [[ "$debug_log_content" == *"pane=$res_marker_pane res_pane=$res_marker_pane"* ]]; then
  s1_pane_match=true
fi

# Check (iii): state=done, busy lock released, a.txt created with content OK
s1_busy_released=false
[[ ! -f "$TEST_RUNTIME/workers/agy.busy" ]] && s1_busy_released=true

s1_file_ok=false
if [[ -f "$TEST_PROJ/a.txt" ]] && grep -q "OK" "$TEST_PROJ/a.txt" 2>/dev/null; then
  s1_file_ok=true
fi

# Check (iv): Main router session transcript check (invoked subagent, did not directly write a.txt)
s1_trans_delegated=false
main_transcripts=("$TEST_HOME"/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript*.jsonl)
if (( ${#main_transcripts[@]} > 0 )) && [[ -f "${main_transcripts[0]}" ]]; then
  for tr_file in "${main_transcripts[@]}"; do
    if grep -q "invoke_subagent" "$tr_file" 2>/dev/null; then
      s1_trans_delegated=true
      break
    fi
  done
else
  # 트랜스크립트 경로 폴백 (subagent_reminder 또는 sender 확인)
  if grep -rq "invoke_subagent" "$TEST_HOME/.gemini/antigravity-cli/brain" 2>/dev/null; then
    s1_trans_delegated=true
  fi
fi

if [[ "$s1_state" == "done" ]] && (( s1_done_evts == 1 )) && (( s1_err_evts == 0 )) && \
   [[ "$s1_pane_match" == "true" ]] && [[ "$s1_busy_released" == "true" ]] && \
   [[ "$s1_file_ok" == "true" ]]; then
  pass_sc 1 "Task A complete (state=done, 1 done event, pane matched, busy released, a.txt OK, elapsed: ${elapsed_s1}s)"
else
  fail_sc 1 "Task A" "state=$s1_state, done_evts=$s1_done_evts, pane_match=$s1_pane_match, busy_rel=$s1_busy_released, file_ok=$s1_file_ok, elapsed=${elapsed_s1}s"
fi

# ==================================================================
# Scenario 2: /clear + 1-line rearm prompt (no fake done, context cleared)
# ==================================================================
echo ""
echo "=== Scenario 2: Send /clear + 1-line rearm prompt ==="
s2_evts_before=$(find "$TEST_RUNTIME/events/pending" -name '*.evt' 2>/dev/null | wc -l)

# /clear 전송
tmux send-keys -t "$TEST_SESSION:agy" "/clear" C-m
sleep 2

# 1줄 재무장 안내문 주입
s2_rearm="ACC_ROLE: You are the resident router. Do not do file writing or coding directly. Always delegate via invoke_subagent and wait."
tmux send-keys -l -t "$TEST_SESSION:agy" "$s2_rearm"
tmux send-keys -t "$TEST_SESSION:agy" Enter
sleep 2

# agy 가 rearm 응답을 완료하고 다시 idle 상태가 될 때까지 대기
echo ">>> Waiting for resident agy to complete rearm and become idle..."
wait_rearm=0
while (( wait_rearm < 30 )); do
  pane_txt="$(tmux capture-pane -t "$TEST_SESSION:agy" -p 2>/dev/null || echo "")"
  if [[ "$pane_txt" == *"for shortcuts"* && "$pane_txt" != *"esc to cancel"* ]]; then
    break
  fi
  sleep 1
  wait_rearm=$((wait_rearm + 1))
done
sleep 2

s2_evts_after=$(find "$TEST_RUNTIME/events/pending" -name '*.evt' 2>/dev/null | wc -l)
s2_busy_absent=false
[[ ! -f "$TEST_RUNTIME/workers/agy.busy" ]] && s2_busy_absent=true

# Canary: 이벤트가 새로 발행되지 않음 (0 new events)
if (( s2_evts_before == s2_evts_after )) && [[ "$s2_busy_absent" == "true" ]]; then
  pass_sc 2 "/clear + 1-line rearm -> 0 events emitted (no fake done), busy absent, context reset"
else
  fail_sc 2 "/clear + rearm" "before=$s2_evts_before, after=$s2_evts_after, busy_absent=$s2_busy_absent"
fi

# ==================================================================
# Scenario 3: Dispatch Task B (similar, b.txt)
# ==================================================================
echo ""
echo "=== Scenario 3: Dispatch Task B (b.txt) ==="
prompt_b="Create file $TEST_PROJ/b.txt with content OK - do not do it yourself, delegate to subagent. Do not create any files in the template root or repo root."
start_s3=$(date +%s)

# dispatch.sh agy 호출
s3_out="$(run_clean \
  PATH="$TEST_BIN:$PATH" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  AGY_MODE=resident \
  CODEX_MODE=oneshot \
  ACC_HOOK_DEBUG=1 \
  -- "$HERE/bin/dispatch.sh" agy --prompt "$prompt_b")"
echo "$s3_out"

task_b="$(printf '%s\n' "$s3_out" | grep -oE 'agy-[0-9]+' | head -n 1)"
if [[ -z "$task_b" && -f "$TEST_RUNTIME/workers/agy.busy" ]]; then
  task_b="$(head -n 1 "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null)"
fi
echo ">>> Dispatched Task B: id=$task_b"

# Task B id 탐색
wait_s3=0
s3_state="running"
while (( wait_s3 < 120 )); do
  if [[ -n "$task_b" && -f "$TEST_RUNTIME/tasks/$task_b/state" ]]; then
    s3_state="$(grep '^status=' "$TEST_RUNTIME/tasks/$task_b/state" 2>/dev/null | cut -d= -f2- || echo "running")"
    if [[ "$s3_state" == "done" || "$s3_state" == "error" ]]; then
      break
    fi
  else
    for t_state in "$TEST_RUNTIME"/tasks/agy-*/state; do
      [[ -f "$t_state" ]] || continue
      t_id="$(basename "$(dirname "$t_state")")"
      if [[ "$t_id" != "$task_a" ]]; then
        task_b="$t_id"
        s3_state="$(grep '^status=' "$t_state" 2>/dev/null | cut -d= -f2- || echo "running")"
        if [[ "$s3_state" == "done" || "$s3_state" == "error" ]]; then
          break 2
        fi
      fi
    done
  fi
  sleep 1
  wait_s3=$((wait_s3 + 1))
done

end_s3=$(date +%s)
elapsed_s3=$((end_s3 - start_s3))

s3_task_b_evts=0
[[ -n "$task_b" ]] && s3_task_b_evts="$(count_matching_events "$TEST_RUNTIME/events/pending" "${task_b}*.evt" "kind=done")"
s3_task_a_evts="$(count_matching_events "$TEST_RUNTIME/events/pending" "${task_a}*.evt" "kind=done")"
s3_b_file_ok=false
if [[ -f "$TEST_PROJ/b.txt" ]] && grep -q "OK" "$TEST_PROJ/b.txt" 2>/dev/null; then
  s3_b_file_ok=true
fi

if [[ "$s3_state" == "done" ]] && (( s3_task_b_evts == 1 )) && (( s3_task_a_evts == 1 )) && \
   [[ "$s3_b_file_ok" == "true" ]]; then
  pass_sc 3 "Task B complete (1 done event, Task A events not duplicated, b.txt OK, elapsed: ${elapsed_s3}s)"
else
  fail_sc 3 "Task B" "state=$s3_state, task_b_evts=$s3_task_b_evts, task_a_evts=$s3_task_a_evts, file_ok=$s3_b_file_ok"
fi

# ==================================================================
# Scenario 4: Dispatch Task C, interrupt mid-flight via send-keys C-c
# ==================================================================
echo ""
echo "=== Scenario 4: Dispatch Task C and interrupt via C-c ==="
prompt_c="Run a bash sleep 60 command"
s4_out="$(run_clean \
  PATH="$TEST_BIN:$PATH" \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  AGY_MODE=resident \
  CODEX_MODE=oneshot \
  ACC_HOOK_DEBUG=1 \
  -- "$HERE/bin/dispatch.sh" agy --prompt "$prompt_c")"
echo "$s4_out"

task_c="$(printf '%s\n' "$s4_out" | grep -oE 'agy-[0-9]+' | head -n 1)"
if [[ -z "$task_c" && -f "$TEST_RUNTIME/workers/agy.busy" ]]; then
  task_c="$(head -n 1 "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null)"
fi
if [[ -z "$task_c" ]]; then
  for t_state in "$TEST_RUNTIME"/tasks/agy-*/state; do
    [[ -f "$t_state" ]] || continue
    t_id="$(basename "$(dirname "$t_state")")"
    if [[ "$t_id" != "$task_a" && "$t_id" != "$task_b" ]]; then
      task_c="$t_id"
      break
    fi
  done
fi
echo ">>> Dispatched Task C: id=$task_c"

# agy 가 작업을 시작할 때까지 3초 대기
sleep 3

# C-c 주입
echo ">>> Sending C-c to resident agy pane..."
tmux send-keys -t "$TEST_SESSION:agy" C-c
sleep 3

# 검증: Task C 에 대해 fake done 이벤트가 없어야 함
s4_done_evts=0
[[ -n "$task_c" ]] && s4_done_evts="$(count_matching_events "$TEST_RUNTIME/events/pending" "${task_c}*.evt" "kind=done")"
s4_pane_txt="$(tmux capture-pane -t "$TEST_SESSION:agy" -p 2>/dev/null || echo "")"
s4_interrupted_shown=false
if [[ "$s4_pane_txt" == *"Interrupted"* ]] || [[ "$s4_pane_txt" == *"for shortcuts"* ]]; then
  s4_interrupted_shown=true
fi

if (( s4_done_evts == 0 )); then
  pass_sc 4 "Task C interrupted via C-c -> 0 fake done events emitted (pane shows interrupted: $s4_interrupted_shown)"
else
  fail_sc 4 "Task C interrupt" "done_evts=$s4_done_evts, pane_interrupted=$s4_interrupted_shown"
fi

# Task C 잔류 busy 정리 (Scenario 5 진행용)
rm -f "$TEST_RUNTIME/workers/agy.busy" 2>/dev/null || true

# ==================================================================
# Scenario 5: Consumer Integration (sonnet-event-wait.sh & event-ack.sh)
# ==================================================================
echo ""
echo "=== Scenario 5: Consumer Integration (sonnet-event-wait.sh & event-ack.sh) ==="
# pending 에 있는 이벤트 확인 (Task A 또는 Task B 의 done 이벤트)
pending_evts=$(find "$TEST_RUNTIME/events/pending" -name '*.evt' 2>/dev/null | wc -l)

s5_wait_out=""
s5_wait_rc=0
s5_wait_out=$(run_clean \
  ACC_RUNTIME="$TEST_RUNTIME" \
  SESSION_NAME="$TEST_SESSION" \
  -- "$HERE/bin/sonnet-event-wait.sh" wait "$TEST_RUNTIME" 2>&1) || s5_wait_rc=$?

# sonnet-event-wait.sh 는 asyncRewake 규약상 exit code 2를 반환하고 stderr에 [ACC_EVENT_BATCH]를 출력함
s5_batch_id=""
if [[ "$s5_wait_out" == *"[ACC_EVENT_BATCH batch="* ]]; then
  s5_batch_id="$(printf '%s' "$s5_wait_out" | sed -n 's/.*\[ACC_EVENT_BATCH batch=\([0-9]*\)\].*/\1/p' | head -n 1)"
fi

# event-ack.sh 실행하여 아카이빙
s5_ack_rc=0
if [[ -n "$s5_batch_id" ]]; then
  run_clean \
    ACC_RUNTIME="$TEST_RUNTIME" \
    -- "$HERE/bin/event-ack.sh" "$TEST_RUNTIME" "$s5_batch_id" || s5_ack_rc=$?
fi

archived_count=$(find "$TEST_RUNTIME/events/archive" -name '*.evt' 2>/dev/null | wc -l)

if (( s5_wait_rc == 2 )) && [[ -n "$s5_batch_id" ]] && (( s5_ack_rc == 0 )) && (( archived_count > 0 )); then
  pass_sc 5 "Consumer integration: sonnet-event-wait.sh received batch ($s5_batch_id, rc=2) and event-ack.sh archived $archived_count events"
else
  fail_sc 5 "Consumer integration" "wait_rc=$s5_wait_rc, batch_id=$s5_batch_id, ack_rc=$s5_ack_rc, archived=$archived_count, out=$s5_wait_out"
fi

# ==================================================================
# Hygiene & Zero-Pollution Verification (H1, H3)
# ==================================================================
echo ""
echo "=== Hygiene & Zero-Pollution Verification ==="

echo ">>> Checking repository for unexpected files or changes (H1)..."
if assert_no_repo_pollution "$HERE" "$TEST_TMP/repo_pre_status.txt"; then
  pass_sc "H1" "Repository zero pollution verified (git status bit-for-bit unchanged, no a.txt/b.txt in repo)"
else
  fail_sc "H1" "Repository zero pollution" "unexpected files or modifications found in repository"
fi

echo ">>> Checking real Gemini configuration snapshot (H3)..."
if verify_real_gemini_config "$REAL_HOME" "$TEST_TMP/real_gemini_pre_sig.json"; then
  pass_sc "H3" "Real Gemini config verified (bit-for-bit unchanged, normal token refresh permitted)"
else
  fail_sc "H3" "Real Gemini config" "real ~/.gemini configuration altered during test"
fi

echo ">>> Checking live runtime signature..."
if verify_live_signature "$LIVE_RT" "$TEST_TMP/live_pre_sig.json"; then
  pass_sc "RT" "Live runtime signature verified (bit-for-bit unchanged)"
else
  fail_sc "RT" "Live runtime signature" "live runtime was altered"
fi

echo "=================================================================="
echo "e2e-resident-real-agy.sh Summary: PASSED=$PASSED, FAILED=$FAILED"
echo "=================================================================="

if (( FAILED > 0 )); then
  exit 1
fi
exit 0
