#!/usr/bin/env bash
# 버스 워치독 (cat-food/WSL 및 Linux/Termux 공용). 60초마다 허브(허브 호스트만)/노드/동기화 세션이 살아있는지 확인하고,
# ~/.config/agent-bus/mode (agent|watch) 가 바뀌면 노드를 재시작한다.
#   bus-watchdog.sh [--hub] [--sync] [--peers a,b,c]
HERE="$(cd "$(dirname "$0")" && pwd)"; CFG="$HOME/.config/agent-bus"; mkdir -p "$CFG" "$HOME/logs"
HUB=false; SYNC=false; PEERS="cat-food,msi,mother-goose,claw8,z-fold8"
while [[ $# -gt 0 ]]; do case "$1" in --hub) HUB=true;; --sync) SYNC=true;; --peers) PEERS="$2"; shift;; esac; shift; done
[[ -f "$CFG/mode" ]] || echo agent > "$CFG/mode"
applied="$CFG/.applied_mode"
alive() { tmux has-session -t "$1" 2>/dev/null; }
while true; do
  mode="$(tr -d "[:space:]" < "$CFG/mode")"; [[ "$mode" == watch ]] || mode=agent
  if $HUB && ! alive agent-bus-hub; then "$HERE/bus-up.sh" hub >/dev/null; fi
  if [[ "$(cat "$applied" 2>/dev/null)" != "$mode" ]]; then tmux kill-session -t agent-bus-main 2>/dev/null; echo "$mode" > "$applied"; fi
  if ! alive agent-bus-main; then
    if [[ "$mode" == agent ]]; then "$HERE/bus-up.sh" node --agent --peers "$PEERS" >/dev/null; else "$HERE/bus-up.sh" node >/dev/null; fi
  fi
  if $SYNC && ! alive agent-bus-sync; then tmux new-session -d -s agent-bus-sync "python3 -u $HERE/bus-sync.py 2>&1 | tee -a $HOME/logs/agent-bus-sync.log"; fi
  sleep 60
done
