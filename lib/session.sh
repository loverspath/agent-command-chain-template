#!/usr/bin/env bash
# lib/session.sh: tmux 세션명 자동 해석 및 v2 런타임/마커 검증 공통 라이브러리
#
# 세션명 해석 우선순위:
#   ① 환경변수 SESSION_NAME
#   ② 런타임 마커 ($ACC_RUNTIME/session_name 파일; bootstrap-v2.sh가 기동 시 기록)
#   ③ 현재 tmux 안이면 `tmux display -p '#S'` (단 그 세션에 대상 윈도우가 있고 v2 마커가 있을 때)
#   ④ config.env 기본값

# Session 내 특정 윈도우 존재 여부 검사
session_has_window() {
  local sess="$1"
  local win="$2"
  [[ -z "$sess" || -z "$win" ]] && return 1
  tmux list-windows -t "$sess" -F '#{window_name}' 2>/dev/null | grep -qx "$win"
}

# 세션이 v2 Full-Push 마커를 보유하고 있는지 검사
# 마커 판별 기준:
#   1. tmux 세션 환경변수 ACC_BOOTSTRAP_VERSION=2
#   2. tmux 세션의 ACC_RUNTIME 경로 내 bootstrap_version=2
#   3. 세션명 기반 candidate runtime 디렉토리 내 bootstrap_version=2
#   4. 현재 ACC_RUNTIME 내 session_name 이 세션명과 일치하고 bootstrap_version=2
session_has_v2_marker() {
  local sess="$1"
  local proj_dir="${2:-${PROJECT_DIR:-$PWD}}"
  local base_here="${3:-${HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
  [[ -z "$sess" ]] && return 1

  # 1. tmux 세션 환경변수 확인
  local env_marker
  env_marker="$(tmux show-environment -t "$sess" ACC_BOOTSTRAP_VERSION 2>/dev/null || true)"
  if [[ "$env_marker" == "ACC_BOOTSTRAP_VERSION=2" ]]; then
    return 0
  fi

  # 2. tmux 세션의 ACC_RUNTIME 경로 확인
  local sess_runtime
  sess_runtime="$(tmux show-environment -t "$sess" ACC_RUNTIME 2>/dev/null | sed -n 's/^ACC_RUNTIME=//p')"
  if [[ -n "$sess_runtime" && -f "$sess_runtime/bootstrap_version" ]] && grep -q '^bootstrap_version=2' "$sess_runtime/bootstrap_version" 2>/dev/null; then
    return 0
  fi

  # 3. 프로젝트 해시 기반 기본 런타임 디렉토리 검사
  local sess_safe proj_hash cand_runtime
  sess_safe="$(printf '%s' "$sess" | tr -cd '[:alnum:]_-')"
  proj_hash="$(printf '%s' "$proj_dir" | md5sum | cut -c1-8)"
  cand_runtime="$base_here/runtime/${sess_safe}-${proj_hash}"
  if [[ -f "$cand_runtime/bootstrap_version" ]] && grep -q '^bootstrap_version=2' "$cand_runtime/bootstrap_version" 2>/dev/null; then
    return 0
  fi

  # 4. ACC_RUNTIME 환경변수가 설정되어 있고 해당 디렉토리의 session_name이 일치하는 경우
  local cur_runtime="${ENV_ACC_RUNTIME:-${ACC_RUNTIME:-}}"
  if [[ -n "$cur_runtime" && -f "$cur_runtime/session_name" ]]; then
    local r_sess
    r_sess="$(head -n 1 "$cur_runtime/session_name" 2>/dev/null | tr -d '\r\n')"
    if [[ "$r_sess" == "$sess" && -f "$cur_runtime/bootstrap_version" ]] && grep -q '^bootstrap_version=2' "$cur_runtime/bootstrap_version" 2>/dev/null; then
      return 0
    fi
  fi

  return 1
}

# v2 마커를 가진 활성 후보 tmux 세션 목록 탐색
# 반환: stdout으로 1줄당 1개 세션명 출력
find_v2_candidate_sessions() {
  local target_win="${1:-}"
  local exclude_sess="${2:-}"
  local proj_dir="${PROJECT_DIR:-$PWD}"
  local base_here="${HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

  local s_name
  while IFS= read -r s_name; do
    [[ -z "$s_name" || "$s_name" == "$exclude_sess" ]] && continue
    if session_has_v2_marker "$s_name" "$proj_dir" "$base_here"; then
      if [[ -z "$target_win" ]] || session_has_window "$s_name" "$target_win"; then
        printf '%s\n' "$s_name"
      fi
    fi
  done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null || true)
}

