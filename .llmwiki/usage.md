---
title: Usage & Operation Guide
tags: [usage, bootstrap, config, watchdog, tmux-bridge, handoff, operation, usage-manual, dashboard]
related: ["[[INDEX]]", "[[architecture]]", "[[known-issues]]"]
summary: 설치부터 v2 Full-Push/Stage 1 상주/대시보드/v1 레거시 실행, config.env 설정, 관찰/개입 명령, 수동 핸드오프, 정리 절차.
---

# Usage & Operation Guide

`agent-command-chain-template`의 사전 요구사항, 환경 설정, v2/v1 실행법, 관찰 및 제어 명령어, Resident TUI 운용, 대시보드 사용법, 에이전트 간 수동 핸드오프, 그리고 세션 정리 절차를 설명한다.

---

## 1. 사전 요구사항 및 준비

### 환경 확인
| 요구 소프트웨어 | 확인 명령 | 비고 |
|---|---|---|
| tmux | `tmux -V` | 3.x 이상 확인됨 |
| claude CLI | `claude --version` | Pro/Max/Team/Enterprise 유료 구독 + 사전 `claude /login` 완료 필수 (RC 사용 시) |
| agy CLI | `agy --version` | 네이티브 Linux 빌드가 `~/.local/bin/agy`에 설치됨 — [[known-issues]] 참고 |
| codex CLI | `codex --version` | `npm install -g @openai/codex@latest`를 **WSL 안에서 직접** 실행해야 리눅스 네이티브 바이너리가 설치됨 |

### 템플릿 클론 및 실행 권한 부여
템플릿 리포지토리는 홈 디렉토리 등에 한 번만 클론해두고 모든 프로젝트에서 재사용한다.

```bash
git clone https://github.com/loverspath/agent-command-chain-template.git ~/agent-command-chain-template
cd ~/agent-command-chain-template
cp config.env.example config.env
chmod +x bootstrap.sh watchdog.sh bootstrap-v2.sh watchdog-v2.sh bin/*.sh adapters/*.sh dashboard/*.sh
```

---

## 2. config.env 주요 설정 항목

| 변수명 | 기본값 | 설명 |
|---|---|---|
| `PROJECT_DIR` | (주석 처리됨) | 채우면 항상 이 프로젝트로 고정. 비워두면 스크립트를 실행한 디렉토리가 작업 대상이 됨 |
| `SESSION_NAME` | `agentchain` | tmux 세션 기본 이름 (v2 권장 세션명: `agentchain-v2`). 여러 프로젝트 동시 실행 시 고유하게 지정 |
| `SONNET_WINDOW` | `sonnet` | 계층 1 Claude Sonnet 윈도우 이름 |
| `AGY_WINDOW` | `agy` | 계층 2 agy (Gemini) 윈도우 이름 |
| `CODEX_WINDOW` | `codex` | 계층 2-내부 Tier 2 Codex Terra 윈도우 이름 |
| `SONNET_CMD` | `claude --model sonnet --remote-control` | Sonnet 기동 명령. 부트스트랩 시 `--add-dir <템플릿경로>`가 세션 범위로 자동 부가됨 |
| `AGY_CMD` | `agy --new-project --mode plan` | agy 대화형 기동 명령 (Windows 전용 환경인 경우 `cmd.exe` 우회 주석 참고) |
| `CODEX_CMD` | `codex --model gpt-5.6-terra -s danger-full-access` | Codex 대화형 TUI 기동 명령 (`codex exec` one-shot 아님) |
| `AUTO_CONFIRM_TRUST` | `true` | 새 디렉토리 첫 진입 시 디렉토리 신뢰("trust this folder?") 대화형 확인 자동 승인 여부 |
| `LOG_DIR` | `./logs` | 로그 및 자동 생성되는 세션 브리핑(`session_brief.md`) 저장 디렉토리 |
| `WATCHDOG_INTERVAL` | `30` | v1 워치독(`watchdog.sh`) 프로세스 생존 검사 주기 (초) |
| `DEFAULT_TASK_TIMEOUT` | `1800` | v2 작업 최대 허용 시간 (초, 30분) |
| `NO_OUTPUT_WARN_SECONDS` | `900` | v2 워커 출력 무변경 스톨 경고 기준 시간 (초, 15분) |

