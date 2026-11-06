# Tailhub (`th` / `ts`) — Termux 측 허브와 catfood 복원 경로

Termux(폰)에서 Tailscale 노드(catfood = WSL2 Ubuntu 등)에 접속하고,
원격 agent-chain 세션을 조회·복구하는 구조 메모.


> **현행**: 에이전트 간 통신은 [`bus/`](bus/README.md) (Tailnet Agent Bus). `ws/` 는 초기 릴레이로 폐기 대상.

## 구성
| 항목 | 위치 | 비고 |
|---|---|---|
| `tailhub` | `~/.local/bin/tailhub` | Python3(표준 라이브러리) curses TUI + CLI |
| `ts` | `~/.local/bin/ts` | `exec tailhub ts "$@"` 래퍼 |
| `th`, `tsh` | `~/.bashrc` alias | `alias th="tailhub"` (대화형 셸 전용) |
| 호스트 목록 | `~/.config/tailhub/hosts.json` | `~/.ssh/config`와 동기화 |

비대화형 셸에서는 alias가 없으므로 `tailhub ...`로 직접 호출한다.

## 서브커맨드
| 명령 | 동작 |
|---|---|
| `th` | TUI (`[t]` = Remote TS 뷰) |
| `th <이름\|번호>` | 해당 호스트로 바로 ssh |
| `th add <name> <host> [-u] [-p] [-d] [-t] [--ssh-config]` | 호스트 추가 |
| `th rm <name>` | 호스트 삭제 |
| `th ts <host> [list\|ls\|recover\|rc] [세션=all] [--json]` | 원격 세션 조회(기본)/복구 |
| `th exec <host> "<cmd>" [--json]` | 원격 명령 실행 |
| `-l/--list`, `-s/--sync`, `--exec` | 목록 / ssh config 동기화 / TUI 복귀 없이 exec |

## 원격 호출 구조
스니펫을 SSH stdin으로 흘려 보내 원격 `python3`로 실행한다.

```
# th ts <host> list
ssh -o BatchMode=yes -o ConnectTimeout=5 <user>@<host> "python3 -"            # stdin = REMOTE_TS_SNIPPET
# th ts <host> recover <세션|all>
ssh -o BatchMode=yes -o ConnectTimeout=5 <user>@<host> "python3 - <세션|all>"  # stdin = REMOTE_RECOVER_SNIPPET
# th exec <host> "<cmd>"
ssh -o BatchMode=yes -o ConnectTimeout=5 <user>@<host> "<cmd>"
```

### 원격 recover 동작
1. `~/agent-command-chain-template/bin/chain-registry.sh json`(없으면 `~/bin/chain-registry.sh`)에서 세션의 `recover_script` 탐색
2. 없으면 `~/agent-command-chain-template/bin/chain-recover.sh`(없으면 `~/bin/chain-recover.sh`) 사용
3. `<script> <세션>` 실행(타임아웃 60초) 후 갱신된 `remote_control_url`을 JSON으로 반환
4. 스크립트가 없으면 exit code 127

## catfood 복원
```
th ts catfood list              # 세션 상태 확인
th ts catfood recover all       # 전체 복구 (또는 recover <세션명>)
tailhub ts catfood recover all  # 비대화형 셸
ssh catfood                     # 단순 접속
```
전제: catfood 쪽에 이 레포(`bin/chain-recover.sh`, `bin/chain-registry.sh`)가 있어야 한다.

## 폰으로 역접속(PC → Termux) 체크리스트
- Termux `sshd` 기동(기본 포트 8022), `whoami`로 사용자명 확인
- `~/.ssh/authorized_keys`에 PC 공개키 등록
