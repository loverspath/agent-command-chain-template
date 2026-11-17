"""Centralized Fleet manifest management, replica sync helpers, and node self-convergence."""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import json
import math
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from typing import Any, Iterable

from .config import (
    CLIENT_CAPS,
    FLEET_EXIT_CODE,
    MAX_FLEET_ATTEMPTS,
    config_file_path,
    fleet_applied_path,
    fleet_attempts_path,
    fleet_file_path,
    get_fleet_git_timeout,
    parse_peer_list,
    parse_url_list,
)
from .journal import new_message_id

FLEET_ACK_PREFIX = "[fleet-ack] "
FLEET_ACK_TARGET = "relay/fleet"

_WARN_DEDUP_LOCK = threading.RLock()
_WARNED_FILE_HASHES: set[tuple[str, str]] = set()
_FLEET_CONVERGE_LOCK = threading.RLock()


class FleetValidationError(ValueError):
    """Raised when a `fleet.json` manifest or CLI argument fails schema validation."""


class FleetConvergeExit(SystemExit):
    """Raised by `run_node` after idle drain & `fleet-ack` flush to trigger supervisor restart (`75`)."""

    def __init__(self, code: int = FLEET_EXIT_CODE) -> None:
        super().__init__(code)


@dataclasses.dataclass(frozen=True)
class FleetConvergenceOutcome:
    action: str  # "ignored" | "retry_later" | "applied" | "blocked"
    applied_doc: dict[str, Any] | None = None
    needs_restart: bool = False
    attempts: int = 0
    reason: str = ""


def _fsync_dir(dir_path: pathlib.Path) -> None:
    if not hasattr(os, "O_RDONLY"):
        return
    fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(str(dir_path), flags)
        os.fsync(fd)
    except OSError:
        pass
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _atomic_write_bytes(path: pathlib.Path, data: bytes, *, mode: int = 0o600) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".tmp-{path.name}.{os.getpid()}.{secrets.token_hex(6)}"
    fd: int | None = None
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
        os.close(fd)
        fd = None
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def _atomic_write_text(path: pathlib.Path, content: str, *, mode: int = 0o600) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"), mode=mode)


def _is_valid_ws_url(url: str) -> bool:
    clean = url.strip()
    if not clean or any(ch.isspace() or ch == "\x00" for ch in clean):
        return False
    try:
        parsed = urllib.parse.urlsplit(clean)
    except Exception:
        return False
    if parsed.scheme.lower() not in ("ws", "wss"):
        return False
    if not parsed.netloc or not parsed.hostname:
        return False
    return True


