---
title: agent-command-chain-template Wiki Index
tags: [index, moc]
related: []
summary: Sonnet/agy/Codex Terra/Sol-Opus 4계층 명령계통을 tmux만으로 브리지하는 원클릭 템플릿.
---

# agent-command-chain-template

Claude Sonnet(감독/RC) · agy(Gemini 3.8 Flash, 라우터+워커) · Codex Terra(중난도
설계/직접수행) · Sol/Opus(최고난도 상담, 상시 창 없음) 4계층을 **tmux 창
세 개 + send-keys/capture-pane/pipe-pane만으로** 브리지하는 경량 템플릿.
SQLite 상태머신, 리스/하트비트 같은 깊은 하네스는 의도적으로 없음
(eraweb-fork의 `tools/router/`를 관찰하고 훨씬 가볍게 재구성한 것).

세션을 처음 시작한다면: [[architecture]] → [[usage]] 순서로 읽으면 충분하다.
뭔가 안 되면 바로 [[known-issues]]부터 훑어라 — 이미 겪고 고친 문제일 확률이 높다.

## Architecture

- [[architecture]] — 4계층 역할, tmux 창 구조, agy→codex 자동연동이 없는 이유

## Usage

- [[usage]] — 설치, config.env 설정, bootstrap.sh/watchdog.sh 실행법, 관찰/개입 명령

## Known Issues

- [[known-issues]] — 트러스트 다이얼로그 스킵 불가, CLAUDE_CODE_CHILD_SESSION
  버그, agy Windows-only 착시, tmux 중첩 attach, send-keys 충돌

## 태그 인덱스

- `#roles` `#tmux-bridge` — [[architecture]]
- `#bootstrap` `#config` `#watchdog` — [[usage]]
- `#bug` `#product-bug` `#permissions` `#tmux-nesting` — [[known-issues]]

---
상위 볼트: `C:\Users\rerun\llm-wiki\Projects\agent-command-chain-template.md`
컨벤션: `C:\Users\rerun\llm-wiki\CONVENTIONS\llm-wiki-convention.md`
