"""Dual-Auth (tailscale whois internal + Bearer/HMAC-SHA256 external), scope isolation, and I4 auth replication."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import fnmatch
import hashlib
import hmac
import ipaddress
import json
import math
import os
import pathlib
import secrets
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Iterable

from .config import get_auth_source
from .store import BusStore

REPLAY_WINDOW_SECONDS = 300
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_WHOIS_CACHE: dict[str, tuple[str, float]] = {}
_AUTH_STATE_LOCK = threading.RLock()

# Optional deterministic crash/fault hook invoked in `apply_replica_manifest` AFTER writing &
# fsyncing `cfg_dir/auth-gen/<gen>/{allow.txt,tokens.json,manifest.json}` and IMMEDIATELY BEFORE
# atomically swapping `cfg_dir/auth-current` (`X10(c)`).
_AUTH_POINTER_CRASH_HOOK: Callable[[int, pathlib.Path], None] | None = None


class ReplicaReadOnlyError(PermissionError):
    """Raised when attempting to directly mutate `allow.txt` or `tokens.json` on a replica hub."""


class AuthSyncError(RuntimeError):
    """Raised when `auth_sync` pull, authorization, integrity check, or monotonicity check fails."""


@dataclasses.dataclass
class AuthResult:
    ok: bool
    node: str = ""
    trust: str = "internal"  # "internal" | "external"
    scopes: list[str] = dataclasses.field(default_factory=list)
    allow_exec: bool = True
    token_id: str | None = None
    error: str = ""
    code: int = 200


@dataclasses.dataclass(frozen=True)
class SourceAuthState:
    generation: int
    allow: list[str]
    tokens: list[dict[str, Any]]
    tombstones: list[str]
    updated_at: float
    changed: bool


@dataclasses.dataclass(frozen=True)
class ReplicaApplyResult:
    generation: int
    updated: bool
    synced_at: float
    sha256: str


def set_auth_pointer_crash_hook(hook: Callable[[int, pathlib.Path], None] | None) -> None:
    """Register or clear an in-process crash/exception hook before `auth-current` pointer swap."""
    global _AUTH_POINTER_CRASH_HOOK
    _AUTH_POINTER_CRASH_HOOK = hook


def _fire_auth_pointer_crash_hook(generation: int, cfg_dir: pathlib.Path) -> None:
    hook = _AUTH_POINTER_CRASH_HOOK
    if hook is not None:
        hook(generation, cfg_dir)

    env_spec = (os.environ.get("BUS_TEST_CRASH_BEFORE_AUTH_POINTER") or "").strip()
    if not env_spec:
        return
    if env_spec == "raise":
        raise RuntimeError(f"injected crash before auth-current pointer swap (gen={generation})")
    if env_spec.startswith("raise_once:"):
        marker = pathlib.Path(env_spec[len("raise_once:"):]).expanduser()
        if not marker.exists():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"{generation}\n", encoding="utf-8")
            raise RuntimeError(f"injected one-shot exception before auth-current swap (gen={generation})")
        return
    if env_spec.startswith(("once:", "exit_once:")):
        prefix = "once:" if env_spec.startswith("once:") else "exit_once:"
        marker = pathlib.Path(env_spec[len(prefix):]).expanduser()
        if not marker.exists():
            marker.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, f"{generation}\n".encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os._exit(91)


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


def normalize_ip(ip_str: str) -> str:
    if not ip_str:
        return ""
    clean = ip_str.split("%", 1)[0].strip()
    try:
        addr = ipaddress.ip_address(clean)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(addr)
    except ValueError:
        return clean


def is_tailnet_ip(ip_str: str) -> bool:
    clean = normalize_ip(ip_str)
    if not clean:
        return False
    try:
        addr = ipaddress.ip_address(clean)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv4Address):
        return addr in _TAILNET_V4
    return addr in _TAILNET_V6


def is_loopback_ip(ip_str: str) -> bool:
    clean = normalize_ip(ip_str)
    if not clean:
        return False
    try:
        return ipaddress.ip_address(clean).is_loopback
    except ValueError:
        return False


def _local_node_name() -> str:
    try:
        r = subprocess.run(["tailscale", "status", "--self", "--json"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            self_node = json.loads(r.stdout).get("Self", {})
            name = (self_node.get("HostName") or self_node.get("DNSName") or "").split(".")[0].lower()
            if name:
                return name
    except Exception:
        pass
    return socket.gethostname().split(".")[0].lower() or "localhost"


def is_replica_mode(cfg_dir: pathlib.Path, *, explicit_auth_source: str | None = None) -> bool:
    """Return True when `cfg_dir` operates as a read-only `auth_sync` replica (`auth_source` set)."""
    return bool(get_auth_source(config_dir=pathlib.Path(cfg_dir), explicit=explicit_auth_source))


def ensure_writable_source_mode(cfg_dir: pathlib.Path, *, explicit_auth_source: str | None = None) -> None:
    if is_replica_mode(cfg_dir, explicit_auth_source=explicit_auth_source):
        raise ReplicaReadOnlyError(
            f"Auth directory {cfg_dir} is configured in replica mode (auth_source is set); "
            "direct edits to allow/tokens are forbidden (single-writer source hub only)."
        )


def auth_generation_path(cfg_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(cfg_dir) / "auth-generation"


def auth_source_state_path(cfg_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(cfg_dir) / "auth-source-state.json"


def auth_current_pointer_path(cfg_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(cfg_dir) / "auth-current"


def auth_sync_state_path(cfg_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(cfg_dir) / "auth-sync-state.json"


def read_replica_current_generation(cfg_dir: pathlib.Path) -> int:
    """Return active replica generation integer (`>0`) from `auth-current`, or `0` if unsynced."""
    cdir = pathlib.Path(cfg_dir)
    ptr = auth_current_pointer_path(cdir)
    if not ptr.is_file():
        return 0
    try:
        raw = ptr.read_text(encoding="utf-8").strip()
        gen = int(raw)
    except Exception:
        return 0
    if gen <= 0:
        return 0
    gdir = cdir / "auth-gen" / str(gen)
    if not gdir.is_dir():
        return 0
    if not (gdir / "allow.txt").is_file() or not (gdir / "tokens.json").is_file():
        return 0
    return gen


def read_replica_last_sync_at(cfg_dir: pathlib.Path) -> float | None:
    """Return last successful `auth_sync` wall-clock timestamp on a replica, or `None`."""
    cdir = pathlib.Path(cfg_dir)
    if read_replica_current_generation(cdir) <= 0:
        return None
    sp = auth_sync_state_path(cdir)
    if sp.is_file():
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
            ts = float(data.get("last_sync_at"))
            if math.isfinite(ts) and ts > 0:
                return ts
        except Exception:
            pass
    gdir = replica_gen_dir(cdir)
    if gdir is not None and (gdir / "manifest.json").is_file():
        try:
            m = json.loads((gdir / "manifest.json").read_text(encoding="utf-8"))
            ts = float(m.get("issued_at"))
            if math.isfinite(ts) and ts > 0:
                return ts
        except Exception:
            pass
    return None


def replica_gen_dir(cfg_dir: pathlib.Path, generation: int | None = None) -> pathlib.Path | None:
    cdir = pathlib.Path(cfg_dir)
    gen = generation if generation is not None else read_replica_current_generation(cdir)
    if gen <= 0:
        return None
    gdir = cdir / "auth-gen" / str(gen)
    return gdir if gdir.is_dir() else None


def _parse_allow_text(text: str) -> set[str]:
    return {
        line.strip().lower()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def allowed_nodes(cfg_dir: pathlib.Path, *, explicit_auth_source: str | None = None) -> set[str]:
    cdir = pathlib.Path(cfg_dir)
    if is_replica_mode(cdir, explicit_auth_source=explicit_auth_source):
        # Replica mode strictly reads ONLY from `cfg_dir/auth-gen/<auth-current>/allow.txt`
        # and fails closed (`set()`) before the first successful sync.
        gdir = replica_gen_dir(cdir)
        if gdir is None:
            return set()
        p_rep = gdir / "allow.txt"
        if not p_rep.is_file():
            return set()
        try:
            return _parse_allow_text(p_rep.read_text(encoding="utf-8"))
        except Exception:
            return set()

    p = cdir / "allow.txt"
    if not p.exists():
        cdir.mkdir(parents=True, exist_ok=True)
        default_node = _local_node_name()
        p.write_text(f"{default_node}\n", encoding="utf-8")
    return _parse_allow_text(p.read_text(encoding="utf-8"))


def write_allow_nodes(
    cfg_dir: pathlib.Path,
    nodes: Iterable[str],
    *,
    explicit_auth_source: str | None = None,
) -> pathlib.Path:
    """Atomically update `allow.txt` on a source hub and bump `auth-generation`."""
    cdir = pathlib.Path(cfg_dir)
    ensure_writable_source_mode(cdir, explicit_auth_source=explicit_auth_source)
    items = [str(n).strip().lower() for n in nodes if str(n).strip()]
    p = cdir / "allow.txt"
    _atomic_write_text(p, "\n".join(items) + ("\n" if items else ""), mode=0o600)
    sync_source_auth_state(cdir, explicit_auth_source=explicit_auth_source)
    return p


def whois_sync(ip_str: str) -> str:
    ip_norm = normalize_ip(ip_str)
    cmd = os.environ.get("BUS_WHOIS_CMD")
    args = [cmd, ip_norm] if cmd else ["tailscale", "whois", "--json", ip_norm]
    r = subprocess.run(args, capture_output=True, text=True, timeout=5)
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    data = json.loads(r.stdout)
    node = data.get("Node") or {}
    return (node.get("ComputedName") or node.get("Name") or "").split(".")[0].lower()


async def whois_async(ip_str: str) -> str:
    now = time.time()
    cached = _WHOIS_CACHE.get(ip_str)
    if cached and now - cached[1] < 60.0:
        return cached[0]
    try:
        node = await asyncio.get_running_loop().run_in_executor(None, whois_sync, ip_str)
    except Exception:
        node = ""
    _WHOIS_CACHE[ip_str] = (node, now)
    return node


def clear_whois_cache() -> None:
    _WHOIS_CACHE.clear()


def tokens_file_path(cfg_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(cfg_dir) / "tokens.json"


def _is_token_expired(item: dict[str, Any], now: float) -> bool:
    raw_exp = item.get("expires_at")
    if raw_exp is None or raw_exp == "":
        return False
    try:
        exp_val = float(raw_exp)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(exp_val):
        return True
    return now >= exp_val


def load_tombstones(cfg_dir: pathlib.Path, *, explicit_auth_source: str | None = None) -> set[str]:
    """Return cumulative revoked/deleted token IDs for either source or replica mode."""
    cdir = pathlib.Path(cfg_dir)
    out: set[str] = set()
    if is_replica_mode(cdir, explicit_auth_source=explicit_auth_source):
        gdir = replica_gen_dir(cdir)
        if gdir is None:
            return out
        for fname in ("manifest.json", "tokens.json"):
            fp = gdir / fname
            if not fp.is_file():
                continue
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                for tid in data.get("tombstones", []) or []:
                    s = str(tid or "").strip()
                    if s:
                        out.add(s)
                for item in data.get("tokens", []) or []:
                    if isinstance(item, dict) and item.get("disabled", False):
                        s = str(item.get("id") or "").strip()
                        if s:
                            out.add(s)
        return out

    sp = auth_source_state_path(cdir)
    if sp.is_file():
        try:
            st = json.loads(sp.read_text(encoding="utf-8"))
            if isinstance(st, dict):
                for tid in st.get("tombstones", []) or []:
                    s = str(tid or "").strip()
                    if s:
                        out.add(s)
        except Exception:
            pass
    tp = tokens_file_path(cdir)
    if tp.is_file():
        try:
            tdata = json.loads(tp.read_text(encoding="utf-8"))
            if isinstance(tdata, dict):
                for tid in tdata.get("tombstones", []) or []:
                    s = str(tid or "").strip()
                    if s:
                        out.add(s)
                for item in tdata.get("tokens", []) or []:
                    if isinstance(item, dict) and item.get("disabled", False):
                        s = str(item.get("id") or "").strip()
                        if s:
                            out.add(s)
                # Remove only if explicitly re-enabled (`disabled: false`) in current tokens.json.
                for item in tdata.get("tokens", []) or []:
                    if isinstance(item, dict) and not item.get("disabled", False):
                        s = str(item.get("id") or "").strip()
                        out.discard(s)
        except Exception:
            pass
    return out


def load_tokens(
    cfg_dir: pathlib.Path,
    *,
    explicit_auth_source: str | None = None,
    now: float | None = None,
) -> dict[str, dict[str, Any]]:
    cdir = pathlib.Path(cfg_dir)
    if is_replica_mode(cdir, explicit_auth_source=explicit_auth_source):
        gdir = replica_gen_dir(cdir)
        if gdir is None:
            return {}
        p = gdir / "tokens.json"
    else:
        p = tokens_file_path(cdir)

    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    tombstones = load_tombstones(cdir, explicit_auth_source=explicit_auth_source)
    cur_now = float(now) if now is not None else time.time()
    items = data.get("tokens", [])
    if not isinstance(items, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or "").strip()
        if not tid:
            continue
        if item.get("disabled", False) or tid in tombstones:
            continue
        if _is_token_expired(item, cur_now):
            continue
        out[tid] = item
    return out


def sync_source_auth_state(
    cfg_dir: pathlib.Path,
    *,
    now: float | None = None,
    explicit_auth_source: str | None = None,
) -> SourceAuthState:
    """Detect SHA-256 changes in `allow.txt` or `tokens.json`, monotonically advance
    `auth-generation`, and accumulate `tombstones` for revoked/disabled/deleted tokens."""
    cdir = pathlib.Path(cfg_dir)
    ensure_writable_source_mode(cdir, explicit_auth_source=explicit_auth_source)
    cur_now = float(now) if now is not None else time.time()

    with _AUTH_STATE_LOCK:
        cdir.mkdir(parents=True, exist_ok=True)
        allow_path = cdir / "allow.txt"
        if not allow_path.exists():
            default_node = _local_node_name()
            _atomic_write_text(allow_path, f"{default_node}\n", mode=0o600)

        allow_bytes = allow_path.read_bytes()
        allow_set = _parse_allow_text(allow_bytes.decode("utf-8", errors="replace"))
        allow_list = sorted(allow_set)
        allow_sha = hashlib.sha256(allow_bytes).hexdigest()

        tok_path = tokens_file_path(cdir)
        tokens_list: list[dict[str, Any]] = []
        file_tombstones: set[str] = set()
        if tok_path.exists():
            tok_bytes = tok_path.read_bytes()
            tok_sha = hashlib.sha256(tok_bytes).hexdigest()
            raw_txt = tok_bytes.decode("utf-8")
            if raw_txt.strip():
                parsed_tok = json.loads(raw_txt)
                if not isinstance(parsed_tok, dict):
                    raise ValueError(f"{tok_path} must contain a JSON object")
                raw_items = parsed_tok.get("tokens", [])
                if not isinstance(raw_items, list):
                    raise ValueError(f"{tok_path} 'tokens' must be a list")
                for it in raw_items:
                    if isinstance(it, dict):
                        tokens_list.append(copy.deepcopy(it))
                for tb in parsed_tok.get("tombstones", []) or []:
                    s = str(tb or "").strip()
                    if s:
                        file_tombstones.add(s)
        else:
            tok_sha = "missing"

        fingerprint = f"allow:{allow_sha}|tokens:{tok_sha}"

        gen_file = auth_generation_path(cdir)
        state_file = auth_source_state_path(cdir)

        disk_gen = 0
        has_gen_file = gen_file.is_file()
        if has_gen_file:
            try:
                disk_gen = max(0, int(gen_file.read_text(encoding="utf-8").strip()))
            except Exception:
                disk_gen = 0

        prev_state: dict[str, Any] = {}
        has_state_file = state_file.is_file()
        if has_state_file:
            try:
                loaded_st = json.loads(state_file.read_text(encoding="utf-8"))
                if isinstance(loaded_st, dict):
                    prev_state = loaded_st
            except Exception:
                prev_state = {}

        state_gen = max(0, int(prev_state.get("generation") or 0))
        prev_gen = max(disk_gen, state_gen)
        prev_fp = str(prev_state.get("fingerprint") or "")
        prev_known_ids = {
            str(x).strip() for x in (prev_state.get("known_token_ids") or []) if str(x).strip()
        }
        prev_tombstones = {
            str(x).strip() for x in (prev_state.get("tombstones") or []) if str(x).strip()
        }
        prev_updated_at = float(prev_state.get("updated_at") or cur_now)

        cur_all_ids: set[str] = set()
        cur_disabled_ids: set[str] = set()
        cur_active_ids: set[str] = set()
        for it in tokens_list:
            tid = str(it.get("id") or "").strip()
            if not tid:
                continue
            cur_all_ids.add(tid)
            if it.get("disabled", False):
                cur_disabled_ids.add(tid)
            else:
                cur_active_ids.add(tid)

        deleted_ids = prev_known_ids - cur_all_ids
        tombstones_set = (
            (prev_tombstones | file_tombstones | cur_disabled_ids | deleted_ids) - cur_active_ids
        )
        tombstones_list = sorted(tombstones_set)
        known_ids_list = sorted(prev_known_ids | cur_all_ids | tombstones_set)

        if prev_gen <= 0:
            new_gen = 1
            changed = True
            updated_at = cur_now
        elif not prev_fp:
            # `auth-generation` existed manually without `auth-source-state.json`: adopt `prev_gen`.
            new_gen = prev_gen
            changed = True
            updated_at = cur_now
        elif fingerprint != prev_fp:
            new_gen = prev_gen + 1
            changed = True
            updated_at = cur_now
        else:
            new_gen = prev_gen
            changed = False
            updated_at = prev_updated_at

        if changed or not has_gen_file or not has_state_file or disk_gen != new_gen:
            new_state_obj = {
                "generation": new_gen,
                "fingerprint": fingerprint,
                "known_token_ids": known_ids_list,
                "tombstones": tombstones_list,
                "updated_at": updated_at,
            }
            _atomic_write_text(
                state_file,
                json.dumps(new_state_obj, indent=2, ensure_ascii=False) + "\n",
                mode=0o600,
            )
            _atomic_write_text(gen_file, f"{new_gen}\n", mode=0o600)

        return SourceAuthState(
            generation=new_gen,
            allow=allow_list,
            tokens=tokens_list,
            tombstones=tombstones_list,
            updated_at=updated_at,
            changed=changed,
        )


def compute_manifest_sha256(
    *,
    generation: int,
    allow: list[str],
    tokens: list[dict[str, Any]],
    tombstones: list[str],
) -> str:
    canonical_payload = {
        "generation": int(generation),
        "allow": [str(a) for a in allow],
        "tokens": tokens,
        "tombstones": [str(t) for t in tombstones],
    }
    canonical_bytes = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def build_auth_sync_manifest(
    cfg_dir: pathlib.Path,
    *,
    now: float | None = None,
    explicit_auth_source: str | None = None,
) -> dict[str, Any]:
    """Build the authoritative `auth_sync` manifest on a source hub."""
    st = sync_source_auth_state(cfg_dir, now=now, explicit_auth_source=explicit_auth_source)
    issued_at = float(now) if now is not None else time.time()
    sha256 = compute_manifest_sha256(
        generation=st.generation,
        allow=st.allow,
        tokens=st.tokens,
        tombstones=st.tombstones,
    )
    return {
        "generation": st.generation,
        "sha256": sha256,
        "allow": st.allow,
        "tokens": st.tokens,
        "tombstones": st.tombstones,
        "issued_at": issued_at,
    }


def validate_auth_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Verify schema, types, per-token secret hashes, and canonical `sha256` of a manifest."""
    if not isinstance(manifest, dict):
        raise AuthSyncError("manifest must be a JSON object")

    raw_gen = manifest.get("generation")
    if isinstance(raw_gen, bool) or not isinstance(raw_gen, int) or raw_gen < 1:
        raise AuthSyncError(f"invalid manifest generation: {raw_gen!r}")

    raw_sha = str(manifest.get("sha256") or "").strip().lower()
    if len(raw_sha) != 64 or any(c not in "0123456789abcdef" for c in raw_sha):
        raise AuthSyncError("invalid manifest sha256")

    raw_allow = manifest.get("allow")
    if not isinstance(raw_allow, list) or any(not isinstance(x, str) for x in raw_allow):
        raise AuthSyncError("manifest 'allow' must be a list of strings")

    raw_tokens = manifest.get("tokens")
    if not isinstance(raw_tokens, list) or any(not isinstance(x, dict) for x in raw_tokens):
        raise AuthSyncError("manifest 'tokens' must be a list of objects")

    raw_tombstones = manifest.get("tombstones")
    if not isinstance(raw_tombstones, list) or any(not isinstance(x, str) for x in raw_tombstones):
        raise AuthSyncError("manifest 'tombstones' must be a list of strings")

    raw_issued = manifest.get("issued_at")
    try:
        issued_at = float(raw_issued)
    except (TypeError, ValueError) as exc:
        raise AuthSyncError("invalid manifest issued_at") from exc
    if not math.isfinite(issued_at) or issued_at <= 0:
        raise AuthSyncError("non-positive or non-finite manifest issued_at")

    expected_sha = compute_manifest_sha256(
        generation=raw_gen,
        allow=raw_allow,
        tokens=raw_tokens,
        tombstones=raw_tombstones,
    )
    if not hmac.compare_digest(expected_sha, raw_sha):
        raise AuthSyncError(
            f"manifest sha256 mismatch: expected={expected_sha} got={raw_sha}"
        )

    for tok in raw_tokens:
        tid = str(tok.get("id") or "").strip()
        if not tid:
            raise AuthSyncError("manifest token missing 'id'")
        sec = tok.get("secret")
        sec_sha = tok.get("secret_sha256")
        if isinstance(sec, str) and sec and isinstance(sec_sha, str) and sec_sha:
            calc_sec_sha = hashlib.sha256(sec.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(calc_sec_sha.lower(), sec_sha.lower()):
                raise AuthSyncError(f"token {tid} secret_sha256 mismatch")

    norm_allow = sorted(
        {x.strip().lower() for x in raw_allow if x.strip() and not x.strip().startswith("#")}
    )
    norm_tombstones = sorted({x.strip() for x in raw_tombstones if x.strip()})
    return {
        "generation": raw_gen,
        "sha256": raw_sha,
        "allow": norm_allow,
        "raw_allow": list(raw_allow),
        "tokens": copy.deepcopy(raw_tokens),
        "tombstones": norm_tombstones,
        "issued_at": issued_at,
    }


def apply_replica_manifest(
    cfg_dir: pathlib.Path,
    manifest: dict[str, Any],
    *,
    now: float | None = None,
) -> ReplicaApplyResult:
    """Validate and atomically apply an `auth_sync` manifest on a replica hub (`design.md` §3.4).

    - Rejects corrupted manifests (`sha256` mismatch) and generation downgrades (`gen < cur_gen`).
    - When `gen == cur_gen`, verifies hash consistency without rewriting generation files,
      updating only `last_sync_at` so healthy steady-state replicas never go falsely stale.
    - When `gen > cur_gen`, writes & fsyncs `allow.txt`, `tokens.json` (`0600`), and
      `manifest.json` (`0600`) inside `cfg_dir/auth-gen/<gen>/`, runs the optional crash hook,
      and ONLY THEN atomically swaps `cfg_dir/auth-current` via temp-file + `fsync` + `os.replace`.
    """
    cdir = pathlib.Path(cfg_dir)
    validated = validate_auth_manifest(manifest)
    gen = int(validated["generation"])
    sha256 = str(validated["sha256"])
    sync_ts = float(now) if now is not None else time.time()

    with _AUTH_STATE_LOCK:
        cdir.mkdir(parents=True, exist_ok=True)
        cur_gen = read_replica_current_generation(cdir)
        if gen < cur_gen:
            raise AuthSyncError(
                f"refusing auth generation downgrade: incoming={gen} < current={cur_gen}"
            )

        if gen == cur_gen and cur_gen > 0:
            cur_gdir = cdir / "auth-gen" / str(cur_gen)
            cur_manifest_path = cur_gdir / "manifest.json"
            if cur_manifest_path.is_file():
                try:
                    existing_m = json.loads(cur_manifest_path.read_text(encoding="utf-8"))
                    existing_sha = str(existing_m.get("sha256") or "").strip().lower()
                except Exception as exc:
                    raise AuthSyncError("corrupted local manifest.json on current generation") from exc
                if existing_sha and not hmac.compare_digest(existing_sha, sha256):
                    raise AuthSyncError(
                        f"manifest sha256 conflict on identical generation {gen}"
                    )
            _atomic_write_text(
                auth_sync_state_path(cdir),
                json.dumps(
                    {"generation": cur_gen, "last_sync_at": sync_ts, "sha256": sha256},
                    indent=2,
                )
                + "\n",
                mode=0o600,
            )
            return ReplicaApplyResult(
                generation=cur_gen,
                updated=False,
                synced_at=sync_ts,
                sha256=sha256,
            )

        # New generation (`gen > cur_gen`): stage all files in `auth-gen/<gen>/` and fsync first.
        gen_dir = cdir / "auth-gen" / str(gen)
        gen_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(gen_dir, 0o700)

        allow_content = (
            "\n".join(validated["allow"]) + ("\n" if validated["allow"] else "")
        )
        tokens_content = (
            json.dumps(
                {
                    "tokens": validated["tokens"],
                    "tombstones": validated["tombstones"],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
        manifest_obj = {
            "generation": gen,
            "sha256": sha256,
            "allow": validated["raw_allow"],
            "tokens": validated["tokens"],
            "tombstones": validated["tombstones"],
            "issued_at": validated["issued_at"],
        }
        manifest_content = json.dumps(manifest_obj, indent=2, ensure_ascii=False) + "\n"

        _atomic_write_text(gen_dir / "allow.txt", allow_content, mode=0o600)
        _atomic_write_text(gen_dir / "tokens.json", tokens_content, mode=0o600)
        _atomic_write_text(gen_dir / "manifest.json", manifest_content, mode=0o600)
        _fsync_dir(gen_dir)

        # Deterministic fault-injection point (`X10(c)`): files in `auth-gen/<gen>/` are on disk,
        # while `auth-current` still points at `cur_gen`.
        _fire_auth_pointer_crash_hook(gen, cdir)

        # Atomic pointer swap: temp file + fsync + os.replace (`0600`).
        _atomic_write_text(auth_current_pointer_path(cdir), f"{gen}\n", mode=0o600)
        _atomic_write_text(
            auth_sync_state_path(cdir),
            json.dumps(
                {"generation": gen, "last_sync_at": sync_ts, "sha256": sha256},
                indent=2,
            )
            + "\n",
            mode=0o600,
        )
        return ReplicaApplyResult(
            generation=gen,
            updated=True,
            synced_at=sync_ts,
            sha256=sha256,
        )


def create_token(
    cfg_dir: pathlib.Path,
    node_name: str,
    scopes: list[str] | None = None,
    allow_exec: bool = False,
    *,
    expires_at: float | None = None,
    explicit_auth_source: str | None = None,
) -> dict[str, Any]:
    cdir = pathlib.Path(cfg_dir)
    ensure_writable_source_mode(cdir, explicit_auth_source=explicit_auth_source)

    clean_node = "".join(c for c in node_name.strip().lower() if c.isalnum() or c in "-_")
    if not clean_node:
        raise ValueError("node_name must contain alphanumeric characters")
    if not clean_node.startswith("ext-"):
        clean_node = f"ext-{clean_node}"
    token_id = f"tok_{secrets.token_hex(8)}"
    secret = f"sec_{secrets.token_hex(24)}"
    if scopes is None:
        scopes = ["send:*/inbox", f"poll:{clean_node}"]
    entry: dict[str, Any] = {
        "id": token_id,
        "node": clean_node,
        "secret": secret,
        "secret_sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        "scopes": scopes,
        "allow_exec": bool(allow_exec),
        "disabled": False,
        "created_at": int(time.time()),
    }
    if expires_at is not None:
        entry["expires_at"] = float(expires_at)

    with _AUTH_STATE_LOCK:
        p = tokens_file_path(cdir)
        if not auth_source_state_path(cdir).exists() and (
            p.exists() or auth_generation_path(cdir).exists()
        ):
            with contextlib.suppress(Exception):
                sync_source_auth_state(cdir, explicit_auth_source=explicit_auth_source)
        p.parent.mkdir(parents=True, exist_ok=True)
        raw: dict[str, Any] = {"tokens": []}
        if p.exists():
            try:
                loaded = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("tokens"), list):
                    raw = loaded
            except Exception:
                pass
        raw["tokens"].append(entry)
        _atomic_write_text(p, json.dumps(raw, indent=2, ensure_ascii=False) + "\n", mode=0o600)
        sync_source_auth_state(cdir, explicit_auth_source=explicit_auth_source)
    return entry


def revoke_token(
    cfg_dir: pathlib.Path,
    token_id: str,
    *,
    explicit_auth_source: str | None = None,
) -> bool:
    cdir = pathlib.Path(cfg_dir)
    ensure_writable_source_mode(cdir, explicit_auth_source=explicit_auth_source)

    with _AUTH_STATE_LOCK:
        p = tokens_file_path(cdir)
        if not p.exists():
            return False
        if not auth_source_state_path(cdir).exists():
            with contextlib.suppress(Exception):
                sync_source_auth_state(cdir, explicit_auth_source=explicit_auth_source)
        data = json.loads(p.read_text(encoding="utf-8"))
        changed = False
        for item in data.get("tokens", []):
            if item.get("id") == token_id and not item.get("disabled", False):
                item["disabled"] = True
                item["revoked_at"] = int(time.time())
                changed = True
        if changed:
            _atomic_write_text(
                p,
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                mode=0o600,
            )
            sync_source_auth_state(cdir, explicit_auth_source=explicit_auth_source)
        return changed


def _test_clock_offset_seconds() -> float:
    raw = os.environ.get("BUS_TEST_CLOCK_OFFSET_S")
    if raw is None or not raw.strip():
        return 0.0
    try:
        val = float(raw.strip())
    except ValueError:
        return 0.0
    return val if math.isfinite(val) else 0.0


def _sanitize_test_local_node(raw: str | None) -> str:
    if not raw:
        return ""
    cleaned = "".join(c for c in raw.strip().lower() if c.isalnum() or c in "-_")[:64]
    return cleaned


def get_test_local_node_header() -> dict[str, str]:
    """Return `{'X-Bus-Test-Local-Node': <node>}` strictly when `BUS_TEST_ALLOW_LOCALHOST=1`."""
    if os.environ.get("BUS_TEST_ALLOW_LOCALHOST") != "1":
        return {}
    node = _sanitize_test_local_node(os.environ.get("BUS_TEST_LOCAL_NODE"))
    if not node:
        return {}
    return {"X-Bus-Test-Local-Node": node}


def compute_hmac_signature(secret: str, ts: str | int, body: bytes | str, nonce: str = "") -> str:
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    prefix = f"{ts}.{nonce}.".encode("utf-8") if nonce else f"{ts}.".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), prefix + body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_hmac_headers(token_id: str, secret: str, body: bytes | str = b"") -> dict[str, str]:
    ts = str(int(time.time() + _test_clock_offset_seconds()))
    nonce = secrets.token_hex(12)
    sig = compute_hmac_signature(secret, ts, body, nonce=nonce)
    return {
        "Authorization": f"Bearer {token_id}",
        "X-Bus-Timestamp": ts,
        "X-Bus-Nonce": nonce,
        "X-Bus-Signature": sig,
    }


def _lower_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {str(k).lower(): str(v) for k, v in headers.items()}


def verify_external_hmac(
    headers: dict[str, str],
    raw_body: bytes,
    store: BusStore,
    cfg_dir: pathlib.Path,
    now: float | None = None,
    *,
    explicit_auth_source: str | None = None,
    auth_stale: bool = False,
) -> AuthResult:
    cdir = pathlib.Path(cfg_dir)
    h = _lower_headers(headers)
    auth_hdr = h.get("authorization", "").strip()
    token_id_hint = auth_hdr[7:].strip() if auth_hdr.lower().startswith("bearer ") else None

    if is_replica_mode(cdir, explicit_auth_source=explicit_auth_source):
        if read_replica_current_generation(cdir) <= 0:
            return AuthResult(
                ok=False,
                token_id=token_id_hint,
                error="auth replica not synced",
                code=401,
            )
        if auth_stale:
            return AuthResult(
                ok=False,
                token_id=token_id_hint,
                error="auth replica stale",
                code=401,
            )

    if not auth_hdr.lower().startswith("bearer "):
        return AuthResult(ok=False, error="missing bearer token", code=401)
    token_id = auth_hdr[7:].strip()

    cur_now = now if now is not None else time.time()
    tombstones = load_tombstones(cdir, explicit_auth_source=explicit_auth_source)
    if token_id in tombstones:
        return AuthResult(ok=False, token_id=token_id, error="invalid or disabled token", code=401)

    tokens = load_tokens(cdir, explicit_auth_source=explicit_auth_source, now=cur_now)
    tok = tokens.get(token_id)
    if not tok:
        return AuthResult(ok=False, token_id=token_id, error="invalid or disabled token", code=401)
    secret = str(tok.get("secret") or "")
    if not secret:
        return AuthResult(ok=False, token_id=token_id, error="token missing HMAC secret", code=401)

    ts_raw = h.get("x-bus-timestamp", "").strip()
    sig_raw = h.get("x-bus-signature", "").strip()
    nonce = h.get("x-bus-nonce", "").strip()
    if not ts_raw or not sig_raw:
        return AuthResult(ok=False, token_id=token_id, error="missing timestamp or signature", code=401)
    try:
        ts_val = float(ts_raw)
    except ValueError:
        return AuthResult(ok=False, token_id=token_id, error="invalid timestamp", code=401)
    if not math.isfinite(ts_val):
        return AuthResult(ok=False, token_id=token_id, error="invalid timestamp", code=401)

    if abs(cur_now - ts_val) > REPLAY_WINDOW_SECONDS:
        return AuthResult(ok=False, token_id=token_id, error="timestamp outside replay window", code=401)

    expected = compute_hmac_signature(secret, ts_raw, raw_body, nonce=nonce)
    if not hmac.compare_digest(expected.lower(), sig_raw.lower()):
        return AuthResult(ok=False, token_id=token_id, error="invalid hmac signature", code=403)

    nonce_key = f"{token_id}:{nonce}" if nonce else f"{token_id}:{ts_raw}:{sig_raw.lower()}"
    if not store.check_and_record_nonce(nonce_key, ttl=REPLAY_WINDOW_SECONDS * 2):
        return AuthResult(ok=False, token_id=token_id, error="replayed request", code=401)

    ext_node = str(tok.get("node") or token_id).strip().lower()
    if not ext_node.startswith("ext-"):
        ext_node = f"ext-{ext_node}"

    return AuthResult(
        ok=True,
        node=ext_node,
        trust="external",
        scopes=[str(s) for s in tok.get("scopes", [])],
        allow_exec=bool(tok.get("allow_exec", False)),
        token_id=token_id,
        code=200,
    )


async def authenticate_ws_peer(
    *,
    remote_ip: str,
    headers: dict[str, str] | None,
    hello: dict[str, Any],
    store: BusStore,
    cfg_dir: pathlib.Path,
    is_gateway: bool = False,
    ws_path: str = "/bus/v1/ws",
    explicit_auth_source: str | None = None,
    auth_stale: bool = False,
) -> AuthResult:
    cdir = pathlib.Path(cfg_dir)
    h = _lower_headers(headers)
    is_funnel = bool(h.get("tailscale-funnel-request"))
    has_bearer = h.get("authorization", "").lower().startswith("bearer ")
    hello_auth = hello.get("auth")

    # External auth path: Funnel request, explicit Bearer/HMAC auth, or non-tailnet gateway client.
    if is_funnel or has_bearer or isinstance(hello_auth, dict):
        if isinstance(hello_auth, dict):
            # Support WS hello payload auth: {"token_id": ..., "ts": ..., "nonce": ..., "sig": ...}
            tok_id = str(hello_auth.get("token_id") or "")
            ts_str = str(hello_auth.get("ts") or "")
            nonce = str(hello_auth.get("nonce") or "")
            sig = str(hello_auth.get("sig") or "")
            fake_headers = {
                "authorization": f"Bearer {tok_id}",
                "x-bus-timestamp": ts_str,
                "x-bus-nonce": nonce,
                "x-bus-signature": sig,
            }
            return verify_external_hmac(
                fake_headers,
                tok_id.encode("utf-8"),
                store,
                cdir,
                explicit_auth_source=explicit_auth_source,
                auth_stale=auth_stale,
            )
        return verify_external_hmac(
            h,
            ws_path.encode("utf-8"),
            store,
            cdir,
            explicit_auth_source=explicit_auth_source,
            auth_stale=auth_stale,
        )

    if is_gateway and not is_tailnet_ip(remote_ip):
        # Gateway never auto-trusts 127.0.0.1 or public IPs without Bearer+HMAC.
        return AuthResult(ok=False, error="external auth required on gateway", code=401)

    # Internal mesh path: tailscale whois + allow.txt (or explicit test localhost override).
    # Note: Even when `auth_stale=True` on a replica, internal `whois` `allow` keeps the
    # last known good synced generation (`design.md` §3.4); unsynced initial replicas fail closed.
    if is_loopback_ip(remote_ip) and os.environ.get("BUS_TEST_ALLOW_LOCALHOST") == "1":
        hdr_node = _sanitize_test_local_node(h.get("x-bus-test-local-node"))
        node = (
            hdr_node
            or _sanitize_test_local_node(os.environ.get("BUS_TEST_LOCAL_NODE"))
            or _local_node_name()
        )
    else:
        node = await whois_async(remote_ip)

    if is_replica_mode(cdir, explicit_auth_source=explicit_auth_source) and read_replica_current_generation(cdir) <= 0:
        return AuthResult(ok=False, node=node, error="auth replica not synced", code=403)

    if not node or node not in allowed_nodes(cdir, explicit_auth_source=explicit_auth_source):
        return AuthResult(ok=False, node=node, error="node not allowed", code=403)
    return AuthResult(ok=True, node=node, trust="internal", allow_exec=True, code=200)


async def authenticate_http_request(
    *,
    remote_ip: str,
    headers: dict[str, str] | None,
    raw_body: bytes,
    store: BusStore,
    cfg_dir: pathlib.Path,
    explicit_auth_source: str | None = None,
    auth_stale: bool = False,
) -> AuthResult:
    cdir = pathlib.Path(cfg_dir)
    h = _lower_headers(headers)
    is_funnel = bool(h.get("tailscale-funnel-request"))
    has_bearer = h.get("authorization", "").lower().startswith("bearer ")

    if is_funnel or has_bearer or not is_tailnet_ip(remote_ip):
        return verify_external_hmac(
            h,
            raw_body,
            store,
            cdir,
            explicit_auth_source=explicit_auth_source,
            auth_stale=auth_stale,
        )

    node = await whois_async(remote_ip)
    if is_replica_mode(cdir, explicit_auth_source=explicit_auth_source) and read_replica_current_generation(cdir) <= 0:
        return AuthResult(ok=False, node=node, error="auth replica not synced", code=403)
    if not node or node not in allowed_nodes(cdir, explicit_auth_source=explicit_auth_source):
        return AuthResult(ok=False, node=node, error="node not allowed", code=403)
    return AuthResult(ok=True, node=node, trust="internal", allow_exec=True, code=200)


def check_external_send_permission(
    auth: AuthResult,
    resolved_to: str,
    kind: str,
    text: str = "",
) -> tuple[bool, str]:
    """Enforce scope & execution isolation for external tokens."""
    if auth.trust != "external":
        return True, ""

    target = resolved_to.strip().lower()
    if target == "*":
        return False, "external tokens cannot broadcast to '*'"

    # Default execution guard: without explicit allow_exec=True, only allow
    # kind='event' to explicit non-main/non-cli labels (e.g. */inbox or */<session>)
    # and reject !ask / !update command triggers.
    node_part, sep, label_part = target.partition("/")
    if not auth.allow_exec:
        if not sep or not label_part or label_part == "main" or label_part.startswith("cli-"):
            return False, "external token lacks allow_exec permission for bare node or worker target"
        if kind != "event":
            return False, "external token without allow_exec can only send kind='event'"
        stripped = text.lstrip()
        if stripped.startswith("!ask") or stripped.startswith("!update"):
            return False, "external token without allow_exec cannot send '!ask' or '!update' commands"

    scopes = auth.scopes or []
    candidates = [
        f"send:{target}",
        f"{kind}:{target}",
    ]
    for pattern in scopes:
        p = pattern.strip().lower()
        for cand in candidates:
            if fnmatch.fnmatchcase(cand, p):
                return True, ""
    return False, f"target '{target}' not permitted by token scopes"
