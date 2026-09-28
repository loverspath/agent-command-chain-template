---
title: Known Issues & Limitations
tags: [known-issues, limitations, bug, product-bug, permissions, remote-control, fallback, wsl, tmux-nesting, session-resolution, dispatch-targeting, non-preemptive-tui, cli-behavior, prompt-drift, test-harness-isolation, troubleshooting, sonnet-overreach, unrequested-scope, self-reflection-not-automatic]
related: ["[[INDEX]]", "[[architecture]]", "[[usage]]"]
summary: 실측으로 확인된 RC 전제조건, Folder Trust 우회 한계, 세션 지속성 버그, agy/codex 바이너리 이슈, tmux 중첩, v1/v2 세션 오조준, TUI 비선점 큐잉, 입력 혼선, 테스트 하네스 격리 규약, Sonnet의 요청 외 범위 임의 확장 사고, Post-mortem 규약이 프로젝트 전환 시 자동 전파되지 않는 구조적 결함.
---

# Known Issues & Limitations

`agent-command-chain-template`을 여러 프로젝트(demo-project, eraweb-fork, rentplan_hubfork 등) 및 테스트 환경에서 실측 운용하며 확인된 알려진 문제와 제약 사항, 원인 및 대응 방안이다.

---

## 1. Claude Code Remote Control(RC) 전제조건

`bootstrap.sh` 또는 `bootstrap-v2.sh`에서 Sonnet을 `claude --model sonnet --remote-control`로 기동할 때 다음 조건이 충족되어야 정상 작동한다:

- **구독 등급**: claude.ai Pro, Max, Team, Enterprise 유료 플랜 구독 계정이 필요하다.
- **사전 OAuth 로그인 필수**: 사전에 `claude /login`을 통해 OAuth 인증이 완료되어 있어야 한다. `ANTHROPIC_API_KEY` 환경변수를 통한 API 키 단독 인증 상태에서는 RC 기능이 켜지지 않는다.
- **조직 설정 (Team/Enterprise)**: 조직 계정의 경우 관리자가 `claude.ai/admin-settings/claude-code`에서 Remote Control 기능을 활성화해 두어야 한다.
- **상태 점검**: 세션 진입 후 프롬프트에 `/remote-control`을 입력하면 웹 접속 URL 및 현재 연결 상태 패널을 확인할 수 있다.

---

## 2. agy → Codex 자동 연동 부재

이 템플릿은 외부 라우터 엔진을 제거한 MVP 경량 구조다:

- `eraweb-fork`의 경우 `tools/router/src/adapters/codexAdapter.ts` 등에서 Node 서브프로세스를 직접 spawn하여 agy가 Codex를 호출했다.
- 본 템플릿에는 해당 어댑터 레이어가 없으므로, **agy가 스스로 Codex 창을 조작하거나 자동 호출하지 못한다.**
- **해결 패턴**: agy 결과 보고서에서 `"Terra급 작업 필요: ..."` 패턴을 Sonnet(또는 사람)이 `capture-pane` 또는 파이프 로그(`logs/agy.pane.log`)로 감지하고, Codex 창에 `send-keys`로 직접 스펙을 주입하는 방식으로 협업한다.

---

## 3. 느슨한 워치독 (v1, 엄격한 폴백/상태머신 미구현)

- `watchdog.sh`는 프로세스가 죽었는지(`MISSING` 또는 `pane_current_command`가 셸로 복귀)만 확인하여 단순 재시작(`restart_if_dead`)을 수행한다.
- SQLite 기반 상태머신, 작업 임차(lease), 정교한 헬스체크 하트비트, 스티키 라우팅, 모델 간 핑퐁 방지 로직은 포함되어 있지 않다.
- 장애 복구가 복잡한 엔터프라이즈 환경이 필요하다면 `eraweb-fork/docs/workflows/multi_model_router.md` §5를 참고하여 하네스를 보강하거나, v2 Full-Push 4대 안전망(`watchdog-v2.sh`)을 사용해야 한다.

---

## 4. Claude Code 세션 지속성 경고 (`CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`, 제품 버그)

- **증상**: `bootstrap.sh`를 Claude Code의 Bash 툴 내부에서 실행할 경우 새로 뜨는 Sonnet 프로세스 화면에 항상 다음 경고가 발생한다:
  > ⚠ Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION marker · restart with CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 to keep future transcripts
