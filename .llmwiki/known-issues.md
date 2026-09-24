---
title: Known Issues & Limitations
tags: [known-issues, limitations, remote-control, fallback, wsl, session-resolution, dispatch-targeting, non-preemptive-tui, cli-behavior, prompt-drift]
related: ["[[INDEX]]", "[[architecture]]", "[[usage]]"]
summary: Remote Control(RC) 전제조건, agy-Codex 자동 연동 부재, v1/v2 세션 오조준(interactive TUI) 및 4계층 자동 해석, 상주 대화형 TUI 비선점형 입력 큐잉 및 지시 지연 병목(9/23 사례).
---

# Known Issues & Limitations

`agent-command-chain-template`의 현재 버전(MVP)에서 확인된 알려진 제약 사항과 주의점이다.

---

## 1. Claude Code Remote Control(RC) 전제조건

`bootstrap.sh`에서 Sonnet을 `claude --model sonnet --remote-control`로 기동할 때 다음 조건이 충족되어야 정상 작동한다:

- **구독 등급**: claude.ai Pro, Max, Team, Enterprise 유료 플랜 구독 계정이 필요하다.
- **사전 OAuth 로그인 필수**: 사전에 `claude /login`을 통해 OAuth 인증이 완료되어 있어야 한다. `ANTHROPIC_API_KEY` 환경변수를 통한 API 키 단독 인증 상태에서는 RC 기능이 켜지지 않는다.
- **조직 설정 (Team/Enterprise)**: 조직 계정의 경우 관리자가 `claude.ai/admin-settings/claude-code`에서 Remote Control 기능을 활성화해 두어야 한다.
- **상태 점검**: 세션 진입 후 프롬프트에 `/remote-control`을 입력하면 웹 접속 URL 및 현재 연결 상태 패널을 확인할 수 있다.

---

## 2. agy → Codex 자동 연동 부재

이 템플릿은 외부 라우터 엔진을 제거한 MVP 구조다:

- `eraweb-fork`의 경우 `tools/router/src/adapters/codexAdapter.ts` 등에서 Node 서브프로세스를 직접 spawn하여 agy가 Codex를 호출했다.
- 본 템플릿에는 해당 어댑터 레이어가 없으므로, **agy가 스스로 Codex 창을 조작하거나 자동 호출하지 못한다.**
- **해결 패턴**: agy 결과 보고서에서 `"Terra급 작업 필요: ..."` 패턴을 Sonnet(또는 사람)이 `capture-pane` 또는 파이프 로그(`logs/agy.pane.log`)로 감지하고, Codex 창에 `send-keys`로 직접 스펙을 주입하는 방식으로 협업한다.

---

## 3. 느슨한 워치독 (엄격한 폴백/상태머신 미구현)

- `watchdog.sh`는 프로세스가 죽었는지(`MISSING` 또는 `pane_current_command`가 셸로 복귀)만 확인하여 단순 재시작(`restart_if_dead`)을 수행한다.
- SQLite 기반 상태머신, 작업 임차(lease), 정교한 헬스체크 하트비트, 스티키 라우팅, 모델 간 핑퐁 방지 로직은 포함되어 있지 않다.
- 장애 복구가 복잡한 엔터프라이즈 환경이 필요하다면 `eraweb-fork/docs/workflows/multi_model_router.md` §5를 참고하여 하네스를 보강해야 한다.

---

## 4. Claude Code 세션 지속성 경고 (v2.1.274 실측)

- `bootstrap.sh`를 Claude Code의 Bash 툴 내부에서 실행할 경우, 새로 뜨는 Sonnet 프로세스가 부모 프로세스 트리를 확인하여 자신을 자식 세션으로 인식하고 transcript 저장을 비활성화한다.
- 이를 방지하기 위해 `export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`을 설정하였으나, 실측(v2.1.274) 기준 해당 환경변수를 주입해도 경고가 여전히 발생한다(제품 자체 버그로 보고됨).
- 대화 및 Remote Control 원격 조작 자체는 정상 동작하지만, 해당 세션은 종료 후 `--resume`으로 복원할 수 없다.

---

## 5. 대화형 디렉토리 신뢰(Folder Trust) 확인 우회

