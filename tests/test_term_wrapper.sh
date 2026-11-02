#!/usr/bin/env bash
# tests/test_term_wrapper.sh: lib/term.sh abstraction layer unit tests
# Verifies:
#   1. tmux backend primitives (session_exists, pane_alive, pid, fg_cmd, send_line, run_cmd, capture_tail, env, multiline rejection)
#   2. resident pane verification (tmux and herdr schemas)
#   3. herdr backend mock dispatch
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "term-wrapper"
setup_mock_runtime "$TEST_RUNTIME"

TEST_BIN="$TEST_HOME/.local/bin"
mkdir -p "$TEST_BIN"
export PATH="$TEST_BIN:$PATH"

# shellcheck disable=SC1091
source "$HERE/lib/term.sh"

echo "=== Test 1: tmux backend primitives on real tmux session ==="
TEST_SESS="acc-term-test-${RAND_HEX}"
tmux new-session -d -s "$TEST_SESS" -n "worker" -c "$TEST_TMP"
wait_pane_shell "$TEST_SESS:worker" 10

# session exists
if term_session_exists "$TEST_SESS" && ! term_session_exists "nonexistent-${RAND_HEX}"; then
  echo "PASS: term_session_exists"
else
  echo "FAIL: term_session_exists" >&2
  exit 1
fi

# has window & pane alive
if term_has_window "$TEST_SESS" "worker" && ! term_has_window "$TEST_SESS" "nonexistent"; then
  echo "PASS: term_has_window"
else
  echo "FAIL: term_has_window" >&2
  exit 1
fi

if term_pane_alive "$TEST_SESS:worker"; then
  echo "PASS: term_pane_alive"
else
  echo "FAIL: term_pane_alive" >&2
  exit 1
fi

# pane pid
p_pid="$(term_pane_pid "$TEST_SESS:worker")"
if [[ "$p_pid" =~ ^[0-9]+$ ]] && (( p_pid > 0 )); then
  echo "PASS: term_pane_pid ($p_pid)"
else
  echo "FAIL: term_pane_pid ($p_pid)" >&2
  exit 1
fi

# fg cmd
p_cmd="$(term_fg_cmd "$TEST_SESS:worker")"
if [[ -n "$p_cmd" ]]; then
  echo "PASS: term_fg_cmd ($p_cmd)"
else
  echo "FAIL: term_fg_cmd ($p_cmd)" >&2
  exit 1
fi

# term_set_env & term_get_env
term_set_env "$TEST_SESS" "ACC_TEST_VAR" "foobar123"
val="$(term_get_env "$TEST_SESS" "ACC_TEST_VAR")"
if [[ "$val" == "foobar123" ]]; then
  echo "PASS: term_set_env & term_get_env"
else
  echo "FAIL: term_set_env & term_get_env (got '$val')" >&2
  exit 1
fi

# term_send_line & execution
test_out="$TEST_TMP/term_send_out.txt"
term_send_line "$TEST_SESS:worker" "printf 'TERM_LINE_OK\n' > '$test_out'"
for _ in $(seq 1 30); do
  [[ -f "$test_out" ]] && break
  sleep 0.1
done
if grep -q "TERM_LINE_OK" "$test_out" 2>/dev/null; then
  echo "PASS: term_send_line execution"
else
  echo "FAIL: term_send_line execution" >&2
  exit 1
fi

# term_capture_tail
captured="$(term_capture_tail "$TEST_SESS:worker" 10)"
if [[ -n "$captured" ]]; then
  echo "PASS: term_capture_tail captured lines"
else
  echo "FAIL: term_capture_tail empty" >&2
  exit 1
fi

# Multiline injection rejection
if term_send_line "$TEST_SESS:worker" $'line1\nline2' 2>/dev/null; then
  echo "FAIL: term_send_line allowed multiline" >&2
  exit 1
else
  echo "PASS: term_send_line rejected multiline"
fi

if term_run_cmd "$TEST_SESS:worker" $'cmd1\ncmd2' 2>/dev/null; then
  echo "FAIL: term_run_cmd allowed multiline" >&2
  exit 1
else
  echo "PASS: term_run_cmd rejected multiline"
fi

# term_paste_emergency
term_paste_emergency "$TEST_SESS" "$TEST_SESS:worker" "EMERGENCY_MSG"
term_send_key "$TEST_SESS:worker" C-m
echo "PASS: term_paste_emergency and term_send_key"

# Clean up tmux session
tmux kill-session -t "$TEST_SESS" 2>/dev/null || true

