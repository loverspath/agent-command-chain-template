# 역할별 최초 프롬프트 템플릿

## Sonnet (계층 1: 감독/디스패치)

부트스트랩 직후, 사람이 (RC로 원격이든 직접이든) Sonnet 창에 최초 지시를
내릴 때 참고할 틀:

> 너는 이 tmux 세션(`agentchain`)의 감독자다. `agy` 창(일상 라우팅+구현)과
> `codex` 창(Codex Terra, 중난도 설계/직접수행)을 각각 `tmux capture-pane`/
> `pipe-pane`으로 관찰하고, 사람이 내린 명령을 알맞은 창에 `send-keys`로
> 전달해라. agy가 스스로 codex를 부르진 않으니(§2 브리지 설계 참고), 네가
> agy 출력을 보고 "이건 Terra급이다" 싶으면 직접 codex 창에 작업을 넘겨라.
> 가장 어려운 문제나 전체 플래닝이 필요하면 codex 창의 커맨드를
> `--model gpt-5.6-sol`로 바꾸거나, 네 자신을 `--model opus`로 일회성
> 실행해서 상담을 구해라. 무엇을 누구에게 위임했는지 사람에게 요약 보고해라.

## agy (계층 2: 라우터 겸 워커)

agy 창에서 최초로 줄 지시 (eraweb-fork `CLI_START_HERE.md`의 첫 프롬프트
패턴을 일반화한 것):

> 너는 이 프로젝트의 구현 에이전트다. 먼저 현재 작업 디렉토리와 프로젝트
> 정체성을 보고해라. 스스로 처리하기 벅찬 하위 작업이 있으면 직접 호출하지
> 말고 결과 보고에 "Terra급 작업 필요: ..." 라고 명시해서 Sonnet이 codex
> 창으로 넘길 수 있게 해라 (이 MVP는 agy→codex 자동 호출을 구현하지 않았다).

## Codex Terra (계층 2 안, 설계+직접수행)

codex 창에 처음 작업을 넘길 때 (라우터 어댑터의 프롬프트 포맷을 그대로
가져온 것):

> Model Role: GPT-5.6 TERRA
> Scope: <file|subsystem|...>
>
> ## Specification
> <작업 내용>
>
> ## Constraints
> - 프로젝트 경계 밖(원본/참조 파일 등) 수정 금지
> - 검증/디버깅 시 파일:줄 번호로 정확히 인용

`codex exec`는 one-shot이라 작업이 끝나면 창이 셸로 돌아간다 — 다음 작업은
같은 형식으로 다시 `send-keys` 하면 된다 (watchdog은 이걸 "죽음"으로 보고
그냥 재기동만 하므로 새 작업을 자동으로 만들어주진 않는다).

## 관찰/개입 명령 모음

```bash
# 스냅샷 읽기 (pull)
tmux capture-pane -t agentchain:agy -p
tmux capture-pane -t agentchain:codex -p

# 실시간 스트림 (async에 가까운 감지, Monitor 툴과 조합)
tmux pipe-pane -o -t agentchain:agy 'cat >> logs/agy.pane.log'
tmux pipe-pane -o -t agentchain:codex 'cat >> logs/codex.pane.log'

# 명령 주입
tmux send-keys -t agentchain:agy "여기에 지시문" C-m
tmux send-keys -t agentchain:codex "여기에 지시문" C-m
```

## 확장 지점 (나중에, 필요해지면)

- agy가 codex를 자동으로 부르게 만들려면 eraweb-fork
  `tools/router/src/adapters/codexAdapter.ts`처럼 agy 쪽에서 직접 프로세스를
  spawn하거나, agy 프롬프트에 "필요하면 codex 창에 send-keys 해라"는 지시를
  줘야 한다 (agy가 tmux를 조작할 권한/도구가 있어야 가능 — 이 템플릿 밖 일).
- Sol/Opus도 상시 창으로 분리하고 싶으면 `config.env`에 `CODEX_SOL_WINDOW`/
  `CLAUDE_OPUS_WINDOW`를 추가하고 `bootstrap.sh`/`watchdog.sh`의 패턴을
  그대로 복붙하면 된다.
- 엄격한 폴백이 필요해지면 `eraweb-fork/docs/workflows/multi_model_router.md`
  §5(리스/하트비트/스티키라우팅/핑퐁방지)를 참고해 `watchdog.sh`를 확장.
