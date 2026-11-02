#!/usr/bin/env bash
# lib/term.sh - Terminal Multiplexer abstraction layer (tmux / herdr)
# Provides 7 core primitives + helper primitives for session/window lifecycle.

ACC_TERM_BACKEND="${ACC_TERM_BACKEND:-${TERM_BACKEND:-tmux}}"

# 1. Check if session exists (0=exists, 1=not exists)
term_session_exists() {
  local sess="${1:-}"
  [[ -z "$sess" ]] && return 1
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    herdr --session "$sess" status 2>/dev/null | grep -q 'status: running'
  else
    tmux has-session -t "$sess" 2>/dev/null
  fi
}

# 2. Check if pane or window is alive
# target can be "<session>:<window>" or "<pane_id>"
term_pane_alive() {
  local target="${1:-}"
  [[ -z "$target" ]] && return 1
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    local sess="${target%%:*}" pane="${target#*:}"
    herdr --session "$sess" pane get "$pane" >/dev/null 2>&1
  else
    if [[ "$target" == *:* ]]; then
      local sess="${target%%:*}" win="${target#*:}"
      tmux list-windows -t "$sess" -F '#{window_name}' 2>/dev/null | grep -qx "$win"
    else
      tmux display-message -p -t "$target" '#{pane_id}' >/dev/null 2>&1
    fi
  fi
}

# 3. Retrieve root process PID of target pane
term_pane_pid() {
  local target="${1:-}"
  [[ -z "$target" ]] && return 1
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    local sess="${target%%:*}" pane="${target#*:}"
    herdr --session "$sess" pane process-info --pane "$pane" 2>/dev/null | \
      sed -nE 's/.*shell_pid=([0-9]+).*/\1/p' | head -n 1
  else
    tmux display-message -p -t "$target" '#{pane_pid}' 2>/dev/null
  fi
}

# 4. Safely send a single line of text with Enter
term_send_line() {
  local target="${1:-}"
  local text="${2:-}"
  if [[ "$text" == *$'\n'* || "$text" == *$'\r'* ]]; then
    echo "[term_send_line] Error: multiline text rejected to prevent injection" >&2
    return 1
  fi
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    local sess="${target%%:*}" pane="${target#*:}"
    herdr --session "$sess" pane send-text "$pane" "$text" && \
    herdr --session "$sess" pane send-keys "$pane" enter
  else
    tmux send-keys -l -t "$target" "$text" && tmux send-keys -t "$target" Enter
  fi
}

# 5. Execute command in target pane (send-keys literal + C-m)
term_run_cmd() {
  local target="${1:-}"
  local cmd="${2:-}"
  if [[ "$cmd" == *$'\n'* || "$cmd" == *$'\r'* ]]; then
    echo "[term_run_cmd] Error: multiline command rejected" >&2
    return 1
  fi
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    local sess="${target%%:*}" pane="${target#*:}"
    herdr --session "$sess" pane run "$pane" "$cmd"
  else
    tmux send-keys -l -t "$target" "$cmd" && tmux send-keys -t "$target" C-m
  fi
}

# 6. Capture tail of terminal output
term_capture_tail() {
  local target="${1:-}"
  local lines="${2:-80}"
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    local sess="${target%%:*}" pane="${target#*:}"
    herdr --session "$sess" pane read "$pane" --lines "$lines" 2>/dev/null
  else
    tmux capture-pane -p -t "$target" -S "-${lines}" 2>/dev/null
  fi
}

# 7. Get current pane ID from environment
term_current_pane_id() {
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    printf '%s' "${HERDR_PANE_ID:-}"
  else
    printf '%s' "${TMUX_PANE:-}"
  fi
}

# --- Helper primitives ---

term_get_env() {
  local sess="$1" key="$2"
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    herdr --session "$sess" env get "$key" 2>/dev/null | cut -d= -f2- || true
  else
    tmux show-environment -t "$sess" "$key" 2>/dev/null | sed -n "s/^${key}=//p" || true
  fi
}

