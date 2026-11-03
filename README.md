# Agent Command Chain — Template (v2.3)

간소화된 4계층 멀티 에이전트 명령계통의 템플릿. `eraweb-fork`의 실전 구조
(Sol/Terra/agy 3역할 + Sonnet/Terra/Sol-Opus 3티어 라우터, `docs/workflows/`)를
관찰한 뒤, 더 가볍게 재구성한 것이다.

> [!IMPORTANT]
> **실행 경로 안내 (v2.3 기본 권장 / v1 폴백)**
> - **기본 권장 경로**: **v2.3 Full-Push 이벤트 통지 및 구조 계약 브리지** (`bootstrap-v2.sh`, `watchdog-v2.sh`, `bin/*`, `adapters/*`, `lib/*`). FIFO 기반 즉각 깨우기(`asyncRewake`)와 4대 안전망 워치독을 기반으로 하며, v2.3에서는 **구조 계약(사유 코드 세분화, 복구 컨텍스트 주입, 능동성 관찰 게이트)**과 **구현 유연성(병렬 worktree 격리, 터미널 추상화 래퍼, agy 모델 동적 해석)**이 완전히 분리되었습니다. 상주 관찰이 필요한 경우 Stage 1 resident TUI 모드(`AGY_MODE=resident`)를 지원합니다.
> - **폴백/레거시 경로**: **v1 기본 브리지** (`bootstrap.sh`, `watchdog.sh`). 순수 tmux 화면 캡처(pull) 방식으로 동작하며, v2 환경 구성이 어렵거나 최소 세션 테스트 시의 폴백으로 완전 보존됩니다. (v1 파일은 절대 삭제되지 않음)
> - **단일 정본(SSOT)**: 감독자 운영·경중 판단·지시서 작성은 [SUPERVISOR.md](SUPERVISOR.md), 워커 보고 계약은 [ROLES.md](ROLES.md), 지침 3층 구조는 [docs/INSTRUCTION-LAYERS.md](docs/INSTRUCTION-LAYERS.md)를 참조하세요.
> - **세션 재개 런북**: 신규 Claude 세션에서 환경을 재현하거나 중단된 작업을 이어받을 때는 [`.llmwiki/session-resume.md`](.llmwiki/session-resume.md)를 참조하세요.

## 1. 역할 구조 및 분배 토폴로지

> [!NOTE]
> **단일 정본(SSOT) 안내**: 감독자 운영 지침·경중 판단·지시서 작성·공통 실행 원칙의 정본은 [SUPERVISOR.md](SUPERVISOR.md)이며, 워커 보고 계약과 센티널 규격은 [ROLES.md](ROLES.md), 지침의 3층 구조는 [docs/INSTRUCTION-LAYERS.md](docs/INSTRUCTION-LAYERS.md)에 정의되어 있습니다. 본 문서는 개요 및 빠른 참조만 제공합니다.

체인에서 반드시 지켜야 하는 것은 **분배 토폴로지(Dispatch Topology)**뿐입니다:
$$\text{모니터/오케스트레이터(감독자)} \longrightarrow \text{디스패처 / 워커} \longrightarrow \text{리드} \longrightarrow \text{워커}$$

| 계층 / 역할 | 기본 슬롯 | 주요 책임 | 핵심 원칙 및 규약 |
|---|---|---|---|
| **0. 사용자** | 사람 | 최종 의사결정, 승인, 명시적 개입 | RC(Remote Control) 또는 터미널을 통해 감독자 세션에 개입 |
| **1. 모니터/오케스트레이터 (감독자)** | Claude Sonnet | 사용자 소통, 배분, 경중 판단, 증거 감사 | **실작업 위임 원칙**: 사용자가 직접 수행을 명시하지 않는 한 실질 작업(문서/코드 작성/조사)은 스스로 하지 않고 워커에 위임. 사용자 요청 원문을 그대로 전달 ([SUPERVISOR.md](SUPERVISOR.md)) |
| **2. 디스패처 / 라우터** | agy (워커) | 리드 및 모델 결정, 작업 배정 | **agy 기본 판단 원칙**: 어떤 리드를 부를지(디스패치)는 **명시가 없으면 기본적으로 agy(워커)가 판단**. 감독자나 사용자는 구체적인 지정(예: "이건 Sol로")이 있을 때만 개입 |
| **3. 리드** | Codex Terra / Sol | 설계·검증설계·판단·운영 반영 | **운영 반영 전담**: 검증 방법을 정하고 결과를 판단하며, 커밋/푸시/배포 등 운영 반영은 리드만 직접 수행. **Terra 6.1**(`resolve-model.sh terra`)은 소·중 작업의 간단한 리드에 권장되며, 영향이 넓은 대 작업이나 고난도 판단은 **Sol**(`resolve-model.sh sol`)이 기본 |
| **4. 워커 (구현·조사·검증)** | agy / 서브에이전트 | 실작업(조사·구현·테스트) 실행 | **메인 직접 작업 금지 및 서브에이전트 위임**: agy 메인은 지시·취합·보고만 수행하고 조사·구현·테스트·git 정리를 포함한 모든 실작업은 `invoke_subagent`에 전량 위임. 메인이 직접 작업 시 응답 불가(busy)가 되므로 직접 작업 엄격 금지 (`bin/agy-main-audit.sh`로 감사). 실패 시 `[[BLOCKED <id>]]` 보고 |