def validate_fleet_manifest(
    raw: Any,
    *,
    require_commit: bool = False,
) -> dict[str, Any]:
    """Validate and normalize a `fleet.json` manifest dict."""
    if not isinstance(raw, dict):
        raise FleetValidationError("fleet manifest must be a JSON object")

    raw_rev = raw.get("rev")
    if isinstance(raw_rev, bool) or not isinstance(raw_rev, int) or raw_rev <= 0:
        raise FleetValidationError(f"fleet 'rev' must be a positive integer (got {raw_rev!r})")

    raw_commit = raw.get("commit")
    if raw_commit is None:
        commit = ""
    elif isinstance(raw_commit, str):
        commit = raw_commit.strip()
    else:
        raise FleetValidationError("fleet 'commit' must be a string")

    if require_commit and not commit:
        raise FleetValidationError("fleet 'commit' must be non-empty")
    if commit and (
        commit.startswith("-") or any(ch.isspace() or ch == "\x00" for ch in commit)
    ):
        raise FleetValidationError(f"invalid fleet 'commit' value: {commit!r}")

    raw_urls = raw.get("urls")
    if not isinstance(raw_urls, list) or not raw_urls:
        raise FleetValidationError("fleet 'urls' must be a non-empty list of ws:// or wss:// URLs")

    seen_urls: set[str] = set()
    norm_urls: list[str] = []
    for item in raw_urls:
        if not isinstance(item, str):
            raise FleetValidationError(f"fleet URL entry must be a string (got {item!r})")
        clean_u = item.strip()
        if not _is_valid_ws_url(clean_u):
            raise FleetValidationError(
                f"invalid fleet URL {item!r}: must be ws:// or wss:// with non-empty host"
            )
        if clean_u not in seen_urls:
            seen_urls.add(clean_u)
            norm_urls.append(clean_u)

    if not norm_urls:
        raise FleetValidationError("fleet 'urls' cannot be empty")

    out: dict[str, Any] = {
        "rev": int(raw_rev),
        "commit": commit,
        "urls": norm_urls,
    }

    if "hub_peers" in raw and raw["hub_peers"] is not None:
        raw_hp = raw["hub_peers"]
        if not isinstance(raw_hp, list) or any(not isinstance(x, str) for x in raw_hp):
            raise FleetValidationError("fleet 'hub_peers' must be a list of strings")
        out["hub_peers"] = parse_peer_list(raw_hp)

    if "min_caps" in raw and raw["min_caps"] is not None:
        raw_mc = raw["min_caps"]
        if not isinstance(raw_mc, list) or any(not isinstance(x, str) for x in raw_mc):
            raise FleetValidationError("fleet 'min_caps' must be a list of strings")
        seen_caps: set[str] = set()
        norm_caps: list[str] = []
        for c in raw_mc:
            cc = c.strip()
            if cc and cc not in seen_caps:
                seen_caps.add(cc)
                norm_caps.append(cc)
        out["min_caps"] = norm_caps

    if "updated_at" in raw and raw["updated_at"] is not None:
        raw_ts = raw["updated_at"]
        if isinstance(raw_ts, bool) or not isinstance(raw_ts, (int, float)):
            raise FleetValidationError("fleet 'updated_at' must be a finite number")
        ts_val = float(raw_ts)
        if not math.isfinite(ts_val) or ts_val <= 0:
            raise FleetValidationError("fleet 'updated_at' must be positive and finite")
        out["updated_at"] = ts_val

    return out


def canonical_fleet_sha256(manifest: dict[str, Any]) -> str:
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()


