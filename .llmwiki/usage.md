---
title: Usage & Operation Guide
tags: [usage, bootstrap, config, watchdog, tmux-bridge, handoff, operation, usage-manual, dashboard]
related: ["[[INDEX]]", "[[architecture]]", "[[known-issues]]", "[[session-resume]]"]
summary: 설치부터 v2 Full-Push/Stage 1 상주/대시보드/v1 레거시 실행, config.env 설정, 관찰/개입 명령, 수동 핸드오프, 세션 재개 런북, 정리 절차.
---

# Usage & Operation Guide

`agent-command-chain-template`의 사전 요구사항, 환경 설정, v2/v1 실행법, 관찰 및 제어 명령어, Resident TUI 운용, 대시보드 사용법, 에이전트 간 수동 핸드오프, 그리고 세션 정리 절차를 설명한다.
신규 세션은 [[session-resume]], 감독자 판단은 [SUPERVISOR](../SUPERVISOR.md), 문서 층과 프로젝트 선택은 [참조 경로](../docs/INSTRUCTION-LAYERS.md)를 읽는다.

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

## 2. 설정 체계 및 계층화 (2-Tier Configuration)

템플릿은 단일 체크아웃에서 복수의 에이전트 체인을 안전하게 운용할 수 있도록 2계층 설정 구조(`lib/config.sh::acc_load_config`)를 제공한다.

### 2.1 계층화 구조 및 우선순위
1. **Tier 1 (공통 기본값)**: `config.env` (또는 환경변수 `ACC_CONFIG_ENV`)
   - 모든 체인 인스턴스가 공유하는 공통 기본 설정.
2. **Tier 2 (인스턴스별 오버라이드)**: `instances/<id>.env` (git 무시)
   - `ACC_INSTANCE` 환경변수가 설정되어 있거나 런타임 표식 파일(`$ACC_RUNTIME/instance`)이 존재할 때 자동 로드.
   - Tier 1의 동일 키를 오버라이드하며, 인스턴스 전용 설정(`SESSION_NAME`, `PROJECT_DIR`, `WORK_DIR`, `HANDOFF_FILE`, `DASHBOARD_PORT` 등)을 정의.
3. **환경변수 최우선 원칙**:
   - 호출 프로세스 셸에 명시된 환경변수(`SESSION_NAME=...`, `ACC_RUNTIME=...` 등)는 파일 설정(Tier 1 & Tier 2)보다 항상 우선.

### 2.2 인스턴스 로딩 규칙 (`lib/config.sh`)
- **인스턴스 미지정 시 (하위 호환)**: `ACC_INSTANCE`와 `$ACC_RUNTIME/instance`가 모두 없으면 기존과 100% 동일하게 `config.env`만 로드됨.
- **인스턴스 ID 검증**: ID는 영숫자, 밑줄, 하이픈(`^[A-Za-z0-9_-]+$`)만 허용. 유효하지 않은 문자나 빈 표식 파일은 stderr 경고 후 `exit 70`(fail-closed).
- **인스턴스 파일 부재 시 거부**: 인스턴스 ID가 결정되었으나 해당 `instances/<id>.env` 파일이 없으면 `config.env`로 조용히 폴백하지 않고 stderr 에러 출력 후 `exit 70`으로 즉시 거부 (침묵 실패 방지).
- **성공 시 export**: 정상 로드 시 `ACC_INSTANCE`가 export되어 하위 프로세스에 전파됨.

### 2.3 주요 설정 항목

이 표는 템플릿 예제 기본값이다. 사용자 인스턴스 모델 선호와 구별한다. `AGY_TIER`·`AGY_MODEL`은 기본 미지정, `AGY_EFFORT`는 high, `ACC_TERM_BACKEND=tmux`, `ACC_PROACTIVE_MODE=observe`다. 새 예제의 키가 기존 로컬 설정에 자동 병합되지는 않는다. 문서 층과 실제 인스턴스 조율은 [참조 경로](../docs/INSTRUCTION-LAYERS.md)에 둔다.


