#!/usr/bin/env bash
# tests/harness-self-test.sh: 하네스 실시간 오염 감지기 및 시그니처 대조기 자체 검증 (F2.3)
# 주의: LIVE_RT_OVERRIDE 를 사용하여 격리된 가짜(fake) 런타임에 대해서만 검증함. 실제 운영 런타임 절대 미접촉.
# 검증 항목:
#   1. 정상 베이스라인 시그니처 대조 성공
#   2. workers/ 변조 시 시그니처 대조 실패 감지
#   3. 비-running tasks/state 변조 시 시그니처 대조 실패 감지
#   4. 런타임 설정 파일(session_name 등) 변조 시 시그니처 대조 실패 감지
#   5. 플레인 텍스트 마커 오염 감지 및 실패
#   6. base64 인코딩 필드(summary_b64=) 내 마커 오염 감지 및 실패
#   7. 임의 base64 토큰 내 마커 오염 감지 및 실패
#   8. running task 예외: running task의 정상 상태 전이 시 경고 출력 및 통과
#   9. running task 부재 시 events/ 변조 감지 및 실패
#   10. H1: 리포 상태 베이스라인 대조 성공
#   11. H1: 리포 내 예상치 못한 파일(a.txt 등) 생성 시 오염 감지 및 실패
#   12. H2: 프로세스가 종료 후 파일 재생성을 시도해도 완전 회수 및 디렉토리 완전 삭제
#   13. H3: 실제 Gemini 설정 스냅샷 베이스라인 대조 성공
#   14. H3: Gemini config (projects/ 등) 변조 감지 및 실패
#   15. H3: Gemini CLI 설정 (settings.json 등) 변조 감지 및 실패
#   16. H3: 정상 OAuth 토큰 갱신(크기/해시 변경) 시 오탐 방지 및 통과
#   17. H3: OAuth 토큰 삭제 감지 및 실패
#   18. H3: run_real_agy_isolated 가 실제 HOME 또는 비-임시 경로 시 즉시 거부(fail-closed)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "harness-self"

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
echo "Running harness-self-test.sh (Marker: $TESTMARK)"
echo "Target runtime: $TEST_RUNTIME"
echo "=================================================================="

# 가짜 라이브 런타임 디렉토리 생성
FAKE_LIVE="$TEST_TMP/fake_live_rt"
mkdir -p "$FAKE_LIVE"/{workers,tasks/{task-run,task-done},events/{pending,inflight,archive}}

printf 'task-run\n1790000000\n' > "$FAKE_LIVE/workers/agy.busy"
cat > "$FAKE_LIVE/tasks/task-run/state" <<'EOF'
task_id=task-run
worker=agy
status=running
started_epoch=1790000000
EOF

cat > "$FAKE_LIVE/tasks/task-done/state" <<'EOF'
task_id=task-done
worker=agy
status=done
started_epoch=1780000000
completed_epoch=1780000100
EOF

printf 'bootstrap_version=2\n' > "$FAKE_LIVE/bootstrap_version"
printf 'fake-live-session\n' > "$FAKE_LIVE/session_name"
printf '{"hooks": {}}\n' > "$FAKE_LIVE/claude-bridge.settings.json"

FAKE_SIG_FILE="$TEST_TMP/fake_pre_sig.json"
record_live_signature "$FAKE_LIVE" "$FAKE_SIG_FILE"

# ------------------------------------------------------------------
# 1. Baseline signature match
# ------------------------------------------------------------------
if verify_live_signature "$FAKE_LIVE" "$FAKE_SIG_FILE" >/dev/null 2>&1; then
  pass_case 1 "baseline signature matches clean fake live runtime"
else
  fail_case 1 "baseline signature match"
fi

# ------------------------------------------------------------------
# 2. workers/ mutation detection
# ------------------------------------------------------------------
touch "$FAKE_LIVE/workers/leak.lock"
if ! verify_live_signature "$FAKE_LIVE" "$FAKE_SIG_FILE" >/dev/null 2>&1; then
  pass_case 2 "mutation in workers/ detected and rejected"
else
  fail_case 2 "workers/ mutation detection" "detector failed to reject"
fi
rm -f "$FAKE_LIVE/workers/leak.lock"

# ------------------------------------------------------------------
# 3. Non-running task state mutation detection
# ------------------------------------------------------------------
echo "status=error" >> "$FAKE_LIVE/tasks/task-done/state"
if ! verify_live_signature "$FAKE_LIVE" "$FAKE_SIG_FILE" >/dev/null 2>&1; then
  pass_case 3 "mutation in non-running task state detected and rejected"
else
  fail_case 3 "non-running task mutation" "detector failed to reject"
fi
# Revert
cat > "$FAKE_LIVE/tasks/task-done/state" <<'EOF'
task_id=task-done
worker=agy
status=done
started_epoch=1780000000
completed_epoch=1780000100
EOF

