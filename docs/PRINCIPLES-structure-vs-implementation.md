# 구조(원칙·계약) vs 구현(하네스·툴·모델) 분리 설계 및 지속성·능동성 비교 분석

> 현행 감독자 운영은 [SUPERVISOR](../SUPERVISOR.md), 문서 소유·참조는 [3층 지침](INSTRUCTION-LAYERS.md)을 따른다. 아래 모델·프로젝트 사례와 적용 계획은 작성 당시 분석이며, 구조 계약의 정본은 PRINCIPLES §2다.

> **상태**: 구현 완료 (v2.3)  
> **작성일**: 2026-11-02  
> **대상 시스템**:  
> - **우리 체인**: `/home/rerun/agent-command-chain-template` (`tmux` + Claude Sonnet 감독자 + `agy`/`codex` 워커, 파일 기반 Outbox·FIFO·워치독·센티널·`asyncRewake`·인계 문서)  
> - **비교 대상**: DeepSeek `dsh-superagent-prototype` (`https://github.com/deepseek-ai/dsh-superagent-prototype`, 커밋 `6337fc823ce20251c8de8085948165c81aaa701e`, MIT, 2026-10-31 공개)

---

## 1. 한 화면 요약

### 1.1 핵심 결론
1. **설계 방침 — "구조는 계약으로 고정하고, 구현은 교체 가능한 슬롯으로 열어 둔다"**
   - AI 에이전트 하네스(`claude`, `agy`, `codex`, `dsh`), 터미널 멀티플렉서(`tmux`, `herdr`), 모델 패밀리(`gpt-5.x-sol/terra`, `gemini-4-argon`, `claude-sonnet/opus-5.5`)는 수개월 단위로 바뀐다.
   - 반면 **"누가 결정하고 누가 실행하는가(책임 경계)"**, **"작업 상태와 완료 신호의 단일 진실 공급원(SSOT)은 무엇인가(지속성 계약)"**, **"언제 에이전트가 깨어나고 언제 침묵하여 사람의 주의력을 아끼는가(능동성 계약)"**는 하네스나 모델이 바뀌어도 변하지 않는 **구조(Structure)**다.
2. **지속성(Persistence) 비교 요약**
   - **DeepSeek `dsh-superagent`**: 단일 SQLite WAL 파일(`orchestrator.sqlite`)에 도메인 상태(`ticket`, `thread`, `decision`), 내구성 부수효과 큐(`effect`), 발신함(`outbox`), 소스 커서(`kv`)를 통합하고, **티켓(Ticket) 수명주기**와 **실행기(Executor/DSH 세션 + 티켓별 `git worktree`) 수명주기**를 분리했다. 호스트 재시작 시 중단된 작업에 `RECOVERY_NOTE`를 붙여 자동 재개하며, 종료된 티켓도 구독 스레드에 사람의 후속 메시지가 오면 기존 위임장(`Mandate`)으로 즉시 깨어난다(`wake()`).
   - **우리 체인(`agent-command-chain-template`)**: 외부 DB나 데몬 없이 순수 파일시스템 원자적 이동(`.tmp` → `mv`), 커널 `flock`, 프로세스 시작 토큰(`proc_token`, `/proc/<pid>/stat` 22번째 필드), 결정론적 종단 이벤트 ID(`${task_id}-terminal`), 4중 안전망 워치독(`watchdog-v2.sh`)만으로 높은 장애 복구력을 달성했다. 다만 **1회성 `Task` 위에 다회성 `Ticket` 개념이 없고**, **모든 워커가 단일 `PROJECT_DIR` 작업 트리를 공유**하며, **`tmux` 호출이 스크립트 전반에 산재**해 있다는 빈틈이 있다.
3. **능동성(Proactivity) 비교 요약**
   - **DeepSeek `dsh-superagent`**: 외부 소스 폴링(GitHub 알림/PR/스레드/레포 관찰 스트림, Feishu) + 2초 주기 `Passes` 루프로 움직이되, **Rules-then-LLM 2단계 필터**(결정론적 `policy.gate` → LLM `MasterAssessor` 규칙 전수 대조)와 **`observe` 모드(기본값: 실제 실행·발화 없이 `would_accept=true`만 감사 기록)**, **3회 발화 규율(착수 ACK 1회 / 블로커·질문 1회 / 완료 보고 1회, 진행 중 침묵)**로 사람의 주의력을 철저히 보호한다.
   - **우리 체인**: 철저히 **인간 승인 중심 반응형("논의 ≠ 승인")**으로 동작하며, `sonnet-event-wait.sh`의 `drain_noise()`가 유휴 시 `started`/`canceled`/`bridge_degraded` 소음을 무음 아카이브하여 감독자 컨텍스트를 보호한다. 외부 이벤트 수집기나 `observe` 모드는 아직 없으나, 기존 `drain_noise`와 워치독 루프를 **결정론적 게이트 계약**으로 승격할 수 있다.
4. **구조 vs 구현 핵심 매핑 (요약)**
   - 새로운 절차를 발명하는 대신 현재 체인에 이미 검증된 **Outbox(`pending→inflight→archive`), 센티널(`[[DONE]]`/`[[BLOCKED]]`), 워치독(`watchdog-v2.sh`), 인계 파일(`RESUME_HANDOFF.md`), 노이즈 드레인(`drain_noise`)**을 **불변 구조 계약**으로 승격하고, `tmux`/`herdr`, `agy`/`codex`/`claude`, 모델 탐색(`codex debug models`, `agy models`), 기상 훅(`asyncRewake`, `Stop` 훅)은 **어댑터·레지스트리 계층**으로 캡슐화한다.

---

## 2. 구조(원칙·계약)

하네스(Claude Code, Antigravity CLI, Codex CLI, DSH 등), 멀티플렉서(`tmux`, `herdr`), 기반 모델이 무엇으로 교체되더라도 절대 깨지지 않아야 하는 **6대 구조적 원칙과 계약(Invariants & Contracts)**이다. 복잡한 승인 절차를 새로 만들지 않고, 현재 체인의 핵심 메커니즘을 계약으로 명문화한다.

### 2.1 계약 1: 책임 경계와 권한 분리 계약 (Role & Authority Boundary)
- **불변 원칙**:
  1. **감독자/라우터(Supervisor/Router)의 비차단(Non-blocking) 원칙**: 감독자는 요구사항 정제, 작업 분배(Dispatch), 결과 감사(2-tier Audit), 인간 보고만 수행한다. 직접 코드를 수정하거나 장시간 블로킹되는 빌드·테스트 루프를 돌리지 않는다(DeepSeek의 `noWait` 가드 및 우리 체인의 `ROLES.md` "No-Bash/No-Edit 직접 구현 금지" 원칙).
  2. **논의 ≠ 승인 (Explicit Mandate Invariant)**: 인간의 탐색적 질문이나 설계 논의는 실행 위임장(Mandate)이 아니다. 파괴적 조작, 외부 원격 반영(`git push`), 대규모 구현 착수는 명시적 위임(인간의 승인 또는 사전 합의된 명시적 트리거)이 있을 때만 개시한다.
  3. **신뢰 경계 코드 강제 (Host-Enforced Boundary)**: 외부 텍스트(이슈 본문, PR 코멘트, 로그 출력)는 신뢰할 수 없는 데이터(Untrusted Data)로 취급하며, 허용 주체·격리 환경·민감정보 마스킹은 프롬프트가 아니라 호스트/래퍼 코드가 강제한다.