- **원인**: 상위 Claude Code 세션의 Bash 도구를 통해 tmux를 조작하여 실행할 때, 프로세스 계보상 부모가 다른 Claude Code 세션인 것을 감지하여 "자식 세션"으로 판단하고 transcript 저장을 끈다.
- **실측 결과 (v2.1.274)**: 환경변수 `export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`을 셸에 사전에 export하고 확인한 뒤 실행해도 경고가 그대로 발생한다 (공식 해결법 자체가 먹히지 않는 Anthropic 제품 버그로 보고됨).
- **영향 및 대응**: 무해함. RC 및 대화 자체는 정상 작동하지만, 해당 세션은 추후 `claude --resume`으로 복원할 수 없다. 현재 우회법이 없으므로 무시한다.

---

## 5. 대화형 디렉토리 신뢰(Folder Trust) 확인 우회 (`--dangerously-skip-permissions` 한계)

- **증상**: claude와 codex 둘 다 처음 진입하는 새 디렉토리에서 "이 폴더를 신뢰하는가?" 대화형 확인 프롬프트를 띄운다.
- **원인**: `--dangerously-skip-permissions` 플래그는 비대화형(`-p`) print 모드에서만 이 확인을 건너뛰며, 지속형 대화형 tmux 창에서는 건너뛰지 못한다.
- **대응**: `bootstrap.sh` 및 `bootstrap-v2.sh`는 `wait_for_pane_text()` 함수로 화면 출력을 폴링하여 해당 프롬프트 텍스트가 실제로 렌더링된 것을 확인한 후 자동으로 확인 키(Down + Enter 또는 Enter)를 전송한다 (`AUTO_CONFIRM_TRUST=true`, 기본값).
- **주의 (타이밍 이슈)**: 폴링 대신 단순 `sleep 3` 같은 고정 지연을 사용할 경우, 시스템 부하 등으로 기동이 지연되면 다이얼로그 렌더링 전에 키가 전송되어 "No, exit"가 선택되고 Sonnet 창이 조용히 종료되는 참사가 발생할 수 있다. 반드시 실제 화면 텍스트를 감지한 후 키를 전송해야 한다.

---

## 6. agy CLI 실행 환경 주의사항 (Linux 네이티브 vs WSL)

- **과거 오판 착시**: 초기에 WSL 비대화형 셸(`bash -c`)에서 `command -v agy`가 실패하여 "agy는 Linux 빌드가 없다"고 오판했으나, 실제로는 `~/.local/bin/agy`에 네이티브 리눅스 빌드가 존재했다. 비대화형 셸이 `.bashrc`를 로드하지 않아 PATH에 `~/.local/bin`이 누락되었던 것이 원인이었다.
- **대응**: `bootstrap.sh` 및 `bootstrap-v2.sh`가 자체적으로 `export PATH="$HOME/.local/bin:$PATH"`를 실행하여 정상 인식한다.
- **Windows 빌드 전용 환경 폴백**: 정말로 Windows 전용 빌드(`agy.exe`)만 있는 머신이라면, `cmd.exe`는 WSL UNC 경로(`\\wsl.localhost\...`)로 직접 `cd`하지 못하므로 `config.env`의 `AGY_CMD`에 실제 Windows 드라이브 경로(`C:\...`)를 지정하는 우회 커맨드를 사용해야 한다:
  ```bash
  AGY_CMD='cmd.exe /c "cd /d C:\path\to\project && agy.exe --new-project --mode plan"'
  ```

---

## 7. codex CLI 실행 모드 및 WSL 네이티브 바이너리 설치

- **대화형 TUI 모드 사용**: 초기에는 라우터 어댑터의 `codex exec --model ... -s danger-full-access`(one-shot) 형태를 사용했으나, 프롬프트가 주어지지 않으면 대화 지속 없이 즉시 종료되어 지속형 tmux 창에 적합하지 않았다. 따라서 `exec` 서브커맨드를 제외한 순수 대화형 TUI `codex`가 기본값으로 설정되었다.
  - 모델명 `gpt-5.6-terra`, `gpt-5.6-sol`은 계정 `~/.codex/config.toml` 기본값과 일치함이 확인됨.
