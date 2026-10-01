#!/usr/bin/env bash
# 사용법: resolve-model.sh <tier>   예) resolve-model.sh sol  ->  gpt-6-sol
# codex 모델 카탈로그(models_cache.json)에서 gpt-<버전>-<tier> 중 visibility=list 인 최신 버전을 출력한다.
# 그 tier 의 최신 세대가 없으면 존재하는 가장 높은 버전으로 폴백하고, tier 자체가 없으면 실패(exit 1)한다.
set -euo pipefail

tier="${1:-}"
if [[ -z "$tier" || ! "$tier" =~ ^[a-z][a-z0-9-]*$ ]]; then
  echo "Usage: $0 <tier>" >&2
  exit 64
fi

catalog="${CODEX_MODELS_CACHE:-${CODEX_HOME:-$HOME/.codex}/models_cache.json}"
if [[ ! -f "$catalog" ]]; then
  echo "resolve-model: catalog not found: $catalog" >&2
  exit 66
fi

python3 - "$catalog" "$tier" <<'PY'
import json, re, sys
path, tier = sys.argv[1], sys.argv[2]
best = None
for m in json.load(open(path)).get("models", []):
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