# ------------------------------------------------------------------
# 4. Config file mutation detection
# ------------------------------------------------------------------
echo "polluted_session" > "$FAKE_LIVE/session_name"
if ! verify_live_signature "$FAKE_LIVE" "$FAKE_SIG_FILE" >/dev/null 2>&1; then
  pass_case 4 "mutation in config file (session_name) detected and rejected"
else
  fail_case 4 "config mutation" "detector failed to reject"
fi
printf 'fake-live-session\n' > "$FAKE_LIVE/session_name"

# ------------------------------------------------------------------
# 5. Plain-text marker leak detection
# ------------------------------------------------------------------
echo "leak with $TESTMARK" > "$FAKE_LIVE/tasks/task-done/output.log"
if ! assert_no_live_pollution "$FAKE_LIVE"; then
  pass_case 5 "plain-text marker leak detected and rejected"
else
  fail_case 5 "plain-text leak" "detector missed plain marker"
fi
rm -f "$FAKE_LIVE/tasks/task-done/output.log"

# ------------------------------------------------------------------
# 6. Base64-encoded marker in summary_b64= field leak detection
# ------------------------------------------------------------------
b64_val="$(printf 'error message %s' "$TESTMARK" | base64 -w0)"
printf 'kind=error\nsummary_b64=%s\n' "$b64_val" > "$FAKE_LIVE/events/pending/leak.evt"
if ! assert_no_live_pollution "$FAKE_LIVE"; then
  pass_case 6 "base64-encoded field (summary_b64=) marker leak detected and rejected"
else
  fail_case 6 "base64 field leak" "detector missed b64 marker"
fi
rm -f "$FAKE_LIVE/events/pending/leak.evt"

# ------------------------------------------------------------------
# 7. Arbitrary base64 token containing marker leak detection
# ------------------------------------------------------------------
b64_token="$(printf 'prefix_%s_suffix' "$TESTMARK" | base64 -w0)"
printf 'some_data: "%s"\n' "$b64_token" > "$FAKE_LIVE/tasks/task-done/notes.json"
if ! assert_no_live_pollution "$FAKE_LIVE"; then
  pass_case 7 "arbitrary base64 token marker leak detected and rejected"
else
  fail_case 7 "arbitrary base64 token leak" "detector missed token"
fi
rm -f "$FAKE_LIVE/tasks/task-done/notes.json"

# ------------------------------------------------------------------
# 8. Running task exception: running task transition outputs warning and passes
# ------------------------------------------------------------------
echo "status=done" > "$FAKE_LIVE/tasks/task-run/state"
echo "event-data" > "$FAKE_LIVE/events/pending/task-run.evt"
c8_err="$TEST_TMP/c8_err.log"
c8_ok=false
if verify_live_signature "$FAKE_LIVE" "$FAKE_SIG_FILE" 2>"$c8_err"; then
  c8_ok=true
fi
rm -f "$FAKE_LIVE/events/pending/task-run.evt"

if [[ "$c8_ok" == "true" ]] && grep -q "라이브 task 진행 중 — 시그니처 비교 생략" "$c8_err"; then
  pass_case 8 "running task transition allowed with warning message"
else
  fail_case 8 "running task exception" "ok=$c8_ok, err=$(cat "$c8_err" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# 9. Events mutation when NO running task -> rejected
# ------------------------------------------------------------------
# Create clean fake live with no running tasks
FAKE_NORUN="$TEST_TMP/fake_norun"
mkdir -p "$FAKE_NORUN"/{workers,tasks/t1,events/{pending,inflight,archive}}
echo "status=done" > "$FAKE_NORUN/tasks/t1/state"
printf 'bootstrap_version=2\n' > "$FAKE_NORUN/bootstrap_version"
printf 's1\n' > "$FAKE_NORUN/session_name"
printf '{}\n' > "$FAKE_NORUN/claude-bridge.settings.json"
NORUN_SIG="$TEST_TMP/norun_sig.json"
record_live_signature "$FAKE_NORUN" "$NORUN_SIG"

# Add event to pending
touch "$FAKE_NORUN/events/pending/leak.evt"
if ! verify_live_signature "$FAKE_NORUN" "$NORUN_SIG" >/dev/null 2>&1; then
  pass_case 9 "events mutation without running task detected and rejected"
else
  fail_case 9 "events mutation without running task" "detector failed to reject"
fi

# ------------------------------------------------------------------
# 10. H1: Repository status baseline matches clean fake repo
# ------------------------------------------------------------------
FAKE_REPO="$TEST_TMP/fake_repo"
mkdir -p "$FAKE_REPO"
git -C "$FAKE_REPO" init -q -b main
git -C "$FAKE_REPO" config user.email "test@example.com"
git -C "$FAKE_REPO" config user.name "Tester"
touch "$FAKE_REPO/tracked.txt"
git -C "$FAKE_REPO" add tracked.txt
git -C "$FAKE_REPO" commit -q -m "init"
FAKE_REPO_SIG="$TEST_TMP/fake_repo_pre.txt"
snapshot_repo_status "$FAKE_REPO" "$FAKE_REPO_SIG"

