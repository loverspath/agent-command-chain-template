# 테스트 스위트 가이드 및 격리 규약 (`tests/`)

본 디렉토리는 `agent-command-chain-template` 프로젝트의 자동화 검증 테스트 스위트를 포함합니다.
외부 LLM API 호출 없이 로컬 격리 환경에서 10초 이내에 전건 실행되며, 운영 세션(`agentchain*`) 및 운영 런타임(`runtime/agentchain-v2-*`)에 대해 100% 무간섭 격리를 보장합니다.

---

## 1. 실행 방법 (Execution Instructions)

### 기본 전체 테스트 실행
```bash
./tests/run-all.sh
```
- 모든 표준 하위 테스트 스위트 6종을 순차 실행합니다.
- 총 소요 시간: 약 9초 (3분 이내 완료 요구 충족).
- 전건 통과 시 exit code `0`, 실패 발생 시 exit code `1`.

### 오염 환경 자체 검증 (Dirty Environment Self-Check)
```bash
./tests/run-all.sh --dirty-env-selfcheck
```
- 부모 프로세스에 실제 운영 런타임 경로(`ACC_RUNTIME=$LIVE_RT`), 운영 세션명(`SESSION_NAME=agentchain-v2`), `TMUX` 변수를 강제 주입한 상태로 전체 스위트를 실행합니다.
- 상위 환경이 오염되어 있더라도 하네스의 3중 격리 메커니즘을 통해 모든 테스트가 정상 격리 실행되고 100% 통과함을 입증합니다.

### 실제 agy 엔드투엔드 테스트 (Real LLM API E2E)
```bash
RUN_REAL_AGY=1 ./tests/e2e-resident-real-agy.sh
```
- 실제 Antigravity CLI 및 LLM API를 호출하여 상주 TUI 브리지 5대 시나리오를 전수 검증합니다.
- 비용 및 지연시간 보호를 위해 기본 `run-all.sh`에서는 제외되며, `RUN_REAL_AGY=1` 환경변수 지정 시에만 실행됩니다.
- 임시 HOME, 임시 토큰 심볼릭 링크, 임시 세션으로 100% 격리 구동됩니다.

### 개별 스위트 단독 실행
```bash
./tests/stop-hook-test.sh
./tests/dispatch-regression-test.sh
./tests/watchdog-resident-test.sh
./tests/bootstrap-resident-test.sh
./tests/harness-self-test.sh
./tests/codex-fail-closed-test.sh
```

---

## 2. 격리 원칙 및 5중 가드 (Isolation Rules & Principles)

테스트 실행 중 실제 운영 중인 라이브 에이전트 세션이나 런타임을 오염시키는 사고를 원천 방지하기 위해 다음 원칙을 준수합니다:

1. **클린 실행 환경 (`run_clean`)**:
   - 상위 프로세스의 환경변수를 일체 상속받지 않도록 `env -i PATH="$PATH" HOME="$TEST_HOME" TESTMARK="$TESTMARK" ...` 형태로 격리 실행.
   - 각 테스트 스크립트마다 독립된 임시 디렉토리(`/tmp/acc-t0924-11-*`)와 전용 tmux 세션을 동적 발급.
2. **동적 마커 발급 (`TESTMARK-<random>`)**:
   - 매 테스트마다 암호학적 랜덤 난수를 포함한 고유 마커를 발급하여 페이로드 및 프롬프트에 주입.
3. **Guard Check 1 (안전 경로 단언)**:
   - 대상 런타임 경로가 실제 운영 런타임(`$LIVE_RT`)과 같거나 그 하위 경로인 경우 즉시 `exit 99`로 abort.
4. **Guard Check 2 (실시간 운영 런타임 오염 감지 단언)**:
   - 테스트 시작 전/후에 `$LIVE_RT` 내 `TESTMARK` 검색.
   - **플레인 텍스트**뿐만 아니라 outbox 이벤트의 **Base64 인코딩 필드(`summary_b64=`)** 및 **임의 Base64 토큰**까지 정밀 디코딩하여 1건이라도 검출 시 `exit 99`로 abort.
5. **Guard Check 3 (운영 런타임 시그니처 불변 검증)**:
   - 테스트 시작 시점의 `$LIVE_RT` 상태를 스냅샷하여 테스트 종료 후 대조:
     - (1) `workers/` 파일 목록 및 SHA256 해시
     - (2) 모든 `tasks/*/state` SHA256 해시 (진행 중인 running task는 상태 전이 경고 예외 처리)
     - (3) `events/pending` 및 `events/inflight` 파일 목록
     - (4) `events/archive` 파일 개수
     - (5) `bootstrap_version`, `session_name`, `claude-bridge.settings.json` 해시
   - 불일치 발생 시 즉시 `exit 99`로 실패 처리.