---

## 3. 부트스트랩 실행

이 템플릿은 프로젝트 종속적이지 않다. **작업하려는 대상 디렉토리로 이동한 뒤 템플릿 스크립트를 호출**하면 해당 디렉토리를 루트로 삼아 세 tmux 창이 열린다.

```bash
# 1. 작업 대상 프로젝트 디렉토리로 이동 (이 디렉토리가 작업 대상이 됨)
cd /path/to/your/actual/project

# 2. [기본 권장 경로: v2 Full-Push 이벤트 통지 브리지]
~/agent-command-chain-template/bootstrap-v2.sh

# 3. [폴백/레거시 경로: v1 순수 pull 브리지]
# ~/agent-command-chain-template/bootstrap.sh
# ~/agent-command-chain-template/watchdog.sh &

# 4. 세션 직접 접속 (필요한 경우)
tmux attach -t agentchain-v2
```

### 부트스트랩 스크립트가 자동으로 수행하는 작업:
1. tmux 세션 생성 및 3개 윈도우(`sonnet`, `agy`, `codex`)를 대상 디렉토리 루트(`-c "$PROJECT_DIR"`)에서 기동.
2. 각 CLI의 "이 폴더를 신뢰하는가?" 대화형 다이얼로그를 화면 텍스트 폴링으로 확인 후 자동 승인 (`AUTO_CONFIRM_TRUST=true`).
3. 이번 실행의 실제 값(세션명, 창 이름, 프로젝트 경로)을 반영한 `logs/session_brief.md` 동적 생성.
4. Sonnet UI의 준비 상태를 감지한 뒤, 브리핑 파일을 열람하라는 오리엔테이션 메시지를 Sonnet 채팅창에 자동 전송.
5. (v2의 경우) `runtime/` 레이아웃 및 `event.fifo`를 초기화하고 `watchdog-v2.sh` 데몬을 백그라운드로 자동 기동.

---

## 4. 제어 및 관찰 명령어

### 4.1 작업 디스패치 (v2 Full-Push 권장)
v2 아키텍처에서 상위 감독자(Sonnet)나 사용자는 파일 기반 디스패처 `bin/dispatch.sh`를 통해 워커에 작업을 지시한다.
세션 오조준 방지를 위해 **`SESSION_NAME=<세션명>`을 명시하는 것을 강력 권장**한다 (생략 시 4계층 우선순위: 환경변수 → 런타임 마커 → 현재 tmux 세션 → config 기본값으로 자동 해석):

```bash
# agy에게 작업 지시 (비대화형 oneshot)
SESSION_NAME=agentchain-v2 ./bin/dispatch.sh agy --prompt-file /path/to/prompt.md

# codex에게 작업 지시 (비대화형 oneshot)
SESSION_NAME=agentchain-v2 ./bin/dispatch.sh codex --prompt-file /path/to/prompt.md --timeout 1800

# 인라인 프롬프트 지시
SESSION_NAME=agentchain-v2 ./bin/dispatch.sh agy --prompt "프로젝트 디렉토리 구조를 분석하라."

# 사전 안전 검증 (dry-run, 작업 디스패치 없이 세션/프로세스 유휴 상태 확인)
SESSION_NAME=agentchain-v2 ./bin/dispatch.sh agy --dry-run
```

#### 체인 컨텍스트 헤더 및 비활성화 (`ACC_NO_BRIEF_HEADER=1`)
`dispatch.sh`는 워커가 체인 규칙(`ROLES.md`) 및 체인 위키(`INDEX.md`)를 즉시 인지할 수 있도록, 디스패치 실행 사본(`$task_dir/prompt.md`) 앞에 1KB 미만의 경량 체인 컨텍스트 헤더를 자동으로 prepend한다 (사용자 원본 프롬프트는 불변).
헤더 주입을 비활성화하고 순수 프롬프트만 전달하려면 환경변수 `ACC_NO_BRIEF_HEADER=1`을 설정한다:

```bash
# 컨텍스트 헤더 없이 순수 프롬프트로 디스패치
ACC_NO_BRIEF_HEADER=1 SESSION_NAME=agentchain-v2 ./bin/dispatch.sh agy --prompt-file /path/to/prompt.md
```

### 4.2 Resident TUI 브리지 운용 (Stage 1: agy 상주)