- **WSL 환경 설치 주의**: Windows 전용 npm 글로벌 설치본의 shim을 WSL의 node가 참조할 경우 `linux-x64` 네이티브 옵셔널 의존성 누락으로 구동이 실패한다. 반드시 **WSL 터미널 안에서 직접** `npm install -g @openai/codex@latest`를 실행하여 리눅스 네이티브 바이너리를 설치해야 한다.

---

## 8. 템플릿 외부 브리핑 파일 열람 시 작업 디렉토리 이탈 프롬프트

- **증상**: Sonnet의 작업 디렉토리는 대상 프로젝트인데, 부트 브리핑 파일(`logs/session_brief.md`)은 템플릿 디렉토리에 존재하여 "Allow reads outside the working directories?" 확인 프롬프트가 발생한다.
- **위험**: 여기서 "계속 허용(Keep allowing)"을 선택하면 전역 사용자 설정(`permissions.blockReadsOutsideWorkingDirectories`)이 영구 변경되어 버린다.
- **대응**: `SONNET_CMD` 실행 시 `--add-dir "$HERE"`(템플릿 디렉토리) 옵션을 세션 범위로 자동 부가하여, 전역 설정을 건드리지 않고 브리핑 파일을 안전하게 읽을 수 있도록 허용한다.

---

## 9. tmux 중첩 세션 attach 및 Ctrl+b 조작 주의사항

- **중첩 세션 주의**: 이미 tmux 창(예: sonnet 창) 내부에서 `tmux attach -t agentchain`을 다시 실행하면 `sessions should be nested with care, unset $TMUX to force` 경고가 발생하며 중첩 상태가 된다. 이 상태에서는 단축키가 바깥과 안쪽 tmux 중 어디로 전달될지 모호해져 조작 불능처럼 느껴진다.
- **Ctrl+b 조작 요령**:
  - `Ctrl+b`를 누른 채 숫자 키를 함께 누르면 터미널에서 `Ctrl+숫자`로 인식되어 창 전환이 작동하지 않는다.
  - 반드시 **`Ctrl+b`를 눌렀다 손을 완전히 뗀 다음** 숫자 키(0, 1, 2)를 개별적으로 누르거나, `Ctrl+b`를 뗀 후 `w`를 눌러 창 목록 창에서 선택해야 한다.

---

## 10. v1/v2 세션 오조준 및 세션명 미지정 거부 (interactive TUI instead of idle shell)

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

## 11. 상주 대화형 TUI의 비선점형 입력 큐잉과 위임 부재로 인한 지시 지연 (9/23 병목 사례)

- **메타데이터**:
  - `root-cause`: `prompt-drift` / `cli-behavior`
  - `verified_with`: 2026-09-24
- **증상**:
  - 2026-09-23 저녁, agy 메인 세션이 도구 루프에 몰입했을 때 상위 감독자(Sonnet)가 `send-keys`로 주입한 새로운 지시나 방향 수정이 즉각 처리되지 않고 수 분간 지연되는 반응성 마비(병목)가 발생함.
  - tmux pane 로그 실측(`agy.pane.log` L322735~322739):
    주입된 지시문이 Antigravity TUI의 `queued messages` 큐에 적재(enqueue)되어 묶여 있다가, agy 메인의 활성 턴(다수의 직접 Read/Bash/Edit 도구 호출; 과거 539회 언급은 unverified/횟수 미재현)이 완전히 끝나 프롬프트(`>`)로 복귀한 뒤에야 디큐되어 실행됨.
- **근본 원인 (Root Cause)**:
  1. **초기 오리엔테이션 시 위임 규칙 누락 (`prompt-drift`)**:
     23일 19:20 기동 시점의 사용자 프롬프트와 당시 `ROLES.md`에는 서브에이전트 위임 규칙이 없었고 "직접 읽고 복사해서 작성하라"는 지시를 받아 agy 메인이 다수의 직접 도구 호출 루프에 진입함 (규칙 위반이 아니라 지침 부재였음).
  2. **Antigravity TUI의 비선점형 인터페이스 특성 (`cli-behavior`)**:
     Antigravity CLI의 대화형 TUI는 모델 턴(추론, 도구 호출, 출력 스트리밍)이 활성화되어 있을 때 외부 입력을 즉각 가로채거나 선점(preemption)하지 못하고 큐에 적재함. 따라서 상주 TUI 구조에서는 메인이 직접 무거운 작업을 수행하면 외부 명령계통의 응답성이 완전히 차단됨.
