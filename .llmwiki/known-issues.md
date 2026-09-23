---
title: Known Issues & Limitations
tags: [known-issues, limitations, remote-control, fallback, wsl]
related: ["[[INDEX]]", "[[architecture]]"]
summary: Remote Control(RC) 구독/OAuth 전제조건, agy-Codex 자동 연동 부재, 무상태 워치독(폴백/하트비트 부재), WSL 환경 주의사항.
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
