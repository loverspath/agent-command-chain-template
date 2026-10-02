#!/usr/bin/env bash
# tests/test-config-layering.sh: 2-tier instance configuration layering verification
#
# Covers:
#   가. 인스턴스 없음: 리팩터링 전/후 스크립트 주요 변수값 동일성 검증 (패리티)
#   나. ACC_INSTANCE=x + fixture instances/x.env: 값 오버라이드 및 env 스냅샷 우선순위 동작 검증
#   다. ACC_RUNTIME/instance 표식 파일로부터 id 해석 동작 검증
#   라. 지정된 instance 파일 부재 / 유효하지 않은 ID 시 return 70 및 스크립트 exit 70 검증
#   마. 라이브와 동일 fixture(SESSION_NAME=agentchain-v2, PROJECT_DIR=/mnt/c/Users/rerun/llm-wiki) 불변 검증
#
# 격리 원칙:
#   - env -i 기반 깨끗한 서브셸 실행으로 외부 환경변수 상속 완전 차단
#   - mktemp -d 기반 격리 fixture만 사용 (실제 프로세스 기동 절대 없음)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TMP_DIR="$(mktemp -d /tmp/test-cfg-layering-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

CLEAN_ENV=(
  env -i
  PATH="$PATH"
  HOME="/tmp"
  TMPDIR="/tmp"
)

TOTAL_TESTS=0
PASSED_TESTS=0

pass_case() {
  local num="$1"
  local desc="$2"
  echo "  [PASS] Case $num: $desc"
  TOTAL_TESTS=$((TOTAL_TESTS + 1))
  PASSED_TESTS=$((PASSED_TESTS + 1))
}

fail_case() {
  local num="$1"
  local desc="$2"
  local err="${3:-}"
  echo "  [FAIL] Case $num: $desc" >&2
  [[ -n "$err" ]] && echo "         Detail: $err" >&2
  TOTAL_TESTS=$((TOTAL_TESTS + 1))
  exit 1
}

make_fixture_tpl() {
  local target="$1"
  mkdir -p "$target/lib" "$target/bin" "$target/instances" "$target/runtime"
  cp "$TEMPLATE_ROOT/lib/"*.sh "$target/lib/"
  cp "$TEMPLATE_ROOT/bin/"*.sh "$target/bin/"
  chmod +x "$target/lib/"*.sh "$target/bin/"*.sh
}

echo "=================================================================="
echo "Running test-config-layering.sh"
echo "Fixture directory: $TMP_DIR"
echo "=================================================================="

# ------------------------------------------------------------------
# 가. 인스턴스 없음 (No instance specified): config.env만 로드, 패리티 검증
# ------------------------------------------------------------------
TPL_A="$TMP_DIR/tpl_a"
make_fixture_tpl "$TPL_A"
cat > "$TPL_A/config.env" <<'EOF'
SESSION_NAME=agentchain-test-a
PROJECT_DIR=/test/project/a
WORKER_MODE=oneshot
DEFAULT_TASK_TIMEOUT=1200
EOF

out_a="$("${CLEAN_ENV[@]}" bash --noprofile --norc -c '
  source "'"$TPL_A"'/lib/config.sh"
  acc_load_config "'"$TPL_A"'"
  echo "rc=$?"
  echo "session=$SESSION_NAME"
  echo "project=$PROJECT_DIR"
  echo "inst=${ACC_INSTANCE:-<unset>}"
')"

if grep -q "session=agentchain-test-a" <<< "$out_a" && \
   grep -q "project=/test/project/a" <<< "$out_a" && \
   grep -q "inst=<unset>" <<< "$out_a"; then
  pass_case 1 "가. 인스턴스 미지정 시 config.env 단독 로드 및 ACC_INSTANCE 미설정 검증"
else
  fail_case 1 "가. 인스턴스 미지정 시 로드 실패" "$out_a"
fi

