---
title: Session Resume & Environment Reproduction Runbook
tags: [session-resume, runbook, operation, tmux-bridge, handoff, troubleshooting, supervisor-norms]
related: ["[[INDEX]]", "[[usage]]", "[[architecture]]", "[[known-issues]]"]
summary: 신규 Claude CLI 세션에서 agent-command-chain-template 환경을 완벽히 재현하고, 세션 분기 시나리오별 대응, stale lease 복구, 프로젝트 핸드오프 절차 및 감독관 행동 규범을 안내하는 운영 런북.
---

# Session Resume & Environment Reproduction Runbook

이 문서는 새로 기동된 Claude CLI 세션(또는 tmux 외부/내부의 신규 세션)이 `agent-command-chain-template` 환경을 완벽하게 재현하고, 진행 중이던 프로젝트 작업을 안전하게 이어받아 재개(Resume)하기 위한 운영 지침서(Runbook)이다.

---

## 1. 사전 요구사항 점검 (Prerequisite Checks)

작업을 시작하기 전, 다음 항목들을 순서대로 점검하여 런타임 환경이 온전한지 확인한다.

### 1.1 CLI 도구 및 버전 확인
```bash
# 1. tmux 버전 확인 (3.x 이상 권장)
tmux -V

# 2. Claude CLI 버전 및 인증 상태 확인
# - Claude Code Remote Control(RC) 사용을 위해서는 claude.ai Pro/Max/Team/Enterprise 유료 구독이 필수이다.
# - ANTHROPIC_API_KEY 환경변수 단독 인증은 불가하며, 사전에 `claude /login` OAuth 로그인이 완료되어 있어야 한다.
claude --version

# 3. Antigravity CLI (agy) 버전 확인
# - 네이티브 Linux 빌드가 ~/.local/bin/agy 에 위치해야 하며 PATH에 등록되어 있어야 한다.
agy --version

# 4. Codex CLI 버전 및 인증 확인
# - WSL 터미널 환경에서 `npm install -g @openai/codex@latest` 로 설치된 네이티브 바이너리여야 한다.
# - codex login 완료 상태여야 한다.
codex --version
```

### 1.2 머신 전역 Antigravity Stop 훅 점검
Antigravity CLI가 작업 완료 시 자동으로 Full-Push 브리지에 통지할 수 있도록, 머신 전역 설정 파일(`~/.gemini/config/hooks.json`)에 Stop 훅이 등록되어 있는지 확인한다:

```bash
cat ~/.gemini/config/hooks.json
```
아래와 같이 **`command` 경로가 `bin/agy-stop-hook.sh` 인 Stop 훅**이 등록되어 있어야 한다. 최상위 키 이름은 동작과 무관하다(이 머신의 실제 키는 `acc-resident-bridge`). 점검은 키 이름이 아니라 command 경로로 한다 (`bin/session-init.sh --check` 가 자동 판정):
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
누락되어 있다면 해당 경로의 스크립트를 Stop 훅으로 등록한다.

### 1.3 Codex Sol 모델 해석 및 쿼터 확인
Codex 작업 디스패치는 Sol 계열 최신 모델을 자동 해석(`bin/resolve-model.sh sol`)하여 선택하며, 수동 오버라이드(`CODEX_MODEL`)는 비상시에만 사용한다.
```bash
# Sol 모델 해석 정상 여부 검증 (Sol 계열 최신 list 모델 반환 확인)
./bin/resolve-model.sh sol
```
- **Codex 쿼터 고갈 점검 및 agy 폴백**:
  Codex 실행 시 쿼터 고갈(Quota/Rate limit exhaustion) 또는 인증 만료가 발생하면, 무리하게 codex 호출을 반복하지 않는다. 즉시 워커를 `agy`로 폴백(fallback)하여 작업을 위임하고, 사용자에게 쿼터 소진 사실을 명확히 보고한다.