6. **Guard Check 4 (리포지토리 무오염 단언, H1)**:
   - 테스트 시작 전/후에 템플릿 리포지토리(`$HERE`)의 `git status --porcelain --ignored=no` 결과를 비교.
   - E2E나 단위 테스트 수행 중 리포지토리 루트에 `a.txt`, `b.txt` 등 불필요한 파일이 생성되거나 기존 코드가 변조되면 즉시 실패 처리 및 파일명 출력.
7. **Guard Check 5 (실제 Gemini 환경 설정 무오염 단언, H3)**:
   - 테스트 시작 전/후에 실제 사용자 홈 `~/.gemini/config/` 전체(파일 목록+크기+해시; `projects/` 포함) 및 `~/.gemini/antigravity-cli/`의 설정류(`settings.json` 등)를 스냅샷 대조.
   - 불일치 검출 시 즉시 `exit 99`로 실패 처리.
   - 인증 토큰(`antigravity-oauth-token`)은 **존재 여부 및 링크 대상만** 비교하고 크기/해시/mtime은 무시하여 정상적인 OAuth 토큰 자동 갱신을 허용.
8. **정교한 정리 순서 및 프로세스 회수 (H2)**:
   - `cleanup_isolated_env`는 다음 순서를 엄격히 준수:
     (1) 임시 tmux 세션 kill
     (2) 임시 트리를 HOME/cwd/`ACC_RUNTIME`로 점유한 모든 프로세스(`/proc/*/environ`, `/proc/*/cwd` 기준)가 종료될 때까지 대기(최대 10초), 미종료 시 SIGTERM -> SIGKILL 순차 강제 회수
     (3) 런타임/리포/Gemini 설정 시그니처 및 무오염 단언
     (4) 임시 디렉토리 `rm -rf`
     (5) 디렉토리 부재 확인 (잔류 시 재삭제 및 실패 보고)
9. **실제 agy 격리 실행 래퍼 (`run_real_agy_isolated`, H3)**:
   - **원칙**: 측정, 탐색, E2E 등 실제 Antigravity CLI를 호출하는 모든 실행은 **반드시** `lib-isolated-env.sh`의 `run_real_agy_isolated`를 통해서만 수행해야 한다.
   - **가드 조건**: `HOME`이 실제 홈이거나 임시 경로(`/tmp/`) 하위가 아니면 즉시 abort(exit 99).
   - **인증 토큰 보호**: 실제 OAuth 토큰은 임시 HOME에 **심볼릭 링크**로만 연결하며 복사 및 내용 출력을 일체 금지.
   - **사고 이력**: Round 3(T0924-12)에서 F8 옵션 측정을 위해 임시 HOME 없이 실제 agy를 직접 실행하여 `~/.gemini/config/projects/`에 등록 파일 3개가 무단 생성됨. 당시 보고서가 `settings.json`/`hooks.json`만 확인하여 "zero pollution"으로 오판 보고했던 사고를 방지하기 위해 강제됨.
10. **보고서 작성 규칙 (H4)**:
   - 보고서에서 "무접촉 / zero pollution" 표현은 스냅샷 비교 출력(전후 동일 입증 증빙)을 첨부할 때만 사용 가능.

---

## 3. 테스트 스위트 상세 목록 및 검증 항목 (Test Suite Summary)

표준 6개 스위트(총 65개 검증 항목) + 실 LLM E2E 1종(5개 시나리오 + 위생 단언 3종):