### 2.2 계약 2: 작업 단위(Task/Ticket) 수명주기 및 작업 공간 격리 계약
- **불변 원칙**:
  1. **작업 상태 기계(State Machine)**: 모든 단위 작업(`task_id`)은 최소한 아래의 단방향 종단 상태 전이를 가진다.
     - `pending`(또는 생성) → `running` → 종단 상태(`done` | `error` | `canceled` | `abandoned`)
     - 한 번 종단 상태에 도달한 `task_id`는 다시 `running`으로 되돌아가지 않는다(재시도나 후속 라운드는 새 `task_id`를 부여하되 상위 문맥/티켓 ID로 연결 가능).
  2. **단일 활성 리스(Single Active Lease)**: 동일 워커 슬롯(또는 동일 작업 공간)에는 동시에 하나의 활성 작업만 배타적 락(`flock`)과 프로세스 신원 토큰(`pid` + `proc_token`)을 보유한다.
  3. **작업 공간 격리(Workspace Isolation)**: 병렬 워커가 동시에 파일을 수정할 때 인덱스나 워킹 트리가 충돌하지 않도록, 읽기 전용 분석이 아닌 병렬 쓰기 작업은 격리된 작업 트리(예: `git worktree` 또는 명시적 파일 소유권 분리)를 계약으로 보장해야 한다.

### 2.3 계약 3: 내구성 Outbox 및 멱등 종단 이벤트 계약 (Durable Outbox & Idempotency)
- **불변 원칙**:
  1. **선(先) 영속화, 후(後) 알림(Write-Before-Pulse)**: 모든 상태 전이와 이벤트는 알림 신호(FIFO pulse, 훅 종료 코드, 웹훅/소켓 이벤트)를 발생시키기 **전에** 비휘발성 저장소(파일시스템 원자적 `rename` 또는 트랜잭션 DB)에 먼저 기록되어야 한다. 알림 채널이 유실되어도 저장소 스캔만으로 100% 복구 가능해야 한다.
  2. **3단계 전달 보장 (`pending` → `inflight` → `archive`)**:
     - 생산자(워커 래퍼, Stop 훅, 워치독)는 `pending/`에 원자적으로 이벤트를 생성한다.
     - 소비자(감독자 브리지)는 처리 직전 `inflight/`로 선점(`claiming` → `awaiting_ack`)하고, 감독자가 실제로 인지·처리한 뒤에만 명시적 ACK(`event-ack.sh`)를 통해 `archive/`로 이동한다.
     - 소비자가 죽거나 타임아웃되면 워치독이 `inflight/` 이벤트를 `pending/`으로 롤백하여 **최소 1회 이상 전달(At-least-once delivery)**을 보장한다.
  3. **결정론적 종단 이벤트 ID (`<task_id>-terminal`)**: 정상 종료, 훅 감지, 워치독 강제 회수 중 어느 경로가 먼저 실행되더라도 `<task_id>-terminal` 단일 키와 커널 배타 락(`terminal.lock`)으로 경쟁 조건을 직렬화하여 **작업당 종단 이벤트는 정확히 1회만 발행**된다.
  4. **내용(Content)과 완료 신호(Completion SSOT)의 분리**: 워커의 화면 출력이나 센티널(`[[DONE]]`, `[[BLOCKED]]`)은 사람이 읽는 **보고서 내용**일 뿐이며, 시스템 차원의 **완료 진실 공급원(SSOT)**은 오직 Outbox 종단 이벤트다.

### 2.4 계약 4: 완료 판정 및 2층 감사 계약 (Completion & Two-Tier Audit)
- **불변 원칙**:
  1. **유휴 상태 + 시작 시각 이후 최종 응답 검증**: 상주형(Resident) 워커의 완료는 (a) 하위 서브에이전트와 백그라운드 툴을 포함한 전체 유휴 상태(`fullyIdle === true`) 및 (b) 작업 `started_epoch` 이후 기록된 최종 모델 응답 내 종단 센티널(`[[DONE]]` 또는 `[[BLOCKED]]`)의 결합으로 판정한다.
  2. **교차 감사(Cross-Verification)**: 워커의 자기 보고(`status: done`, `refs_loaded` 선언)를 맹신하지 않는다. 감독자(또는 감사 루틴)는 (a) 1층 요약 보고와 (b) 2층 물리적 증거(실제 `git diff`/`git status`, 테스트 실행 결과, 트랜스크립트 내 실제 파일 열람 흔적)를 대조한다.

### 2.5 계약 5: 장애 복구 및 컨텍스트 인계 계약 (Recovery & Handoff)
- **불변 원칙**:
  1. **커널 자동 해제 락 + PID 재사용 방지**: 비정상 종료(`SIGKILL`, 전원 차단) 시 데드락이 남지 않도록 파일 존재 여부가 아닌 커널 디스크립터 락(`flock`)을 사용하고, PID 생존 확인 시 반드시 프로세스 시작 시각 토큰(`proc_token`)을 대조한다.
  2. **4중 안전망 감시(Watchdog Invariants)**: 주기적 감시자는 (a) 감독자 세션 생존 확인 및 부활, (b) 죽은 워커 PID/세션 회수, (c) 절대 데드라인(`timeout`) 및 무출력 정체(`stall`) 감지 + 누락된 상주 센티널 보정, (d) 미확인(`inflight`) 이벤트 롤백 및 브리지 불능 시 비상 알림을 수행한다.
  3. **컨텍스트 회전 및 인계 문서(`HANDOFF_FILE`) 계약**: 감독자나 워커의 컨텍스트 창이 한계에 도달하거나 세션이 재시작될 때, 진행 중인 목표·완료된 커밋·남은 블로커·미처리 이벤트를 정형화된 인계 상태(`docs/RESUME_HANDOFF.md` 및 중단 작업 복구 노트)로 남겨 무손실 재개를 보장한다.

### 2.6 계약 6: 능동성 및 인간 주의력 보존 계약 (Proactivity & Attention Economy)
- **불변 원칙**:
  1. **Rules-then-LLM (결정론적 게이트 우선)**: 모든 내부·외부 이벤트는 LLM(감독자)을 깨우기 전에 **비용 0의 결정론적 필터(Deterministic Gate)**를 먼저 통과해야 한다. 자기 자신이 발생시킨 이벤트, 유휴 상태의 단순 시작/취소 소음(`drain_noise`), 권한 없는 발신자, 중복 알림은 코드 수준에서 즉시 소거하거나 조용히 아카이브한다.
  2. **단계적 자율성 (`off` | `observe` | `auto`)**: 새로운 능동적 트리거(주기 점검, 외부 이슈/PR/CI 감시 등)를 도입할 때는 즉시 실행(`auto`)하지 않고, 반드시 **판단 결과만 로그에 남기고 실제 워커 디스패치나 외부 발화는 하지 않는 관찰 모드(`observe`, dry-run)**를 거친다.
  3. **침묵 규율 (Speak Only When Actionable)**: 에이전트는 (a) 작업 수락/착수 확인, (b) 인간의 결정이나 권한이 필요한 구체적 블로커(`[[BLOCKED]]`), (c) 검증 완료된 최종 산출물(`[[DONE]]`) 시점에만 인간을 호출한다. 정상적인 중간 진행 상태나 불확실한 추측으로 인간의 주의력을 소모하지 않는다.

