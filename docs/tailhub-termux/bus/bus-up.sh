#!/usr/bin/env bash
# 멱등 기동: bus-up.sh hub|node [--agent] [--peers a,b]   (tmux 세션 agent-bus-hub / agent-bus-main)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; mkdir -p "$HOME/logs"
role="${1:-node}"; shift || true
if [[ "$role" == hub ]]; then s=agent-bus-hub; cmd="python3 $HERE/hub.py"; else s=agent-bus-main; cmd="python3 $HERE/node.py run --label main $*"; fi
tmux has-session -t "$s" 2>/dev/null && { echo "$s 이미 실행 중"; exit 0; }
tmux new-session -d -s "$s" "cd ~ && $cmd 2>&1 | tee -a $HOME/logs/$s.log"
echo "$s 시작됨"