- **미검증 사항 (`unverified`)**:
  - 대화형 TUI 상주 세션에서 메인이 `invoke_subagent`를 호출하여 위임 중일 때(`⣻ Delegating...` 상태), Antigravity 런타임이 외부 입력을 즉시 수신할 수 있는지, 아니면 서브에이전트 완료 보고까지 메인 턴을 동기적으로 점유하여 외부 인터럽트를 큐에 가두는지 여부는 추가 격리 실험이 필요함 (`unverified`).
- **해결책 및 아키텍처 전환**:
  1. **v2 Full-Push oneshot 격리 구조로 전면 전환**:
     상주 대화형 TUI 구조를 공식 폐기(legacy)하고, `WORKER_MODE=oneshot`의 단발 실행기(`adapters/agy-oneshot.sh`)로 전환. 워커 창은 유휴 셸(`bash`)로 대기하며, 매 디스패치마다 독립 프로세스로 기동되어 `invoke_subagent`로 서브에이전트를 생성/취합하고 정상 종료함.
  2. **직접 작업 엄격 금지 규칙 확립**:
     경량 작업이라도 agy 메인의 직접 Read/Edit/Bash를 금지하고 제어면(라우팅/취합/보고)만 전담하도록 지침 정합화. 소환 실패 시 `[[BLOCKED]]`로 상위에 즉시 에스컬레이션.

---

## 12. 상주 워커 창 직접 타이핑 위험 및 사람-스크립트 간 send-keys 충돌 (Human Typing Hazard)

- **메타데이터**:
  - `root-cause`: `operational-hazard` / `human-interference`
  - `verified_with`: 2026-09-24
- **위험 내용**:
  - `AGY_MODE=resident` 환경에서 워커 tmux 창은 상시 대화형 TUI(composer 입력창)로 대기한다.
  - 인간 관찰자가 `tmux attach`로 세션에 진입하여 워커 창에 키보드로 직접 문자열을 입력하거나 수정 중인 상태에서 자동화 디스패치가 도달하면 다음과 같은 치명적 왜곡이 발생할 수 있다:
    1. **입력 버퍼 오염 (Composer Corruption)**: 사용자가 타이핑 중이던 미완성 텍스트와 디스패처의 1줄 도어벨(`Read and execute task prompt: ...`)이 합쳐져 비정상 프롬프트로 전송됨.
    2. **비인가 수동 실행 및 상태 불일치**: 사용자가 워커 창에서 직접 엔터를 눌러 프롬프트를 실행할 경우, `workers/<worker>.busy` 및 `tasks/<id>/state`가 기록되지 않은 상태이므로 `Stop` 훅은 fake `done` 발행을 안전하게 차단하지만 에이전트의 대화 맥락이 오염됨.
    3. **동시 입력 섞임**: RC로 사람이 타이핑 중인 창에 스크립트나 AI가 동시에 `tmux send-keys`를 보내면, tmux 입장에서는 구분이 불가능하므로 입력이 섞여 들어가 메시지가 깨지거나 절단된다.
- **방어 및 운용 규약**:
  - **디스패치 단계 선행 검증 (Input Interleaving Guard)**: `dispatch.sh`는 디스패치 전 대상 창이 상주 TUI 상태인지(`cur_cmd == worker`) 및 이전 작업의 `busy` 파일이 부재한지 검증하여, 최소한 자동화 에이전트 간 중복 입력은 원천 차단한다.
  - **인간 관찰자 운용 수칙**: 사람 운영자가 상주 세션을 관찰할 때는 **읽기 전용 모드(`tmux attach -r -t <session>`)**로 접속하거나 `sonnet` 창을 통해서만 감사하며, 워커 창(`agy`, `codex`) 내부에서 임의의 키보드 입력을 전송하지 않아야 한다. 사람이 활발히 타이핑 중인 것이 확인되면 자동화 쪽에서는 send-keys를 중단하고 `capture-pane`으로 대기해야 한다.