---

## 3. 구현(교체 가능 계층과 조합 방식)

구조 계층의 6대 계약만 준수하면, 아래 4개 구현 계층은 AI 생태계의 발전에 따라 언제든 다른 도구·하네스·모델로 교체하거나 조합할 수 있다.

### 3.1 계층 1: 터미널/세션 멀티플렉서 어댑터 (`tmux` ↔ `herdr` 등)
- **현재 구현**: `bootstrap-v2.sh`, `lib/session.sh`, `bin/dispatch.sh`, `bin/session-init.sh`, `watchdog-v2.sh`, `bin/agy-stop-hook.sh`에 `tmux` 명령어(`has-session`, `list-panes`, `send-keys`, `load-buffer`, `paste-buffer`, `capture-pane`)와 `$TMUX_PANE` 환경변수가 직접 하드코딩되어 있다.
- **교체 및 추상화 방식 (`lib/term.sh` 얇은 래퍼 규격)**:
  - 앞서 수행된 Herdr 스파이크 실험(`/mnt/c/Users/rerun/llm-wiki/Research/2026-10-31-herdr-spike-results.md`)에서 확인되었듯, 멀티플렉서가 제공해야 할 구조적 계약은 **7개 프리미티브**뿐이다:
    1. `term_session_exists <session>`
    2. `term_pane_alive <pane_id>`
    3. `term_pane_pid <pane_id>`
    4. `term_send_line <pane_id> <text>` (상주형 워커에 1줄 도어벨 전송 또는 비상 텍스트 주입)
    5. `term_run_cmd <pane_id> <cmd>` (단발성 명령 기동)
    6. `term_capture_tail <pane_id> <lines>` (진단용 화면 스냅샷)
    7. `term_current_pane_id` (`$TMUX_PANE` 또는 `$HERDR_PANE_ID` 추상화)
  - **주의할 구현 특성**: `herdr`의 자체 상태 머신(`running`/`idle`/`done`)이나 비영속 인메모리 소켓(`events --Wait`)은 완료 SSOT 계약을 만족하지 못하므로 무시하고, 오직 PTY 관리·프로세스 조회(`herdr pane process-info`)·입력 전달 수단으로만 사용한다.

### 3.2 계층 2: 에이전트 하네스 및 워커 실행기 어댑터 (`adapters/*.sh`, `WORKER_MODE`)
- **현재 구현**:
  - **상주형(Resident)**: `agy` TUI가 워크스페이스 인덱스·LSP·MCP 연결을 메모리에 유지한 채 상주하고, `dispatch.sh`가 1줄 도어벨 프롬프트(`Read and execute the task in ...`)를 보내며, 완료는 `.gemini/settings.json`에 등록된 `bin/agy-stop-hook.sh`(`Stop` 훅)가 감지한다.
  - **단발성(Oneshot)**: `bin/run-task.sh`가 `adapters/codex-oneshot.sh`(`codex exec -m ... -C ... -`) 또는 `adapters/agy-oneshot.sh`(`agy --dangerously-skip-permissions -p ...`)를 서브쉘로 실행하고 종료 코드를 수거한다.
- **교체 및 조합 방식 (어댑터 계약)**:
  - 어떤 신규 하네스(예: `claude -p`, `dsh`, 오픈소스 코딩 에이전트 CLI)가 나오더라도 아래 두 가지 어댑터 규격 중 하나만 맞추면 즉시 체인에 편입된다:
    1. **Oneshot 어댑터 규격 (`adapters/<name>-oneshot.sh <prompt_file> <project_dir> <task_id>`)**: 표준 출력/에러를 스트리밍하고 종료 코드(`0` 또는 비정상 코드)를 반환하며, 마지막 출력에 `[[DONE]]` 또는 `[[BLOCKED]]` 요약을 남긴다. Outbox 이벤트 발행과 락 관리는 공통 래퍼(`bin/run-task.sh`)가 전담한다.
    2. **Resident 어댑터 규격**: (a) 1줄 파일 포인터 프롬프트 수신 가능, (b) 트랜스크립트/로그 파일의 mtime 갱신(워치독 무출력 정체 감지용), (c) 완전 유휴 도달 시 `bin/event-emit.sh`를 호출하는 라이프사이클 훅(또는 워치독의 `reconcile_resident_sentinel`과 호환되는 JSONL 트랜스크립트) 제공.

### 3.3 계층 3: 모델 카탈로그 탐색 및 능력 기반 해석기 (`bin/resolve-model.sh`)
- **현재 구현**:
  - `bin/resolve-model.sh`는 모델 이름을 코드에 고정하지 않고, 역할/티어(`CODEX_DEFAULT_TIER="sol"`, `CODEX_DEEP_TIER="terra"`)와 추론 강도(`CODEX_DEFAULT_EFFORT="high"`, `CODEX_DEEP_EFFORT="xhigh"`)를 입력받아 `timeout 30 codex debug models`(실패 시 `~/.codex/models_cache.json` 폴백)로 `visibility == "list"`인 `gpt-<버전>-<티어>` 중 최고 버전을 동적으로 선택한다.
  - 반면 `agy`의 경우 현재 `bin/resolve-model.sh`에서 `echo "${AGY_MODEL:-}"`만 반환하고 있으나, 실제 CLI 확인 결과 `agy models` 명령이 사용 가능한 모델 목록(`gemini-4-argon-high`, `gemini-3.8-flash-high`, `claude-opus-5-5-high`, `claude-sonnet-5-5-high`, `gpt-oss-120b-medium` 등)을 탭 구분 형식으로 반환하며 `--model` 및 `--effort (low|medium|high|xhigh|max)` 플래그를 지원한다.
- **교체 및 확장 방식 (Capability Resolver 패턴)**:
  - **구조적 요구사항**: 작업 디스패치 시 구체적인 모델 ID 대신 **요구 능력/티어(`fast` | `default` | `deep` | `review`)**와 **추론 강도(`low` | `medium` | `high` | `xhigh`)**를 지정한다.
  - **구현 해석기(`bin/resolve-model.sh`)**:
    - `codex`: `codex debug models` / `~/.codex/models_cache.json`에서 최신 `sol`/`terra` 모델 자동 탐색.
    - `agy`: `agy models` 출력(및 로컬 캐시)에서 패밀리/티어 규칙(예: `gemini-*-argon-high`, `claude-opus-*-high`)으로 최신 모델 자동 매핑.
    - **쿼터 고갈·일시 장애 폴백**: 한 하네스/프로바이더가 쿼터 초과(`429` 등)로 `[[BLOCKED]]` 또는 `error`를 반환하면, 동일 `prompt.md`를 다른 하네스 슬롯(예: `codex` → `agy`)으로 재디스패치한다.

### 3.4 계층 4: 기상(Wake) 브리지 및 알림 전송 계층
- **현재 구현**:
  - 생산자(`event-emit.sh`)가 Outbox 파일 생성 후 `event.fifo`에 비차단 1바이트(`1\n`)를 쓰고, 소비자(`sonnet-event-wait.sh`)가 `read -t`로 깨어나 Claude Code의 `SessionStart`/`Stop` 훅 규격(`asyncRewake: true` + `exit 2` + `stderr` JSON 페이로드)으로 감독자를 깨운다.
  - 훅이 죽었을 때는 워치독이 15분 쿨다운으로 `tmux paste-buffer` 비상 알림을 주입한다.
