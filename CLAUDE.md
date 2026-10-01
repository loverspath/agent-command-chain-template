# CLAUDE.md — Agent Command Chain Template

이 리포지토리에서 작업하는 모든 Claude Code 세션은 아래 지침을 따른다.

## 시작 시 필독 지침

1. **내부 위키 색인 먼저 확인**: 세션을 시작하면 가장 먼저 [`.llmwiki/INDEX.md`](.llmwiki/INDEX.md)를 읽어라. 이 리포의 아키텍처, 사용법, 알려진 이슈가 태그/링크로 인덱싱되어 있다.
2. **선별적 문서 열람**: 위키 내 모든 문서를 한 번에 다 읽지 말고, 현재 수행하려는 작업과 관련된 페이지만 골라서 읽어라 (예: 브리지/프로세스 수정 시 `architecture.md`, 제어 명령 확인 시 `usage.md`, 제약/에러 대응 시 `known-issues.md`).
3. **위키 수정 및 추가 규칙**: 위키 문서를 수정하거나 새로 작성할 때는 `.llmwiki/` 내부 규칙(YAML frontmatter 필수: `title`, `tags`, `related`, `summary`)을 엄격히 준수해라.
4. **세션 재개 및 환경 재현**: 신규 세션에서 환경을 재현하거나 프로젝트 작업을 이어받을 때는 [`.llmwiki/session-resume.md`](.llmwiki/session-resume.md)를 먼저 확인하라.

## 프로젝트 기본 정보

- **정체성**: 프로젝트 비종속 범용 4계층 명령계통 tmux 템플릿 (v2 Full-Push 기본 권장, v1 레거시/폴백 보존).
- **핵심 파일 및 구조**:
  - `bootstrap-v2.sh` / `watchdog-v2.sh`: v2 Full-Push(FIFO + asyncRewake) 브리지 및 4대 안전망 워치독.
  - `bootstrap.sh` / `watchdog.sh`: v1 tmux 세션 기동(폴더 신뢰 자동확인, 브리핑 생성) 및 무상태 워치독.
  - `bin/`: 디스패처(`dispatch.sh`), 이벤트 발행/대기/확인(`event-emit.sh`, `sonnet-event-wait.sh`, `event-ack.sh`), 워커 러너(`run-task.sh`), agy Stop 훅(`agy-stop-hook.sh`).
  - `adapters/`: agy/codex 단발 실행기(`agy-oneshot.sh`, `codex-oneshot.sh`).
  - `dashboard/`: 읽기 전용 상태 웹 대시보드 (`server.py`, `index.html`, `run.sh`).
  - `config.env.example` / `config.env`: 환경 설정 및 CLI 기동 파라미터.
  - `ROLES.md`: 계층별 최초 프롬프트 및 수동 핸드오프 포맷.