def load_fleet_manifest_file(
    path: pathlib.Path,
    *,
    warn_prefix: str = "fleet-warn",
) -> tuple[dict[str, Any] | None, str]:
    """Load and validate `fleet.json` from `path`.

    Returns `(validated_manifest, raw_sha256)` when valid; `(None, 'missing')` if absent;
    or `(None, 'invalid')` (with a stderr warning) if corrupt/invalid.
    """
    p = pathlib.Path(path)
    if not p.is_file():
        return None, "missing"
    try:
        raw_bytes = p.read_bytes()
    except OSError as exc:
        print(f"[{warn_prefix}] failed reading {p}: {exc}", file=sys.stderr, flush=True)
        return None, "invalid"

    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
        validated = validate_fleet_manifest(parsed, require_commit=False)
        return validated, raw_sha
    except Exception as exc:
        dedup_key = (str(p.resolve()), raw_sha)
        with _WARN_DEDUP_LOCK:
            should_log = dedup_key not in _WARNED_FILE_HASHES
            if should_log:
                _WARNED_FILE_HASHES.add(dedup_key)
                if len(_WARNED_FILE_HASHES) > 256:
                    _WARNED_FILE_HASHES.clear()
                    _WARNED_FILE_HASHES.add(dedup_key)
        if should_log:
            print(
                f"[{warn_prefix}] ignoring invalid fleet manifest at {p}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        return None, "invalid"


def load_active_fleet_manifest(
    cfg_dir: pathlib.Path,
    *,
    explicit_auth_source: str | None = None,
) -> dict[str, Any] | None:
    """Return active validated `fleet` manifest for `cfg_dir` (source or replica), or `None`."""
    from . import auth as _auth

    cdir = pathlib.Path(cfg_dir)
    if _auth.is_replica_mode(cdir, explicit_auth_source=explicit_auth_source):
        gdir = _auth.replica_gen_dir(cdir)
        if gdir is None:
            return None
        manifest, _ = load_fleet_manifest_file(gdir / "fleet.json")
        return manifest
    manifest, _ = load_fleet_manifest_file(fleet_file_path(cdir))
    return manifest


def build_banner_fleet(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build the `"fleet"` dict embedded in hub WS banners (`None` when disabled)."""
    if manifest is None:
        return None
    out: dict[str, Any] = {
        "rev": int(manifest["rev"]),
        "commit": str(manifest.get("commit") or ""),
        "urls": list(manifest["urls"]),
    }
    if "hub_peers" in manifest and manifest["hub_peers"] is not None:
        out["hub_peers"] = list(manifest["hub_peers"])
    if "min_caps" in manifest and manifest["min_caps"] is not None:
        out["min_caps"] = list(manifest["min_caps"])
    if "updated_at" in manifest and manifest["updated_at"] is not None:
        out["updated_at"] = float(manifest["updated_at"])
    return out


def build_health_fleet(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build the target `"fleet"` object exposed on `/v1/health` and WS `op: 'health'`."""
    if manifest is None:
        return None
    out: dict[str, Any] = {
        "rev": int(manifest["rev"]),
        "commit": str(manifest.get("commit") or ""),
        "urls": list(manifest["urls"]),
        "hub_peers": list(manifest.get("hub_peers") or []),
    }
    if "min_caps" in manifest and manifest["min_caps"] is not None:
        out["min_caps"] = list(manifest["min_caps"])
    if "updated_at" in manifest and manifest["updated_at"] is not None:
        out["updated_at"] = float(manifest["updated_at"])
    return out


def is_node_converged(
    target_fleet: dict[str, Any] | None,
    node_status: dict[str, Any],
) -> bool:
    """Return True iff `node_status` matches `target_fleet`'s `rev` with an `ok` result."""
    if target_fleet is None:
        return True
    try:
        node_rev = int(node_status.get("rev") or 0)
    except (TypeError, ValueError):
        return False
    if node_rev != int(target_fleet["rev"]):
        return False
    res = str(node_status.get("result") or "")
    if not (res == "ok" or res.startswith("ok:")):
        return False
    target_commit = str(target_fleet.get("commit") or "").strip().lower()
    if target_commit and res == "ok":
        node_commit = str(node_status.get("commit") or "").strip().lower()
        if not node_commit:
            return False
        if not (node_commit == target_commit or node_commit.startswith(target_commit)):
            return False
    return True


def _parse_caps_arg(raw: Any) -> list[str]:
    if raw is None:
        return []
    tokens: list[str] = []
    if isinstance(raw, str):
        tokens.extend(raw.split(","))
    elif isinstance(raw, (list, tuple, set, frozenset)):
        for it in raw:
            if it is not None:
                tokens.extend(str(it).split(","))
    else:
        tokens.extend(str(raw).split(","))
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        clean = tok.strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def set_fleet_manifest(
    cfg_dir: pathlib.Path,
    *,
    commit: str,
    urls: Any,
    hub_peers: Any = None,
    min_caps: Any = None,
    explicit_rev: int | None = None,
    explicit_auth_source: str | None = None,
    now: float | None = None,
) -> tuple[dict[str, Any], int]:
    """Atomically create or advance `<cfg_dir>/fleet.json` on a source hub and bump `auth-generation`."""
    from . import auth as _auth

    cdir = pathlib.Path(cfg_dir)
    _auth.ensure_writable_source_mode(cdir, explicit_auth_source=explicit_auth_source)

    cur_now = float(now) if now is not None else time.time()
    fp = fleet_file_path(cdir)

    prev_rev = 0
    if fp.is_file():
        try:
            existing = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                r_val = existing.get("rev")
                if not isinstance(r_val, bool) and isinstance(r_val, int) and r_val > 0:
                    prev_rev = r_val
        except Exception:
            pass

    st_path = _auth.auth_source_state_path(cdir)
    if st_path.is_file():
        try:
            st_doc = json.loads(st_path.read_text(encoding="utf-8"))
            if isinstance(st_doc, dict):
                hist_rev = int(st_doc.get("max_fleet_rev") or 0)
                prev_rev = max(prev_rev, hist_rev)
        except Exception:
            pass

    if explicit_rev is not None:
        if isinstance(explicit_rev, bool) or not isinstance(explicit_rev, int) or explicit_rev <= 0:
            raise FleetValidationError("--rev must be a positive integer")
        if explicit_rev <= prev_rev:
            raise FleetValidationError(
                f"refusing fleet rev regression: --rev {explicit_rev} <= current rev {prev_rev}"
            )
        next_rev = explicit_rev
    else:
        next_rev = prev_rev + 1

    url_list = parse_url_list(urls)
    candidate: dict[str, Any] = {
        "rev": next_rev,
        "commit": str(commit or "").strip(),
        "urls": url_list,
        "updated_at": cur_now,
    }
    if hub_peers is not None:
        candidate["hub_peers"] = parse_peer_list(hub_peers)
    if min_caps is not None:
        candidate["min_caps"] = _parse_caps_arg(min_caps)

    validated = validate_fleet_manifest(candidate, require_commit=True)
    _atomic_write_text(
        fp,
        json.dumps(validated, indent=2, ensure_ascii=False) + "\n",
        mode=0o600,
    )
    src_state = _auth.sync_source_auth_state(
        cdir,
        now=cur_now,
        explicit_auth_source=explicit_auth_source,
    )
    return validated, src_state.generation


# ----------------------------------------------------------------------
# Node local state (`fleet-applied.json`, `fleet-attempts.json`) & `fleet-ack`
# ----------------------------------------------------------------------


def load_fleet_applied(cfg_dir: pathlib.Path) -> dict[str, Any] | None:
    p = fleet_applied_path(cfg_dir)
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            return None
        rev = doc.get("rev")
        if isinstance(rev, bool) or not isinstance(rev, int) or rev <= 0:
            return None
        return doc
    except Exception:
        return None


def save_fleet_applied(cfg_dir: pathlib.Path, doc: dict[str, Any]) -> pathlib.Path:
    p = fleet_applied_path(cfg_dir)
    _atomic_write_text(p, json.dumps(doc, indent=2, ensure_ascii=False) + "\n", mode=0o600)
    return p


def record_fleet_applied_acked_hub(cfg_dir: pathlib.Path, rev: int, hub_key: str) -> None:
    clean_hk = str(hub_key or "").strip()
    if not clean_hk:
        return
    with _FLEET_CONVERGE_LOCK:
        doc = load_fleet_applied(cfg_dir)
        if doc is None or int(doc.get("rev") or 0) != int(rev):
            return
        acked = doc.get("acked_hubs")
        acked_list = [str(x) for x in acked] if isinstance(acked, list) else []
        if clean_hk not in acked_list:
            acked_list.append(clean_hk)
            doc["acked_hubs"] = acked_list
            save_fleet_applied(cfg_dir, doc)


def load_fleet_attempts(cfg_dir: pathlib.Path) -> dict[str, Any]:
    p = fleet_attempts_path(cfg_dir)
    if not p.is_file():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def save_fleet_attempts(cfg_dir: pathlib.Path, doc: dict[str, Any]) -> pathlib.Path:
    p = fleet_attempts_path(cfg_dir)
    _atomic_write_text(p, json.dumps(doc, indent=2, ensure_ascii=False) + "\n", mode=0o600)
    return p


def clear_fleet_attempts(cfg_dir: pathlib.Path) -> None:
    p = fleet_attempts_path(cfg_dir)
    if p.exists():
        with contextlib.suppress(OSError):
            p.unlink()


def build_fleet_ack_payload(
    applied: dict[str, Any],
    *,
    mid: str | None = None,
) -> dict[str, Any]:
    """Construct a durable `kind='event'`, `rkind='event'` `fleet-ack` outbound message."""
    ack_doc = {
        "rev": int(applied["rev"]),
        "commit": str(applied.get("commit") or ""),
        "target_commit": str(applied.get("target_commit") or ""),
        "result": str(applied["result"]),
        "config_changed": bool(applied.get("config_changed", False)),
        "code_changed": bool(applied.get("code_changed", False)),
        "attempts": int(applied.get("attempts") or 1),
        "updated_at": float(applied.get("applied_at") or time.time()),
    }
    msg_id = str(mid).strip() if mid else new_message_id()
    body_json = json.dumps(ack_doc, ensure_ascii=False, separators=(",", ":"))
    return {
        "v": 2,
        "op": "send",
        "id": msg_id,
        "corr_id": msg_id,
        "kind": "event",
        "rkind": "event",
        "to": FLEET_ACK_TARGET,
        "topic": "fleet-ack",
        "fleet_ack": ack_doc,
        "text": f"{FLEET_ACK_PREFIX}{body_json}",
    }


def parse_fleet_ack_payload(payload: dict[str, Any], text: str = "") -> dict[str, Any] | None:
    """Extract & validate `fleet-ack` fields from an incoming `publish_message` payload."""
    raw_ack = payload.get("fleet_ack")
    if not isinstance(raw_ack, dict):
        eff_text = text or str(payload.get("text") or "")
        if not eff_text.startswith(FLEET_ACK_PREFIX):
            return None
        try:
            raw_ack = json.loads(eff_text[len(FLEET_ACK_PREFIX):].strip())
        except Exception:
            return None
        if not isinstance(raw_ack, dict):
            return None

    raw_rev = raw_ack.get("rev")
    if isinstance(raw_rev, bool) or not isinstance(raw_rev, int) or raw_rev <= 0:
        return None
    raw_res = str(raw_ack.get("result") or "").strip()
    if not raw_res:
        return None
    raw_ts = raw_ack.get("updated_at")
    try:
        updated_at = float(raw_ts) if raw_ts is not None else time.time()
        if not math.isfinite(updated_at) or updated_at <= 0:
            updated_at = time.time()
    except (TypeError, ValueError):
        updated_at = time.time()

    try:
        attempts = max(1, int(raw_ack.get("attempts") or 1))
    except (TypeError, ValueError):
        attempts = 1

    return {
        "rev": int(raw_rev),
        "commit": str(raw_ack.get("commit") or "").strip(),
        "target_commit": str(raw_ack.get("target_commit") or "").strip(),
        "result": raw_res[:256],
        "config_changed": bool(raw_ack.get("config_changed", False)),
        "code_changed": bool(raw_ack.get("code_changed", False)),
        "attempts": attempts,
        "updated_at": updated_at,
    }


# ----------------------------------------------------------------------
# Git & Config Convergence Engine
# ----------------------------------------------------------------------


def resolve_fleet_repo_dir() -> pathlib.Path:
    """Return target Git repository root (`BUS_FLEET_REPO_DIR` override or package repo root)."""
    env_override = (os.environ.get("BUS_FLEET_REPO_DIR") or "").strip()
    if env_override:
        return pathlib.Path(env_override).expanduser().resolve()
    return pathlib.Path(__file__).resolve().parents[2]


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("LC_ALL", "C")
    return env


def _run_git(
    repo_dir: pathlib.Path,
    args: list[str],
    *,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        env=_git_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _read_git_head_safe(repo_dir: pathlib.Path, *, timeout: float = 5.0) -> str:
    if not repo_dir.exists():
        return ""
    try:
        res = _run_git(repo_dir, ["rev-parse", "--verify", "HEAD"], timeout=timeout)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def _resolve_local_commit_sha(repo_dir: pathlib.Path, spec: str, *, timeout: float = 5.0) -> str:
    try:
        res = _run_git(repo_dir, ["rev-parse", "--verify", "--quiet", f"{spec}^{{commit}}"], timeout=timeout)
        if res.returncode == 0 and res.stdout.strip():
            sha = res.stdout.strip()
            chk = _run_git(repo_dir, ["cat-file", "-e", f"{sha}^{{commit}}"], timeout=timeout)
            if chk.returncode == 0:
                return sha
    except Exception:
        pass
    return ""


def _is_dirty_tracked_worktree(repo_dir: pathlib.Path, *, timeout: float = 10.0) -> bool:
    """Return True if tracked files are modified/staged or a merge/rebase is in progress."""
    try:
        git_dir_res = _run_git(repo_dir, ["rev-parse", "--git-dir"], timeout=5.0)
        if git_dir_res.returncode == 0 and git_dir_res.stdout.strip():
            gdir = pathlib.Path(git_dir_res.stdout.strip())
            if not gdir.is_absolute():
                gdir = repo_dir / gdir
            for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply"):
                if (gdir / marker).exists():
                    return True
        st = _run_git(repo_dir, ["status", "--porcelain", "--untracked-files=no"], timeout=timeout)
        if st.returncode != 0:
            return True
        return bool(st.stdout.strip())
    except Exception:
        return True


def _is_commit_on_origin(repo_dir: pathlib.Path, resolved_sha: str, *, timeout: float = 10.0) -> bool:
    """Verify `resolved_sha` is reachable from at least one `refs/remotes/origin/*` ref."""
    try:
        res = _run_git(
            repo_dir,
            ["for-each-ref", f"--contains={resolved_sha}", "--format=%(refname)", "refs/remotes/origin"],
            timeout=timeout,
        )
        if res.returncode == 0 and res.stdout.strip():
            return True
        # Fallback if clone has no remote-tracking refspec but updated FETCH_HEAD from origin
        refs_check = _run_git(
            repo_dir,
            ["for-each-ref", "--format=%(refname)", "refs/remotes/origin"],
            timeout=timeout,
        )
        if refs_check.returncode == 0 and not refs_check.stdout.strip():
            anc = _run_git(repo_dir, ["merge-base", "--is-ancestor", resolved_sha, "FETCH_HEAD"], timeout=timeout)
            return anc.returncode == 0
    except Exception:
        pass
    return False


def should_attempt_convergence(
    cfg_dir: pathlib.Path,
    incoming_fleet: Any,
) -> dict[str, Any] | None:
    """Return validated `incoming_fleet` iff valid and `incoming.rev > local_applied.rev`."""
    try:
        validated = validate_fleet_manifest(incoming_fleet, require_commit=False)
    except FleetValidationError as exc:
        print(
            f"[fleet-warn] ignoring invalid incoming fleet payload: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None
    applied = load_fleet_applied(cfg_dir)
    local_rev = int(applied.get("rev") or 0) if applied is not None else 0
    if validated["rev"] <= local_rev:
        return None
    return validated


def _converge_config_json(
    cfg_dir: pathlib.Path,
    validated: dict[str, Any],
) -> bool:
    """Backup `<cfg_dir>/config.json` -> `config.json.bak` (`fsync`) and atomically update `urls`/`hub_peers`.

    Preserves all unknown custom keys verbatim. Also updates `"url"` to `urls[0]` if `"url"` was
    already present in `config.json` so legacy `get_bus_url()` stays consistent with `get_bus_urls()`.
    Returns `True` iff `config.json` was created or modified.
    """
    cfg_path = config_file_path(cfg_dir)
    existing_bytes: bytes | None = None
    existing_cfg: dict[str, Any] = {}
    corrupt_existing = False
    if cfg_path.is_file():
        existing_bytes = cfg_path.read_bytes()
        try:
            parsed = json.loads(existing_bytes.decode("utf-8"))
            if isinstance(parsed, dict):
                existing_cfg = parsed
            else:
                corrupt_existing = True
        except Exception:
            corrupt_existing = True

    new_urls = list(validated["urls"])
    has_hub_peers = "hub_peers" in validated and validated["hub_peers"] is not None
    new_hub_peers = list(validated["hub_peers"]) if has_hub_peers else None

    needs_write = (
        (existing_bytes is None)
        or corrupt_existing
        or (existing_cfg.get("urls") != new_urls)
        or ("url" in existing_cfg and existing_cfg.get("url") != new_urls[0])
        or (new_hub_peers is not None and existing_cfg.get("hub_peers") != new_hub_peers)
    )
    if not needs_write:
        return False

    if existing_bytes is not None:
        bak_path = cfg_path.with_name("config.json.bak")
        _atomic_write_bytes(bak_path, existing_bytes, mode=0o600)

    updated_cfg = copy.deepcopy(existing_cfg)
    updated_cfg["urls"] = new_urls
    if "url" in updated_cfg:
        updated_cfg["url"] = new_urls[0]
    if new_hub_peers is not None:
        updated_cfg["hub_peers"] = new_hub_peers

    _atomic_write_text(
        cfg_path,
        json.dumps(updated_cfg, indent=2, ensure_ascii=False) + "\n",
        mode=0o600,
    )
    return True


def converge_fleet_once(
    cfg_dir: pathlib.Path,
    incoming_fleet: Any,
    *,
    auto_update: bool = True,
    supported_caps: Iterable[str] = CLIENT_CAPS,
    repo_dir: pathlib.Path | None = None,
    git_timeout: float | None = None,
    max_attempts: int = MAX_FLEET_ATTEMPTS,
    now: float | None = None,
) -> FleetConvergenceOutcome:
    """Execute one deterministic self-convergence pass for `incoming_fleet`."""
    cdir = pathlib.Path(cfg_dir)
    with _FLEET_CONVERGE_LOCK:
        validated = should_attempt_convergence(cdir, incoming_fleet)
        if validated is None:
            return FleetConvergenceOutcome(action="ignored", reason="not_needed_or_invalid")

        rev = int(validated["rev"])
        target_commit = str(validated.get("commit") or "").strip()
        cur_now = float(now) if now is not None else time.time()
        eff_repo = (
            pathlib.Path(repo_dir).expanduser().resolve()
            if repo_dir is not None
            else resolve_fleet_repo_dir()
        )
        eff_timeout = (
            max(0.2, float(git_timeout))
            if git_timeout is not None
            else get_fleet_git_timeout(config_dir=cdir)
        )

        # 1) Capabilities safety gate (`min_caps`)
        req_caps = validated.get("min_caps") or []
        sup_caps = {str(c) for c in supported_caps}
        missing_caps = [c for c in req_caps if c not in sup_caps]
        if missing_caps:
            clear_fleet_attempts(cdir)
            doc = {
                "rev": rev,
                "commit": _read_git_head_safe(eff_repo),
                "target_commit": target_commit,
                "applied_at": cur_now,
                "result": "blocked:missing_caps",
                "missing_caps": missing_caps,
                "config_changed": False,
                "code_changed": False,
                "attempts": 1,
                "acked_hubs": [],
            }
            save_fleet_applied(cdir, doc)
            return FleetConvergenceOutcome(
                action="blocked",
                applied_doc=doc,
                needs_restart=False,
                attempts=1,
                reason="blocked:missing_caps",
            )

        att_state = load_fleet_attempts(cdir)
        prev_att = int(att_state.get("attempts") or 0) if int(att_state.get("rev") or 0) == rev else 0
        attempt_num = prev_att + 1

        def _block_immediately(err_tag: str) -> FleetConvergenceOutcome:
            clear_fleet_attempts(cdir)
            b_doc = {
                "rev": rev,
                "commit": _read_git_head_safe(eff_repo),
                "target_commit": target_commit,
                "applied_at": cur_now,
                "result": f"blocked:{err_tag}",
                "config_changed": False,
                "code_changed": False,
                "attempts": attempt_num,
                "acked_hubs": [],
            }
            save_fleet_applied(cdir, b_doc)
            return FleetConvergenceOutcome(
                action="blocked",
                applied_doc=b_doc,
                needs_restart=False,
                attempts=attempt_num,
                reason=f"blocked:{err_tag}",
            )

        def _fail_transient(err_tag: str) -> FleetConvergenceOutcome:
            save_fleet_attempts(
                cdir,
                {
                    "rev": rev,
                    "attempts": attempt_num,
                    "last_error": err_tag,
                    "updated_at": cur_now,
                },
            )
            if attempt_num >= max(1, int(max_attempts)):
                clear_fleet_attempts(cdir)
                b_doc = {
                    "rev": rev,
                    "commit": _read_git_head_safe(eff_repo),
                    "target_commit": target_commit,
                    "applied_at": cur_now,
                    "result": f"blocked:{err_tag}",
                    "config_changed": False,
                    "code_changed": False,
                    "attempts": attempt_num,
                    "acked_hubs": [],
                }
                save_fleet_applied(cdir, b_doc)
                return FleetConvergenceOutcome(
                    action="blocked",
                    applied_doc=b_doc,
                    needs_restart=False,
                    attempts=attempt_num,
                    reason=f"blocked:{err_tag}",
                )
            return FleetConvergenceOutcome(
                action="retry_later",
                applied_doc=None,
                needs_restart=False,
                attempts=attempt_num,
                reason=err_tag,
            )

        # 2) Git commit convergence (`--ff-only`; skipped when `--no-auto-update`)
        initial_head = _read_git_head_safe(eff_repo)
        final_head = initial_head
        code_changed = False

        if auto_update and target_commit:
            if not initial_head:
                return _fail_transient("not_a_git_repo")

            already_at_commit = initial_head.lower() == target_commit.lower()
            if not already_at_commit and len(target_commit) >= 7:
                local_resolved = _resolve_local_commit_sha(eff_repo, target_commit)
                if local_resolved and local_resolved.lower() == initial_head.lower():
                    already_at_commit = True

            if not already_at_commit:
                if _is_dirty_tracked_worktree(eff_repo, timeout=eff_timeout):
                    return _block_immediately("dirty_worktree")

                try:
                    fetch_res = _run_git(
                        eff_repo,
                        ["fetch", "--prune", "origin"],
                        timeout=eff_timeout,
                    )
                except subprocess.TimeoutExpired:
                    return _fail_transient("fetch_timeout")
                except Exception:
                    return _fail_transient("fetch_failed")

                if fetch_res.returncode != 0:
                    return _fail_transient("fetch_failed")

                resolved_sha = _resolve_local_commit_sha(eff_repo, target_commit, timeout=eff_timeout)
                if not resolved_sha or not _is_commit_on_origin(
                    eff_repo, resolved_sha, timeout=eff_timeout
                ):
                    return _fail_transient("commit_not_on_origin")

                if resolved_sha.lower() != initial_head.lower():
                    anc_res = _run_git(
                        eff_repo,
                        ["merge-base", "--is-ancestor", initial_head, resolved_sha],
                        timeout=eff_timeout,
                    )
                    if anc_res.returncode != 0:
                        return _block_immediately("non_fast_forward")

                    try:
                        merge_res = _run_git(
                            eff_repo,
                            ["merge", "--ff-only", resolved_sha],
                            timeout=eff_timeout,
                        )
                    except Exception:
                        return _block_immediately("non_fast_forward")

                    if merge_res.returncode != 0:
                        mh = _run_git(eff_repo, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], timeout=5.0)
                        if mh.returncode == 0:
                            with contextlib.suppress(Exception):
                                _run_git(eff_repo, ["merge", "--abort"], timeout=5.0)
                        return _block_immediately("non_fast_forward")

                    final_head = _read_git_head_safe(eff_repo) or resolved_sha
                    code_changed = final_head.lower() != initial_head.lower()

        # 3) `config.json` atomic convergence (`.bak` + preserve unknown keys)
        try:
            config_changed = _converge_config_json(cdir, validated)
        except Exception:
            return _fail_transient("config_write_failed")

        clear_fleet_attempts(cdir)
        result_str = "ok:no_auto_update" if not auto_update else "ok"
        applied_doc = {
            "rev": rev,
            "commit": final_head,
            "target_commit": target_commit,
            "applied_at": cur_now,
            "result": result_str,
            "commit_skipped": (not auto_update),
            "config_changed": config_changed,
            "code_changed": code_changed,
            "attempts": attempt_num,
            "acked_hubs": [],
        }
        save_fleet_applied(cdir, applied_doc)
        return FleetConvergenceOutcome(
            action="applied",
            applied_doc=applied_doc,
            needs_restart=bool(config_changed or code_changed),
            attempts=attempt_num,
            reason=result_str,
        )
