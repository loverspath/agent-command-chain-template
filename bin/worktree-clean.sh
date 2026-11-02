#!/usr/bin/env bash
# bin/worktree-clean.sh: Safe cleanup of isolated git worktrees
# Usage:
#   worktree-clean.sh <task_id> [--delete-branch] [--force]
#   worktree-clean.sh --prune-merged
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck disable=SC1091
source "$HERE/lib/config.sh"
acc_load_config "$HERE" || exit $?
# shellcheck disable=SC1091
source "$HERE/lib/session.sh"

task_id=""
delete_branch=false
force=false
prune_merged=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --delete-branch)
      delete_branch=true
      shift
      ;;
    --force)
      force=true
      shift
      ;;
    --prune-merged)
      prune_merged=true
      shift
      ;;
    -*)
      echo "Unknown option: $1" >&2
      exit 64
      ;;
    *)
      if [[ -z "$task_id" ]]; then
        task_id="$1"
      else
        echo "Unexpected argument: $1" >&2
        exit 64
      fi
      shift
      ;;
  esac
done

if [[ "$prune_merged" == "false" && -z "$task_id" ]]; then
  echo "Usage: $0 <task_id> [--delete-branch] [--force] | --prune-merged" >&2
  exit 64
fi

resolve_session_and_runtime "worktree-clean" "" false
runtime="$ACC_RUNTIME"
wt_root="$runtime/worktrees"

clean_single_worktree() {
  local tid="$1"
  local wt_dir="$wt_root/$tid"
  local meta_file="$runtime/tasks/$tid/worktree.meta"

  if [[ ! -d "$wt_dir" ]]; then
    echo "Notice: Worktree directory '$wt_dir' does not exist." >&2
    return 0
  fi

  local base_dir="${PROJECT_DIR:-$PWD}"
  local wt_branch="acc/$tid"

  if [[ -f "$meta_file" ]]; then
    local b_from_meta dir_from_meta
    b_from_meta="$(grep '^worktree_branch=' "$meta_file" 2>/dev/null | cut -d= -f2- || true)"
    dir_from_meta="$(grep '^base_project_dir=' "$meta_file" 2>/dev/null | cut -d= -f2- || true)"
    [[ -n "$b_from_meta" ]] && wt_branch="$b_from_meta"
    [[ -n "$dir_from_meta" ]] && base_dir="$dir_from_meta"
  fi

  # Check uncommitted changes in worktree
  if [[ -d "$wt_dir/.git" || -f "$wt_dir/.git" ]]; then
    local dirty
    dirty="$(git -C "$wt_dir" status --porcelain 2>/dev/null || true)"
    if [[ -n "$dirty" && "$force" == "false" ]]; then
      echo "Error: Worktree '$wt_dir' has uncommitted changes. Use --force to discard." >&2
      return 1
    fi
  fi

  # Check unmerged commits if delete_branch requested
  if [[ "$delete_branch" == "true" && "$force" == "false" ]]; then
    if ! git -C "$base_dir" merge-base --is-ancestor "$wt_branch" HEAD 2>/dev/null; then
      echo "Error: Worktree branch '$wt_branch' has unmerged commits. Merge first or use --force." >&2
      return 1
    fi
  fi

  # Remove worktree
  if [[ "$force" == "true" ]]; then
    git -C "$base_dir" worktree remove --force "$wt_dir" >/dev/null 2>&1 || rm -rf "$wt_dir" 2>/dev/null || true
  else
    if ! git -C "$base_dir" worktree remove "$wt_dir" >/dev/null 2>&1; then
      rm -rf "$wt_dir" 2>/dev/null || true
    fi
  fi

  # Delete branch if requested
  if [[ "$delete_branch" == "true" ]]; then
    if [[ "$force" == "true" ]]; then
      git -C "$base_dir" branch -D "$wt_branch" >/dev/null 2>&1 || true
    else
      git -C "$base_dir" branch -d "$wt_branch" >/dev/null 2>&1 || true
    fi
  fi

  git -C "$base_dir" worktree prune >/dev/null 2>&1 || true
  echo "Cleaned worktree for task '$tid'."
  return 0
}

if [[ "$prune_merged" == "true" ]]; then
  if [[ ! -d "$wt_root" ]]; then
    exit 0
  fi
  base_dir="${PROJECT_DIR:-$PWD}"
  for d in "$wt_root"/*; do
    [[ -d "$d" ]] || continue
    tid="$(basename "$d")"
    meta="$runtime/tasks/$tid/worktree.meta"
    b="acc/$tid"
    [[ -f "$meta" ]] && b="$(grep '^worktree_branch=' "$meta" 2>/dev/null | cut -d= -f2- || echo "$b")"
    if git -C "$base_dir" merge-base --is-ancestor "$b" HEAD 2>/dev/null; then
      clean_single_worktree "$tid"
      git -C "$base_dir" branch -d "$b" >/dev/null 2>&1 || true
    fi
  done
  exit 0
fi

clean_single_worktree "$task_id"
exit $?
