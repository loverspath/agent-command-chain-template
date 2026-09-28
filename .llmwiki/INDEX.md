---
title: agent-command-chain-template Wiki Index
tags: [index, moc, tmux-bridge, multi-agent-orchestration]
related: ["[[architecture]]", "[[usage]]", "[[known-issues]]"]
summary: Sonnet/agy/Codex 4계층 명령계통 및 v2 Full-Push/Stage 1 상주/대시보드/v1 tmux 브리지 내부 위키 색인.
---

# agent-command-chain-template

Claude Sonnet(감독/RC) · agy(Gemini 3.8 Flash, 라우터+워커) · Codex Terra(중난도 설계/직접수행) · Sol/Opus(최고난도 상담, 상시 창 없음) 4계층을 연결하는 범용 멀티 에이전트 명령계통 템플릿이다.
무거운 외부 하네스(DB, 하트비트, 리스 등) 없이 순수 셸 스크립트와 tmux 기반으로 시작하여, v2 Full-Push(POSIX FIFO + Durable Outbox) 비동기 통지, Stage 1 agy 상주 TUI 모드, tailnet 전용 읽기 전용 대시보드(`dashboard/`)까지 지원한다.
어느 디렉토리에서든 스크립트를 호출하면 해당 작업 디렉토리를 대상으로 삼아 즉시 멀티 에이전트 협업 환경을 구축한다.

세션을 처음 시작한다면: [[architecture]] → [[usage]] 순서로 읽으면 충분하다.
작업 중 막히거나 경고가 발생하면 바로 [[known-issues]]를 확인하라 — 이미 실측하고 해결책/우회책을 정리해 두었다.

## Architecture

- [[architecture]] — 4계층 역할 구조, Full-Push v2 브리지(FIFO+Outbox), Stage 1 Resident TUI 모드, 대시보드, v1 tmux 브리지 및 폴백 설계, agy→codex 자동 연동 부재 이유

## Usage

- [[usage]] — 사전 요구사항, 템플릿 설정(config.env), bootstrap-v2.sh/bootstrap.sh 실행법, 디스패치 및 관찰/개입 명령, Resident TUI 및 대시보드 운용, agy-Codex 수동 핸드오프, 세션 정리

## Known Issues

- [[known-issues]] — RC 전제조건, 디렉토리 신뢰(Folder Trust) 자동 확인 한계, 세션 지속성 제품 버그, agy/codex 바이너리 이슈, tmux 중첩 attach, v1/v2 세션 오조준(interactive TUI) 및 4계층 자동 해석, 상주 대화형 TUI 비선점형 입력 큐잉(9/23 병목), 동시 타이핑 혼선, 테스트 하네스 격리 규약, Sonnet의 요청 외 범위 임의 확장(SSH 과설계) 사고, Post-mortem 규약 미자동화(10라운드 전량 누락) 사고

## 태그 인덱스

- `#4-tier-command` — [[architecture]]
- `#architecture` — [[architecture]]
- `#bootstrap` — [[architecture]], [[usage]]
- `#bug` — [[known-issues]]
- `#cli-behavior` — [[known-issues]]
- `#config` — [[usage]]
- `#dashboard` — [[architecture]], [[usage]]
- `#dispatch-targeting` — [[known-issues]]
- `#fallback` — [[architecture]], [[known-issues]]
- `#fifo` — [[architecture]]
- `#full-push` — [[architecture]], [[usage]]
- `#handoff` — [[usage]]
- `#isolation` — [[architecture]], [[known-issues]]
- `#known-issues` — [[known-issues]]
- `#limitations` — [[known-issues]]
- `#non-preemptive-tui` — [[known-issues]]
- `#operation` — [[usage]]
- `#outbox` — [[architecture]]
- `#permissions` — [[known-issues]]
- `#product-bug` — [[known-issues]]
- `#prompt-drift` — [[known-issues]]
- `#remote-control` — [[known-issues]]
- `#resident-tui` — [[architecture]], [[usage]]
- `#roles` — [[architecture]]
- `#session-resolution` — [[architecture]], [[known-issues]]
- `#sonnet-overreach` — [[known-issues]]
- `#unrequested-scope` — [[known-issues]]
- `#self-reflection-not-automatic` — [[known-issues]]
- `#test-harness-isolation` — [[architecture]], [[known-issues]]
- `#tmux-bridge` — [[architecture]], [[usage]]
- `#tmux-nesting` — [[known-issues]]
- `#troubleshooting` — [[known-issues]]
- `#usage` — [[usage]]
- `#usage-manual` — [[usage]]
- `#watchdog` — [[architecture]], [[usage]]
- `#wsl` — [[known-issues]], [[usage]]

---
- 상위 볼트: `C:\Users\rerun\llm-wiki\Projects\agent-command-chain-template\`
- 위키 컨벤션: `C:\Users\rerun\llm-wiki\CONVENTIONS\llm-wiki-convention.md`