echo "=== Test 2: Resident pane verification ==="
marker="$TEST_TMP/mock.resident"
cat > "$marker" <<EOF
pane_id=%42
session=mock-sess
EOF

# Matching pane
if TMUX_PANE="%42" term_verify_resident_pane "$marker"; then
  echo "PASS: term_verify_resident_pane matching pane"
else
  echo "FAIL: term_verify_resident_pane matching pane" >&2
  exit 1
fi

# Mismatching pane
if TMUX_PANE="%99" term_verify_resident_pane "$marker"; then
  echo "FAIL: term_verify_resident_pane allowed mismatch" >&2
  exit 1
else
  echo "PASS: term_verify_resident_pane rejected mismatch"
fi

# Herdr marker schema
cat > "$marker" <<EOF
backend=herdr
pane_id=pane-xyz
session=herdr-sess
socket_path=/tmp/herdr.sock
EOF

if HERDR_PANE_ID="pane-xyz" HERDR_SESSION="herdr-sess" HERDR_SOCKET_PATH="/tmp/herdr.sock" term_verify_resident_pane "$marker"; then
  echo "PASS: term_verify_resident_pane matching herdr marker"
else
  echo "FAIL: term_verify_resident_pane matching herdr marker" >&2
  exit 1
fi

echo "=== Test 3: Herdr backend mock dispatch ==="
HERDR_LOG="$TEST_TMP/herdr_mock.log"
cat > "$TEST_BIN/herdr" <<HERDR_EOF
#!/usr/bin/env bash
echo "\$@" >> "$HERDR_LOG"
case "\$*" in
  *"status"*)
    echo "status: running"
    exit 0
    ;;
  *"pane get"*)
    exit 0
    ;;
  *"pane process-info"*)
    echo "shell_pid=12345 current_command=bash"
    exit 0
    ;;
  *"pane list"*)
    echo "worker"
    echo "sonnet"
    exit 0
    ;;
  *"env get"*)
    echo "MY_VAR=herdr_val"
    exit 0
    ;;
  *"pane read"*)
    echo "mock herdr output lines"
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
HERDR_EOF
chmod +x "$TEST_BIN/herdr"

export ACC_TERM_BACKEND="herdr"

if term_session_exists "herdr-sess"; then
  echo "PASS: herdr term_session_exists"
else
  echo "FAIL: herdr term_session_exists" >&2
  exit 1
fi

if term_pane_alive "herdr-sess:worker"; then
  echo "PASS: herdr term_pane_alive"
else
  echo "FAIL: herdr term_pane_alive" >&2
  exit 1
fi

h_pid="$(term_pane_pid "herdr-sess:worker")"
if [[ "$h_pid" == "12345" ]]; then
  echo "PASS: herdr term_pane_pid ($h_pid)"
else
  echo "FAIL: herdr term_pane_pid ($h_pid)" >&2
  exit 1
fi

h_cmd="$(term_fg_cmd "herdr-sess:worker")"
if [[ "$h_cmd" == "bash" ]]; then
  echo "PASS: herdr term_fg_cmd ($h_cmd)"
else
  echo "FAIL: herdr term_fg_cmd ($h_cmd)" >&2
  exit 1
fi

h_val="$(term_get_env "herdr-sess" "MY_VAR")"
if [[ "$h_val" == "herdr_val" ]]; then
  echo "PASS: herdr term_get_env ($h_val)"
else
  echo "FAIL: herdr term_get_env ($h_val)" >&2
  exit 1
fi

term_send_line "herdr-sess:worker" "echo test"
if grep -q "pane send-text worker echo test" "$HERDR_LOG" && grep -q "pane send-keys worker enter" "$HERDR_LOG"; then
  echo "PASS: herdr term_send_line"
else
  echo "FAIL: herdr term_send_line (log: $(cat "$HERDR_LOG"))" >&2
  exit 1
fi

term_run_cmd "herdr-sess:worker" "echo run"
if grep -q "pane run worker echo run" "$HERDR_LOG"; then
  echo "PASS: herdr term_run_cmd"
else
  echo "FAIL: herdr term_run_cmd" >&2
  exit 1
fi

h_tail="$(term_capture_tail "herdr-sess:worker" 5)"
if [[ "$h_tail" == "mock herdr output lines" ]]; then
  echo "PASS: herdr term_capture_tail"
else
  echo "FAIL: herdr term_capture_tail" >&2
  exit 1
fi

echo "=================================================================="
echo "test_term_wrapper.sh: ALL TESTS PASSED (100% OK)"
echo "=================================================================="