- 처음 방문하는 프로젝트 디렉토리에서 `claude`와 `codex`를 실행하면 "이 폴더의 내용을 신뢰하는가?" 대화형 프롬프트가 뜬다.
- `--dangerously-skip-permissions` 플래그는 비대화형(`-p`) 모드에서만 이 확인을 건너뛰며, 대화형 모드에서는 건너뛰지 못한다.
- 이에 따라 `bootstrap.sh`는 `wait_for_pane_text` 함수로 터미널 화면 출력을 최대 15초간 폴링하여 프롬프트가 확인되면 자동으로 확인 키(Down + Enter 또는 Enter)를 주입한다 (`AUTO_CONFIRM_TRUST=true`).
- 신뢰하지 않는 경로에서 자동 승인을 원치 않는다면 `config.env`에서 `AUTO_CONFIRM_TRUST=false`로 변경해야 한다.

---

## 6. agy CLI 실행 환경 주의사항 (WSL)

- Linux 네이티브 바이너리(`~/.local/bin/agy`)가 설치되어 있는 경우 `bootstrap.sh`가 PATH를 자동 등록하여 문제없이 동작한다.
- Windows 전용 빌드(`agy.exe`)만 존재하는 환경이라면 WSL 터미널에서 `cmd.exe` interop을 통해 호출해야 한다.
- 이때 `cmd.exe`는 WSL UNC 경로(`\\wsl.localhost\...`)로 직접 `cd`하지 못하므로, `config.env`의 `AGY_CMD`에 실제 Windows 드라이브 경로(`C:\...`)를 지정해야 한다:
  ```bash
  AGY_CMD='cmd.exe /c "cd /d C:\path\to\project && agy.exe --new-project --mode plan"'
  ```

---

## 7. v1/v2 세션 오조준 및 세션명 미지정 거부 (interactive TUI instead of idle shell)

- **메타데이터**:
  - `root-cause`: `template-bug`/`env`
  - `verified_with`: 2026-09-24
- **증상**:
  `bin/dispatch.sh` 실행 시 다음 에러와 함께 exit code 71로 즉시 거부됨:
  ```text
  Error: Target window 'agy' in session 'agentchain' is running interactive TUI 'agy' instead of idle shell. Dispatch rejected.
  Hint: If this is a v1 session, re-run with SESSION_NAME=<v2-session> (e.g. SESSION_NAME=agentchain-v2) to target the v2 idle worker shell.
  ```
- **근본 원인 (Root Cause)**:
  `config.env`의 기본값 `SESSION_NAME=agentchain`은 v1 대화형 TUI 상주 세션의 기본 이름이다. v2 세션을 `agentchain-v2`로 기동한 상태에서 상위 감독자(Sonnet)나 사용자가 `SESSION_NAME` 없이 `dispatch.sh`를 호출하면, 환경변수 부재 시 기본값 `agentchain`을 바라보게 된다. 그 결과 v1 세션의 `agy` 창(대화형 TUI)을 대상으로 디스패치를 시도하고, v2의 프로세스 오염 방지 fail-closed 방어 기제에 의해 즉시 거부된다. 즉 아키텍처적 구조 충돌이 아니라 **세션명 해석 실패(Session Name Resolution Failure)**가 실제 원인이다.
- **해결책 및 권장 사항**:
  1. **명시적 세션명 전달 (강력 권장)**:
     브리지를 작동시키거나 디스패치를 호출할 때 항상 명시적으로 `SESSION_NAME`을 지정한다:
     ```bash
     SESSION_NAME=agentchain-v2 /path/to/bin/dispatch.sh agy --prompt-file /path/to/prompt.md
     ```
  2. **세션명 4계층 자동 해석 우선순위**:
     명시적 환경변수가 주어지지 않은 경우 `dispatch.sh` 및 관련 도구(`event-ack.sh`, `watchdog-v2.sh`)는 다음 우선순위를 따른다:
     - ① 환경변수 `SESSION_NAME`
     - ② 런타임 마커 (`$ACC_RUNTIME/session_name` 파일; `bootstrap-v2.sh` 기동 시 자동 기록)
     - ③ 현재 tmux 세션 (`tmux display -p '#S'`; 단 해당 세션에 대상 윈도우가 있고 v2 마커가 확인될 때)
     - ④ `config.env` 기본값
  3. **후보 v2 세션 검증 가드 (Candidate Guard)**:
     해석된 대상 세션에 v2 마커(`bootstrap_version=2`)가 없는데 다른 활성 tmux 세션에 v2 마커가 존재할 경우, 조용히 v1으로 진행하지 않고 exit code 70과 함께 감지된 v2 세션명 목록과 "SESSION_NAME=<이름> 으로 재실행" 안내를 출력하며 fail-close 거부한다.

