#!/usr/bin/env bash
# 사용법: resolve-model.sh <tier>   예) resolve-model.sh sol  ->  gpt-<version>-sol
# codex 모델 카탈로그에서 gpt-<버전>-<tier> 중 visibility=list 인 최신 버전을 출력한다.
# 1) codex debug models (라이브 카탈로그, 타임아웃 30초)를 먼저 시도한다.
# 2) 실패하거나 타임아웃 시 models_cache.json 파일(캐시)로 폴백한다.
# 그 tier 의 최신 세대가 없으면 존재하는 가장 높은 버전으로 폴백하고, tier 자체가 없으면 실패(exit 1)한다.
set -euo pipefail

tier="${1:-}"
if [[ -z "$tier" || ! "$tier" =~ ^[a-z][a-z0-9-]*$ ]]; then
  echo "Usage: $0 <tier>" >&2
  exit 64
fi

catalog="${CODEX_MODELS_CACHE:-${CODEX_HOME:-$HOME/.codex}/models_cache.json}"

# 1. codex debug models 라이브 카탈로그 조회 시도 (타임아웃 30초)
if command -v codex >/dev/null 2>&1; then
  live_output=""
  if live_output="$(timeout 30 codex debug models 2>/dev/null)" && [[ -n "$live_output" ]]; then
    live_model=""
    live_rc=0
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
' "$tier" <<<"$live_output" 2>/dev/null)" || live_rc=$?

    if [[ $live_rc -eq 0 && -n "$live_model" ]]; then
      echo "$live_model"
      exit 0
    fi
  fi
fi

# 2. 캐시 파일 폴백
if [[ ! -f "$catalog" ]]; then
  echo "resolve-model: catalog not found: $catalog" >&2
  exit 66
fi

python3 - "$catalog" "$tier" <<'PY'
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
