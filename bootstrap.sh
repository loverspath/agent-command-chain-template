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

# bootstrap.sh 자신이 Claude Code의 Bash 툴로 실행되는 경우(이 템플릿을 Claude
# Code더러 대신 돌리게 시켰을 때), 새로 뜨는 Sonnet이 프로세스 계보(부모가
# 결국 그 Claude Code 세션)를 보고 "나는 자식 세션"이라 판단해 transcript
# 저장을 꺼버린다. 공식 안내는 CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 로
# 복원하라는 것인데, 실측(v2.1.274)으로는 이 변수를 셋해도 경고가 그대로
# 뜬다 — 현재 버전에서 안 먹는 것으로 보임(제품 버그로 보고함). 그래도 향후
# 고쳐질 걸 대비해 켜둔다. 지금 당장은 그냥 무해한 경고이고 RC/대화 자체는
# 정상 동작한다 — 다만 이 세션은 나중에 --resume 으로 못 이어붙인다.
# --add-dir 로 템플릿 디렉토리(브리핑 파일이 사는 곳)를 미리 허용해둔다.
# 안 하면 PROJECT_DIR 밖의 session_brief.md 를 읽을 때마다 "작업 디렉토리 밖
# 읽기 허용?" 프롬프트가 뜬다(실측 확인). --add-dir 은 이 세션에만 적용되고
# 전역 설정(permissions.blockReadsOutsideWorkingDirectories)은 안 건드린다.
SONNET_LAUNCH="export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1; $SONNET_CMD --add-dir \"$HERE\""

echo "[$SONNET_WINDOW] Sonnet 기동 + RC on: $SONNET_CMD"
tmux send-keys -t "${SESSION_NAME}:${SONNET_WINDOW}" "$SONNET_LAUNCH" C-m

echo "[$AGY_WINDOW] agy 기동: $AGY_CMD"
tmux send-keys -t "${SESSION_NAME}:${AGY_WINDOW}" "$AGY_CMD" C-m

echo "[$CODEX_WINDOW] Codex Terra 기동: $CODEX_CMD"
tmux send-keys -t "${SESSION_NAME}:${CODEX_WINDOW}" "$CODEX_CMD" C-m

# 새 디렉토리에서 처음 뜰 때 claude/codex 둘 다 "이 폴더를 신뢰하는가" 대화형
# 확인을 띄운다 (--dangerously-skip-permissions 로도 이 대화형 확인은 못
# 건너뛴다는 걸 실측으로 확인함 — 있는 건 -p 비대화형 모드에서만 자동 스킵되는
# 것). 매번 손으로 누르기 귀찮으니 기본값(각 CLI의 "예, 신뢰함" 옵션)을
# 자동으로 눌러준다. 프롬프트가 실제로 뜬 걸 화면에서 확인한 뒤에만 키를
# 보낸다(고정 sleep으로는 느린 기동 시 타이밍이 어긋나 실수로 "No, exit"가
# 눌릴 수 있음을 실측으로 확인함). 이미 신뢰된 디렉토리라 프롬프트 자체가 안
# 뜨면 그냥 아무 것도 안 보낸다. 끄고 싶으면 config.env 에서
# AUTO_CONFIRM_TRUST=false 로.
wait_for_pane_text() {
  local target="$1" pattern="$2" timeout_s="${3:-15}"
  local waited=0
  while (( waited < timeout_s * 2 )); do
    if tmux capture-pane -t "$target" -p 2>/dev/null | grep -qF "$pattern"; then
      return 0
    fi
    sleep 0.5
    waited=$((waited + 1))
  done
  return 1
}

if [[ "${AUTO_CONFIRM_TRUST:-true}" == "true" ]]; then
  if wait_for_pane_text "${SESSION_NAME}:${SONNET_WINDOW}" "trust this folder" 15; then
    tmux send-keys -t "${SESSION_NAME}:${SONNET_WINDOW}" Down C-m   # "No, exit" -> "Yes, I trust this folder"
  fi
  if wait_for_pane_text "${SESSION_NAME}:${CODEX_WINDOW}" "trust the contents" 15; then
    tmux send-keys -t "${SESSION_NAME}:${CODEX_WINDOW}" C-m          # 기본 선택지가 이미 "Yes, continue"
  fi
