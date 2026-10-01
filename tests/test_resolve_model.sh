#!/usr/bin/env bash
# tests/test_resolve_model.sh: tests for bin/resolve-model.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOLVE_SCRIPT="$SCRIPT_DIR/../bin/resolve-model.sh"

TMP_DIR="$(mktemp -d /tmp/test-resolve-model-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

CATALOG="$TMP_DIR/models_cache.json"

echo "=== Test 1: Invalid tier argument (exit 64) ==="
# Missing tier
rc=0
"$RESOLVE_SCRIPT" 2>/dev/null || rc=$?
if [[ "$rc" -ne 64 ]]; then
  echo "FAIL: Expected exit 64 for missing argument, got $rc" >&2
  exit 1
fi

# Invalid tier syntax (digits leading, uppercase, special characters)
for bad_tier in "123tier" "SOL" "sol!" "-sol" ""; do
  rc=0
  "$RESOLVE_SCRIPT" "$bad_tier" 2>/dev/null || rc=$?
  if [[ "$rc" -ne 64 ]]; then
    echo "FAIL: Expected exit 64 for invalid tier '$bad_tier', got $rc" >&2
    exit 1
  fi
done
echo "PASS: Invalid tier argument checks"

echo "=== Test 2: Missing catalog file (exit 66) ==="
rc=0
CODEX_MODELS_CACHE="$TMP_DIR/nonexistent.json" "$RESOLVE_SCRIPT" sol 2>/dev/null || rc=$?
if [[ "$rc" -ne 66 ]]; then
  echo "FAIL: Expected exit 66 for missing catalog, got $rc" >&2
  exit 1
fi
echo "PASS: Missing catalog check"

echo "=== Test 3: Tier not found in catalog (exit 1) ==="
cat > "$CATALOG" <<'EOF'
{
  "models": [
    {"slug": "gpt-5.6-terra", "visibility": "list"},
    {"slug": "gpt-6-terra", "visibility": "list"},
    {"slug": "gpt-6-sol", "visibility": "hidden"}
  ]
}
EOF
rc=0
CODEX_MODELS_CACHE="$CATALOG" "$RESOLVE_SCRIPT" sol 2>/dev/null || rc=$?
if [[ "$rc" -ne 1 ]]; then
  echo "FAIL: Expected exit 1 for unlisted/missing tier, got $rc" >&2
  exit 1
fi
echo "PASS: Tier not found check"

echo "=== Test 4: Highest version selection ==="
cat > "$CATALOG" <<'EOF'
{
  "models": [
    {"slug": "gpt-5.6-sol", "visibility": "list"},
    {"slug": "gpt-6-sol", "visibility": "list"},
    {"slug": "gpt-5.10-sol", "visibility": "list"},
    {"slug": "gpt-7-sol", "visibility": "hidden"},
    {"slug": "other-model", "visibility": "list"}
  ]
}
EOF
output="$(CODEX_MODELS_CACHE="$CATALOG" "$RESOLVE_SCRIPT" sol)"
if [[ "$output" != "gpt-6-sol" ]]; then
  echo "FAIL: Expected 'gpt-6-sol', got '$output'" >&2
  exit 1
fi
echo "PASS: Highest version selection (gpt-6-sol selected over 5.6 and 5.10, ignoring hidden 7)"

cat > "$CATALOG" <<'EOF'
{
  "models": [
    {"slug": "gpt-5.9-sol", "visibility": "list"},
    {"slug": "gpt-5.10-sol", "visibility": "list"}
  ]
}
EOF
output="$(CODEX_MODELS_CACHE="$CATALOG" "$RESOLVE_SCRIPT" sol)"
if [[ "$output" != "gpt-5.10-sol" ]]; then
  echo "FAIL: Expected 'gpt-5.10-sol' (numeric version comparison), got '$output'" >&2
  exit 1
fi
echo "PASS: Numeric version comparison (5.10 > 5.9)"

echo "All tests in test_resolve_model.sh passed successfully!"
exit 0