- **교체 방식**:
  - 감독자 하네스가 Claude Code에서 다른 CLI나 데몬으로 바뀌더라도 **Outbox 디렉터리(`pending/`, `inflight/`, `archive/`)와 `event-ack.sh` 계약은 그대로 유지**하고, `sonnet-event-wait.sh`에 해당하는 **소비자 어댑터만 교체**(예: `--input-format stream-json` 파이프 주입, MCP 알림, 로컬 소켓 트리거 등)하면 된다.

### 3.5 지속성·능동성 기준 [구조(계약) vs 구현(교체 가능)] 분류 종합표

| 영역 | 세부 항목 | 구조 계층 (불변 원칙·계약) | 구현 계층 (교체·조합 가능한 수단) |
| :--- | :--- | :--- | :--- |
| **지속성** | **상태 저장소 (Store)** | 작업 상태(`pending/running/done/error/canceled/abandoned`)와 이벤트의 원자적 기록, 단일 진실 공급원(SSOT) 보장 | 우리: 파일시스템 원자적 `.tmp` → `mv` (`$ACC_RUNTIME/tasks`, `events`)<br>DeepSeek: 단일 SQLite WAL 파일 (`orchestrator.sqlite`) |
| **지속성** | **이벤트 전달 (Outbox)** | 선(先) 영속화 후(後) 알림, `pending → inflight → archive` 3단계 수명주기, 명시적 ACK 전까지 유실 금지, 결정론적 종단 키(`<task_id>-terminal`) | 우리: 디렉터리 이동 + `event.fifo` 1바이트 펄스 + `event-ack.sh`<br>DeepSeek: `outbox` 테이블 + `effect` 큐(`generation`, `seq`, 지수 백오프) |
| **지속성** | **동시성·락 및 프로세스 신원** | 동일 슬롯/종단 처리의 배타적 직렬화, 비정상 종료 시 락 자동 해제, PID 재사용 오판 방지 | 우리: 커널 `flock`(`terminal.lock`, `watchdog.lock`) + `/proc/<pid>/stat` 필드 22(`proc_token`)<br>DeepSeek: 런타임 디렉터리 락 + 단일 프로세스 `Engine` 트랜잭션(`SAVEPOINT`) |
| **지속성** | **작업 공간 격리** | 병렬 쓰기 작업 간 워킹 트리·인덱스 충돌 금지 | 우리(현재): 단일 `PROJECT_DIR` 공유 (개선 후보: 옵션형 `git worktree`)<br>DeepSeek: 티켓별 `executor.prepare` 훅으로 `<worktreeName>-<ticketId>` 독립 생성 |
| **지속성** | **워커 실행 및 모델 선택** | 프롬프트 파일 입력 → 작업 수행 → 종단 센티널(`[[DONE]]`/`[[BLOCKED]]`) 및 종료 상태 반환, 티어/능력 기반 모델 지정 | 하네스: `agy` (상주 TUI / 단발 `-p`), `codex exec`, `claude -p`, `dsh`<br>멀티플렉서: `tmux`, `herdr`<br>모델 탐색: `bin/resolve-model.sh` (`codex debug models`, `agy models`) |
| **지속성** | **장애 복구 및 인계** | 감독자/워커 사망 감시, 데드라인·무출력 정체 회수, 미확인 이벤트 롤백, 컨텍스트 회전 시 상태 인계 및 중복 외부 부수효과 방지 | 우리: `watchdog-v2.sh` (60초 4중 안전망) + `session-init.sh` + `docs/RESUME_HANDOFF.md`<br>DeepSeek: `DshExecutor.resume()` (`RECOVERY_NOTE` 주입 + `requeueUntaken`) + 외부 1시간 주기 헬스체크 |
| **능동성** | **기상(Wake) 및 트리거 수용** | 이벤트 발생 시 감독자를 비차단으로 깨우되, 유휴 상태에서는 CPU/토큰을 소모하지 않음 | 우리: `sonnet-event-wait.sh` (`Claude` `asyncRewake` `exit 2` + `event.fifo`)<br>DeepSeek: `Passes` (2초 주기 `setInterval` + 코얼레싱) + GitHub/Feishu 폴러 |
| **능동성** | **소음 차단 (Rules-then-LLM)** | LLM을 깨우기 전 결정론적 규칙(Gate)으로 자기 메시지·유휴 소음·무권한 이벤트를 비용 0으로 선제 차단 | 우리: `sonnet-event-wait.sh`의 `drain_noise()` (`started`/`canceled`/`bridge_degraded` 무음 아카이브)<br>DeepSeek: `policy.gate` (발신자·멘션·봇·3칸 Admission Table) + LLM `MasterAssessor` |
| **능동성** | **자율성 단계 제어** | 검증되지 않은 능동 규칙의 즉시 실행 금지 (`off → observe → auto` 단계적 승격), "논의 ≠ 승인" | 우리: `ROLES.md` 인간 명시 승인 원칙<br>DeepSeek: `AcceptMode` (`observe` 기본값 — `audit.would_accept = true`만 기록하고 실행 억제) |
| **능동성** | **발화 규율 (인간 주의력 보존)** | 착수 확인(ACK)·실질적 블로커/질문·검증 완료 시점에만 인간에게 알림, 중간 진행 소음 억제 | 우리: 종단 이벤트(`done`/`error`/`stalled`) 중심 기상 + 2층 감사 후 요약 보고<br>DeepSeek: 3회 발화 원칙 + `progress` 내부 기록 + 채팅 100단어 제한(`checkedImText`) |

---

## 4. 지속성 분석 (DeepSeek `dsh-superagent` vs 우리 체인)

### 4.1 DeepSeek `dsh-superagent`의 지속성 메커니즘 (실제 확인한 코드 근거)
1. **단일 SQLite WAL 저장소 (`src/app/store.ts`, `docs/007-architecture.md`)**
   - `<stateDir>/orchestrator.sqlite` 단일 파일에 `WAL` 저널 모드와 `synchronous=FULL`을 설정하고, 프로세스 시작 시 `Runtime`이 디렉터리 락을 먼저 획득해 다중 프로세스 동시 쓰기를 원천 차단한다.
   - `trigger`, `disposition`, `trigger_thread`, `thread`, `ticket`, `decision`, `effect`, `outbox`, `kv` 9개 테이블을 단일 스키마로 관리한다.
   - 2초마다 도는 `Passes` 루프가 전체 이력을 풀스캔하지 않도록 `ticket.open`, `ticket.active`, `thread.active`, `thread.subscribed`, `outbox.pending` 등 **SQLite Generated Column과 Partial Index**를 사용하여 활성 작업 집합(Working Set)만 상수 시간에 가깝게 조회한다.
   - 중첩 호출이 가능한 `atomic` 헬퍼(`SAVEPOINT orchestrator_atomic_<level>`)와 `afterCommit` 콜백 큐를 통해, 도메인 상태 변경이 커밋된 직후에만 외부 I/O가 트리거되도록 보장한다.
