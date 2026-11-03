# 메인트보드 — 체인 조율 지침

공통 역할·경중·검증 선택은 [SUPERVISOR](../SUPERVISOR.md), 보고는 [ROLES](../ROLES.md)를 읽고 적용한다. 현재 위치와 HANDOFF 사본의 한계는 [프로젝트 선택표](README.md)에 있다.

## 작업 맥락과 경계

개발은 `/home/rerun/maintboard-worktrees/app`(app), 운영 코드는 `/home/rerun/maintboard-worktrees/live`(detached)다. `/home/rerun/maintboard`는 초기 spec/p1이며 최신 구현으로 간주하지 않는다. 상태는 [IMPLEMENTATION_STATUS](/home/rerun/maintboard-worktrees/app/docs/IMPLEMENTATION_STATUS.md), 목적은 [CONTEXT](/home/rerun/maintboard-worktrees/app/docs/CONTEXT.md), 실제 API는 [API_RUNTIME](/home/rerun/maintboard-worktrees/app/docs/API_RUNTIME.md)로 확인한다.

- 목적은 정비 수행 누적과 장비 맥락 조회다. 상태는 라벨이며 필수 검사·승인자·종결조건·전이 제한을 제품에 새로 만들지 않는다. 설정 지향·한 도메인 한 모듈을 따른다.
- RentPlan은 원천 입력이고 이카운트는 읽기만 한다. 외부 ERP 직접 호출을 추가하지 않는다. 디스패처의 스냅샷을 흡수하며 디스패처 폴더·8088·운영 세션은 이 프로젝트 범위 밖이다.
- 재흡수는 멱등, raw 보존, 사람이 바꾼 라벨·메모·장비 연결 보호가 기준이다. partial/conflict는 자동 병합하지 않는다. [IMPORT](/home/rerun/maintboard-worktrees/app/docs/IMPORT.md)가 세부 정본이다. 오래된 상태를 근거로 기록을 삭제하지 않는다.
- 이미 취득한 실제 CSV·스냅샷([A/S CSV](/home/rerun/ecount_hub/data/as_records.csv) 등)을 사용한다. 원본은 보존한다. 개별 장비 사실 확인은 데이터 정리 목록으로 남기고 구조 결정과 섞지 않는다.

## 검증과 운영 반영

개발 루프의 기본 확인은 실제 화면(폰 폭 포함)과 의도 일치다. 보고용 스모크·형식적 재검증을 의무로 추가하지 않는다. 데이터·흡수 변경에는 해당 위험을 확인한다. 테스트가 필요하면 `run_tests.py`가 임시 DB를 쓰지만 사진 경로 `MAINTBOARD_MEDIA_DIR`도 별도로 격리한다. 상세는 [첨부 런타임](/home/rerun/maintboard-worktrees/app/docs/attachments-runtime.md)을 따른다.

운영 8090·DB(`app/data/maintboard.db`) 쓰기, `imports ensure apply`, live checkout·재기동은 리드의 승인된 운영 반영 범위에서만 한다. 운영 DB 변경 전 DB+사진을 함께 온라인 백업·검증하고, 복원은 새 경로에서 먼저 한다. 명령·옵션은 [backup-restore](/home/rerun/maintboard-worktrees/app/docs/backup-restore.md)에만 둔다. 배포된 커밋은 live HEAD·기동 시각·배포 기록을 대조한다.

## 이 프로젝트의 운영 선호

이미 위임받은 빌드 루프는 매 지시서 승인 질문 없이 끝까지 진행한다. Sol 산출물·사용자 말은 원문으로 전달한다. 관련 사용자 발언은 기존 [mb-inbox](/home/rerun/ecount_hub/logs/mb-inbox.md)에 원문으로 기록하며 감독 해석을 섞지 않는다. 설계·명세의 공유가 필요한 마일스톤에는 기존 문서 main에 게시하되 매 소 수정마다 별도 문서·푸시 단계를 만들지 않는다.

상주 agy를 Flash로 쓰라는 선호는 이 로컬 인스턴스의 쿼터 교정 이력이다. 고정 버전 문자열을 새 템플릿 기본값으로 옮기지 말고 `agy models`/resolver와 인스턴스 오버라이드를 사용한다. 예제 설정과 실제 적용값을 혼동하지 않는다.

출처: 기존 HANDOFF §1~5, AGENTS, CONTEXT, backup-restore, 통합 전 스모크/원문전달/실제CSV/강제프로세스 금지 메모리.
