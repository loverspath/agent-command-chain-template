# ws — cat-food ↔ Termux 간단 WebSocket 릴레이

PC(cat-food, WSL2)와 Termux(z-fold8)의 Claude가 메시지를 주고받기 위한 최소 릴레이.
Tailscale 주소(100.87.35.41)에만 바인딩, 공유 토큰 인증, 의존성은 `websockets` 하나.

## 파일
| 파일 | 역할 |
|---|---|
| `relay.py` | 서버. cat-food 에서 `ws://100.87.35.41:8765` 로 실행 중 (tmux 세션 `ws-relay`) |
| `client.py` | 클라이언트. 채팅 모드(기본) / `--agent` 모드 |

## 프로토콜 (JSON)
1. 첫 메시지: `{"auth": TOKEN, "name": NAME}` (틀리면 연결 종료)
2. 이후: `{"text": "...", "to": NAME(선택)}` — `to` 없으면 다른 모든 피어에게 전달
3. 수신: `{"from": NAME, "text": "..."}`

## Termux 에서 쓰는 법
```
pip install websockets
cd <repo>/docs/tailhub-termux/ws        # git pull 로 최신 받기
# 폰(z-fold8, 100.94.216.66)은 Tailscale 신뢰 IP 라 토큰 불필요

python3 client.py --name termux                 # 채팅
python3 client.py --name termux --agent         # 받은 메시지를 claude -p (Read/Grep/Glob 읽기전용)로 처리해 회신
python3 client.py --name termux --to pc         # pc 에게만 보내기
```
인증: 소스 IP 가 `WS_TRUSTED_IPS`(기본 100.94.216.66, Tailscale 이 인증한 기기)면 토큰 없이 허용, 그 외는 PC 의 `~/.config/ws-relay/token` 필요. **레포·문서·채팅에 토큰을 적지 않는다.**

## 주의
- `--agent` 는 수신 메시지가 곧 Claude 프롬프트다. 토큰을 아는 상대만 접속할 수 있으므로 토큰 유출에 주의.
- 서버(tmux `ws-relay`)는 PC 재부팅 시 사라진다. 자동 기동은 아직 없음.
- 연결 테스트: `client.py` 로 접속해 `connected as termux; peers=[...]` 응답이 오면 성공.