### 핵심 원칙 및 계층 참조
1. **모델 배정은 교체 가능한 구현 슬롯**: 모델 이름(Sonnet, agy, Terra, Sol 등)은 기본값일 뿐이며, 작업 성격·품질·사용량 한도 소진 등에 따라 언제든 변경될 수 있습니다 (토폴로지 위반 아님). 단, 진행 중인 작업의 리드는 임의로 교체하지 않고 교체 시 사용자에게 알립니다.
2. **지침 3층 구조 ([docs/INSTRUCTION-LAYERS.md](docs/INSTRUCTION-LAYERS.md))**:
   - **체인 템플릿**: [SUPERVISOR.md](SUPERVISOR.md), [ROLES.md](ROLES.md), [구조 원칙](docs/PRINCIPLES-structure-vs-implementation.md) (프로젝트 비종속 공통 계약)
   - **프로젝트 지침**: [projects/](projects/) 디렉터리 (`projects/<프로젝트>.md`, `projects/README.md`) (프로젝트별 조율, 배포·검증 기본, 편성 예외)
   - **개인 메모리**: 감독자의 사용자 선호·교정과 위 정본 링크만 유지 (실행 절차 복제 금지)
3. **경중 판단 및 지시서 작성 ([SUPERVISOR.md](SUPERVISOR.md))**:
   - 파일 수나 모델 티어가 아닌 **영향과 불확실성**으로 판정 (소: 국소 수정 / 중: 작은 설계 판단 / 대: 공통 구조·데이터 변경).
   - 지시서는 **목표 + 맥락만** 작성하며, 소 작업 지시서 본문은 1KB 이내로 제한하고 공통 절차나 안전 규칙을 불필요하게 복제하지 않습니다.

## 2. 브리지 설계

### 2.1 Full-Push 이벤트 통지 브리지 (v2, 기본 권장)
v2는 tmux의 pull 모델(화면 스크래핑/폴링)의 지연과 불안정성을 극복하기 위해 **POSIX FIFO 기반 Full-Push 이벤트 통지 아키텍처**를 기본 경로로 채택합니다.

```
[Sonnet (계층 1 감독자)]
   ▲
   │ asyncRewake (FIFO 이벤트 수신, exit code 2로 즉각 기상)
[runtime/event.fifo] ◄── [bin/event-emit.sh (Durable Outbox 원자적 기록 & FIFO 펄스)]
                               ▲
       ┌───────────────────────┴───────────────────────┐
 [bin/run-task.sh (oneshot 워커 실행기)]      [watchdog-v2.sh (4대 안전망 데몬)]
 - 커널 자동 해제형 terminal flock           - 세션/Sonnet 생존 감시
 - 상태 전이(pending→running→done/error)      - PID 증발 비정상 작업 원자적 Reap
 - adapters/ (agy/codex) 격리 실행           - Deadline/Stall 초과 감시 및 브리지 정체 해소
```