---

## 13. 테스트 하네스 런타임 오염 사고 및 훅/테스트 환경 격리 규약 (T0924-09/T0924-10)

- **메타데이터**:
  - `root-cause`: `test-harness-isolation` / `hook-fail-open`
  - `verified_with`: 2026-09-24
- **증상 및 사고 경위 (Live Pollution Incident)**:
  - T0924-09 수행 중 테스트 하네스가 상위 세션으로부터 상속받은 `ACC_RUNTIME` 환경변수(= 실제 라이브 운영 런타임 `runtime/agentchain-v2-153d25bc`)를 정제하지 않고 테스트를 실행함.
  - `bin/agy-stop-hook.sh`가 `ACC_RUNTIME` 부재 시 `resolve_session_and_runtime`으로 임의의 런타임에 부착되는 fail-open 결함과 결합하여, 실제 운영 태스크의 상태를 `error`로 덮어쓰고 `workers/agy.busy` 및 `lock`을 삭제하는 심각한 운영 런타임 오염 사고가 발생함.
  - 당시 보고서에서는 런타임 무오염을 허위 보고하였으나, 실측 감사 결과 운영 런타임 변조가 적발됨.
- **근본 원인 (Root Cause)**:
  1. **`bin/agy-stop-hook.sh` fail-open 결함**:
     - `ACC_RUNTIME` 미지정 시 폴백 해석으로 라이브 세션에 자동 부착.
     - task의 `mode=resident` 여부를 확인하지 않아 oneshot 작업에도 오반응.
     - `TMUX_PANE`을 확인하지 않아 v1 TUI/서브에이전트/oneshot agy의 Stop 훅에도 오반응.
  2. **테스트 하네스 미격리 (`test-harness-isolation`)**:
     - 테스트 스크립트 실행 시 환경변수 미정제(`env -i` 부재).
     - 실시간 운영 런타임 변조 감지 및 단언(`TESTMARK` grep assertion) 부재.
- **해결책 및 격리 규약 (Fix & Isolation Protocol)**:
  1. **훅 Fail-Closed 원칙 (Hook Fail-Closed Rules)**:
     - `bin/agy-stop-hook.sh`는 `ACC_RUNTIME`이 명시적으로 설정되어 있고 디렉토리이며, `workers/agy.resident` 마커 파일이 존재할 때만 실행.
     - `workers/agy.resident` 내 `pane_id`와 환경변수 `TMUX_PANE`이 일치할 때만 실행.
     - 작업 상태 파일(`state`)에 `mode=resident`와 `status=running`이 명시되어 있을 때만 이벤트 발행.
     - 그 외 모든 예외/부적합 상황에서는 무작업(no-op)으로 `{"decision":"allow"}`를 반환하고 즉시 안전 종료.
  2. **테스트 하네스 격리 규약 (Test Harness Isolation Rules)**:
     - 모든 테스트는 `env -i PATH="$PATH" HOME="$TMP/home" ...`로 깨끗한 환경에서 명시적 변수만 주입하여 실행 (`tests/lib-isolated-env.sh`).
     - 각 테스트는 고유 마커 `TESTMARK-<random>`을 생성하여 페이로드에 포함.
     - **Guard Check 1**: 임시 런타임 경로가 운영 런타임(`$LIVE_RT`)과 동일하거나 하위 디렉토리이면 즉시 abort.
     - **Guard Check 2**: 테스트 시작 및 teardown 시 운영 런타임 내 `TESTMARK` 검색 결과가 정확히 0건이어야 함을 단언 (0건 초과 시 즉시 테스트 실패). 플레인 텍스트뿐만 아니라 `summary_b64=` 등 Base64 인코딩 패턴도 함께 검사.
     - **Guard Check 3 (운영 런타임 시그니처 대조 - 필수)**:
       - **함정 (Pitfall)**: 단순 플레인 텍스트 마커 grep만으로는 Base64 인코딩 필드(`summary_b64`, `detail_path_b64`) 및 훅이 무시하는 페이로드 필드에 마커가 위치할 경우 운영 런타임 누수를 탐지하지 못하고 놓치는 심각한 맹점이 존재함. 따라서 운영 런타임 시그니처 대조(`workers/`, `tasks/*/state`, `events/` 목록, 설정 파일 해시)는 필수 불가결한 검증 규약임.
       - 테스트 시작 시점의 스냅샷과 테스트 종료 시점의 스냅샷을 대조하여 `workers/`, `tasks/*/state`, `events/`, 설정 파일(`bootstrap_version`, `session_name`, `claude-bridge.settings.json`)이 1바이트라도 변조되면 즉시 실패 처리. 진행 중인 running task가 있는 경우 정상 완료 상태 전이에 대한 오탐을 방지하기 위해 경고 출력 후 예외를 인정하되, `workers/` 및 다른 task들의 상태는 엄격히 불변을 대조함.