if assert_no_repo_pollution "$FAKE_REPO" "$FAKE_REPO_SIG" >/dev/null 2>&1; then
  pass_case 10 "H1: baseline repo status matches clean fake repo"
else
  fail_case 10 "H1: baseline repo status match"
fi

# ------------------------------------------------------------------
# 11. H1: Repository pollution detection (new file detected and rejected)
# ------------------------------------------------------------------
touch "$FAKE_REPO/a.txt"
c11_err="$TEST_TMP/c11_err.log"
c11_fail=false
if ! assert_no_repo_pollution "$FAKE_REPO" "$FAKE_REPO_SIG" 2>"$c11_err"; then
  c11_fail=true
fi
rm -f "$FAKE_REPO/a.txt"

if [[ "$c11_fail" == "true" ]] && grep -q "a.txt" "$c11_err"; then
  pass_case 11 "H1: repo pollution (a.txt) detected and rejected with filename"
else
  fail_case 11 "H1: repo pollution detection" "fail=$c11_fail, err=$(cat "$c11_err" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# 12. H2: Process cleanup and directory deletion (stubborn process)
# ------------------------------------------------------------------
MOCK_TREE="$TEST_TMP/mock_cleanup_tree"
mkdir -p "$MOCK_TREE/home/cache"
(
  export HOME="$MOCK_TREE/home"
  cd "$MOCK_TREE"
  trap "" TERM
  while true; do
    mkdir -p "$MOCK_TREE/home/cache"
    touch "$MOCK_TREE/home/cache/leak.tmp"
    sleep 0.05
  done
) &
MOCK_PID=$!
sleep 0.2

# Run cleanup_process_and_tree
c12_ok=false
if cleanup_process_and_tree "$MOCK_TREE" "" 5; then
  c12_ok=true
fi

# Check PID is dead and directory gone
pid_alive=false
if kill -0 "$MOCK_PID" 2>/dev/null; then
  pid_alive=true
  kill -9 "$MOCK_PID" 2>/dev/null || true
fi

dir_remains=false
if [[ -e "$MOCK_TREE" ]]; then
  dir_remains=true
  rm -rf "$MOCK_TREE" 2>/dev/null || true
fi

if [[ "$c12_ok" == "true" && "$pid_alive" == "false" && "$dir_remains" == "false" ]]; then
  pass_case 12 "H2: stubborn process terminated and directory completely removed"
else
  fail_case 12 "H2: process cleanup" "ok=$c12_ok, pid_alive=$pid_alive, dir_remains=$dir_remains"
fi

# ------------------------------------------------------------------
# 13. H3: Real Gemini config baseline match on clean fake home
# ------------------------------------------------------------------
FAKE_GEMINI_HOME="$TEST_TMP/fake_gemini_home"
mkdir -p "$FAKE_GEMINI_HOME/.gemini/config/projects"
mkdir -p "$FAKE_GEMINI_HOME/.gemini/antigravity-cli"
printf '{"version": 1}\n' > "$FAKE_GEMINI_HOME/.gemini/config/mcp_config.json"
printf '{"agentMode": "accept-edits"}\n' > "$FAKE_GEMINI_HOME/.gemini/antigravity-cli/settings.json"
printf 'initial_oauth_token_v1\n' > "$FAKE_GEMINI_HOME/.gemini/antigravity-cli/antigravity-oauth-token"
FAKE_GEMINI_SIG="$TEST_TMP/fake_gemini_pre.json"
snapshot_real_gemini_config "$FAKE_GEMINI_HOME" "$FAKE_GEMINI_SIG"

if verify_real_gemini_config "$FAKE_GEMINI_HOME" "$FAKE_GEMINI_SIG" >/dev/null 2>&1; then
  pass_case 13 "H3: baseline Gemini config snapshot matches clean fake home"
else
  fail_case 13 "H3: baseline Gemini config match"
fi

# ------------------------------------------------------------------
# 14. H3: Gemini config pollution detection (projects/leak.json added)
# ------------------------------------------------------------------
touch "$FAKE_GEMINI_HOME/.gemini/config/projects/leak.json"
c14_err="$TEST_TMP/c14_err.log"
c14_fail=false
if ! verify_real_gemini_config "$FAKE_GEMINI_HOME" "$FAKE_GEMINI_SIG" 2>"$c14_err"; then
  c14_fail=true
fi
rm -f "$FAKE_GEMINI_HOME/.gemini/config/projects/leak.json"