- **제로 딜레이 비동기 통지**: 워커가 작업을 마치면 `bin/event-emit.sh`가 Durable Outbox에 원자적으로 이벤트를 스풀링하고 FIFO에 펄스를 주입하여 `bin/sonnet-event-wait.sh`를 깨웁니다. Claude Code의 `asyncRewake` 훅이 즉시 Sonnet을 기상시킵니다.
- **4대 안전망 워치독 (`watchdog-v2.sh`)**:
  1. *세션/Sonnet 증발*: tmux 세션 또는 Sonnet 프로세스 사망 시 즉각 복구.
  2. *PID 증발 Reap*: 워커 PID가 사라진 비정상 중단 작업을 원자적으로 감지하고 fail 처리 및 이벤트 발행.
  3. *Deadline/Stall 초과*: 작업별 타임아웃 및 출력 무변경(stall) 감시.
  4. *브리지 정체 해소*: ACK 타임아웃 초과 이벤트를 pending으로 재인계하여 전달 보장.
- **커널 자동 해제형 소유권**: 프로세스 비정상 종료(SIGKILL) 시에도 stale lock이 남지 않도록 커널 `flock`을 기반으로 터미널 소유권과 워치독 단일 인스턴스를 보호합니다.
- **Resident TUI 모드 (Stage 1 agy 상주 지원)**:
  `AGY_MODE=resident` 지정 시 agy가 tmux 창에 상시 대화형 TUI로 유지되며, 디스패처는 파일(`$task_dir/prompt.md`)과 1줄 도어벨 트리거로 안전하게 지시를 전달합니다. 완료 통지는 Antigravity CLI의 `Stop` 라이프사이클 훅(`bin/agy-stop-hook.sh`)을 통해 `fullyIdle: true` 및 마지막 모델 응답의 완료 센티널(`[[DONE <id>]]` / `[[BLOCKED <id>]]`) 검증을 거쳐 원자적으로 발행되며(지시서가 논리적 이름 ID를 사용해도 유연하게 허용, 이전 작업 잔여 센티널 오탐 방지 타임스탬프 가드 적용), 디스패치 시 지시서 규약 D가 자동 주입됩니다.
- **세션 타겟팅 및 4계층 자동 해석**:
  `bin/dispatch.sh` 및 관련 도구들은 세션 오조준 방지를 위해 `SESSION_NAME` 4계층 자동 해석(환경변수 > 런타임 마커 > 현재 tmux 세션 > config.env) 및 v2 마커 가드(오조준 시 `exit 70`)를 내장하고 있습니다. 명시적인 `SESSION_NAME=agentchain-v2` 사용을 강력 권장합니다.
- **v2.3 구조 계약(Structure Contracts) 및 구현 유연성 분리**:
  1. *종단 사유 코드 (`reason_code`)*: 모든 종료 이벤트와 상태에 머신 판독용 사유 코드(`ok`, `timeout`, `stalled`, `interrupted`, `rate_limit`, `tool_failed`, `exec_error` 등)를 기록하고 비휘발성 저장.
  2. *복구 컨텍스트 주입 (`RECOVERY_NOTE`)*: 비정상 중단 작업(`last_interrupted`) 후속 디스패치 시 이전 작업 정보 및 실패 상태 자동 주입 (`--recover-from`, `--no-recovery-note`).
  3. *병렬 작업 공간 격리 (`--worktree`)*: 단일 리포 내 병렬 쓰기 충돌을 방지하는 온디맨드 `git worktree` 격리 및 검증/정리 도구(`bin/worktree-clean.sh`).
  4. *터미널 추상화 계층 (`lib/term.sh`)*: 스크립트 전반의 멀티플렉서 명령을 순수 함수형 래퍼로 캡슐화 (`ACC_TERM_BACKEND=tmux|herdr`).
  5. *Agy 모델 동적 해석 확장*: `bin/resolve-model.sh --harness agy <tier>` 지원 (`fast`, `deep`, `argon` 등) 및 `AGY_TIER`, `AGY_EFFORT` 설정 연동.
  6. *능동성 관찰 게이트 (`Rules-then-LLM`, `lib/gate.sh`)*: `ACC_PROACTIVE_MODE=observe|notify|off` 설정을 통해 능동적 트리거를 결정론적으로 필터링하고, 감독자 기상 없이 `events/archive/observed-*`에 감사 기록.

### 2.2 tmux 브리지 (v1, 폴백/레거시)
세 프로세스(Sonnet CLI, agy CLI, Codex CLI)를 **같은 WSL Ubuntu tmux 세션의 서로 다른 윈도우**에 띄우고, 외부 컨트롤러(사람 또는 Sonnet 자신의 Bash 툴)가 `tmux send-keys` / `capture-pane` / `pipe-pane`으로 조작하는 기존 순수 pull 방식입니다.

