---
title: Usage
tags: [bootstrap, config, watchdog, usage-manual]
related: ["[[INDEX]]", "[[architecture]]", "[[known-issues]]"]
summary: 설치부터 실행, config.env 옵션, 관찰/개입 명령, 정리까지 실제 사용 절차.
---

# Usage

## 사전 요구사항

| 요구 | 확인 명령 | 비고 |
|---|---|---|
| tmux | `tmux -V` | 3.x 이상 확인됨 |
| claude CLI | `claude --version` | Pro/Max/Team/Enterprise 구독 + `claude /login` 완료 필요 (RC 쓰려면) |
| agy CLI | `agy --version` | 네이티브 Linux 빌드가 `~/.local/bin/agy`류에 설치되는 경우 있음 — [[known-issues]] 참고 |
| codex CLI | `codex --version` | `npm install -g @openai/codex@latest`를 **WSL 안에서 직접** 실행해야 리눅스 네이티브 빌드가 깔림 |

## 1. 클론

```bash
git clone https://github.com/loverspath/agent-command-chain-template.git ~/agent-command-chain-template
cd ~/agent-command-chain-template
cp config.env.example config.env
chmod +x bootstrap.sh watchdog.sh
```

## 2. config.env 주요 옵션

| 변수 | 기본값 | 설명 |
|---|---|---|
| `PROJECT_DIR` | (비움) | 채우면 항상 이 프로젝트로 고정. 비워두면 `bootstrap.sh`를 실행한 디렉토리가 대상 |
| `SESSION_NAME` | `agentchain` | tmux 세션 이름. 동시에 여러 프로젝트 돌리려면 프로젝트마다 다르게 |
| `SONNET_CMD` | `claude --model sonnet --remote-control` | `--add-dir <템플릿경로>`가 `bootstrap.sh`에서 자동으로 덧붙음 |
| `AGY_CMD` | `agy --new-project --mode plan` | Windows 전용 빌드만 있는 환경이면 주석에 적힌 `cmd.exe` 우회로 교체 |
| `CODEX_CMD` | `codex --model gpt-5.6-terra -s danger-full-access` | `codex exec`(one-shot) 아님 — 대화형 지속 모드 |
| `AUTO_CONFIRM_TRUST` | `true` | 새 디렉토리의 "이 폴더 신뢰?" 대화형 확인을 자동으로 눌러줌 |
| `LOG_DIR` | `./logs` | watchdog 로그 + 매 실행마다 자동생성되는 `session_brief.md` 위치 |
| `WATCHDOG_INTERVAL` | `30` | watchdog.sh 폴링 주기(초) |

## 3. 실행

```bash
cd /path/to/your/actual/project      # 이 디렉토리가 대상이 됨 (PROJECT_DIR 안 채웠을 때)
~/agent-command-chain-template/bootstrap.sh
```

이게 자동으로 하는 일:
1. tmux 세션 생성, 세 창(sonnet/agy/codex) 기동
2. 각 CLI의 "이 폴더 신뢰?" 대화형 확인을 실제로 뜬 걸 폴링으로 확인한 뒤 자동 승인
3. `logs/session_brief.md`를 이번 실행의 실제 값(세션명, 창 이름, 프로젝트 경로)으로
   생성
4. Sonnet의 UI가 준비된 걸 확인한 뒤, 그 브리핑 파일을 읽으라는 메시지를
   Sonnet 채팅창에 직접 보냄 — 사람이 매번 설명 안 해줘도 Sonnet이 스스로
   감독 역할을 파악함

## 4. 관찰/개입

```bash
tmux attach -t agentchain                       # 직접 들어가서 보기
# 창 전환: Ctrl+b 를 누르고 손을 뗀 다음 숫자(0/1/2) — 같이 누르면 안 됨!
# 또는 Ctrl+b 뗀 다음 w → 목록에서 선택

tmux capture-pane -t agentchain:agy -p           # agy 화면 스냅샷 읽기
tmux capture-pane -t agentchain:codex -p         # codex 화면 스냅샷 읽기
tmux send-keys -t agentchain:agy "지시문" C-m    # agy에 명령 주입

tmux pipe-pane -o -t agentchain:agy \
  'cat >> logs/agy.pane.log'                     # 실시간 스트림 (async 감지용)
```

⚠ **같은 창에 사람(RC/attach)과 스크립트/AI가 동시에 send-keys를 보내면
입력이 섞인다** — 둘 다 그냥 "이 pane에 들어온 키 입력"이라 tmux가 구분 못 함.
자세한 건 [[known-issues]].

## 5. watchdog (선택, 느슨한 자동복구)

```bash
~/agent-command-chain-template/watchdog.sh &
```

세션/창이 죽으면 그냥 재기동한다. 로그: `logs/watchdog.log`. 리스/하트비트
같은 엄격한 장애복구는 없음 — [[architecture]]의 "watchdog.sh" 절 참고.

## 6. 정리

```bash
tmux kill-session -t agentchain
```

`logs/`는 `.gitignore` 처리되어 있어 커밋 안 됨(`session_brief.md` 포함,
매 실행마다 새로 생성되므로).
