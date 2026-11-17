# abus v2 시행착오 모음과 원클릭 구성

작성: Claude Sonnet 5.5 (cat-food 호스트 WSL2/MSI, 2026-11-17). 운영 개요는 `abus-v2-operations.md`.

## 원클릭
- Linux/WSL/Termux: `curl`/clone 후 `bash docs/tailhub-termux/bus/abus-join.sh [--label main] [--mode hybrid] [--peers ...] [--dry-run]`
- Windows: `powershell -ExecutionPolicy Bypass -File docs\tailhub-termux\bus\abus-join.ps1 [-Label main] [-Mode hybrid] [-DryRun]`
- 하는 일(멱등): 저장소 clone 또는 ff-only pull(`.git` 없으면 `.old-<시각>`으로 보존 후 clone) → `config.json`에 `urls`(두 허브)·label·peers 병합(`.bak`) → `mode` 파일 → 감독자(Termux `~/.termux/boot`, Windows 로그온 작업 스케줄러) → 기동 → `abus doctor`로 두 허브 확인.
- 허가는 허브 쪽 별개: cat-food `~/.config/agent-bus/allow.txt`에 tailscale 노드명 추가(mother-goose 복제 허브에는 자동 동기화). 외부 클라이언트는 `tokens.json`.
- 코드 갱신: 새 허브/노드 코드를 반영할 때 노드 재시작 전 `abus journal status`로 running 0 확인, 정확한 PID만 종료. 반영 중에는 `--no-auto-update`.

## 시행착오와 귀찮았던 점
| 증상 | 원인 | 해결/예방 |
|---|---|---|
| 새 노드가 `4003 node not allowed` | allow.txt에 tailscale 노드명 없음(z-fold5, codex-environment, lunar-cat 모두 같은 증상) | allow 추가. 접속마다 재로드되므로 허브 재시작 불필요 |
| 응답 없는 `!ask`(Codex Cloud, z-fold5) | 수신 노드 `--peers`에 발신자 없음 / `--mode watch`는 ack만 하고 실행 안 함(저널 state=skipped) | `--mode hybrid`, peers 점검. 저널 상태로 원인 판별 |
| `exec_uncertain` | 노드 재시작이 `!ask` 실행 중에 발생 | 정상 동작(재실행 안 함). 재시작은 running 0 확인 후, 회신을 재시작 전에 보내게 지시 |
| 배포 지시가 노드마다 다름 | 감독자 없는 노드가 있음(msi, Termux), Windows는 작업 스케줄러, hermes는 커스텀 래퍼 | `abus-join`이 환경별로 처리. 감독자 없는 노드의 자기 재시작은 `execv` 방식(fleet 작업) |
| z-fold8 `.git` 없음 | 수동 복사본으로 설치됨 | clone 후 교체 |
| 노드당 체크아웃이 둘(`~/agent-command-chain-template`, `~/act`) | funnel이 별도 경로 사용 | 갱신 대상 경로를 실행 모듈 기준으로 확인, 두 곳 HEAD 기록 |
| funnel이 `urls`를 못 읽음 | 구 shim이 단일 `BUS_URL`만 사용 | `BUS_URLS` 환경변수 + `python -m agent_bus.cli run` |
| `pkill -f`가 자기 셸/감독 루프 종료 | 패턴이 자기 명령줄과 일치 | 정확한 PID만 종료 |
| Windows authorized_keys 쓰기 거부 | ACL 잠금, administrators_authorized_keys는 관리자 권한 필요 | 관리자 셸에서 1회 처리, 에이전트 세션은 불가 |
| z-fold5 Claude OAuth 만료 | 장기 미사용 | `claude auth login`(사용자 1회) |
| 인증/접근 등록을 에이전트가 거절 | 일부 에이전트는 버스 메시지의 소유자 승인 주장을 검증 못 함 | 소유자가 해당 에이전트에 직접 승인 발언, 또는 허용 규칙을 사전 설정 |
| viewer/KB에 신규 기기 미반영 | 등록 절차에 KB·viewer 반영 단계 없음 | `abus-v2-operations.md` 새 기기 절차에 포함(Opus 리드 작업) |
| Funnel 경로 추가 중 mother-goose :443 Funnel이 꺼짐 | `tailscale serve` 변경 부작용 | serve 상태 백업 후 변경, 변경 뒤 funnel 상태 확인 |
| 시계/기기 절전 | Windows 절전, Doze | keep-awake, `termux-wake-lock`, 저널이 재접속 후 재전송 |
| 헤드리스 대기 | Monitor 30분 만료, `pgrep -f` 자기 매칭 | PID 기반 대기, 만료 시 재무장 |

## 정리
- 앞으로 새 기기 = (1) allow 추가 (2) 기기에서 `abus-join` (3) `abus doctor` 확인 (4) `abus-kb record`. 코드 갱신은 fleet 자기 수렴(`abus-fleet-sync`) 반영 후 허브에서 `abus fleet set` 한 번으로 전파.
