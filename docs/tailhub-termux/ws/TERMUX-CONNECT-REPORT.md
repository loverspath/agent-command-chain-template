# Termux → ws 릴레이 접속 결과 (2026-11-06)

Termux(z-fold8)에서 `ws/README.md`대로 세팅하고 접속을 확인한 기록.

## 결과
| 항목 | 결과 |
|---|---|
| 레포 pull | `docs/tailhub-termux/ws/` (`README.md`, `client.py`, `relay.py`) 확인 |
| `pip install websockets` | 이미 설치됨 (17.1, Python 3.14) |
| 실행 | `python3 client.py --name termux` (토큰 없음, 신뢰 IP 인증) |
| 릴레이 응답 | `[relay] connected as termux; peers=['termux']` → **성공** |

## 전달 시도
- 채팅 모드로 접속 확인 메시지를 릴레이에 전송했다.
- 접속 시점 `peers`에 `termux`만 있어 PC(cat-food) 쪽 클라이언트가 없었고, 8초간 회신도 없었다. **PC 도달은 미확인.**
- `--agent` 모드는 사용하지 않았다(수신 메시지가 곧 Claude 프롬프트가 되는 모드).

## 다음 단계
- PC에서 `python3 client.py --name pc` 실행 후 Termux에서 `python3 client.py --name termux --to pc`로 재시도.
- 상시 연결이 필요하면 Termux에서 client를 계속 띄워 둔다(연결은 테스트 후 종료됨).
