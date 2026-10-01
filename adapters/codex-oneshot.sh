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

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${CODEX_MODEL:-}" ]]; then
  CODEX_MODEL="$("$HERE/bin/resolve-model.sh" "${CODEX_TIER:-sol}")" || {
    echo "Error: cannot resolve codex model for tier '${CODEX_TIER:-sol}'. Set CODEX_MODEL explicitly." >&2
    exit 65
  }
fi
echo "[codex-oneshot] model=$CODEX_MODEL tier=${CODEX_TIER:-sol} effort=${CODEX_EFFORT:-medium}"

exec codex exec \
  --model "$CODEX_MODEL" \
  -c model_reasoning_effort="${CODEX_EFFORT:-medium}" \
  --skip-git-repo-check \
  -s danger-full-access \
  -C "$project_dir" \
  - <"$prompt_file"