2. **내구성 부수효과 큐 (`src/app/effects.ts`) 및 발신함 (`src/app/outbox.ts`)**
   - `Engine`은 외부 API나 실행기를 직접 호출하지 않고 트랜잭션 내부에서 `effect` 행(`assessment`, `launch`, `delivery`, `relay`, `notice`, `reply` 6종)을 적재한다(`ON CONFLICT(kind, key) DO UPDATE`로 `generation`만 올리고 순번 `seq`는 보존).
   - 실패 시 지수 백오프로 재시도하며, 선행 조건 미충족 시 `Later` 예외를 던져 실패 횟수 증가 없이 다음 틱으로 지연시킨다. 종료 시 `drain()`은 인플라이트 핸들러만 기다리고 미처리 행은 SQLite에 남겨 다음 기동 시 이어받는다.
   - `Outbox`(`src/app/outbox.ts`)는 발신 영수증(`Receipt`: `pending | sent | dropped`)과 `reply` 이펙트를 한 트랜잭션에 묶고, `store.receipts.behind(id, 3)`로 **동일 티켓·동일 대화 채널 내 메시지 발송 순서(착수 확인 → 최종 결과)**와 영구 중복 제거를 보장한다.
3. **티켓(`Ticket`)과 할당(`Assignment` / 워크트리) 수명주기의 분리 (`src/core/tickets.ts`, `src/core/assignments.ts`, `src/executor/dsh.ts`)**
   - **티켓 수명주기**: 티켓은 `open`에서 시작해 `completed | merged | closed | dropped | stopped` 사유와 함께 `ended`가 된다. GitHub PR과 연결된 경우(`follows`), 워커가 작업을 마쳐도 PR이 실제로 머지되거나 닫힐 때까지 티켓이 열린 상태를 유지하거나 PR 결과에 맞춰 자동 종결된다(`followsDone`).
   - **종료 티켓의 재기상(`wake()`)**: 티켓이 종료되어도 위임장(`Mandate`)과 스레드 구독(`Subscriptions`)은 보존된다. 구독 중인 스레드에 **사람(봇/CI 제외)**의 후속 메시지가 올라오면 `policy.revalidate(mandate)` 검증 후 새 `Eval`이나 `Decision` 없이 기존 티켓을 즉시 다시 열어(`wake()`) 후속 작업을 이어간다(`src/core/intake.ts`, `src/core/tickets.ts`). 오직 사람의 명시적 `stop`만이 위임장을 영구 철회한다.
   - **미열람 후속 입력 반환(`letGo` / `inputsRead`)**: 각 입력(`Input`)은 워커 전달 시각(`deliveredTo`)과 실제 세션 히스토리 진입 시각(`readAt`, `session/event`의 `user/message` 수신 시 기록)을 구분한다. 티켓이 닫히는 순간 워커가 아직 읽지 못한(`readAt === null`) 사람의 후속 메시지가 있다면 이를 삼키지 않고 `Eval` 대기열로 되돌려 독립적인 새 요청으로 평가받게 한다.
   - **티켓별 영속 세션과 격리 워크트리(`src/executor/dsh.ts`)**: 각 티켓은 고유 DSH 세션(`worker-<ticketId>`)과 격리 디렉터리(`<executionRoot>/<worktreeName>-<ticketId.slice(0,12)>`)를 갖는다. 최초 기동 시 `executor.prepare` 훅(기본 `git worktree add`, 타임아웃 10분)으로 1회 초기화하며, 초기화 실패(`PrepareFailed`) 시 불완전한 디렉터리를 정상 워크스페이스로 오인하지 않도록 자동 재시도를 막고 즉시 차단한다.
4. **재시작 및 장애 복구 (`DshExecutor.resume`, `SETUP.md`)**
   - 호스트 재시작 시 `DshExecutor.resume()`가 열린 티켓을 오래된 순으로 순회한다. 턴 실행 도중 끊겼거나 마지막 보고 이후 미열람 입력이 있던 활성 `Assignment`는 `hostRestarted`로 종결 처리하고, 즉시 새 `Assignment`를 생성하면서 **`RECOVERY_NOTE`**(*"호스트가 재시작되었으니 외부 작업이나 커밋을 반복하기 전에 현재 워크트리, git 히스토리, GitHub 상태를 먼저 확인하라"*)와 미열람 입력(`requeueUntaken`)을 함께 주입한다.
   - 일시적 네트워크/프로바이더 오류(HTTP 408, 429, 5xx, `ECONNRESET` 등)는 15초·30초·60초 백오프로 최대 3회 자동 재시도한다.
   - 데몬 외부에서는 운영자의 개인 DSH 프로필에 등록된 1시간 주기 크론 작업이 `scripts/dsh-superagent status` 종료 코드(0=정상, 1=재시작 가능 런타임 오류, 2=컴포넌트 오류, 3=설정 필요)를 확인하고 코드 `1`일 때 `--respect-stop restart`를 수행한다(`docs/006-deployment.md`).

### 4.2 우리 체인(`agent-command-chain-template`)의 지속성 현황과 강점·빈틈 (실제 확인한 코드 근거)
1. **현재 구조와 강점 (무데몬·파일시스템 기반 고신뢰성)**
   - **격리된 런타임 디렉터리 (`lib/config.sh`)**: `runtime/<session_safe>-<proj_hash>`(`$ACC_RUNTIME`) 아래에 `events/{pending,inflight,archive}`, `tasks/<task_id>/{prompt.md,state,output.log,terminal.lock}`, `workers/<worker>.{busy,lock,resident,transcript}`, `event.fifo`, `bootstrap_version`, `session_name`, `retired`를 구성하여 동일 호스트의 다중 프로젝트를 완벽히 분리한다.
   - **원자적 쓰기와 결정론적 종단 키 (`bin/event-emit.sh`, `bin/run-task.sh`, `bin/agy-stop-hook.sh`)**: 모든 상태 파일과 이벤트 JSON은 `.tmp.$$`에 쓴 뒤 `mv`로 원자적 교체한다. 특히 종단 이벤트는 생성 주체(`run-task.sh`, `agy-stop-hook.sh`, `watchdog-v2.sh`)를 불문하고 `${task_id}-terminal` 단일 ID와 `tasks/<task_id>/terminal.lock`(`flock`)을 공유하여 중복 발행을 원천 차단한다.
   - **PID 재사용 방지 토큰 (`lib/session.sh`, `bin/dispatch.sh`, `watchdog-v2.sh`)**: `/proc/<pid>/stat`의 `)` 뒤 20번째 토큰(전체 22번째 필드 `starttime`)을 `proc_token`으로 기록하고 `pid_alive()` 검사 시 대조하여, 리눅스 커널의 PID 재사용으로 인한 유령 프로세스 오판을 막는다.
   - **4중 안전망 워치독 (`watchdog-v2.sh`)**:
     - `--close` 자기 래핑으로 `watchdog.lock` 파일 디스크립터가 자식 프로세스(부활시킨 `tmux` 서버 등)로 누수되는 것을 방지한다.
     - 60초 주기로 ① `tmux` 세션 및 Sonnet 감독자 생존 확인·부활, ② 죽은 워커 PID/창 회수, ③ 작업 데드라인(`DEFAULT_TASK_TIMEOUT=1800`)·무출력 정체(`NO_OUTPUT_WARN_SECONDS=900`, 상주형 `agy`는 `agy.transcript` mtime 기준) 감지 및 상주형 누락 센티널 역산 보정(`reconcile_resident_sentinel`), ④ `inflight` 이벤트(`claiming` 30초, `awaiting_ack` 600초) `pending` 롤백 및 브리지 사망 시 15분 쿨다운 비상 `paste-buffer` 주입을 수행한다.
   - **세션 회전 및 인계 (`bin/session-init.sh`, `bin/chain-recover.sh`, `bin/task-abandon.sh`)**: 감독자 컨텍스트 포화 시 `session-init.sh --apply --retire-old-supervisor`로 기존 세션을 `retired` 마킹한 뒤 새 세션을 띄우고 `docs/RESUME_HANDOFF.md`를 통해 맥락을 인계한다.
