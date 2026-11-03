# 역할 진입점과 워커 보고 계약

감독자 운영·경중 라우팅·지시서 작성·공통 실행 원칙은 [SUPERVISOR.md](SUPERVISOR.md) 한 곳에 둔다. 감독자·리드·워커는 그 문서와 선택한 프로젝트 지침을 읽고 적용한다. 프로젝트 선택은 [문서 3층 참조](docs/INSTRUCTION-LAYERS.md)를 따른다.

## agy 메인 에이전트

agy 메인은 조사·구현·테스트 실행·`git add/commit/push`를 포함한 **모든 실작업을 서브에이전트(`invoke_subagent`)에 맡기고**, 자신은 지시·결과 취합·보고만 한다. 작업이 끝나는 시점의 git 정리·push, 테스트 실행도 서브에이전트가 한다. 메인이 직접 작업하면 그동안 응답 불가(busy)가 된다. 감독자는 `bin/agy-main-audit.sh <task-id> [로그] [다음-task-id]`로 한 작업 구간의 서브에이전트 호출 수와 메인 직접 작업을 확인할 수 있고, 직접 작업이 보이면 교정 메시지를 보낸다.

## 실행과 이벤트

기본은 v2 Full-Push, v1은 레거시 폴백이다. oneshot/resident는 실행 방식이며 역할 변경이 아니다. v2 디스패치·ACK 명령은 [.llmwiki/usage.md](.llmwiki/usage.md)에 둔다. 반복 화면 폴링 대신 이벤트를 기다린다. 상태 문의·장애 진단의 단발 화면 확인만 필요할 때 사용한다.

Outbox·완료 판정·2층 감사·복구·병렬 작업 공간 격리의 계약은 [구조 원칙 §2](docs/PRINCIPLES-structure-vs-implementation.md)에만 둔다. 구현 규격은 [v2.3 구현 설계](docs/IMPL-DESIGN-20261102.md)와 코드가 근거다. 워커 슬롯·모델 이름으로 리드 역할을 추정하지 않는다.

## 공통 보고 규약

`knowledge_refs`는 이 문서와 실제 작업 자료의 경로 참조다. 절차 본문을 지시서에 복사하지 않는다. 최종 응답에 `worker_id`, `files_changed`, `checks_run`(실행한 확인과 근거), `refs_loaded`(읽은 경로), `learning_capsule`(unexpected/cause/reusable, 없으면 none)을 짧게 남긴다. 별도 보고 파일은 작업이 요구할 때만 만든다.

마지막 줄은 다음 중 하나다. 센티널은 중간 응답·서브에이전트 지시에 넣지 않는다. agy의 자동 규약 D는 하네스 보조이며, codex를 포함한 워커의 보고 규칙은 이 절이 정본이다.

```text
[[DONE <task_id>]] result=<요약> | learning=<한줄|none>
[[BLOCKED <task_id>]] reason=<사유>
```

`ACK`는 수신 보고이며 완료 신호가 아니다. 사유 코드의 허용 집합은 [lib/reason.sh](lib/reason.sh), 이벤트의 기록·소비는 구조 계약 §2.3을 참조한다. 지시서에 보고 필드를 매번 재기술할 필요 없이 “ROLES.md를 읽고 적용” 한 줄로 전달한다.
