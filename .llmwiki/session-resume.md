---
title: Session Resume & Environment Reproduction Runbook
tags: [session-resume, runbook, operation, tmux-bridge, handoff, troubleshooting, supervisor-norms]
related: ["[[INDEX]]", "[[usage]]", "[[architecture]]", "[[known-issues]]"]
summary: 세션 점검·교체·stale lease 복구와 감독자 정본·프로젝트 지침으로 연결하는 재개 런북.
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
역할별 한도 소진 대응은 [SUPERVISOR](../SUPERVISOR.md)의 역할 절을 따른다.

### 1.4 템플릿 복제 및 환경 설정 파일 생성
새 머신이거나 템플릿이 처음이라면:
```bash
git clone https://github.com/loverspath/agent-command-chain-template.git ~/agent-command-chain-template
cd ~/agent-command-chain-template
cp config.env.example config.env
```
복사한 `config.env`에서 다음 항목을 인스턴스 용도에 맞게 설정한다:
1. `AGY_MODE`: oneshot/resident 중 해당 인스턴스 실행 방식을 선택한다.
2. `CODEX_EFFORT`: 모델 구현 설정이며 프로젝트/사용자 인스턴스 선호를 따른다.
3. `HANDOFF_FILE`: 세션 재개 시 자동 참조할 인수인계서 경로 지정 (예: `HANDOFF_FILE=docs/RESUME_HANDOFF.md`).
4. `PROJECT_DIR`: (선택 사항) 특정 프로젝트 디렉토리를 작업 대상으로 영구 고정할 경우 지정 (비워두면 부트스트랩 실행 위치가 기본 작업 대상이 됨).

---

## 2. 템플릿 기본값과 인스턴스 설정

설정 변수·기본값은 [[usage#2-설정-체계-및-계층화-2-tier-configuration]]와 `config.env.example`이 정본이다. 이 런북에 머신 설정 표를 복제하지 않는다. 사용자 선호·프로젝트 조율은 [문서 3층](../docs/INSTRUCTION-LAYERS.md)을 따른다.

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
     # 런타임 브리핑 우선순위: session_brief.md -> supervisor_brief.md -> logs/session_brief.md
     if [[ -f "$RUNTIME_DIR/session_brief.md" ]]; then
       BRIEF_FILE="$RUNTIME_DIR/session_brief.md"
     elif [[ -f "$RUNTIME_DIR/supervisor_brief.md" ]]; then
       BRIEF_FILE="$RUNTIME_DIR/supervisor_brief.md"
     else
       BRIEF_FILE="$TEMPLATE_DIR/logs/session_brief.md"
     fi

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

## 4. 기동 후 점검

`bin/session-init.sh --check --session <세션명>`으로 창·워치독·FIFO·단일 리스너·dispatch 대상 상태를 한 번 확인한다. 이미 같은 점검을 마쳤으면 수동 체크리스트를 중복 실행하지 않는다. 이 도구는 `--apply` 없이 기동·교체하지 않는다. 새로 기동할 때와 작업 완료를 기다릴 때의 점검을 구별한다.

## 5. Stale Lease 복구 절차 (Stale Lease Recovery)

### 5.1 증상 및 원인
- agy 상주 TUI 워커에서 서브에이전트가 예기치 않게 종료되었거나, 프로세스가 중단되었음에도 태스크 상태 파일(`state`)이 `status=running`으로 방치되는 경우가 있다.
- 이 상태에서는 `dispatch.sh` 실행 시 **`exit 75 (Worker is currently busy executing task ... Dispatch rejected)`** 에러가 발생하며 새 작업이 영구 차단된다.

### 5.2 진단 및 확인
```bash
# 1. 점유 중인 태스크 ID 확인
cat "$RUNTIME_DIR/workers/agy.busy"

# 2. 태스크 상태 확인
cat "$RUNTIME_DIR/tasks/<task_id>/state"
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
거절 문구·기존 허용 설정·직전 성공 명령을 확인하고 승인 범위 안에서 전용 도구의 단순 호출로 해결한다. 원인을 확인하지 않은 채 사용자 설정 추가나 직접 실행을 요구하지 않는다. 해소 불가능한 차단만 실제 결과와 함께 보고한다.

---

## 6. 프로젝트 핸드오프 재개 절차

[SUPERVISOR](../SUPERVISOR.md) → [문서 3층](../docs/INSTRUCTION-LAYERS.md) → HANDOFF_FILE → 해당 프로젝트 지침과 현재 상태를 읽는다. HANDOFF가 겸용이면 요청한 프로젝트를 고른다. 과거 운영 HEAD·검사 수·날짜 한정 지시를 현재 사실로 사용하지 않는다.

현재 상태 / 다음 작업 / 실제 사용자 결정이 필요한 항목만 한국어로 짧게 보고한다. 기존 실행 위임 범위는 그대로 이어간다.

## 7. 감독자 운영 지침

[SUPERVISOR](../SUPERVISOR.md)가 단일 정본이다. 이 런북에는 역할·매 지시서 보고 필드·검증 세트를 반복하지 않는다. 지시서는 ROLES와 선택한 프로젝트 지침을 읽으라는 참조 한 줄로 연결한다.

---

## 부록: tmux 서버 사망(WSL/윈도우 재부팅) 후 세션 복구

`tmux ls` 가 "no server running" 이면 세션·워치독·상주 워커가 모두 죽은 것이다 (디스크의 `runtime/` 과 Claude 대화 transcript 는 보존).
세션명은 인스턴스마다 다르므로 `bin/chain list` 로 확인 후:

```bash
bin/chain-recover.sh <세션명> --dry-run   # 런타임/PROJECT_DIR/감독자 session id 탐색 결과 확인
bin/chain-recover.sh <세션명>             # sonnet/agy/codex 창 재생성 + claude --resume --remote-control
bin/ts                                     # 또는 Tokyo Night TUI 매니저 실행 후 세션 선택 및 [r] 복구
```

복구되지 않는 것: 워치독, agy 상주 TUI, 진행 중 codex/agy 작업, 끊긴 백그라운드 쉘. 감독자 세션에서 인계 파일을 재읽고 재dispatch 한다.
정식 재기동(워치독 포함)은 `bootstrap-v2.sh --restart` 이나 감독자가 새 세션이 되어 이력이 이어지지 않는다.
Claude 스킬: `~/.claude/skills/agentchain-recover`.
