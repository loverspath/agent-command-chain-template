---
title: Known Issues
tags: [bug, product-bug, permissions, tmux-nesting, troubleshooting]
related: ["[[INDEX]]", "[[architecture]]", "[[usage]]"]
summary: 실측으로 확인된 문제 6가지와 각각의 원인/대응 — 막히면 여기부터 확인.
---

# Known Issues

실제로 이 템플릿을 여러 프로젝트(demo-project, eraweb-fork, rentplan_hubfork)에
돌려보면서 부딪힌 것들. 재조사하지 말고 여기부터 확인할 것.

## 1. `--dangerously-skip-permissions`로 트러스트 다이얼로그를 못 건너뛴다

claude/codex 둘 다 새 디렉토리에서 "이 폴더를 신뢰하는가" 대화형 확인을
띄우는데, `--dangerously-skip-permissions` 같은 플래그로는 **이 대화형 확인
자체를 못 건너뛴다** (실측 확인, 인터랙티브 모드 기준). 유일하게 문서화된
스킵은 `claude`의 `-p`(비대화형 print 모드)일 때뿐이고, 우리는 지속형 tmux
창(대화형)을 쓰므로 해당 없음.

**대응**: `bootstrap.sh`가 화면에 그 문구가 실제로 뜬 걸 확인한 뒤에만(폴링)
기본 선택지를 자동으로 눌러준다 (`AUTO_CONFIRM_TRUST=true`, 기본값).

**주의**: 처음엔 폴링 대신 고정 `sleep 3`으로 구현했는데, 기동이 느릴 때
타이밍이 어긋나서 "No, exit"가 잘못 눌리고 Sonnet 창이 조용히 종료된 적이
있었다. 반드시 `wait_for_pane_text()`처럼 실제 화면 텍스트를 확인한 뒤에만
키를 보내야 한다.

## 2. `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`이 안 먹는다 (제품 버그)

Sonnet 창이 항상 이 경고를 띄운다:

> ⚠ Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION marker ·
> restart with CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 to keep future transcripts

원인: 이 Sonnet 프로세스가 (Claude Code의 Bash 툴로 tmux를 조작해서 띄운
경우) 프로세스 계보상 부모가 다른 Claude Code 세션인 걸 감지해서 "나는 자식
세션"이라 판단, transcript 저장을 끈다. **env var를 실제로 export하고 shell에
잡힌 것까지 확인한 뒤 재시작해도 경고가 그대로 뜬다** — v2.1.274 기준 이
공식 해결법 자체가 안 먹는 것으로 보임(Anthropic에 제품 버그로 보고함).

**영향**: 무해함. RC/대화 자체는 정상. 다만 이 세션은 나중에
`claude --resume`으로 못 이어붙인다. 현재 우회법 없음 — 그냥 무시.

## 3. agy가 "Windows 전용"이라고 착각했었다 (거짓 결론이었음)

처음에 `command -v agy`가 WSL 비대화형 셸에서 실패해서 "agy는 Linux 빌드가
없다"고 결론 냈었는데, **틀렸다**. 실제로는 `~/.local/bin/agy`에 네이티브
리눅스 빌드가 있었고, 못 찾은 이유는 비대화형 셸(`bash -c`)이 `.bashrc`를 안
읽어서 PATH에 `~/.local/bin`이 없었을 뿐이었다.

**대응**: `bootstrap.sh`가 스스로
`export PATH="$HOME/.local/bin:$PATH"`를 실행해서 이 오탐을 없앴다. 정말
Windows 빌드만 있는 환경이면(agy 버전에 따라 다를 수 있음)
`config.env.example`에 적힌 `cmd.exe /c "cd /d C:\...\ && agy.exe ..."` 우회를
대신 쓴다 — 단, cmd.exe는 UNC 경로(`\\wsl.localhost\...`)로 못 들어가므로
실제 Windows 드라이브 경로가 필요하다.

## 4. codex는 `exec` 서브커맨드가 아니라 대화형 모드로 써야 한다

처음엔 라우터 어댑터의 `codex exec --model ... -s danger-full-access`(one-shot)
형태를 그대로 썼는데, 프롬프트를 안 주면 지속형 대화 없이 stdin만 기다리다
끝나버려서 지속형 tmux 창에 안 맞았다. `exec` 서브커맨드를 뺀 순수 `codex`는
대화형 TUI로 열려서 sonnet/agy와 같은 패턴으로 쓸 수 있다 — 이게 현재 기본값.

모델 이름(`gpt-5.6-terra`, `gpt-5.6-sol`)은 실제 계정
`~/.codex/config.toml` 기본값과 일치 확인됨 — 지어낸 이름 아님.

## 5. 템플릿 밖 파일(session_brief.md) 읽으려다 "작업 디렉토리 밖 읽기" 프롬프트

Sonnet의 작업 디렉토리는 대상 프로젝트인데, 브리핑 파일(`session_brief.md`)은
템플릿 디렉토리에 있어서 "Allow reads outside the working directories?" 확인이
뜬다. **"계속 허용"을 고르면 전역 설정
(`permissions.blockReadsOutsideWorkingDirectories`)이 바뀐다** — 세션 하나 때문에
전역을 바꾸는 건 과함.

**대응**: `SONNET_CMD` 실행 시 `--add-dir "$HERE"`(템플릿 디렉토리)를 자동으로
붙여서, 이 세션 범위에서만 허용한다. 전역 설정은 안 건드림.

## 6. tmux 안에서 tmux attach 하면 Ctrl+b가 먹통처럼 보인다

이미 tmux pane(예: sonnet 창) 안에서 `tmux attach -t agentchain`을 또
실행하면 `sessions should be nested with care, unset $TMUX to force` 경고가
뜨고 중첩 상태가 된다. 이때 Ctrl+b가 바깥/안쪽 어느 tmux로 갈지 애매해져서
창 전환이 안 되는 것처럼 느껴진다.

별개로, **흔한 실수**: Ctrl+b를 누른 채로 숫자까지 같이 누르면 `Ctrl+숫자`로
인식돼서 반응이 없다. Ctrl+b를 눌렀다 **뗀 다음** 숫자만 따로 눌러야 한다
(또는 `Ctrl+b` 뗀 다음 `w`로 목록에서 선택).

## 7. 같은 창에 사람과 자동화가 동시에 send-keys를 보내면 입력이 섞인다

RC로 사람이 타이핑 중인 창에 스크립트/AI가 동시에 `tmux send-keys`를 보내면
tmux 입장에선 구분이 안 되고 순서대로 섞여 들어간다. 메시지가 안 보내지거나
이상하게 잘리는 것처럼 보이는 원인이 될 수 있다.

**대응**: 사람이 활발히 쓰고 있는 걸 확인했으면(예: 입력창에 미전송 텍스트가
보이면) 자동화 쪽에서 그 창에 send-keys를 보내지 말 것 — `capture-pane`
읽기만 하고 기다린다.
