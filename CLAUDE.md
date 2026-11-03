# CLAUDE.md — Agent Command Chain Template

이 리포지토리에서 작업하는 모든 Claude Code 세션은 아래 지침을 따른다.

## 시작 시 참조

1. [SUPERVISOR.md](SUPERVISOR.md)를 읽는다. 역할·작업 경중·지시서·검증 판단의 단일 정본이다.
2. [문서 3층과 참조 경로](docs/INSTRUCTION-LAYERS.md)에 따라 프로젝트 지침과 인계 파일을 선택한다.
3. [.llmwiki/INDEX.md](.llmwiki/INDEX.md)에서 현재 작업에 필요한 구현·운영 페이지만 읽는다. 재개 작업은 [session-resume](.llmwiki/session-resume.md)를 참조한다.
4. 위키 편집 시 YAML frontmatter `title`, `tags`, `related`, `summary`를 유지하고 INDEX를 갱신한다.

## 프로젝트 기본 정보

- **정체성**: 프로젝트 비종속 범용 4계층 명령계통 tmux 템플릿 (v2 Full-Push 기본 권장, v1 레거시/폴백 보존, v2.3 구조 계약 및 구현 유연성 분리).
- **핵심 파일 및 구조**:
  - `bootstrap-v2.sh` / `watchdog-v2.sh`: v2 Full-Push(FIFO + asyncRewake) 브리지 및 4대 안전망 워치독.
  - `bootstrap.sh` / `watchdog.sh`: v1 tmux 세션 기동(폴더 신뢰 자동확인, 브리핑 생성) 및 무상태 워치독.
  - `bin/`: 디스패처(`dispatch.sh`), 이벤트 발행/대기/확인(`event-emit.sh`, `sonnet-event-wait.sh`, `event-ack.sh`), 워커 러너(`run-task.sh`), agy Stop 훅(`agy-stop-hook.sh`), 작업트리 정리(`worktree-clean.sh`), 모델 동적 해석(`resolve-model.sh`), 체인 상태/복구(`chain`, `chain-recover.sh`, `task-abandon.sh`).
  - `lib/`: 터미널 백엔드 추상화(`term.sh`), 세션 관리(`session.sh`), 사유 코드 분류·검증(`reason.sh`), 능동성 게이트(`gate.sh`).
  - `adapters/`: agy/codex 단발 실행기(`agy-oneshot.sh`, `codex-oneshot.sh`).
  - `dashboard/`: 읽기 전용 상태 웹 대시보드 (`server.py`, `index.html`, `run.sh`).
  - `config.env.example` / `config.env`: 환경 설정 및 CLI 기동 파라미터 (Agy 모델 티어, 터미널 백엔드, 능동성 모드 포함).
  - `SUPERVISOR.md` / `ROLES.md`: 감독자 운영 정본 / 워커 진입점·보고 계약.
  - `docs/`: 구조 계약 및 구현 설계서 (`PRINCIPLES-structure-vs-implementation.md`, `IMPL-DESIGN-20261102.md`).
