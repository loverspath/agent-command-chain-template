#!/usr/bin/env bash
# lib/reason.sh - reason_code validation, classification, and normalization
# Provides deterministic classification of worker termination reasons without external dependencies.

acc_validate_reason_code() {
  local code="${1:-}"
  [[ "$code" =~ ^[a-z0-9_]{1,40}$ ]]
}

acc_classify_reason_from_text() {
  local text="${1:-}"
  local default_code="${2:-exec_error}"

  if [[ -z "$text" ]]; then
    printf '%s\n' "$default_code"
    return 0
  fi

  # Quota / Rate limit patterns
  if echo "$text" | grep -iqE '429|Too Many Requests|rate_limit_exceeded|RESOURCE_EXHAUSTED|hit your usage limit|quota exceeded|usage limit|try again at'; then
    printf 'transient_quota\n'
    return 0
  fi

  # Transient network patterns
  if echo "$text" | grep -iqE 'ECONNRESET|ETIMEDOUT|EAI_AGAIN|502 Bad Gateway|503 Service Unavailable|504 Gateway Timeout|overloaded_error|stream disconnected'; then
    printf 'transient_network\n'
    return 0
  fi

  # Specific blocked / dispatch reasons
  if echo "$text" | grep -iqE 'subagent-unavailable|subagent_unavailable'; then
    printf 'subagent_unavailable\n'
    return 0
  fi

  if echo "$text" | grep -iqE 'needs-terra|needs_terra'; then
    printf 'needs_terra\n'
    return 0
  fi

  if echo "$text" | grep -iqE 'verify_failed|test_failed'; then
    printf 'verify_failed\n'
    return 0
  fi

  if echo "$text" | grep -iqE 'ambiguous-spec|needs_human_decision'; then
    printf 'needs_human_decision\n'
    return 0
  fi

  printf '%s\n' "$default_code"
}

acc_normalize_blocked_reason() {
  local rest="${1:-}"

  # Look for reason=<token>
  local extracted
  extracted=$(echo "$rest" | sed -nE 's/.*reason=([a-zA-Z0-9_-]+).*/\1/p' | head -n 1)

  if [[ -n "$extracted" ]]; then
    local normalized="${extracted//-/_}"
    normalized="$(echo "$normalized" | tr '[:upper:]' '[:lower:]')"
    case "$normalized" in
      subagent_unavailable|subagent-unavailable)
        printf 'subagent_unavailable\n'
        return 0
        ;;
      needs_terra|needs-terra)
        printf 'needs_terra\n'
        return 0
        ;;
      verify_failed|test_failed)
        printf 'verify_failed\n'
        return 0
        ;;
      ambiguous_spec|ambiguous-spec|needs_human_decision)
        printf 'needs_human_decision\n'
        return 0
        ;;
      transient_quota|transient_network|timeout)
        printf '%s\n' "$normalized"
        return 0
        ;;
      *)
        if acc_validate_reason_code "$normalized"; then
          printf '%s\n' "$normalized"
          return 0
        else
          printf 'needs_human_decision\n'
          return 0
        fi
        ;;
    esac
  fi

  # If no explicit reason= token, check whole text for classified reason or fallback to needs_human_decision
  local classified
  classified="$(acc_classify_reason_from_text "$rest" "needs_human_decision")"
  printf '%s\n' "$classified"
}
