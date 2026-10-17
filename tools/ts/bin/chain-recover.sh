#!/usr/bin/env bash
# bin/chain-recover.sh <세션명|런타임ID> [--dry-run] [--session-id <uuid>]
# tmux 서버가 죽어(WSL/윈도우 재부팅 등) 사라진 체인 tmux 세션을 같은 이름으로 재생성하고,
# 감독자 Claude 를 --resume 으로 이어 붙인다. watchdog/agy/codex 워커는 건드리지 않는다.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
want="${1:-}"; shift || true
DRY=false; SID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=true ;;
    --session-id) SID="${2:-}"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac; shift
done
[[ -n "$want" ]] || { echo "Usage: $0 <session-name> [--dry-run] [--session-id <uuid>]" >&2; bin/chain list 2>/dev/null; exit 64; }

# 1) 런타임 탐색: session_name 마커 또는 디렉토리명 접두 일치
RT=""
for d in "$HERE"/runtime/*/; do
  n="$(head -n1 "$d/session_name" 2>/dev/null | tr -d '\r\n')"
  if [[ "$n" == "$want" || "$(basename "$d")" == "$want" ]]; then
    [[ -f "$d/retired" ]] && continue
    # 같은 이름이 여러 개면 가장 최근 수정된 것
    if [[ -z "$RT" || "$d" -nt "$RT" ]]; then RT="${d%/}"; fi
  fi
done
[[ -n "$RT" ]] || { echo "런타임 없음: $want" >&2; exit 70; }
S="$(head -n1 "$RT/session_name" | tr -d '\r\n')"

# 2) 설정 로드 (인스턴스 env 우선, 없으면 config.env)
PROJECT_DIR=""; SESSION_NAME=""; SONNET_CMD="claude --model sonnet --remote-control"
SONNET_WINDOW=sonnet; AGY_WINDOW=agy; CODEX_WINDOW=codex
[[ -f "$HERE/config.env" ]] && source "$HERE/config.env"
[[ -f "$HERE/instances/$S.env" ]] && source "$HERE/instances/$S.env"
P="${PROJECT_DIR:?PROJECT_DIR 미설정}"

if tmux has-session -t "$S" 2>/dev/null; then echo "이미 살아있음: tmux attach -t $S" >&2; exit 0; fi

# 3) 감독자 Claude 트랜스크립트 탐색: PROJECT_DIR 의 프로젝트 폴더에서 이 런타임의 훅을 언급하는 가장 최근 세션
PD="$HOME/.claude/projects/$(printf '%s' "$P" | sed 's#[/._]#-#g')"
if [[ -z "$SID" ]]; then
  f="$(grep -l -F "sonnet-event-wait.sh stop $RT" $(ls -t "$PD"/*.jsonl 2>/dev/null) 2>/dev/null | head -n1)"
  [[ -n "$f" ]] && SID="$(basename "$f" .jsonl)"
fi
echo "session=$S runtime=$RT project=$P supervisor-session-id=${SID:-<없음: 새 세션>}"

RESUME=""; [[ -n "$SID" ]] && RESUME="--resume $SID"
LAUNCH="export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 ACC_RUNTIME=\"$RT\" ACC_TEMPLATE_ROOT=\"$HERE\" SESSION_NAME=\"$S\"; $SONNET_CMD $RESUME --settings \"$RT/claude-bridge.settings.json\" --add-dir \"$HERE\" --append-system-prompt \"\$(cat '$RT/session_brief.md')\""
$DRY && { echo "[dry-run] $LAUNCH"; exit 0; }

export PATH="$HOME/.local/bin:$PATH"
tmux new-session -d -s "$S" -n "$SONNET_WINDOW" -c "$P"
tmux set-environment -t "$S" HOME "$HOME"
tmux set-environment -t "$S" ACC_RUNTIME "$RT"
tmux set-environment -t "$S" ACC_BOOTSTRAP_VERSION 2
for w in "$AGY_WINDOW" "$CODEX_WINDOW"; do
  tmux new-window -t "$S" -n "$w" -c "$P"
  tmux send-keys -t "$S:$w" "export ACC_RUNTIME=\"$RT\" PROJECT_DIR=\"$P\" HERE=\"$HERE\"; cd \"$P\"" C-m
done
tmux send-keys -t "$S:$SONNET_WINDOW" "$LAUNCH" C-m
echo "복구 완료: tmux attach -t $S   (워치독/agy 상주/codex 는 미기동 — 필요시 감독자에게 지시)"