2. **현재 지속성의 빈틈 (Gaps)**
   - **빈틈 1 — 1회성 `Task` 위에 다회성 `Ticket` 묶음이 없음**: `dispatch.sh`는 매번 새로운 `task_id`(`<worker>-<epoch_ns>`)를 생성한다. 초기 구현 → 코드 리뷰 → 수정 재디스패치로 이어지는 다회성 작업 흐름을 하나의 상위 작업 단위(Ticket)로 묶어 추적하거나, 중단된 작업 재개 시 이전 시도 이력과 `RECOVERY_NOTE`를 자동 주입하는 표준 규격이 없다.
   - **빈틈 2 — 단일 `PROJECT_DIR` 공유로 인한 병렬 작업 충돌 위험**: `agy`와 `codex`가 동시에 같은 `PROJECT_DIR`에서 실행될 경우 `git index.lock`이나 워킹 트리 파일이 서로 덮어써질 수 있다(현재는 감독자가 파일 범위를 수동 분리하거나 순차 디스패치하여 회피 중).
   - **빈틈 3 — 수동 인계 문서(`RESUME_HANDOFF.md`) 의존**: 인계 파일 갱신이 에이전트의 규율에 의존하므로, 예기치 않은 크래시 직후에는 `tasks/*/state`와 `git status`를 사람이 다시 조합해야 할 수 있다.
   - **빈틈 4 — `tmux` 강결합**: 세션·창 제어 로직이 `lib/term.sh` 같은 단일 인터페이스로 모여 있지 않고 6개 스크립트에 분산되어 있다.

---

## 5. 능동성 분석 (DeepSeek `dsh-superagent` vs 우리 체인)

### 5.1 DeepSeek `dsh-superagent`의 능동성 메커니즘 (실제 확인한 코드 근거)
1. **에이전트를 움직이는 동력 (`src/sources/github.ts`, `src/app/passes.ts`, `src/core/intake.ts`)**
   - **다중 폴링 소스**: GitHub 알림(`notifications`), 열린 티켓의 PR 상태 및 CI 지문(`check-runs` + `status` 결합 해시), 활성 티켓 및 종료 후 2주 이내 티켓의 구독 스레드 코멘트, 그리고 선택적인 레포지토리 전체 관찰 스트림(`observeIssues`, `observePulls`, `observeComments`, `observeCi` — ETag/304 조건부 요청 지원)과 Feishu 웹소켓/폴링을 통해 외부 세계의 변화를 `Trigger`로 수집한다.
   - **2초 주기 `Passes` 루프 (`src/app/passes.ts`)**: `setInterval(..., 2000)`과 즉시 실행(`receive`)이 결합된 직렬화 루프(`pumping`/`pumpAgain` 코얼레싱)가 `Effects` 큐를 밀어내고 `Engine.evaluateDue()`를 호출하여 시간 기반 상태 전이를 구동한다.
   - **구독 스레드 직통 팬아웃 (`src/core/intake.ts`)**: 이미 티켓이 소유(`own`)하고 있는 스레드에 올라온 **사람의 후속 메시지**는 무거운 `Eval`을 거치지 않고 곧바로 해당 티켓의 `Input`으로 직행한다(범위 밖 요청이면 워커가 `orchestrator_trigger_handback`으로 본체에 돌려보냄).
2. **Rules-then-LLM 2단계 필터와 `observe` 모드 (`src/policy.ts`, `src/app/assessment.ts`, `prompts/admission.md`)**
   - **1단계 — 결정론적 게이트 (`policy.gate`, 토큰 비용 0)**:
     - 에이전트 자신이 작성한 메시지(`viewerLogin`)는 소스 변환 단계에서 즉시 폐기한다.
     - `SourceScope`(`listened` vs `observed` vs `row`)를 확인하고, 봇/자동화 계정은 `listedBot`에 등록된 경우에만 최대 `inferred`(추론) 강도로 통과시킨다.
     - `allowedSender`(허용된 인간 목록)와 `addressed`(직접 멘션/호출 여부)를 조합하여, 허용된 사람의 직접 호출은 `explicit`(명시적 위임)으로, 그 외 3칸 Admission Table(`listedUnaddressed`, `unlistedAddressed`, `unlistedUnaddressed`) 통과 건은 `inferred`(추론 후보)로 분류한다.
   - **2단계 — LLM 의미 평가 (`MasterAssessor` / `assessmentOutcome`)**:
     - `explicit` 후보는 규칙 전수 검사 없이 곧바로 `orchestrator_route`로 라우팅된다.
     - `inferred` 후보는 메인 세션이 후보 카드만 보고 명백한 노이즈를 즉시 무시(`ignoredFromCard`)하거나, `orchestrator_eval` → `orchestrator_eval_decide`를 호출해 평가한다.
     - **코드 수준의 엄격한 평가 검증 (`assessmentOutcome` in `src/app/assessment.ts`)**: LLM이 `accept`를 내려도 호스트 코드가 ① 활성화된 모든 규칙(`enabledRules`)에 대해 1:1 판정(`rule_assessments`)을 제출했는지, ② `directionAllowed`(`implementation_requested === true`, `target_basis`가 `'specified'` 또는 `'existing-standard'`, `requires_design_decision === false`)를 충족하는지, ③ 최소 1개 이상의 활성 규칙이 매칭되었고 원문 인용 증거(`intent_evidence`)가 있는지를 검증한다. 하나라도 어기면 `Fault`를 발생시켜 거부한다.
     - **`AcceptMode` (`off | observe | auto`, 기본값 `observe`)**: `observe` 모드에서는 위 모든 검증을 통과해 `accept` 판정이 나더라도 실제 티켓을 생성하거나 외부에 댓글을 달지 않고, `disposition`을 `ignore`(`reason: 'observed'`)로 기록하면서 감사 로그에 `audit.would_accept = true`만 남긴다. `auto` 모드일 때만 실제 티켓을 열며, 이때도 `mandateInstructions`를 통해 "단일 모듈 내 소규모 내부 변경"으로 작업 범위를 제한한다.
3. **발화와 침묵의 규율 및 권한 경계 (`docs/000-philosophy.md`, `prompts/replies.md`, `docs/002-authorization.md`)**
   - **3회 발화 원칙**: 에이전트는 티켓 수명주기 동안 ① 최초 착수 확인(`orchestrator_ack`, 트리거 ID 기준 영구 중복 제거), ② 막혔을 때 질문 1개 또는 실패 보고(`blocked`/`failed` + 구조화된 `human_action`), ③ 산출물 준비 완료(`ready`) 시점에만 말한다.
   - **철저한 침묵**: 중간 진행 보고(`progress`)는 내부 상태만 갱신하고 메인 세션조차 깨우지 않으며, 채팅 메시지는 100단어 상한(`checkedImText`)을 코드로 강제한다.
   - **권한 분리**: 전용 GitHub 계정, 환경변수 시크릿 스트리핑(`KEY/PASSWORD/SECRET/TOKEN` 제거), `<stateDir>/worker-bin/gh` 래퍼를 사용하며, 에이전트 계정으로는 절대 PR Auto-merge를 활성화하지 않는다.

