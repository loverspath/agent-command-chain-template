#!/usr/bin/env bash
# lib/config.sh: 2-tier instance config layering for agent-command-chain
#
# Config layering order:
#   1. Base configuration: config.env (or $ACC_CONFIG_ENV if specified)
#   2. Instance override: instances/<id>.env (if ACC_INSTANCE or $ACC_RUNTIME/instance exists)
#
# Exit / Return code convention:
#   0  : Configuration loaded successfully
#   70 : Configuration error (invalid ID format, missing instance file, corrupt marker)

acc_load_config() {
  local here="${1:-}"
  if [[ -z "$here" ]]; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  fi

  local base="${ACC_CONFIG_ENV:-$here/config.env}"
  if [[ -f "$base" ]]; then
    # shellcheck disable=SC1090
    source "$base"
  fi

  local id="${ACC_INSTANCE:-}"
  if [[ -z "$id" && -n "${ACC_RUNTIME:-}" && -f "$ACC_RUNTIME/instance" ]]; then
    if ! IFS= read -r id < "$ACC_RUNTIME/instance" && [[ -z "$id" ]]; then
      echo "[acc] marker file '$ACC_RUNTIME/instance' is empty — invalid instance id" >&2
      return 70
    fi
    # Trim leading and trailing whitespace
    id="${id%"${id##*[![:space:]]}"}"
    id="${id#"${id%%[![:space:]]*}"}"
    if [[ -z "$id" ]]; then
      echo "[acc] marker file '$ACC_RUNTIME/instance' is empty or whitespace — invalid instance id" >&2
      return 70
    fi
  fi

  # If neither ACC_INSTANCE nor $ACC_RUNTIME/instance is set, remain backward compatible
  if [[ -z "$id" ]]; then
    return 0
  fi

  # Validate instance ID: only alphanumeric, underscore, hyphen
  if [[ ! "$id" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "[acc] invalid instance id '$id': must match ^[A-Za-z0-9_-]+$" >&2
    return 70
  fi

  local inst="$here/instances/$id.env"
  if [[ ! -f "$inst" ]]; then
    echo "[acc] instance '$id' has no $inst — refusing (no silent fallback to config.env)" >&2
    return 70
  fi

  # shellcheck disable=SC1090
  source "$inst"
  ACC_INSTANCE="$id"
  export ACC_INSTANCE
  return 0
}
