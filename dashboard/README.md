# ACC Dashboard Server

agent-command-chain-template(ACC) 아키텍처의 실시간 상태를 모니터링하는 경량 대시보드 서버입니다.
Python 3 표준 라이브러리만을 사용하여 동작하며 디스크에 어떠한 파일도 쓰지 않는 완전 읽기 전용 서버입니다.

## 실행 방법 (Usage)
- 시작: `./run.sh start [--port 8765] [--host <ip>] [--runtime <dir>] [--transcript-root <dir>]`
- 정지: `./run.sh stop`
- 상태 확인: `./run.sh status`
- 재시작: `./run.sh restart`

### 환경 변수 (Environment Variables)
- `DASHBOARD_STATE_DIR`: PID 파일(`.pid`) 및 로그 파일(`dashboard.log`) 저장 디렉터리 (기본값: 미설정 시 `$SCRIPT_DIR/.pid`, `$REPO_DIR/logs/dashboard.log`). 테스트 격리 및 다중 인스턴스 실행 시 라이브 프로세스 간섭 방지를 위해 사용.
- `ACC_DASHBOARD_PID_FILE`: PID 파일 위치 개별 지정.
- `ACC_DASHBOARD_LOG_FILE`: 로그 파일 위치 개별 지정.
- `ACC_RUNTIME`: 모니터링할 런타임 디렉터리 지정.
- `ACC_TRANSCRIPT_ROOT`: 트랜스크립트 루트 디렉터리 지정 (기본값: `~/.gemini/antigravity-cli`).

## 엔드포인트 (Endpoints)

### 기존 엔드포인트
- `GET /`, `GET /index.html`: 대시보드 프론트엔드 웹페이지 서빙 (`Cache-Control: no-cache`).
- `GET /healthz`: 서버 헬스체크 (`{"status": "ok"}`).
- `GET /api/state`: 실시간 세션, 워커, 데몬, 작업(최근 30개), 이벤트(최근 40개) 집합 상태 JSON (1.5초 인메모리 캐시).