### 1.4 템플릿 복제 및 환경 설정 파일 생성
새 머신이거나 템플릿이 처음이라면:
```bash
git clone https://github.com/loverspath/agent-command-chain-template.git ~/agent-command-chain-template
cd ~/agent-command-chain-template
cp config.env.example config.env
```
복사한 `config.env`에서 다음 필수 항목들을 설정한다:
1. `AGY_MODE=resident`: agy를 상주 TUI 모드로 기동 (Stage 1 지원).
2. `CODEX_EFFORT=medium`: 상위 티어(Sol 등) 추론 강도를 medium으로 고정 (사용자 결정 반영).
3. `HANDOFF_FILE`: 세션 재개 시 자동 참조할 인수인계서 경로 지정 (예: `HANDOFF_FILE=docs/RESUME_HANDOFF.md`).
4. `PROJECT_DIR`: (선택 사항) 특정 프로젝트 디렉토리를 작업 대상으로 영구 고정할 경우 지정 (비워두면 부트스트랩 실행 위치가 기본 작업 대상이 됨).

---

## 2. 본 머신의 표준 설정값 (Standard Configuration Values)

본 머신에서 운용되는 표준 설정값과 `config.env.example`의 기본값 비교이다 (비밀값 없음):

| 설정 변수 | 머신 표준 설정 (`config.env`) | 템플릿 기본값 (`config.env.example`) | 설명 |
|---|---|---|---|
| `SESSION_NAME` | `agentchain-v2` | `agentchain` | tmux 세션 고유 식별자. v2 운용 시 `agentchain-v2` 권장 |
| `SONNET_WINDOW` | `sonnet` | `sonnet` | 계층 1 Claude Sonnet 감독자 윈도우 |
| `AGY_WINDOW` | `agy` | `agy` | 계층 2 agy 라우터/워커 윈도우 |
| `CODEX_WINDOW` | `codex` | `codex` | 계층 2 Codex 워커 윈도우 |
| `SONNET_CMD` | `claude --model sonnet --remote-control` | `claude --model sonnet --remote-control` | Sonnet 기동 명령 (RC 자동 활성화) |
| `AGY_CMD` | `agy --new-project --mode plan` | `agy --new-project --mode plan` | agy 대화형 기동 명령 (v1 레거시용) |
| `AGY_RESIDENT_CMD` | `agy --dangerously-skip-permissions` | `agy --dangerously-skip-permissions` | agy 상주 TUI 기동 명령 (Stage 1 상주 모드) |
| `CODEX_CMD` | `codex --model $(bin/resolve-model.sh sol) -s danger-full-access` | `codex --model $(bin/resolve-model.sh sol) -s danger-full-access` | Codex 대화형 기동 명령 (v1 폴백용) |
| `CODEX_TIER` | `sol` | `sol` | Codex 최상위 모델 티어 (Sol 계열 최신 자동 해석: `bin/resolve-model.sh sol`) |
| `CODEX_MODEL` | (미지정, 자동 해석) | (미지정) | 명시적 Codex 모델 지정 시 자동 해석 우회 (비상시에만 수동 오버라이드) |
| `CODEX_EFFORT` | `medium` | `medium` | Codex 추론 강도 (상위 티어는 medium 추론 강도 고정) |
| `BRIDGE_MODE` | `push` | `push` | Full-Push 이벤트 통지 브리지 모드 (FIFO+asyncRewake) |
| `WORKER_MODE` | `oneshot` | `oneshot` | 기본 워커 모드 (`oneshot` 또는 `resident`) |
| `AGY_MODE` | `resident` | (미지정) | agy 상주 TUI 브리지 모드 (Stage 1 활성화) |
| `CODEX_MODE` | `oneshot` | (미지정) | codex 워커 모드 (Stage 1에서는 oneshot 고정) |
| `HANDOFF_FILE` | (프로젝트별 지정) | `` (비워둠) | 세션 재개 시 자동 참조할 프로젝트 인계 파일 경로 |
| `START_WATCHDOG` | `true` | `true` | bootstrap 시 watchdog-v2.sh 백그라운드 자동 기동 |
| `WATCHDOG_INTERVAL` | `60` | `60` | 워치독 프로세스 생존 검사 주기 (초) |
| `DEFAULT_TASK_TIMEOUT`| `1800` | `1800` | 작업 기본 타임아웃 (30분) |
| `NO_OUTPUT_WARN_SECONDS`| `900` | `900` | 출력 정체 경고 기준 시간 (15분) |
| `AUTO_CONFIRM_TRUST`| `true` | `true` | 디렉토리 신뢰("trust this folder?") 자동 승인 |