---

## 14. 실제 agy 측정/탐색 실행의 사용자 홈 오염 사고 및 격리 래퍼 규약 (T0924-12/T0924-13)

- **메타데이터**:
  - `root-cause`: `test-harness-isolation` / `unisolated-measurement`
  - `verified_with`: 2026-09-24
- **증상 및 사고 경위 (Home Config Pollution Incident)**:
  - T0924-12 (Round 3) 수행 중 F8 측정 실행(CLI 옵션 및 TUI 동작 계측)이 임시 HOME 없이 실제 사용자 환경에서 직접 실행됨.
  - 그 결과 실제 사용자 홈 `~/.gemini/config/projects/`에 등록 파일 3개가 무단 생성되는 오염이 발생함 (감독자가 사후 수동 삭제함).
  - 당시 보고서는 `settings.json` 및 `hooks.json`의 불변만 확인하고 "zero pollution"으로 오판 보고하였으나, `~/.gemini/config/` 전체를 검사하지 않아 발생한 맹점이었음.
  - 또한 E2E 실행 중 작업 대상 파일 경로가 상대 경로(`Create file a.txt in project dir...`)로 주어져, 서브에이전트가 디스패치 헤더의 `TEMPLATE_ROOT`(리포 루트)에 `a.txt`, `b.txt`를 생성하는 리포 오염 결함도 함께 확인됨.
- **근본 원인 (Root Cause)**:
  1. **실제 agy 직접 호출 시 임시 HOME 부재**: 계측/탐색 실험을 격리 하네스 래퍼 없이 직접 셸에서 실행.
  2. **검증 범위 협소**: `settings.json`만 검사하고 `~/.gemini/config/` 전체 트리 및 리포지토리 파일 생성 여부를 감시하지 않음.
- **해결책 및 격리 규약 (Fix & Isolation Protocol)**:
  1. **실제 agy 격리 실행 래퍼 필수화 (`run_real_agy_isolated`)**:
     - 측정, 탐색, E2E 등 실제 Antigravity CLI를 호출하는 모든 실행은 **반드시** `lib-isolated-env.sh`의 `run_real_agy_isolated`를 통해서만 수행해야 한다.
     - `HOME`이 실제 홈이거나 임시 경로(`/tmp/`) 하위가 아니면 즉시 abort(`exit 99`).
     - 실제 OAuth 토큰은 임시 HOME에 **심볼릭 링크**로만 마운트하며(복사 금지), 토큰 내용의 출력/노출은 엄격히 금지된다.
  2. **Gemini 전역 설정 전체 트리 스냅샷 대조 (`Guard Check 5`)**:
     - 테스트 전후에 실제 `~/.gemini/config/` 전체(파일 목록+크기+해시; `projects/` 포함)와 `~/.gemini/antigravity-cli/`의 설정류를 스냅샷 대조하여 1바이트라도 변경 시 즉시 실패 처리.
     - 단, 인증 토큰(`antigravity-oauth-token`)은 존재 여부 및 심볼릭 링크 대상만 대조하고 크기/해시/mtime은 무시하여 agy의 정상적인 OAuth 토큰 자동 갱신을 허용.
  3. **리포지토리 무오염 단언 (`Guard Check 4`) 및 절대경로 지정**:
     - E2E 및 모든 디스패치 프롬프트에서 작업 대상 파일은 반드시 절대 경로(`$TEST_PROJ/a.txt` 등)로 명시.
     - 테스트 전후 리포지토리의 `git status --porcelain --ignored=no`를 대조하여 새 파일이 검출되면 즉시 실패 처리.
  4. **보고서 규칙 (H4)**:
     - "무접촉 / zero pollution" 표현은 전역 설정 및 리포지토리 스냅샷 대조 출력(전후 동일 입증 증빙)을 첨부할 때만 사용 가능.

