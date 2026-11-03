---
title: Known Issues & Limitations
tags: [known-issues, limitations, bug, product-bug, permissions, remote-control, fallback, wsl, tmux-nesting, session-resolution, dispatch-targeting, non-preemptive-tui, cli-behavior, prompt-drift, test-harness-isolation, troubleshooting, sonnet-overreach, unrequested-scope, self-reflection-not-automatic, stale-lease, auto-mode-classifier, resident-subagent, process-kill-safety, codex-quota-fallback, live-session-pollution]
related: ["[[INDEX]]", "[[architecture]]", "[[usage]]", "[[session-resume]]"]
summary: 실측으로 확인된 RC 전제조건, Folder Trust 우회 한계, 세션 지속성 버그, agy/codex 바이너리 이슈, tmux 중첩, v1/v2 세션 오조준, TUI 비선점 큐잉, 입력 혼선, 테스트 하네스 격리 규약, Sonnet의 요청 외 범위 임의 확장 사고, Post-mortem 누락 사고, stale lease 고착, auto-mode 분류기 차단, 상주 agy 대기 정체, 데몬 kill 세션 종료, Codex 쿼터 고갈, 라이브 런타임 오염 방지.
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

현행 감독자 운영은 [SUPERVISOR](../SUPERVISOR.md)와 [ROLES](../ROLES.md)를 따른다. 아래 날짜·버전·프로젝트 사례는 사고 이력이며 새 작업의 기본 절차가 아니다.

## 2. agy → Codex 자동 연동 부재

이 템플릿은 외부 라우터 엔진을 제거한 MVP 경량 구조다:

- `eraweb-fork`의 경우 `tools/router/src/adapters/codexAdapter.ts` 등에서 Node 서브프로세스를 직접 spawn하여 agy가 Codex를 호출했다.
- 본 템플릿에는 해당 어댑터 레이어가 없으므로, **agy가 스스로 Codex 창을 조작하거나 자동 호출하지 못한다.**
- **현행 대응**: v2 이벤트를 받고 역할에 맞게 재dispatch한다. 실행 명령은 [[usage]], 역할 판단은 SUPERVISOR에 둔다.

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
  - 모델명은 `bin/resolve-model.sh`를 통해 계정 카탈로그/설정의 최신 지원 모델(sol 계열 등)로 자동 동적 해석됨.
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

2026-09-23, agy 메인의 직접 툴 루프 도중 새 입력이 `queued messages`에 쌓여 상위 지시가 늦게 수신됐다. 당시 위임 규칙 누락과 TUI 비선점 특성이 원인이었다. 539회라는 도구 횟수는 미재현이며 위임 중 인터럽트 선점도 당시 미검증이었다.

