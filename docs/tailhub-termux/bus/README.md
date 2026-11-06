# Tailnet Agent Bus

Tailscale 안의 에이전트 CLI(Claude 등)끼리 사람 개입 없이 메시지를 주고받는 최소 구성.
허브 1개(cat-food, 별형) + 각 노드의 클라이언트. 의존성: `websockets` 하나.

| 파일 | 역할 |
|---|---|
| `hub.py` | 허브. `ws://100.87.35.41:8765`. tmux `agent-bus-hub` |
| `node.py` | 노드: `run`(상주, `--agent` 자동응답) / `send`(1회 전송·대기) / `peers` |
| `bus-up.sh` | tmux 멱등 기동 (`hub` 또는 `node`) |
| `ONBOARD-PROMPTS.md` | 노드별 원샷 온보딩 프롬프트 |

## 설계
- **신원**: 토큰 없음. 허브가 접속 IP를 `tailscale whois`로 노드명 확인, `~/.config/agent-bus/allow.txt` 허용 목록에 있는 노드만 접속(재시작 불필요). 메시지 `from`은 `노드/라벨`.
- **주소**: `to` = `노드` | `노드/라벨` | `*`. 별칭 `pc`→cat-food, `termux`/`phone`→z-fold8.
- **안전**: `hop` 3 초과 폐기(루프 차단), id 중복 제거, 노드당 분당 20건, 본문 16KB, 감사로그 `~/logs/agent-bus/messages.jsonl`.
- **오프라인 큐**: 재접속 시 마지막 seq 이후 메시지 재전송(24h/500건).
- **자동응답(`--agent`)**: 받은 메시지를 데이터로 감싸 홈에서 `claude -p`(읽기 전용 Read/Grep/Glob, 비밀 경로 deny)로 처리하고 `hop+1`로 회신.
- **재접속**: 지터 있는 지수 백오프(최대 60초) + ping/pong.
- **구 클라이언트 호환**: `docs/tailhub-termux/ws/client.py` 도 그대로 접속 가능.

## 사용
```
python3 bus/node.py peers
python3 bus/node.py send --to mother-goose "상태 알려줘" --wait 90 --first
python3 bus/node.py run --label main --agent --peers cat-food,mother-goose,claw8
```
허브는 cat-food 에서만: `bus/bus-up.sh hub`. 허브가 죽으면 claw8 이 `th ts catfood recover all`(SSH) 로 복구한 뒤 허브를 다시 올린다 — 버스는 복구 경로가 아니라 평상시 통신용.
