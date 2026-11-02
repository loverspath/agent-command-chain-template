#!/usr/bin/env bash
# 사용법:
#   resolve-model.sh <tier>   (기본: codex)
#   resolve-model.sh --harness <codex|agy> <tier_or_role> [--effort <low|medium|high|xhigh|max>]
#   resolve-model.sh <codex|agy> <tier_or_role> [effort]
set -euo pipefail

harness="codex"
tier=""
effort=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --harness)
      harness="${2:-}"
      shift 2 || { echo "Usage error: missing argument for --harness" >&2; exit 64; }
      ;;
    --effort)
      effort="${2:-}"
      shift 2 || { echo "Usage error: missing argument for --effort" >&2; exit 64; }
      ;;
    codex|agy)
      if [[ -z "$tier" && $# -ge 2 && "$1" == "$harness" ]]; then
        shift
      elif [[ -z "$tier" && $# -ge 2 ]]; then
        harness="$1"
        shift
      else
        tier="$1"
        shift
      fi
      ;;
    *)
      if [[ -z "$tier" ]]; then
        tier="$1"
      elif [[ -z "$effort" ]]; then
        effort="$1"
      fi
      shift
      ;;
  esac
done

if [[ "$harness" != "codex" && "$harness" != "agy" ]]; then
  echo "Usage error: invalid harness '$harness' (must be codex or agy)" >&2
  exit 64
fi

if [[ -z "$tier" || ! "$tier" =~ ^[a-z][a-z0-9_-]*$ ]]; then
  echo "Usage: $0 [--harness <codex|agy>] <tier> [--effort <level>]" >&2
  exit 64
fi

if [[ -n "$effort" && ! "$effort" =~ ^(low|medium|high|xhigh|max)$ ]]; then
  echo "Usage error: invalid effort '$effort' (must be low|medium|high|xhigh|max)" >&2
  exit 64
fi

# ==============================================================================
# 1. CODEX HARNESS RESOLUTION
# ==============================================================================
resolve_codex() {
  local target_tier="$1"
  case "$target_tier" in
    default) target_tier="${CODEX_DEFAULT_TIER:-sol}" ;;
    deep) target_tier="${CODEX_DEEP_TIER:-terra}" ;;
    fast) target_tier="${CODEX_FAST_TIER:-sol}" ;;
  esac

  local catalog="${CODEX_MODELS_CACHE:-${CODEX_HOME:-$HOME/.codex}/models_cache.json}"

  # 1-1. Live query
  if command -v codex >/dev/null 2>&1; then
    local live_output=""
    if live_output="$(timeout 30 codex debug models 2>/dev/null)" && [[ -n "$live_output" ]]; then
      local live_model=""
      local live_rc=0
      live_model="$(python3 -c '
import json, re, sys
tier = sys.argv[1]
try:
    data = json.loads(sys.stdin.read())
except Exception:
    sys.exit(2)
best = None
for m in data.get("models", []):
    if m.get("visibility") != "list":
        continue
    hit = re.fullmatch(r"gpt-(\d+(?:\.\d+)*)-" + re.escape(tier), m.get("slug", ""))
    if not hit:
        continue
    ver = tuple(int(x) for x in hit.group(1).split("."))
    if best is None or ver > best[0]:
        best = (ver, m["slug"])
if best is None:
    sys.exit(1)
print(best[1])
' "$target_tier" <<<"$live_output" 2>/dev/null)" || live_rc=$?

      if [[ $live_rc -eq 0 && -n "$live_model" ]]; then
        echo "$live_model"
        return 0
      fi
    fi
  fi

  # 1-2. Cache fallback
  if [[ ! -f "$catalog" ]]; then
    echo "resolve-model: catalog not found: $catalog" >&2
    return 66
  fi

  python3 - "$catalog" "$target_tier" <<'PY'
import json, re, sys
path, tier = sys.argv[1], sys.argv[2]
best = None
try:
    with open(path) as f:
        data = json.load(f)
except Exception as e:
    sys.stderr.write("resolve-model: failed to read catalog: %s\n" % e)
    sys.exit(66)

for m in data.get("models", []):
    if m.get("visibility") != "list":
        continue
    hit = re.fullmatch(r"gpt-(\d+(?:\.\d+)*)-" + re.escape(tier), m.get("slug", ""))
    if not hit:
        continue
    ver = tuple(int(x) for x in hit.group(1).split("."))
    if best is None or ver > best[0]:
        best = (ver, m["slug"])
if best is None:
    sys.stderr.write("resolve-model: no listed model for tier '%s'\n" % tier)
    sys.exit(1)
print(best[1])
PY
}

