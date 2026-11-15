# Tailnet Agent Bus (`abus`)

크로스-환경(Linux/WSL2, Windows, Termux, Tailscale Funnel 외부 클라이언트) 에이전트 간 **동기 RPC + 비동기 내구성 메일박스** 통신 버스.
구현 코어는 레포 루트의 `bus/` 패키지(`abus` CLI)에 있으며, 이 디렉터리의 `hub.py`·`node.py` 는 기존 워치독·스케줄드 태스크와 100% 호환되는 경량 심(shim)입니다.

> 운영 현황·복구 절차는 비공개 레포 `loverspath/agent-docs` 의 `tailnet-bus/OPERATIONS.md` 를 참조합니다.

## 아키텍처 (Phase 1–3)
- **패키지(`bus/agent_bus/`)**:
  - `store.py`: stdlib SQLite WAL(`~/.local/share/agent-bus/bus.db`) 기반 메시지 영속화, 리스(lease) 기반 최소 1회 전달 + 실행 후 ACK, `corr_id` RPC 결과 보존(`abus result`), 재전송 논스 캐시.
  - `hub.py`: 내부 Tailnet WebSocket 허브(`BUS_HOST:BUS_PORT`, 기본 `:8765`) + Dual-Auth 게이트웨이(`127.0.0.1:8766`). 내부 메시지는 `~/logs/agent-bus/messages.jsonl` 에 기록되어 `bus-sync.py` 와 호환됩니다.
  - `gateway.py`: Tailscale Funnel 뒤에서 동작하는 HTTP+WS 게이트웨이(`/bus/v1/send`, `/bus/v1/poll`, `/bus/v1/ack`, `/bus/v1/rpc/{corr_id}`, `/bus/v1/peers`, `/bus/v1/health`, `/bus/v1/ws`).
  - `auth.py`: Dual-Auth — Tailnet 내부 피어는 `tailscale whois` + `~/.config/agent-bus/allow.txt`, 외부/Funnel 요청(`Tailscale-Funnel-Request` 또는 비-Tailnet IP)은 `~/.config/agent-bus/tokens.json` 의 **Bearer 토큰 + HMAC-SHA256 서명(`±300s` 재생 방지 + 논스 중복 차단) + 네임스페이스 격리(`ext-*`, `*` 브로드캐스트 차단, `allow_exec` 가드)** 적용.
  - `bridge.py`: `<host>/inbox` 또는 `<host>/<session>` 수신 시 `~/.config/agent-bus/inbox.jsonl` & `inbox/*.md` 적재 및 ACC `bin/event-emit.sh external done` (`[ACC_EVENT_BATCH]`) 푸시 웨이크업.
  - `client.py` / `cli.py`: `abus` CLI 및 `node.py` 호환 레이어(에페메랄 `cli-<uuid6>` 라벨, 실행 완료 후 커서/ACK 커밋, `outbox.jsonl` 오프라인 큐, `claude -p` 인증/실행 오류 감지, Windows 로그 핸들 잠금 없는 `abus supervise`).

## 빠른 온보딩 (2커맨드)
```bash
git clone https://github.com/loverspath/agent-command-chain-template ~/act && python3 -m pip install -e ~/act/bus
abus init --url ws://<HUB_HOST>:8765 && abus doctor
```
*(외부/Funnel 클라이언트: `abus init --url https://<FUNNEL_HOST>:10000/bus --token-id <TOKEN_ID> --secret <SECRET>`)*

## 주요 명령
```bash
abus peers
abus send --to mother-goose "상태 알려줘" --wait 120          # corr_id 동기 RPC
abus result <corr_id> --wait 60                               # 타임아웃 후 지연 응답 조회
abus send --to cat-food/cattin01 --say "빌드 완료"            # ACC 세션 inbox 브리지 알림
abus inbox && abus ack all                                    # 로컬 인박스 조회 및 확인
abus run --label main --peers cat-food,msi,mother-goose,z-fold8
abus supervise --label main --peers cat-food,msi,mother-goose,z-fold8
abus hub                                                      # 허브(:8765) + 게이트웨이(:8766)
abus doctor --json                                            # 런타임/허브/커서/Claude/gh 점검
abus token create --node ext-runner --scope "send:cat-food/inbox" --scope "poll:ext-runner"
```

## 응답 모드 (`agent | watch | hybrid`, 기본 `hybrid`)
- `~/.config/agent-bus/mode` (`BUS_MODE_FILE`) 를 메시지마다 읽습니다(재시작 불필요).
- `!ask` 접두가 있는 요청 또는 `agent` 모드의 메시지만 `claude -p --allowedTools Read,Grep,Glob,Bash,Edit,Write` 로 실행합니다.
- 실행 종료코드·인증 만료를 검증해 `status` (`ok | exec_error | auth_error`) 를 반환합니다.

## 코드 배포 (`!update`)
1. 변경사항을 푸시한 뒤 `abus send --to "*" --update` (또는 `node.py send --to "*" --update`) 실행.
2. 각 노드는 `git pull --ff-only` 로 HEAD 변경 시 POSIX 에서는 `os.execv`, Windows 에서는 `sys.exit(0)`(또는 `abus supervise` 하위 `42`) 로 로그 파일 잠금 없이 안전 재시작합니다.
