#!/usr/bin/env bash
# bin/session-init.sh — 감독관(Supervisor) Claude 세션 교체/점검 단일 진입점 (초안)
#
#   session-init.sh [--check]                     읽기 전용 점검 + 한국어 보고 (기본)
#   session-init.sh --apply [--retire-old-supervisor] [--start-dashboard] [--no-launch]
#
# 공통 옵션: --project DIR  --session NAME  --handoff FILE  --runtime DIR(비상용)
#
# 안전 규칙 (DESIGN.md 참고):
#  - 기본은 읽기 전용. 프로세스 종료는 --retire-old-supervisor 가 있을 때만, 이번 실행에서
#    직접 찾은 정확한 PID(+시작시각 토큰 일치)에만 SIGTERM. pkill/pgrep -f 로 죽이지 않는다. SIGKILL 없음.
#  - PROTECTED_SESSIONS / PROTECTED_PORTS 에 걸린 세션·프로세스는 절대 건드리지 않는다.
#  - bootstrap-v2.sh 를 호출하지 않는다(--restart 포함).
#  - 호출자 셸에 ACC_RUNTIME/SESSION_NAME 을 export 하지 않는다(source 금지). 자식 호출은 env -i 로 명시 주입.
set -uo pipefail
umask 077

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo "session-init.sh 는 source 하지 말고 실행하세요 (환경변수 누수 방지)." >&2
  return 64 2>/dev/null || exit 64
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 초안 위치(scratchpad)에서 돌릴 때를 위한 템플릿 경로 해석
[[ -f "$HERE/bootstrap-v2.sh" ]] || HERE="${ACC_TEMPLATE_ROOT_OVERRIDE:-/home/rerun/agent-command-chain-template}"

# 호출자 환경 스냅샷 (판단용으로만 쓰고 그대로 신뢰하지 않음)
CALLER_ACC_RUNTIME="${ACC_RUNTIME:-}"
CALLER_SESSION_NAME="${SESSION_NAME:-}"

# ---------- 설정 로드 (이 프로세스 안에서만) ----------
CFG="${ACC_CONFIG_ENV:-$HERE/config.env}"
if [[ -f "$CFG" ]]; then
  # shellcheck disable=SC1090
  source "$CFG"
fi
CFG_SESSION_NAME="${SESSION_NAME:-}"
SONNET_WINDOW="${SONNET_WINDOW:-sonnet}"
AGY_WINDOW="${AGY_WINDOW:-agy}"
SONNET_CMD="${SONNET_CMD:-claude --model sonnet --remote-control}"
AUTO_CONFIRM_TRUST="${AUTO_CONFIRM_TRUST:-true}"
PROTECTED_SESSIONS="${PROTECTED_SESSIONS:-scheduler-prod-8088}"
PROTECTED_PORTS="${PROTECTED_PORTS:-8088}"
CFG_HANDOFF_FILE="${HANDOFF_FILE:-}"
unset ACC_RUNTIME SESSION_NAME   # 이후 값은 아래에서 라이브 상태로부터 다시 정한다

MODE=check RETIRE=false START_DASH=false NO_LAUNCH=false
OPT_PROJECT="" OPT_SESSION="" OPT_HANDOFF="" OPT_RUNTIME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE=check ;;
    --apply) MODE=apply ;;
    --retire-old-supervisor) RETIRE=true ;;
    --start-dashboard) START_DASH=true ;;
    --no-launch) NO_LAUNCH=true ;;
    --project) OPT_PROJECT="$2"; shift ;;
    --session) OPT_SESSION="$2"; shift ;;
    --handoff) OPT_HANDOFF="$2"; shift ;;
    --runtime) OPT_RUNTIME="$2"; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 64 ;;
  esac
  shift
done
if [[ "$MODE" == check ]] && { $RETIRE || $START_DASH; }; then
  echo "--retire-old-supervisor / --start-dashboard 는 --apply 와 함께만 씁니다." >&2; exit 64
fi

PROJECT="${OPT_PROJECT:-${SUPERVISOR_PROJECT_DIR:-$PWD}}"
PROJECT="$(cd "$PROJECT" 2>/dev/null && pwd || echo "$PROJECT")"

# ---------- 결과 수집 ----------
ROWS=() ; NFAIL=0 ; NWARN=0
add() { ROWS+=("$1|$2|$3"); [[ $1 == FAIL ]] && NFAIL=$((NFAIL+1)); [[ $1 == WARN ]] && NWARN=$((NWARN+1)); return 0; }
print_rows() {
  local r s k v
  for r in "${ROWS[@]}"; do
    IFS='|' read -r s k v <<<"$r"
    printf '  %-4s  %-18s %s\n' "$s" "$k" "$v"
  done
}
say() { printf '%s\n' "$*"; }