fi

# Sonnet 자신이 이 tmux 세션의 구조(윈도우 이름, agy/codex 관찰·개입 명령,
# 역할 경계)를 스스로 알아야 감독 역할을 할 수 있다. 실행 시점의 실제 값으로
# 브리핑 파일을 생성해서(세션 이름/프로젝트 경로가 실행마다 달라지므로) 그
# 절대경로를 Sonnet 채팅창에 직접 "메시지로" 보낸다 — 사람이 매번 뭘 읽으라고
# 알려줄 필요가 없어진다.
BRIEF_FILE="$HERE/$LOG_DIR/session_brief.md"
mkdir -p "$(dirname "$BRIEF_FILE")"
cat > "$BRIEF_FILE" <<EOF
# 이 tmux 세션 실제 작동 방식 (자동 생성, $(date '+%Y-%m-%d %H:%M:%S'))

너(Sonnet)는 tmux 세션 \`$SESSION_NAME\`의 감독자다. 창 셋: \`$SONNET_WINDOW\`(너 자신),
\`$AGY_WINDOW\`(agy, 라우터 겸 워커), \`$CODEX_WINDOW\`(Codex Terra, 중난도 설계+직접수행).
대상 프로젝트: \`$PROJECT_DIR\`. 셋 다 그 경로에서 시작됐다.

## 관찰 (pull)
\`\`\`
tmux capture-pane -t $SESSION_NAME:$AGY_WINDOW -p
tmux capture-pane -t $SESSION_NAME:$CODEX_WINDOW -p
\`\`\`

## 실시간 스트림 (async에 가까운 감지)
\`\`\`
tmux pipe-pane -o -t $SESSION_NAME:$AGY_WINDOW 'cat >> $HERE/$LOG_DIR/agy.pane.log'
tmux pipe-pane -o -t $SESSION_NAME:$CODEX_WINDOW 'cat >> $HERE/$LOG_DIR/codex.pane.log'
\`\`\`

## 명령 주입
\`\`\`
tmux send-keys -t $SESSION_NAME:$AGY_WINDOW "여기에 지시문" C-m
tmux send-keys -t $SESSION_NAME:$CODEX_WINDOW "여기에 지시문" C-m
\`\`\`

## 역할 경계 (중요)
- agy는 스스로 난이도를 판단해 작업하지만, **codex(Terra)를 자동으로 부르지 않는다.**
  agy 출력을 보고 "이건 Terra급이다" 싶으면 네가 직접 codex 창에 send-keys 로 넘겨라.
- codex는 대화형(one-shot exec 아님)이라 계속 떠 있다. 작업 하나 끝나면 그 결과를
  읽고 다음 지시를 또 send-keys 하면 된다.
- Sol/Opus(최고난도 상담)는 상시 창이 없다. 필요하면 codex 창 커맨드를
  \`--model gpt-5.6-sol\`로 바꿔서 새로 켜거나, 네 자신을 \`--model opus\`로 일회성
  실행해서 상담을 구해라.
- 사람이 새 명령을 내리면 알맞은 창에 전달하고, 진행상황을 요약해서 보고해라.

## 더 자세한 배경
- $HERE/README.md — 전체 설계 이유와 한계
- $HERE/ROLES.md — 역할별 최초 프롬프트 템플릿, 확장 지점
EOF

if [[ "${AUTO_CONFIRM_TRUST:-true}" == "true" ]] && wait_for_pane_text "${SESSION_NAME}:${SONNET_WINDOW}" "auto mode on" 20; then
  tmux send-keys -t "${SESSION_NAME}:${SONNET_WINDOW}" "이 tmux 세션이 어떻게 동작하는지 먼저 $BRIEF_FILE 를 읽고 파악해라. 그 안의 지시대로 agy/codex 창을 감독해라." C-m
  echo "[$SONNET_WINDOW] 오리엔테이션 메시지 전송함 (참조: $BRIEF_FILE)"
fi

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
