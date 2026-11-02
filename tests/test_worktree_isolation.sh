#!/usr/bin/env bash
# tests/test_worktree_isolation.sh: Tests for Candidate 3 (Optional git worktree isolation)
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib-isolated-env.sh"

init_isolated_env "worktree-iso"
setup_mock_runtime "$TEST_RUNTIME"
export TEST_TMP

echo "=================================================================="
echo "Running test_worktree_isolation.sh (Marker: $TESTMARK)"
echo "Target runtime: $TEST_RUNTIME"
echo "=================================================================="

# Create mock tmux session
tmux new-session -d -s "$TEST_SESSION" -n agy -c "$TEST_TMP"
tmux new-window -t "$TEST_SESSION" -n codex -c "$TEST_TMP"
wait_pane_shell "$TEST_SESSION:agy"
wait_pane_shell "$TEST_SESSION:codex"

trap_cleanup() {
  tmux kill-session -t "$TEST_SESSION" 2>/dev/null || true
  cleanup_isolated_env
}
trap trap_cleanup EXIT

# Case 1: Non-git directory rejection (exit 65)
echo "--- Case 1: Non-git directory rejection ---"
non_git_dir="$TEST_TMP/non_git"
mkdir -p "$non_git_dir"

rc=0
PROJECT_DIR="$non_git_dir" ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" \
  "$TEMPLATE_ROOT/bin/dispatch.sh" codex --worktree --prompt "test" >/dev/null 2>&1 || rc=$?

if [[ "$rc" -ne 65 ]]; then
  echo "FAIL: Expected exit 65 for non-git directory with --worktree, got $rc" >&2
  exit 1
fi
echo "  [PASS] Case 1: Non-git directory properly rejected with exit 65"

# Case 2: Git worktree preparation and context header
echo "--- Case 2: Git worktree preparation and context header ---"
git_repo="$TEST_TMP/repo"
mkdir -p "$git_repo"
git -C "$git_repo" init -b main >/dev/null 2>&1 || { git -C "$git_repo" init >/dev/null 2>&1; git -C "$git_repo" checkout -b main >/dev/null 2>&1 || true; }
git -C "$git_repo" config user.email "test@example.com"
git -C "$git_repo" config user.name "Tester"
echo "hello world" > "$git_repo/file.txt"
git -C "$git_repo" add file.txt
git -C "$git_repo" commit -m "initial commit" >/dev/null

PROJECT_DIR="$git_repo" ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" \
  "$TEMPLATE_ROOT/bin/dispatch.sh" codex --worktree --prompt "Do isolated work" >/dev/null

task_id="$(ls -1td "$TEST_RUNTIME"/tasks/codex-* | head -n 1 | xargs basename)"
wt_dir="$TEST_RUNTIME/worktrees/$task_id"

if [[ ! -d "$wt_dir" ]]; then
  echo "FAIL: Expected worktree directory $wt_dir to exist" >&2
  exit 1
fi

if [[ ! -f "$TEST_RUNTIME/tasks/$task_id/worktree.meta" ]]; then
  echo "FAIL: Expected worktree.meta to exist" >&2
  exit 1
fi

if ! grep -q "WORKTREE_DIR: $wt_dir" "$TEST_RUNTIME/tasks/$task_id/prompt.md"; then
  echo "FAIL: Expected WORKTREE_DIR in prompt.md" >&2
  exit 1
fi

echo "  [PASS] Case 2: Worktree created and metadata bound"

# Clear busy
rm -f "$TEST_RUNTIME/workers/codex.busy"

# Case 3: Parallel writes into separate worktrees
echo "--- Case 3: Parallel worktree isolation ---"
PROJECT_DIR="$git_repo" ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" \
  "$TEMPLATE_ROOT/bin/dispatch.sh" agy --worktree --prompt "Agy worktree work" >/dev/null

agy_task_id="$(ls -1td "$TEST_RUNTIME"/tasks/agy-* | head -n 1 | xargs basename)"
agy_wt_dir="$TEST_RUNTIME/worktrees/$agy_task_id"

if [[ ! -d "$agy_wt_dir" ]]; then
  echo "FAIL: Expected agy worktree directory $agy_wt_dir to exist" >&2
  exit 1
fi

# Simulate modifications in each worktree
echo "modified by codex" >> "$wt_dir/file.txt"
echo "modified by agy" >> "$agy_wt_dir/file.txt"

# Original repo must remain pristine
if ! git -C "$git_repo" diff --exit-code >/dev/null; then
  echo "FAIL: Original repo working tree was modified!" >&2
  exit 1
fi
echo "  [PASS] Case 3: Worktrees modified in isolation without touching main repo"

# Case 4: worktree-clean.sh safety guard and cleanup
echo "--- Case 4: worktree-clean.sh safety guard and cleanup ---"
rc=0
# Worktree has uncommitted modifications; clean without --force must fail
ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" PROJECT_DIR="$git_repo" \
  "$TEMPLATE_ROOT/bin/worktree-clean.sh" "$task_id" >/dev/null 2>&1 || rc=$?

if [[ "$rc" -ne 1 ]]; then
  echo "FAIL: Expected exit 1 when cleaning dirty worktree without --force, got $rc" >&2
  exit 1
fi

# Clean with --force and --delete-branch
ACC_RUNTIME="$TEST_RUNTIME" SESSION_NAME="$TEST_SESSION" PROJECT_DIR="$git_repo" \
  "$TEMPLATE_ROOT/bin/worktree-clean.sh" "$task_id" --force --delete-branch >/dev/null

if [[ -d "$wt_dir" ]]; then
  echo "FAIL: Expected worktree directory $wt_dir to be removed" >&2
  exit 1
fi

if git -C "$git_repo" show-ref --verify --quiet "refs/heads/acc/$task_id" 2>/dev/null; then
  echo "FAIL: Expected branch acc/$task_id to be deleted" >&2
  exit 1
fi
echo "  [PASS] Case 4: worktree-clean refused dirty without force, and cleaned cleanly with force"

echo "=================================================================="
echo "test_worktree_isolation.sh: ALL CASES PASSED"
echo "=================================================================="
exit 0