| 스위트 파일명 | 항목 수 | 주요 검증 내용 |
|---|:---:|---|
| [`stop-hook-test.sh`](file:///home/rerun/agent-command-chain-template/tests/stop-hook-test.sh) | 15 | Antigravity CLI Stop 훅 fail-closed 동작 검증: fullyIdle=false 대기, resident task 1개 done 이벤트 발행 및 lock/busy 해제, 멱등성 보장, oneshot task 오반응 방지, ACC_RUNTIME 부재 fail-closed, TMUX_PANE 불일치 fail-closed, 서브에이전트 트랜스크립트 필터링, error 페이로드 발행, no busy(/clear) 무동작, broken JSON 방어, marker pane_id 부재/빈값 no-op (Case 11), marker 손상 no-op (Case 12), terminationReason=interrupted 에러 매핑(Case 13), terminationReason=cancelled 에러 매핑(Case 14), ACC_HOOK_DEBUG=1 디버그 로깅(Case 15) |
| [`dispatch-regression-test.sh`](file:///home/rerun/agent-command-chain-template/tests/dispatch-regression-test.sh) | 14 | dispatch.sh 회귀 및 상주 모드 e2e: 세션 해석 우선순위 3단계(env -> marker -> tmux-current), v1 세션 거부(exit 70), oneshot 비-셸 거부(exit 71), resident 비-agy 거부(exit 71), 체인 헤더 주입 및 원본 md5 보존, ACC_NO_BRIEF_HEADER=1 비활성화, 128KB 초과 프롬프트 거부(exit 65), codex 어댑터 --skip-git-repo-check 포함 검증, oneshot 스텁 e2e, resident dispatch 모드/상태 기록, resident task 1 완료, resident task 2 연속 완료(G4c) 및 busy 누수 없음 |
| [`watchdog-resident-test.sh`](file:///home/rerun/agent-command-chain-template/tests/watchdog-resident-test.sh) | 4 | 상주 프로세스 워치독 안전망: Case A(마감시간 초과 stalled 경고), Case B(SIGSTOP 시그널 정지 stalled 감지 및 reaper 오반응 방지), Case C(프로세스 사망 시 process_exit 회수 및 busy 해제), Case D(ACC_RUNTIME 미지정 fail-closed 거부) |
| [`bootstrap-resident-test.sh`](file:///home/rerun/agent-command-chain-template/tests/bootstrap-resident-test.sh) | 6 | bootstrap-v2.sh 상주 경로 및 회귀 방지: (a) workers/agy.resident 생성 및 pane_id 일치, (b) session_name 마커 일치, (c) 스텁 agy /proc/<pid>/environ 내 임시 ACC_RUNTIME 주입 검증(라이브 런타임 미참조), (d) 1줄 재무장 안내문(<= 500바이트) 상주 창 주입, (e) 기본 oneshot 모드 무회귀(마커 및 상주 프로세스 미생성), (f) 운영 세션 agentchain* 무간섭 검증 |
| [`harness-self-test.sh`](file:///home/rerun/agent-command-chain-template/tests/harness-self-test.sh) | 18 | 하네스 자체 검증(가짜 런타임/홈/리포 대상): Case 1(정상 베이스라인 시그니처 대조 통과), Case 2(workers/ 변조 감지), Case 3(비-running task 변조 감지), Case 4(config 변조 감지), Case 5(플레인 텍스트 마커 감지), Case 6(base64 필드 마커 감지), Case 7(임의 base64 토큰 마커 감지), Case 8(running task 상태 전이 경고 예외 처리), Case 9(events 변조 감지), Case 10(H1: 리포 상태 베이스라인 대조), Case 11(H1: 리포 오염 a.txt 감지 및 실패), Case 12(H2: 프로세스 완전 회수 및 디렉토리 완전 삭제), Case 13(H3: Gemini 설정 스냅샷 베이스라인 대조), Case 14(H3: Gemini config 변조 감지), Case 15(H3: Gemini CLI 설정 변조 감지), Case 16(H3: 정상 토큰 갱신 허용), Case 17(H3: 토큰 삭제 감지), Case 18(H3: run_real_agy_isolated fail-closed 가드) |
| [`codex-fail-closed-test.sh`](file:///home/rerun/agent-command-chain-template/tests/codex-fail-closed-test.sh) | 8 | Codex 상주 모드 차단 fail-closed 검증 (Task F7): bootstrap-v2.sh(exit 64), dispatch.sh codex(exit 64), watchdog-v2.sh(exit 64), WORKER_MODE=resident 상속 차단(exit 64), AGY_MODE=resident 및 CODEX_MODE=oneshot 정상 허용 검증 |
| [`e2e-resident-real-agy.sh`](file:///home/rerun/agent-command-chain-template/tests/e2e-resident-real-agy.sh) | 5+3 | 실제 Antigravity CLI 상주 E2E 검증 (`RUN_REAL_AGY=1` 전용): Scenario 1(서브에이전트 위임, 절대경로 a.txt 생성, 1 done 이벤트, pane 일치), Scenario 2(/clear + 1줄 재무장 0건 이벤트), Scenario 3(연속 Task B 완료 및 이벤트 비중복), Scenario 4(C-c 중단 mid-flight 및 fake done 미발행), Scenario 5(sonnet-event-wait.sh [ACC_EVENT_BATCH] 수신 및 event-ack.sh 아카이빙), H1 리포 무오염 검증, H3 실제 Gemini 설정 무오염 검증, RT 라이브 런타임 불변 검증 |