당시 “상주 폐기”는 후속 resident Stage 1 지원으로 대체됐다. 현행 역할은 [SUPERVISOR](../SUPERVISOR.md), oneshot/resident 실행은 [[usage#42-resident-tui-브리지-운용-stage-1-agy-상주]]에 둔다.

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

2026-09-24 T0924-09에서 상속된 ACC_RUNTIME과 Stop 훅의 fail-open이 결합해 라이브 태스크·busy 상태가 변조됐다. 당시 무오염 자기보고와 실제 로그가 달랐다.

훅은 명시 runtime·resident/pane/task 일치를 검사하도록 수정됐다. 체인 훅·dispatch **실험**은 [tests/lib-isolated-env.sh](../tests/lib-isolated-env.sh)의 격리 하네스를 사용한다. 구현 격리·전후 런타임 서명 검사는 [[architecture#29-테스트-하네스-격리-규약-및-운영-런타임-보호-체계-test-harness-isolation--live-protection]]에 둔다. 이 규약을 일반 제품 작업의 지시서에 복사하지 않는다.

---

## 14. 실제 agy 측정/탐색 실행의 사용자 홈 오염 사고 및 격리 래퍼 규약 (T0924-12/T0924-13)

2026-09-24 T0924-12에서 실제 agy 계측을 실제 HOME으로 실행해 전역 projects 등록 파일 3개가 생겼다. 일부 설정만 대조한 보고가 이를 놓쳤고, 상대 대상 경로로 a.txt/b.txt가 템플릿에 생긴 사고도 있었다.

실제 CLI 계측·E2E는 `tests/lib-isolated-env.sh`의 `run_real_agy_isolated`와 전체 설정·리포 전후 검사를 사용한다. 상세 구현은 architecture §2.9, 하네스가 정본이다. 인증값을 읽으라는 지시로 해석하지 않는다.

---

## 15. Sonnet이 요청받지 않은 아키텍처 후보를 Tier-3 자문에 임의로 얹어 과설계로 확대된 사고 (ecount_hub Termux 지원, 2026-09-28)

2026-09-28 원격 실행 환경 자문에 감독자가 사용자 미요청 기술 후보를 추가해 조사·설계·보안 절차가 크게 늘었다. 사용자 원문보다 감독자의 후보가 요구로 굳어진 사고다.

현행 범위 판단·원문 전달은 [SUPERVISOR](../SUPERVISOR.md)에 둔다. 자문에 감독자 제안을 추가한다면 사용자 요구와 구분해 표시하고, 그 제안을 승인된 구현 범위로 취급하지 않는다.

---

## 16. Post-mortem/Learning Capsule 규약 전파 누락 사고 (2026-09-28)

과거 10라운드에서 ROLES에 있던 보고 규약이 전달되지 않았다. 이후 임시방편인 “매 지시서에 전부 재기술”이 지침·메모리에 복제되어 과지시를 만들었다.

현행 정본은 [ROLES](../ROLES.md)의 공통 보고 규약이며 지시서에는 그 문서를 **읽고 적용**하라는 참조만 둔다. 현재 dispatch는 agy에 규약 D를 자동 주입하지만 knowledge_refs/refs_loaded/learning_capsule 전부를 자동 생성하지는 않는다. 자동 생성과 문서 열람을 혼동하지 않는다. 이전의 수동 재기술 체크리스트와 자동 경고 제안은 현행 의무가 아니다.

---

## 17. 워커 비정상 중단 후 state 파일이 running으로 고착되어 디스패치가 거절되는 문제 (exit 75, Stale Lease)

- **메타데이터**:
  - `root-cause`: `concurrency-control` / `stale-lease`
  - `verified_with`: 2026-10-01 (운영 실측)
- **증상**:
  - `dispatch.sh` 실행 시 다음 에러와 함께 즉시 종료된다:
    ```text
    Error: Worker 'agy' is currently busy executing task 'agy-1727...'. Dispatch rejected.
    ```
    (종료 코드: exit 75)
- **원인**:
  - agy 상주 TUI 또는 워커 프로세스가 예기치 않게 종료되었거나 크래시되었으나, `tasks/<task_id>/state` 파일이 `status=running`으로 방치되고 `workers/<worker>.busy` 파일이 잔류함.
  - `dispatch.sh`는 이중 디스패치 방지를 위해 `workers/<worker>.busy`에 기록된 활성 태스크의 `state` 파일 내 `status=running` 여부를 검사하므로, 해당 파일이 정리되지 않으면 무기한 디스패치를 거부함.
- **대응 및 해결 방안**:
  - 전용 복구 스크립트인 `bin/task-abandon.sh`를 실행한다:
    ```bash
    ./bin/task-abandon.sh <task_id>
    # 또는 워커명 지정 자동 복구:
    ./bin/task-abandon.sh agy
    ```
  - `bin/task-abandon.sh`는 `state` 파일의 상태를 `status=abandoned`로 원자적 치환하고, `workers/<worker>.busy` 파일을 즉각 삭제하여 다음 디스패치가 즉시 가능하도록 회수한다.
  - 만약 워커 PID가 여전히 생존해 있다면 스크립트가 안전을 위해 수정을 거부하므로, 프로세스 종료를 동반하려면 `--force` 플래그를 붙인다 (`./bin/task-abandon.sh agy --force`).

---

## 18. Claude Code auto-mode 보안 분류기(classifier)에 의한 상태 파일 직접 편집 차단

- **메타데이터**:
  - `root-cause`: `cli-behavior` / `auto-mode-classifier`
  - `verified_with`: 2026-10-01 (운영 실측)
- **증상**:
  - Claude Code의 auto-mode 환경에서 모델이 `sed`, `awk`, 셸 리다이렉션(`>`) 등을 사용해 `tasks/*/state` 파일을 직접 수정하여 복구하려고 시도하면, Claude Code의 안전 분류기(classifier)에 의해 도구 호출이 사전 차단(block)되고 사용자 승인 프롬프트가 뜨거나 명령이 실패한다.
- **원인**:
  - Claude Code auto-mode의 보안 정책이 시스템 내부 상태 파일이나 런타임 제어 파일을 임의로 변조하는 패턴을 고위험 행위로 분류하여 차단하기 때문이다.
- **대응 및 해결 방안**:
  - 모델이 셸 커맨드로 `state` 파일을 직접 변조하려고 시도하지 않는다.
  - 표준 복구 도구 `bin/task-abandon.sh`를 사용한다. 차단 대응은 [[session-resume#54-claude-auto-mode-분류기-차단-시-대처]]에 둔다.

---

## 19. Resident agy 상주 TUI가 사망한 서브에이전트를 무한정 대기하는 현상

- **메타데이터**:
  - `root-cause`: `cli-behavior` / `resident-subagent`
  - `verified_with`: 2026-10-01 (운영 실측)
- **증상**:
  - `AGY_MODE=resident` 환경에서 agy 메인 라우터가 `invoke_subagent` 도구로 서브에이전트를 기동한 후, 서브에이전트가 예기치 않게 종료되었음에도 메인 TUI가 결과를 수신하지 못하고 무한정 대기(waiting_for_message) 상태에 머문다.
- **원인**:
  - Antigravity CLI의 서브에이전트 라이프사이클 관리에서 자식 에이전트의 충돌/사망 이벤트가 상위 TUI 세션의 턴 종료 트리거로 즉각 연결되지 못하는 엣지 케이스가 존재하기 때문이다.
- **대응 및 해결 방안**:
  - `watchdog-v2.sh`의 stall/deadline 감시 이벤트가 발생하면, 감독자(Sonnet)는 `bin/task-abandon.sh agy`를 실행해 태스크를 포기 처리한다.
  - 이후 agy 윈도우에 `/clear` 명령을 전송하여 컨텍스트를 초기화하고 유휴 상태로 재무장(rearm) 지침을 주입한 뒤 작업을 재디스패치한다:
    ```bash
    tmux send-keys -t agentchain-v2:agy "/clear" Enter
    sleep 1
    tmux send-keys -l -t agentchain-v2:agy "ACC_ROLE: You are the resident router. Do not do file writing or coding directly. Always delegate via invoke_subagent and wait."
    tmux send-keys -t agentchain-v2:agy Enter
    ```

---

## 20. 별도 tmux 창에서 백그라운드 데몬 서버 PID 강제 kill 시 세션 전체 종료 사고

2026-10-01 데몬인 줄 알고 종료한 PID가 tmux 창의 루트 프로세스라 창·세션이 닫혔다. 패턴 종료는 부모까지 매칭할 수 있었다.

공통 안전선은 [SUPERVISOR](../SUPERVISOR.md)에 둔다. 대시보드는 `dashboard/run.sh stop`을 사용하고, 그 외 종료는 계보를 확인한 작업 프로세스 PID만 대상으로 한다.

---

## 21. Codex 쿼터 소진 (Quota/Rate Limit Exhaustion)

429/usage limit을 실제 관측했을 때 중복 재시도를 피한다. 과거의 “codex이면 무조건 agy” 규칙은 리드와 구현 슬롯을 혼동하므로 대체한다. 현행 폴백은 [SUPERVISOR](../SUPERVISOR.md) 역할 절을 따른다.

---

## 22. 실험 중 SESSION_NAME 누락 또는 환경변수 ACC_RUNTIME 상속으로 인한 라이브 운영 세션 오염 사고

2026-10-01 실험에서 세션명 누락·ACC_RUNTIME 상속으로 라이브 런타임에 시험 태스크가 생겼다. 원인과 격리 대응은 §13과 같으므로 반복하지 않는다. 호출 대상 선택은 [[usage#41-작업-디스패치-v2-full-push-권장]], 실험은 격리 하네스를 따른다.

---

## 23. agy resident 완료 알림 누락 및 턴 조기 종료 결함 (센티널 기반 완료 판정 부재)

2026-10-13 resident Stop 훅이 서브에이전트 위임 직후 조기 done을 발행하거나 완료 알림을 놓쳤다. fullyIdle만 보던 판정을 마지막 모델 응답의 센티널과 시작 시각 가드로 보강했다. 논리 이름 ID도 허용하되 busy가 가리키는 현재 작업만 닫는다.

현재 구현은 [[architecture#28-resident-tui-브리지-stage-1-agy-상주--codex-oneshot]], 보고 계약은 [ROLES](../ROLES.md), 회귀 사례는 [stop-hook-test.sh](../tests/stop-hook-test.sh)에 둔다. 자동 규약 D가 주입되므로 지시서에 꼬리말을 복사하지 않는다.

---

## 24. 워커 정체 경고 진단

정체 경고에서 변경 파일 존재만 보고 정상 진행이라 결론내리지 않는다. 실제 장시간 대기 프로세스·CPU·마지막 출력 시각을 확인한다. 기존 `~/bin/stall-diag.sh <worktree>`가 있으면 사용하고, 확인된 정체 작업·임시 서버만 정확한 PID로 처리한다. 운영 프로세스에는 프로젝트 경계를 적용한다. 과거 경고 2회에도 테스트가 38분 멈춘 사례의 교정을 메모리에서 이곳으로 옮겼다.