```
tmux session: agentchain (config.env의 SESSION_NAME)
 ├─ window 0 "sonnet": claude --model sonnet --remote-control --add-dir <템플릿경로>
 ├─ window 1 "agy":    agy --new-project --mode plan (대화형, 지속)
 └─ window 2 "codex":  codex --model "$(bin/resolve-model.sh sol)" -s danger-full-access (대화형, 지속 — `codex exec`는 one-shot이라 안 씀)
```

- **push 없음**: tmux는 근본적으로 pull입니다. 실시간성이 필요하면 `pipe-pane`으로 로그 파일을 만들고 tail하는 것이 근접한 비동기 감지 수단입니다.
- **동적 대상 프로젝트 디렉토리 (`PROJECT_DIR`)**: `bootstrap.sh`/`watchdog.sh`를 실행한 디렉토리가 대상 프로젝트가 되며(`-c "$PROJECT_DIR"`), `config.env`의 `PROJECT_DIR`로 특정 경로를 고정할 수도 있습니다.
- **폴더 신뢰(Folder Trust) 자동 승인**: 새 디렉토리 첫 실행 시 대화형 "trust this folder?" 프롬프트를 `wait_for_pane_text`로 감지하여 자동 확인합니다 (`AUTO_CONFIRM_TRUST=true`).
- **부트 브리핑 자동 전송**: 기동 시 `logs/session_brief.md`를 동적 생성하고, Sonnet UI 준비 완료를 감지한 뒤 해당 파일을 읽도록 채팅을 자동 전송합니다 (`--add-dir`로 세션 범위 읽기 허용).
- **agy ↔ codex 자동 연동 부재**: 외부 라우터 엔진이 없으므로 agy가 codex를 직접 부르지 않으며, Sonnet(또는 사람)이 결과를 보고 수동으로 `send-keys` 핸드오프합니다.

## 3. 파일 구성

