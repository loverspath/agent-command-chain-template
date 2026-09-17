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
| **2-내부 Tier 2** | Codex Terra | agy가 호출하는 중난이도 작업의 설계 및 직접 수행 | agy 라우팅 안에서 동작, Sonnet과 직접 통신 안 함 |
| **2-내부 Tier 3** | Sol / Opus | agy 라우팅상 최고난도 문제, 전체 플래닝 상담 | 자문 역할, 구현은 도로 Tier1/2로 위임 |

핵심 단순화: **Sonnet은 agy만 상대한다.** Terra/Sol/Opus는 agy 내부 라우팅
대상이며 Sonnet이 직접 브리지를 걸 필요가 없다 (eraweb-fork 원본의 "Terra는
agy 보고를 정규화해 Sol에게만 전달, agy는 Sol에게 직접 보고 안 함" 규칙을
계층 자체로 끌어올린 것).

## 2. 브리지 설계 — tmux만 사용

두 프로세스(Sonnet CLI, agy CLI)를 **같은 WSL Ubuntu tmux 세션의 서로 다른
윈도우**에 띄우고, 외부 컨트롤러(사람 또는 Sonnet 자신의 Bash 툴)가
`tmux send-keys` / `capture-pane` / `pipe-pane`으로 조작한다.

```
tmux session: agentchain
 ├─ window 0 "sonnet": claude 실행, RC on
 └─ window 1 "agy":    agy CLI 실행
```

- **push 없음.** tmux는 근본적으로 pull이다. 실시간성이 필요하면
  `tmux pipe-pane -o -t agentchain:agy 'cat >> logs/agy.pane.log'` 로 로그
  파일을 만들고, Monitor 툴로 그 파일을 tail — 이게 이 템플릿에서 가장
  근접한 "async 감지" 수단이다.
- **폴백은 엄격하지 않다.** 세션/프로세스가 죽으면 watchdog이 그냥
  재시작한다. eraweb-fork의 리스/하트비트/스티키라우팅/핑퐁방지 같은 상태
  머신은 이 MVP에 없다 — 필요해지면 나중에 추가.

## 3. 파일 구성

| 파일 | 역할 |
|---|---|
| `bootstrap.sh` | 원클릭: tmux 세션 생성 → sonnet/agy 윈도우 기동 → RC on |
| `watchdog.sh` | 세션/프로세스 생존 감시, 죽으면 재시작 (백그라운드 루프) |
| `config.env` | 세션 이름, 모델, agy 실행 커맨드 등 설정값 |
| `state.example.json` | 최소 상태 스키마 (lite 모드 참고, strict 아님) |
| `ROLES.md` | 역할별 프롬프트 템플릿 (agy에게 줄 최초 지시문 등) |

## 4. 빠른 시작

```bash
cd agent-command-chain-template
cp config.env.example config.env   # 필요시 값 수정
./bootstrap.sh                     # tmux 세션 생성 + sonnet/agy 기동 + RC on
./watchdog.sh &                    # 워치독 백그라운드 실행 (선택)
tmux attach -t agentchain          # 직접 들어가서 보고 싶을 때
```

## 5. 알려진 한계 (초안 단계)

- **RC 전제조건**: `claude --remote-control`은 claude.ai **Pro/Max/Team/Enterprise
  구독 + `claude /login` OAuth 로그인**이 사전에 되어 있어야 작동한다.
  API 키 인증만으로는 안 됨. Team/Enterprise는 조직 Owner가
  claude.ai/admin-settings/claude-code에서 RC를 켜둬야 한다. 세션 안에서
  `/remote-control` 을 치면 상태 패널(URL, 연결 상태)을 볼 수 있다.
- agy가 Terra/Sol/Opus를 실제로 어떻게 호출하는지는 agy 자체 구현에 위임 —
  이 템플릿은 그 내부를 건드리지 않는다.
- 인증/쿼터 관리, 헬스 상태머신, 스티키 라우팅은 없음. 필요해지면
  `eraweb-fork/docs/workflows/multi_model_router.md`의 §5를 참고해 확장.
