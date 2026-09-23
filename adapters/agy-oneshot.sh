#!/usr/bin/env bash
set -euo pipefail
umask 077

export PATH="$HOME/.local/bin:$PATH"

prompt_file="${1:-}"
if [[ -z "$prompt_file" || ! -f "$prompt_file" ]]; then
  echo "Usage: $0 <prompt_file>" >&2
  exit 64
fi

# ARG_MAX 방어를 위한 프롬프트 크기 사전 검증 (128KB 제한)
prompt_size="$(wc -c < "$prompt_file")"
if (( prompt_size > 131072 )); then
  echo "Error: Prompt file exceeds maximum allowed size (128KB) for agy CLI" >&2
  exit 65
fi

prompt="$(<"$prompt_file")"
exec agy --dangerously-skip-permissions -p "$prompt"
