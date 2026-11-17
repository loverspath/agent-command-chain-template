#!/usr/bin/env bash
# abus v2 원클릭 노드 구성 (Linux/WSL/Termux). 멱등. 사용: abus-join.sh [--label main] [--mode hybrid] [--peers a,b] [--repo DIR] [--dry-run]
# 하는 일: 저장소 clone/ff-pull → config.json urls(듀얼홈) 병합 → mode 파일 → 기동 스크립트(Termux boot) → tmux 기동 → doctor 확인
set -euo pipefail
LABEL=main; MODE=hybrid; DRY=0
PEERS="cat-food,msi,mother-goose,claw8,z-fold8,z-fold5,codex-environment"
URLS="ws://100.87.35.41:8765,ws://100.124.66.37:8765"
REPO="$HOME/agent-command-chain-template"; GIT=https://github.com/loverspath/agent-command-chain-template.git
while [ $# -gt 0 ]; do case "$1" in
  --label) LABEL=$2; shift 2;; --mode) MODE=$2; shift 2;; --peers) PEERS=$2; shift 2;;
  --repo) REPO=$2; shift 2;; --urls) URLS=$2; shift 2;; --dry-run) DRY=1; shift;; *) echo "알 수 없는 옵션 $1"; exit 2;; esac; done
run(){ echo "+ $*"; [ "$DRY" = 1 ] || "$@"; }
CFG="$HOME/.config/agent-bus"; mkdir -p "$HOME/logs" "$CFG"
# 1) 저장소: .git 없는 체크아웃은 보존 후 clone, 있으면 ff-only
if [ -d "$REPO/.git" ]; then run git -C "$REPO" pull --ff-only
else [ -e "$REPO" ] && run mv "$REPO" "$REPO.old-$(date +%Y%m%d%H%M)"; run git clone "$GIT" "$REPO"; fi
# 2) config.json: 기존 키 보존, urls/label/peers 병합(.bak)
if [ "$DRY" = 0 ]; then
  [ -f "$CFG/config.json" ] && cp "$CFG/config.json" "$CFG/config.json.bak-$(date +%Y%m%d%H%M)"
  CFGF="$CFG/config.json" URLS="$URLS" LABEL="$LABEL" PEERS="$PEERS" python3 - <<'PY'
import json,os
p=os.environ["CFGF"]
try: c=json.load(open(p))
except Exception: c={}
u=[x for x in os.environ["URLS"].split(",") if x]
c["urls"]=u; c.setdefault("url",u[0]); c["label"]=os.environ["LABEL"]; c["peers"]=os.environ["PEERS"]
json.dump(c,open(p,"w"),indent=2)
PY
  echo "$MODE" > "$CFG/mode"
fi
# 3) 기동 스크립트: Termux 는 ~/.termux/boot, 그 외는 tmux 수동 기동만
CMD="python3 $REPO/docs/tailhub-termux/bus/node.py run --label $LABEL --mode $MODE --peers $PEERS"
if [ -n "${TERMUX_VERSION:-}" ] || [ -d "$HOME/.termux" ]; then
  if [ "$DRY" = 0 ]; then mkdir -p "$HOME/.termux/boot"; cat > "$HOME/.termux/boot/agent-bus.sh" <<B
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
tmux has-session -t agent-bus-$LABEL 2>/dev/null || tmux new-session -d -s agent-bus-$LABEL "cd ~ && $CMD 2>&1 | tee -a \$HOME/logs/agent-bus-$LABEL.log"
B
  chmod +x "$HOME/.termux/boot/agent-bus.sh"; else echo "+ ~/.termux/boot/agent-bus.sh 작성"; fi
  command -v termux-wake-lock >/dev/null && run termux-wake-lock || true
fi
# 4) 기동(이미 있으면 건너뜀; 실행 중 코드 갱신은 노드 재시작 필요: abus journal status 로 running 0 확인 후 PID 종료)
if tmux has-session -t "agent-bus-$LABEL" 2>/dev/null; then echo "agent-bus-$LABEL 이미 실행 중 (코드 갱신 반영하려면 running 0 확인 후 해당 PID만 종료)"
else run tmux new-session -d -s "agent-bus-$LABEL" "cd ~ && $CMD 2>&1 | tee -a $HOME/logs/agent-bus-$LABEL.log"; fi
# 5) 확인
[ "$DRY" = 1 ] || { sleep 8; PYTHONPATH="$REPO/bus" python3 -m agent_bus.cli doctor --json | python3 -c "import json,sys;d=json.load(sys.stdin);print('mode',d.get('mode'),'all_hubs_ok',d.get('all_hubs_ok'));[print(h['url'],h['ok']) for h in d.get('hubs',[])]"; }