### 5.2 우리 체인(`agent-command-chain-template`)의 능동성 현황과 강점·빈틈 (실제 확인한 코드 근거)
1. **현재 구조와 강점 (인간 주의력 보호 + 결정론적 소음 드레인)**
   - **이벤트 기반 비차단 기상 (`bin/sonnet-event-wait.sh`, `claude-bridge.settings.json`)**: 감독자(Sonnet)는 폴링 루프를 돌지 않고 유휴 상태로 대기하다가, `event.fifo` 신호와 함께 `asyncRewake: true`(`exit 2`)로 깨어난다.
   - **결정론적 소음 제거 (`drain_noise()` in `bin/sonnet-event-wait.sh`, `watchdog-v2.sh`)**:
     - `drain_noise()`는 현재 `status=running`인 작업이 하나도 없을 때(체인 유휴 상태), `pending/`에 쌓인 `started` 및 `canceled` 이벤트를 감독자에게 전달하지 않고 `archive/auto-*`로 조용히 이동시킨다.
     - `bridge_degraded` 이벤트 역시 활성 작업 유무와 무관하게 `drain_noise()`에서 즉시 자동 아카이브하여 감독자의 컨텍스트 창이 인프라 경고로 오염되는 것을 막는다(`watchdog-v2.sh`의 `check_bridge_health`도 미처리 `bridge_degraded`가 이미 있으면 중복 발행하지 않음).
     - 결과적으로 감독자는 **`done`, `error`, `stalled`, `process_exit`** 같은 실질적 종단/이상 이벤트나 인간의 직접 입력에만 깨어난다.
   - **엄격한 거버넌스 (`ROLES.md`, `.llmwiki/session-resume.md`)**: "논의 ≠ 승인" 원칙을 통해 인간이 질문하거나 옵션을 검토하는 단계에서 에이전트가 임의로 워커를 디스패치하거나 코드를 수정하는 폭주를 방지한다.
2. **현재 능동성의 빈틈 (Gaps)**
   - **빈틈 1 — 외부/주기적 이벤트 소스(Source) 및 `observe` 모드 부재**: 현재 체인은 로컬 워커가 만든 Outbox 이벤트나 사람의 직접 입력에만 반응한다. CI 실패, PR 리뷰 코멘트, 주기적 저장소 위생 점검 등을 수집하는 `Source` 어댑터나, 이를 안전하게 시험할 `observe`(`would_accept` 드라이런 로깅) 규격이 없다.
   - **빈틈 2 — 일시적 장애(Quota/Rate-limit)와 설계 블로커의 미분류**: 워커가 `[[BLOCKED]]`나 `error`를 낼 때, 이것이 (a) 단순 API 쿼터/일시적 네트워크 오류(`429`/`5xx`)여서 다른 하네스(`codex` → `agy`)로 자동 폴백하거나 백오프 재시도할 사안인지, (b) 인간의 설계 결정이 필요한 사안인지 구조화된 사유 코드(`reason_code`)로 구분되어 있지 않아 매번 감독자 LLM이 로그를 읽고 판단해야 한다.
   - **빈틈 3 — 감독자 역할 경계의 프롬프트 의존**: DeepSeek는 라우터 세션에 `noWait` 가드와 제한된 툴셋을 코드로 강제하지만, 우리 체인의 감독자 `No-Bash/No-Edit`(직접 구현 금지) 규칙은 `CLAUDE.md`/`ROLES.md` 프롬프트 규율에 의존한다.

---

## 6. 도입 후보와 비채택 항목

우리 체인의 제1원칙인 **"단순성 유지(복잡한 필수 절차나 승인 단계 추가 금지, 기존 메커니즘의 계약 승격 우선)"**에 따라, DeepSeek `dsh-superagent` 및 최근 스파이크 결과에서 가져올 것과 버릴 것을 명확히 구분한다.

### 6.1 도입 후보 (구조/계약 승격 및 가벼운 구현 개선)

| 순위 | 도입 항목 | 구분 | 설계 요지 및 기존 체인과의 결합 방식 |
| :---: | :--- | :---: | :--- |
| **1** | **종단 이벤트/센티널 사유 코드(`reason_code`) 세분화** | **구조 계약 승격** | 기존 Outbox 이벤트(`done`/`error`/`stalled`)와 `[[BLOCKED]]` 센티널에 선택적 `reason_code` (`transient_quota` \| `transient_network` \| `needs_human_decision` \| `verify_failed` \| `host_restarted`) 필드를 추가한다. 감독자나 워치독이 `transient_quota`를 보면 인간을 깨우지 않고 백오프 재시도 또는 대체 워커(`codex` ↔ `agy`) 폴백을 제안/수행할 수 있다. |
| **2** | **중단·재시작 작업에 대한 자동 `RECOVERY_NOTE` 계약** | **구조 계약 승격** | 워치독(`watchdog-v2.sh`)이나 `chain-recover.sh`가 중단된 작업을 재개하거나 후속 태스크를 디스패치할 때, 프롬프트 상단에 표준 복구 주의사항(*"이전 시도가 중단되었으니 외부 명령이나 커밋을 반복하기 전에 `git status`, `git log -n 5`, 기존 산출물을 먼저 확인하라"*)과 직전 `task_id` 포인터를 자동으로 주입한다. |
| **3** | **병렬 쓰기 작업용 옵션형 `git worktree` 격리 (`--isolate`)** | **구현 계층 확장** | 기본값은 현재처럼 단순한 단일 `PROJECT_DIR`을 유지하되, `agy`와 `codex`에 동시에 파일 수정 작업을 병렬 디스패치할 때만 `dispatch.sh --worktree` (또는 작업별 격리 디렉터리)를 생성해 인덱스 충돌을 원천 배제한다. |
| **4** | **터미널 멀티플렉서 얇은 래퍼 (`lib/term.sh`) 분리** | **구현 계층 모듈화** | 6개 스크립트에 흩어진 `tmux` 직접 호출을 `lib/term.sh`의 7개 함수로 모은다. 기본 백엔드는 검증된 `tmux`로 유지하되, 환경변수(`ACC_TERM_BACKEND=tmux|herdr`)만으로 멀티플렉서를 교체할 수 있게 한다. |
| **5** | **`bin/resolve-model.sh`의 다중 하네스 동적 해석 확장** | **구현 계층 확장** | 현재 `codex debug models`만 동적 조회하는 `bin/resolve-model.sh`를 확장하여, `agy`에 대해서도 `agy models` 출력(및 캐시)을 파싱해 티어(`default`/`deep`/`fast`)별 최신 모델을 자동 선택하도록 통일한다. |
| **6** | **능동적 트리거 도입 시 `Rules-then-LLM` + `observe` 모드 계약화** | **구조 계약 승격** | 향후 GitHub/CI 폴러나 주기적 점검 크론을 붙일 때, 기존 `sonnet-event-wait.sh`의 `drain_noise()`를 일반화하여 **결정론적 게이트 스크립트**를 앞에 두고, 신규 규칙은 반드시 `observe` 모드(실행 없이 `events/archive/observed-*.json`에 `would_accept` 로그만 기록)로 먼저 검증한다는 원칙을 고정한다. |

