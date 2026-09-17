#!/usr/bin/env bash
# 원클릭 부트스트랩: tmux 세션 생성 -> Sonnet/agy/codex 윈도우 기동 -> Sonnet RC on
# WSL Ubuntu 등 일반 Linux 셸에서 실행 (Windows 쪽이 아니라 WSL 내부에서 돌릴 것).
#
# 대상 프로젝트는 "실행 위치" 기준이다: 어느 프로젝트 디렉토리에서 이 스크립트를
# 부르든(직접 경로로 실행하든, PATH에 넣고 별칭으로 부르든) 그 디렉토리가 세
# 창의 시작 위치가 된다. config.env 에 PROJECT_DIR 을 명시하면 그 값이 우선한다.
set -euo pipefail

# agy 등이 ~/.local/bin 에 깔리는 경우가 있는데, 이 스크립트는 비대화형으로
# 실행되어 .bashrc를 안 읽는다 — 여기서 직접 PATH에 넣어 오탐을 없앤다.
export PATH="$HOME/.local/bin:$PATH"

INVOKED_DIR="$PWD"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [[ -f config.env ]]; then
  # shellcheck disable=SC1091
  source config.env
else
  echo "config.env 가 없다. config.env.example 을 복사해서 값을 채워라." >&2
  exit 1
fi

PROJECT_DIR="${PROJECT_DIR:-$INVOKED_DIR}"
if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "PROJECT_DIR '$PROJECT_DIR' 가 존재하지 않는다." >&2
  exit 1
fi
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"

mkdir -p "$LOG_DIR"

command -v tmux >/dev/null 2>&1 || { echo "tmux 가 설치되어 있지 않다."; exit 1; }
command -v claude >/dev/null 2>&1 || { echo "claude CLI 가 PATH 에 없다."; exit 1; }
command -v agy >/dev/null 2>&1 || echo "경고: agy 가 PATH 에 없다 — agy 창은 뜨지만 명령이 실패할 것이다."
command -v codex >/dev/null 2>&1 || echo "경고: codex 가 PATH 에 없다 — codex 창은 뜨지만 명령이 실패할 것이다."

echo "프로젝트 디렉토리: $PROJECT_DIR"

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "tmux 세션 '$SESSION_NAME' 이 이미 존재한다. 재사용한다."
else
  echo "tmux 세션 '$SESSION_NAME' 생성 (윈도우: $SONNET_WINDOW)"
  tmux new-session -d -s "$SESSION_NAME" -n "$SONNET_WINDOW" -c "$PROJECT_DIR"
  tmux new-window -t "$SESSION_NAME" -n "$AGY_WINDOW" -c "$PROJECT_DIR"
  tmux new-window -t "$SESSION_NAME" -n "$CODEX_WINDOW" -c "$PROJECT_DIR"
fi

echo "[$SONNET_WINDOW] Sonnet 기동 + RC on: $SONNET_CMD"
tmux send-keys -t "${SESSION_NAME}:${SONNET_WINDOW}" "$SONNET_CMD" C-m

echo "[$AGY_WINDOW] agy 기동: $AGY_CMD"
tmux send-keys -t "${SESSION_NAME}:${AGY_WINDOW}" "$AGY_CMD" C-m

echo "[$CODEX_WINDOW] Codex Terra 기동: $CODEX_CMD"
tmux send-keys -t "${SESSION_NAME}:${CODEX_WINDOW}" "$CODEX_CMD" C-m

echo ""
echo "완료. 다음으로 확인/개입:"
echo "  tmux attach -t $SESSION_NAME              # 직접 붙기"
echo "  tmux capture-pane -t ${SESSION_NAME}:${SONNET_WINDOW} -p   # Sonnet 화면 읽기"
echo "  tmux capture-pane -t ${SESSION_NAME}:${AGY_WINDOW} -p      # agy 화면 읽기"
echo "  tmux capture-pane -t ${SESSION_NAME}:${CODEX_WINDOW} -p    # Codex 화면 읽기"
echo ""
echo "RC 활성화는 Sonnet 창에서 '/remote-control' 또는 프롬프트에 뜨는 '/rc active'"
echo "표시로 확인해라. 뜨지 않으면 claude.ai 구독 등급/로그인 상태를 확인할 것"
echo "(README.md §5 참고)."
