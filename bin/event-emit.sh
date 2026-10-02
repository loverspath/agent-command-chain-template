#!/usr/bin/env bash
set -euo pipefail
umask 077

source_name="${1:-}"
kind="${2:-}"
task_id="${3:-}"
summary="${4:-}"
detail_path="${5:-}"
custom_id="${6:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ENV_SESSION_NAME="${SESSION_NAME:-}"
ENV_ACC_RUNTIME="${ACC_RUNTIME:-}"

# shellcheck disable=SC1091
source "$HERE/lib/config.sh"
acc_load_config "$HERE" || exit $?
CONFIG_SESSION_NAME="${SESSION_NAME:-agentchain}"

if [[ -z "$ENV_ACC_RUNTIME" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/lib/session.sh"
  resolve_session_and_runtime "event-emit" "" true
  runtime="$ACC_RUNTIME"
else
  runtime="$ENV_ACC_RUNTIME"
fi

pending="$runtime/events/pending"
fifo="$runtime/event.fifo"

case "$source_name" in agy|codex|watchdog|bridge) ;; *) echo "Invalid source: $source_name" >&2; exit 64 ;; esac
case "$kind" in started|done|error|canceled|stalled|process_exit|bridge_degraded) ;;
  *) echo "Invalid kind: $kind" >&2; exit 64 ;;
esac

if [[ -z "$task_id" ]]; then
  echo "task_id is required" >&2
  exit 64
fi

mkdir -p "$pending"

id="${custom_id:-$(date +%s%N)-${source_name}-$$}"

# 멱등성: pending, inflight, archive에 이미 동일 이벤트가 있으면 성공으로 종료
if [[ -f "$pending/$id.evt" ]] || \
   find "$runtime/events" -name "$id.evt" -print -quit 2>/dev/null | grep -q .; then
  if [[ -p "$fifo" ]]; then
    timeout 0.2 bash -c 'printf ".\n" >"$1"' _ "$fifo" >/dev/null 2>&1 || true
  fi
  exit 0
fi

# 요약 데이터 정제 (제어문자 제거, 개행문자 공백 치환, < > 무력화, 120자 단일행 강제)
clean_summary="$(printf '%s' "$summary" | tr -d '\000-\010\013\014\016-\037\177' | tr '\r\n\t' '   ' | tr '<>' '[]' | tr -s ' ' | cut -c 1-120)"
clean_path="$(printf '%s' "$detail_path" | tr -d '\r\n' | tr '<>' '[]')"

tmp="$pending/.event.$id.tmp"
summary_b64="$(printf '%s' "$clean_summary" | base64 -w0)"
path_b64="$(printf '%s' "$clean_path" | base64 -w0)"

{
  printf 'version=1\n'
  printf 'id=%s\n' "$id"
  printf 'source=%s\n' "$source_name"
  printf 'kind=%s\n' "$kind"
  printf 'task_id=%s\n' "$task_id"
  printf 'created_epoch=%s\n' "$(date +%s)"
  printf 'exit_code=%s\n' "${ACC_EXIT_CODE:-}"
  printf 'summary_b64=%s\n' "$summary_b64"
  printf 'detail_path_b64=%s\n' "$path_b64"
} >"$tmp"

if ! mv "$tmp" "$pending/$id.evt"; then
  echo "Error: Failed to move $tmp to $pending/$id.evt" >&2
  rm -f "$tmp" 2>/dev/null || true
  exit 1
fi

# Listener가 없더라도 emitter가 블로킹되지 않도록 timeout 보호
if [[ -p "$fifo" ]]; then
  timeout 0.2 bash -c 'printf ".\n" >"$1"' _ "$fifo" >/dev/null 2>&1 || true
fi