---

## 3. 세션 분기 시나리오 (Session Branching Scenarios)

Claude 세션을 시작했을 때 마주할 수 있는 3가지 시나리오와 대응 절차이다.

### 시나리오 (a): tmux 세션이 이미 살아있는 경우
`tmux has-session -t agentchain-v2`가 성공하는 경우이다.

1. **`bootstrap-v2.sh`의 기존 세션 처리 동작**:
   - `bootstrap-v2.sh`는 이미 세션이 존재하면 프로세스 커맨드라인과 윈도우 상태를 점검하여 v2 호환성을 검증한다.
   - 이미 호환되는 세션이 실행 중이라면, 누락된 윈도우만 보완하고 기존 프로세스와 대화를 보존한다.
2. **경솔한 `--restart` 실행 금지 경고**:
   - `bootstrap-v2.sh --restart` 플래그는 기존 세션을 **강제 kill (`tmux kill-session`)**하고 재생성한다.
   - 이 경우 **진행 중이던 대화 맥락, Claude 세션 히스토리, 미저장 작업이 완전히 소실**된다.
   - 따라서 기존 세션이 살아있다면 절대로 `--restart`를 함부로 실행하지 마라.
3. **현재 실행 상태 진단 명령**:
   ```bash
   # 1. 워커 점유(busy) 상태 확인
   cat runtime/*/workers/*.busy 2>/dev/null || echo "No active busy lease"

   # 2. 실행 중인 작업 상태 확인
   grep -H '^status=' runtime/*/tasks/*/state 2>/dev/null

   # 3. 워치독 데몬 생존 확인
   pgrep -fa watchdog-v2.sh || echo "Watchdog is NOT running"

   # 4. 상태 대시보드 확인
   ./dashboard/run.sh status
   ```

### 시나리오 (b): tmux 세션이 존재하지 않는 경우
세션이 아직 생성되지 않은 상태이다.
```bash
# 1. 작업 대상 프로젝트 디렉토리로 이동 (예: 대상 프로젝트 루트)
cd /path/to/target/project

# 2. 템플릿 부트스트랩 스크립트 실행 (절대경로 또는 상대경로)
/home/rerun/agent-command-chain-template/bootstrap-v2.sh
```
스크립트가 `sonnet`, `agy`, `codex` 윈도우를 생성하고, 런타임 환경(`runtime/`), FIFO 파이프, `watchdog-v2.sh`를 자동으로 구성한다.

### 권장: `bin/session-init.sh` 로 점검/교체
```bash
./bin/session-init.sh                 # 읽기 전용 점검 + 한국어 보고 (기본)
./bin/session-init.sh --apply         # 브리지 감독관이 없을 때 새로 기동 + 검증
./bin/session-init.sh --apply --retire-old-supervisor   # 옛 감독관 /exit → 새 감독관 기동 + 검증
```
- 같은 런타임에 브리지 감독관은 **항상 1명**이어야 한다. `sonnet-event-wait.sh` 는 `listener.lock` 을 `flock -n` 으로 잡으므로, 옛 감독관의 대기자가 살아 있으면 새 감독관의 대기자는 즉시 조용히 종료되어 **알림을 영영 못 받는다**.
- 아래 (c)의 수동 절차는 비상용이다. 수동으로 띄울 때도 옛 감독관을 먼저 종료하라.

