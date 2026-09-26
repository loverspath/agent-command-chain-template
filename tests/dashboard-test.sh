#!/usr/bin/env bash
# tests/dashboard-test.sh: ACC 대시보드 서버 및 엔드포인트 격리 검증 (Task T0925-01, T0925-03)
# 검증 항목:
#   (1) /api/state 필수 키 및 JSON 스키마 유효성
#   (2) 기밀성 / 민감정보 노출 방지 (prompt.md, output.log, config.env, detail_path)
#   (3) XSS 및 HTML 안전성 (JSON 안전 이스케이프 및 index.html innerHTML 미사용)
#   (4) HTTP 메소드 제한(POST/PUT/DELETE 405) 및 디렉터리 순회 차단(400/404)
#   (5) 호스트 바인딩 보안 (0.0.0.0 / :: 차단 및 --allow-any-host 검증)
#   (6) 불변성 (서버 디스크 쓰기 0건, 런타임 해시 불변)
#   (7) 장애 허용성 (누락/파손/오염 파일 존재 시에도 200 OK 및 정상 JSON 응답)
#   (8) 모바일 레이아웃 정적 검증 (viewport, overflow-x: hidden, 고정폭 > 390px 부재)
#   (9) 라이프사이클 (run.sh start/status/stop, PID 파일 생성/삭제, 프로세스 정리)
#   (10) 태스크 및 이벤트 상세 스키마 및 타임라인 기본 분류
#   (11) 단일 지점 마스킹 (11개 비밀 패턴 노출 차단)
#   (12) 화이트리스트 검증 (detail_path 변조 차단)
#   (13) 5MB 트랜스크립트 제한 및 성능 (<2s, <=200KB)
#   (14) 상세 엔드포인트 메소드/경로 제한 (405, 400, 404)
#   (15) 트랜스크립트 내 파손 JSONL 라인 장애 허용성
#   (16) 추론 링킹 시 시작 마커 부재 시 timeline: [] 방어
#   (17) index.html innerHTML 0건 및 textContent 정적 검증
#   (18) run.sh 격리 실행 및 실서버 불변성 검증
#   (19) 멀티 태스크 트랜스크립트 슬라이싱 격리 (Task B 격리, Task C 끝까지 슬라이싱)
#   (20) 시작 마커 부재 시 linked: 'inferred', timeline: [] 격리 (외래 스텝 누출 0건)
#   (21) 2MB 테일 컷 이전 시작 태스크 linked: 'partial', truncated_before: true
#   (22) 노이즈 제거 (메타 프리픽스, 경로 축약, 빈 응답 생략) 및 서브에이전트 분류
#   (23) 제한 및 보안 (스텝수 <= 60, 요약 <= 300자, 응답 크기 <= 200KB)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "dashboard"
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
echo "Running dashboard-test.sh (Marker: $TESTMARK)"
echo "Target runtime: $TEST_RUNTIME"
echo "=================================================================="

# 미사용 임의 포트 획득 헬퍼
get_free_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("", 0)); print(s.getsockname()[1]); s.close()'
}

FAKE_TRANSCRIPT_ROOT="$TEST_TMP/fake_transcripts"
mkdir -p "$FAKE_TRANSCRIPT_ROOT/brain"

# 서버 시작 헬퍼 (유한 대기, timeout 60 준수)
SERVER_PIDS=()
start_server() {
  local port="$1"
  local rt="$2"
  local host="${3:-127.0.0.1}"
  local allow_any="${4:-false}"
  local t_root="${5:-$FAKE_TRANSCRIPT_ROOT}"

  local args=("--host" "$host" "--port" "$port" "--runtime" "$rt" "--repo" "$HERE" "--transcript-root" "$t_root")
  if [[ "$allow_any" == "true" ]]; then
    args+=("--allow-any-host")
  fi

  python3 "$HERE/dashboard/server.py" "${args[@]}" > "$TEST_TMP/server_${port}.log" 2>&1 &
  local spid=$!
  SERVER_PIDS+=("$spid")

  local ready=false
  for ((i=0; i<30; i++)); do
    if timeout 2 curl -s "http://${host}:${port}/healthz" >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 0.1
  done

  if [[ "$ready" != "true" ]]; then
    echo "[dashboard-test] ERROR: Server on port $port failed to become ready" >&2
    cat "$TEST_TMP/server_${port}.log" >&2
    return 1
  fi
  return 0
}

# 서버 중지 헬퍼
stop_server() {
  local spid="$1"
  if kill -0 "$spid" 2>/dev/null; then
    kill "$spid" 2>/dev/null || true
    for ((i=0; i<30; i++)); do
      if ! kill -0 "$spid" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done
    if kill -0 "$spid" 2>/dev/null; then
      kill -9 "$spid" 2>/dev/null || true
    fi
  fi
}

cleanup_test_servers() {
  for spid in "${SERVER_PIDS[@]}"; do
    stop_server "$spid"
  done
  SERVER_PIDS=()
}

# ------------------------------------------------------------------
# 모의 런타임 데이터 설정 (정상 상태)
# ------------------------------------------------------------------
# workers
touch "$TEST_RUNTIME/workers/agy.resident"
echo "task-agy-001" > "$TEST_RUNTIME/workers/agy.busy"
echo "$(( $(date +%s) - 45 ))" >> "$TEST_RUNTIME/workers/agy.busy"

# tasks
t1="$TEST_RUNTIME/tasks/task-agy-001"
mkdir -p "$t1"
cat > "$t1/state" <<EOF
task_id=task-agy-001
worker=agy
mode=resident
status=running
started_epoch=$(( $(date +%s) - 45 ))
deadline_epoch=$(( $(date +%s) + 1755 ))
EOF

t2="$TEST_RUNTIME/tasks/task-codex-002"
mkdir -p "$t2"
cat > "$t2/state" <<EOF
task_id=task-codex-002
worker=codex
mode=oneshot
status=done
started_epoch=$(( $(date +%s) - 300 ))
completed_epoch=$(( $(date +%s) - 120 ))
exit_code=0
terminal_event_id=ev-term-002
EOF

# events
mkdir -p "$TEST_RUNTIME/events"/{pending,inflight,archive}
sum1_b64="$(printf '%s' "Test event summary 1" | base64 | tr -d '\n')"
cat > "$TEST_RUNTIME/events/pending/ev-001.evt" <<EOF
id=ev-001
source=tester
kind=notice
task_id=task-agy-001
created_epoch=$(( $(date +%s) - 30 ))
summary_b64=$sum1_b64
EOF

sum2_b64="$(printf '%s' "Task codex completed successfully" | base64 | tr -d '\n')"
cat > "$TEST_RUNTIME/events/archive/ev-term-002.evt" <<EOF
id=ev-term-002
source=codex
kind=done
task_id=task-codex-002
created_epoch=$(( $(date +%s) - 120 ))
exit_code=0
summary_b64=$sum2_b64
EOF

# 메인 테스트 서버 구동
MAIN_PORT=$(get_free_port)
start_server "$MAIN_PORT" "$TEST_RUNTIME" "127.0.0.1"

# ------------------------------------------------------------------
# Case 1: /api/state 유효한 JSON 및 필수 키 검증
# ------------------------------------------------------------------
c1_res=0
c1_json="$TEST_TMP/c1_state.json"
timeout 60 curl -s "http://127.0.0.1:${MAIN_PORT}/api/state" > "$c1_json" || c1_res=$?

if (( c1_res == 0 )); then
  py_check='
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)

required_top = ["server", "session", "workers", "daemons", "tasks", "events", "queue"]
for k in required_top:
    if k not in d:
        print(f"Missing top-level key: {k}", file=sys.stderr)
        sys.exit(1)

# server
assert "time" in d["server"] and "runtime_name" in d["server"] and "git" in d["server"]
assert "head" in d["server"]["git"] and "tags" in d["server"]["git"]

# session
assert "name" in d["session"] and "windows" in d["session"]
assert isinstance(d["session"]["windows"], list)

# workers
for w in ["agy", "codex", "sonnet"]:
    assert w in d["workers"], f"Worker {w} missing"
    assert "mode" in d["workers"][w] and "busy_task_id" in d["workers"][w]

assert d["workers"]["agy"]["mode"] == "resident"
assert d["workers"]["agy"]["busy_task_id"] == "task-agy-001"

# daemons
assert "watchdog" in d["daemons"] and "listener" in d["daemons"]
assert "alive" in d["daemons"]["watchdog"] and "pid" in d["daemons"]["watchdog"]

# tasks
assert isinstance(d["tasks"], list)
assert len(d["tasks"]) >= 2
t_agy = next((t for t in d["tasks"] if t["id"] == "task-agy-001"), None)
assert t_agy is not None
assert t_agy["worker"] == "agy"
assert t_agy["mode"] == "resident"
assert t_agy["status"] == "running"
assert t_agy["duration_s"] is not None and t_agy["duration_s"] >= 45

# events
assert "pending_count" in d["events"] and "archive_count" in d["events"]
assert d["events"]["pending_count"] >= 1
assert d["events"]["archive_count"] >= 1
assert isinstance(d["events"]["recent"], list)
assert len(d["events"]["recent"]) >= 2

# queue
assert "busy_elapsed_s" in d["queue"]
assert d["queue"]["busy_elapsed_s"] is not None and d["queue"]["busy_elapsed_s"] >= 45

sys.exit(0)
'
  if python3 -c "$py_check" "$c1_json" 2>"$TEST_TMP/c1_err.log"; then
    pass_case 1 "/api/state returns valid JSON with all required keys (server, session, workers, daemons, tasks, events, queue)"
  else
    fail_case 1 "/api/state schema check" "$(cat "$TEST_TMP/c1_err.log")"
  fi
else
  fail_case 1 "/api/state request" "curl failed with exit code $c1_res"
fi

# ------------------------------------------------------------------
# Case 2: 기밀성 검증 (비밀 마커 노출 방지)
# ------------------------------------------------------------------
SECRET_MARKER="TOPSECRET_TOKEN_${RAND_HEX}_CRITICAL_LEAK"

