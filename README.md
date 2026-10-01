# Agent Command Chain — Template (MVP)

간소화된 4계층 멀티 에이전트 명령계통의 템플릿. `eraweb-fork`의 실전 구조
(Sol/Terra/agy 3역할 + Sonnet/Terra/Sol-Opus 3티어 라우터, `docs/workflows/`)를
관찰한 뒤, 더 가볍게 재구성한 것이다.

> [!IMPORTANT]
> **실행 경로 안내 (v2 기본 권장 / v1 폴백)**
> - **기본 권장 경로**: **v2 Full-Push 이벤트 통지 브리지** (`bootstrap-v2.sh`, `watchdog-v2.sh`, `bin/*`, `adapters/*`). FIFO 기반 즉각 깨우기(`asyncRewake`)와 4대 안전망 워치독을 통해 지연 없는 이벤트 통지와 신뢰성을 보장합니다. 상주 관찰이 필요한 경우 Stage 1 resident TUI 모드(`AGY_MODE=resident`)를 지원합니다.
> - **폴백/레거시 경로**: **v1 기본 브리지** (`bootstrap.sh`, `watchdog.sh`). 순수 tmux 화면 캡처(pull) 방식으로 동작하며, v2 환경 구성이 어렵거나 최소 세션 테스트 시의 폴백으로 완전 보존됩니다. (v1 파일은 절대 삭제되지 않음)
> - **세션 재개 런북**: 신규 Claude 세션에서 환경을 재현하거나 중단된 작업을 이어받을 때는 [`.llmwiki/session-resume.md`](.llmwiki/session-resume.md)를 참조하세요.

## 1. 역할 구조 (4계층)

| 계층 | 주체 | 역할 | 비고 |
|---|---|---|---|
| **0. 사용자** | 사람 | 최종 의사결정, 승인 | RC로 Sonnet 세션에 원격 개입 |
| **1. 감독/디스패치** | Claude Sonnet | 모니터링·감사, 작업 디스패치, 루프 관리, RC(원격제어) | 사람과의 상시 소통 창구. **사용자가 직접 요청하지 않는 한 실질 작업(문서/코드 작성/조사)은 스스로 하지 않고 agy/codex에 위임** |
| **2. 라우터 겸 워커 총괄** | agy (Gemini 3.8 Flash) | 난이도 판단(hard routing) 및 서브에이전트 위임·총괄 | **메인 세션 직접 작업(Read/Bash/Edit) 금지 및 서브에이전트(`invoke_subagent`) 위임.** v2 단발 실행(oneshot 한 턴) 라우팅 또는 Stage 1 상주 TUI 모드. 소환 불가/실패 시 직접 처리 금지 및 `[[BLOCKED <id>]] reason=subagent-unavailable\|needs-terra` 보고. (v1 상주 TUI responsive state는 legacy) |
| **2-내부 Tier 2** | Codex Terra | 중난이도 작업의 설계 및 직접 수행 | 별도 tmux 창(§2). agy가 자동 호출하진 않음 — 아래 "한계" 참고 |
| **2-내부 Tier 3** | Sol / Opus | 최고난도 문제, 전체 플래닝 상담 | 상시 창 없음. 필요할 때 codex 창의 커맨드를 Sol 계열 최신(`resolve-model.sh sol`)으로 바꿔 send-keys, 또는 claude 쪽은 `--model opus`로 즉석 실행 |

핵심 원칙:
1. **계층 1 Sonnet(감독)은 사람과의 상시 소통 창구이자 agy/codex의 모니터링·감사가 주 역할이다.** 사용자가 Sonnet에게 직접 수행하라고 명시적으로 요구하지 않는 한, 문서 작성/코드 작성/조사 같은 실질적인 작업은 스스로 직접 처리하지 않고 agy 또는 codex(Terra/Sol)에 위임해야 한다.
2. **계층 2 agy는 v2 Full-Push 환경에서 `WORKER_MODE=oneshot`의 단발 실행 한 턴으로 기동되며, 자신이 직접 Read/Bash/Edit 등의 툴을 호출해 파일 읽기/쓰기/수정 작업에 몰입하지 말고 내장 도구 `invoke_subagent`를 호출해 서브에이전트에 실작업을 위임해야 한다 (경량 작업 예외 없음).** 서브에이전트 소환이 불가능하거나 실패하면 조용히 직접 처리하지 말고 즉시 `[[BLOCKED <id>]] reason=subagent-unavailable|needs-terra`로 보고하여 Sonnet이 Codex Terra 등으로 재라우팅할 수 있게 해야 한다. (참고: v1의 "상주 대화형 TUI 라우터/responsive state"는 legacy이며, Antigravity TUI의 비선점형 입력 큐잉(`queued messages`)으로 인해 직접 툴 루프 시 상위 지시가 지연되는 문제가 확인되어 v2 유휴 셸 + oneshot 구조로 전환됨)

