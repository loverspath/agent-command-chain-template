"""Dual-Auth (tailscale whois internal + Bearer/HMAC-SHA256 external) and scope isolation."""

from __future__ import annotations

import asyncio
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
import time
from typing import Any

from .store import BusStore

REPLAY_WINDOW_SECONDS = 300
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_WHOIS_CACHE: dict[str, tuple[str, float]] = {}


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


def allowed_nodes(cfg_dir: pathlib.Path) -> set[str]:
    p = cfg_dir / "allow.txt"
    if not p.exists():
        cfg_dir.mkdir(parents=True, exist_ok=True)
        default_node = _local_node_name()
        p.write_text(f"{default_node}\n", encoding="utf-8")
    return {
        line.strip().lower()
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


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
    return cfg_dir / "tokens.json"


def load_tokens(cfg_dir: pathlib.Path) -> dict[str, dict[str, Any]]:
    p = tokens_file_path(cfg_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = data.get("tokens", []) if isinstance(data, dict) else []
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or "").strip()
        if tid and not item.get("disabled", False):
            out[tid] = item
    return out


def create_token(
    cfg_dir: pathlib.Path,
    node_name: str,
    scopes: list[str] | None = None,
    allow_exec: bool = False,
) -> dict[str, Any]:
    clean_node = "".join(c for c in node_name.strip().lower() if c.isalnum() or c in "-_")
    if not clean_node:
        raise ValueError("node_name must contain alphanumeric characters")
    if not clean_node.startswith("ext-"):
        clean_node = f"ext-{clean_node}"
    token_id = f"tok_{secrets.token_hex(8)}"
    secret = f"sec_{secrets.token_hex(24)}"
    if scopes is None:
        scopes = [f"send:*/inbox", f"poll:{clean_node}"]
    entry = {
        "id": token_id,
        "node": clean_node,
        "secret": secret,
        "secret_sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        "scopes": scopes,
        "allow_exec": bool(allow_exec),
        "disabled": False,
        "created_at": int(time.time()),
    }
    p = tokens_file_path(cfg_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = {"tokens": []}
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("tokens"), list):
                raw = loaded
        except Exception:
            pass
    raw["tokens"].append(entry)
    p.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return entry


def revoke_token(cfg_dir: pathlib.Path, token_id: str) -> bool:
    p = tokens_file_path(cfg_dir)
    if not p.exists():
        return False
    data = json.loads(p.read_text(encoding="utf-8"))
    changed = False
    for item in data.get("tokens", []):
        if item.get("id") == token_id and not item.get("disabled", False):
            item["disabled"] = True
            changed = True
    if changed:
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed


def compute_hmac_signature(secret: str, ts: str | int, body: bytes | str, nonce: str = "") -> str:
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    prefix = f"{ts}.{nonce}.".encode("utf-8") if nonce else f"{ts}.".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), prefix + body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_hmac_headers(token_id: str, secret: str, body: bytes | str = b"") -> dict[str, str]:
    ts = str(int(time.time()))
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
) -> AuthResult:
    h = _lower_headers(headers)
    auth_hdr = h.get("authorization", "").strip()
    if not auth_hdr.lower().startswith("bearer "):
        return AuthResult(ok=False, error="missing bearer token", code=401)
    token_id = auth_hdr[7:].strip()
    tokens = load_tokens(cfg_dir)
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

    cur_now = now if now is not None else time.time()
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
) -> AuthResult:
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
            return verify_external_hmac(fake_headers, tok_id.encode("utf-8"), store, cfg_dir)
        return verify_external_hmac(h, ws_path.encode("utf-8"), store, cfg_dir)

    if is_gateway and not is_tailnet_ip(remote_ip):
        # Gateway never auto-trusts 127.0.0.1 or public IPs without Bearer+HMAC.
        return AuthResult(ok=False, error="external auth required on gateway", code=401)

    # Internal mesh path: tailscale whois + allow.txt (or explicit test localhost override).
    if is_loopback_ip(remote_ip) and os.environ.get("BUS_TEST_ALLOW_LOCALHOST") == "1":
        node = os.environ.get("BUS_TEST_LOCAL_NODE") or _local_node_name()
    else:
        node = await whois_async(remote_ip)

    if not node or node not in allowed_nodes(cfg_dir):
        return AuthResult(ok=False, node=node, error="node not allowed", code=403)
    return AuthResult(ok=True, node=node, trust="internal", allow_exec=True, code=200)


async def authenticate_http_request(
    *,
    remote_ip: str,
    headers: dict[str, str] | None,
    raw_body: bytes,
    store: BusStore,
    cfg_dir: pathlib.Path,
) -> AuthResult:
    h = _lower_headers(headers)
    is_funnel = bool(h.get("tailscale-funnel-request"))
    has_bearer = h.get("authorization", "").lower().startswith("bearer ")

    if is_funnel or has_bearer or not is_tailnet_ip(remote_ip):
        return verify_external_hmac(h, raw_body, store, cfg_dir)

    node = await whois_async(remote_ip)
    if not node or node not in allowed_nodes(cfg_dir):
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