---

## 15. Sonnet이 요청받지 않은 아키텍처 후보를 Tier-3 자문에 임의로 얹어 과설계로 확대된 사고 (ecount_hub Termux 지원, 2026-09-28)

- **메타데이터**:
  - `root-cause`: `sonnet-overreach` (요청받지 않은 범위를 감독자 스스로 설계 후보에 포함)
  - `verified_with`: 2026-09-28

- **사고 경위**:
  - 사용자는 "Termux(안드로이드)처럼 Playwright/Chromium을 못 쓰는 환경에서도 MCP를 쓰게 만들 방법을 고민해 달라, Sol을 불러라"고만 요청했다. **SSH를 쓰라는 지시는 없었다.**
  - Sonnet이 Sol에게 보낼 자문 요청 프롬프트를 직접 작성하면서, 비교할 아키텍처 후보 목록에 **스스로 "(A) 원격 실행 위임: SSH를 통한 stdio MCP 서버"를 임의로 추가**했다(다른 두 후보 B/C도 함께 제시했으나, 문제는 사용자가 요청도 언급도 하지 않은 구체적 기술 스택 하나를 감독자가 먼저 제시해버린 것).
  - Sol과 그 답변을 받은 agy는 이 후보를 성실히 검토한 결과 "Phase 1 최적 선택"으로 채택하고, 이를 기반으로 SSH 키 생성, `authorized_keys` `restrict,command=` 제한, Tailscale 기기 분실 대응 매뉴얼, 보안 비교표, Claude Code `claude mcp add` SSH 설정 예시까지 **상당한 분량의 구체적 구현 가이드**를 `TERMUX_SUPPORT.md`에 작성해 커밋·푸시했다.
  - 사용자는 이후 세션에서 "리버스엔지니어링(순수 파이썬 네이티브 로그인)이 나을 것 같다"고 먼저 방향을 제시했고, 실제로 그 방향(Phase 2/3)이 성공적으로 구현된 뒤에야 "SSH로 할 거면 의미가 없다(그냥 호스트에 직접 붙는 것과 다를 바 없다)"고 SSH 방안 자체의 가치를 부정했다. 이 시점에 되짚어보니 SSH는 애초에 사용자가 원한 적이 없었고, 불필요하게 많은 분량의 문서·설계가 낭비됐다.
- **근본 원인**:
  - 감독자(Sonnet)가 하위 Tier-3 자문(Sol)에게 조사 과제를 위임할 때, **"사용자가 실제로 요청한 것"과 "감독자 자신이 떠올린 후보 아이디어"를 구분하지 않고 동일한 무게로 섞어서 제시**했다.
  - Sol/agy 입장에서는 감독자가 준 후보 목록을 곧이곧대로 신뢰하고 성실히 발전시켰을 뿐이므로, 문제의 원인은 하위 워커가 아니라 **상위 프롬프트를 작성한 감독자 자신에게 있다.**
  - 결과가 그럴듯하고 분량이 많다고 해서(보안 비교표, 로드맵, 단계별 가이드) 사용자가 원했던 방향이라고 착각하기 쉽다 — 산출물의 완성도와 "사용자가 요청했는지 여부"는 별개다.
- **재발 방지**:
  1. Tier-3(Sol/Opus) 자문 프롬프트를 작성할 때, 감독자 자신이 떠올린 후보/아이디어는 **"사용자 요청"과 명시적으로 분리 표기**한다(예: "사용자 요청: ~~~" vs "감독자 제안 후보(사용자 미확인): ~~~"). 최소한 자문 결과를 사용자에게 전달할 때 어느 부분이 감독자 자신의 아이디어였는지 명확히 밝힌다.
  2. 특히 구체적인 기술 스택(SSH, 특정 프로토콜, 특정 라이브러리 등)을 후보로 제시할 때는, 그것이 사용자가 이미 언급한 제약/선호에서 자연스럽게 도출된 것인지, 아니면 감독자가 일반론적으로 떠올린 것인지 스스로 점검한다.
  3. 하위 워커(Sol/agy)가 감독자 제시 후보 중 하나를 "최적 선택"으로 확정해 대량의 구현물을 만들기 전에, 그 후보가 사용자 요청 범위 안에 있는 것이 맞는지 감독자가 한 번 더 확인하고 넘기는 것이 이상적이다(단, 매번 왕복 확인을 요구하면 좋았던 delegation 흐름이 느려지므로, 최소한 "이건 내가 제안한 후보다"라는 라벨링만이라도 반드시 남긴다).

