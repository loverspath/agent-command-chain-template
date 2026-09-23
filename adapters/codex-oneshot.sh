#!/usr/bin/env bash
set -euo pipefail
umask 077

export PATH="$HOME/.local/bin:$PATH"

prompt_file="${1:-}"
if [[ -z "$prompt_file" || ! -f "$prompt_file" ]]; then
  echo "Usage: $0 <prompt_file>" >&2
  exit 64
fi

project_dir="${PROJECT_DIR:-$PWD}"

exec codex exec \
  --model "${CODEX_MODEL:-gpt-5.6-terra}" \
  -s danger-full-access \
  -C "$project_dir" \
  - <"$prompt_file"