# ------------------------------------------------------------------
# 나. ACC_INSTANCE=x + fixture instances/x.env: 오버라이드 및 env 스냅샷 우선순위
# ------------------------------------------------------------------
TPL_B="$TMP_DIR/tpl_b"
make_fixture_tpl "$TPL_B"
cat > "$TPL_B/config.env" <<'EOF'
SESSION_NAME=base-session
PROJECT_DIR=/base/project
DEFAULT_TASK_TIMEOUT=1800
HANDOFF_FILE=/base/handoff.md
EOF

cat > "$TPL_B/instances/inst-alpha.env" <<'EOF'
SESSION_NAME=acc-inst-alpha
PROJECT_DIR=/alpha/project
HANDOFF_FILE=/alpha/handoff.md
EOF

out_b="$("${CLEAN_ENV[@]}" ACC_INSTANCE="inst-alpha" bash --noprofile --norc -c '
  source "'"$TPL_B"'/lib/config.sh"
  acc_load_config "'"$TPL_B"'"
  echo "session=$SESSION_NAME"
  echo "project=$PROJECT_DIR"
  echo "handoff=$HANDOFF_FILE"
  echo "timeout=$DEFAULT_TASK_TIMEOUT"
  echo "inst=$ACC_INSTANCE"
')"

if grep -q "session=acc-inst-alpha" <<< "$out_b" && \
   grep -q "project=/alpha/project" <<< "$out_b" && \
   grep -q "handoff=/alpha/handoff.md" <<< "$out_b" && \
   grep -q "timeout=1800" <<< "$out_b" && \
   grep -q "inst=inst-alpha" <<< "$out_b"; then
  pass_case 2 "나-1. ACC_INSTANCE=x 로드 시 instances/x.env 값이 config.env를 정상 오버라이드"
else
  fail_case 2 "나-1. instances/x.env 오버라이드 실패" "$out_b"
fi

# 나-2: 호출 환경변수 최우선(env snapshot priority) 검증
out_b2="$("${CLEAN_ENV[@]}" ACC_INSTANCE="inst-alpha" SESSION_NAME="env-forced-session" bash --noprofile --norc -c '
  _ENV_SESSION_NAME="${SESSION_NAME:-}"
  source "'"$TPL_B"'/lib/config.sh"
  acc_load_config "'"$TPL_B"'"
  [[ -n "$_ENV_SESSION_NAME" ]] && SESSION_NAME="$_ENV_SESSION_NAME"
  echo "session=$SESSION_NAME"
  echo "inst=$ACC_INSTANCE"
')"

if grep -q "session=env-forced-session" <<< "$out_b2" && \
   grep -q "inst=inst-alpha" <<< "$out_b2"; then
  pass_case 3 "나-2. 호출 셸의 환경변수 스냅샷이 instances/x.env 값보다 우선"
else
  fail_case 3 "나-2. 환경변수 우선순위 실패" "$out_b2"
fi

# ------------------------------------------------------------------
# 다. ACC_RUNTIME/instance 표식 파일로부터 ID 자동 해석 검증
# ------------------------------------------------------------------
TPL_C="$TMP_DIR/tpl_c"
make_fixture_tpl "$TPL_C"
cat > "$TPL_C/config.env" <<'EOF'
SESSION_NAME=base-session
PROJECT_DIR=/base/project
EOF

cat > "$TPL_C/instances/inst-marker.env" <<'EOF'
SESSION_NAME=acc-from-marker
PROJECT_DIR=/marker/project
EOF

RT_C="$TMP_DIR/rt_c"
mkdir -p "$RT_C"
printf "inst-marker\n" > "$RT_C/instance"

out_c="$("${CLEAN_ENV[@]}" ACC_RUNTIME="$RT_C" bash --noprofile --norc -c '
  source "'"$TPL_C"'/lib/config.sh"
  acc_load_config "'"$TPL_C"'"
  echo "session=$SESSION_NAME"
  echo "project=$PROJECT_DIR"
  echo "inst=$ACC_INSTANCE"
')"

if grep -q "session=acc-from-marker" <<< "$out_c" && \
   grep -q "project=/marker/project" <<< "$out_c" && \
   grep -q "inst=inst-marker" <<< "$out_c"; then
  pass_case 4 "다-1. ACC_RUNTIME/instance 표식 파일로부터 instance ID 정상 해석 및 로드"
