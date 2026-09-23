---
title: Architecture
tags: [roles, tmux-bridge, architecture]
related: ["[[INDEX]]", "[[usage]]", "[[known-issues]]"]
summary: 4계층 역할 구조와 tmux 세 창 브리지 설계, agy가 codex를 자동으로 안 부르는 이유.
---

# Architecture

## 역할 구조 (4계층)

| 계층 | 주체 | 역할 |
|---|---|---|
| 0. 사용자 | 사람 | 최종 의사결정, RC로 Sonnet에 원격 개입 |
| 1. 감독/디스패치 | Claude Sonnet | 모니터링·감사, 명령 하달, 루프 관리, RC |
| 2. 라우터 겸 워커 | agy (Gemini 3.8 Flash) | 난이도 판단, 스스로 작업 수행 |
| 2-내부 Tier 2 | Codex Terra | 중난도 작업 설계+직접수행. **agy가 자동 호출 안 함** |
| 2-내부 Tier 3 | Sol / Opus | 최고난도 상담. 상시 창 없음, 필요시 모델 바꿔 즉석 실행 |

핵심 단순화: Sonnet은 agy와 codex 두 창만 직접 본다. eraweb-fork의 "Terra가
agy 보고를 정규화해서 Sol에게만 전달, agy는 Sol에 직접 보고 안 함" 규칙을
계층 구조 자체로 끌어올린 것.

## tmux 브리지 구조

```
tmux session: agentchain (config.env 의 SESSION_NAME)
 ├─ sonnet 창: claude --model sonnet --remote-control --add-dir <템플릿경로>
 ├─ agy 창:    agy --new-project --mode plan (대화형, 지속)
 └─ codex 창:  codex --model gpt-5.6-terra -s danger-full-access (대화형, 지속)
```

세 창 모두 **bootstrap.sh를 실행한 디렉토리**(PROJECT_DIR)에서 시작한다.
하드코딩된 프로젝트 경로는 없음 — 실행 위치 기준으로 동적 결정.

외부 컨트롤러(사람 또는 Sonnet 자신)가 `tmux send-keys` / `capture-pane` /
`pipe-pane`으로 조작한다. push는 없다 — tmux는 근본적으로 pull. 실시간성이
필요하면 `pipe-pane`으로 로그 파일을 만들고 그걸 tail하는 게 가장 async에
가까운 감지 수단이다.

## agy → codex 자동 연동이 없는 이유

eraweb-fork에서 agy가 Terra/Sol을 실제로 호출하는 로직은
`tools/router/src/adapters/*.ts`(Node `child_process.spawn`)에 있었는데, 이
템플릿은 그 라우터 자체를 의도적으로 뺐다(깊은 하네스 없이 가려는 목표).
그래서 **codex 창은 agy와 자동으로 연동되지 않는다** — agy 출력을 보고
"이건 Terra급이다" 판단되면 Sonnet(또는 사람)이 직접 codex 창에 send-keys로
작업을 넘겨야 한다. 진짜 자동 연동이 필요해지면 eraweb-fork의 라우터
어댑터를 참고해서 별도 구현.

## Sol/Opus는 왜 상시 창이 없나

사용량 빈도가 낮고(최고난도 상담), 상시 프로세스를 켜두는 것보다 필요할 때
`codex` 창 커맨드를 `--model gpt-5.6-sol`로 바꾸거나 Sonnet을
`--model opus`로 일회성 실행하는 게 더 가볍다. 상시 창으로 분리하고 싶으면
`config.env`에 새 WINDOW/CMD 변수를 추가하고 `bootstrap.sh`/`watchdog.sh`의
기존 패턴(3개 창 처리하는 부분)을 그대로 복붙하면 된다.

## watchdog.sh (느슨한 폴백)

세션/프로세스가 죽으면 그냥 재시작만 한다. eraweb-fork에 있던 리스/하트비트/
스티키라우팅/핑퐁방지 같은 상태머신은 의도적으로 없음 — 필요해지면
`eraweb-fork/docs/workflows/multi_model_router.md` §5를 참고해서 확장.