---

## 16. Post-mortem/Learning Capsule 규약이 자동 전파되지 않고 프로젝트를 바꾸자마자 소실된 사고 (2026-09-28)

- **메타데이터**:
  - `root-cause`: `template-bug` / `self-reflection-not-automatic`
  - `verified_with`: 2026-09-28

- **증상 (정량적)**:
  - 같은 세션 안에서 `ecount_hub`(별개 프로젝트) 작업을 10라운드 디스패치했는데, **10건 전부 `learning_capsule`/`knowledge_refs` 규약을 포함하지 않았다.**
  - ROLES.md §"종료 보고 형식"에는 `refs_loaded`, `learning_capsule`, `[[DONE <id>]] result=... | learning=...` 형식이 명시되어 있음에도, 실제로는 단 한 번도 지켜지지 않았다.
- **근본 원인**:
  1. `bin/dispatch.sh`가 모든 디스패치에 자동 주입하는 `[CHAIN CONTEXT]` 헤더(`WORKER_RULES`)에는 "직접 작업 금지"와 "codex 컨벤션 우선"만 박혀 있고, **`learning_capsule`/보고 형식 규칙은 전혀 포함되어 있지 않다.**
  2. agy는 oneshot 모드에서 `ROLES.md`를 자동으로 읽지 않는다(§1 T0924-01 프로브에서 이미 실측 확인된 사실). 따라서 이 규약은 **감독자(Sonnet)가 매 디스패치 프롬프트에 직접 재기술해야만** 작동하는, 사실상 감독자의 기억력에 100% 의존하는 비자동 시스템이었다.
  3. 감독자가 새 프로젝트(다른 작업 맥락)로 전환하면서 매번 처음부터 프롬프트를 새로 작성했고, 그 과정에서 이 관례를 그대로 빠뜨렸다. 이를 잡아낼 어떤 기계적 안전장치도 없었다 — 조용히 샜고, 아무도 눈치채지 못했다(사용자가 직접 "자가반영이 되는지 훑어보라"고 요청하기 전까지).
- **함의**: 어제(9/23) 정성 들여 설계한 Post-mortem Lifecycle(Capture→Curate, `known-issues.md`/`lessons.md`/`incidents/*.md` 3계층)은 **문서로만 존재하고, 실제로는 작동을 보장하는 메커니즘이 없다.** "규칙을 문서에 적어뒀다"와 "규칙이 실제로 매번 지켜진다"는 완전히 다른 문제라는 걸 정량적으로 보여준 사례.
- **재발 방지 (제안, 미구현)**:
  1. **`bin/dispatch.sh`의 자동 헤더에 규약을 직접 인라인**해야 한다(ROLES.md 경로만 던져주는 게 아니라). 예: `WORKER_RULES`에 `"Report format: end with [[DONE <task_id>]] result=<summary> | learning=<one-line|none>"` 한 줄을 추가하면, 어떤 프로젝트로 디스패치하든 감독자가 매번 재기술할 필요 없이 구조적으로 강제된다.
  2. 이게 되기 전까지는, 감독자가 새 프로젝트/새 맥락으로 넘어갈 때마다 "이 프로젝트에도 Post-mortem 규약을 프롬프트에 넣었는가"를 스스로 체크리스트로 확인해야 한다(신뢰할 수 없는 임시방편이지만, 구조적 수정 전까지는 최소한의 안전장치).
  3. 장기적으로는 워커의 최종 보고에 `learning=` 필드가 없으면 `event-emit`/감독자 감사 단계에서 경고를 내는 것도 고려할 만하다(다만 자연어 자유서식 보고를 기계적으로 파싱해야 하므로 난이도가 있음 — 이번엔 제안만 남기고 구현하지 않음).