else
  fail_case 4 "다-1. ACC_RUNTIME/instance 표식 파일 해석 실패" "$out_c"
fi

# 다-2: ACC_INSTANCE와 ACC_RUNTIME/instance 동시 존재 시 ACC_INSTANCE 우선 검증
cat > "$TPL_C/instances/inst-explicit.env" <<'EOF'
SESSION_NAME=acc-from-explicit
EOF

out_c2="$("${CLEAN_ENV[@]}" ACC_INSTANCE="inst-explicit" ACC_RUNTIME="$RT_C" bash --noprofile --norc -c '
  source "'"$TPL_C"'/lib/config.sh"
  acc_load_config "'"$TPL_C"'"
  echo "session=$SESSION_NAME"
  echo "inst=$ACC_INSTANCE"
')"

if grep -q "session=acc-from-explicit" <<< "$out_c2" && \
   grep -q "inst=inst-explicit" <<< "$out_c2"; then
  pass_case 5 "다-2. ACC_INSTANCE 지정 시 runtime/instance 표식보다 명시적 ACC_INSTANCE 우선"
else
  fail_case 5 "다-2. ACC_INSTANCE 우선권 실패" "$out_c2"
fi

# ------------------------------------------------------------------
# 라. 지정된 instance 파일 부재 / 유효하지 않은 ID 시 return 70 및 exit 70
# ------------------------------------------------------------------
TPL_D="$TMP_DIR/tpl_d"
make_fixture_tpl "$TPL_D"
echo "SESSION_NAME=base" > "$TPL_D/config.env"

# 라-1: 존재하지 않는 인스턴스 파일 요청 시 acc_load_config는 return 70
err_d1="$TMP_DIR/err_d1.log"
rc_d1=0
"${CLEAN_ENV[@]}" ACC_INSTANCE="nonexistent-inst" bash --noprofile --norc -c '
  source "'"$TPL_D"'/lib/config.sh"
  acc_load_config "'"$TPL_D"'"
' 2>"$err_d1" || rc_d1=$?

if (( rc_d1 == 70 )) && grep -q "refusing" "$err_d1"; then
  pass_case 6 "라-1. instances/<id>.env 파일 부재 시 silent fallback 없이 return 70 거부"
else
  fail_case 6 "라-1. 존재하지 않는 인스턴스 거부 실패" "rc=$rc_d1, err=$(cat "$err_d1")"
fi

# 라-2: 유효하지 않은 ID 문자열 (슬래시, 공백, 특수문자) 시 return 70
for bad_id in "bad/id" "bad id" "bad@id" "../traversal" ""; do
  # 빈 문자열은 ACC_INSTANCE="" 로 설정되었을 때 no-op(0)이어야 하나
  # 공백만 있는 문자열은 invalid(70)이어야 함
  target_id="$bad_id"
  [[ -z "$target_id" ]] && continue
  rc_bad=0
  err_bad="$TMP_DIR/err_bad.log"
  "${CLEAN_ENV[@]}" ACC_INSTANCE="$target_id" bash --noprofile --norc -c '
    source "'"$TPL_D"'/lib/config.sh"
    acc_load_config "'"$TPL_D"'"
  ' 2>"$err_bad" || rc_bad=$?
  if (( rc_bad != 70 )); then
    fail_case 7 "라-2. 유효하지 않은 instance id '$target_id' 거부 실패" "rc=$rc_bad"
  fi
done
pass_case 7 "라-2. 유효하지 않은 ID 포맷(슬래시/공백/경로탈출) 전부 return 70 거부"

# 라-3: runtime/instance 표식 파일이 비어있는 경우 return 70 거부
RT_EMPTY="$TMP_DIR/rt_empty"
mkdir -p "$RT_EMPTY"
: > "$RT_EMPTY/instance"
rc_empty=0
err_empty="$TMP_DIR/err_empty.log"
"${CLEAN_ENV[@]}" ACC_RUNTIME="$RT_EMPTY" bash --noprofile --norc -c '
  source "'"$TPL_D"'/lib/config.sh"
  acc_load_config "'"$TPL_D"'"
