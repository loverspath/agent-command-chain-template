#!/usr/bin/env bash
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$HERE/config.env" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/config.env"
fi

session_safe="$(printf '%s' "${SESSION_NAME:-agentchain}" | tr -cd '[:alnum:]_-')"
proj_hash="$(printf '%s' "${PROJECT_DIR:-$PWD}" | md5sum | cut -c1-8)"
default_runtime="$HERE/runtime/${session_safe}-${proj_hash}"
if [[ $# -eq 1 ]]; then
  runtime="${ACC_RUNTIME:-$default_runtime}"
  batch="$1"
elif [[ $# -ge 2 ]]; then
  runtime="$1"
  batch="$2"
else
  echo "Usage: $0 [runtime] <batch> or $0 <batch>" >&2
  exit 64
fi

case "$batch" in *[!0-9]*) echo "Invalid batch format: $batch" >&2; exit 64 ;; esac

src="$runtime/events/inflight/$batch"
dst="$runtime/events/archive/$batch"

mkdir -p "$runtime/events/archive"

if [[ -d "$src" ]]; then
  mv "$src" "$dst"
fi

rm -f "$runtime/events/batch-$batch.txt"

# Retention policy: 7일 초과 아카이브 정리
find "$runtime/events/archive" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true

# Retention policy: 최대 1,000개 초과 시 오래된 배치 정리 (공백 안전 null 구분 처리)
total_archives=$(find "$runtime/events/archive" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
if [[ "$total_archives" -gt 1000 ]]; then
  to_delete=$(( total_archives - 1000 ))
  while IFS= read -r -d '' entry; do
    dir="${entry#* }"
    [[ -n "$dir" ]] && rm -rf "$dir"
  done < <(find "$runtime/events/archive" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\0' 2>/dev/null | sort -z -n | head -z -n "$to_delete")
fi
