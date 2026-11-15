"""ACC [ACC_EVENT_BATCH] inbox bridge and durable local mailbox."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from typing import Any

from .config import home_dir, load_config


def find_acc_repo(configured_repo: str | pathlib.Path | None = None) -> pathlib.Path | None:
    candidates: list[pathlib.Path] = []
    if configured_repo:
        candidates.append(pathlib.Path(configured_repo).expanduser())
    env_repo = os.environ.get("BUS_ACC_REPO")
    if env_repo:
        candidates.append(pathlib.Path(env_repo).expanduser())
    cfg_repo = load_config().get("acc_repo")
    if cfg_repo:
        candidates.append(pathlib.Path(str(cfg_repo)).expanduser())
    # Repo root if running from inside agent-command-chain-template/bus/agent_bus/
    candidates.append(pathlib.Path(__file__).resolve().parents[2])
    candidates.append(home_dir() / "agent-command-chain-template")
    candidates.append(home_dir() / "act")

    for cand in candidates:
        if (cand / "bin" / "event-emit.sh").is_file():
            return cand
    return None


def resolve_acc_runtime(acc_repo: pathlib.Path, session_name: str) -> pathlib.Path | None:
    if not session_name:
        return None
    reg_path = acc_repo / "runtime" / "registry.json"
    if reg_path.is_file():
        try:
            data = json.loads(reg_path.read_text(encoding="utf-8"))
            for sess in data.get("sessions", []):
                if sess.get("session_name") == session_name and not sess.get("retired"):
                    rt = sess.get("runtime_dir")
                    if rt and pathlib.Path(rt).is_dir():
                        return pathlib.Path(rt)
        except Exception:
            pass

    runtime_root = acc_repo / "runtime"
    if runtime_root.is_dir():
        matches: list[pathlib.Path] = []
        for entry in runtime_root.iterdir():
            if not entry.is_dir() or (entry / "retired").exists():
                continue
            sn_file = entry / "session_name"
            if sn_file.is_file():
                try:
                    name = sn_file.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
                except Exception:
                    name = ""
                if name == session_name:
                    matches.append(entry)
        if matches:
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return matches[0]
    return None


def _sanitize_summary(text: str) -> str:
    clean = " ".join(text.replace("<", "[").replace(">", "]").split())
    return clean[:120] or "(empty message)"


def deliver_to_inbox(
    msg: dict[str, Any],
    *,
    cfg_dir: pathlib.Path,
    target_session: str = "",
    acc_repo: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Persist an incoming message to inbox.jsonl + inbox/*.md and optionally wake ACC."""
    cfg_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir = cfg_dir / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    seq = int(msg.get("seq") or 0)
    mid = str(msg.get("id") or f"seq-{seq}")
    safe_mid = "".join(c for c in mid if c.isalnum() or c in "-_") or "msg"
    detail_path = inbox_dir / f"msg-{seq}-{safe_mid}.md"
    detail_path.write_text(
        f"# Agent Bus Message seq={seq} id={mid}\n\n"
        f"- **From**: `{msg.get('from', '')}`\n"
        f"- **To**: `{msg.get('to', '')}`\n"
        f"- **Kind**: `{msg.get('kind', 'event')}`\n"
        f"- **Corr-ID**: `{msg.get('corr_id') or ''}`\n"
        f"- **Timestamp**: `{msg.get('ts', '')}`\n\n"
        f"## Body\n\n{msg.get('text', '')}\n",
        encoding="utf-8",
    )

    record = {
        "seq": seq,
        "id": mid,
        "corr_id": msg.get("corr_id"),
        "from": msg.get("from", ""),
        "to": msg.get("to", ""),
        "kind": msg.get("kind", "event"),
        "ts": msg.get("ts") or time.time(),
        "text": msg.get("text", ""),
        "detail_path": str(detail_path),
        "acked": False,
    }
    inbox_jsonl = cfg_dir / "inbox.jsonl"
    # Deduplicate by id if already recorded
    already = False
    if inbox_jsonl.exists():
        for line in inbox_jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                if json.loads(line).get("id") == mid:
                    already = True
                    break
            except Exception:
                continue
    if not already:
        with open(inbox_jsonl, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    acc_emitted = False
    acc_error = ""
    session = target_session.strip()
    if session == "inbox":
        session = os.environ.get("BUS_ACC_SESSION") or str(load_config().get("acc_session") or "")
    if session:
        repo = acc_repo or find_acc_repo()
        if repo is not None:
            rt = resolve_acc_runtime(repo, session)
            if rt is not None:
                emit_sh = repo / "bin" / "event-emit.sh"
                env = os.environ.copy()
                env["ACC_RUNTIME"] = str(rt)
                env["SESSION_NAME"] = session
                summary = f"bus from {msg.get('from', '?')}: {_sanitize_summary(str(msg.get('text', '')))}"
                try:
                    res = subprocess.run(
                        [
                            str(emit_sh),
                            "external",
                            "done",
                            f"bus-{seq}",
                            summary,
                            str(detail_path),
                            f"bus-{seq}-{safe_mid}",
                            "bus_msg",
                        ],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if res.returncode == 0:
                        acc_emitted = True
                    else:
                        acc_error = (res.stderr or res.stdout).strip()
                except Exception as exc:
                    acc_error = str(exc)
            else:
                acc_error = f"runtime not found for session {session}"
        else:
            acc_error = "acc repo not found"

    return {
        "detail_path": str(detail_path),
        "acc_emitted": acc_emitted,
        "acc_error": acc_error,
        "session": session,
    }


def read_inbox(cfg_dir: pathlib.Path, include_acked: bool = False) -> list[dict[str, Any]]:
    p = cfg_dir / "inbox.jsonl"
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if include_acked or not item.get("acked", False):
            out.append(item)
    return out


def ack_inbox(cfg_dir: pathlib.Path, target: str) -> int:
    """Mark matching inbox.jsonl entry (by seq, id, or 'all') as acked."""
    p = cfg_dir / "inbox.jsonl"
    if not p.exists():
        return 0
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    updated: list[str] = []
    count = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        match = (
            target == "all"
            or str(item.get("seq")) == str(target)
            or str(item.get("id")) == str(target)
        )
        if match and not item.get("acked", False):
            item["acked"] = True
            item["acked_at"] = time.time()
            count += 1
        updated.append(json.dumps(item, ensure_ascii=False))
    p.write_text(("\n".join(updated) + "\n") if updated else "", encoding="utf-8")
    return count