---

## 8. 상주 대화형 TUI의 비선점형 입력 큐잉과 위임 부재로 인한 지시 지연 (9/23 병목 사례)

- **메타데이터**:
  - `root-cause`: `prompt-drift` / `cli-behavior`
  - `verified_with`: 2026-09-24
- **증상**:
  - 2026-09-23 저녁, agy 메인 세션이 도구 루프에 몰입했을 때 상위 감독자(Sonnet)가 `send-keys`로 주입한 새로운 지시나 방향 수정이 즉각 처리되지 않고 수 분간 지연되는 반응성 마비(병목)가 발생함.
  - tmux pane 로그 실측(`agy.pane.log` L322735~322739):
    주입된 지시문이 Antigravity TUI의 `queued messages` 큐에 적재(enqueue)되어 묶여 있다가, agy 메인의 활성 턴(다수의 직접 Read/Bash/Edit 도구 호출; 과거 539회 언급은 unverified/횟수 미재현)이 완전히 끝나 프롬프트(`>`)로 복귀한 뒤에야 디큐되어 실행됨.
- **근본 원인 (Root Cause)**:
  1. **초기 오리엔테이션 시 위임 규칙 누락 (`prompt-drift`)**:
     23일 19:20 기동 시점의 사용자 프롬프트와 당시 `ROLES.md`에는 서브에이전트 위임 규칙이 없었고 "직접 읽고 복사해서 작성하라"는 지시를 받아 agy 메인이 다수의 직접 도구 호출(과거 539회 언급은 unverified/횟수 미재현) 루프에 진입함 (규칙 위반이 아니라 지침 부재였음).
  2. **Antigravity TUI의 비선점형 인터페이스 특성 (`cli-behavior`)**:
     Antigravity CLI의 대화형 TUI는 모델 턴(추론, 도구 호출, 출력 스트리밍)이 활성화되어 있을 때 외부 입력을 즉각 가로채거나 선점(preemption)하지 못하고 큐에 적재함. 따라서 상주 TUI 구조에서는 메인이 직접 무거운 작업을 수행하면 외부 명령계통의 응답성이 완전히 차단됨.
- **미검증 사항 (`unverified`)**:
  - 대화형 TUI 상주 세션에서 메인이 `invoke_subagent`를 호출하여 위임 중일 때(`⣻ Delegating...` 상태), Antigravity 런타임이 외부 입력을 즉시 수신할 수 있는지, 아니면 서브에이전트 완료 보고까지 메인 턴을 동기적으로 점유하여 외부 인터럽트를 큐에 가두는지 여부는 추가 격리 실험이 필요함 (`unverified`).
- **해결책 및 아키텍처 전환**:
  1. **v2 Full-Push oneshot 격리 구조로 전면 전환**:
     상주 대화형 TUI 구조를 공식 폐기(legacy)하고, `WORKER_MODE=oneshot`의 단발 실행기(`adapters/agy-oneshot.sh`)로 전환. 워커 창은 유휴 셸(`bash`)로 대기하며, 매 디스패치마다 독립 프로세스로 기동되어 `invoke_subagent`로 서브에이전트를 생성/취합하고 정상 종료함.
  2. **직접 작업 엄격 금지 규칙 확립**:
     경량 작업이라도 agy 메인의 직접 Read/Edit/Bash를 금지하고 제어면(라우팅/취합/보고)만 전담하도록 지침 정합화. 소환 실패 시 `[[BLOCKED]]`로 상위에 즉시 에스컬레이션.