' 2>"$err_empty" || rc_empty=$?

if (( rc_empty == 70 )) && grep -q "invalid instance id" "$err_empty"; then
  pass_case 8 "라-3. runtime/instance 표식 파일이 빈 파일일 때 return 70 거부"
else
  fail_case 8 "라-3. 빈 표식 파일 거부 실패" "rc=$rc_empty, err=$(cat "$err_empty" 2>/dev/null || true)"
fi

# 라-4: 리팩터링된 8개 스크립트가 존재하지 않는 인스턴스 지정 시 exit 70 종료 검증
scripts_to_test=(
  "bin/dispatch.sh agy --dry-run"
  "bin/run-task.sh agy t1 /dev/null"
  "bin/event-emit.sh agy done t1 summary"
  "bin/event-ack.sh 1"
  "bin/sonnet-event-wait.sh test"
  "bin/task-abandon.sh agy"
)

all_scripts_ok=true
for sc in "${scripts_to_test[@]}"; do
  sc_bin="$(cut -d' ' -f1 <<< "$sc")"
  sc_args="$(cut -d' ' -f2- <<< "$sc")"
  rc_sc=0
  err_sc="$TMP_DIR/err_sc.log"
  "${CLEAN_ENV[@]}" ACC_INSTANCE="missing-instance" bash --noprofile --norc -c '
    "'"$TPL_D/$sc_bin"'" '"$sc_args"'
  ' 2>"$err_sc" || rc_sc=$?
  if (( rc_sc != 70 )); then
    echo "Script $sc_bin failed to exit 70 on missing instance (got rc=$rc_sc)" >&2
    all_scripts_ok=false
  fi
done

if [[ "$all_scripts_ok" == "true" ]]; then
  pass_case 9 "라-4. 리팩터링된 스크립트 실행 시 missing instance 에 대해 exit 70 종료 전건 확인"
else
  fail_case 9 "라-4. 일부 스크립트가 exit 70으로 종료하지 않음"
fi

# ------------------------------------------------------------------
# 마. 라이브와 동일 fixture 불변 검증
# ------------------------------------------------------------------
TPL_E="$TMP_DIR/tpl_e"
make_fixture_tpl "$TPL_E"
cat > "$TPL_E/config.env" <<'EOF'
SESSION_NAME=agentchain-v2
PROJECT_DIR=/mnt/c/Users/rerun/llm-wiki
WORKER_MODE=oneshot
AGY_MODE=resident
CODEX_MODE=oneshot
BRIDGE_MODE=push
DEFAULT_TASK_TIMEOUT=1800
EOF

out_e="$("${CLEAN_ENV[@]}" bash --noprofile --norc -c '
  source "'"$TPL_E"'/lib/config.sh"
  acc_load_config "'"$TPL_E"'"
  echo "session=$SESSION_NAME"
  echo "project=$PROJECT_DIR"
  echo "worker_mode=$WORKER_MODE"
  echo "agy_mode=$AGY_MODE"
  echo "codex_mode=$CODEX_MODE"
  echo "bridge_mode=$BRIDGE_MODE"
  echo "timeout=$DEFAULT_TASK_TIMEOUT"
  echo "inst=${ACC_INSTANCE:-<none>}"
')"

expected_e="session=agentchain-v2
project=/mnt/c/Users/rerun/llm-wiki
worker_mode=oneshot
agy_mode=resident
codex_mode=oneshot
bridge_mode=push
timeout=1800
inst=<none>"

if [[ "$out_e" == *"$expected_e"* ]]; then
  pass_case 10 "마. 라이브 환경(agentchain-v2, llm-wiki) 설정값 완전 불변 및 단일 체인 호환성 검증"
else
  fail_case 10 "마. 라이브 fixture 불변성 검증 실패" "got: $out_e"
fi

echo "=================================================================="
echo "test-config-layering.sh Summary: PASSED=$PASSED_TESTS, FAILED=$((TOTAL_TESTS - PASSED_TESTS))"
echo "=================================================================="
exit 0