# ---------- /proc 헬퍼 ----------
# /proc/PID/stat 의 comm 에 공백이 있을 수 있어 마지막 ')' 뒤부터 센다
stat_field() { local s; s="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1; s="${s##*) }"; awk -v f="$2" '{print $(f-2)}' <<<"$s"; }
ppid_of() { stat_field "$1" 4; }
start_tok() { stat_field "$1" 22; }
cmdline_of() { tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null; }
env_of() { tr '\0' '\n' <"/proc/$1/environ" 2>/dev/null | sed -n "s/^$2=//p" | head -n1; }
comm_of() { cat "/proc/$1/comm" 2>/dev/null; }
# 조상 중 comm 이 $2 인 첫 PID
ancestor_with_comm() {
  local p="$1" want="$2" n=0
  while [[ -n "$p" && "$p" -gt 1 && $n -lt 64 ]]; do
    [[ "$(comm_of "$p")" == "$want" ]] && { echo "$p"; return 0; }
    p="$(ppid_of "$p")"; n=$((n+1))
  done
  return 1
}
# PID 가 속한 tmux 위치 "session:window(%pane)" (조상 중 pane_pid 매칭)
declare -A PANE_OF_PID=()
while read -r ppid loc; do PANE_OF_PID[$ppid]="$loc"; done < <(
  tmux list-panes -a -F '#{pane_pid} #{session_name}:#{window_name}(#{pane_id})' 2>/dev/null)
tmux_loc_of() {
  local p="$1" n=0
  while [[ -n "$p" && "$p" -gt 1 && $n -lt 64 ]]; do
    [[ -n "${PANE_OF_PID[$p]:-}" ]] && { echo "${PANE_OF_PID[$p]}"; return 0; }
    p="$(ppid_of "$p")"; n=$((n+1))
  done
  echo "tmux밖"
}
is_protected_loc() {
  local loc="$1" s
  for s in $PROTECTED_SESSIONS; do [[ "$loc" == "$s:"* ]] && return 0; done
  return 1
}
pid_listens_protected_port() {
  local pid="$1" port
  for port in $PROTECTED_PORTS; do
    ss -ltnpH 2>/dev/null | grep -E ":${port}\b" | grep -q "pid=${pid}," && return 0
  done
  return 1
}

# ---------- 0. 실행 맥락 가드 ----------
CTX_BAD=false
if worker_anc="$(ancestor_with_comm $$ agy || ancestor_with_comm $$ codex)"; then
  add WARN "실행 맥락" "agy/codex 워커(PID $worker_anc) 안에서 실행 중 — 격리 실험 누수 위험. --apply 거부"
  CTX_BAD=true
fi
if [[ "$CALLER_ACC_RUNTIME" == /tmp/* || -n "${TESTMARK:-}${ACC_TEST:-}" ]]; then
  add WARN "실행 맥락" "테스트/격리 환경 변수 감지(ACC_RUNTIME=$CALLER_ACC_RUNTIME) — --apply 거부"
  CTX_BAD=true
fi
real_home="$(getent passwd "$(id -u)" | cut -d: -f6)"
if [[ "${HOME:-}" != "$real_home" ]]; then
  add WARN "실행 맥락" "HOME=$HOME (실제 $real_home 아님) — 격리 래퍼 안으로 보임. --apply 거부"
  CTX_BAD=true
fi
my_claude="$(ancestor_with_comm $$ claude || true)"
if [[ -n "$my_claude" ]]; then
  if [[ -z "$(env_of "$my_claude" ACC_RUNTIME)" ]]; then
    add INFO "실행 맥락" "이 스크립트를 부른 Claude(PID $my_claude, $(tmux_loc_of "$my_claude"))는 브리지 없음 → 완료 알림 못 받음(시나리오 c)"
  fi
fi

# ---------- 1. 도구 ----------
tools=""
for t in tmux claude agy codex python3; do
  if command -v "$t" >/dev/null 2>&1; then
    case $t in
      tmux) v="$(tmux -V)"; v="${v##* }" ;;
      claude) v="$(claude --version 2>/dev/null | awk '{print $1}')" ;;
      *) v="$("$t" --version 2>/dev/null | head -n1)"; v="${v##* }" ;;
    esac
    tools+="$t=$v "
  else
    if [[ $t == agy || $t == codex ]]; then add WARN "도구" "$t 없음"; else add FAIL "도구" "$t 없음"; fi
  fi
done
add PASS "도구" "$tools"

# ---------- 2. 라이브 세션/런타임 해석 (tmux 가 정본, ls -td 금지) ----------
v2_sessions=()
while read -r s; do
  [[ -z "$s" ]] && continue
  [[ "$(tmux show-environment -t "$s" ACC_BOOTSTRAP_VERSION 2>/dev/null)" == "ACC_BOOTSTRAP_VERSION=2" ]] && v2_sessions+=("$s")
done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null)

SESSION="${OPT_SESSION:-}"
if [[ -z "$SESSION" ]]; then
  if (( ${#v2_sessions[@]} == 1 )); then SESSION="${v2_sessions[0]}"
  elif (( ${#v2_sessions[@]} == 0 )); then SESSION=""
  else add FAIL "세션" "v2 세션이 여러 개(${v2_sessions[*]}) — --session 으로 지정 필요"; fi
fi
RT=""
if [[ -n "$SESSION" ]]; then
  RT="${OPT_RUNTIME:-$(tmux show-environment -t "$SESSION" ACC_RUNTIME 2>/dev/null | sed -n 's/^ACC_RUNTIME=//p')}"
  if [[ -n "$RT" && -f "$RT/bootstrap_version" ]] && grep -q '^bootstrap_version=2' "$RT/bootstrap_version" \
     && [[ "$(head -n1 "$RT/session_name" 2>/dev/null)" == "$SESSION" ]]; then
    add PASS "세션/런타임" "$SESSION → ${RT/#$HERE\//}"
  else
    add FAIL "세션/런타임" "$SESSION 의 ACC_RUNTIME($RT) 이 v2 마커/세션명과 안 맞음"; RT=""
  fi
elif (( ${#v2_sessions[@]} == 0 )); then
  add FAIL "세션/런타임" "살아있는 v2 세션 없음 → 먼저 bootstrap-v2.sh (시나리오 b, 이 스크립트는 안 띄움)"
fi

# ---------- 3. 설정 드리프트 ----------
if [[ -n "$SESSION" && "$CFG_SESSION_NAME" != "$SESSION" ]]; then
  add WARN "config 드리프트" "config.env SESSION_NAME=$CFG_SESSION_NAME, 라이브=$SESSION (dispatch 오조준 위험)"
fi
HANDOFF="${OPT_HANDOFF:-$CFG_HANDOFF_FILE}"
if [[ -z "$HANDOFF" ]]; then
  if [[ -f "$PROJECT/docs/RESUME_HANDOFF.md" ]]; then HANDOFF="docs/RESUME_HANDOFF.md"; fi
  add WARN "config 드리프트" "HANDOFF_FILE 비어 있음 (임시로 ${HANDOFF:-없음} 사용)"
fi
[[ -n "$HANDOFF" && "$HANDOFF" != /* ]] && HANDOFF="$PROJECT/$HANDOFF"
if [[ -n "$HANDOFF" && -f "$HANDOFF" ]]; then add PASS "인계 파일" "$HANDOFF"
else add FAIL "인계 파일" "없음: ${HANDOFF:-<미지정>} (프로젝트 $PROJECT)"; fi
if [[ -n "$RT" ]]; then
  rt_hash="${RT##*-}"; proj_hash="$(printf '%s' "$PROJECT" | md5sum | cut -c1-8)"
  if [[ "$rt_hash" != "$proj_hash" ]]; then
    gpd="$(tmux show-environment -g PROJECT_DIR 2>/dev/null | sed -n 's/^PROJECT_DIR=//p')"
    add WARN "프로젝트↔런타임" "런타임은 다른 디렉터리(${gpd:-?}) 기준으로 만들어짐. 새 감독관 cwd=$PROJECT 는 ACC_RUNTIME 명시로만 연결됨 (워커 창 cwd 는 그대로)"
  fi
  brief_sess="$(grep -m1 -o '세션 `[^`]*`' "$HERE/logs/session_brief.md" 2>/dev/null | tr -d '`' | awk '{print $2}')"
  if [[ "$brief_sess" != "$SESSION" ]]; then
    add WARN "브리핑 파일" "logs/session_brief.md 가 다른 세션($brief_sess) 것으로 덮어써짐 → 그대로 쓰면 안 됨. 런타임 전용 브리핑을 새로 생성해 사용"
  fi
fi

# ---------- 4. agy Stop 훅 (키 이름이 아니라 스크립트 경로로 판정) ----------
HOOKS="$HOME/.gemini/config/hooks.json"
hook_key="$(python3 - "$HOOKS" "$HERE/bin/agy-stop-hook.sh" <<'PY' 2>/dev/null
import json, sys
try: d = json.load(open(sys.argv[1]))
except Exception: sys.exit(1)
for k, v in d.items():
    for h in (v.get("Stop") or []) if isinstance(v, dict) else []:
        if h.get("command", "").split()[0:1] == [sys.argv[2]]:
            print(k); sys.exit(0)
sys.exit(1)
PY
)"
if [[ -n "$hook_key" ]]; then
  note=""; [[ "$hook_key" != "agent-command-chain-bridge" ]] && note=" (키 이름 '$hook_key' — 런북 표기와 다르지만 동작 동일)"
  add PASS "agy Stop 훅" "등록됨$note"
else
  add FAIL "agy Stop 훅" "$HOOKS 에 $HERE/bin/agy-stop-hook.sh 가 Stop 훅으로 없음"
fi

# ---------- 5. codex 모델 / 쿼터 ----------
if m="$(env -i PATH="$PATH" HOME="$HOME" "$HERE/bin/resolve-model.sh" sol 2>/dev/null)" && [[ -n "$m" ]]; then
  add PASS "codex 모델" "resolve-model sol → $m"
else
  add WARN "codex 모델" "resolve-model.sh sol 실패 → codex 대신 agy 사용"
fi
if [[ -n "$RT" ]]; then
  last_codex="$(ls -1d "$RT"/tasks/codex-* 2>/dev/null | sort | tail -n1)"
  if [[ -n "$last_codex" ]] && lim="$(tail -c 4000 "$last_codex/output.log" 2>/dev/null | grep -m1 -oE "hit your usage limit.*try again at [^.]*")"; then
    add WARN "codex 쿼터" "최근 codex 작업이 사용 한도로 실패(${lim##*try again at }까지) → 그동안 agy 로 폴백"
  else
    add PASS "codex 쿼터" "최근 codex 작업 로그에 한도 초과 흔적 없음"
  fi
fi

# ---------- 6. 런타임 상태: FIFO / 워치독 / 리스 / 이벤트 ----------
dry_run() {  # 깨끗한 환경에서만 실행 (호출자 env 상속 차단)
  env -i PATH="$PATH" HOME="$HOME" SESSION_NAME="$SESSION" ACC_RUNTIME="$RT" \
    "$HERE/bin/dispatch.sh" "$1" --dry-run >/dev/null 2>&1
}
if [[ -n "$RT" ]]; then
  [[ -p "$RT/event.fifo" ]] && add PASS "FIFO" "event.fifo 존재" || add FAIL "FIFO" "event.fifo 없음"
  wpid="$(cat "$RT/watchdog.pid" 2>/dev/null || true)"
  if [[ -n "$wpid" ]] && [[ "$(cmdline_of "$wpid")" == *watchdog-v2.sh* ]] && [[ "$(env_of "$wpid" ACC_RUNTIME)" == "$RT" ]]; then
    add PASS "워치독" "PID $wpid (60초마다 sonnet 창이 셸이면 자동 재기동함)"
  else
    add FAIL "워치독" "watchdog-v2 미가동/런타임 불일치 (watchdog.pid=${wpid:-없음})"
  fi
  busy="$(cd "$RT/workers" 2>/dev/null && ls -1 ./*.busy ./*.lock 2>/dev/null | tr '\n' ' ')"
  running="$(grep -l '^status=running' "$RT"/tasks/*/state 2>/dev/null | awk -F/ '{print $(NF-1)}' | tr '\n' ' ')"
  if [[ -z "$busy$running" ]]; then add PASS "워커 점유" "busy/lock 없음, 실행 중 작업 없음"
  else add WARN "워커 점유" "busy/lock: ${busy:-없음} / running: ${running:-없음} (stale 이면 task-abandon.sh)"; fi
  npend="$(find "$RT/events/pending" -maxdepth 1 -name '*.evt' 2>/dev/null | wc -l)"
  ninfl="$(find "$RT/events/inflight" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)"
  if (( npend + ninfl == 0 )); then add PASS "이벤트 큐" "pending 0, inflight 0"
  else add WARN "이벤트 큐" "pending $npend, inflight $ninfl (ack 안 된 배치가 있으면 교체 전 처리)"; fi
  for w in agy codex; do
    if dry_run "$w"; then add PASS "dispatch dry-run" "$w → exit 0"
    else rc=$?; add WARN "dispatch dry-run" "$w → exit $rc (75=점유, 71=창 상태, 70=세션/마커)"; fi
  done
fi
if env -i PATH="$PATH" HOME="$HOME" "$HERE/dashboard/run.sh" status >/dev/null 2>&1; then
  add PASS "대시보드" "실행 중"
else
  add WARN "대시보드" "꺼져 있음 (--apply --start-dashboard 로 기동)"
fi

# ---------- 7. 감독관 Claude / 대기자(waiter) / 세션 목록 ----------
BRIDGED=()   # 이 런타임에 붙은 감독관 PID
declare -A CL_LOC=()
for p in $(pgrep -x claude 2>/dev/null); do
  loc="$(tmux_loc_of "$p")"; CL_LOC[$p]="$loc"
  rt_env="$(env_of "$p" ACC_RUNTIME)"; cl="$(cmdline_of "$p")"
  if [[ -n "$RT" && "$rt_env" == "$RT" && "$cl" == *"$RT/claude-bridge.settings.json"* ]]; then
    BRIDGED+=("$p")
    add INFO "감독관(브리지)" "PID $p @ $loc, 시작 $(ps -o lstart= -p "$p" | xargs)"
  elif [[ "$cl" == *claude-bridge.settings.json* ]]; then
    add WARN "감독관(타 런타임)" "PID $p @ $loc (다른 런타임 브리지)"
  else
    add INFO "일반 Claude" "PID $p @ $loc — 브리지 없음(완료 알림 못 받음)"
  fi
done
WAITERS=() LOCK_HOLDER=""
for d in /proc/[0-9]*; do
  p="${d#/proc/}"
  c="$(cmdline_of "$p")" || continue
  [[ "$c" == bash*sonnet-event-wait.sh* && ( -z "$RT" || "$c" == *"$RT"* ) ]] || continue
  WAITERS+=("$p")
  [[ -n "$RT" && "$(readlink "/proc/$p/fd/9" 2>/dev/null)" == "$RT/listener.lock" ]] && LOCK_HOLDER="$p"
done
if [[ -n "$RT" ]]; then
  if (( ${#WAITERS[@]} == 1 )) && [[ -n "$LOCK_HOLDER" ]]; then
    owner="$(ancestor_with_comm "$LOCK_HOLDER" claude || echo 없음)"
    add PASS "FIFO 대기자" "1개(PID $LOCK_HOLDER, 주인 Claude PID $owner)"
  elif (( ${#WAITERS[@]} == 0 )); then
    add WARN "FIFO 대기자" "없음 — 지금 완료 알림을 받을 감독관이 없음"
  else
    add FAIL "FIFO 대기자" "${#WAITERS[@]}개(${WAITERS[*]}) — 소비자 경합"
  fi
fi
if [[ -n "$CALLER_ACC_RUNTIME$CALLER_SESSION_NAME" ]] && [[ "$CALLER_ACC_RUNTIME" != "${RT:-}" || "${CALLER_SESSION_NAME:-$SESSION}" != "$SESSION" ]]; then
  add WARN "호출자 환경" "상속된 ACC_RUNTIME/SESSION_NAME(${CALLER_ACC_RUNTIME:--}/${CALLER_SESSION_NAME:--})이 라이브와 다름 — 이 스크립트는 무시하지만 이 셸에서 다른 도구를 돌리면 오조준 위험"
fi
if (( ${#BRIDGED[@]} > 1 )); then add FAIL "감독관 수" "브리지 감독관 ${#BRIDGED[@]}명 — 이벤트를 서로 뺏음"; fi
while read -r s; do
  [[ -z "$s" ]] && continue
  if is_protected_loc "$s:"; then add INFO "tmux 세션" "$s — 보호 대상(운영). 이 스크립트는 절대 건드리지 않음"
  elif [[ "$s" == "$SESSION" ]]; then :
  elif [[ " ${v2_sessions[*]} " == *" $s "* ]]; then add WARN "tmux 세션" "$s — 추가 v2 세션"
  else add INFO "tmux 세션" "$s — v2 아님(구버전/일반). 자동 정리 안 함"
  fi
done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null)

# ---------- 보고 ----------
report() {
  say "== 감독관 세션 점검 ($(date '+%F %T'), 모드: $MODE) =="
  if (( NFAIL > 0 )); then
    say "결론: 지금은 교체하면 안 됩니다. 아래 FAIL ${NFAIL}건을 먼저 고치세요."
  elif (( ${#BRIDGED[@]} == 1 )); then
    say "결론: 교체 가능. 단, 옛 감독관(PID ${BRIDGED[0]}, ${CL_LOC[${BRIDGED[0]}]})이 알림 통로를 쥐고 있어"
    say "      먼저 정리해야 합니다 → session-init.sh --apply --retire-old-supervisor"
  elif (( ${#BRIDGED[@]} == 0 )); then
    say "결론: 바로 새 감독관을 띄울 수 있습니다 → session-init.sh --apply"
  fi
  say "새 감독관 위치: ${SESSION:-?}:${SONNET_WINDOW} / 작업 폴더: $PROJECT / 주의 ${NWARN}건"
  say ""
  print_rows
}

if [[ "$MODE" == check ]]; then
  report
  (( NFAIL == 0 )) && exit 0 || exit 1
fi

# =====================================================================
# --apply
# =====================================================================
report; say ""
die() { say "중단: $*"; exit 1; }
$CTX_BAD && die "격리/워커 맥락에서는 --apply 를 하지 않습니다. 일반 터미널에서 다시 실행하세요."
(( NFAIL == 0 )) || die "FAIL 항목이 있어 적용하지 않습니다."
(( ${#BRIDGED[@]} <= 1 )) || die "브리지 감독관이 2명 이상 — 수동 판단 필요."
is_protected_loc "$SESSION:" && die "보호 세션은 대상이 될 수 없습니다."

RESULT=()
res() { RESULT+=("$1|$2|$3"); return 0; }
pane_id() { tmux display-message -p -t "$SESSION:$SONNET_WINDOW" '#{pane_id}' 2>/dev/null; }
pane_cmd() { tmux display-message -p -t "$SESSION:$SONNET_WINDOW" '#{pane_current_command}' 2>/dev/null; }
is_shell() { case "$1" in bash|zsh|sh|-bash|-zsh|-sh) return 0;; *) return 1;; esac; }
alive_same() { [[ -d "/proc/$1" && "$(start_tok "$1")" == "$2" ]]; }
MARK="$RT/supervisor.rotating"

# --- A. 옛 감독관 은퇴 (명시 플래그 필요) ---
if (( ${#BRIDGED[@]} == 1 )); then
  OLD="${BRIDGED[0]}"; OLD_TOK="$(start_tok "$OLD")"; OLD_LOC="${CL_LOC[$OLD]}"
  $RETIRE || die "옛 감독관(PID $OLD @ $OLD_LOC)이 살아 있습니다. 은퇴시키려면 --retire-old-supervisor 를 붙이세요."
  is_protected_loc "$OLD_LOC" && die "옛 감독관이 보호 세션 안에 있음 — 손대지 않음."
  pid_listens_protected_port "$OLD" && die "옛 감독관이 보호 포트를 열고 있음 — 손대지 않음."
  ninfl="$(find "$RT/events/inflight" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)"
  (( ninfl == 0 )) || die "ack 안 된 inflight 배치 ${ninfl}개 — 옛 감독관이 먼저 ack 하게 하세요."
  old_waiters=(); for w in "${WAITERS[@]}"; do
    [[ "$(ancestor_with_comm "$w" claude)" == "$OLD" ]] && old_waiters+=("$w:$(start_tok "$w")")
  done
  old_pane="${OLD_LOC##*(}"; old_pane="${old_pane%)}"
  printf 'pid=%s\nstarted=%s\n' "$$" "$(date +%s)" >"$MARK"   # (워치독 패치용 표식, 패치 전엔 무해)
  trap 'rm -f "$MARK"' EXIT
  say "[1/4] 옛 감독관에 /exit 전송 (PID $OLD, pane $old_pane) — 대화는 claude --resume 으로 복구 가능"
  tmux send-keys -t "$old_pane" -l "/exit" && tmux send-keys -t "$old_pane" Enter
  for _ in $(seq 1 30); do alive_same "$OLD" "$OLD_TOK" || break; sleep 1; done
  if alive_same "$OLD" "$OLD_TOK"; then
    say "      30초 내 종료 안 됨 → 정확한 PID $OLD 에 SIGTERM"
    kill -TERM "$OLD" 2>/dev/null
    for _ in $(seq 1 15); do alive_same "$OLD" "$OLD_TOK" || break; sleep 1; done
  fi
  alive_same "$OLD" "$OLD_TOK" && die "옛 감독관이 SIGTERM 후에도 살아 있음 (SIGKILL 은 하지 않음). 수동 확인 필요."
  res PASS "옛 감독관 종료" "PID $OLD"
  for wt in "${old_waiters[@]}"; do
    w="${wt%%:*}"; tok="${wt#*:}"
    if alive_same "$w" "$tok"; then kill -TERM "$w" 2>/dev/null; sleep 1; fi
    if alive_same "$w" "$tok"; then res FAIL "옛 대기자 정리" "PID $w 생존"; else res PASS "옛 대기자 정리" "PID $w 종료"; fi
  done
fi

if $NO_LAUNCH; then
  say "--no-launch: 새 감독관은 띄우지 않습니다."
else
  # --- B. 런타임 전용 브리핑 + 첫 프롬프트 + 기동 스크립트 생성 ---
  BRIEF="$RT/supervisor_brief.md"; FIRST="$RT/supervisor_first_prompt.txt"; LAUNCH="$RT/supervisor-launch.sh"
  cat >"$BRIEF" <<EOF
# 감독관 브리핑 (session-init.sh 생성, $(date '+%F %T'))
너(Claude)는 tmux 세션 \`$SESSION\`의 상위 감독관이다. 워커 창: \`$AGY_WINDOW\`(agy 상주), codex(oneshot).
작업 프로젝트: \`$PROJECT\`. 런타임: \`$RT\`. 템플릿: \`$HERE\`.
- 작업 지시는 오직: SESSION_NAME=$SESSION ACC_RUNTIME=$RT $HERE/bin/dispatch.sh <agy|codex> --prompt-file <파일>
- 디스패치 프롬프트에는 매번 직접 적는다(자동화 안 됨): knowledge_refs / learning_capsule / 마지막 줄 [[DONE <task_id>]] result=… | learning=…
- 완료 알림은 [ACC_EVENT_BATCH] 로 자동 도착한다. tmux capture-pane 반복 폴링 금지(필요 시 3~5분 이상 간격, 1회성, 자동 재무장 금지).
- 알림 처리 후 안내된 event-ack.sh 명령으로 ack 한다. 워커 자기보고는 output.log 와 git diff 로 직접 검증한다.
- 사용자의 명시 지시 전 dispatch / bootstrap-v2.sh --restart / git push 금지. 논의는 승인이 아니다.
- tmux 세션 \`${PROTECTED_SESSIONS}\` 와 포트 ${PROTECTED_PORTS} 는 운영 서버 — 지시 없이 절대 건드리지 않는다.
EOF
  cat >"$FIRST" <<EOF
세션 인수인계를 시작한다. 1) $HANDOFF 를 읽어라. 2) $HERE/.llmwiki/session-resume.md 의 §6, §7 을 읽어라.
3) 한국어 3~5줄로 보고하라: 현재 상태 / 다음 후보 작업 2~3개 / 내가 결정해야 할 것.
내 명시 지시가 있기 전에는 dispatch.sh 실행, 파일 수정, git 작업을 하지 마라.
EOF
  {
    printf '#!/usr/bin/env bash\n# session-init.sh 가 생성. 이 파일만 실행하면 같은 감독관을 다시 띄울 수 있음.\n'
    printf 'cd %q || exit 1\n' "$PROJECT"
    printf 'exec env CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 ACC_RUNTIME=%q ACC_TEMPLATE_ROOT=%q SESSION_NAME=%q \\\n' "$RT" "$HERE" "$SESSION"
    printf '  %s --settings %q --add-dir %q \\\n' "$SONNET_CMD" "$RT/claude-bridge.settings.json" "$HERE"
    printf '  --append-system-prompt "$(cat %q)" "${1:-$(cat %q)}"\n' "$BRIEF" "$FIRST"
  } >"$LAUNCH"
  chmod 700 "$LAUNCH"

  # --- C. sonnet 창에서 기동 ---
  if ! tmux list-windows -t "$SESSION" -F '#{window_name}' | grep -qx "$SONNET_WINDOW"; then
    tmux new-window -d -t "$SESSION" -n "$SONNET_WINDOW" -c "$PROJECT"; sleep 1
  fi
  pid_pane="$(pane_id)"
  for _ in $(seq 1 10); do is_shell "$(pane_cmd)" && break; sleep 1; done
  if is_shell "$(pane_cmd)"; then
    say "[2/4] $SESSION:$SONNET_WINDOW 에서 새 감독관 기동"
    tmux send-keys -t "$pid_pane" -l "bash $(printf '%q' "$LAUNCH")" && tmux send-keys -t "$pid_pane" Enter
    launched_by=us
  else
    say "[2/4] 창이 이미 '$(pane_cmd)' 실행 중 (워치독이 먼저 재기동했을 수 있음) → 기동 생략, 검증만 수행"
    launched_by=other
  fi
  if [[ "$AUTO_CONFIRM_TRUST" == true && $launched_by == us ]]; then
    for _ in $(seq 1 20); do   # 기동 직후 1회성 신뢰 대화상자 처리(bootstrap 과 동일), 작업 폴링 아님
      if tmux capture-pane -p -t "$pid_pane" 2>/dev/null | grep -qF "trust this folder"; then
        tmux send-keys -t "$pid_pane" Down Enter; break
      fi
      sleep 1
    done
  fi

  # --- D. 기동 후 검증 ---
  say "[3/4] 검증 (최대 60초)"
  NEW=""
  pane_root="$(tmux display-message -p -t "$pid_pane" '#{pane_pid}')"
  for _ in $(seq 1 60); do
    for p in $(pgrep -x claude); do
      [[ "${OLD:-}" == "$p" ]] && continue
      a="$p"; while [[ -n "$a" && "$a" -gt 1 && "$a" != "$pane_root" ]]; do a="$(ppid_of "$a")"; done
      [[ "$a" == "$pane_root" ]] && NEW="$p"
    done
    if [[ -n "$NEW" ]]; then
      h="$(readlink "/proc/$(cat "$RT/listener.pid" 2>/dev/null)/fd/9" 2>/dev/null)"
      [[ "$h" == "$RT/listener.lock" ]] && break
    fi
    sleep 1
  done
  if [[ -n "$NEW" ]]; then
    res PASS "새 감독관" "PID $NEW @ $SESSION:$SONNET_WINDOW"
    [[ "$(cmdline_of "$NEW")" == *"--settings $RT/claude-bridge.settings.json"* ]] && res PASS "브리지 플래그" "--settings OK" || res FAIL "브리지 플래그" "cmdline 에 --settings 없음"
    for kv in "ACC_RUNTIME=$RT" "ACC_TEMPLATE_ROOT=$HERE" "SESSION_NAME=$SESSION" "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1"; do
      [[ "$(env_of "$NEW" "${kv%%=*}")" == "${kv#*=}" ]] && res PASS "환경변수" "$kv" || res FAIL "환경변수" "${kv%%=*} 불일치"
    done
    [[ $launched_by == other ]] && res WARN "첫 프롬프트" "워치독이 띄운 감독관 — 인계 프롬프트 미주입. 직접 붙여넣기: $FIRST"
  else
    res FAIL "새 감독관" "60초 안에 sonnet 창에서 claude 를 못 찾음"
  fi
  holders=0; owner=""
  for d in /proc/[0-9]*; do
    p="${d#/proc/}"
    [[ "$(readlink "$d/fd/9" 2>/dev/null)" == "$RT/listener.lock" ]] || continue
    holders=$((holders+1)); owner="$(ancestor_with_comm "$p" claude)"
  done
  if (( holders == 1 )) && [[ -n "$NEW" && "$owner" == "$NEW" ]]; then res PASS "FIFO 대기자" "정확히 1개, 주인=새 감독관"
  else res FAIL "FIFO 대기자" "보유자 ${holders}개, 주인=${owner:-없음}"; fi
  if dry_run agy; then res PASS "dispatch dry-run" "agy exit 0"; else res FAIL "dispatch dry-run" "agy exit $?"; fi
  wpid="$(cat "$RT/watchdog.pid" 2>/dev/null)"; [[ -n "$wpid" && -d /proc/$wpid ]] && res PASS "워치독" "PID $wpid" || res FAIL "워치독" "없음"
fi

if $START_DASH; then
  say "[4/4] 대시보드 기동"
  if env -i PATH="$PATH" HOME="$HOME" "$HERE/dashboard/run.sh" status >/dev/null 2>&1; then res PASS "대시보드" "이미 실행 중"
  elif env -i PATH="$PATH" HOME="$HOME" "$HERE/dashboard/run.sh" start --runtime "$RT" >/dev/null 2>&1; then res PASS "대시보드" "기동"
  else res FAIL "대시보드" "기동 실패 (logs/dashboard.log)"; fi
fi

say ""; say "== 적용 결과 =="
nf=0; for r in "${RESULT[@]}"; do IFS='|' read -r s k v <<<"$r"; printf '  %-4s  %-16s %s\n' "$s" "$k" "$v"; [[ $s == FAIL ]] && nf=$((nf+1)); done
if (( nf == 0 )); then say "결론: 새 감독관이 브리지에 정상 연결됐습니다. tmux attach -t $SESSION 후 $SONNET_WINDOW 창에서 대화하세요."
else say "결론: ${nf}건 실패 — 위 표를 확인하세요. (옛 대화는 해당 폴더에서 claude --resume 으로 복구 가능)"; exit 1; fi
