---
title: Usage & Operation Guide
tags: [usage, bootstrap, tmux-bridge, handoff, operation]
related: ["[[INDEX]]", "[[architecture]]", "[[known-issues]]"]
summary: 템플릿 설정(config.env), 대상 프로젝트에서의 bootstrap/watchdog 실행법, tmux 관찰/개입 명령, Sonnet 매개 agy-Codex 수동 핸드오프 절차.
---

# Usage & Operation Guide

`agent-command-chain-template`의 일상적인 운용, 설정, 제어 명령어, 그리고 에이전트 간 수동 핸드오프 절차를 설명한다.

---

## 1. 사전 준비 및 설정

### 템플릿 클론 및 설정 파일 복사
템플릿 리포지토리는 홈 디렉토리 등에 한 번만 클론해두고 재사용한다.

```bash
git clone https://github.com/loverspath/agent-command-chain-template.git ~/agent-command-chain-template
cd ~/agent-command-chain-template
cp config.env.example config.env
```

### `config.env` 주요 설정 항목

| 변수명 | 기본값 | 설명 |
|---|---|---|
| `PROJECT_DIR` | (주석 처리됨) | 명시하면 고정된 경로로 실행. 비워두면 스크립트 실행 위치가 대상이 됨 |
| `SESSION_NAME` | `agentchain` | tmux 세션 이름. 여러 프로젝트 동시 실행 시 고유하게 변경 |
| `SONNET_WINDOW` | `sonnet` | 계층 1 Claude Sonnet 윈도우 이름 |
| `AGY_WINDOW` | `agy` | 계층 2 agy (Gemini) 윈도우 이름 |
| `CODEX_WINDOW` | `codex` | 계층 2-내부 Tier 2 Codex Terra 윈도우 이름 |
| `SONNET_CMD` | `claude --model sonnet --remote-control` | Sonnet 기동 명령 (RC 필수 전제) |
| `AGY_CMD` | `agy --new-project --mode plan` | agy 대화형 기동 명령 |
| `CODEX_CMD` | `codex --model gpt-5.6-terra -s danger-full-access` | Codex 대화형 TUI 기동 명령 |
| `AUTO_CONFIRM_TRUST` | `true` | 첫 기동 시 디렉토리 신뢰 대화형 프롬프트 자동 승인 여부 |
| `LOG_DIR` | `./logs` | 로그 및 세션 브리핑 저장 디렉토리 |
| `WATCHDOG_INTERVAL` | `30` | 워치독 프로세스 생존 검사 주기 (초) |

---

## 2. 부트스트랩 및 워치독 실행

이 템플릿은 프로젝트 종속적이지 않다. **작업하려는 대상 디렉토리로 이동한 뒤 템플릿 스크립트를 호출**하면 그 위치에서 tmux 창들이 열린다.

```bash
# 1. 작업 대상 프로젝트 디렉토리로 이동
cd /path/to/your/actual/project

# 2. 부트스트랩 스크립트 실행 (tmux 세션 및 3개 창 기동 + 신뢰 확인 자동 처리 + 브리핑 전달)
~/agent-command-chain-template/bootstrap.sh

# 3. (선택) 워치독 백그라운드 기동
~/agent-command-chain-template/watchdog.sh &

# 4. 세션 직접 확인 (필요한 경우)
tmux attach -t agentchain
```

---

## 3. tmux 관찰 및 제어 명령어

컨트롤러(사람 또는 Sonnet의 Bash 툴)는 표준 tmux 명령을 통해 각 창의 상태를 파악하고 지시를 내린다.

### 스냅샷 읽기 (Pull)
터미널 창의 현재 텍스트 스냅샷을 덤프한다:

```bash
# agy 창 화면 스냅샷 읽기
tmux capture-pane -t agentchain:agy -p

# Codex 창 화면 스냅샷 읽기
tmux capture-pane -t agentchain:codex -p

# Sonnet 감독 창 화면 스냅샷 읽기
tmux capture-pane -t agentchain:sonnet -p
```

### 실시간 스트림 로깅 (Async 감지)
`pipe-pane`을 활성화하면 터미널 출력이 실시간으로 지정 파일에 추가된다. 상위 컨트롤러가 로그 파일을 tail 감시(Monitor 툴)할 수 있다:

```bash
# agy 창의 실시간 출력을 로그 파일로 파이프
tmux pipe-pane -o -t agentchain:agy 'cat >> logs/agy.pane.log'

# Codex 창의 실시간 출력을 로그 파일로 파이프
tmux pipe-pane -o -t agentchain:codex 'cat >> logs/codex.pane.log'
```

### 명령 주입 (Push)
지정된 윈도우에 키 입력을 주입한다 (`C-m`은 Enter 키에 해당):

```bash
# agy 창에 지시문 전달
tmux send-keys -t agentchain:agy "현재 프로젝트 파일 구조를 확인하고 분석해라." C-m

# Codex 창에 지시문 전달
tmux send-keys -t agentchain:codex "Specification 문서를 확인하고 테스트를 작성해라." C-m
```

---

## 4. agy → Codex 수동 핸드오프 절차

템플릿 자체에는 agy가 Codex를 직접 spawn하는 자동 라우터가 포함되어 있지 않으므로, **Sonnet(또는 사용자)이 중계 역할**을 수행한다.

### 단계 1: agy 초기 지시
agy 창에 최초 프롬프트를 주입할 때 한계 상황 시의 보고 규칙을 명시한다:

```text
너는 이 프로젝트의 구현 에이전트(메인 세션)다. 먼저 현재 작업 디렉토리와 프로젝트 정체성을 보고해라.
스스로 처리하기 벅찬 하위 작업이 있으면 직접 호출하지 말고 결과 보고에
"Terra급 작업 필요: ..." 라고 명시해서 Sonnet이 codex 창으로 넘길 수 있게 해라.

[핵심 원칙: 메인 세션 직접 작업 금지 및 서브에이전트 위임]
- 너 자신(메인 세션)은 직접 Read/Bash/Edit 등의 툴을 호출해 파일 작업에 몰입하지 마라.
- agy CLI의 `--agent` 서브에이전트 기능으로 워커를 소환해 실작업을 위임하고, 지시/취합/보고만 총괄해라.
- 네 존재 이유는 상위 감독자(Sonnet)의 추가 지시나 인터럽트를 언제든 즉시 수신할 수 있는 대기 상태(responsive state)를 유지하는 데 있다.
- 서브에이전트 소환이 불가능하거나 실패하면 조용히 메인 세션이 직접 처리하지 말고 상위(Sonnet)에 명시 보고해라.
```

### 단계 2: Sonnet의 관찰 및 판단
Sonnet은 `capture-pane` 또는 `logs/agy.pane.log`를 주기적으로 검사하다가 agy 출력에서 `Terra급 작업 필요: <내용>` 패턴을 감지한다.

### 단계 3: Codex Terra로 작업 전달
Sonnet(또는 사람)은 아래 규격에 맞추어 `send-keys`로 `codex` 창에 지시를 주입한다:

```bash
tmux send-keys -t agentchain:codex "Model Role: GPT-5.6 TERRA
Scope: src/core/auth.py

## Specification
agy에서 분리된 토큰 검증 로직 구현 및 엣지 케이스 단위 테스트 작성

## Constraints
- 프로젝트 경계 밖 파일 수정 금지
- 검증/디버깅 시 파일:줄 번호로 정확히 인용" C-m
```

> **참고**: `codex`는 대화형 TUI 모드로 지속 기동되어 있으므로 작업이 끝나면 결과가 화면에 유지되며, 추가 지시를 계속해서 `send-keys`로 전달할 수 있다.

### 단계 4: Tier 3 (Sol / Opus) 최고난도 상담 필요 시
- **Codex Sol 상담**: Codex 창에서 프로세스를 종료하거나 커맨드를 변경하여 일회성으로 호출:
  ```bash
  tmux send-keys -t agentchain:codex C-c
  tmux send-keys -t agentchain:codex "codex --model gpt-5.6-sol -s danger-full-access" C-m
  ```
- **Claude Opus 상담**: Sonnet 세션 내에서 일회성 플래닝/상담 명령을 `--model opus`로 실행.