`AGY_MODE=resident` 모드에서는 agy가 tmux Window 1에 상시 대화형 TUI로 상주하며, 파일 디스패치와 1줄 도어벨 트리거를 통해 작동한다.

#### 1. 머신 전역 훅 설정 (Zero Project Pollution)
대상 프로젝트 리포지토리에 `.agents/hooks.json`을 추가할 필요 없이, 머신 전역 설정 파일(`~/.gemini/config/hooks.json`)에 Stop 훅을 등록한다:

```json
{
  "agent-command-chain-bridge": {
    "Stop": [
      {
        "type": "command",
        "command": "/home/rerun/agent-command-chain-template/bin/agy-stop-hook.sh"
      }
    ]
  }
}
```
> [!NOTE]
> `~/.gemini/config/hooks.json`이 이미 존재하면 상기 `"agent-command-chain-bridge"` 키를 기존 JSON 객체 안에 추가(병합)한다. 훅 제거 시 해당 키 블록만 삭제하면 된다.

#### 2. 상주 세션 기동
```bash
# agy 상주 모드로 v2 세션 기동
AGY_MODE=resident ./bootstrap-v2.sh
```

#### 3. 상주 agy 작업 디스패치
```bash
# 상주 agy에 작업 디스패치 (1줄 트리거가 tmux send-keys로 주입됨)
AGY_MODE=resident SESSION_NAME=agentchain-v2 ./bin/dispatch.sh agy --prompt-file /path/to/prompt.md

# 사전 상태 점검 (dry-run)
AGY_MODE=resident SESSION_NAME=agentchain-v2 ./bin/dispatch.sh agy --dry-run
```

#### 4. 유휴 시점 컨텍스트 소거 (/clear) 및 1줄 재무장
워커가 유휴 상태(`workers/agy.busy` 부재)일 때 대화 맥락을 소거하고 역할을 재장착한다:
```bash
# 1. /clear 전송 후 1초 대기
tmux send-keys -l -t agentchain-v2:agy "/clear" && tmux send-keys -t agentchain-v2:agy Enter
sleep 1

# 2. 1줄 재무장 지침 주입
tmux send-keys -l -t agentchain-v2:agy "ACC_ROLE: You are the resident router. Do not do file writing or coding directly. Always delegate via invoke_subagent and wait."
tmux send-keys -t agentchain-v2:agy Enter
```

### 4.3 처리 완료 이벤트 확인 및 아카이빙 (v2)
Sonnet이 비동기 기상(`asyncRewake`) 리마인더를 수신한 후 이벤트를 확인하고 보관 처리한다:

```bash
# 이벤트 아카이브 (런타임 경로 생략 시 자동 해석 지원)
SESSION_NAME=agentchain-v2 ./bin/event-ack.sh <batch_id>
# 또는 명시적 런타임 전달
./bin/event-ack.sh "$ACC_RUNTIME" <batch_id>
```

### 4.4 상태 모니터링 대시보드 (`dashboard/`)
전체 체인의 활성 상태, 워커 현황, 최근 이벤트, 작업 타임라인을 웹 브라우저에서 관찰할 수 있다:

```bash
# 대시보드 데몬 기동 (기본 Tailscale IP 바인딩, 포트 8765)
~/agent-command-chain-template/dashboard/run.sh start

# 상태 확인
~/agent-command-chain-template/dashboard/run.sh status

# 대시보드 데몬 정지
~/agent-command-chain-template/dashboard/run.sh stop
```

### 4.5 v1 레거시 관찰 및 수동 주입 (순수 pull 폴백)
컨트롤러(사람 또는 Sonnet의 Bash 툴)는 표준 tmux 명령을 통해 각 창의 상태를 파악하고 지시를 내린다.

#### 직접 세션 접속 및 창 전환
```bash
tmux attach -t agentchain                       # v1 세션 접속
# 창 전환: Ctrl+b 를 누르고 손을 완전히 뗀 다음 숫자(0/1/2) — 같이 누르면 안 됨!
# 또는 Ctrl+b 뗀 다음 w → 목록에서 선택
```