### 시나리오 (c): 일반 Claude CLI 세션 내부에서 시작된 경우 (브리지 플래그 누락)
tmux 창 밖의 일반 셸 터미널에서 `claude`를 실행했거나, 브리지 플래그 없이 시작된 Claude 세션인 경우이다.

- **원인 (왜 `asyncRewake`가 작동하지 않는가?)**:
  - Claude Code의 백그라운드 이벤트 자동 기상(`asyncRewake`)은 `--settings <runtime>/claude-bridge.settings.json`에 정의된 Stop 훅을 통해 동작한다.
  - 브리지 플래그와 `ACC_RUNTIME`, `ACC_TEMPLATE_ROOT` 환경변수가 주입되지 않은 일반 세션은 FIFO 이벤트를 감지할 수 없다.
- **해결 방안 (2가지 중 선택)**:
  1. **Sonnet 윈도우로 전환 (권장)**:
     이미 기동된 `agentchain-v2` 세션의 `sonnet` 윈도우로 이동하여 작업한다.
     ```bash
     tmux attach -t agentchain-v2:sonnet
     ```
  2. **브리지 설정과 함께 Claude 재실행**:
     현재 세션을 종료하고 올바른 브리지 플래그를 부가하여 Claude를 기동한다:
     ```bash
     TEMPLATE_DIR="/home/rerun/agent-command-chain-template"
     RUNTIME_DIR="$(tmux show-environment -t agentchain-v2 ACC_RUNTIME | sed -n 's/^ACC_RUNTIME=//p')"   # ls -td 는 테스트 런타임을 집을 수 있음
     BRIEF_FILE="$TEMPLATE_DIR/logs/session_brief.md"

     export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1
     export ACC_RUNTIME="$RUNTIME_DIR"
     export ACC_TEMPLATE_ROOT="$TEMPLATE_DIR"
     export SESSION_NAME=agentchain-v2

     claude --model sonnet --remote-control \
       --settings "$RUNTIME_DIR/claude-bridge.settings.json" \
       --add-dir "$TEMPLATE_DIR" \
       --append-system-prompt "$(cat "$BRIEF_FILE")"
     ```

---

## 4. 기동 후 검증 체크리스트 (Post-Launch Verification Checklist)

세션이 기동되거나 재개되었을 때 다음 6가지 항목을 점검한다:

1. **3개 윈도우 확인**:
   ```bash
   tmux list-windows -t agentchain-v2
   # 출력 결과에 sonnet, agy, codex 3개 윈도우가 모두 존재해야 함
   ```
2. **agy 상주 TUI 준비 상태 확인 (최초 1회성 상태 점검)**:
   ```bash
   tmux capture-pane -t agentchain-v2:agy -p | tail -n 5
   # 프롬프트 기호(>)가 보이고 유휴 대기 상태여야 함
   ```
   *주의:* 이 명령은 세션 기동/재개 시 **최초 1회 상태 점검(sanity check)** 목적에 한하며, 작업 진행 중 주기적 폴링(polling) 용도로 사용하는 것은 전면 엄격 금지된다 (완료 통지는 Full-Push 이벤트 대기).
3. **watchdog-v2 프로세스 생존 확인**:
   ```bash
   pgrep -fa "watchdog-v2.sh"
   # 워치독 PID가 정상 출력되어야 함
   ```
4. **이벤트 FIFO 파이프 존재 확인**:
   ```bash
   test -p "$(ls -td runtime/agentchain-v2-*/event.fifo | head -n 1)" && echo "FIFO OK"
   ```
5. **웹 대시보드 상태 확인**:
   ```bash
   ./dashboard/run.sh status
   # 실행 중이지 않다면 `./dashboard/run.sh start` 로 기동
   ```
