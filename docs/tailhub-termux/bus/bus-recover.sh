#!/usr/bin/env bash
# 부팅/복구 진입점(멱등): 워치독 tmux 세션을 보장한다. cat-food 는 허브+동기화 포함.
HERE="$(cd "$(dirname "$0")" && pwd)"
tmux has-session -t agent-bus-watchdog 2>/dev/null && exit 0
tmux new-session -d -s agent-bus-watchdog "$HERE/bus-watchdog.sh $* 2>&1 | tee -a $HOME/logs/agent-bus-watchdog.log"
