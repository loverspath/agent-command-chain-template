# CLAUDE.md — Agent Command Chain Template

이 리포지토리에서 작업하는 모든 Claude Code 세션은 아래 지침을 따른다.

## 시작 시 필독 지침

1. **내부 위키 색인 먼저 확인**: 세션을 시작하면 가장 먼저 [`.llmwiki/INDEX.md`](.llmwiki/INDEX.md)를 읽어 프로젝트 전체 구조와 문서 맵을 파악해라.
2. **선별적 문서 열람**: 위키 내 모든 문서를 한 번에 다 읽지 말고, 현재 수행하려는 작업과 관련된 페이지만 골라서 읽어라 (예: 브리지/프로세스 수정 시 `architecture.md`, 제어 명령 확인 시 `usage.md`, 제약/에러 대응 시 `known-issues.md`).
3. **위키 수정 및 추가 규칙**: 위키 문서를 수정하거나 새로 작성할 때는 `.llmwiki/` 내부 규칙(YAML frontmatter 필수: `title`, `tags`, `related`, `summary`)을 엄격히 준수해라.

## 프로젝트 기본 정보

- **정체성**: 프로젝트 비종속 범용 4계층 명령계통 tmux 템플릿 (`sonnet`, `agy`, `codex`).
- **핵심 파일**:
  - `bootstrap.sh`: 원클릭 tmux 세션 및 3개 윈도우 기동, 자동 폴더 신뢰 승인, 브리핑 전달.
  - `watchdog.sh`: 3개 윈도우 프로세스 생존 주기적 감시 및 단순 재기동.
  - `config.env.example` / `config.env`: 환경 설정 및 CLI 기동 파라미터.
  - `ROLES.md`: 계층별 최초 프롬프트 및 수동 핸드오프 포맷.
