#!/usr/bin/env bash
# tests/test_resolve_model.sh: tests for bin/resolve-model.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOLVE_SCRIPT="$SCRIPT_DIR/../bin/resolve-model.sh"

TMP_DIR="$(mktemp -d /tmp/test-resolve-model-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

CATALOG="$TMP_DIR/models_cache.json"
FAKE_BIN="$TMP_DIR/bin"
mkdir -p "$FAKE_BIN"
export PATH="$FAKE_BIN:$PATH"

# Default fake codex: fails by default so tests 2-4 test cache fallback
cat > "$FAKE_BIN/codex" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$FAKE_BIN/codex"

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

echo "=== Test 5: Live catalog has newer version -> selects it ==="
cat > "$FAKE_BIN/codex" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == *"debug models"* ]]; then
  cat <<'JSON'
{
  "models": [
    {"slug": "gpt-6.2-sol", "visibility": "list"},
    {"slug": "gpt-6-sol", "visibility": "list"}
  ]
}
JSON
  exit 0
fi
exit 1
EOF
chmod +x "$FAKE_BIN/codex"

cat > "$CATALOG" <<'EOF'
{
  "models": [
    {"slug": "gpt-6-sol", "visibility": "list"}
  ]
}
EOF
output="$(CODEX_MODELS_CACHE="$CATALOG" "$RESOLVE_SCRIPT" sol)"
if [[ "$output" != "gpt-6.2-sol" ]]; then
  echo "FAIL: Expected 'gpt-6.2-sol' from live catalog, got '$output'" >&2
  exit 1
fi
echo "PASS: Live catalog has newer version and was selected"

echo "=== Test 6: Live catalog fails -> falls back to cache file ==="
cat > "$FAKE_BIN/codex" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$FAKE_BIN/codex"

cat > "$CATALOG" <<'EOF'
{
  "models": [
    {"slug": "gpt-6-sol", "visibility": "list"},
    {"slug": "gpt-5.6-sol", "visibility": "list"}
  ]
}
EOF
output="$(CODEX_MODELS_CACHE="$CATALOG" "$RESOLVE_SCRIPT" sol)"
if [[ "$output" != "gpt-6-sol" ]]; then
  echo "FAIL: Expected 'gpt-6-sol' from fallback cache, got '$output'" >&2
  exit 1
fi
echo "PASS: Live catalog fails and successfully falls back to cache file"

echo "=== Test 7: Both live catalog and cache fail -> returns non-zero ==="
cat > "$FAKE_BIN/codex" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$FAKE_BIN/codex"

rc=0
CODEX_MODELS_CACHE="$TMP_DIR/nonexistent.json" "$RESOLVE_SCRIPT" sol 2>/dev/null || rc=$?
if [[ "$rc" -eq 0 ]]; then
  echo "FAIL: Expected non-zero exit when both live and cache fail, got $rc" >&2
  exit 1
fi
echo "PASS: Both fail returns non-zero (rc=$rc)"

echo "=== Test 8: Agy live catalog highest version selection ==="
cat > "$FAKE_BIN/agy" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == *"models"* ]]; then
  cat <<'TXT'
gemini-3.5-argon-high	Gemini 3.5 Argon (High)
gemini-4-argon-high	Gemini 4 Argon (High)
gemini-3.10-argon-high	Gemini 3.10 Argon (High)
claude-opus-4-6-high	Claude Opus 4.6 (High)
claude-opus-5-5-high	Claude Opus 5.5 (High)
TXT
  exit 0
fi
exit 1
EOF
chmod +x "$FAKE_BIN/agy"

out_argon="$("$RESOLVE_SCRIPT" --harness agy default)"
if [[ "$out_argon" != "gemini-4-argon-high" ]]; then
  echo "FAIL: Expected 'gemini-4-argon-high', got '$out_argon'" >&2
  exit 1