### 6.2 비채택 항목 (우리 체인과 맞지 않아 가져오지 않을 것)

| 비채택 항목 | 출처 | 비채택 사유 (단순성 및 아키텍처 적합성 기준) |
| :--- | :--- | :--- |
| **1. 파일 기반 Outbox/State를 SQLite + 상주 Node.js/Cordis 데몬으로 전면 교체** | DeepSeek `store.ts`, `runtime.ts` | 우리 체인은 외부 런타임 의존성 없이 순수 Bash + POSIX 파일시스템 원자적 `mv` + `flock`만으로 이미 완벽한 멱등성과 크래시 복구력을 갖추고 있다. 상주 DB 데몬을 도입하면 데몬 자체의 죽음을 감시해야 하는 복잡도가 추가되고 `ls`/`cat`/`jq`로 즉시 디버깅하던 투명성을 잃는다. |
| **2. 다중 테넌트용 3칸 Admission Table 및 교차 스레드 위임장(`Mandate`) 해시 재검증** | DeepSeek `policy.ts`, `002-authorization.md` | DeepSeek는 공개/사내 GitHub 레포지토리에서 불특정 다수(허용된 동료, 외부 기여자, 봇)의 댓글을 동시에 받는 상주 봇이므로 복잡한 발신자×멘션 매트릭스가 필수적이다. 반면 우리 체인은 **단일 운영자(Single Operator)의 명시적 지휘 체인**이므로, 이를 그대로 들여오면 불필요한 설정·승인 절차만 늘어난다. |
| **3. `auto` 모드 기반 무승인 자율 티켓 생성 및 원격 반영** | DeepSeek `AcceptMode: auto` | 우리 체인의 핵심 안전장치인 **"논의 ≠ 승인 (`ROLES.md`)"** 원칙에 위배된다. 사람의 명시적 승인 없는 자율 작업 착수는 채택하지 않으며, 능동적 감지가 필요하더라도 감독자에게 판단 후보를 보고하거나 `observe` 로그를 남기는 수준으로 제한한다. |
| **4. React + Vite 기반 별도 웹 패널(`web/`) 도입** | DeepSeek `005-panel.md`, `web/` | 우리 체인에는 이미 외부 패키지 의존성이 전혀 없는 Python 3 표준 라이브러리 기반 읽기 전용 대시보드(`dashboard/server.py`, 민감정보 마스킹 내장)가 존재한다. 별도의 프론트엔드 빌드 파이프라인을 추가할 이유가 없다. |
| **5. Herdr의 자체 상태 머신(`done`)이나 비영속 소켓(`events --Wait`)을 완료 SSOT로 사용** | Herdr Spike (`2026-10-31-herdr-spike-results.md`) | 스파이크 실험에서 확인되었듯 `herdr`의 `done` 상태는 셸 프롬프트 복귀를 감지하는 휴리스틱에 불과하고, 소켓 이벤트는 재연결 시 유실된다. 완료 판정의 단일 진실 공급원(SSOT)은 반드시 파일 기반 Outbox 계약으로 유지해야 한다. |

---

## 7. 실제로 열어 본 출처 (URL·파일 경로, 날짜)

> **정직성 원칙 확인**: 아래 목록은 **2026-11-02** 본 조사 과정에서 실제로 클론·열람·실행하여 직접 확인한 원천 파일 및 명령어만 기재하였다. 직접 열지 않은 파일이나 확인하지 못한 사항은 본문에서 추측으로 서술하지 않았다.

### 7.1 외부 원격 저장소 및 로컬 클론 (`/home/rerun/.local/opt/dsh-superagent-prototype`)
- **원격 URL**: `https://github.com/deepseek-ai/dsh-superagent-prototype` (MIT License, 2026-10-31 공개, 확인 커밋: `6337fc823ce20251c8de8085948165c81aaa701e`, 확인일: 2026-11-02)
- **실제로 열어 본 문서 (`docs/` 및 루트)**:
  - `/home/rerun/.local/opt/dsh-superagent-prototype/README.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/SETUP.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/docs/000-philosophy.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/docs/001-domain.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/docs/002-authorization.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/docs/003-execution.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/docs/004-prompts.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/docs/005-panel.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/docs/006-deployment.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/docs/007-architecture.md` (전체 617줄 열람, 열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/docs/008-cookbook.md` (열람일: 2026-11-02)
- **실제로 열어 본 핵심 소스 (`src/` 및 `prompts/`)**:
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/core/ports.ts` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/core/model.ts` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/core/intake.ts` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/core/tickets.ts` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/core/assignments.ts` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/app/passes.ts` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/app/effects.ts` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/app/outbox.ts` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/app/store.ts` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/app/assessment.ts` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/policy.ts` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/executor/dsh.ts` (전체 673줄 열람, 열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/src/sources/github.ts` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/prompts/admission.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/prompts/orchestrator.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/prompts/worker.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/prompts/replies.md` (열람일: 2026-11-02)
  - `/home/rerun/.local/opt/dsh-superagent-prototype/prompts/subscriptions.md` (열람일: 2026-11-02)

### 7.2 우리 체인 저장소 (`/home/rerun/agent-command-chain-template`)
- **문서 및 설정**:
  - `/home/rerun/agent-command-chain-template/CLAUDE.md` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/README.md` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/ROLES.md` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/config.env` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/config.env.example` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/claude-bridge.settings.json` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/.llmwiki/architecture.md` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/.llmwiki/session-resume.md` (열람일: 2026-11-02)
- **스크립트, 라이브러리, 어댑터, 대시보드**:
  - `/home/rerun/agent-command-chain-template/bootstrap-v2.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/watchdog-v2.sh` (전체 894줄 열람, 열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/lib/config.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/lib/session.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/bin/chain` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/bin/dispatch.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/bin/run-task.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/bin/event-emit.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/bin/sonnet-event-wait.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/bin/event-ack.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/bin/agy-stop-hook.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/bin/resolve-model.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/bin/session-init.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/bin/chain-recover.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/bin/task-abandon.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/adapters/agy-oneshot.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/adapters/codex-oneshot.sh` (열람일: 2026-11-02)
  - `/home/rerun/agent-command-chain-template/dashboard/server.py` (1~60줄 열람, 열람일: 2026-11-02)
- **실제로 실행하여 확인한 CLI 명령**:
  - `agy --help` 및 `agy models` (실행일: 2026-11-02)

### 7.3 LLM Wiki 선행 연구 문서 (`/mnt/c/Users/rerun/llm-wiki/Research`)
- `/mnt/c/Users/rerun/llm-wiki/Research/2026-11-02-deepseek-superagent.md` (열람일: 2026-11-02)
- `/mnt/c/Users/rerun/llm-wiki/Research/2026-10-31-herdr-agentchain-sol61.md` (열람일: 2026-11-02)
- `/mnt/c/Users/rerun/llm-wiki/Research/2026-10-31-herdr-agentchain-opus55.md` (열람일: 2026-11-02)
- `/mnt/c/Users/rerun/llm-wiki/Research/2026-10-31-herdr-spike-results.md` (열람일: 2026-11-02)
- `/mnt/c/Users/rerun/llm-wiki/Research/INDEX.md` (열람일: 2026-11-02)
