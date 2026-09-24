# Agent Command Chain — Template (MVP)

간소화된 4계층 멀티 에이전트 명령계통의 템플릿. `eraweb-fork`의 실전 구조
(Sol/Terra/agy 3역할 + Sonnet/Terra/Sol-Opus 3티어 라우터, `docs/workflows/`)를
관찰한 뒤, 더 가볍게 재구성한 것이다.

> [!IMPORTANT]
> **실행 경로 안내 (v2 기본 권장 / v1 폴백)**
> - **기본 권장 경로**: **v2 Full-Push 이벤트 통지 브리지** (`bootstrap-v2.sh`, `watchdog-v2.sh`, `bin/*`, `adapters/*`). FIFO 기반 즉각 깨우기(`asyncRewake`)와 4대 안전망 워치독을 통해 지연 없는 이벤트 통지와 신뢰성을 보장합니다.
> - **폴백/레거시 경로**: **v1 기본 브리지** (`bootstrap.sh`, `watchdog.sh`). 순수 tmux 화면 캡처(pull) 방식으로 동작하며, v2 환경 구성이 어렵거나 최소 세션 테스트 시의 폴백으로 완전 보존됩니다. (v1 파일은 절대 삭제되지 않음)

## 1. 역할 구조 (4계층)

| 계층 | 주체 | 역할 | 비고 |
|---|---|---|---|
| **0. 사용자** | 사람 | 최종 의사결정, 승인 | RC로 Sonnet 세션에 원격 개입 |
| **1. 감독/디스패치** | Claude Sonnet | 모니터링·감사, 작업 디스패치, 루프 관리, RC(원격제어) | 사람과의 상시 소통 창구. **사용자가 직접 요청하지 않는 한 실질 작업(문서/코드 작성/조사)은 스스로 하지 않고 agy/codex에 위임** |
| **2. 라우터 겸 워커 총괄** | agy (Gemini 3.8 Flash) | 난이도 판단(hard routing) 및 서브에이전트 위임·총괄 | **메인 세션 직접 작업(Read/Bash/Edit) 금지 및 서브에이전트(--agent) 위임.** 상위(Sonnet) 인터럽트 수신 대기 상태(responsive state) 유지. 소환 불가/실패 시 직접 처리 금지 및 Sonnet에 명시 보고 |
| **2-내부 Tier 2** | Codex Terra | 중난이도 작업의 설계 및 직접 수행 | 별도 tmux 창(§2). agy가 자동 호출하진 않음 — 아래 "한계" 참고 |
| **2-내부 Tier 3** | Sol / Opus | 최고난도 문제, 전체 플래닝 상담 | 상시 창 없음. 필요할 때 codex 창의 커맨드를 `--model gpt-5.6-sol`로 바꿔 send-keys, 또는 claude 쪽은 `--model opus`로 즉석 실행 |

핵심 원칙:
1. **계층 1 Sonnet(감독)은 사람과의 상시 소통 창구이자 agy/codex의 모니터링·감사가 주 역할이다.** 사용자가 Sonnet에게 직접 수행하라고 명시적으로 요구하지 않는 한, 문서 작성/코드 작성/조사 같은 실질적인 작업은 스스로 직접 처리하지 않고 agy 또는 codex(Terra/Sol)에 위임해야 한다.
2. **계층 2 agy(메인 세션) 역시 자신이 직접 Read/Bash/Edit 등의 툴을 호출해 파일 읽기/쓰기/수정 작업에 몰입하지 말고, 서브에이전트(`--agent`)를 소환해 워커로 위임해야 한다.** agy 메인 세션의 존재 이유는 상위 감독자(Sonnet)의 추가 지시나 인터럽트를 언제든 즉시 수신할 수 있는 대기 상태(responsive state)를 유지하고, 지시/취합/보고를 총괄하는 데 있다. 서브에이전트 소환이 불가능하거나 실패하면 조용히 메인 세션이 직접 처리하지 말고 그 사실을 상위(Sonnet)에 명시 보고해야 한다.

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

### 2.2 tmux 브리지 (v1, 폴백/레거시)
세 프로세스를 같은 WSL Ubuntu tmux 세션의 서로 다른 윈도우에 띄우고 `send-keys`/`capture-pane`으로 조작하는 기존 순수 pull 방식입니다. v2 환경 구성이 어렵거나 최소 환경에서의 폴백/레거시 경로로 완전 보존됩니다.

## 3. 파일 구성

