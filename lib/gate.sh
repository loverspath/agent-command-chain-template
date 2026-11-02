#!/usr/bin/env bash
# lib/gate.sh - Deterministic Rules-then-LLM Gate and Observe Mode Logger
# Filters events deterministically before waking the supervisor LLM.

acc_gate_evaluate_event() {
  local evt_file="$1"
  local runtime="$2"

  if [[ ! -f "$evt_file" ]]; then
    printf 'PASS\n'
    return 0
  fi

  local knd src tid actor dedupe_key
  knd="$(grep '^kind=' "$evt_file" 2>/dev/null | cut -d= -f2- || echo "")"
  src="$(grep '^source=' "$evt_file" 2>/dev/null | cut -d= -f2- || echo "")"
  tid="$(grep '^task_id=' "$evt_file" 2>/dev/null | cut -d= -f2- || echo "")"
  actor="$(grep '^actor=' "$evt_file" 2>/dev/null | cut -d= -f2- || echo "")"
  dedupe_key="$(grep '^dedupe_key=' "$evt_file" 2>/dev/null | cut -d= -f2- || echo "")"

  # 1. Essential completion / error / stall events MUST always pass
  case "$knd" in
    done|error|stalled|process_exit)
      printf 'PASS\n'
      return 0
      ;;
    bridge_degraded)
      printf 'DRAIN_AUTO\n'
      return 0
      ;;
    started|canceled)
      if grep -qs '^status=running' "$runtime"/tasks/*/state 2>/dev/null; then
        printf 'PASS\n'
      else
        printf 'DRAIN_AUTO\n'
      fi
      return 0
      ;;
    proactive)
      # Deterministic Gate (Rules-then-LLM, Cost 0)
      # Check self-generated echo
      local self_actor="${ACC_SELF_ACTOR:-agentchain}"
      if [[ -n "$actor" && "$actor" == "$self_actor" ]]; then
        printf 'DROP_SELF\n'
        return 0
      fi

      # Check deduplication window
      if [[ -n "$dedupe_key" ]]; then
        local dedupe_dir="$runtime/events/dedupe"
        local dedupe_file="$dedupe_dir/$dedupe_key"
        local now
        now="$(date +%s)"
        local window="${PROACTIVE_DEDUPE_WINDOW:-3600}"

        if [[ -f "$dedupe_file" ]]; then
          local last_ts
          last_ts="$(stat -c %Y "$dedupe_file" 2>/dev/null || echo 0)"
          if (( now - last_ts < window )); then
            printf 'DROP_DEDUPE\n'
            return 0
          fi
        fi
      fi

      # Check Autonomy Mode
      local mode="${ACC_PROACTIVE_MODE:-observe}"
      case "$mode" in
        off)
          printf 'DROP_OFF\n'
          return 0
          ;;
        notify)
          if [[ -n "$dedupe_key" ]]; then
            mkdir -p "$runtime/events/dedupe" 2>/dev/null || true
            touch "$runtime/events/dedupe/$dedupe_key" 2>/dev/null || true
          fi
          printf 'PASS\n'
          return 0
          ;;
        observe|*)
          printf 'OBSERVE_ONLY\n'
          return 0
          ;;
      esac
      ;;
    *)
      printf 'PASS\n'
      return 0
      ;;
  esac
}

acc_gate_archive_observed() {
  local evt_file="$1"
  local runtime="$2"
  local rule_id="${3:-}"

  local date_str
  date_str="$(date +%Y%m%d)"
  local target_dir="$runtime/events/archive/observed-$date_str"
  mkdir -p "$target_dir"

  local evt_id src tid knd summary_b64 actor dedupe_key summary
  evt_id="$(grep '^id=' "$evt_file" 2>/dev/null | cut -d= -f2- || basename "$evt_file" .evt)"
  src="$(grep '^source=' "$evt_file" 2>/dev/null | cut -d= -f2- || echo "")"
  tid="$(grep '^task_id=' "$evt_file" 2>/dev/null | cut -d= -f2- || echo "")"
  knd="$(grep '^kind=' "$evt_file" 2>/dev/null | cut -d= -f2- || echo "proactive")"
  summary_b64="$(grep '^summary_b64=' "$evt_file" 2>/dev/null | cut -d= -f2- || echo "")"
  actor="$(grep '^actor=' "$evt_file" 2>/dev/null | cut -d= -f2- || echo "")"
  dedupe_key="$(grep '^dedupe_key=' "$evt_file" 2>/dev/null | cut -d= -f2- || echo "")"
  [[ -z "$rule_id" ]] && rule_id="$(grep '^rule_id=' "$evt_file" 2>/dev/null | cut -d= -f2- || echo "")"

  summary="$(printf '%s' "$summary_b64" | base64 -d 2>/dev/null || echo "")"

  # Record dedupe marker if dedupe_key present
  if [[ -n "$dedupe_key" ]]; then
    mkdir -p "$runtime/events/dedupe" 2>/dev/null || true
    touch "$runtime/events/dedupe/$dedupe_key" 2>/dev/null || true
  fi

  # Record 1 JSON line to observed-audit.jsonl
  python3 -c '
import json, sys, datetime
audit_file, eid, src, knd, tid, rid, dkey, act, smy = sys.argv[1:10]
rec = {
    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "event_id": eid,
    "source": src,
    "kind": knd,
    "task_id": tid,
    "rule_id": rid,
    "dedupe_key": dkey,
    "actor": act,
    "mode": "observe",
    "would_accept": True,
    "reason": "observed_only",
    "summary": smy
}
with open(audit_file, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
' "$target_dir/observed-audit.jsonl" "$evt_id" "$src" "$knd" "$tid" "$rule_id" "$dedupe_key" "$actor" "$summary" 2>/dev/null || true

  mv "$evt_file" "$target_dir/" 2>/dev/null || true
}