# ==============================================================================
# 2. AGY HARNESS RESOLUTION
# ==============================================================================
resolve_agy() {
  local target_role="$1"
  local target_effort="$2"

  local family="" target_tier="" def_effort=""
  case "$target_role" in
    default|argon)
      family="gemini"
      target_tier="argon"
      def_effort="${AGY_DEFAULT_EFFORT:-high}"
      ;;
    fast|flash)
      family="gemini"
      target_tier="flash"
      def_effort="${AGY_FAST_EFFORT:-high}"
      ;;
    deep|review|opus)
      family="claude"
      target_tier="opus"
      def_effort="${AGY_DEEP_EFFORT:-high}"
      ;;
    sonnet)
      family="claude"
      target_tier="sonnet"
      def_effort="${AGY_SONNET_EFFORT:-high}"
      ;;
    oss|120b)
      family="gpt-oss"
      target_tier="120b"
      def_effort="medium"
      ;;
    *)
      # Generic fallback matching tier directly
      family="any"
      target_tier="$target_role"
      def_effort="high"
      ;;
  esac

  local eff="${target_effort:-$def_effort}"

  local catalog="${AGY_MODELS_CACHE:-${HOME}/.gemini/antigravity-cli/cache/agy_models_cache.txt}"
  local raw_models=""

  # 2-1. Live query
  if command -v agy >/dev/null 2>&1; then
    local live_out
    if live_out="$(timeout 15 agy models 2>/dev/null)" && [[ -n "$live_out" ]]; then
      # Check if output contains model-like strings
      if echo "$live_out" | grep -qE '[a-z0-9]+-[a-z0-9.]+'; then
        raw_models="$live_out"
      fi
    fi
  fi

  # 2-2. Cache fallback
  if [[ -z "$raw_models" ]]; then
    if [[ ! -f "$catalog" ]]; then
      echo "resolve-model: agy catalog not found: $catalog" >&2
      return 66
    fi
    raw_models="$(cat "$catalog" 2>/dev/null || true)"
    if [[ -z "$raw_models" ]]; then
      echo "resolve-model: agy catalog is empty: $catalog" >&2
      return 66
    fi
  fi

  # Parse models with Python
  python3 -c '
import sys, re

family, tier, desired_effort = sys.argv[1:4]
lines = [l.strip() for l in sys.stdin.read().splitlines() if l.strip()]

slugs = []
for line in lines:
    parts = line.split()
    if parts:
        slugs.append(parts[0])

# Effort preference rank
effort_ranks = {"max": 5, "xhigh": 4, "high": 3, "medium": 2, "low": 1}

candidates = []

for slug in slugs:
    # 1. Gemini pattern: gemini-<ver>-<tier>-<effort>
    m_gem = re.fullmatch(r"gemini-(\d+(?:\.\d+)*)-([a-z0-9]+)-(low|medium|high|xhigh|max)", slug)
    if m_gem:
        s_fam = "gemini"
        s_ver = tuple(int(x) for x in m_gem.group(1).split("."))
        s_tier = m_gem.group(2)
        s_eff = m_gem.group(3)
        if (family in ("gemini", "any")) and (s_tier == tier):
            candidates.append((s_ver, s_eff == desired_effort, effort_ranks.get(s_eff, 0), slug))
        continue

    # 2. Claude pattern: claude-<tier>-<ver>-<effort> (ver can have dots or hyphens: 5-5 -> (5, 5))
    m_claude = re.fullmatch(r"claude-([a-z0-9]+)-(\d+(?:[.-]\d+)*)-(low|medium|high|xhigh|max)", slug)
    if m_claude:
        s_fam = "claude"
        s_tier = m_claude.group(1)
        v_str = m_claude.group(2).replace("-", ".")
        s_ver = tuple(int(x) for x in v_str.split("."))
        s_eff = m_claude.group(3)
        if (family in ("claude", "any")) and (s_tier == tier):
            candidates.append((s_ver, s_eff == desired_effort, effort_ranks.get(s_eff, 0), slug))
        continue

    # 3. GPT-OSS pattern: gpt-oss-<tier>-<effort>
    m_oss = re.fullmatch(r"gpt-oss-([a-z0-9]+)-(low|medium|high|xhigh|max)", slug)
    if m_oss:
        s_tier = m_oss.group(1)
        s_eff = m_oss.group(2)
        if (family in ("gpt-oss", "any")) and (s_tier == tier):
            candidates.append(((1,), s_eff == desired_effort, effort_ranks.get(s_eff, 0), slug))
        continue

if not candidates:
    sys.stderr.write("resolve-model: no model found for tier \x27%s\x27 (family=\x27%s\x27)\n" % (tier, family))
    sys.exit(1)

# Sort candidates:
# 1. highest version tuple
# 2. exact desired effort match (True > False)
# 3. effort rank
candidates.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
print(candidates[0][3])
' "$family" "$target_tier" "$eff" <<<"$raw_models"
}

if [[ "$harness" == "codex" ]]; then
  resolve_codex "$tier"
else
  resolve_agy "$tier" "$effort"
fi
