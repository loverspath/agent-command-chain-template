#!/usr/bin/env bash
# tests/run-all.sh: 전체 격리 테스트 순차 실행 및 종합 결과 보고
# 대상 테스트:
#   1. tests/stop-hook-test.sh
#   2. tests/dispatch-regression-test.sh
#   3. tests/watchdog-resident-test.sh
#   4. tests/bootstrap-resident-test.sh
#   5. tests/harness-self-test.sh
# 옵션:
#   --dirty-env-selfcheck: 상위 프로세스에 오염 환경변수(ACC_RUNTIME, SESSION_NAME, TMUX)를 강제 주입하여
#                          오염된 환경에서도 테스트 하네스가 완전 격리 및 100% 통과함을 입증
# 요구사항: 3분 이내 완료, LLM 호출 없음, 전건 통과 시 exit 0
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

DIRTY_SELFCHECK=false
for arg in "$@"; do
  case "$arg" in
    --dirty-env-selfcheck)
      DIRTY_SELFCHECK=true
      ;;
    -h|--help)
      echo "Usage: $0 [--dirty-env-selfcheck]"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 64
      ;;
  esac
done

echo "=================================================================="
echo "Starting test suite run-all.sh at $(date '+%Y-%m-%d %H:%M:%S')"
echo "=================================================================="

if [[ "$DIRTY_SELFCHECK" == "true" ]]; then
  echo ">>> [run-all.sh] Injected DIRTY environment variables for self-check:"
  export ACC_RUNTIME="${LIVE_RT}"
  export SESSION_NAME="agentchain-v2"
  export TMUX="${TMUX:-/tmp/tmux-dirty,9999,99}"
  echo "    ACC_RUNTIME=$ACC_RUNTIME"
  echo "    SESSION_NAME=$SESSION_NAME"
  echo "    TMUX=$TMUX"
  echo "=================================================================="
fi

start_time=$(date +%s)
TOTAL_PASS=0
TOTAL_FAIL=0

run_suite() {
  local suite_name="$1"
  local script_path="$SCRIPT_DIR/$suite_name"
  echo ""
  echo ">>> Running $suite_name ..."
  local rc=0
  "$script_path" || rc=$?
  if (( rc == 0 )); then
    echo ">>> $suite_name: COMPLETED (PASS)"
    TOTAL_PASS=$((TOTAL_PASS + 1))
  else
    echo ">>> $suite_name: COMPLETED (FAIL, exit code: $rc)"
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
  fi
}

run_suite "stop-hook-test.sh"
run_suite "dispatch-regression-test.sh"
run_suite "watchdog-resident-test.sh"
run_suite "bootstrap-resident-test.sh"
run_suite "harness-self-test.sh"
run_suite "codex-fail-closed-test.sh"
run_suite "dashboard-test.sh"

end_time=$(date +%s)
elapsed=$((end_time - start_time))

echo ""
echo "=================================================================="
echo "RUN-ALL SUMMARY (Elapsed: ${elapsed}s)"
echo "Suites Passed: $TOTAL_PASS / $((TOTAL_PASS + TOTAL_FAIL))"
echo "Suites Failed: $TOTAL_FAIL / $((TOTAL_PASS + TOTAL_FAIL))"
echo "=================================================================="

if (( TOTAL_FAIL > 0 )); then
  echo "Result: FAIL (Some test suites failed)"
  exit 1
fi

echo "Result: ALL TESTS PASSED (100% OK)"
exit 0