# 세션명 및 런타임 자동 해석 메인 함수
# 사용법: resolve_session_and_runtime <caller_name> [target_window] [silent]
# 출력: 해석된 세션명과 출처를 stderr로 1줄 출력 (예: [dispatch] session=agentchain-v2 (source=runtime-marker))
# 설정되는 전역 변수:
#   SESSION_NAME: 해석된 세션명
#   SESSION_SOURCE: 세션명 출처 ('env', 'runtime-marker', 'tmux-current', 'config-default')
#   ACC_RUNTIME: 유효한 런타임 디렉토리 절대경로
resolve_session_and_runtime() {
  local caller_name="${1:-${0##*/}}"
  caller_name="${caller_name%.sh}"
  local target_win="${2:-}"
  local silent="${3:-false}"

  local base_here="${HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  local proj_dir="${PROJECT_DIR:-$PWD}"

  local resolved_session=""
  local resolved_source=""

  # ① 환경변수 SESSION_NAME
  local raw_env_session="${ENV_SESSION_NAME:-${_ENV_SESSION_NAME:-}}"
  if [[ -z "$raw_env_session" && -n "${SESSION_NAME:-}" && "${SESSION_NAME:-}" != "${CONFIG_SESSION_NAME:-agentchain}" ]]; then
    raw_env_session="$SESSION_NAME"
  fi

  if [[ -n "$raw_env_session" ]]; then
    resolved_session="$raw_env_session"
    resolved_source="env"
  fi

  # ② 런타임 마커 ($ACC_RUNTIME/session_name)
  if [[ -z "$resolved_session" ]]; then
    local check_runtime="${ENV_ACC_RUNTIME:-${ACC_RUNTIME:-}}"
    if [[ -n "$check_runtime" && -f "$check_runtime/session_name" ]]; then
      local m_sess
      m_sess="$(head -n 1 "$check_runtime/session_name" 2>/dev/null | tr -d '\r\n')"
      if [[ -n "$m_sess" ]]; then
        resolved_session="$m_sess"
        resolved_source="runtime-marker"
      fi
    fi
  fi

  # ③ 현재 tmux 안이면 tmux display -p '#S' (단 대상 윈도우가 있고 v2 마커가 있을 때)
  if [[ -z "$resolved_session" ]]; then
    local cur_tmux
    cur_tmux="$(tmux display -p '#S' 2>/dev/null || true)"
    if [[ -n "$cur_tmux" ]]; then
      local win_ok=true
      if [[ -n "$target_win" ]] && ! session_has_window "$cur_tmux" "$target_win"; then
        win_ok=false
      fi
      if [[ "$win_ok" == "true" ]] && session_has_v2_marker "$cur_tmux" "$proj_dir" "$base_here"; then
        resolved_session="$cur_tmux"
        resolved_source="tmux-current"
      fi
    fi
  fi

  # ④ config.env 기본값
  if [[ -z "$resolved_session" ]]; then
    resolved_session="${CONFIG_SESSION_NAME:-${SESSION_NAME:-agentchain}}"
    resolved_source="config-default"
  fi

  # 해석된 세션명과 출처를 stderr로 1줄 출력
  if [[ "$silent" != "true" ]]; then
    echo "[$caller_name] session=$resolved_session (source=$resolved_source)" >&2
  fi

  # 런타임 디렉토리 산출
  local s_safe p_hash def_runtime
  s_safe="$(printf '%s' "$resolved_session" | tr -cd '[:alnum:]_-')"
  p_hash="$(printf '%s' "$proj_dir" | md5sum | cut -c1-8)"
  def_runtime="$base_here/runtime/${s_safe}-${p_hash}"

  local final_runtime=""
  if [[ "$resolved_source" == "runtime-marker" && -n "${ENV_ACC_RUNTIME:-${ACC_RUNTIME:-}}" ]]; then
    final_runtime="${ENV_ACC_RUNTIME:-$ACC_RUNTIME}"
  elif [[ -n "${ENV_ACC_RUNTIME:-}" && -f "${ENV_ACC_RUNTIME}/session_name" && "$(head -n 1 "${ENV_ACC_RUNTIME}/session_name" 2>/dev/null | tr -d '\r\n')" == "$resolved_session" ]]; then
    final_runtime="$ENV_ACC_RUNTIME"
  else
    final_runtime="$def_runtime"
  fi

  # 상속된 ACC_RUNTIME 이 다른 세션의 런타임이면 조용히 해시 재계산하지 않고 거부 (다중 체인 오염 차단)
  if [[ "$resolved_source" != "runtime-marker" && -n "${ENV_ACC_RUNTIME:-}" && -f "${ENV_ACC_RUNTIME}/session_name" ]]; then
    local inh_sess
    inh_sess="$(head -n 1 "${ENV_ACC_RUNTIME}/session_name" 2>/dev/null | tr -d '\r\n')"
    if [[ -n "$inh_sess" && "$inh_sess" != "$resolved_session" ]]; then
      echo "Error: ACC_RUNTIME(${ENV_ACC_RUNTIME})은 세션 '$inh_sess' 소속인데 해석된 세션은 '$resolved_session' 입니다. 다른 체인의 환경 상속 의심 — 거부." >&2
      exit 70
    fi
  fi

  # 작업 1.3: 해석된 세션에 v2 마커가 없고 다른 세션에 있으면 조용히 진행하지 않고 거부 및 후보 세션명 안내
  if ! session_has_v2_marker "$resolved_session" "$proj_dir" "$base_here"; then
    local candidates=()
    while IFS= read -r c_cand; do
      [[ -n "$c_cand" ]] && candidates+=("$c_cand")
    done < <(find_v2_candidate_sessions "$target_win" "$resolved_session")

    if (( ${#candidates[@]} > 0 )); then
      echo "Error: Target session '$resolved_session' lacks v2 Full-Push marker (missing bootstrap_version=2)," >&2
      echo "       but active v2 candidate session(s) detected: ${candidates[*]}" >&2
      echo "Please re-run with: SESSION_NAME=${candidates[0]} $0 ..." >&2
      exit 70
    fi
  fi

  SESSION_NAME="$resolved_session"
  SESSION_SOURCE="$resolved_source"
  ACC_RUNTIME="$final_runtime"
}
