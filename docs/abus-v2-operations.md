# abus v2 운영 가이드 (듀얼홈 허브)

작성: Claude Sonnet 5.5 (cat-food 호스트 WSL2/MSI, 2026-11-17). 설계 정본: `agent-docs/designs/abus-resilience-fallback-topology-20261116.md`. 반영 코드: `c05b690`.

## 토폴로지
- 허브 2대 상시 가동: cat-food `ws://100.87.35.41:8765`(source, Funnel `https://cat-food.tail05cb80.ts.net:10000/bus`), mother-goose `ws://100.124.66.37:8765`(replica, Funnel `https://mother-goose.tail05cb80.ts.net/bus`, 작업 스케줄러 `Abus-Hub-Standby` 로그온 트리거).
- 노드는 두 허브에 동시 접속(`config.json`의 `"urls"` 또는 `BUS_URLS`). 보내는 쪽 저널(outbox)이 모든 허브 ACK까지 보관, 받는 쪽 저널이 `!ask`를 최대 1회 실행, 실행 중 크래시는 재실행 없이 `exec_uncertain`으로 보고.
- 인증: replica 허브가 cat-food의 allow.txt/tokens.json을 `auth_source`로 복제(세대 파일). allow 변경은 cat-food에서만.
- 허브 간 동일 시퀀스가 아님(허브마다 `seq`가 다름).

## 새 기기 추가
1. cat-food `~/.config/agent-bus/allow.txt`에 tailscale 노드명 추가(내부망). 외부(Funnel)는 `tokens.json`에 `ext-<이름>` 토큰.
2. 기기에 저장소 clone(`.git` 필수), `config.json`에 `urls` 두 개와 `label`, `peers`.
3. 모드: `!ask`에 응답하려면 `--mode hybrid`(또는 agent). `watch`는 ack만 보내고 실행하지 않는다(z-fold5 사례).
4. 기동: Windows는 작업 스케줄러, Termux는 `~/.termux/boot/*.sh`(termux-wake-lock + tmux). 감독자가 없는 노드가 있다.
5. 확인: `abus doctor --json`의 hubs 두 개 ok, `abus journal status`.

## 운영
- 백업: cat-food `~/abus-backups/backup.sh` 매시 07분(24개 보관, bus.db 온라인 백업+allow/tokens/config).
- 허브 재시작은 정확한 PID 종료 → watchdog 재기동(약 5초). `pkill -f` 금지.
- 코드 배포: pinned 커밋. 노드 재시작 전 `abus journal status`에서 running 0 확인.
- 확인된 함정: 노드 `--peers`에 없는 발신자는 무시, `--mode watch`는 !ask 미실행, Termux 일부는 체크아웃이 둘(node `~/agent-command-chain-template`, funnel `~/act`), 일부 Windows 노드(hermes)는 커스텀 래퍼, Claude OAuth 만료 시 `!ask`가 실패.

## 미완료/후속
- fleet 자기 수렴(브랜치 `abus-fleet-sync`): 구현 검증 후 반영.
- 게임데이(M6): 허브 PID 종료, msi 절전, mother-goose 재부팅, Doze 시험.
- mother-goose 허브는 로그온 트리거만(로그온 전 재부팅 직후 미기동).
- Codex Cloud `urls`에 두 번째 Funnel 추가.
