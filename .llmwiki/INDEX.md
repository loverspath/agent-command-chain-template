---
title: agent-command-chain-template Wiki Index
tags: [index, moc, tmux-bridge, multi-agent-orchestration]
related: []
summary: tmux 창 3개(Sonnet/agy/Codex) 기반 프로젝트 비종속 4계층 멀티 에이전트 명령계통 템플릿의 내부 위키 색인.
---

# agent-command-chain-template

임의의 프로젝트 디렉토리에서 Sonnet(감독), agy(라우터 겸 워커), Codex Terra(설계/수행) 세 CLI를 tmux 창 3개로 브리지하는 범용 4계층 명령계통 템플릿이다.
무거운 외부 하네스(DB, 하트비트, 리스 등) 없이 순수 셸 스크립트(`bootstrap.sh`, `watchdog.sh`)와 tmux 내장 명령(`send-keys`, `capture-pane`, `pipe-pane`)만으로 동작한다.
어느 디렉토리에서든 스크립트를 호출하면 해당 디렉토리를 작업 대상으로 삼아 즉시 멀티 에이전트 협업 환경을 구축한다.

## Architecture

- [[architecture]] — tmux 윈도우 3개 브리지 설계, 4계층 명령계통 구조, bootstrap.sh 및 watchdog.sh의 구체적 동작 흐름

## Usage

- [[usage]] — 템플릿 설정(config.env), 부트스트랩 및 워치독 기동, tmux 관찰/개입 명령, Sonnet 매개 agy-Codex 수동 핸드오프 절차

## Known Issues

- [[known-issues]] — Claude Code Remote Control(RC) 활성화 전제조건, agy→Codex 자동 라우팅 부재, 세션 지속성 경고, 무상태 워치독 한계

## 태그 인덱스

- `#architecture` — [[architecture]]
- `#tmux-bridge` — [[architecture]], [[usage]]
- `#4-tier-command` — [[architecture]]
- `#bootstrap` — [[architecture]], [[usage]]
- `#watchdog` — [[architecture]]
- `#usage` — [[usage]]
- `#handoff` — [[usage]]
- `#operation` — [[usage]]
- `#known-issues` — [[known-issues]]
- `#limitations` — [[known-issues]]
- `#remote-control` — [[known-issues]]

---
새 페이지 추가 규칙: [[/mnt/c/Users/rerun/llm-wiki/CONVENTIONS/llm-wiki-convention|LLM Wiki Convention]] 참고.