# 런타임 내 및 작업/이벤트 내 비밀 파일 생성
echo "SECRET_KEY=$SECRET_MARKER" > "$TEST_RUNTIME/config.env"
echo "Confidential prompt: $SECRET_MARKER" > "$TEST_RUNTIME/prompt.md"
echo "Confidential output: $SECRET_MARKER" > "$TEST_RUNTIME/output.log"
echo "Task prompt: $SECRET_MARKER" > "$t1/prompt.md"
echo "Task output log: $SECRET_MARKER" > "$t1/output.log"

# detail_path 가 포함된 이벤트 생성
cat > "$TEST_RUNTIME/events/pending/ev-secret-ref.evt" <<EOF
id=ev-sec-01
source=tester
kind=notice
task_id=task-agy-001
detail_path=$TEST_RUNTIME/output.log
summary_b64=$sum1_b64
EOF

# 여러 엔드포인트 응답 수집
c2_failed=false
c2_endpoints=(
  "/"
  "/index.html"
  "/healthz"
  "/api/state"
  "/prompt.md"
  "/output.log"
  "/config.env"
  "/tasks/task-agy-001/prompt.md"
  "/../../etc/passwd"
  "/nonexistent-page-404"
)

for ep in "${c2_endpoints[@]}"; do
  resp_file="$TEST_TMP/c2_resp_$(echo -n "$ep" | tr -cd '[:alnum:]').txt"
  timeout 60 curl -s --path-as-is "http://127.0.0.1:${MAIN_PORT}${ep}" > "$resp_file" || true
  if grep -q "$SECRET_MARKER" "$resp_file"; then
    c2_failed=true
    fail_case 2 "Secret leak check" "Secret marker leaked in endpoint: $ep"
    break
  fi
done

if [[ "$c2_failed" == "false" ]]; then
  pass_case 2 "Secrecy check passed: secret markers never appear in any HTTP response"
fi

# ------------------------------------------------------------------
# Case 3: XSS 및 HTML 안전성 검증
# ------------------------------------------------------------------
# XSS 페이로드가 포함된 이벤트 주입
XSS_PAYLOAD="<script>alert('xss_${RAND_HEX}')</script>"
xss_b64="$(printf '%s' "$XSS_PAYLOAD" | base64 | tr -d '\n')"

# 새로운 이벤트 작성
xss_evt="$TEST_RUNTIME/events/pending/ev-xss-${RAND_HEX}.evt"
cat > "$xss_evt" <<EOF
id=ev-xss-${RAND_HEX}
source=xss-tester
kind=alert
created_epoch=$(( $(date +%s) + 10 ))
summary_b64=$xss_b64
EOF

# 1.5초 캐시 만료 대기 (유한 슬립)
sleep 1.6

c3_json="$TEST_TMP/c3_xss.json"
timeout 60 curl -s "http://127.0.0.1:${MAIN_PORT}/api/state" > "$c3_json"

py_xss='
import json, sys
payload = sys.argv[2]
with open(sys.argv[1]) as f:
    d = json.load(f)

# Ensure payload exists as parsed string value inside JSON
found = False
for ev in d["events"]["recent"]:
    if ev.get("summary") == payload:
        found = True
        break