| 구분 | 파일 / 디렉토리 | 역할 |
|---|---|---|
| **지침 및 계약 (SSOT)** | `SUPERVISOR.md` | **감독자 운영 정본 지침**: 분배 토폴로지, 역할, 경중 판단, 지시서 규격, 공통 실행 원칙 |
| | `ROLES.md` | 역할 진입점 및 워커 보고 계약, 완료 센티널(`[[DONE]]`/`[[BLOCKED]]`) 및 사유 코드 규격 |
| | `projects/` | 프로젝트별 설정 및 오버라이드 지침 (`projects/README.md`, `projects/<프로젝트>.md`) |
| | `docs/INSTRUCTION-LAYERS.md` | 지침 계층 구조 (체인 템플릿 → 프로젝트 → 개인 메모리 3층) 및 참조 경로 |
| | `docs/PRINCIPLES-structure-vs-implementation.md` | 구조 계약(토폴로지, Durable Outbox, 2층 감사)과 구현 유연성 분리 원칙 정본 |
| | `docs/IMPL-DESIGN-20261102.md` | v2.3 6대 구조 계약 도입 사양 및 구현 설계서 |
| **v2 (기본 권장)** | `bootstrap-v2.sh` | v2 원클릭: 런타임 디렉토리·FIFO 초기화 + tmux 세션 기동 + 워치독 v2 자동 백그라운드 구동 |
| | `watchdog-v2.sh` | 4대 비정상 상태(세션/Sonnet, PID증발, Stall/Deadline, 브리지 정체) 전담 안전망 |
| | `bin/dispatch.sh` | 작업 디스패처 (4계층 세션 자동 해석, v2 마커 검증, `--worktree`, `--recover-from`, `--dry-run`) |
| | `bin/agy-main-audit.sh` | agy 메인 세션 감사 도구 (서브에이전트 위임 횟수 및 메인 직접 작업 위반 점검) |
| | `bin/session-init.sh` | 세션 초기화 브리프 생성 및 정본 포인터 주입 스크립트 |
| | `bin/event-emit.sh` | 원자적 이벤트 스풀링(Durable Outbox), 사유 코드(`reason_code`) 및 FIFO 펄스 전송 |
| | `bin/sonnet-event-wait.sh` | Claude Code asyncRewake 전용 FIFO 이벤트 대기 어댑터 (exit 2로 기상, 게이트 필터링) |
| | `bin/event-ack.sh` | 처리 완료된 이벤트의 archive 이동 및 FIFO 드레인 |
| | `bin/run-task.sh` | 커널 자동 해제형 terminal flock 기반 워커 프로세스 실행 및 상태/이벤트 발행 |
| | `bin/resolve-model.sh` | Codex 및 Agy 하네스별 최신 모델 동적 해석 (`--harness agy\|codex`) |
| | `bin/worktree-clean.sh` | 작업트리 미커밋 오염 검사 및 안전 회수/삭제 도구 (`--force`) |
| | `bin/task-abandon.sh` | 비정상 중단 작업 원자적 회수 및 `last_interrupted` 마커 기록 |
| | `bin/chain`, `bin/chain-recover.sh` | 체인 런타임/리스너/워치독 목록 점검 및 비정상 체인 복구 도구 |
| | `bin/ts`, `bin/ts-tui` | TMUX 빠른 접속 및 에이전트 체인 복구/신규 기동 통합 CLI & Tokyo Night TUI (`ts`) ([상세 가이드](tools/ts/README.md)) |
| | `tools/ts/` | `ts` 세션 매니저 통합 도구 킷 보관소 및 설치 스크립트 ([문서](tools/ts/README.md)) |
| | `bin/agy-stop-hook.sh` | Stage 1 agy 상주 모드 Native Stop 훅 핸들러 |
| | `lib/term.sh` | 터미널 멀티플렉서 추상화 레이어 (`tmux` / `herdr`) |
| | `lib/reason.sh` | 종단 이벤트 및 상태 `reason_code` 검증 및 출력/센티널 자동 분류기 |
| | `lib/gate.sh` | 능동적 트리거 필터링 및 침묵 감사를 위한 Rules-then-LLM 결정론적 게이트 |
| | `lib/session.sh` | 세션명 자동 해석 및 v2 런타임/마커 검증 공통 라이브러리 |
| | `adapters/agy-oneshot.sh` | agy 단발 실행기 어댑터 (모델 해석 및 사유 코드 전달) |
| | `adapters/codex-oneshot.sh` | Codex 단발 실행기 어댑터 |
| | `claude-bridge.settings.json` | Claude Code asyncRewake 훅 및 명령어 권한 설정 |
| **설정 및 환경** | `config.env.example` | 세션명, 타임아웃, 브리지 모드, agy 모델 티어, 터미널 백엔드, 능동성 모드 등 전체 환경설정 |
| | `instances/` | 인스턴스별 환경설정 오버라이드 디렉터리 (`instances/<id>.env`) |
| **대시보드** | `dashboard/` | 상태 모니터링 대시보드 (읽기 전용, tailnet 바인딩, 작업 슬라이싱 및 민감 정보 마스킹) |
| **v1 (폴백/레거시)** | `bootstrap.sh` | v1 tmux 세션 생성 → sonnet/agy/codex 윈도우 기동 (순수 pull 기반, 자동 신뢰 확인/브리핑 지원) |
| | `watchdog.sh` | v1 세션/프로세스 생존 감시 및 단순 재기동 (순수 pull 기반) |
| **위키/문서** | `.llmwiki/` | 아키텍처 및 상세 사용법 위키 문서 (`usage.md`, `session-resume.md`, `architecture.md` 등) |

## 4. 빠른 시작

**이 템플릿은 프로젝트 종속적이지 않다.** `bootstrap-v2.sh`나 `bootstrap.sh`를 실행한 디렉토리가 곧 대상 프로젝트가 됩니다 (세 tmux 창 모두 그 경로에서 시작). 경로를 고정하고 싶으면 `config.env`의 `PROJECT_DIR`을 채우면 됩니다.
감독관과 리드는 세션 기동 시 [SUPERVISOR.md](SUPERVISOR.md)와 [docs/INSTRUCTION-LAYERS.md](docs/INSTRUCTION-LAYERS.md)를 먼저 읽고, 해당 프로젝트 지침([projects/](projects/))을 확인합니다.

