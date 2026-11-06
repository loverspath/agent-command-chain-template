# 원샷 온보딩 프롬프트

각 기기의 Claude 에 아래 블록을 통째로 붙여넣는다. 레포는 `loverspath/agent-command-chain-template`.

## 공통 (mother-goose / claw8)
```
너는 Tailscale 노드 <NODE> 의 Claude 야. Tailnet Agent Bus 에 노드로 참여시켜줘. 순서대로 하고 막히면 멈춰서 이유만 보고해.
1. `tailscale status` 로 내 노드명과 cat-food(100.87.35.41) 가 보이는지 확인. 내 노드명이 <NODE> 와 다르면 실제 이름을 보고 (허브 허용목록 수정 필요).
2. 레포 확보: ~ 아래 agent-command-chain-template 이 있으면 `git pull --ff-only`, 없으면 `git clone https://github.com/loverspath/agent-command-chain-template` 후 docs/tailhub-termux/bus/README.md 를 읽어.
3. `pip install websockets` (Windows 면 `python -m pip`). python 3.8+ 필요.
4. 연결 시험: `python3 docs/tailhub-termux/bus/node.py peers` → `peers=...` 가 나오면 성공. 실패(4003)면 허용목록 문제라고 보고.
5. 상주 기동(자동응답): tmux 가 있으면 `docs/tailhub-termux/bus/bus-up.sh node --agent --peers cat-food,mother-goose,claw8`, tmux 없는 Windows 면 `Start-Process python -ArgumentList "node.py run --label main --agent --peers cat-food,mother-goose,claw8" -WindowStyle Hidden` (홈 디렉토리에서).
6. 통신 시험: `node.py send --to cat-food "hello from <NODE>" --wait 90 --first` 후 응답을 그대로 보고.
규칙: 파일 수정·삭제·재시작은 위 단계에 필요한 것만. 토큰/키를 출력하지 마.
```
- mother-goose: `<NODE>` = mother-goose
- claw8: `<NODE>` = claw8, 그리고 추가 임무 — cat-food 가 응답하지 않으면(`send` 타임아웃) `th ts catfood list` → `th ts catfood recover all` 로 복구하고, 허브가 죽었으면 cat-food 에 SSH 후 `bus/bus-up.sh hub && bus/bus-up.sh node --agent` 를 실행.
