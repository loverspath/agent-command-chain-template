#!/usr/bin/env bash
# 버스 워치독 (cat-food/WSL 및 Linux/Termux 공용). 60초마다 허브(허브 호스트만)/노드/동기화 세션이 살아있는지 확인하고,
# ~/.config/agent-bus/mode (agent|watch|hybrid, 기본 hybrid) 는 node.py 가 메시지마다 읽는다(재시작 불필요).
#   bus-watchdog.sh [--hub] [--sync] [--peers a,b,c]
HERE="$(cd "$(dirname "$0")" && pwd)"; CFG="$HOME/.config/agent-bus"; mkdir -p "$CFG" "$HOME/logs"
HUB=false; SYNC=false; PEERS="cat-food,msi,mother-goose,claw8,z-fold8"
while [[ $# -gt 0 ]]; do case "$1" in --hub) HUB=true;; --sync) SYNC=true;; --peers) PEERS="$2"; shift;; esac; shift; done
[[ -f "$CFG/mode" ]] || echo hybrid > "$CFG/mode"
applied="$CFG/.applied_mode"
alive() { tmux has-session -t "$1" 2>/dev/null; }
while true; do
  mode="$(tr -d "[:space:]" < "$CFG/mode")"; [[ "$mode" == watch ]] || mode=agent
  if $HUB && ! alive agent-bus-hub; then "$HERE/bus-up.sh" hub >/dev/null; fi
  if ! alive agent-bus-main; then "$HERE/bus-up.sh" node --peers "$PEERS" >/dev/null; fi  # 모드는 node.py 가 매 메시지마다 mode 파일에서 읽음
  if $SYNC && ! alive agent-bus-sync; then tmux new-session -d -s agent-bus-sync "python3 -u $HERE/bus-sync.py 2>&1 | tee -a $HOME/logs/agent-bus-sync.log"; fi
  sleep 60
done