```bash
# 템플릿 클론 및 설정 준비 (한 번만 수행)
git clone https://github.com/loverspath/agent-command-chain-template.git ~/agent-command-chain-template
cd ~/agent-command-chain-template && cp config.env.example config.env   # 필요시 값 수정

# 작업 대상 프로젝트 디렉토리로 이동 (이 디렉토리가 대상이 됨)
cd /path/to/your/actual/project

# [기본 권장 경로: v2 Full-Push 이벤트 통지 브리지]
~/agent-command-chain-template/bootstrap-v2.sh    # 세션 기동 + 런타임/FIFO 준비 + watchdog-v2 자동 기동 (START_WATCHDOG=true 기본값)

# [작업 디스패치: 세션명 명시 권장]
SESSION_NAME=agentchain-v2 ~/agent-command-chain-template/bin/dispatch.sh agy --prompt-file /path/to/prompt.md

# [병렬 쓰기 격리 디스패치: 독립 worktree 생성]
SESSION_NAME=agentchain-v2 ~/agent-command-chain-template/bin/dispatch.sh agy --prompt-file /path/to/prompt.md --worktree

# [작업트리 정리 (작업 종료 후)]
SESSION_NAME=agentchain-v2 ~/agent-command-chain-template/bin/worktree-clean.sh /path/to/worktree

# [중단 작업 명시 복구 디스패치]
SESSION_NAME=agentchain-v2 ~/agent-command-chain-template/bin/dispatch.sh agy --prompt-file /path/to/prompt.md --recover-from task-12345678

# [agy 메인 세션 서브에이전트 위임 감사]
~/agent-command-chain-template/bin/agy-main-audit.sh <task-id>

# [Stage 1: agy 상주 TUI 모드로 기동할 경우]
# AGY_MODE=resident ~/agent-command-chain-template/bootstrap-v2.sh

# [상태 대시보드 기동 (선택사항, tailnet 전용 읽기 전용)]
# ~/agent-command-chain-template/dashboard/run.sh start

# [폴백/레거시 경로: v1 순수 pull 브리지]
# ~/agent-command-chain-template/bootstrap.sh
# ~/agent-command-chain-template/watchdog.sh &

tmux attach -t agentchain-v2                       # 직접 들어가서 보고 싶을 때 (v2 세션)
./bin/ts                                           # Tokyo Night TUI 세션 매니저 및 체인 복구/기동
```

## 5. 알려진 한계 및 주의사항

- **RC 전제조건**: `claude --remote-control`은 claude.ai **Pro/Max/Team/Enterprise
  구독 + `claude /login` OAuth 로그인**이 사전에 되어 있어야 작동한다.
  API 키 인증만으로는 안 됨. Team/Enterprise는 조직 Owner가
  claude.ai/admin-settings/claude-code에서 RC를 켜둬야 한다. 세션 안에서
  `/remote-control` 을 치면 상태 패널(URL, 연결 상태)을 볼 수 있다.
- **agy → codex 자동 호출 부재 및 라우팅 원칙**: 외부 라우터 엔진 하네스를 배제하여 agy 프로세스가 codex 프로세스를 직접 spawn하지 않습니다. 디스패처 역할(어떤 리드와 모델을 부를지)은 기본적으로 agy가 판단하여 보고(`[[BLOCKED]] reason=needs-terra` 등)하며, 사용자나 감독자가 `bin/dispatch.sh`로 해당 리드/워커를 디스패치합니다.
- **agy는 네이티브 Linux 빌드가 있다** (`~/.local/bin/agy` 등에 설치되는 형태 —
  2026-09 실측 확인). 비대화형 셸 환경을 위해 `bootstrap.sh`/`bootstrap-v2.sh`가 자체적으로
  `PATH="$HOME/.local/bin:$PATH"`를 추가해 확인한다. 정말 Windows 전용 빌드만 있는
  환경이라면 `config.env.example`의 주석에 적힌 `cmd.exe` 우회를 대신 써라.
