#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DASHBOARD_STATE_DIR="${DASHBOARD_STATE_DIR:-}"

if [[ -n "$DASHBOARD_STATE_DIR" ]]; then
  DEFAULT_PID_FILE="$DASHBOARD_STATE_DIR/.pid"
  DEFAULT_LOG_FILE="$DASHBOARD_STATE_DIR/dashboard.log"
else
  DEFAULT_PID_FILE="$SCRIPT_DIR/.pid"
  DEFAULT_LOG_FILE="$REPO_DIR/logs/dashboard.log"
fi

PID_FILE="${ACC_DASHBOARD_PID_FILE:-$DEFAULT_PID_FILE}"
LOG_FILE="${ACC_DASHBOARD_LOG_FILE:-$DEFAULT_LOG_FILE}"

ACTION="${1:-}"
if [[ -z "$ACTION" ]]; then
  echo "Usage: $0 <start|stop|status|restart> [--port <port>] [--host <host>] [--runtime <dir>] [--transcript-root <dir>] [--pid-file <file>] [--log-file <file>]" >&2
  exit 1
fi
shift || true

PORT=""
HOST=""
RUNTIME=""
TRANSCRIPT_ROOT="${ACC_TRANSCRIPT_ROOT:-}"
ALLOW_ANY_HOST=false
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      PORT="$2"
      shift 2
      ;;
    --host)
      HOST="$2"
      shift 2
      ;;
    --runtime)
      RUNTIME="$2"
      shift 2
      ;;
    --transcript-root)
      TRANSCRIPT_ROOT="$2"
      shift 2
      ;;
    --pid-file)
      PID_FILE="$2"
      shift 2
      ;;
    --log-file)
      LOG_FILE="$2"
      shift 2
      ;;
    --allow-any-host)
      ALLOW_ANY_HOST=true
      shift
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

get_default_host() {
  local ip
  if command -v tailscale >/dev/null 2>&1; then
    ip="$(tailscale ip -4 2>/dev/null || true)"
    if [[ -n "$ip" && "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      echo "$ip"
      return 0
    fi
  fi
  echo "127.0.0.1"
}

if [[ -z "$PORT" ]]; then
  PORT="8765"
fi

if [[ -z "$HOST" ]]; then
  HOST="$(get_default_host)"
fi

is_server_process() {
  local target_pid="$1"
  if [[ -z "$target_pid" ]] || ! kill -0 "$target_pid" 2>/dev/null; then
    return 1
  fi
  local cmdline=""
  if [[ -f "/proc/$target_pid/cmdline" ]]; then
    cmdline="$(tr '\0' ' ' < "/proc/$target_pid/cmdline" 2>/dev/null || true)"
  fi
  if [[ "$cmdline" == *"dashboard/server.py"* || "$cmdline" == *"server.py"* ]]; then
    return 0
  fi
  return 1
}

do_start() {
  if [[ -f "$PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
      if is_server_process "$existing_pid"; then
        echo "Dashboard server is already running (PID $existing_pid)."
        return 0
      else
        echo "Warning: PID file exists with PID $existing_pid, but process is not dashboard/server.py. Cleaning stale PID file." >&2
        rm -f "$PID_FILE"
      fi
    else
      rm -f "$PID_FILE"
    fi
  fi

  mkdir -p "$(dirname "$PID_FILE")"
  mkdir -p "$(dirname "$LOG_FILE")"

  local cmd_args=("--host" "$HOST" "--port" "$PORT" "--repo" "$REPO_DIR")
  if [[ -n "$RUNTIME" ]]; then
    cmd_args+=("--runtime" "$RUNTIME")
  fi
  if [[ -n "$TRANSCRIPT_ROOT" ]]; then
    cmd_args+=("--transcript-root" "$TRANSCRIPT_ROOT")
  fi
  if [[ "$ALLOW_ANY_HOST" == "true" ]]; then
    cmd_args+=("--allow-any-host")
  fi
  if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
    cmd_args+=("${EXTRA_ARGS[@]}")
  fi

  setsid nohup python3 "$SCRIPT_DIR/server.py" "${cmd_args[@]}" >> "$LOG_FILE" 2>&1 &
  local new_pid=$!
  echo "$new_pid" > "$PID_FILE"

  sleep 0.3
  if kill -0 "$new_pid" 2>/dev/null; then
    echo "Dashboard server started (PID $new_pid) at http://${HOST}:${PORT}"
    return 0
  else
    echo "Error: Dashboard server failed to start (PID $new_pid exited). Check logs at $LOG_FILE" >&2
    rm -f "$PID_FILE"
    return 1
  fi
}

do_stop() {
  if [[ ! -f "$PID_FILE" ]]; then
    echo "Dashboard server is not running (no PID file)."
    return 0
  fi

  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    rm -f "$PID_FILE"
    echo "Removed empty PID file."
    return 0
  fi

  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Dashboard server was not running (stale PID $pid). Removed PID file."
    return 0
  fi

  if ! is_server_process "$pid"; then
    local cmdline=""
    if [[ -f "/proc/$pid/cmdline" ]]; then
      cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    fi
    echo "Warning: PID $pid does not match dashboard/server.py (cmdline: $cmdline). Refusing to kill. Removing stale PID file." >&2
    rm -f "$PID_FILE"
    return 0
  fi

  echo "Stopping dashboard server (PID $pid)..."
  kill "$pid" 2>/dev/null || true

  local waited=0
  while kill -0 "$pid" 2>/dev/null && (( waited < 50 )); do
    sleep 0.1
    waited=$(( waited + 1 ))
  done

  if kill -0 "$pid" 2>/dev/null; then
    echo "Dashboard server did not exit in 5s; sending SIGKILL..."
    kill -9 "$pid" 2>/dev/null || true
    sleep 0.2
  fi

  rm -f "$PID_FILE"
  echo "Dashboard server stopped."
  return 0
}

do_status() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      if is_server_process "$pid"; then
        echo "Dashboard server is running (PID $pid)."
        return 0
      fi
    fi
  fi
  echo "Dashboard server is not running."
  return 1
}

do_restart() {
  do_stop
  do_start
}

case "$ACTION" in
  start)
    do_start
    ;;
  stop)
    do_stop
    ;;
  status)
    do_status
    ;;
  restart)
    do_restart
    ;;
  *)
    echo "Unknown action '$ACTION'. Usage: $0 <start|stop|status|restart> [--port <port>] [--host <host>] [--runtime <dir>] [--transcript-root <dir>]" >&2
    exit 1
    ;;
esac