term_set_env() {
  local sess="$1" key="$2" val="$3"
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    herdr --session "$sess" env set "$key" "$val" 2>/dev/null || true
  else
    tmux set-environment -t "$sess" "$key" "$val" 2>/dev/null || true
  fi
}

term_has_window() {
  local sess="$1" win="$2"
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    herdr --session "$sess" pane list 2>/dev/null | grep -q "$win"
  else
    tmux list-windows -t "$sess" -F '#{window_name}' 2>/dev/null | grep -qx "$win"
  fi
}

term_new_session() {
  local sess="$1" win="$2" dir="$3"
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    herdr --session "$sess" start >/dev/null 2>&1
  else
    tmux new-session -d -s "$sess" -n "$win" -c "$dir"
  fi
}

term_new_window() {
  local sess="$1" win="$2" dir="$3"
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    herdr --session "$sess" pane create --name "$win" >/dev/null 2>&1
  else
    tmux new-window -t "$sess" -n "$win" -c "$dir"
  fi
}

term_kill_session() {
  local sess="$1"
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    herdr --session "$sess" stop 2>/dev/null || true
  else
    tmux kill-session -t "$sess" 2>/dev/null || true
  fi
}

term_fg_cmd() {
  local target="$1"
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    local sess="${target%%:*}" pane="${target#*:}"
    herdr --session "$sess" pane process-info --pane "$pane" 2>/dev/null | \
      sed -nE 's/.*current_command=([^ ]+).*/\1/p' | head -n 1
  else
    tmux display-message -p -t "$target" '#{pane_current_command}' 2>/dev/null || true
  fi
}

term_send_key() {
  local target="$1" key="$2"
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    local sess="${target%%:*}" pane="${target#*:}"
    herdr --session "$sess" pane send-keys "$pane" "$key" 2>/dev/null || true
  else
    tmux send-keys -t "$target" "$key" 2>/dev/null || true
  fi
}

term_paste_emergency() {
  local sess="$1" target="$2" text="$3"
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    term_send_line "$target" "$text"
  else
    local sanitized_sess="${sess//[^A-Za-z0-9_-]/_}"
    local buf_name="acc-${sanitized_sess}"
    tmux set-buffer -b "$buf_name" "$text" 2>/dev/null || true
    tmux paste-buffer -d -b "$buf_name" -t "$target" 2>/dev/null || true
  fi
}

term_list_sessions() {
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    herdr list 2>/dev/null || true
  else
    tmux list-sessions -F '#S' 2>/dev/null || true
  fi
}

term_current_session() {
  if [[ "${ACC_TERM_BACKEND:-tmux}" == "herdr" ]]; then
    printf '%s\n' "${HERDR_SESSION:-}"
  else
    tmux display-message -p '#S' 2>/dev/null || true
  fi
}

term_verify_resident_pane() {
  local marker_file="$1"
  [[ ! -f "$marker_file" ]] && return 1

  local res_backend="tmux" res_pane="" res_sess="" res_sock=""
  while IFS= read -r line; do
    case "$line" in
      backend=*) res_backend="${line#backend=}" ;;
      pane_id=*) res_pane="${line#pane_id=}" ;;
      session=*) res_sess="${line#session=}" ;;
      socket_path=*) res_sock="${line#socket_path=}" ;;
    esac
  done < "$marker_file"

  if [[ "$res_backend" == "herdr" ]]; then
    [[ -n "${HERDR_PANE_ID:-}" && "$HERDR_PANE_ID" == "$res_pane" ]] && \
    [[ -n "${HERDR_SESSION:-}" && "$HERDR_SESSION" == "$res_sess" ]] && \
    [[ -n "${HERDR_SOCKET_PATH:-}" && "$HERDR_SOCKET_PATH" == "$res_sock" ]]
  else
    local cur_p
    cur_p="$(term_current_pane_id)"
    [[ -n "$cur_p" && -n "$res_pane" && "$cur_p" == "$res_pane" ]]
  fi
}