- **codex 실행 모드**: v1 브리지에서는 대화형 지속 TUI `codex`를 기본값으로 사용하며, v2 브리지의 oneshot 실행(`adapters/codex-oneshot.sh`)에서는 `codex exec`를 통해 단발 실행됩니다. WSL 환경에서는 `npm install -g @openai/codex@latest`를 WSL 안에서 직접 설치해야 리눅스 네이티브 바이너리가 정상 작동합니다.
- **디렉토리 신뢰 프롬프트 자동 승인**: claude/codex의 신규 디렉토리 "trust this folder?" 프롬프트는 `--dangerously-skip-permissions`로 건너뛸 수 없어 스크립트가 화면 텍스트를 감지한 후 자동으로 키 입력을 전송한다 (`AUTO_CONFIRM_TRUST=true`).
- **세션 지속성 경고 (`CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`)**: 부모 Claude Code 세션 감지로 인한 transcript 저장 비활성화 경고는 v2.1.274 기준 환경변수로 억제되지 않는 제품 자체 버그다. RC/대화는 정상 동작하지만 `--resume` 복원은 불가능하다.
- **tmux 조작 주의**: tmux 창 안에서 다시 `tmux attach`를 실행하면 중첩 세션 경고가 뜨며 Ctrl+b가 먹통처럼 보일 수 있다. 창 전환 시에는 `Ctrl+b`를 눌렀다 **손을 뗀 다음** 숫자 키(0/1/2)나 `w`를 눌러야 한다. 또한 사람(RC/attach)과 스크립트가 같은 창에 동시에 `send-keys`를 보내면 입력이 섞일 수 있으므로 주의해야 한다.
- 인증/쿼터 관리, 헬스 상태머신, 스티키 라우팅은 없음. 필요해지면
  `eraweb-fork/docs/workflows/multi_model_router.md`의 §5를 참고해 확장.

## 6. 변경 이력 (Changelog)

- **v2.3 (2026-11-03)**: 감독자 운영 지침 정본화 및 지침 계층 구조 정립
  - `SUPERVISOR.md` 정본 제정: 분배 토폴로지(`감독자 → 디스패처/워커 → 리드 → 워커`), agy 기본 리드/모델 판단 원칙, 작업 경중(소/중/대) 판정 및 지시서 규격 통합.
  - 지침 3층 구조 확립 (`docs/INSTRUCTION-LAYERS.md`): 체인 템플릿 → 프로젝트 지침(`projects/`) → 개인 메모리의 명확한 책임 분리.
  - agy 메인 에이전트 실작업 전량 위임(`invoke_subagent`) 및 메인 감사 도구(`bin/agy-main-audit.sh`) 도입.
  - 리드 모델 선택 기준 명확화: 간단한 리드(소·중)는 Terra 6.1 권장, 복잡·고난도 리드(대)는 Sol 기본.
  - 구조 계약(Structure Contracts) 및 구현 유연성 분리 (6대 도입 후보 완수):
    - `reason_code` 세분화 및 기계 판독용 표준 사유 코드 규격화 (`lib/reason.sh`, 이벤트 및 `state` 스키마 확장)
    - 비정상 중단 작업 복구 인계 자동 주입 (`RECOVERY_NOTE`, `bin/task-abandon.sh`, `--recover-from`)
    - 병렬 쓰기 작업 공간 격리를 위한 온디맨드 `git worktree` 격리 (`--worktree`) 및 회수 도구 (`bin/worktree-clean.sh`)
    - 터미널 멀티플렉서 추상화 레이어 도입 (`lib/term.sh`, `ACC_TERM_BACKEND=tmux|herdr` 지원)
    - Agy 모델 동적 해석 확장 (`bin/resolve-model.sh --harness agy`, `AGY_TIER`, `AGY_EFFORT` 연동)
    - 능동적 트리거를 위한 결정론적 게이트 및 `observe` 침묵 관찰 모드 (`lib/gate.sh`, `ACC_PROACTIVE_MODE=observe|notify|off`)
    - 구조 계약 및 구현 설계서 제정 (`docs/PRINCIPLES-structure-vs-implementation.md`, `docs/IMPL-DESIGN-20261102.md`)
- **v2.2 (2026-09-25)**: 상주 agy TUI 모드(Stage 1, 옵션) 및 격리 시험 스위트
  - `bin/agy-stop-hook.sh`: agy Stop 훅 기반 원자적 완료 통지
  - `watchdog-v2.sh`: 상주 워커 감시 (마감/스톨/pane 종료)
  - `tests/lib-isolated-env.sh`: 라이브 세션 시그니처 및 오염 불변 검증 하네스
- **v2.1 (2026-09-24)**: Full-Push 이벤트 통지 브리지 및 Durable Outbox
  - FIFO 기반 즉각 깨우기(`asyncRewake`) 및 4대 안전망 워치독
  - 커널 `flock` 기반 터미널 소유권 보장
- **v1.0 (2026-09-23)**: 4계층 명령계통 tmux 풀(pull) 기반 템플릿 최초 릴리스