#### 스냅샷 읽기 (Pull)
```bash
# agy 창 화면 스냅샷 읽기
tmux capture-pane -t agentchain:agy -p

# Codex 창 화면 스냅샷 읽기
tmux capture-pane -t agentchain:codex -p

# Sonnet 감독 창 화면 스냅샷 읽기
tmux capture-pane -t agentchain:sonnet -p
```

#### 실시간 스트림 로깅 (Async 감지)
`pipe-pane`을 활성화하면 터미널 출력이 실시간으로 지정 파일에 추가된다:
```bash
# agy 창의 실시간 출력을 로그 파일로 파이프
tmux pipe-pane -o -t agentchain:agy 'cat >> logs/agy.pane.log'

# Codex 창의 실시간 출력을 로그 파일로 파이프
tmux pipe-pane -o -t agentchain:codex 'cat >> logs/codex.pane.log'
```

#### 수동 명령 주입 (Push)
지정된 윈도우에 키 입력을 주입한다 (`C-m`은 Enter 키에 해당):
```bash
# agy 창에 지시문 전달
tmux send-keys -t agentchain:agy "현재 프로젝트 파일 구조를 확인하고 분석해라." C-m

# Codex 창에 지시문 전달
tmux send-keys -t agentchain:codex "Specification 문서를 확인하고 테스트를 작성해라." C-m
```

> [!WARNING]
> **입력 충돌 주의**: 사람이 RC나 `tmux attach`로 타이핑 중인 창에 스크립트나 AI가 동시에 `tmux send-keys`를 보내면 두 입력이 섞여 명령어가 깨진다. 사람이 타이핑 중일 때는 `capture-pane`으로 읽기만 하고 키 주입을 대기해야 한다 ([[known-issues]] 참고).

#### v1 워치독 백그라운드 기동
```bash
~/agent-command-chain-template/watchdog.sh &
```
- 세션 또는 창 프로세스가 종료되면 단순 재시작한다. 로그는 `logs/watchdog.log`에 기록된다.

---

## 5. agy → Codex 수동 핸드오프 절차

템플릿 자체에는 agy가 Codex를 직접 spawn하는 자동 라우터가 포함되어 있지 않으므로, **Sonnet(또는 사용자)이 중계 역할**을 수행한다.

### 단계 1: agy 초기 지시
agy 창에 최초 프롬프트를 주입할 때 한계 상황 시의 보고 규칙을 명시한다:

```text
너는 이 프로젝트의 라우터 에이전트다. 먼저 현재 작업 디렉토리와 프로젝트 정체성을 보고해라.
스스로 처리하기 벅찬 하위 작업이 있으면 직접 호출하지 말고 결과 보고에
"Terra급 작업 필요: ..." 라고 명시해서 Sonnet이 codex 창으로 넘길 수 있게 해라.

[v2 핵심 원칙: oneshot 단발 턴 내 invoke_subagent 위임 및 직접 작업 금지]
- 너의 실행 단위는 WORKER_MODE=oneshot의 단발 실행 한 턴이다.
- 너 자신(메인 세션)은 직접 Read/Edit/Bash 등의 툴을 호출해 파일 작업에 몰입하지 마라 (경량 작업 예외 없음).
- 내장 도구 invoke_subagent를 호출해 워커 서브에이전트에게 실작업을 위임하고, 지시/취합/보고만 총괄해라.
- 서브에이전트 소환이 불가능하거나 실패하면 조용히 직접 처리하지 말고 [[BLOCKED <id>]] reason=subagent-unavailable|needs-terra 로 상위(Sonnet)에 명시 보고해라.
(참고: v1 상주 TUI의 responsive state 대기는 legacy이며, TUI 비선점형 입력 큐잉 지연 방지를 위해 v2 oneshot으로 전환됨)
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

---

## 6. 세션 정리 (Clean-up)

```bash
# v2 세션 종료
tmux kill-session -t agentchain-v2

# (v1 세션인 경우)
# tmux kill-session -t agentchain

# 대시보드 종료
~/agent-command-chain-template/dashboard/run.sh stop
```

- `logs/` 디렉토리는 `.gitignore` 처리되어 있어 Git에 커밋되지 않는다 (`session_brief.md` 포함, 매 실행마다 새로 생성됨).
- `runtime/` 디렉토리 역시 `.gitignore` 처리되어 있어 세션별 작업 아티팩트가 리포지토리를 오염시키지 않는다.