핵심 단순화: **Sonnet은 agy와 codex(Terra) 두 창만 직접 본다.** Sol/Opus는
상시 프로세스가 아니라 필요할 때만 커맨드를 바꿔 일회성으로 부르는 대상
(eraweb-fork 원본의 "Terra는 agy 보고를 정규화해 Sol에게만 전달, agy는
Sol에게 직접 보고 안 함" 규칙을 계층 자체로 끌어올린 것).

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
  `AGY_MODE=resident` 지정 시 agy가 tmux 창에 상시 대화형 TUI로 유지되며, 디스패처는 파일(`$task_dir/prompt.md`)과 1줄 도어벨 트리거로 안전하게 지시를 전달합니다. 완료 통지는 Antigravity CLI의 `Stop` 라이프사이클 훅(`bin/agy-stop-hook.sh`)을 통해 `fullyIdle: true` 시점에 발행됩니다.
- **세션 타겟팅 및 4계층 자동 해석**:
  `bin/dispatch.sh` 및 관련 도구들은 세션 오조준 방지를 위해 `SESSION_NAME` 4계층 자동 해석(환경변수 > 런타임 마커 > 현재 tmux 세션 > config.env) 및 v2 마커 가드(오조준 시 `exit 70`)를 내장하고 있습니다. 명시적인 `SESSION_NAME=agentchain-v2` 사용을 강력 권장합니다.

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
| **v2 (기본 권장)** | `bootstrap-v2.sh` | v2 원클릭: 런타임 디렉토리·FIFO 초기화 + tmux 세션 기동 + 워치독 v2 자동 백그라운드 구동 |
| | `watchdog-v2.sh` | 4대 비정상 상태(세션/Sonnet, PID증발, Stall/Deadline, 브리지 정체) 전담 안전망 |
| | `bin/event-emit.sh` | 원자적 이벤트 스풀링(Durable Outbox) 및 FIFO 펄스 전송 |
| | `bin/sonnet-event-wait.sh` | Claude Code asyncRewake 전용 FIFO 이벤트 대기 어댑터 (exit 2로 기상) |
| | `bin/event-ack.sh` | 처리 완료된 이벤트의 archive 이동 및 FIFO 드레인 |
| | `bin/dispatch.sh` | 작업 디스패처 (4계층 세션 자동 해석, v2 마커 검증, `--dry-run` 지원) |
| | `bin/agy-stop-hook.sh` | Stage 1 agy 상주 모드 Native Stop 훅 핸들러 |
| | `lib/session.sh` | 세션명 자동 해석 및 v2 런타임/마커 검증 공통 라이브러리 |
| | `bin/run-task.sh` | 커널 자동 해제형 terminal flock 기반 워커 프로세스 실행 및 상태/이벤트 발행 |
| | `adapters/agy-oneshot.sh` | agy 단발 실행기 어댑터 |
| | `adapters/codex-oneshot.sh` | Codex 단발 실행기 어댑터 |
| | `claude-bridge.settings.json` | Claude Code asyncRewake 훅 및 명령어 권한 설정 |
| **대시보드** | `dashboard/` | 상태 모니터링 대시보드 (읽기 전용, tailnet 바인딩, 작업 슬라이싱 및 민감 정보 마스킹) |
| **v1 (폴백/레거시)** | `bootstrap.sh` | v1 tmux 세션 생성 → sonnet/agy/codex 윈도우 기동 (순수 pull 기반, 자동 신뢰 확인/브리핑 지원) |
| | `watchdog.sh` | v1 세션/프로세스 생존 감시 및 단순 재기동 (순수 pull 기반) |
| **공통** | `config.env.example` | 세션명, 타임아웃, 브리지 모드 등 전체 환경설정 템플릿 |
| | `ROLES.md` | 역할별 프롬프트 템플릿 및 운용 가이드 |
| | `.llmwiki/` | 아키텍처 및 상세 사용법 위키 문서 |

## 4. 빠른 시작

**이 템플릿은 프로젝트 종속적이지 않다.** `bootstrap-v2.sh`나 `bootstrap.sh`를 실행한 디렉토리가 곧 대상 프로젝트가 됩니다 (세 tmux 창 모두 그 경로에서 시작). 경로를 고정하고 싶으면 `config.env`의 `PROJECT_DIR`을 채우면 됩니다.

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

# [Stage 1: agy 상주 TUI 모드로 기동할 경우]
# AGY_MODE=resident ~/agent-command-chain-template/bootstrap-v2.sh

# [상태 대시보드 기동 (선택사항, tailnet 전용 읽기 전용)]
# ~/agent-command-chain-template/dashboard/run.sh start

# [폴백/레거시 경로: v1 순수 pull 브리지]
# ~/agent-command-chain-template/bootstrap.sh
# ~/agent-command-chain-template/watchdog.sh &

tmux attach -t agentchain-v2                       # 직접 들어가서 보고 싶을 때 (v2 세션)
```

## 5. 알려진 한계 및 주의사항

- **RC 전제조건**: `claude --remote-control`은 claude.ai **Pro/Max/Team/Enterprise
  구독 + `claude /login` OAuth 로그인**이 사전에 되어 있어야 작동한다.
  API 키 인증만으로는 안 됨. Team/Enterprise는 조직 Owner가
  claude.ai/admin-settings/claude-code에서 RC를 켜둬야 한다. 세션 안에서
  `/remote-control` 을 치면 상태 패널(URL, 연결 상태)을 볼 수 있다.
- **agy → codex 자동 호출은 없다** (위 §2 마지막 항목). Sonnet이 사람 대신
  그 연결을 수동으로 메꾸는 구조다.
- **agy는 네이티브 Linux 빌드가 있다** (`~/.local/bin/agy` 등에 설치되는 형태 —
  2026-09 실측 확인). 비대화형 셸 환경을 위해 `bootstrap.sh`/`bootstrap-v2.sh`가 자체적으로
  `PATH="$HOME/.local/bin:$PATH"`를 추가해 확인한다. 정말 Windows 전용 빌드만 있는
  환경이라면 `config.env.example`의 주석에 적힌 `cmd.exe` 우회를 대신 써라.
- **codex는 대화형 TUI 모드로 구동된다**: one-shot인 `codex exec` 대신 지속형 TUI인 `codex`를 기본값으로 사용한다. WSL 환경에서는 `npm install -g @openai/codex@latest`를 WSL 안에서 직접 설치해야 리눅스 네이티브 바이너리가 정상 작동한다.
- **디렉토리 신뢰 프롬프트 자동 승인**: claude/codex의 신규 디렉토리 "trust this folder?" 프롬프트는 `--dangerously-skip-permissions`로 건너뛸 수 없어 스크립트가 화면 텍스트를 감지한 후 자동으로 키 입력을 전송한다 (`AUTO_CONFIRM_TRUST=true`).
- **세션 지속성 경고 (`CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`)**: 부모 Claude Code 세션 감지로 인한 transcript 저장 비활성화 경고는 v2.1.274 기준 환경변수로 억제되지 않는 제품 자체 버그다. RC/대화는 정상 동작하지만 `--resume` 복원은 불가능하다.
- **tmux 조작 주의**: tmux 창 안에서 다시 `tmux attach`를 실행하면 중첩 세션 경고가 뜨며 Ctrl+b가 먹통처럼 보일 수 있다. 창 전환 시에는 `Ctrl+b`를 눌렀다 **손을 뗀 다음** 숫자 키(0/1/2)나 `w`를 눌러야 한다. 또한 사람(RC/attach)과 스크립트가 같은 창에 동시에 `send-keys`를 보내면 입력이 섞일 수 있으므로 주의해야 한다.
- 인증/쿼터 관리, 헬스 상태머신, 스티키 라우팅은 없음. 필요해지면
  `eraweb-fork/docs/workflows/multi_model_router.md`의 §5를 참고해 확장.
