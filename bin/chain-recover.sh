#!/usr/bin/env bash
# bin/chain-recover.sh <세션명|런타임ID|all> [--dry-run] [--restart-claude] [--session-id <uuid>]
# - tmux 세션이 죽어 사라졌으면 세션을 재생성하고 감독자 Claude를 --resume --remote-control 로 기동한다.
# - tmux 세션은 살아있으나 sonnet 창이나 Claude 프로세스가 죽었으면 해당 창에서 Claude만 재기동한다.
# - "Resume this conversation?" 대화상자가 떠 있으면 자동으로 Enter(1. Resume)를 눌러 복구를 완료한다.
# - ~/.claude/sessions/<pid>.json 및 패널 출력을 검사하여 Remote Control 연결 및 URL을 확인·기록한다.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$HERE/lib/term.sh"

want="${1:-}"; shift || true
DRY=false
RESTART_CLAUDE=false
SID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=true ;;
    --restart-claude|--force-rc) RESTART_CLAUDE=true ;;
    --session-id) SID="${2:-}"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
  shift
done

[[ -n "$want" ]] || {
  echo "Usage: $0 <session-name|all> [--dry-run] [--restart-claude] [--session-id <uuid>]" >&2
  "$HERE/bin/chain" list 2>/dev/null
  exit 64
}