6. **디스패치 드라이런 검증 (`--dry-run`)**:
   ```bash
   SESSION_NAME=agentchain-v2 ./bin/dispatch.sh agy --dry-run
   ```
   - 정상 출력: `[dry-run] Target window 'agy' in session 'agentchain-v2' is ready ...` (exit code 0)
   - **exit 75의 의미**: 워커에 활성 리스(`workers/<worker>.busy`)가 걸려 있거나 태스크가 아직 `status=running` 상태여서 새 디스패치가 충돌/거부됨을 의미함 (§5 참조).

---

## 5. Stale Lease 복구 절차 (Stale Lease Recovery)

### 5.1 증상 및 원인
- agy 상주 TUI 워커에서 서브에이전트가 예기치 않게 종료되었거나, 프로세스가 중단되었음에도 태스크 상태 파일(`state`)이 `status=running`으로 방치되는 경우가 있다.
- 이 상태에서는 `dispatch.sh` 실행 시 **`exit 75 (Worker is currently busy executing task ... Dispatch rejected)`** 에러가 발생하며 새 작업이 영구 차단된다.

### 5.2 진단 및 확인
```bash
# 1. 점유 중인 태스크 ID 확인
cat runtime/agentchain-v2-*/workers/agy.busy

# 2. 태스크 상태 확인
cat runtime/agentchain-v2-*/tasks/<task_id>/state
```

### 5.3 복구 명령 (`bin/task-abandon.sh`)
템플릿에 내장된 원자적 복구 스크립트를 실행한다:
```bash
# 태스크 ID 지정 복구
./bin/task-abandon.sh <task_id>

# 또는 워커명 지정 자동 복구 (busy 파일의 태스크를 찾아 처리)
./bin/task-abandon.sh agy
```
- 만약 워커 프로세스가 여전히 백그라운드에 살아있다면 스크립트가 안전을 위해 변경을 거부한다. 강제 종료 및 상태 회수를 원할 경우 `--force` 플래그를 붙인다:
  ```bash
  ./bin/task-abandon.sh agy --force
  ```
- **동작 결과**:
  - `status=running`을 `status=abandoned`로 원자적 치환.
  - `workers/agy.busy` 리스 파일 즉각 삭제.
  - 브리지에 `canceled` 이벤트를 발행하여 대시보드 및 상위에 알림.
  - 이후 `dispatch.sh agy --dry-run`이 즉시 성공함.

### 5.4 Claude auto-mode 분류기 차단 시 대처
- Claude Code의 auto-mode 환경에서는 모델이 `sed`나 `echo`로 `state` 파일을 직접 수정하려 할 때 보안 분류기(classifier)에 의해 툴 호출이 거절될 수 있다.
- 해결책: 모델이 스크립트를 직접 편집하려 하지 말고, 사용자에게 터미널에서 `!` 접두사로 스크립트를 실행하도록 안내하라:
  ```bash
  ! ./bin/task-abandon.sh <task_id>
  ```

---

## 6. 프로젝트 핸드오프 재개 절차 (Project Handoff Resume Procedure)

신규 세션에서 특정 프로젝트 작업을 이어받을 때는 다음 순서를 엄격히 준수한다.

### 6.1 인계 문서 열람 순서
프로젝트 내에 지정된 인계 문서(예: `docs/RESUME_HANDOFF.md` 또는 `config.env`의 `HANDOFF_FILE`)가 있다면 아래 순서로 읽는다:
1. **규칙 및 안전 수칙 (Rules & Live Session Safety)**: 수정 금지 대상 경로, 보호 세션, 격리 규약.
2. **운영 현황 (Operations)**: 현재 살아있는 tmux 윈도우, 워치독 PID, 대시보드 포트.
3. **완료 및 진행 중 작업 (Completed / In-flight Tasks)**: 이미 커밋된 내용과 미완료 항목.
4. **다음 후보 작업 (Next Candidate Plans)**: 대기 중인 설계 및 구현 과제.