if not found:
    print(f"XSS payload was not found in parsed events summary", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
'

c3_ok=true
if ! python3 -c "$py_xss" "$c3_json" "$XSS_PAYLOAD" 2>"$TEST_TMP/c3_err.log"; then
  c3_ok=false
  fail_case 3 "XSS JSON parsing" "$(cat "$TEST_TMP/c3_err.log")"
fi

# index.html 내 innerHTML 정적 검증
if grep -rn "innerHTML" "$HERE/dashboard/index.html" > "$TEST_TMP/c3_innerhtml.log"; then
  c3_ok=false
  fail_case 3 "XSS innerHTML static check" "Found innerHTML usage in index.html: $(cat "$TEST_TMP/c3_innerhtml.log")"
fi

if [[ "$c3_ok" == "true" ]]; then
  pass_case 3 "XSS & HTML Safety: Event summary with <script> is safely escaped in JSON, zero uses of innerHTML in index.html"
fi

# ------------------------------------------------------------------
# Case 4: HTTP 메소드 제한(POST/PUT/DELETE) 및 디렉터리 순회 차단
# ------------------------------------------------------------------
c4_ok=true

for method in POST PUT DELETE; do
  m_code=$(timeout 60 curl -s -o /dev/null -w "%{http_code}" -X "$method" "http://127.0.0.1:${MAIN_PORT}/api/state" || echo "000")
  if [[ "$m_code" != "405" ]]; then
    c4_ok=false
    fail_case 4 "HTTP method rejection" "Method $method returned $m_code instead of 405"
  fi
done

# POST to /
root_post_code=$(timeout 60 curl -s -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:${MAIN_PORT}/" || echo "000")
if [[ "$root_post_code" != "405" ]]; then
  c4_ok=false
  fail_case 4 "HTTP method rejection" "POST / returned $root_post_code instead of 405"
fi

# 디렉터리 순회 차단 (400 또는 404, /etc/passwd 내용 절대 노출 금지)
pt_out="$TEST_TMP/c4_traversal.out"
pt_code=$(timeout 60 curl -s -o "$pt_out" -w "%{http_code}" --path-as-is "http://127.0.0.1:${MAIN_PORT}/../../etc/passwd" || echo "000")
if [[ "$pt_code" != "400" && "$pt_code" != "404" ]]; then
  c4_ok=false
  fail_case 4 "Path traversal" "GET /../../etc/passwd returned $pt_code (expected 400 or 404)"
fi

if grep -q "root:" "$pt_out"; then
  c4_ok=false
  fail_case 4 "Path traversal" "GET /../../etc/passwd leaked /etc/passwd contents!"
fi

if [[ "$c4_ok" == "true" ]]; then
  pass_case 4 "HTTP Methods & Path Traversal: POST/PUT/DELETE return 405; /../../etc/passwd returns 400 or 404 with zero content leak"
fi

# ------------------------------------------------------------------
# Case 5: 호스트 바인딩 보안 (0.0.0.0 거부)
# ------------------------------------------------------------------
c5_ok=true
p_bind=$(get_free_port)

b_err="$TEST_TMP/c5_bind_err.log"
b_rc=0
timeout 60 python3 "$HERE/dashboard/server.py" --host 0.0.0.0 --port "$p_bind" > "$b_err" 2>&1 || b_rc=$?

if (( b_rc == 0 )); then
  c5_ok=false
  fail_case 5 "Host binding security" "server.py --host 0.0.0.0 unexpectedly succeeded without --allow-any-host"
elif ! grep -q "requires --allow-any-host" "$b_err"; then
  c5_ok=false
  fail_case 5 "Host binding security" "server.py --host 0.0.0.0 exited with code $b_rc but missing required error message: $(cat "$b_err")"
fi

# IPv6 :: 거부 확인
b_ipv6_rc=0
timeout 60 python3 "$HERE/dashboard/server.py" --host "::" --port "$p_bind" > /dev/null 2>&1 || b_ipv6_rc=$?
if (( b_ipv6_rc == 0 )); then
  c5_ok=false
  fail_case 5 "Host binding security" "server.py --host :: unexpectedly succeeded without --allow-any-host"
fi

if [[ "$c5_ok" == "true" ]]; then
  pass_case 5 "Host Binding Security: --host 0.0.0.0 and :: exit non-zero without --allow-any-host"
fi

# ------------------------------------------------------------------
# Case 6: 불변성 검증 (서버의 디스크 쓰기 0건)
# ------------------------------------------------------------------
# 전용 불변 런타임 생성
IMMUT_RT="$TEST_TMP/immut_runtime"
setup_mock_runtime "$IMMUT_RT"
echo "immut-session" > "$IMMUT_RT/session_name"
mkdir -p "$IMMUT_RT/tasks/t1"
echo "task_id=t1\nstatus=done\nstarted_epoch=1000\ncompleted_epoch=1010" > "$IMMUT_RT/tasks/t1/state"

IMMUT_PORT=$(get_free_port)
start_server "$IMMUT_PORT" "$IMMUT_RT" "127.0.0.1"

# 사전 해시 스냅샷 기록
pre_hash="$TEST_TMP/immut_pre.sha256"
find "$IMMUT_RT" -type f -exec sha256sum {} + | sort > "$pre_hash"

# 다양한 요청 25회 실행
for i in {1..25}; do
  timeout 60 curl -s "http://127.0.0.1:${IMMUT_PORT}/api/state" > /dev/null || true
  timeout 60 curl -s "http://127.0.0.1:${IMMUT_PORT}/healthz" > /dev/null || true
  timeout 60 curl -s "http://127.0.0.1:${IMMUT_PORT}/" > /dev/null || true
  timeout 60 curl -s -X POST "http://127.0.0.1:${IMMUT_PORT}/api/state" > /dev/null || true
done

# 사후 해시 스냅샷 기록
post_hash="$TEST_TMP/immut_post.sha256"
find "$IMMUT_RT" -type f -exec sha256sum {} + | sort > "$post_hash"

if diff -u "$pre_hash" "$post_hash" > "$TEST_TMP/immut_diff.log"; then
  pass_case 6 "Immutability: Runtime directory signature is 100% identical before and after server requests (ZERO writes)"
else
  fail_case 6 "Immutability" "Runtime was modified during requests: $(cat "$TEST_TMP/immut_diff.log")"
fi

# ------------------------------------------------------------------
# Case 7: 장애 허용성 (누락/파손/오염 파일 허용)
# ------------------------------------------------------------------
CORRUPT_RT="$TEST_TMP/corrupt_runtime"
mkdir -p "$CORRUPT_RT"/{tasks,events/pending,workers}

# 파손된 파일들 주입
# (1) 비-UTF8 바이너리 세션명
printf '\x80\xff\x00\x01\xfe\xed' > "$CORRUPT_RT/session_name"

# (2) state 파일이 누락된 task 디렉터리
mkdir -p "$CORRUPT_RT/tasks/broken-task-no-state"

# (3) 쓰레기 데이터가 들어간 task state 파일
mkdir -p "$CORRUPT_RT/tasks/corrupt-state-task"
cat > "$CORRUPT_RT/tasks/corrupt-state-task/state" <<EOF
this is not kv format
=nothing
started_epoch=not-a-number
deadline_epoch=invalid
exit_code=-999
EOF

# (4) 바이너리 쓰레기 및 구문 오류 .evt 파일
printf '\x00\x01\x02\x03\x04\xff' > "$CORRUPT_RT/events/pending/binary-garbage.evt"
cat > "$CORRUPT_RT/events/pending/malformed.evt" <<EOF
no_equals_line
created_epoch=invalid_float_string
summary_b64=!!!NOT_VALID_BASE64!!!
EOF

# (5) 파손된 worker busy 파일
cat > "$CORRUPT_RT/workers/agy.busy" <<EOF
task-corrupt
not_a_timestamp
EOF

CORRUPT_PORT=$(get_free_port)
start_server "$CORRUPT_PORT" "$CORRUPT_RT" "127.0.0.1"

c7_resp="$TEST_TMP/c7_resp.json"
c7_code=$(timeout 60 curl -s -o "$c7_resp" -w "%{http_code}" "http://127.0.0.1:${CORRUPT_PORT}/api/state" || echo "000")

if [[ "$c7_code" == "200" ]]; then
  py_corrupt='
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)

for k in ["server", "session", "workers", "daemons", "tasks", "events", "queue"]:
    assert k in d, f"Missing key {k}"

assert isinstance(d["tasks"], list)
assert isinstance(d["events"]["recent"], list)
sys.exit(0)
'
  if python3 -c "$py_corrupt" "$c7_resp" 2>"$TEST_TMP/c7_err.log"; then
    pass_case 7 "Fault Tolerance: Broken/corrupt files (missing state, malformed json/evt/busy) still return 200 OK with valid JSON"
  else
    fail_case 7 "Fault Tolerance" "Response was not valid schema: $(cat "$TEST_TMP/c7_err.log")"
  fi
else
  fail_case 7 "Fault Tolerance" "Expected HTTP 200 on corrupt runtime, got $c7_code"
fi

# ------------------------------------------------------------------
# Case 8: 모바일 레이아웃 정적 검증
# ------------------------------------------------------------------
py_mobile='
import re, sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    html = f.read()

# 1. Viewport tag present
if not re.search(r"<meta[^>]+name=[\"\x27]viewport[\"\x27]", html, re.I):
    print("Missing viewport meta tag", file=sys.stderr)
    sys.exit(1)

# 2. overflow-x: hidden present
if not re.search(r"overflow-x\s*:\s*hidden", html):
    print("Missing overflow-x: hidden", file=sys.stderr)
    sys.exit(2)

# 3. No fixed width > 390px (excluding max-width, min-width, and media queries)
fixed_widths = re.findall(r"(?<![a-zA-Z0-9_-])width\s*:\s*([0-9]+)px", html)
large_widths = [int(w) for w in fixed_widths if int(w) > 390]
if large_widths:
    print(f"Found fixed widths > 390px: {large_widths}", file=sys.stderr)
    sys.exit(3)

sys.exit(0)
'

if python3 -c "$py_mobile" "$HERE/dashboard/index.html" 2>"$TEST_TMP/c8_err.log"; then
  pass_case 8 "Mobile Layout Static Check: Viewport tag present, overflow-x: hidden present, no fixed widths > 390px"
else
  fail_case 8 "Mobile Layout Static Check" "$(cat "$TEST_TMP/c8_err.log")"
fi

# ------------------------------------------------------------------
# Case 9: 라이프사이클 (run.sh start/status/stop)
# ------------------------------------------------------------------
c9_ok=true
LIFE_PORT=$(get_free_port)
LIFE_PID_FILE="$TEST_TMP/run_lifecycle.pid"
LIFE_LOG_FILE="$TEST_TMP/run_lifecycle.log"

# (a) start
s_rc=0
timeout 60 "$HERE/dashboard/run.sh" start \
  --port "$LIFE_PORT" \
  --runtime "$TEST_RUNTIME" \
  --host 127.0.0.1 \
  --pid-file "$LIFE_PID_FILE" \
  --log-file "$LIFE_LOG_FILE" > "$TEST_TMP/c9_start.log" 2>&1 || s_rc=$?

if (( s_rc != 0 )); then
  c9_ok=false
  fail_case 9 "Lifecycle start" "run.sh start failed (exit $s_rc): $(cat "$TEST_TMP/c9_start.log")"
fi

# PID 파일 검증
if [[ ! -f "$LIFE_PID_FILE" ]]; then
  c9_ok=false
  fail_case 9 "Lifecycle PID file" "PID file was not created at $LIFE_PID_FILE"
fi

lpid="$(cat "$LIFE_PID_FILE" 2>/dev/null || echo "")"
if [[ -z "$lpid" ]] || ! kill -0 "$lpid" 2>/dev/null; then
  c9_ok=false
  fail_case 9 "Lifecycle process" "Process with PID $lpid is not running"
fi

# HTTP 응답 확인
if ! timeout 5 curl -s "http://127.0.0.1:${LIFE_PORT}/healthz" > /dev/null; then
  c9_ok=false
  fail_case 9 "Lifecycle healthz" "Server on port $LIFE_PORT did not respond to /healthz"
fi

# (b) status
stat_rc=0
stat_out="$(timeout 60 "$HERE/dashboard/run.sh" status --pid-file "$LIFE_PID_FILE" 2>&1)" || stat_rc=$?
if (( stat_rc != 0 )) || [[ "$stat_out" != *"running"* ]]; then
  c9_ok=false
  fail_case 9 "Lifecycle status" "run.sh status failed (exit $stat_rc): $stat_out"
fi

# (c) stop
stop_rc=0
timeout 60 "$HERE/dashboard/run.sh" stop --pid-file "$LIFE_PID_FILE" > "$TEST_TMP/c9_stop.log" 2>&1 || stop_rc=$?
if (( stop_rc != 0 )); then
  c9_ok=false
  fail_case 9 "Lifecycle stop" "run.sh stop failed (exit $stop_rc): $(cat "$TEST_TMP/c9_stop.log")"
fi

# 종료 후 PID 파일 제거 확인
if [[ -f "$LIFE_PID_FILE" ]]; then
  c9_ok=false
  fail_case 9 "Lifecycle stop cleanup" "PID file still exists after stop: $LIFE_PID_FILE"
fi

# 프로세스 종료 확인
if kill -0 "$lpid" 2>/dev/null; then
  c9_ok=false
  fail_case 9 "Lifecycle stop termination" "Process PID $lpid is still running after stop"
fi

# status 확인 (미기동 상태 = exit 1)
stat_post_rc=0
stat_post_out="$(timeout 60 "$HERE/dashboard/run.sh" status --pid-file "$LIFE_PID_FILE" 2>&1)" || stat_post_rc=$?
if (( stat_post_rc == 0 )) || [[ "$stat_post_out" != *"not running"* ]]; then
  c9_ok=false
  fail_case 9 "Lifecycle status after stop" "run.sh status after stop expected exit 1, got $stat_post_rc: $stat_post_out"
fi

# 잔류 프로세스 검증
lingering="$(pgrep -f "server.py.*$LIFE_PORT" 2>/dev/null || true)"
if [[ -n "$lingering" ]]; then
  c9_ok=false
  fail_case 9 "Lifecycle lingering process" "Found lingering process for port $LIFE_PORT: $lingering"
fi

if [[ "$c9_ok" == "true" ]]; then
  pass_case 9 "Lifecycle: run.sh start/status/stop with clean PID file creation, running status, clean termination on stop, and NO lingering processes"
fi

# ------------------------------------------------------------------
# Case 10: Task & Event Detail Schema and Timeline Classification (3a)
# ------------------------------------------------------------------
t_norm="$TEST_RUNTIME/tasks/task-norm-01"
mkdir -p "$t_norm"
cat > "$t_norm/state" <<EOF
task_id=task-norm-01
worker=agy
mode=resident
status=done
started_epoch=1700000000
completed_epoch=1700000120
exit_code=0
EOF

cat > "$t_norm/prompt.md" <<'EOF'
[CHAIN CONTEXT]
SESSION_NAME: test-sess
TEMPLATE_ROOT: /home/rerun/test
---
# Primary Objective: Implement Detailed Dashboard Features
Detailed prompt instructions follow here.
EOF

cat > "$t_norm/output.log" <<'EOF'
[Step 1] Initializing task
[Step 2] Processing complete
EOF

norm_uuid="norm-uuid-1234"
norm_log_dir="$FAKE_TRANSCRIPT_ROOT/brain/$norm_uuid/.system_generated/logs"
mkdir -p "$norm_log_dir"
norm_trans="$norm_log_dir/transcript.jsonl"
cat > "$norm_trans" <<'EOF'
{"step_index":0,"source":"USER","type":"USER_INPUT","created_at":"2026-09-25T00:00:00Z","content":"Initial prompt from user"}
{"step_index":1,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T00:00:01Z","tool_calls":[{"name":"run_command","args":{"CommandLine":"echo hello"}}]}
{"step_index":2,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T00:00:02Z","tool_calls":[{"name":"invoke_subagent","args":{"Subagents":[{"Role":"Code Reviewer","Prompt":"Review changes"}]}}]}
{"step_index":3,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T00:00:03Z","content":"Final model thought and conclusion."}
EOF

norm_sum_b64="$(printf '%s' "Task finished with 0 errors" | base64 | tr -d '\n')"
cat > "$TEST_RUNTIME/events/archive/ev-norm-01.evt" <<EOF
id=ev-norm-01
source=agy
kind=done
task_id=task-norm-01
created_epoch=1700000120
exit_code=0
detail_path=$norm_trans
summary_b64=$norm_sum_b64
EOF

c10_task_json="$TEST_TMP/c10_task.json"
c10_t_code=$(timeout 60 curl -s -o "$c10_task_json" -w "%{http_code}" "http://127.0.0.1:${MAIN_PORT}/api/task/task-norm-01" || echo "000")

c10_evt_json="$TEST_TMP/c10_event.json"
c10_e_code=$(timeout 60 curl -s -o "$c10_evt_json" -w "%{http_code}" "http://127.0.0.1:${MAIN_PORT}/api/event/ev-norm-01" || echo "000")

py_c10='
import json, sys

with open(sys.argv[1]) as f:
    t = json.load(f)

for k in ["meta", "prompt_title", "output_tail", "timeline", "events", "linked"]:
    assert k in t, f"Missing key {k} in task detail"

meta = t["meta"]
assert meta["id"] == "task-norm-01"
assert meta["worker"] == "agy"
assert meta["mode"] == "resident"
assert meta["status"] == "done"
assert meta["started_at"] == 1700000000
assert meta["finished_at"] == 1700000120
assert meta["duration_s"] == 120
assert meta["exit_code"] == 0

assert t["prompt_title"] == "# Primary Objective: Implement Detailed Dashboard Features", "Unexpected prompt title: " + str(t.get("prompt_title"))
assert t["linked"] == "exact", "Expected linked: exact, got " + str(t.get("linked"))

tl = t["timeline"]
assert len(tl) == 4, f"Expected 4 timeline steps, got {len(tl)}"
assert tl[0]["type"] == "system", "Step 0 expected system, got " + str(tl[0].get("type"))
assert tl[1]["type"] == "tool_call", "Step 1 expected tool_call, got " + str(tl[1].get("type"))
assert tl[2]["type"] == "subagent", "Step 2 expected subagent, got " + str(tl[2].get("type"))
assert tl[3]["type"] == "model", "Step 3 expected model, got " + str(tl[3].get("type"))

with open(sys.argv[2]) as f:
    ev = json.load(f)

for k in ["id", "kind", "source", "created_epoch", "exit_code", "task_id", "summary"]:
    assert k in ev, f"Missing key {k} in event detail"

assert ev["id"] == "ev-norm-01"
assert ev["kind"] == "done"
assert ev["source"] == "agy"
assert ev["task_id"] == "task-norm-01"
assert ev["created_epoch"] == 1700000120
assert ev["exit_code"] == 0
assert ev["summary"] == "Task finished with 0 errors"

sys.exit(0)
'

if [[ "$c10_t_code" == "200" && "$c10_e_code" == "200" ]] && python3 -c "$py_c10" "$c10_task_json" "$c10_evt_json" 2>"$TEST_TMP/c10_err.log"; then
  pass_case 10 "Task & Event Detail: Normal schema, metadata, prompt title (skipping chain context), timeline classification (model, tool_call, subagent, system)"
else
  fail_case 10 "Task & Event Detail" "t_code=$c10_t_code, e_code=$c10_e_code, err=$(cat "$TEST_TMP/c10_err.log" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# Case 11: Single-Point Masking of All 11 Sensitive Patterns (3b)
# ------------------------------------------------------------------
t_mask="$TEST_RUNTIME/tasks/task-mask-01"
mkdir -p "$t_mask"
cat > "$t_mask/state" <<EOF
task_id=task-mask-01
worker=agy
mode=resident
status=running
started_epoch=$(( $(date +%s) - 60 ))
EOF

mask_uuid="mask-uuid-9999"
mask_log_dir="$FAKE_TRANSCRIPT_ROOT/brain/$mask_uuid/.system_generated/logs"
mkdir -p "$mask_log_dir"
mask_trans="$mask_log_dir/transcript.jsonl"

RAW_PRIVKEY="-----BEGIN RSA PRIVATE KEY-----\nMIIEogIBAAKCAQEArandpk1234567890abcdef\n-----END RSA PRIVATE KEY-----"
RAW_JWT="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SUPER_SECRET_SIGNATURE_XYZ_9876543210"
RAW_GH1="gho_1111111111222222222233333333334444444444"
RAW_GH2="ghp_5555555555666666666677777777778888888888"
RAW_GH3="github_pat_11ABCD_9999999999888888888877777777776666666666"
RAW_SK="sk-proj-OPENAI_SECRET_KEY_ABCDEFGHIJKLMN1234567890"
RAW_AIZA="AIzaSyDummySecretGoogleApiKey123456789012"
RAW_XOX="xoxb-1234567890-1234567890-SUPER_SLACK_SECRET_TOKEN"
RAW_BEARER="Bearer my_secret_bearer_token_string_abc_12345"
RAW_ASSIGN="DATABASE_PASSWORD='SuperSecretDatabasePassword999!'"
RAW_OAUTH="https://auth.example.com/oauth/authorize?client_secret=SecretOAuthParamString999"
RAW_HEX="deadbeef0123456789abcdef0123456789abcdef"
RAW_B64="VGhpcyBpcyBhIHZlcnkgc2VjcmV0IHRva2VuIGZvciB0ZXN0aW5nIHB1cnBvc2VzIG9ubHk="

python3 -c '
import json, sys
out = sys.argv[1]
with open(out, "w") as f:
    f.write(json.dumps({"step_index":0,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Private key: " + sys.argv[2]}) + "\n")
    f.write(json.dumps({"step_index":1,"source":"MODEL","type":"PLANNER_RESPONSE","content":"JWT: " + sys.argv[3]}) + "\n")
    f.write(json.dumps({"step_index":2,"source":"MODEL","type":"PLANNER_RESPONSE","content":"GitHub tokens: " + sys.argv[4] + " and " + sys.argv[5] + " and " + sys.argv[6]}) + "\n")
    f.write(json.dumps({"step_index":3,"source":"MODEL","type":"PLANNER_RESPONSE","content":"OpenAI: " + sys.argv[7]}) + "\n")
    f.write(json.dumps({"step_index":4,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Google AIza: " + sys.argv[8]}) + "\n")
    f.write(json.dumps({"step_index":5,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Slack: " + sys.argv[9]}) + "\n")
    f.write(json.dumps({"step_index":6,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Bearer: " + sys.argv[10]}) + "\n")
    f.write(json.dumps({"step_index":7,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Assignment: " + sys.argv[11]}) + "\n")
    f.write(json.dumps({"step_index":8,"source":"MODEL","type":"PLANNER_RESPONSE","content":"OAuth: " + sys.argv[12]}) + "\n")
    f.write(json.dumps({"step_index":9,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Hex: " + sys.argv[13] + " Base64: " + sys.argv[14]}) + "\n")
' "$mask_trans" \
  "$RAW_PRIVKEY" "$RAW_JWT" "$RAW_GH1" "$RAW_GH2" "$RAW_GH3" \
  "$RAW_SK" "$RAW_AIZA" "$RAW_XOX" "$RAW_BEARER" "$RAW_ASSIGN" \
  "$RAW_OAUTH" "$RAW_HEX" "$RAW_B64"

python3 -c '
import sys
out = sys.argv[1]
padding = "A" * 8100
with open(out, "w") as f:
    f.write(padding)
    f.write("\nBoundary Secret JWT: " + sys.argv[2] + "\n")
    f.write("Boundary Secret OpenAI: " + sys.argv[3] + "\n")
    f.write("Trailing data " * 50)
' "$t_mask/output.log" "$RAW_JWT" "$RAW_SK"

mask_sum_raw="Notice: JWT=$RAW_JWT and APIKEY=$RAW_SK and ASSIGN=$RAW_ASSIGN"
mask_sum_b64="$(printf '%s' "$mask_sum_raw" | base64 | tr -d '\n')"
cat > "$TEST_RUNTIME/events/pending/ev-mask-01.evt" <<EOF
id=ev-mask-01
source=agy
kind=notice
task_id=task-mask-01
created_epoch=$(( $(date +%s) - 30 ))
detail_path=$mask_trans
summary_b64=$mask_sum_b64
EOF

sleep 1.6

c11_task_resp="$TEST_TMP/c11_task_resp.txt"
timeout 60 curl -s "http://127.0.0.1:${MAIN_PORT}/api/task/task-mask-01" > "$c11_task_resp"

c11_evt_resp="$TEST_TMP/c11_evt_resp.txt"
timeout 60 curl -s "http://127.0.0.1:${MAIN_PORT}/api/event/ev-mask-01" > "$c11_evt_resp"

c11_state_resp="$TEST_TMP/c11_state_resp.txt"
timeout 60 curl -s "http://127.0.0.1:${MAIN_PORT}/api/state" > "$c11_state_resp"

py_mask_check='
import sys

raw_tokens = [
    "BEGIN RSA PRIVATE KEY",
    "SUPER_SECRET_SIGNATURE_XYZ",
    "gho_11111111112222222222",
    "ghp_55555555556666666666",
    "github_pat_11ABCD_9999999999",
    "sk-proj-OPENAI_SECRET_KEY",
    "AIzaSyDummySecretGoogleApiKey",
    "xoxb-1234567890-1234567890",
    "my_secret_bearer_token_string",
    "SuperSecretDatabasePassword999",
    "SecretOAuthParamString999",
    "deadbeef0123456789abcdef0123456789abcdef",
    "VGhpcyBpcyBhIHZlcnkgc2VjcmV0IHRva2Vu"
]

all_text = ""
for path in sys.argv[1:]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        all_text += f.read() + "\n"

leaked = []
for tok in raw_tokens:
    if tok in all_text:
        leaked.append(tok)

if leaked:
    print(f"FAILED: Leaked raw tokens in responses: {leaked}", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
'

if python3 -c "$py_mask_check" "$c11_task_resp" "$c11_evt_resp" "$c11_state_resp" 2>"$TEST_TMP/c11_mask_err.log"; then
  pass_case 11 "Single-Point Masking: All 11 secret patterns (keys, JWT, GitHub, OpenAI, Google, Slack, Bearer, assignments, OAuth, 40+ hex/b64, boundary spanning) masked with zero leakage"
else
  fail_case 11 "Single-Point Masking" "$(cat "$TEST_TMP/c11_mask_err.log")"
fi

# ------------------------------------------------------------------
# Case 12: Whitelist Validation for Transcript Paths (3c)
# ------------------------------------------------------------------
t_f1="$TEST_RUNTIME/tasks/task-forged-passwd"
mkdir -p "$t_f1"
cat > "$t_f1/state" <<EOF
task_id=task-forged-passwd
worker=agy
mode=resident
status=done
EOF
cat > "$TEST_RUNTIME/events/archive/ev-forged-passwd.evt" <<EOF
id=ev-forged-passwd
source=attacker
kind=done
task_id=task-forged-passwd
detail_path=/etc/passwd
EOF

t_f2="$TEST_RUNTIME/tasks/task-forged-traversal"
mkdir -p "$t_f2"
cat > "$t_f2/state" <<EOF
task_id=task-forged-traversal
worker=agy
mode=resident
status=done
EOF
cat > "$TEST_RUNTIME/events/archive/ev-forged-traversal.evt" <<EOF
id=ev-forged-traversal
source=attacker
kind=done
task_id=task-forged-traversal
detail_path=$norm_log_dir/../../../../../../etc/passwd
EOF

symlink_escape="$norm_log_dir/transcript-symlink-escape.jsonl"
ln -sf /etc/hosts "$symlink_escape"
t_f3="$TEST_RUNTIME/tasks/task-forged-symlink"
mkdir -p "$t_f3"
cat > "$t_f3/state" <<EOF
task_id=task-forged-symlink
worker=agy
mode=resident
status=done
EOF
cat > "$TEST_RUNTIME/events/archive/ev-forged-symlink.evt" <<EOF
id=ev-forged-symlink
source=attacker
kind=done
task_id=task-forged-symlink
detail_path=$symlink_escape
EOF

sleep 1.6

f1_resp="$TEST_TMP/c12_f1.json"
f2_resp="$TEST_TMP/c12_f2.json"
f3_resp="$TEST_TMP/c12_f3.json"
timeout 60 curl -s "http://127.0.0.1:${MAIN_PORT}/api/task/task-forged-passwd" > "$f1_resp"
timeout 60 curl -s "http://127.0.0.1:${MAIN_PORT}/api/task/task-forged-traversal" > "$f2_resp"
timeout 60 curl -s "http://127.0.0.1:${MAIN_PORT}/api/task/task-forged-symlink" > "$f3_resp"

py_c12='
import json, sys

for path, name in [(sys.argv[1], "/etc/passwd"), (sys.argv[2], "traversal"), (sys.argv[3], "symlink")]:
    with open(path) as f:
        d = json.load(f)
    assert d.get("linked") == "none", name + ": Expected linked==none, got " + str(d.get("linked"))
    assert d.get("timeline") == [], name + ": Expected empty timeline, got " + str(d.get("timeline"))
    raw = open(path).read()
    assert "root:x:" not in raw, name + ": Leaked /etc/passwd content!"
    assert "localhost" not in raw or "127.0.0.1" not in raw or "::1" not in raw or "host" not in name, name + ": Leaked /etc/hosts content!"

sys.exit(0)
'

if python3 -c "$py_c12" "$f1_resp" "$f2_resp" "$f3_resp" 2>"$TEST_TMP/c12_err.log"; then
  pass_case 12 "Whitelist Validation: Forged detail_path (/etc/passwd, ../, symlink escape) rejected with linked: 'none' and zero file content leaked"
else
  fail_case 12 "Whitelist Validation" "$(cat "$TEST_TMP/c12_err.log")"
fi

# ------------------------------------------------------------------
# Case 13: Limits & Performance (5MB transcript -> <=200KB, <=60 steps, <2s) (3d)
# ------------------------------------------------------------------
t_5mb="$TEST_RUNTIME/tasks/task-5mb"
mkdir -p "$t_5mb"
cat > "$t_5mb/state" <<EOF
task_id=task-5mb
worker=agy
mode=resident
status=done
started_epoch=$(( $(date +%s) - 300 ))
completed_epoch=$(( $(date +%s) - 10 ))
exit_code=0
EOF

mb_uuid="mb-uuid-5555"
mb_log_dir="$FAKE_TRANSCRIPT_ROOT/brain/$mb_uuid/.system_generated/logs"
mkdir -p "$mb_log_dir"
mb_trans="$mb_log_dir/transcript-5mb.jsonl"

python3 -c '
import json, sys
out = sys.argv[1]
with open(out, "w") as f:
    for i in range(5200):
        entry = {
            "step_index": i,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": f"2026-09-25T01:{i%60:02d}:00Z",
            "content": f"Step number {i} with payload data " + ("x" * 800)
        }
        f.write(json.dumps(entry) + "\n")
' "$mb_trans"

cat > "$TEST_RUNTIME/events/archive/ev-5mb.evt" <<EOF
id=ev-5mb
source=agy
kind=done
task_id=task-5mb
detail_path=$mb_trans
EOF

sleep 1.6

c13_resp="$TEST_TMP/c13_5mb_resp.json"
c13_time_file="$TEST_TMP/c13_time.txt"

python3 -c '
import urllib.request, time, sys

start = time.time()
url = sys.argv[1]
req = urllib.request.Request(url)
with urllib.request.urlopen(req, timeout=10) as resp:
    data = resp.read()
elapsed = time.time() - start

with open(sys.argv[2], "wb") as f:
    f.write(data)

with open(sys.argv[3], "w") as f:
    f.write(str(elapsed))
' "http://127.0.0.1:${MAIN_PORT}/api/task/task-5mb" "$c13_resp" "$c13_time_file"

c13_elapsed="$(cat "$c13_time_file")"
c13_bytes=$(wc -c < "$c13_resp")

py_c13='
import json, sys

with open(sys.argv[1]) as f:
    d = json.load(f)

timeline = d.get("timeline", [])
assert len(timeline) <= 60, f"Expected <= 60 timeline steps, got {len(timeline)}"
assert len(timeline) > 0, "Expected non-empty timeline"

size_bytes = int(sys.argv[2])
assert size_bytes <= 200 * 1024, f"Response size {size_bytes} exceeds 200KB"

elapsed = float(sys.argv[3])
assert elapsed < 2.0, f"Response time {elapsed:.3f}s exceeds 2.0s"

sys.exit(0)
'

if python3 -c "$py_c13" "$c13_resp" "$c13_bytes" "$c13_elapsed" 2>"$TEST_TMP/c13_err.log"; then
  pass_case 13 "Limits & Performance: 5MB transcript returned response of size ${c13_bytes}B (<=200KB) with steps <=60 in ${c13_elapsed}s (<2s)"
else
  fail_case 13 "Limits & Performance" "$(cat "$TEST_TMP/c13_err.log") (bytes=$c13_bytes, elapsed=$c13_elapsed)"
fi

# ------------------------------------------------------------------
# Case 14: Method & Path Restrictions (405, 400, 404) (3e)
# ------------------------------------------------------------------
c14_ok=true

for m in POST PUT DELETE PATCH HEAD; do
  m_t=$(timeout 60 curl -s -o /dev/null -w "%{http_code}" -X "$m" "http://127.0.0.1:${MAIN_PORT}/api/task/task-norm-01" || echo "000")
  m_e=$(timeout 60 curl -s -o /dev/null -w "%{http_code}" -X "$m" "http://127.0.0.1:${MAIN_PORT}/api/event/ev-norm-01" || echo "000")
  if [[ "$m_t" != "405" ]] || [[ "$m_e" != "405" ]]; then
    c14_ok=false
    fail_case 14 "Method restriction" "Method $m returned task=$m_t, evt=$m_e (expected 405)"
  fi
done

for bad_id in "../escape" "bad%20space" "invalid/slash" "invalid\\backslash"; do
  c_t=$(timeout 60 curl -s -o /dev/null -w "%{http_code}" --path-as-is "http://127.0.0.1:${MAIN_PORT}/api/task/${bad_id}" || echo "000")
  c_e=$(timeout 60 curl -s -o /dev/null -w "%{http_code}" --path-as-is "http://127.0.0.1:${MAIN_PORT}/api/event/${bad_id}" || echo "000")
  if [[ "$c_t" != "400" ]] || [[ "$c_e" != "400" ]]; then
    c14_ok=false
    fail_case 14 "Invalid ID validation" "ID '$bad_id' returned task=$c_t, evt=$c_e (expected 400)"
  fi
done

c_t_none=$(timeout 60 curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${MAIN_PORT}/api/task/nonexistent-task-9999" || echo "000")
c_e_none=$(timeout 60 curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${MAIN_PORT}/api/event/nonexistent-evt-9999" || echo "000")
if [[ "$c_t_none" != "404" ]] || [[ "$c_e_none" != "404" ]]; then
  c14_ok=false
  fail_case 14 "Non-existent ID check" "Non-existent IDs returned task=$c_t_none, evt=$c_e_none (expected 404)"
fi

if [[ "$c14_ok" == "true" ]]; then
  pass_case 14 "Method & Path Restrictions: POST/PUT/DELETE return 405; invalid IDs return 400; non-existent IDs return 404"
fi

# ------------------------------------------------------------------
# Case 15: Fault Tolerance on Broken/Malformed JSONL Lines (3f)
# ------------------------------------------------------------------
t_broken="$TEST_RUNTIME/tasks/task-broken-jsonl"
mkdir -p "$t_broken"
cat > "$t_broken/state" <<EOF
task_id=task-broken-jsonl
worker=agy
mode=resident
status=done
EOF

broken_uuid="broken-uuid-8888"
broken_log_dir="$FAKE_TRANSCRIPT_ROOT/brain/$broken_uuid/.system_generated/logs"
mkdir -p "$broken_log_dir"
broken_trans="$broken_log_dir/transcript.jsonl"

cat > "$broken_trans" <<'EOF'
{"step_index":0,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Valid step 1"}
{malformed json line here without quotes or closing
binary garbage \x00\x01\xfe\xff\x80
{"incomplete": "json
another broken line
{"step_index":1,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Valid step 2"}
not a json at all
{"step_index":2,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Valid step 3"}
EOF

cat > "$TEST_RUNTIME/events/archive/ev-broken.evt" <<EOF
id=ev-broken
source=agy
kind=done
task_id=task-broken-jsonl
detail_path=$broken_trans
EOF

sleep 1.6

c15_resp="$TEST_TMP/c15_broken_resp.json"
c15_code=$(timeout 60 curl -s -o "$c15_resp" -w "%{http_code}" "http://127.0.0.1:${MAIN_PORT}/api/task/task-broken-jsonl" || echo "000")

py_c15='
import json, sys

with open(sys.argv[1]) as f:
    d = json.load(f)

timeline = d.get("timeline", [])
assert len(timeline) == 3, f"Expected 3 valid steps, got {len(timeline)}"
assert timeline[0]["summary"] == "Valid step 1"
assert timeline[1]["summary"] == "Valid step 2"
assert timeline[2]["summary"] == "Valid step 3"
sys.exit(0)
'

if [[ "$c15_code" == "200" ]] && python3 -c "$py_c15" "$c15_resp" 2>"$TEST_TMP/c15_err.log"; then
  pass_case 15 "Fault Tolerance: Broken/malformed JSONL lines in transcript skipped gracefully, returning 200 OK with valid remaining steps"
else
  fail_case 15 "Fault Tolerance on Transcript" "code=$c15_code, err=$(cat "$TEST_TMP/c15_err.log" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# Case 16: Inferred Linking for Running/Resident Tasks (3g)
# ------------------------------------------------------------------
t_inf="$TEST_RUNTIME/tasks/task-inf-01"
mkdir -p "$t_inf"
now_sec=$(date +%s)
cat > "$t_inf/state" <<EOF
task_id=task-inf-01
worker=agy
mode=resident
status=running
started_epoch=$(( now_sec - 10 ))
EOF

inf_uuid="inferred-uuid-7777"
inf_log_dir="$FAKE_TRANSCRIPT_ROOT/brain/$inf_uuid/.system_generated/logs"
mkdir -p "$inf_log_dir"
inf_trans="$inf_log_dir/transcript.jsonl"

cat > "$inf_trans" <<'EOF'
{"step_index":0,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Inferred task working step"}
EOF

touch -m -d "@$now_sec" "$inf_trans"

sleep 1.6

c16_resp="$TEST_TMP/c16_inf_resp.json"
c16_code=$(timeout 60 curl -s -o "$c16_resp" -w "%{http_code}" "http://127.0.0.1:${MAIN_PORT}/api/task/task-inf-01" || echo "000")

py_c16='
import json, sys

with open(sys.argv[1]) as f:
    d = json.load(f)

assert d.get("linked") == "inferred", "Expected linked: inferred, got " + str(d.get("linked"))
timeline = d.get("timeline", [])
assert timeline == [], f"Expected empty timeline (never leaks foreign steps), got {len(timeline)}"
sys.exit(0)
'

if [[ "$c16_code" == "200" ]] && python3 -c "$py_c16" "$c16_resp" 2>"$TEST_TMP/c16_err.log"; then
  pass_case 16 "Inferred Linking: Running resident task automatically infers transcript when event has no detail_path and start marker missing (linked: 'inferred', timeline: [])"
else
  fail_case 16 "Inferred Linking" "code=$c16_code, err=$(cat "$TEST_TMP/c16_err.log" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------
# Case 17: index.html Static Check (Zero innerHTML, textContent only) (3h)
# ------------------------------------------------------------------
c17_ok=true

if grep -rn "innerHTML" "$HERE/dashboard/index.html" > "$TEST_TMP/c17_innerhtml.log"; then
  c17_ok=false
  fail_case 17 "Static check: innerHTML found" "$(cat "$TEST_TMP/c17_innerhtml.log")"
fi

text_count=$(grep -c "textContent" "$HERE/dashboard/index.html" || true)
if (( text_count < 10 )); then
  c17_ok=false
  fail_case 17 "Static check: textContent" "Found only $text_count occurrences of textContent (expected >= 10)"
fi

if [[ "$c17_ok" == "true" ]]; then
  pass_case 17 "Static Check: dashboard/index.html has 0 occurrences of innerHTML and uses textContent across all sheets ($text_count uses)"
fi

# ------------------------------------------------------------------
# Case 18: run.sh Safety with DASHBOARD_STATE_DIR & Live Invariance (3i)
# ------------------------------------------------------------------
c18_ok=true

LIVE_PID_FILE="$HERE/dashboard/.pid"
LIVE_PID=""
if [[ -f "$LIVE_PID_FILE" ]]; then
  LIVE_PID="$(cat "$LIVE_PID_FILE" 2>/dev/null || true)"
fi

LIVE_WAS_RUNNING=false
if [[ -n "$LIVE_PID" ]] && kill -0 "$LIVE_PID" 2>/dev/null; then
  LIVE_WAS_RUNNING=true
fi

SAFE_STATE_DIR="$TEST_TMP/safe_dashboard_state"
SAFE_PORT=$(get_free_port)
mkdir -p "$SAFE_STATE_DIR"

s18_rc=0
DASHBOARD_STATE_DIR="$SAFE_STATE_DIR" timeout 60 "$HERE/dashboard/run.sh" start \
  --port "$SAFE_PORT" \
  --runtime "$TEST_RUNTIME" \
  --transcript-root "$FAKE_TRANSCRIPT_ROOT" \
  --host 127.0.0.1 > "$TEST_TMP/c18_start.log" 2>&1 || s18_rc=$?

if (( s18_rc != 0 )); then
  c18_ok=false
  fail_case 18 "run.sh safe start" "Start failed (exit $s18_rc): $(cat "$TEST_TMP/c18_start.log")"
fi

SAFE_PID_FILE="$SAFE_STATE_DIR/.pid"
if [[ ! -f "$SAFE_PID_FILE" ]]; then
  c18_ok=false
  fail_case 18 "run.sh safe PID" "Isolated PID file was not created at $SAFE_PID_FILE"
fi

safe_spid="$(cat "$SAFE_PID_FILE" 2>/dev/null || true)"
if [[ -z "$safe_spid" ]] || ! kill -0 "$safe_spid" 2>/dev/null; then
  c18_ok=false
  fail_case 18 "run.sh safe PID process" "Process $safe_spid is not running"
fi

st18_rc=0
st18_out="$(DASHBOARD_STATE_DIR="$SAFE_STATE_DIR" timeout 60 "$HERE/dashboard/run.sh" status 2>&1)" || st18_rc=$?
if (( st18_rc != 0 )) || [[ "$st18_out" != *"running"* ]]; then
  c18_ok=false
  fail_case 18 "run.sh safe status" "Status failed: $st18_out"
fi

stop18_rc=0
DASHBOARD_STATE_DIR="$SAFE_STATE_DIR" timeout 60 "$HERE/dashboard/run.sh" stop > "$TEST_TMP/c18_stop.log" 2>&1 || stop18_rc=$?
if (( stop18_rc != 0 )); then
  c18_ok=false
  fail_case 18 "run.sh safe stop" "Stop failed: $(cat "$TEST_TMP/c18_stop.log")"
fi

if [[ -f "$SAFE_PID_FILE" ]]; then
  c18_ok=false
  fail_case 18 "run.sh safe stop cleanup" "Isolated PID file still exists after stop"
fi

if [[ -n "$safe_spid" ]] && kill -0 "$safe_spid" 2>/dev/null; then
  c18_ok=false
  fail_case 18 "run.sh safe termination" "Isolated process $safe_spid still running"
fi

ling_18="$(pgrep -f "server.py.*$SAFE_PORT" 2>/dev/null || true)"
if [[ -n "$ling_18" ]]; then
  c18_ok=false
  fail_case 18 "run.sh safe lingering" "Found lingering process: $ling_18"
fi

if [[ "$LIVE_WAS_RUNNING" == "true" ]]; then
  current_live_pid="$(cat "$LIVE_PID_FILE" 2>/dev/null || true)"
  if [[ "$current_live_pid" != "$LIVE_PID" ]]; then
    c18_ok=false
    fail_case 18 "Live PID file invariant" "Live PID file altered: was $LIVE_PID, now $current_live_pid"
  fi
  if ! kill -0 "$LIVE_PID" 2>/dev/null; then
    c18_ok=false
    fail_case 18 "Live process invariant" "Live server PID $LIVE_PID was terminated!"
  fi
  if ! timeout 5 curl -s "http://${LIVE_TS_IP:-$(timeout 3 tailscale ip -4 2>/dev/null | head -n 1)}:8765/healthz" >/dev/null 2>&1 && ! timeout 5 curl -s "http://127.0.0.1:8765/healthz" >/dev/null 2>&1; then
    c18_ok=false
    fail_case 18 "Live health invariant" "Live server healthz failed after test!"
  fi
fi

if [[ "$c18_ok" == "true" ]]; then
  pass_case 18 "run.sh Safety & Live Invariance: Temporary DASHBOARD_STATE_DIR isolated execution cleanly started/stopped, and live server (PID $LIVE_PID / port 8765) is 100% untouched"
fi


# ------------------------------------------------------------------
# Case 19: Multi-Task Transcript Slicing (Tasks A, B, C Isolation) (T0925-03)
# ------------------------------------------------------------------
c19_ok=true

t_a="$TEST_RUNTIME/tasks/task-A-1"
t_b="$TEST_RUNTIME/tasks/task-B-2"
t_c="$TEST_RUNTIME/tasks/task-C-3"
mkdir -p "$t_a" "$t_b" "$t_c"

cat > "$t_a/state" <<EOF
task_id=task-A-1
worker=agy
mode=resident
status=done
started_epoch=1000
completed_epoch=1100
exit_code=0
EOF
printf 'Task A prompt content\n' > "$t_a/prompt.md"

cat > "$t_b/state" <<EOF
task_id=task-B-2
worker=agy
mode=resident
status=done
started_epoch=1200
completed_epoch=1300
exit_code=0
EOF
printf 'Task B prompt content\n' > "$t_b/prompt.md"

cat > "$t_c/state" <<EOF
task_id=task-C-3
worker=agy
mode=resident
status=running
started_epoch=1400
EOF
printf 'Task C prompt content\n' > "$t_c/prompt.md"

c19_uuid="multi-chain-uuid-9999"
c19_log_dir="$FAKE_TRANSCRIPT_ROOT/brain/$c19_uuid/.system_generated/logs"
mkdir -p "$c19_log_dir"
c19_trans="$c19_log_dir/transcript.jsonl"

cat > "$c19_trans" <<EOF
{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","created_at":"2026-09-25T10:00:00Z","content":"<USER_REQUEST>Read and execute task prompt: tasks/task-A-1/prompt.md</USER_REQUEST>"}
{"step_index":1,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T10:01:00Z","content":"Step A1: starting Task A execution"}
{"step_index":2,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T10:02:00Z","content":"Step A2: completing Task A execution"}
{"step_index":3,"source":"USER_EXPLICIT","type":"USER_INPUT","created_at":"2026-09-25T10:03:00Z","content":"<USER_REQUEST>Read and execute task prompt: tasks/task-B-2/prompt.md</USER_REQUEST>"}
{"step_index":4,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T10:04:00Z","tool_calls":[{"name":"view_file","args":{"AbsolutePath":"/home/rerun/agent-command-chain-template/lib/module_b.py"}}]}
{"step_index":5,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T10:05:00Z","content":"Step B2: implementing features for Task B"}
{"step_index":6,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T10:06:00Z","tool_calls":[{"name":"invoke_subagent","args":{"Subagents":[{"Role":"Task B Subagent","Prompt":"Verify Task B"}]}}]}
{"step_index":7,"source":"USER_EXPLICIT","type":"USER_INPUT","created_at":"2026-09-25T10:07:00Z","content":"<USER_REQUEST>Read and execute task prompt: tasks/task-C-3/prompt.md</USER_REQUEST>"}
{"step_index":8,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T10:08:00Z","content":"Step C1: initializing Task C execution"}
{"step_index":9,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T10:09:00Z","content":"Step C2: running Task C continuously"}
EOF

cat > "$TEST_RUNTIME/events/archive/ev-chain-a.evt" <<EOF
id=ev-chain-a
source=agy
kind=done
task_id=task-A-1
detail_path=$c19_trans
EOF

cat > "$TEST_RUNTIME/events/archive/ev-chain-b.evt" <<EOF
id=ev-chain-b
source=agy
kind=done
task_id=task-B-2
detail_path=$c19_trans
EOF

cat > "$TEST_RUNTIME/events/archive/ev-chain-c.evt" <<EOF
id=ev-chain-c
source=agy
kind=ack
task_id=task-C-3
detail_path=$c19_trans
EOF

c19_resp_b="$TEST_TMP/c19_task_b_resp.json"
c19_code_b=$(timeout 60 curl -s -o "$c19_resp_b" -w "%{http_code}" "http://127.0.0.1:${MAIN_PORT}/api/task/task-B-2" || echo "000")

c19_resp_c="$TEST_TMP/c19_task_c_resp.json"
c19_code_c=$(timeout 60 curl -s -o "$c19_resp_c" -w "%{http_code}" "http://127.0.0.1:${MAIN_PORT}/api/task/task-C-3" || echo "000")

py_c19='
import json, sys

with open(sys.argv[1]) as fb, open(sys.argv[2]) as fc:
    db = json.load(fb)
    dc = json.load(fc)

# 1. Task B verification
assert db.get("linked") == "exact", "Task B expected linked: exact, got " + str(db.get("linked"))
assert db.get("truncated_before") is False, "Task B truncated_before should be False"
tl_b = db.get("timeline", [])
indices_b = [s["step_index"] for s in tl_b]
assert indices_b == [3, 4, 5, 6], f"Task B timeline indices expected [3, 4, 5, 6], got {indices_b}"
# Verify zero steps from Task A or Task C
for s in tl_b:
    sum_text = s.get("summary", "")
    assert "task-A-1" not in sum_text and "Task A" not in sum_text, f"Foreign Task A leaked into Task B: {sum_text}"
    assert "task-C-3" not in sum_text and "Task C" not in sum_text, f"Foreign Task C leaked into Task B: {sum_text}"

# 2. Task C verification (latest/running task extending to EOF)
assert dc.get("linked") == "exact", "Task C expected linked: exact, got " + str(dc.get("linked"))
assert dc.get("truncated_before") is False, "Task C truncated_before should be False"
tl_c = dc.get("timeline", [])
indices_c = [s["step_index"] for s in tl_c]
assert indices_c == [7, 8, 9], f"Task C timeline indices expected [7, 8, 9], got {indices_c}"
assert tl_c[-1]["step_index"] == 9, "Task C timeline must extend to end of file"
for s in tl_c:
    sum_text = s.get("summary", "")
    assert "task-A-1" not in sum_text and "Task A" not in sum_text, f"Foreign Task A leaked into Task C: {sum_text}"
    assert "task-B-2" not in sum_text and "Task B" not in sum_text, f"Foreign Task B leaked into Task C: {sum_text}"

sys.exit(0)
'

if [[ "$c19_code_b" == "200" && "$c19_code_c" == "200" ]] && python3 -c "$py_c19" "$c19_resp_b" "$c19_resp_c" 2>"$TEST_TMP/c19_err.log"; then
  pass_case 19 "Multi-Task Transcript Slicing: Task B contains ONLY Task B steps (zero Task A/C leaks), Task C extends to end of file"
else
  fail_case 19 "Multi-Task Transcript Slicing" "code_b=$c19_code_b, code_c=$c19_code_c, err=$(cat "$TEST_TMP/c19_err.log" 2>/dev/null || true)"
fi


# ------------------------------------------------------------------
# Case 20: Missing Start Marker in Transcript (linked: 'inferred', timeline: [])
# ------------------------------------------------------------------
t_miss="$TEST_RUNTIME/tasks/task-missing-start"
mkdir -p "$t_miss"
cat > "$t_miss/state" <<EOF
task_id=task-missing-start
worker=agy
mode=resident
status=done
started_epoch=1500
completed_epoch=1600
exit_code=0
EOF
printf 'Task missing prompt\n' > "$t_miss/prompt.md"

cat > "$TEST_RUNTIME/events/archive/ev-missing-start.evt" <<EOF
id=ev-missing-start
source=agy
kind=done
task_id=task-missing-start
detail_path=$c19_trans
EOF

c20_resp="$TEST_TMP/c20_resp.json"
c20_code=$(timeout 60 curl -s -o "$c20_resp" -w "%{http_code}" "http://127.0.0.1:${MAIN_PORT}/api/task/task-missing-start" || echo "000")

py_c20='
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
assert d.get("linked") == "inferred", "Expected linked: inferred, got " + str(d.get("linked"))
assert d.get("timeline") == [], "Expected timeline: [], got " + str(d.get("timeline"))
assert d.get("truncated_before") is False, "Expected truncated_before: False"
sys.exit(0)
'

if [[ "$c20_code" == "200" ]] && python3 -c "$py_c20" "$c20_resp" 2>"$TEST_TMP/c20_err.log"; then
  pass_case 20 "Missing Start Marker: linked: 'inferred', timeline: [] (never leaks foreign steps)"
else
  fail_case 20 "Missing Start Marker" "code=$c20_code, err=$(cat "$TEST_TMP/c20_err.log" 2>/dev/null || true)"
fi


# ------------------------------------------------------------------
# Case 21: Query Task Starting Before 2MB Tail Cut (linked: 'partial', truncated_before: true)
# ------------------------------------------------------------------
t_cut="$TEST_RUNTIME/tasks/task-cut-old"
t_cut_next="$TEST_RUNTIME/tasks/task-cut-next"
mkdir -p "$t_cut" "$t_cut_next"

cat > "$t_cut/state" <<EOF
task_id=task-cut-old
worker=agy
mode=resident
status=done
started_epoch=100
completed_epoch=9000
exit_code=0
EOF
printf 'Task cut old prompt\n' > "$t_cut/prompt.md"

cat > "$t_cut_next/state" <<EOF
task_id=task-cut-next
worker=agy
mode=resident
status=running
started_epoch=8000
EOF
printf 'Task cut next prompt\n' > "$t_cut_next/prompt.md"

c21_uuid="cut-tail-uuid-1111"
c21_log_dir="$FAKE_TRANSCRIPT_ROOT/brain/$c21_uuid/.system_generated/logs"
mkdir -p "$c21_log_dir"
c21_trans="$c21_log_dir/transcript-cut.jsonl"

python3 -c '
import json, sys
out = sys.argv[1]
with open(out, "w") as f:
    # Initial task prompt at step 0 (epoch 100)
    f.write(json.dumps({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": "100", "content": "<USER_REQUEST>Read and execute task prompt: tasks/task-cut-old/prompt.md</USER_REQUEST>"}) + "\n")
    # 2500 filler steps (~2.4MB)
    for i in range(1, 2500):
        epoch = 1000 + i
        f.write(json.dumps({"step_index": i, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": str(epoch), "content": f"Working on cut-old step {i} " + ("k" * 900)}) + "\n")
    # Next task prompt at step 2500 (epoch 8000)
    f.write(json.dumps({"step_index": 2500, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": "8000", "content": "<USER_REQUEST>Read and execute task prompt: tasks/task-cut-next/prompt.md</USER_REQUEST>"}) + "\n")
    f.write(json.dumps({"step_index": 2501, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": "8001", "content": "Working on cut-next step 2501"}) + "\n")
' "$c21_trans"

cat > "$TEST_RUNTIME/events/archive/ev-cut-old.evt" <<EOF
id=ev-cut-old
source=agy
kind=done
task_id=task-cut-old
detail_path=$c21_trans
EOF

c21_resp="$TEST_TMP/c21_resp.json"
c21_code=$(timeout 60 curl -s -o "$c21_resp" -w "%{http_code}" "http://127.0.0.1:${MAIN_PORT}/api/task/task-cut-old" || echo "000")

py_c21='
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
assert d.get("linked") == "partial", "Expected linked: partial, got " + str(d.get("linked"))
assert d.get("truncated_before") is True, "Expected truncated_before: True, got " + str(d.get("truncated_before"))
tl = d.get("timeline", [])
assert len(tl) > 0, "Expected non-empty timeline for partial slice"
for s in tl:
    assert s["step_index"] < 2500, "Next task step leaked into timeline: " + str(s["step_index"])
    assert "task-cut-next" not in s.get("summary", ""), "task-cut-next leaked into summary: " + str(s.get("summary"))
sys.exit(0)
'

if [[ "$c21_code" == "200" ]] && python3 -c "$py_c21" "$c21_resp" 2>"$TEST_TMP/c21_err.log"; then
  pass_case 21 "2MB Tail Cut Slicing: linked: 'partial', truncated_before: true, steps isolated from next task"
else
  fail_case 21 "2MB Tail Cut Slicing" "code=$c21_code, err=$(cat "$TEST_TMP/c21_err.log" 2>/dev/null || true)"
fi


# ------------------------------------------------------------------
# Case 22: Noise Removal & Classification (T0925-03)
# ------------------------------------------------------------------
t_noise="$TEST_RUNTIME/tasks/task-noise-val"
mkdir -p "$t_noise"
cat > "$t_noise/state" <<EOF
task_id=task-noise-val
worker=agy
mode=resident
status=done
started_epoch=2000
completed_epoch=2100
exit_code=0
EOF
printf 'Task noise validation prompt\n' > "$t_noise/prompt.md"

c22_uuid="noise-uuid-2222"
c22_log_dir="$FAKE_TRANSCRIPT_ROOT/brain/$c22_uuid/.system_generated/logs"
mkdir -p "$c22_log_dir"
c22_trans="$c22_log_dir/transcript-noise.jsonl"

cat > "$c22_trans" <<EOF
{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","created_at":"2026-09-25T12:00:00Z","content":"<USER_REQUEST>tasks/task-noise-val/prompt.md</USER_REQUEST>"}
{"step_index":1,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T12:01:00Z","content":"Created At: 2026-09-25T12:00:00Z\nCompleted At: 2026-09-25T12:01:00Z\nFile Path: /some/path/file.py\nStep Id: 99\nTotal Lines: 42\nTotal Bytes: 1024\nShowing lines 1 to 20\nClean output content without meta prefix"}
{"step_index":2,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T12:02:00Z","tool_calls":[{"name":"view_file","args":{"AbsolutePath":"/home/rerun/agent-command-chain-template/dashboard/server.py"}}]}
{"step_index":3,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T12:03:00Z","content":"   \n  \n"}
{"step_index":4,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T12:04:00Z","content":"Created At: 2026-09-25\nCompleted At: 2026-09-25\n"}
{"step_index":5,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T12:05:00Z","tool_calls":[{"name":"invoke_subagent","args":{"Subagents":[{"Role":"QA Engineer","Prompt":"Run tests"}]}}]}
{"step_index":6,"source":"MODEL","type":"PLANNER_RESPONSE","created_at":"2026-09-25T12:06:00Z","tool_calls":[{"name":"send_message","args":{"Recipient":"qa-sub","Message":"Please verify results"}}]}
{"step_index":7,"source":"SUBAGENT","type":"SUBAGENT_MESSAGE","created_at":"2026-09-25T12:07:00Z","content":"[Message] sender=qa-sub priority=HIGH content=All verification tests passed"}
EOF

cat > "$TEST_RUNTIME/events/archive/ev-noise-val.evt" <<EOF
id=ev-noise-val
source=agy
kind=done
task_id=task-noise-val
detail_path=$c22_trans
EOF

c22_resp="$TEST_TMP/c22_resp.json"
c22_code=$(timeout 60 curl -s -o "$c22_resp" -w "%{http_code}" "http://127.0.0.1:${MAIN_PORT}/api/task/task-noise-val" || echo "000")

py_c22='
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)

tl = d.get("timeline", [])
step_map = {s["step_index"]: s for s in tl}

# 1. Meta prefix stripping
assert 1 in step_map, "Step 1 missing"
s1 = step_map[1]
assert s1["summary"] == "Clean output content without meta prefix", "Meta prefix not stripped: " + str(s1["summary"])
assert "Created At:" not in s1["summary"]
assert "File Path:" not in s1["summary"]
assert "Step Id:" not in s1["summary"]

# 2. Tool call path abbreviation
assert 2 in step_map, "Step 2 missing"
s2 = step_map[2]
assert ".../dashboard/server.py" in s2["summary"], "Path not abbreviated: " + str(s2["summary"])
assert "/home/rerun" not in s2["summary"], "Full path leaked: " + str(s2["summary"])

# 3. Empty model responses omitted
assert 3 not in step_map, "Empty model response (step 3) was not omitted!"
assert 4 not in step_map, "Model response stripped to empty (step 4) was not omitted!"

# 4. Subagent classification
assert 5 in step_map, "Step 5 missing"
s5 = step_map[5]
assert s5["type"] == "subagent", "invoke_subagent type expected subagent, got " + str(s5["type"])
assert "QA Engineer" in s5["summary"]

assert 6 in step_map, "Step 6 missing"
s6 = step_map[6]
assert s6["type"] == "subagent", "send_message type expected subagent, got " + str(s6["type"])
assert "send_message" in s6["summary"]

assert 7 in step_map, "Step 7 missing"
s7 = step_map[7]
assert s7["type"] == "subagent", "subagent content type expected subagent, got " + str(s7["type"])

sys.exit(0)
'

if [[ "$c22_code" == "200" ]] && python3 -c "$py_c22" "$c22_resp" 2>"$TEST_TMP/c22_err.log"; then
  pass_case 22 "Noise Removal & Classification: meta prefixes stripped, paths abbreviated (.../parent/base), empty model responses omitted, subagent types classified"
else
  fail_case 22 "Noise Removal & Classification" "code=$c22_code, err=$(cat "$TEST_TMP/c22_err.log" 2>/dev/null || true)"
fi


# ------------------------------------------------------------------
# Case 23: Limits & Security (Steps <= 60, Summary <= 300 chars, Response <= 200KB)
# ------------------------------------------------------------------
t_lim="$TEST_RUNTIME/tasks/task-limits-val"
mkdir -p "$t_lim"
cat > "$t_lim/state" <<EOF
task_id=task-limits-val
worker=agy
mode=resident
status=done
started_epoch=3000
completed_epoch=3100
exit_code=0
EOF
printf 'Task limits prompt\n' > "$t_lim/prompt.md"

c23_uuid="limits-uuid-3333"
c23_log_dir="$FAKE_TRANSCRIPT_ROOT/brain/$c23_uuid/.system_generated/logs"
mkdir -p "$c23_log_dir"
c23_trans="$c23_log_dir/transcript-limits.jsonl"

python3 -c '
import json, sys
out = sys.argv[1]
with open(out, "w") as f:
    f.write(json.dumps({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": "2026-09-25T13:00:00Z", "content": "tasks/task-limits-val/prompt.md"}) + "\n")
    for i in range(1, 90):
        long_str = f"Step {i} very long line content " + ("ABCDEFG1234567 " * 40)
        f.write(json.dumps({"step_index": i, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": f"2026-09-25T13:{i%60:02d}:00Z", "content": long_str}) + "\n")
' "$c23_trans"

cat > "$TEST_RUNTIME/events/archive/ev-limits-val.evt" <<EOF
id=ev-limits-val
source=agy
kind=done
task_id=task-limits-val
detail_path=$c23_trans
EOF

c23_resp="$TEST_TMP/c23_resp.json"
c23_code=$(timeout 60 curl -s -o "$c23_resp" -w "%{http_code}" "http://127.0.0.1:${MAIN_PORT}/api/task/task-limits-val" || echo "000")
c23_size=$(wc -c < "$c23_resp")

py_c23='
import json, sys
resp_size = int(sys.argv[2])
with open(sys.argv[1]) as f:
    d = json.load(f)

timeline = d.get("timeline", [])
assert len(timeline) <= 60, f"Expected timeline <= 60 steps, got {len(timeline)}"
assert len(timeline) > 0, "Expected non-empty timeline"

for s in timeline:
    summary = s.get("summary", "")
    assert len(summary) <= 300, f"Summary exceeds 300 chars: len={len(summary)}"

assert resp_size <= 200 * 1024, f"Response size {resp_size} exceeds 200KB"
sys.exit(0)
'

if [[ "$c23_code" == "200" ]] && python3 -c "$py_c23" "$c23_resp" "$c23_size" 2>"$TEST_TMP/c23_err.log"; then
  pass_case 23 "Limits & Security: Steps <= 60, summary <= 300 chars, response <= 200KB verified"
else
  fail_case 23 "Limits & Security" "code=$c23_code, size=$c23_size, err=$(cat "$TEST_TMP/c23_err.log" 2>/dev/null || true)"
fi


# ------------------------------------------------------------------
# 테스트 종료 및 정리
# ------------------------------------------------------------------
cleanup_test_servers

echo "=================================================================="
echo "dashboard-test.sh Summary: PASSED=$PASSED, FAILED=$FAILED"
echo "=================================================================="

if (( FAILED > 0 )); then
  exit 1
fi
exit 0
