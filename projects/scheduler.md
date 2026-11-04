# 디스패처 — 체인 조율 지침

공통 역할·경중·검증 선택은 [SUPERVISOR](../SUPERVISOR.md), 보고는 [ROLES](../ROLES.md)를 읽고 적용한다. 이 문서는 기존 프로젝트 규칙의 진입점이며 세부 절차를 재기술하지 않는다.

## 작업 맥락과 경계

[현재 상태·기능 지도](/home/rerun/scheduler/docs/RESUME_HANDOFF.md) §0 → [개발 규범](/home/rerun/scheduler/docs/DEVELOPMENT_GUIDELINES.md) → [관련 CONTRACTS](/home/rerun/scheduler/CONTRACTS.md) 순으로 필요한 절만 읽는다. 개발은 scheduler-staging 또는 격리 worktree, 운영은 scheduler(main), `scheduler-prod-8088`/8088이다. 운영 변경·접촉은 위임 범위에 포함될 때만 한다.

RentPlan·이카운트 원천은 읽기 입력이다. 명시 승인 없는 외부 쓰기는 하지 않는다. 이번 문서 정리에는 외부 쓰기 승인이 없다. 내부 배차·메모 오버레이와 외부 원천 쓰기를 구별한다. UI 값·문구는 기존 config→meta→화면 경로를 사용한다. 여러 화면의 같은 도메인 정보는 기존 공용 데이터 계약·렌더러·문구 설정을 함께 재사용한다. 실제 응답 파서 확인에는 취득한 응답을 쓴다.

## 검증과 운영 반영

- 개발 확인은 변경 위험에 맞는 한 번으로 묶는다. 화면 국소 변경은 해당 화면 확인, 파서·데이터 변경은 실제 응답/페이로드 대조 등 해당 계약을 확인한다. UI 전체 스모크를 자동 추가하지 않는다.
- **배포 정본:** [DEPLOY.md](/home/rerun/scheduler/docs/DEPLOY.md). 리드가 `release.sh` 내장 preflight를 그대로 사용한다. 소 작업이라는 이유로 내장 검사·격리를 우회하지 않으며, 같은 전체 검사를 별도로 중복 지시하지 않는다. 격리 포트는 18088이다.
- **상태 백업:** main/staging push 또는 릴리스 직후 리드가 `backup_state.sh`로 private-state를 갱신한다. 격리 사본의 누락된 상태는 `restore_state.sh <대상폴더>`로 복원한다. 명령·대상과 포함 범위는 DEPLOY §1.4 및 해당 도구가 정본이다. 비밀값은 열지 않는다.

## 편성·현행성

기본 편성의 예외는 현재 승인된 작업에만 적용한다. 10/14 구조 정리 be1~be3의 Terra 임시 배정과 10/24 하루 한정 Sol 지시는 만료됐다. 실제 Sol 최신 모델은 resolver로 선택하고, 이 사용자 인스턴스의 effort 선호는 medium이다. 과거 장애의 “월요일 재확인”은 상시 명령이 아니다. RentPlan 큰 응답 장애는 메모리의 10/26 복구 기록으로 종료됐으며, 남은 후보는 현재 HANDOFF/백로그에서 판단한다.

출처: 기존 handoff-20261012 운영 추가분, 프로젝트 AGENTS·DEPLOY·RESUME_HANDOFF, 통합 전 backup/MVP/워커/모델 메모리. 반복 규칙은 이 파일 또는 링크한 원래 절차에만 둔다.