### 6.2 사용자 보고 형식 (한국어 간략 보고)
인계 문서를 확인한 직후, 사용자에게 **한국어로 3~5줄 내외의 요약 보고**를 작성한다:
- **현재 시스템 상태**: 세션 생존 여부, 완료된 최근 커밋, 워커 가용 상태.
- **다음 작업 후보**: 착수 가능한 후보 과제 2~3개.
- **사용자 결정 필요 사항 (Pending Decisions)**: 설계 방향, 브랜치 전략 등 사용자 승인이 필요한 사항.

### 6.3 절대 금지 수칙 (Strict Constraints)
> [!CAUTION]
> **사용자의 명시적 승인/지시가 있기 전에는 다음 작업을 절대 수행하지 마라:**
> 1. `bin/dispatch.sh`를 통한 임의의 워커 작업 디스패치 금지.
> 2. `bootstrap-v2.sh --restart`를 통한 세션 강제 재시작 금지.
> 3. git push 또는 메인 브랜치 머지 금지.
> 4. 사용자와의 대화/논의는 승인이 아니므로, 명시적 승인을 확인한 후 디스패치하라.

---

## 7. 감독관 행동 규범 (Supervisor Behavioral Norms)

Claude Sonnet(계층 1 감독자)으로서 지켜야 할 핵심 행동 규범이다:

1. **역할 분담의 철칙 (구현과 테스트는 agy, 감독은 설계·검수·머지)**:
   - **감독관(Sonnet)**: 사람과의 소통, 상위 아키텍처 설계, 작업 디스패치, 결과 감사(Audit), Git 커밋 및 머지 담당. 감독관은 코드를 직접 수정하지 않으며 문서 정리 작업 역시 agy에게 위임한다.
   - **구현 워커(agy & codex)**: 실제 코드 파일 편집, 문서 정리, 대량 탐색, 정적 검사 및 **단위/통합 테스트 실행 ("테스트도 agy가 한다" 원칙)**.
   - Sol 쿼터 한도 소진 시에도 동일하게 agy가 구현과 테스트를 전담한다.
   - 감독관이 직접 수십 줄 이상의 코드를 작성하거나 툴 루프에 빠져 구현·테스트를 대신하지 않는다.
2. **화면 폴링 금지, Full-Push 이벤트 대기**:
   - `tmux capture-pane`이나 루프를 통한 주기적 상태 조회를 전면 금지한다.
   - 워커가 작업을 마치면 `[ACC_EVENT_BATCH]` 시스템 알림으로 자동 기상하므로, 디스패치 후에는 조용히 대기하라.
3. **디스패치 프롬프트 3대 필수 요소**:
   - `knowledge_refs`: 워커가 사전에 반드시 읽어야 할 위키/규약 문서 경로.
   - `learning_capsule`: 워커 종료 시 예상 밖 상황/원인/재사용 규칙 보고 요구.
   - `[[DONE <task_id>]]` sentinel: 작업 완료 시 최종 보고 형식.
4. **실측 상태 직접 검증 (2층 감사 구조)**:
   - 워커가 제출한 자기보고 텍스트(`refs_loaded`, `learning_capsule`)만 맹신하지 마라.
   - 반드시 `$ACC_RUNTIME/tasks/<task_id>/output.log`와 실제 `git diff`를 직접 열람하여 교차 검증하라.
5. **논의는 승인이 아니다 (Discussion != Approval)**:
   - 사용자가 "어떻게 생각하세요?", "이런 방향은 어떨까요?"라고 질문한 것은 계획 수립을 위한 의견 교환일 뿐, 구현 착수 승인이 아니다.
   - 반드시 계획을 정리하여 제시한 뒤 사용자의 명시적인 진행 승인을 얻고 디스패치하라.
