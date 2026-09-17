# Agent Command Chain — Template (MVP)

간소화된 4계층 멀티 에이전트 명령계통의 초안 템플릿. `eraweb-fork`의 실전 구조
(Sol/Terra/agy 3역할 + Sonnet/Terra/Sol-Opus 3티어 라우터, `docs/workflows/`)를
관찰한 뒤, 더 가볍게 재구성한 것이다. **깊은 하네스(SQLite 상태머신, 헬스체크
상태기계, 엄격한 폴백 규약) 없이** tmux 기반 브리지 + 셸 스크립트만으로 동작한다.

## 1. 역할 구조 (4계층)

| 계층 | 주체 | 역할 | 비고 |
|---|---|---|---|
| **0. 사용자** | 사람 | 최종 의사결정, 승인 | RC로 Sonnet 세션에 원격 개입 |
| **1. 감독/디스패치** | Claude Sonnet | 모니터링·감사, 사용자 명령 하달, 루프 관리, RC(원격제어) | 항상 사람과 가장 가까운 지점 |
| **2. 라우터 겸 워커** | agy (Gemini 3.8 Flash) | 난이도 판단(hard routing) 및 서브에이전트 호출 | Tier 2/3 호출 여부를 agy 스스로 결정 |
| **2-내부 Tier 2** | Codex Terra | 중난이도 작업의 설계 및 직접 수행 | 별도 tmux 창(§2). agy가 자동 호출하진 않음 — 아래 "한계" 참고 |
| **2-내부 Tier 3** | Sol / Opus | 최고난도 문제, 전체 플래닝 상담 | 상시 창 없음. 필요할 때 codex 창의 커맨드를 `--model gpt-5.6-sol`로 바꿔 send-keys, 또는 claude 쪽은 `--model opus`로 즉석 실행 |

핵심 단순화: **Sonnet은 agy와 codex(Terra) 두 창만 직접 본다.** Sol/Opus는
상시 프로세스가 아니라 필요할 때만 커맨드를 바꿔 일회성으로 부르는 대상
(eraweb-fork 원본의 "Terra는 agy 보고를 정규화해 Sol에게만 전달, agy는
Sol에게 직접 보고 안 함" 규칙을 계층 자체로 끌어올린 것).

## 2. 브리지 설계 — tmux만 사용

세 프로세스(Sonnet CLI, agy CLI, Codex CLI)를 **같은 WSL Ubuntu tmux 세션의
서로 다른 윈도우**에 띄우고, 외부 컨트롤러(사람 또는 Sonnet 자신의 Bash 툴)가
`tmux send-keys` / `capture-pane` / `pipe-pane`으로 조작한다.

```
tmux session: agentchain
 ├─ window 0 "sonnet": claude 실행, RC on
 ├─ window 1 "agy":    agy CLI 실행 (대화형, 지속)
 └─ window 2 "codex":  codex --model gpt-5.6-terra (대화형, 지속 — `codex exec`는 one-shot이라 안 씀)
```

- **push 없음.** tmux는 근본적으로 pull이다. 실시간성이 필요하면
  `tmux pipe-pane -o -t agentchain:agy 'cat >> logs/agy.pane.log'` 로 로그
  파일을 만들고, Monitor 툴로 그 파일을 tail — 이게 이 템플릿에서 가장
  근접한 "async 감지" 수단이다.
- **폴백은 엄격하지 않다.** 세션/프로세스가 죽으면 watchdog이 그냥
  재시작한다. eraweb-fork의 리스/하트비트/스티키라우팅/핑퐁방지 같은 상태
  머신은 이 MVP에 없다 — 필요해지면 나중에 추가.
- **agy ↔ codex 연결은 이 템플릿 밖의 일이다.** eraweb-fork에서 agy가
  Terra/Sol을 실제로 호출하는 로직은 `tools/router/src/adapters/*.ts`(Node
  프로세스 spawn)에 있었고, 이 MVP는 그 라우터 자체를 빼기로 했다. 그래서
  codex 창은 agy와 자동 연동되지 않는다 — Sonnet(또는 사람)이 agy 창의
  출력을 보고 필요하다고 판단하면 codex 창에 직접 send-keys 로 작업을
  넘기는 구조다. 진짜 "agy가 알아서 codex를 부르는" 자동화가 필요해지면
  eraweb-fork의 라우터 어댑터를 참고해서 별도로 구현해야 한다.

## 3. 파일 구성

| 파일 | 역할 |
|---|---|
| `bootstrap.sh` | 원클릭: tmux 세션 생성 → sonnet/agy/codex 윈도우 기동 → RC on |
| `watchdog.sh` | 세션/프로세스 생존 감시, 죽으면 재시작 (백그라운드 루프) |
| `config.env` | 세션 이름, 모델, agy/codex 실행 커맨드 등 설정값 |
| `state.example.json` | 최소 상태 스키마 (lite 모드 참고, strict 아님) |
| `ROLES.md` | 역할별 프롬프트 템플릿 (agy/codex에게 줄 최초 지시문 등) |

## 4. 빠른 시작

```bash
cd agent-command-chain-template
cp config.env.example config.env   # 필요시 값 수정
./bootstrap.sh                     # tmux 세션 생성 + sonnet/agy/codex 기동 + RC on
./watchdog.sh &                    # 워치독 백그라운드 실행 (선택)
tmux attach -t agentchain          # 직접 들어가서 보고 싶을 때
```

## 5. 알려진 한계 (초안 단계)

- **RC 전제조건**: `claude --remote-control`은 claude.ai **Pro/Max/Team/Enterprise
  구독 + `claude /login` OAuth 로그인**이 사전에 되어 있어야 작동한다.
  API 키 인증만으로는 안 됨. Team/Enterprise는 조직 Owner가
  claude.ai/admin-settings/claude-code에서 RC를 켜둬야 한다. 세션 안에서
  `/remote-control` 을 치면 상태 패널(URL, 연결 상태)을 볼 수 있다.
- **agy → codex 자동 호출은 없다** (위 §2 마지막 항목). Sonnet이 사람 대신
  그 연결을 수동으로 메꾸는 구조다.
- **agy가 Windows 전용 바이너리일 수 있다.** 2026-09 실측 기준 Antigravity
  CLI는 Linux 빌드가 없어서, WSL에서 쓰려면 `config.env.example`에 적힌
  대로 `cmd.exe /c "cd /d C:\...\ && agy.exe ..."` 우회가 필요했다. agy가
  네이티브 Linux 빌드로 나오면 이 우회는 필요 없어진다.
- 인증/쿼터 관리, 헬스 상태머신, 스티키 라우팅은 없음. 필요해지면
  `eraweb-fork/docs/workflows/multi_model_router.md`의 §5를 참고해 확장.