fi
out_opus="$("$RESOLVE_SCRIPT" --harness agy deep)"
if [[ "$out_opus" != "claude-opus-5-5-high" ]]; then
  echo "FAIL: Expected 'claude-opus-5-5-high', got '$out_opus'" >&2
  exit 1
fi
echo "PASS: Agy highest version selection verified"

echo "=== Test 9: Agy role aliases and --effort override ==="
cat > "$FAKE_BIN/agy" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == *"models"* ]]; then
  cat <<'TXT'
gemini-4-argon-low	Gemini 4 Argon (Low)
gemini-4-argon-high	Gemini 4 Argon (High)
gemini-3.8-flash-high	Gemini 3.8 Flash (High)
claude-opus-5-5-high	Claude Opus 5.5 (High)
claude-sonnet-5-5-medium	Claude Sonnet 5.5 (Medium)
TXT
  exit 0
fi
exit 1
EOF
chmod +x "$FAKE_BIN/agy"

out_fast="$("$RESOLVE_SCRIPT" --harness agy fast)"
if [[ "$out_fast" != "gemini-3.8-flash-high" ]]; then
  echo "FAIL: Expected 'gemini-3.8-flash-high', got '$out_fast'" >&2
  exit 1
fi
out_low="$("$RESOLVE_SCRIPT" --harness agy argon --effort low)"
if [[ "$out_low" != "gemini-4-argon-low" ]]; then
  echo "FAIL: Expected 'gemini-4-argon-low', got '$out_low'" >&2
  exit 1
fi
echo "PASS: Agy role aliases and --effort override verified"

echo "=== Test 10: Agy live fails -> falls back to AGY_MODELS_CACHE ==="
cat > "$FAKE_BIN/agy" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$FAKE_BIN/agy"

AGY_CACHE="$TMP_DIR/agy_cache.txt"
cat > "$AGY_CACHE" <<'EOF'
gemini-4-argon-high	Gemini 4 Argon (High)
gemini-3.8-flash-high	Gemini 3.8 Flash (High)
EOF

out_cache="$(AGY_MODELS_CACHE="$AGY_CACHE" "$RESOLVE_SCRIPT" --harness agy argon)"
if [[ "$out_cache" != "gemini-4-argon-high" ]]; then
  echo "FAIL: Expected 'gemini-4-argon-high' from cache, got '$out_cache'" >&2
  exit 1
fi
echo "PASS: Agy cache fallback verified"

echo "=== Test 11: Agy missing cache exit 66 and unlisted tier exit 1 ==="
rc=0
AGY_MODELS_CACHE="$TMP_DIR/nonexistent.txt" "$RESOLVE_SCRIPT" --harness agy argon 2>/dev/null || rc=$?
if [[ "$rc" -ne 66 ]]; then
  echo "FAIL: Expected exit 66 for missing agy cache, got $rc" >&2
  exit 1
fi

rc=0
AGY_MODELS_CACHE="$AGY_CACHE" "$RESOLVE_SCRIPT" --harness agy nonexistent_tier 2>/dev/null || rc=$?
if [[ "$rc" -ne 1 ]]; then
  echo "FAIL: Expected exit 1 for unlisted agy tier, got $rc" >&2
  exit 1
fi
echo "PASS: Agy error code conventions (66 and 1) verified"

echo "=== Test 12: Positional shorthand syntax (agy opus high) ==="
cat > "$FAKE_BIN/agy" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == *"models"* ]]; then
  echo -e "claude-opus-5-5-high\tClaude Opus 5.5 (High)"
  exit 0
fi
exit 1
EOF
chmod +x "$FAKE_BIN/agy"

out_pos="$("$RESOLVE_SCRIPT" agy opus high)"
if [[ "$out_pos" != "claude-opus-5-5-high" ]]; then
  echo "FAIL: Expected 'claude-opus-5-5-high' for positional shorthand, got '$out_pos'" >&2
  exit 1
fi
echo "PASS: Positional shorthand syntax verified"

echo "All tests in test_resolve_model.sh passed successfully!"
exit 0
