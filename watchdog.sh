#!/usr/bin/env bash
# 느슨한 워치독: tmux 세션/윈도우가 죽어있으면 재시작만 한다.
# 리스, 하트비트, 스티키 라우팅 같은 엄격한 상태머신은 없음 (의도된 설계 —
# eraweb-fork의 무거운 헬스 상태머신 대신 "죽으면 다시 켠다" 수준).
set -uo pipefail

INVOKED_DIR="$PWD"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# shellcheck disable=SC1091
source config.env
PROJECT_DIR="${PROJECT_DIR:-$INVOKED_DIR}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/watchdog.log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# 창의 현재 실행 커맨드가 셸로 돌아와 있으면 "죽은 것"으로 간주하고 재기동한다.
# (claude/agy 프로세스가 종료되면 pane_current_command 가 bash/zsh 등으로 바뀐다)
restart_if_dead() {
  local window="$1" launch_cmd="$2"
  local cur
  cur="$(tmux display-message -p -t "${SESSION_NAME}:${window}" '#{pane_current_command}' 2>/dev/null || echo "MISSING")"

  case "$cur" in
    MISSING)
      log "윈도우 ${window} 없음 — 새로 생성 (cwd: $PROJECT_DIR)"
      tmux new-window -t "$SESSION_NAME" -n "$window" -c "$PROJECT_DIR"
      tmux send-keys -t "${SESSION_NAME}:${window}" "$launch_cmd" C-m
      ;;
    bash|zsh|sh|-bash|-zsh|-sh)
      log "윈도우 ${window} 프로세스 죽음(현재: $cur) — 재기동: $launch_cmd"
      tmux send-keys -t "${SESSION_NAME}:${window}" "$launch_cmd" C-m
      ;;
    *)
      : # 살아있음, 아무것도 안 함
      ;;
  esac
}

log "워치독 시작 (세션=$SESSION_NAME, 주기=${WATCHDOG_INTERVAL}s)"

while true; do
  if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    log "세션 '$SESSION_NAME' 자체가 없음 — bootstrap.sh 재실행 (cwd: $PROJECT_DIR)"
    PROJECT_DIR="$PROJECT_DIR" ./bootstrap.sh >> "$LOG_FILE" 2>&1
  else
    restart_if_dead "$SONNET_WINDOW" "$SONNET_CMD"
    restart_if_dead "$AGY_WINDOW" "$AGY_CMD"
    restart_if_dead "$CODEX_WINDOW" "$CODEX_CMD"
  fi
  sleep "$WATCHDOG_INTERVAL"
done