if [[ "$c14_fail" == "true" ]] && grep -q "projects/leak.json" "$c14_err"; then
  pass_case 14 "H3: Gemini config pollution (projects/leak.json) detected and rejected"
else
  fail_case 14 "H3: Gemini config pollution detection" "fail=$c14_fail, err=$(cat "$c14_err" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# 15. H3: Gemini CLI settings pollution detection (settings.json modified)
# ------------------------------------------------------------------
echo '{"agentMode": "plan"}' > "$FAKE_GEMINI_HOME/.gemini/antigravity-cli/settings.json"
c15_err="$TEST_TMP/c15_err.log"
c15_fail=false
if ! verify_real_gemini_config "$FAKE_GEMINI_HOME" "$FAKE_GEMINI_SIG" 2>"$c15_err"; then
  c15_fail=true
fi
printf '{"agentMode": "accept-edits"}\n' > "$FAKE_GEMINI_HOME/.gemini/antigravity-cli/settings.json"

if [[ "$c15_fail" == "true" ]] && grep -q "settings.json" "$c15_err"; then
  pass_case 15 "H3: Gemini CLI setting alteration (settings.json) detected and rejected"
else
  fail_case 15 "H3: Gemini CLI setting alteration" "fail=$c15_fail, err=$(cat "$c15_err" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# 16. H3: Token renewal tolerance (size and hash changed -> pass)
# ------------------------------------------------------------------
printf 'renewed_oauth_token_v2_with_different_size_and_hash_string\n' > "$FAKE_GEMINI_HOME/.gemini/antigravity-cli/antigravity-oauth-token"
if verify_real_gemini_config "$FAKE_GEMINI_HOME" "$FAKE_GEMINI_SIG" >/dev/null 2>&1; then
  pass_case 16 "H3: token renewal tolerance (size/hash changed) permitted without false alarm"
else
  fail_case 16 "H3: token renewal tolerance" "normal token renewal was falsely rejected"
fi

# ------------------------------------------------------------------
# 17. H3: Token deletion detection (token deleted -> fail)
# ------------------------------------------------------------------
rm -f "$FAKE_GEMINI_HOME/.gemini/antigravity-cli/antigravity-oauth-token"
c17_err="$TEST_TMP/c17_err.log"
c17_fail=false
if ! verify_real_gemini_config "$FAKE_GEMINI_HOME" "$FAKE_GEMINI_SIG" 2>"$c17_err"; then
  c17_fail=true
fi
# Restore
printf 'initial_oauth_token_v1\n' > "$FAKE_GEMINI_HOME/.gemini/antigravity-cli/antigravity-oauth-token"

if [[ "$c17_fail" == "true" ]] && grep -q "Token metadata changed" "$c17_err"; then
  pass_case 17 "H3: token deletion detected and rejected"
else
  fail_case 17 "H3: token deletion detection" "fail=$c17_fail, err=$(cat "$c17_err" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# 18. H3: run_real_agy_isolated aborts if HOME is real home or not under temp
# ------------------------------------------------------------------
c18_abort_real=false
if ( REAL_HOME_OVERRIDE="$FAKE_GEMINI_HOME" run_real_agy_isolated "$FAKE_GEMINI_HOME" ) >/dev/null 2>&1; then
  :
else
  c18_abort_real=true
fi

c18_abort_notemp=false
if ( REAL_HOME_OVERRIDE="$FAKE_GEMINI_HOME" TEST_TMP_BASE="$TEST_TMP" run_real_agy_isolated "/opt/not_temp" ) >/dev/null 2>&1; then
  :
else
  c18_abort_notemp=true
fi

# Valid temp home setup succeeds and creates token symlink
VALID_TMP_HOME="$TEST_TMP/valid_tmp_home"
c18_valid_ok=false
if REAL_HOME_OVERRIDE="$FAKE_GEMINI_HOME" TEST_TMP_BASE="$TEST_TMP" run_real_agy_isolated "$VALID_TMP_HOME"; then
  if [[ -L "$VALID_TMP_HOME/.gemini/antigravity-cli/antigravity-oauth-token" ]]; then
    c18_valid_ok=true
  fi
fi

if [[ "$c18_abort_real" == "true" && "$c18_abort_notemp" == "true" && "$c18_valid_ok" == "true" ]]; then
  pass_case 18 "H3: run_real_agy_isolated fails-closed on real HOME/non-temp, symlinks token on valid temp"
else
  fail_case 18 "H3: run_real_agy_isolated guard" "abort_real=$c18_abort_real, abort_notemp=$c18_abort_notemp, valid_ok=$c18_valid_ok"
fi

echo "=================================================================="
echo "harness-self-test.sh Summary: PASSED=$PASSED, FAILED=$FAILED"
echo "=================================================================="

if (( FAILED > 0 )); then
  exit 1
fi
exit 0