| 변수명 | 기본값 | 설명 |
|---|---|---|
| `PROJECT_DIR` | (주석 처리됨) | 채우면 항상 이 프로젝트로 고정. 비워두면 스크립트를 실행한 디렉토리가 작업 대상이 됨 |
| `SESSION_NAME` | `agentchain` | tmux 세션 기본 이름 (v2 권장 세션명: `agentchain-v2`). 여러 프로젝트 동시 실행 시 고유하게 지정 |
| `SONNET_WINDOW` | `sonnet` | 계층 1 Claude Sonnet 윈도우 이름 |
| `AGY_WINDOW` | `agy` | 계층 2 agy (Gemini) 윈도우 이름 |
| `CODEX_WINDOW` | `codex` | Codex 워커 슬롯 이름(리드 배정과 별개) |
| `SONNET_CMD` | `claude --model sonnet --remote-control` | Sonnet 기동 명령. 부트스트랩 시 `--add-dir <템플릿경로>`가 세션 범위로 자동 부가됨 |
| `AGY_CMD` | `agy --new-project --mode plan` | agy 대화형 기동 명령 (v1 레거시 상주용) |
| `AGY_RESIDENT_CMD` | `agy --dangerously-skip-permissions` | agy 상주 TUI 기동 명령 (Stage 1 상주 모드) |
| `CODEX_CMD` | `codex --model $(bin/resolve-model.sh sol) -s danger-full-access` | Codex 대화형 TUI 기동 명령 (v1 레거시/폴백 전용, v2에서는 adapters/codex-oneshot.sh 사용) |
| `CODEX_TIER` | `sol` | Codex 모델 티어 (`bin/resolve-model.sh`를 통해 최신 list 모델 자동 해석) |
| `CODEX_MODEL` | (미지정) | 명시적 Codex 모델 지정 시 `bin/resolve-model.sh` 자동 해석 우회 (비상용 수동 오버라이드, 평소 비워둠) |
| `CODEX_EFFORT` | `medium` | Codex 추론 강도 (reasoning effort). 사용자 결정에 따라 상위 티어(Sol 등) 모델은 medium 추론 강도 고정 |
| `AUTO_CONFIRM_TRUST` | `true` | 새 디렉토리 첫 진입 시 디렉토리 신뢰("trust this folder?") 대화형 확인 자동 승인 여부 |
| `LOG_DIR` | `./logs` | 로그 및 자동 생성되는 세션 브리핑(`session_brief.md`) 저장 디렉토리 |
| `WATCHDOG_INTERVAL` | `60` | 워치독 프로세스 생존 검사 주기 (초) |
| `START_WATCHDOG` | `true` | `bootstrap-v2.sh` 실행 시 `watchdog-v2.sh` 백그라운드 자동 기동 여부 |
| `BRIDGE_MODE` | `push` | v2 Full-Push 이벤트 통지 브리지 모드 (push 고정) |
| `WORKER_MODE` | `oneshot` | 기본 워커 실행 모드 (`oneshot` 또는 `resident`) |
| `AGY_MODE` | `oneshot` | agy 워커 개별 모드 (미지정 시 `WORKER_MODE` 상속, Stage 1은 `resident` 지원) |
| `CODEX_MODE` | `oneshot` | codex 워커 개별 모드 (Stage 1은 `oneshot` 고정) |
| `HANDOFF_FILE` | (비워둠) | 세션 재개 시 자동 참조할 프로젝트 인계 파일 경로 (선택 사항) |
| `DEFAULT_TASK_TIMEOUT` | `1800` | v2 작업 최대 허용 시간 (초, 30분) |
| `NO_OUTPUT_WARN_SECONDS` | `900` | v2 워커 출력 무변경 스톨 경고 기준 시간 (초, 15분) |
| `EVENT_DELIVERY_GRACE` | `120` | pending 이벤트 전달 지연 유예 시간 (초, 2분) |
| `EVENT_CLAIM_TIMEOUT` | `30` | 이벤트 선점(claiming) 타임아웃 (초, 30초) |
| `EVENT_ACK_TIMEOUT` | `600` | 이벤트 확인 응답(ack) 타임아웃 (초, 10분) |
| `EVENT_BATCH_MAX` | `8` | 1회 알림당 최대 이벤트 묶음 수 |
| `EVENT_TAIL_BYTES` | `1600` | 이벤트 요약 시 로그 꼬리 추출 바이트 수 |
| `WATCHDOG_AUTO_KILL` | `false` | 마감 초과 시 강제 kill 여부 (기본 false: 모델 판단 위임) |

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
5. (v2의 경우) `runtime/` 레이아웃 및 `event.fifo`를 초기화하고 `watchdog-v2.sh` 데몬을 백그라운드로 자동 기동. 로그는 `$ACC_RUNTIME/watchdog.log`에 격리 기록된다 (상세 규약은 [[architecture#다중-인스턴스-런타임-범위화-규약-s2]] 참고).

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

#### 4. 상주 완료 판정 및 규약 D (Sentinel Protocol)

판정·훅 구현은 [[architecture#28-resident-tui-브리지-stage-1-agy-상주--codex-oneshot]], 워커 보고는 [ROLES](../ROLES.md)가 정본이다. dispatch는 agy 실행 사본에 규약 D를 자동 주입한다. 필드 전체 자동생성과는 다르다.

#### 5. 유휴 시점 컨텍스트 소거 (/clear) 및 1줄 재무장
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

### 4.4a 체인 여러 개 운용 (`bin/chain`)
- 인스턴스 ID = tmux 세션명. 새 체인: `instances/<세션명>.env`(`SESSION_NAME`, `PROJECT_DIR`, `WORK_DIR`, `HANDOFF_FILE`)를 만들고 `ACC_INSTANCE=<세션명> ./bootstrap-v2.sh`. 템플릿 체크아웃은 하나를 공유한다.
- 목록·상태: `bin/chain list|status [id]` (세션 생존, tmux env 일치, 워치독/리스너 pid, pending/busy, retired). 은퇴: `bin/chain retire <id>`는 `$ACC_RUNTIME/retired` 표식만 쓰며 워치독이 종료된다. tmux 세션 종료는 직접 한다.
- 안전장치: 상속된 `ACC_RUNTIME`이 다른 세션 소속이면 해석이 exit 70으로 거부하고, `bootstrap-v2.sh`는 다른 런타임 소속의 기존 세션을 보완하거나 `--restart` 하지 않는다(exit 70). 워치독의 런타임 범위 규칙은 architecture.md §2.5.

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
- 세션 또는 창 프로세스가 종료되면 단순 재시작한다. 로그는 `logs/watchdog.log`에 기록된다. (v2의 경우 워치독 로그 정본은 `$ACC_RUNTIME/watchdog.log`에 기록되며 세션명이 접두됨)

---

## 5. 역할·핸드오프

현행 역할 배분·경중·폴백 판단은 [SUPERVISOR](../SUPERVISOR.md), 보고는 [ROLES](../ROLES.md)에만 둔다. v2는 위 §4.1의 dispatch와 §4.3의 이벤트 ACK를 사용한다. v1 화면·send-keys 예시는 §4.5의 레거시 참고이며 v2 작업 지시로 사용하지 않는다.

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
