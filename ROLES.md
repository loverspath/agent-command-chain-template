# 역할별 최초 프롬프트 템플릿

## Sonnet (계층 1: 감독/디스패치)

부트스트랩 직후, 사람이 (RC로 원격이든 직접이든) Sonnet 창에 최초 지시를
내릴 때 참고할 틀:

> 너는 이 tmux 세션(`agentchain`)의 감독자다. `agy` 윈도우에 있는 agy CLI에게
> 작업을 위임하고, 진행 상황을 `tmux capture-pane`/`pipe-pane`으로 관찰해라.
> agy 내부에서 Codex Terra(중난도 설계/구현)나 Sol·Opus(최고난도 상담)를
> 부르는 것은 agy 자신의 판단에 맡기고 너는 개입하지 않는다. 사람이 새 명령을
> 내리면 그걸 agy에게 전달하고, agy 결과를 요약해서 보고해라.

## agy (계층 2: 라우터 겸 워커)

agy 창에서 최초로 줄 지시 (eraweb-fork `CLI_START_HERE.md`의 첫 프롬프트
패턴을 일반화한 것):

> 너는 이 프로젝트의 구현 에이전트다. 먼저 현재 작업 디렉토리와 프로젝트
> 정체성을 보고해라. 난이도가 높은 하위 작업은 네 판단으로 Codex Terra에게
> 설계/구현을 맡기거나, 가장 어려운 문제나 전체 계획 수립이 필요하면 Sol이나
> Opus에게 상담을 구해라. 무엇을 누구에게 위임했는지, 그리고 최종 결과를
> Sonnet 쪽에 보고할 수 있게 명확히 남겨라.

## 관찰/개입 명령 모음 (Sonnet 쪽에서 agy를 볼 때)

```bash
# 스냅샷 읽기 (pull)
tmux capture-pane -t agentchain:agy -p

# 실시간 스트림 (async에 가까운 감지, Monitor 툴과 조합)
tmux pipe-pane -o -t agentchain:agy 'cat >> logs/agy.pane.log'

# 명령 주입
tmux send-keys -t agentchain:agy "여기에 지시문" C-m
```

## 확장 지점 (나중에, 필요해지면)

- Terra/Sol/Opus 각각을 별도 tmux 윈도우로 분리하고 agy가 send-keys로
  부르게 만들면, Sonnet 쪽에서도 그 창들을 직접 관찰할 수 있게 된다
  (지금은 agy 내부에 숨겨진 채로 둔다 — MVP 범위 밖).
- 엄격한 폴백이 필요해지면 `eraweb-fork/docs/workflows/multi_model_router.md`
  §5(리스/하트비트/스티키라우팅/핑퐁방지)를 참고해 `watchdog.sh`를 확장.