| 구분 | 파일 / 디렉토리 | 역할 |
|---|---|---|
| **v2 (기본 권장)** | `bootstrap-v2.sh` | v2 원클릭: 런타임 디렉토리·FIFO 초기화 + tmux 세션 기동 + 워치독 v2 자동 백그라운드 구동 |
| | `watchdog-v2.sh` | 4대 비정상 상태(세션/Sonnet, PID증발, Stall/Deadline, 브리지 정체) 전담 안전망 |
| | `bin/event-emit.sh` | 원자적 이벤트 스풀링(Durable Outbox) 및 FIFO 펄스 전송 |
| | `bin/sonnet-event-wait.sh` | Claude Code asyncRewake 전용 FIFO 이벤트 대기 어댑터 (exit 2로 기상) |
| | `bin/event-ack.sh` | 처리 완료된 이벤트의 archive 이동 및 FIFO 드레인 |
| | `bin/dispatch.sh` | 파일 기반 작업 명세(`runtime/tasks/<id>/spec.json`) 생성 및 워커 창 1줄 실행 커맨드 주입 |
| | `bin/run-task.sh` | 커널 자동 해제형 terminal flock 기반 워커 프로세스 실행 및 상태/이벤트 발행 |
| | `adapters/agy-oneshot.sh` | agy 단발 실행기 어댑터 |
| | `adapters/codex-oneshot.sh` | Codex 단발 실행기 어댑터 |
| | `claude-bridge.settings.json` | Claude Code asyncRewake 훅 및 명령어 권한 설정 |
| **v1 (폴백/레거시)** | `bootstrap.sh` | v1 tmux 세션 생성 → sonnet/agy/codex 윈도우 기동 (순수 pull 기반) |
| | `watchdog.sh` | v1 세션/프로세스 생존 감시 및 단순 재기동 (순수 pull 기반) |
| **공통** | `config.env.example` | 세션명, 타임아웃, 브리지 모드 등 전체 환경설정 템플릿 |
| | `ROLES.md` | 역할별 프롬프트 템플릿 및 운용 가이드 |
| | `.llmwiki/` | 아키텍처 및 상세 사용법 위키 문서 |

## 4. 빠른 시작

**이 템플릿은 프로젝트 종속적이지 않다.** 실행한 디렉토리가 곧 대상 프로젝트가 됩니다.

```bash
git clone https://github.com/loverspath/agent-command-chain-template.git ~/agent-command-chain-template
cd ~/agent-command-chain-template && cp config.env.example config.env   # 필요시 값 수정

cd /path/to/your/actual/project      # 지금부터 이 디렉토리가 대상이 된다

# [기본 권장 경로: v2 Full-Push 이벤트 통지 브리지]
~/agent-command-chain-template/bootstrap-v2.sh    # 세션 기동 + 런타임/FIFO 준비 + watchdog-v2 자동 기동 (START_WATCHDOG=true 기본값)

# [폴백/레거시 경로: v1 순수 pull 브리지]
# ~/agent-command-chain-template/bootstrap.sh
# ~/agent-command-chain-template/watchdog.sh &

tmux attach -t agentchain                          # 직접 들어가서 보고 싶을 때
```

## 5. 알려진 한계 (초안 단계)

- **RC 전제조건**: `claude --remote-control`은 claude.ai **Pro/Max/Team/Enterprise
  구독 + `claude /login` OAuth 로그인**이 사전에 되어 있어야 작동한다.
  API 키 인증만으로는 안 됨. Team/Enterprise는 조직 Owner가
  claude.ai/admin-settings/claude-code에서 RC를 켜둬야 한다. 세션 안에서
  `/remote-control` 을 치면 상태 패널(URL, 연결 상태)을 볼 수 있다.
- **agy → codex 자동 호출은 없다** (위 §2 마지막 항목). Sonnet이 사람 대신
  그 연결을 수동으로 메꾸는 구조다.
- **agy는 네이티브 Linux 빌드가 있다** (`~/.local/bin` 등에 설치되는 형태 —
  2026-09 실측 확인). `~/.local/bin`이 PATH에 없어도 `bootstrap.sh`가 자체적
  으로 추가해서 확인하니 신경 안 써도 된다. 정말 Windows 전용 빌드만 있는
  환경이라면 `config.env.example`의 주석에 적힌 `cmd.exe` 우회를 대신 써라.
- 인증/쿼터 관리, 헬스 상태머신, 스티키 라우팅은 없음. 필요해지면
  `eraweb-fork/docs/workflows/multi_model_router.md`의 §5를 참고해 확장.
