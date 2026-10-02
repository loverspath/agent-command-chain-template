#!/usr/bin/env bash
# bin/task-abandon.sh: Stale lease recovery script
# Usage: task-abandon.sh <task_id|worker> [--force]
set -euo pipefail
umask 077

force=false
target=""

for arg in "$@"; do
  case "$arg" in
    --force|-f)
      force=true
      ;;
    -h|--help)
      echo "Usage: $0 <task_id|worker> [--force]" >&2
      exit 0
      ;;
    *)
      if [[ -z "$target" ]]; then
        target="$arg"
      else
        echo "Error: Unexpected argument '$arg'" >&2
        echo "Usage: $0 <task_id|worker> [--force]" >&2
        exit 64
      fi
      ;;
  esac
done

if [[ -z "$target" ]]; then
  echo "Usage: $0 <task_id|worker> [--force]" >&2
  exit 64
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ENV_SESSION_NAME="${SESSION_NAME:-}"
ENV_ACC_RUNTIME="${ACC_RUNTIME:-}"

# shellcheck disable=SC1091
source "$HERE/lib/config.sh"
acc_load_config "$HERE" || exit $?
CONFIG_SESSION_NAME="${SESSION_NAME:-agentchain}"

if [[ -z "$ENV_ACC_RUNTIME" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/lib/session.sh"
  resolve_session_and_runtime "task-abandon" "" false
  runtime="$ACC_RUNTIME"
else
  runtime="$ENV_ACC_RUNTIME"
fi

if [[ -z "$runtime" || ! -d "$runtime" ]]; then
  echo "Error: Runtime directory not found: $runtime" >&2
  exit 66
fi

# Determine task_id and worker
task_id=""
worker=""

if [[ "$target" == "agy" || "$target" == "codex" ]]; then
  worker="$target"
  busy_file="$runtime/workers/$worker.busy"
  if [[ -f "$busy_file" ]]; then
    task_id="$(head -n 1 "$busy_file" 2>/dev/null || true)"
  fi
  if [[ -z "$task_id" && -d "$runtime/tasks" ]]; then
    # Find most recent running task for this worker
    for t_dir in $(ls -dt "$runtime/tasks/${worker}-"* 2>/dev/null || true); do
      if [[ -f "$t_dir/state" ]] && grep -q '^status=running' "$t_dir/state"; then
        task_id="$(basename "$t_dir")"
        break
      fi
    done
  fi
  if [[ -z "$task_id" ]]; then
    echo "Error: No active or running task found for worker '$worker'" >&2
    exit 1
  fi
else
  task_id="$target"
fi

task_dir="$runtime/tasks/$task_id"
state_file="$task_dir/state"

if [[ ! -d "$task_dir" ]]; then
  echo "Error: Task directory not found: $task_dir" >&2
  exit 66
fi

if [[ ! -f "$state_file" ]]; then
  echo "Error: State file not found: $state_file" >&2
  exit 66
fi

cur_status="$(grep '^status=' "$state_file" 2>/dev/null | head -n 1 | cut -d= -f2- || true)"
if [[ -z "$worker" ]]; then
  worker="$(grep '^worker=' "$state_file" 2>/dev/null | head -n 1 | cut -d= -f2- || true)"
  if [[ -z "$worker" ]]; then
    case "$task_id" in
      agy-*) worker="agy" ;;
      codex-*) worker="codex" ;;
    esac
  fi
fi
cur_pid="$(grep '^pid=' "$state_file" 2>/dev/null | head -n 1 | cut -d= -f2- || true)"
cur_proc_token="$(grep '^proc_token=' "$state_file" 2>/dev/null | head -n 1 | cut -d= -f2- || true)"

# Check if already terminal
case "$cur_status" in
  done|error|canceled|abandoned|failed)
    echo "Notice: Task '$task_id' is already in terminal state '$cur_status'. No modification made."
    # Even if terminal, clean stale busy lease if matching
    if [[ -n "$worker" && -f "$runtime/workers/$worker.busy" ]]; then
      if grep -qx "$task_id" "$runtime/workers/$worker.busy" 2>/dev/null; then
        rm -f "$runtime/workers/$worker.busy"
      fi
    fi
    exit 0
    ;;
esac

# Check if worker PID is still alive
if [[ -n "$cur_pid" && "$cur_pid" =~ ^[0-9]+$ ]] && (( cur_pid > 0 )); then
  if kill -0 "$cur_pid" 2>/dev/null; then
    live_token="$(awk '{print $22}' "/proc/$cur_pid/stat" 2>/dev/null || echo "")"
    if [[ -z "$cur_proc_token" || -z "$live_token" || "$cur_proc_token" == "$live_token" ]]; then
      if [[ "$force" != "true" ]]; then
        echo "Error: Worker process for task '$task_id' (PID $cur_pid) is still alive. Refusing to abandon without --force." >&2
        exit 1
      else
        echo "Warning: Worker process (PID $cur_pid) is alive; terminating with SIGTERM due to --force..." >&2
        kill -TERM "$cur_pid" 2>/dev/null || true
      fi
    fi
  fi
fi

# Atomic write state file with status=abandoned
tmp_state="$state_file.tmp.$$"
python3 - "$state_file" "$tmp_state" <<'PY'
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]
with open(src_path, "r", encoding="utf-8", errors="replace") as f:
    lines = f.readlines()

new_lines = []
status_replaced = False
for line in lines:
    if line.startswith("status="):
        new_lines.append("status=abandoned\n")
        status_replaced = True
    else:
        new_lines.append(line)

if not status_replaced:
    new_lines.append("status=abandoned\n")

with open(dst_path, "w", encoding="utf-8") as f:
    f.writelines(new_lines)
PY

mv "$tmp_state" "$state_file"

# Compatible with dispatch.sh lease cleanup
if [[ -n "$worker" ]]; then
  busy_file="$runtime/workers/$worker.busy"
  if [[ -f "$busy_file" ]]; then
    if grep -qx "$task_id" "$busy_file" 2>/dev/null || grep -q "^$task_id" "$busy_file" 2>/dev/null; then
      rm -f "$busy_file"
      echo "Cleared busy lease file: $busy_file"
    fi
  fi
  lock_dir="$runtime/workers/$worker.lock"
  if [[ -d "$lock_dir" ]]; then
    lock_pid="$(cat "$lock_dir/pid" 2>/dev/null || echo "")"
    if [[ -z "$lock_pid" ]] || ! kill -0 "$lock_pid" 2>/dev/null; then
      rm -rf "$lock_dir" 2>/dev/null || true
    fi
  fi
fi

# Emit canceled event for bridge notification
if [[ -x "$HERE/bin/event-emit.sh" && -n "$worker" ]]; then
  "$HERE/bin/event-emit.sh" "${worker:-watchdog}" canceled "$task_id" "Task was abandoned" "$state_file" >/dev/null 2>&1 || true
fi

echo "Task '$task_id' successfully marked as abandoned."
exit 0
