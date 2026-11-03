# 현재 프로젝트와 인스턴스 연결

템플릿 규칙은 [SUPERVISOR](../SUPERVISOR.md), 프로젝트 조율은 아래 두 파일이다. 이 표는 프로젝트 층의 로컬 배치 기록이다(2026-11-03).

| 대상 | 작업 위치·상태 진입점 | 조율 정본 |
|---|---|---|
| 디스패처 | 개발 `/home/rerun/scheduler-staging`, 운영 `/home/rerun/scheduler`; [RESUME_HANDOFF](/home/rerun/scheduler/docs/RESUME_HANDOFF.md) §0 | [scheduler.md](scheduler.md) |
| 메인트보드 | 개발 `/home/rerun/maintboard-worktrees/app`, 운영 `/home/rerun/maintboard-worktrees/live`; [구현 상태](/home/rerun/maintboard-worktrees/app/docs/IMPLEMENTATION_STATUS.md) | [maintboard.md](maintboard.md) |

## agentchain-v3 / v4 매핑

- v3: runtime `agentchain-v3-20261012`, 기존 브리프의 PROJECT_DIR은 `/mnt/c/Users/rerun/llm-wiki`. 디스패처·메인트보드 겸용 감독 세션이다. [기존 HANDOFF 진입점](/home/rerun/ecount_hub/logs/handoff-20261012.md)에서 이 표로 연결한다. 요청 대상에 따라 위 지침 **하나**를 선택한다. 위키 cwd를 제품 작업 위치로 간주하지 않는다.
- v4: runtime `agentchain-v4-20261030`, 기존 브리프의 PROJECT_DIR도 `/mnt/c/Users/rerun/llm-wiki`다. v4를 메인트보드 전용 등으로 추정하지 않는다. 새 브리프의 템플릿 → 이 선택표 → 요청 대상 지침 경로를 따른다. HANDOFF_FILE이 있으면 먼저 읽되 위 표와 연결한다. 인스턴스 env 자체는 이번 작업에서 열람·수정하지 않았다.
- `HANDOFF_FILE`을 새로 지정할 때: 겸용은 이 파일의 절대경로, 단일 대상은 기존 프로젝트 HANDOFF(이 표 링크 포함) 또는 해당 지침을 지정하면 된다. 기존 인스턴스 값을 변경했다는 뜻이 아니다.
- v3 watchdog(10/20 기동), v4 watchdog(10/30 기동)은 11/02 수정 전 프로세스다. v5(11/02 11:39 기동)는 새 코드 세션이다. 이번에는 어느 프로세스도 재시작하지 않았다.

메인트보드 main의 `docs/HANDOFF.md`는 git worktree 목록상 `/tmp/mb-pub`가 prunable로 남아 실제 경로가 없다. [남아 있는 main 문서 사본](/home/rerun/tmp/mb-pub-home/docs/HANDOFF.md)을 읽어 조율 근거를 확인했다. 그 사본의 날짜·HEAD 표는 현재 상태가 아니며, 실제 app/live git과 배포 기록이 근거다. 인계 문서를 새 위치에 복제하지 않는다.

## 위키 자체 작업

루트 볼트는 `/mnt/c/Users/rerun/llm-wiki`다. 그 프로젝트에서 심층 상담·조사 산출물을 등록할 때는 `Research/YYYY-MM-DD-<topic>.md`와 `Research/INDEX.md`를 사용한다. 기존 ROLES의 로컬 볼트 자동등록 규칙을 이 프로젝트 층으로 옮겼다. 제품의 소 수정에 조사·위키 등록 단계를 새로 만들라는 뜻은 아니다.