### 신규 상세 엔드포인트
- `GET /api/task/<id>`:
  - `<id>` 파라미터는 `^[A-Za-z0-9._-]{1,80}$` 정규식으로 엄격히 검증되며, 경로 순회(`..`, `/`, `\`) 시 `400 Bad Request` 반환.
  - 해당 작업 디렉터리가 없을 경우 `404 Not Found` 반환.
  - 응답 JSON 구조:
    ```json
    {
      "meta": {
        "id": "<id>",
        "worker": "agy|codex",
        "mode": "resident|oneshot",
        "status": "running|done|error|...",
        "started_at": 1727244000,
        "finished_at": 1727244100,
        "duration_s": 100,
        "exit_code": 0,
        "deadline_at": 1727245800
      },
      "prompt_title": "...",
      "output_tail": "...",
      "timeline": [
        {
          "step_index": 1,
          "timestamp": "2026-09-25T05:00:00Z",
          "type": "model|tool_call|subagent|system",
          "summary": "..."
        }
      ],
      "events": [
        {
          "id": "...",
          "kind": "...",
          "created_epoch": 1727244000,
          "summary": "..."
        }
      ],
      "linked": "exact|partial|inferred|none",
      "truncated_before": false
    }
    ```
  - **프롬프트 제목 (`prompt_title`)**: `[CHAIN CONTEXT]...---` 및 YAML 메타데이터 블록을 건너뛰고 첫 번째 헤딩(`#`, `##`) 또는 첫 번째 의미 있는 텍스트 줄만 추출 (최대 200자, 마스킹 적용).
  - **로그 꼬리 (`output_tail`)**: `output.log`의 마지막 8KB 추출 (ANSI 이스케이프 시퀀스 제거, 마스킹 적용).
  - **작업 단위 슬라이싱 및 타임라인 (`timeline`)**:
    - 상주(resident) 워커는 모든 작업을 하나의 지속 트랜스크립트에서 처리하므로, 타 작업 스텝 혼입을 방지하는 정밀 작업 단위 슬라이싱(Task Slicing)을 수행합니다.
    - 트랜스크립트 파일 후미 최대 2MB(`seek_offset = max(0, file_size - 2MB)`)를 탐색합니다.
    - **구간 시작 표지 (`start_idx`)**: 스텝 내용 또는 도구 호출 인자에 `tasks/<task_id>/prompt.md`가 포함된 첫 번째 스텝(주로 `Read and execute task prompt: ...` 형식의 `USER_INPUT` 스텝).
    - **구간 종료 표지 (`end_idx`)**: `start_idx` 이후 다른 작업의 프롬프트 표지(`tasks/<other_id>/prompt.md`, `<other_id> != task_id`)가 포함된 후속 사용자 입력 스텝 직전 또는 파일 끝.
    - **슬라이스 추출**: 슬라이스된 구간 내에서 최근 최대 60스텝 추출 (`slice_steps[-60:]`). 트랜스크립트 원본 `step_index` 보존.
    - **노이즈 제거**:
      - 메타 접두 줄 제거: 스텝 내용/요약에서 `Created At:`, `Completed At:`, `File Path:`, `Step Id:`, `Total Lines:`, `Total Bytes:`, `Showing lines`로 시작하는 선행 줄 자동 제거.
      - 도구 호출 포맷: `{tool_name}: {compact_args}` 형태로 간결화하며, 경로 인자(`AbsolutePath`, `TargetFile`, `path` 등)는 `.../<parent>/<basename>` (예: `.../dashboard/server.py`) 형태로 축약.
      - 빈 모델 응답 생략: 본문/thinking이 비어있고 도구 호출이 없는 모델 스텝은 생략.
      - 서브에이전트 위임: `invoke_subagent`, `send_message` 등 서브에이전트 위임 및 송수신 메시지는 `type: "subagent"`로 분류.
    - **단일 지점 마스킹**: 모든 스텝 요약은 절단(최대 300자) 전에 반드시 `mask_sensitive`를 선행 적용하여 토큰 절단 누출 방지.
  - **연결 방식 (`linked`) 및 이전 스텝 생략 플래그 (`truncated_before`)**:
    - `exact`: 트랜스크립트에서 해당 작업의 시작 표지(`tasks/<task_id>/prompt.md`)를 정확히 찾아 해당 작업 구간만 슬라이싱함.
    - `partial`: 2MB 탐색 창 밖에서 작업이 시작되어 시작 표지를 찾지 못했으나, 작업 시작 시각(`started_at`)이 탐색 창 내 최초 스텝 시각 이전이어서 창 내의 유효 구간을 반환함 (`truncated_before: true`).
    - `inferred`: 트랜스크립트 파일은 식별되었으나 해당 작업의 시작 표지를 찾지 못함. 타 작업의 스텝 혼입을 엄격히 방지하기 위해 `timeline: []` (빈 배열) 반환 (`truncated_before: false`).
    - `none`: 유효한 트랜스크립트가 없거나 화이트리스트 검증 실패 시 `timeline: []` 반환 (`truncated_before: false`).
    - `truncated_before`: 2MB 탐색 창 절단으로 인해 작업의 이전 스텝이 생략되었는지 여부를 나타내는 불리언 값 (`exact` 연결 시 `seek_offset > 0 and start_idx == 0`인 경우, 또는 `partial` 연결인 경우 `true`).
  - 성능 및 크기 제한: 2초 인메모리 캐시, 응답 전체 크기 200KB 이하 엄격 제한.

- `GET /api/event/<id>`:
  - `<id>` 파라미터는 `^[A-Za-z0-9._-]{1,80}$` 정규식으로 엄격히 검증되며, 경로 순회 시 `400 Bad Request` 반환.
  - `runtime/events/` 내 `pending`, `inflight`, `archive` 디렉터리에서 해당 이벤트를 검색, 없으면 `404 Not Found` 반환.
  - 응답 JSON 구조:
    ```json
    {
      "id": "<id>",
      "kind": "done|error|notice|...",
      "source": "agy|codex|tester",
      "created_epoch": 1727244000,
      "exit_code": 0,
      "task_id": "...",
      "summary": "..."
    }
    ```
  - 이벤트 요약(`summary`)은 제어문자를 공백 치환 후 단일 지점 마스킹 적용 (최대 500자).
  - 2초 인메모리 캐시 적용.

## 보안 및 격리 원칙 (Security & Isolation)

1. **완전 읽기 전용 (Strictly Read-Only)**:
   - 서버는 디스크에 어떠한 파일(임시파일, 캐시파일, 로그 등)도 생성하거나 수정하지 않습니다. 모든 캐시는 메모리 상에서만 관리됩니다.
2. **경로 조작 방어 및 트랜스크립트 허용 목록 (Whitelist Validation)**:
   - 클라이언트 파라미터나 이벤트 파일 내의 임의 경로(`detail_path`)를 그대로 신뢰하지 않습니다.
   - 트랜스크립트 파일을 열기 전에 반드시 `os.path.realpath`로 정규화하여 다음 조건을 모두 만족해야만 접근을 허용합니다:
     1. 정규화된 경로가 `os.path.realpath(transcript_root) + "/brain/"` 하위에 위치.
     2. 정규화된 경로에 `/.system_generated/logs/` 세그먼트 포함.
     3. 파일명이 정규식 `^transcript.*\.jsonl$`과 일치.
     4. 실제 존재하는 일반 파일일 것.
   - 허용 목록을 벗어나거나 심볼릭 링크를 통해 루트 외부(`/etc/passwd` 등)로 탈출하려는 시도는 즉시 거부되며 `linked: "none"`으로 처리되고 어떠한 파일 내용도 노출되지 않습니다.
3. **단일 지점 마스킹 (Single-point Masking)**:
   - 모든 노출 텍스트(`output_tail`, `prompt_title`, 트랜스크립트 스텝 요약, 이벤트 요약)는 단일 마스킹 함수 `mask_sensitive()`를 거칩니다.
   - **중요**: 마스킹은 절단(truncation) 경계에서 토큰이 잘려 노출되는 사고를 방지하기 위해 **반드시 절단 전에 먼저 수행**됩니다.
   - 마스킹 대상 패턴 (`***`로 치환):
     - GitHub 토큰: `gho_...`, `ghp_...`, `github_pat_...`
     - OpenAI 키: `sk-...`
     - Google API 키: `AIza...`
     - Slack 토큰: `xox[baprs]-...`
     - 헤더: `Bearer <토큰>`, `Authorization: ...`
     - JWT 토큰: `eyJ...`
     - 비밀키 블록: `-----BEGIN ... PRIVATE KEY----- ... -----END ... PRIVATE KEY-----`
     - 설정 대입문: `KEY=...`, `TOKEN=...`, `SECRET=...`, `PASSWORD=...`, `API_KEY=...` (대소문자 무관)
     - OAuth 경로 및 토큰: `oauth`를 포함하는 URL/경로/파라미터/토큰 문자열
     - Hex 블록: 40자 이상의 16진수 문자열
     - Base64 블록: 40자 이상의 base64 문자열
4. **프로세스 관리 안전성 (`run.sh`)**:
   - `DASHBOARD_STATE_DIR`를 통해 테스트 및 개발 환경에서 라이브 PID 파일 및 로그 파일을 완전히 분리할 수 있습니다.
   - `stop` 명령 수행 시 PID 파일에 기록된 PID를 즉시 죽이지 않고, `/proc/$PID/cmdline`을 검사하여 `dashboard/server.py`가 실제로 실행 중인 경우에만 종료 신호(SIGTERM/SIGKILL)를 전송합니다. 다른 프로세스인 경우 경고를 출력하고 PID 파일만 정리하여 오작동 및 타 프로세스 오폭을 방지합니다.