# all 지정 시 chain-registry.sh 파이프라인(runtime/registry.json)에서 활성 체인 목록을 받아 각 세션의 recover_script 실행
if [[ "$want" == "all" ]]; then
  targets=()
  if [[ -x "$HERE/bin/chain-registry.sh" ]]; then
    while IFS= read -r s_cand; do
      [[ -n "$s_cand" ]] && targets+=("$s_cand")
    done < <("$HERE/bin/chain-registry.sh" json | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    for s in d.get("sessions", []):
        if not s.get("retired"):
            print(s.get("session_name", ""))
except Exception:
    pass
')
  fi

  if [[ "${#targets[@]}" -eq 0 ]]; then
    echo "복구 가능한 활성 체인 인스턴스가 없습니다."
    exit 0
  fi

  rc_overall=0
  for t in "${targets[@]}"; do
    opts=()
    $DRY && opts+=(--dry-run)
    $RESTART_CLAUDE && opts+=(--restart-claude)
    "$0" "$t" "${opts[@]}" || rc_overall=$?
  done
  [[ -x "$HERE/bin/chain-registry.sh" ]] && "$HERE/bin/chain-registry.sh" sync >/dev/null 2>&1 || true
  exit "$rc_overall"
fi

# 1) 런타임 탐색: instances/<want>.env 우선, 이후 session_name 마커 또는 디렉토리명 일치
RT=""
if [[ -f "$HERE/instances/$want.env" ]]; then
  RT="$(grep '^ACC_RUNTIME=' "$HERE/instances/$want.env" 2>/dev/null | cut -d= -f2- | tr -d '"'"' \r\n" || true)"
  [[ -n "$RT" && ! -d "$RT" ]] && RT=""
fi
if [[ -z "$RT" ]]; then
  for d in "$HERE"/runtime/*/; do
    [[ -d "$d" ]] || continue
    n="$(head -n1 "$d/session_name" 2>/dev/null | tr -d '\r\n')"
    if [[ "$n" == "$want" || "$(basename "$d")" == "$want" ]]; then
      [[ -f "$d/retired" ]] && continue
      if [[ -z "$RT" || "$d" -nt "$RT" ]]; then RT="${d%/}"; fi
    fi
  done
fi
[[ -n "$RT" && -d "$RT" ]] || { echo "런타임 없음: $want" >&2; exit 70; }
S="$(head -n1 "$RT/session_name" 2>/dev/null | tr -d '\r\n')"
[[ -n "$S" ]] || S="$want"

# 2) 설정 로드 (인스턴스 env 우선, 없으면 config.env)
PROJECT_DIR=""; SESSION_NAME=""; SONNET_CMD="claude --model sonnet --remote-control"
SONNET_WINDOW=sonnet; AGY_WINDOW=agy; CODEX_WINDOW=codex
[[ -f "$HERE/config.env" ]] && source "$HERE/config.env"
[[ -f "$HERE/instances/$S.env" ]] && source "$HERE/instances/$S.env"
P="${PROJECT_DIR:?PROJECT_DIR 미설정}"

# 3) 감독자 Claude 트랜스크립트 탐색:
#    1순위: 현재 실행 중인 Claude 세션 메타데이터
#    2순위: PROJECT_DIR 의 프로젝트 폴더에서 이 런타임의 훅을 언급하는 가장 최근 세션
#    3순위: $RT/supervisor_session_id 캐시
PD="$HOME/.claude/projects/$(printf '%s' "$P" | sed 's#[/._]#-#g')"
if [[ -z "$SID" ]]; then
  f="$(grep -l -F "sonnet-event-wait.sh stop $RT" $(ls -t "$PD"/*.jsonl 2>/dev/null) 2>/dev/null | head -n1 || true)"
  [[ -n "$f" ]] && SID="$(basename "$f" .jsonl)"
fi
if [[ -z "$SID" && -f "$RT/supervisor_session_id" ]]; then
  SID="$(head -n1 "$RT/supervisor_session_id" 2>/dev/null | tr -d '\r\n' || true)"
fi
if [[ -n "$SID" && "$DRY" != "true" ]]; then
  printf '%s\n' "$SID" > "$RT/supervisor_session_id" 2>/dev/null || true
fi

echo "session=$S runtime=$RT project=$P supervisor-session-id=${SID:-<없음: 새 세션>}"

# 3.1) 고아 running 태스크 정리 (tmux 세션이 아예 죽어있던 경우만 정리)
if ! term_session_exists "$S"; then
  for t_state in "$RT"/tasks/*/state; do
    [[ -f "$t_state" ]] || continue
    if grep -q '^status=running' "$t_state" 2>/dev/null; then
      t_id="$(grep '^task_id=' "$t_state" 2>/dev/null | cut -d= -f2- || true)"
      w_name="$(grep '^worker=' "$t_state" 2>/dev/null | cut -d= -f2- || true)"
      if [[ "$DRY" == "true" ]]; then
        echo "[dry-run] Would mark running task $t_id as abandoned (reason_code=host_restarted)"
      else
        tmp_s="${t_state}.tmp.$$"
        sed -e 's/^status=running/status=abandoned/' "$t_state" > "$tmp_s"
        if ! grep -q '^reason_code=' "$tmp_s"; then
          printf 'reason_code=host_restarted\n' >> "$tmp_s"
        else
          sed -i 's/^reason_code=.*/reason_code=host_restarted/' "$tmp_s"
        fi
        mv "$tmp_s" "$t_state"
        if [[ -n "$w_name" ]]; then
          rm -f "$RT/workers/${w_name}.busy" 2>/dev/null || true
          i_file="$RT/workers/${w_name}.last_interrupted"
          {
            printf 'task_id=%s\n' "$t_id"
            printf 'status=abandoned\n'
            printf 'reason_code=host_restarted\n'
            printf 'interrupted_epoch=%s\n' "$(date +%s)"
          } > "${i_file}.tmp.$$"
          mv "${i_file}.tmp.$$" "$i_file" 2>/dev/null || true
        fi
      fi
    fi
  done
fi

RESUME=""
[[ -n "$SID" ]] && RESUME="--resume $SID"
LAUNCH="export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 ACC_RUNTIME=\"$RT\" ACC_TEMPLATE_ROOT=\"$HERE\" SESSION_NAME=\"$S\"; $SONNET_CMD $RESUME --settings \"$RT/claude-bridge.settings.json\" --add-dir \"$HERE\" --append-system-prompt \"\$(cat '$RT/session_brief.md')\""

if $DRY; then
  echo "[dry-run] $LAUNCH"
  exit 0
fi

export PATH="$HOME/.local/bin:$PATH"

# 4) tmux 세션 및 윈도우 보장 + Claude 기동 상태 확인
need_launch=false
if ! term_session_exists "$S"; then
  term_new_session "$S" "$SONNET_WINDOW" "$P"
  term_set_env "$S" HOME "$HOME"
  term_set_env "$S" ACC_RUNTIME "$RT"
  term_set_env "$S" ACC_BOOTSTRAP_VERSION 2
  for w in "$AGY_WINDOW" "$CODEX_WINDOW"; do
    term_new_window "$S" "$w" "$P"
    term_run_cmd "$S:$w" "export ACC_RUNTIME=\"$RT\" PROJECT_DIR=\"$P\" HERE=\"$HERE\"; cd \"$P\""
  done
  need_launch=true
else
  term_set_env "$S" HOME "$HOME"
  term_set_env "$S" ACC_RUNTIME "$RT"
  term_set_env "$S" ACC_BOOTSTRAP_VERSION 2
  if ! term_has_window "$S" "$SONNET_WINDOW"; then
    term_new_window "$S" "$SONNET_WINDOW" "$P"
    need_launch=true
  fi
  for w in "$AGY_WINDOW" "$CODEX_WINDOW"; do
    if ! term_has_window "$S" "$w"; then
      term_new_window "$S" "$w" "$P"
      term_run_cmd "$S:$w" "export ACC_RUNTIME=\"$RT\" PROJECT_DIR=\"$P\" HERE=\"$HERE\"; cd \"$P\""
    fi
  done

  if [[ "$need_launch" == "false" ]]; then
    pane_pid="$(term_pane_pid "$S:$SONNET_WINDOW" || true)"
    claude_pid=""
    if [[ -n "$pane_pid" ]]; then
      claude_pid="$(pgrep -P "$pane_pid" -f claude 2>/dev/null | head -n1 || true)"
    fi
    fg_cmd="$(term_fg_cmd "$S:$SONNET_WINDOW")"

    if [[ "$RESTART_CLAUDE" == "true" && -n "$claude_pid" ]]; then
      echo "[$S] --restart-claude 요청: 기존 Claude(PID $claude_pid)를 종료하고 재기동합니다..."
      kill -TERM "$claude_pid" 2>/dev/null || true
      for _ in 1 2 3 4 5; do
        kill -0 "$claude_pid" 2>/dev/null || break
        sleep 1
      done
      claude_pid=""
      need_launch=true
    elif [[ -z "$claude_pid" && "$fg_cmd" != "claude" ]]; then
      echo "[$S] sonnet 창에서 Claude 프로세스가 실행 중이지 않습니다(현재: ${fg_cmd:-없음}). Claude를 재기동합니다..."
      need_launch=true
    fi
  fi
fi

if [[ "$need_launch" == "true" ]]; then
  term_run_cmd "$S:$SONNET_WINDOW" "$LAUNCH"
fi

# 5) Claude 기동, "Resume this conversation?" 자동 승인, Remote Control URL 검증
claude_pid=""
claude_status="starting"
bridge_id=""
rc_url=""

for attempt in $(seq 1 12); do
  pane_pid="$(term_pane_pid "$S:$SONNET_WINDOW" || true)"
  if [[ -n "$pane_pid" ]]; then
    claude_pid="$(pgrep -P "$pane_pid" -f claude 2>/dev/null | head -n1 || true)"
    if [[ -z "$claude_pid" ]] && [[ "$(term_fg_cmd "$S:$SONNET_WINDOW")" == "claude" ]]; then
      claude_pid="$pane_pid"
    fi
  fi

  # "Resume this conversation?" 대화상자가 떠 있으면 자동 Enter 전송
  tail_out="$(term_capture_tail "$S:$SONNET_WINDOW" 25 || true)"
  if printf '%s\n' "$tail_out" | grep -q "Resume this conversation?" && printf '%s\n' "$tail_out" | grep -q "1\. Resume"; then
    echo "[$S] 'Resume this conversation?' 확인 프롬프트 감지 -> 자동 Resume(Enter) 전송"
    term_send_key "$S:$SONNET_WINDOW" Enter
    sleep 1
  fi

  # ~/.claude/sessions/<claude_pid>.json 에서 상태 및 bridgeSessionId 추출
  if [[ -n "$claude_pid" && -f "$HOME/.claude/sessions/${claude_pid}.json" ]]; then
    meta="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    st = d.get("status", "unknown")
    wf = d.get("waitingFor", "")
    br = d.get("bridgeSessionId", "")
    si = d.get("sessionId", "")
    print(f"{st}|{wf}|{br}|{si}")
except Exception:
    print("|||")
' "$HOME/.claude/sessions/${claude_pid}.json" 2>/dev/null || echo "|||")"
    IFS='|' read -r claude_status waiting_for bridge_id live_sid <<< "$meta"
    if [[ -n "$live_sid" ]]; then
      SID="$live_sid"
      printf '%s\n' "$SID" > "$RT/supervisor_session_id" 2>/dev/null || true
    fi
    if [[ -n "$bridge_id" && "$waiting_for" != "dialog open" ]]; then
      rc_url="https://claude.ai/code/${bridge_id}"
      break
    fi
  fi

  # 패널 출력에서 URL 폴백 추출
  if [[ -z "$rc_url" ]]; then
    found_url="$(printf '%s\n' "$tail_out" | grep -Eo 'https://claude\.ai/code/session_[A-Za-z0-9_]+' | tail -n1 || true)"
    if [[ -n "$found_url" ]]; then
      rc_url="$found_url"
    fi
  fi

  sleep 1
done

if [[ -n "$rc_url" ]]; then
  printf '%s\n' "$rc_url" > "$RT/remote_control_url" 2>/dev/null || true
elif [[ -f "$RT/remote_control_url" ]]; then
  rc_url="$(head -n1 "$RT/remote_control_url" 2>/dev/null | tr -d '\r\n' || true)"
fi

if [[ -n "$claude_pid" ]]; then
  [[ -x "$HERE/bin/chain-registry.sh" ]] && "$HERE/bin/chain-registry.sh" sync >/dev/null 2>&1 || true
  echo "복구/점검 완료: session=$S claude=alive(PID:$claude_pid,status:$claude_status) resume=${SID:-n/a} rc=${rc_url:-확인중}"
else
  echo "경고: session=$S 에서 Claude 프로세스를 아직 확인하지 못했습니다. 'tmux attach -t $S' 로 확인하세요." >&2
  exit 1
fi
