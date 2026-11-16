"""Deterministic chaos oracle: `RequestLedger`, file-backed `SideEffectCounter`, and `ClaudeStub`.

Guarantees:
- `RequestLedger` tracks per-request `id` timestamps (`sent`, `hub_ack`, `received`,
  `executed`, `result`) and multi-hub / execution metadata safely across threads.
- `SideEffectCounter` immediately appends + flushes + fsyncs every `ask_claude` execution
  to a file on disk (`side_effects.jsonl`) and exposes `count(msg_id)` and `all_ids()`.
- `ClaudeStub` completely replaces `agent_bus.client.ask_claude` (never invokes `claude -p`)
  and supports deterministic per-request delays, `ExecutionGate`s, and `asyncio.Event`
  blocking hooks across both `run_in_executor` worker threads and `asyncio` coroutines.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import pathlib
import re
import stat
import sys
import threading
import time
from typing import Any

_ID_TAG_RE = re.compile(r"\[(?:req_)?id=([^\]\s]+)\]")


def make_ask_prompt(req_id: str, body: str = "work", *, include_prefix: bool = True) -> str:
    """Format a deterministic `!ask` payload embedding `req_id` so `ClaudeStub` extracts it."""
    core = f"[id={req_id}] {body}".strip()
    return f"!ask {core}" if include_prefix else core


def extract_req_id(text: str, prompt_map: dict[str, str] | None = None) -> str:
    """Extract canonical request `id` from an incoming `ask_claude(text, ...)` prompt."""
    clean = str(text or "").strip()
    if clean.startswith("!ask"):
        clean = clean[4:].strip()
    m = _ID_TAG_RE.search(clean)
    if m:
        return m.group(1)
    if prompt_map:
        if clean in prompt_map:
            return prompt_map[clean]
        for substr, mapped_id in prompt_map.items():
            if substr and substr in clean:
                return mapped_id
    return clean or "unknown"


@dataclasses.dataclass
class LedgerEntry:
    req_id: str
    sent: float | None = None
    hub_ack: float | None = None
    received: float | None = None
    executed: float | None = None
    result: float | None = None
    acked_hubs: list[str] = dataclasses.field(default_factory=list)
    received_count: int = 0
    executed_count: int = 0
    result_count: int = 0
    result_status: str | None = None
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class RequestLedger:
    """Thread-safe lifecycle ledger recording G1–G5 stage timestamps per request `id`."""

    def __init__(self, ledger_file: pathlib.Path | None = None) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, LedgerEntry] = {}
        self.ledger_file = pathlib.Path(ledger_file) if ledger_file is not None else None
        if self.ledger_file is not None:
            self.ledger_file.parent.mkdir(parents=True, exist_ok=True)

    def _ensure(self, req_id: str) -> LedgerEntry:
        entry = self._entries.get(req_id)
        if entry is None:
            entry = LedgerEntry(req_id=req_id)
            self._entries[req_id] = entry
        return entry

    def _append_event(self, stage: str, req_id: str, ts: float, extra: dict[str, Any]) -> None:
        if self.ledger_file is None:
            return
        record = {"stage": stage, "id": req_id, "ts": ts, **extra}
        with open(self.ledger_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()

    def record_sent(self, req_id: str, *, ts: float | None = None, **meta: Any) -> LedgerEntry:
        now = ts if ts is not None else time.time()
        with self._lock:
            entry = self._ensure(req_id)
            if entry.sent is None:
                entry.sent = now
            entry.meta.update(meta)
            self._append_event("sent", req_id, now, meta)
            return dataclasses.replace(entry, acked_hubs=list(entry.acked_hubs), meta=dict(entry.meta))

    def record_hub_ack(
        self,
        req_id: str,
        *,
        hub: str = "hub_a",
        ts: float | None = None,
        **meta: Any,
    ) -> LedgerEntry:
        now = ts if ts is not None else time.time()
        with self._lock:
            entry = self._ensure(req_id)
            if entry.hub_ack is None:
                entry.hub_ack = now
            if hub and hub not in entry.acked_hubs:
                entry.acked_hubs.append(hub)
            entry.meta.update(meta)
            self._append_event("hub_ack", req_id, now, {"hub": hub, **meta})
            return dataclasses.replace(entry, acked_hubs=list(entry.acked_hubs), meta=dict(entry.meta))

    def record_received(self, req_id: str, *, ts: float | None = None, **meta: Any) -> LedgerEntry:
        now = ts if ts is not None else time.time()
        with self._lock:
            entry = self._ensure(req_id)
            if entry.received is None:
                entry.received = now
            entry.received_count += 1
            entry.meta.update(meta)
            self._append_event("received", req_id, now, {"received_count": entry.received_count, **meta})
            return dataclasses.replace(entry, acked_hubs=list(entry.acked_hubs), meta=dict(entry.meta))

    def record_executed(self, req_id: str, *, ts: float | None = None, **meta: Any) -> LedgerEntry:
        now = ts if ts is not None else time.time()
        with self._lock:
            entry = self._ensure(req_id)
            if entry.executed is None:
                entry.executed = now
            entry.executed_count += 1
            entry.meta.update(meta)
            self._append_event("executed", req_id, now, {"executed_count": entry.executed_count, **meta})
            return dataclasses.replace(entry, acked_hubs=list(entry.acked_hubs), meta=dict(entry.meta))

    def record_result(
        self,
        req_id: str,
        *,
        status: str | None = None,
        ts: float | None = None,
        **meta: Any,
    ) -> LedgerEntry:
        now = ts if ts is not None else time.time()
        with self._lock:
            entry = self._ensure(req_id)
            if entry.result is None:
                entry.result = now
            entry.result_count += 1
            if status is not None:
                entry.result_status = status
            entry.meta.update(meta)
            self._append_event("result", req_id, now, {"status": status, **meta})
            return dataclasses.replace(entry, acked_hubs=list(entry.acked_hubs), meta=dict(entry.meta))

    # Ergonomic aliases
    mark_sent = record_sent
    mark_hub_ack = record_hub_ack
    mark_received = record_received
    mark_executed = record_executed
    mark_result = record_result

    def get(self, req_id: str) -> LedgerEntry | None:
        with self._lock:
            entry = self._entries.get(req_id)
            if entry is None:
                return None
            return dataclasses.replace(entry, acked_hubs=list(entry.acked_hubs), meta=dict(entry.meta))

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: v.to_dict() for k, v in self._entries.items()}


class SideEffectCounter:
    """File-backed deterministic side-effect log (`side_effects.jsonl`) for execution counting."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        self._lock = threading.Lock()
        self._seq = 0

    def record(self, msg_id: str, *, frm: str = "", text: str = "", ts: float | None = None) -> dict[str, Any]:
        now = ts if ts is not None else time.time()
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "id": str(msg_id),
                "from": str(frm),
                "text": str(text),
                "ts": now,
                "pid": os.getpid(),
            }
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            return entry

    def records(self, msg_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            items: list[dict[str, Any]] = []
            for raw_line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not raw_line.strip():
                    continue
                item = json.loads(raw_line)
                if msg_id is None or item.get("id") == msg_id:
                    items.append(item)
            return items

    def all_ids(self) -> list[str]:
        return [str(r["id"]) for r in self.records()]

    def count(self, msg_id: str) -> int:
        return len(self.records(msg_id))

    def reset(self) -> None:
        with self._lock:
            self._seq = 0
            self.path.write_text("", encoding="utf-8")

    def write_subprocess_stub_script(self, script_path: pathlib.Path) -> pathlib.Path:
        """Create an executable `BUS_CLAUDE_CMD` stub that appends to `self.path` (for X02 subprocess crash tests)."""
        script_path = pathlib.Path(script_path).resolve()
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            f"#!{sys.executable}\n"
            "import json, os, re, sys, time\n"
            "raw = sys.stdin.read()\n"
            r"m = re.search(r'\[(?:req_)?id=([^\]\s]+)\]', raw)" + "\n"
            "mid = m.group(1) if m else 'unknown'\n"
            f"p = {str(self.path)!r}\n"
            "rec = {'seq': 0, 'id': mid, 'from': 'subprocess', 'text': raw[-200:], 'ts': time.time(), 'pid': os.getpid()}\n"
            "with open(p, 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps(rec, ensure_ascii=False) + '\\n')\n"
            "    fh.flush()\n"
            "    os.fsync(fh.fileno())\n"
            "sys.stdout.write(f'STUB_OK:{mid}\\n')\n",
            encoding="utf-8",
        )
        script_path.chmod(script_path.stat().st_mode | stat.S_IRWXU)
        return script_path


class ExecutionGate:
    """Bridges an `asyncio` test coroutine with a synchronous `run_in_executor` `ask_claude` stub."""

    def __init__(self, req_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self.req_id = req_id
        self.loop = loop
        self.entered = asyncio.Event()
        self._entered_thread = threading.Event()
        self._release_thread = threading.Event()
        self.enter_count = 0

    def notify_entered(self) -> int:
        self.enter_count += 1
        self._entered_thread.set()
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.entered.set)
        return self.enter_count

    def wait_for_release_in_worker(self, timeout: float = 15.0) -> bool:
        return self._release_thread.wait(timeout=timeout)

    async def wait_entered(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.entered.wait(), timeout=timeout)

    def release(self) -> None:
        self._release_thread.set()


class ClaudeStub:
    """Deterministic `agent_bus.client.ask_claude` replacement with side-effect logging & blocking hooks."""

    def __init__(
        self,
        *,
        counter: SideEffectCounter,
        ledger: RequestLedger,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.counter = counter
        self.ledger = ledger
        self.loop = loop
        self._lock = threading.RLock()
        self._prompt_map: dict[str, str] = {}
        self._delays: dict[str, float] = {}
        self._default_delay: float = 0.0
        self._responses: dict[str, tuple[str, str]] = {}
        self._gates: dict[str, ExecutionGate] = {}
        self._async_blocks: dict[str, tuple[asyncio.Event, asyncio.Event | None, bool]] = {}

    def register_prompt_id(self, prompt_substring: str, req_id: str) -> None:
        with self._lock:
            self._prompt_map[prompt_substring] = req_id

    def set_delay(self, req_id: str | None, delay_sec: float) -> None:
        with self._lock:
            if req_id is None:
                self._default_delay = max(0.0, float(delay_sec))
            else:
                self._delays[req_id] = max(0.0, float(delay_sec))

    def set_response(self, req_id: str, text: str, status: str = "ok") -> None:
        with self._lock:
            self._responses[req_id] = (text, status)

    def gate(self, req_id: str, *, only_first: bool = True) -> ExecutionGate:
        """Create and register an `ExecutionGate` that blocks `req_id` inside `ask_claude` until `gate.release()`."""
        with self._lock:
            g = ExecutionGate(req_id, self.loop)
            self._gates[req_id] = g
            # Store whether only the 1st invocation blocks (default True so duplicate replay completes cleanly).
            g._only_first = only_first  # type: ignore[attr-defined]
            return g

    def set_block_event(
        self,
        req_id: str,
        release_event: asyncio.Event,
        *,
        entered_event: asyncio.Event | None = None,
        only_first: bool = True,
    ) -> None:
        """Register a standard `asyncio.Event` hook that pauses `ask_claude` for `req_id` until `release_event.set()`."""
        with self._lock:
            self._async_blocks[req_id] = (release_event, entered_event, only_first)

    def release_all(self) -> None:
        with self._lock:
            gates = list(self._gates.values())
            blocks = list(self._async_blocks.values())
        for g in gates:
            g.release()
        if not self.loop.is_closed():
            for rel_ev, _, _ in blocks:
                self.loop.call_soon_threadsafe(rel_ev.set)

    def __call__(
        self,
        text: str,
        frm: str,
        cwd: pathlib.Path | str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[str, str]:
        explicit_id = kwargs.get("msg_id") or kwargs.get("id")
        with self._lock:
            req_id = str(explicit_id) if explicit_id else extract_req_id(text, self._prompt_map)
            delay = self._delays.get(req_id, self._default_delay)
            gate = self._gates.get(req_id)
            async_block = self._async_blocks.get(req_id)

        rec = self.counter.record(req_id, frm=frm, text=text)
        ledger_entry = self.ledger.record_executed(req_id, ts=rec["ts"], frm=frm, cwd=str(cwd or ""))
        exec_idx = ledger_entry.executed_count

        if gate is not None:
            only_first = bool(getattr(gate, "_only_first", True))
            n = gate.notify_entered()
            if (not only_first) or n == 1:
                gate.wait_for_release_in_worker(timeout=15.0)

        if async_block is not None:
            rel_ev, ent_ev, only_first = async_block
            if ent_ev is not None and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(ent_ev.set)
            if ((not only_first) or exec_idx == 1) and not self.loop.is_closed():
                fut = asyncio.run_coroutine_threadsafe(rel_ev.wait(), self.loop)
                fut.result(timeout=15.0)

        if delay > 0:
            time.sleep(delay)

        with self._lock:
            resp_text, resp_status = self._responses.get(req_id, (f"STUB_OK:{req_id}", "ok"))
        return resp_text, resp_status


class ChaosOracle:
    """Unified oracle fixture bundling `RequestLedger`, `SideEffectCounter`, and `ClaudeStub`."""

    def __init__(self, base_dir: pathlib.Path, loop: asyncio.AbstractEventLoop) -> None:
        self.base_dir = pathlib.Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = RequestLedger(self.base_dir / "ledger.jsonl")
        self.counter = SideEffectCounter(self.base_dir / "side_effects.jsonl")
        self.stub = ClaudeStub(counter=self.counter, ledger=self.ledger, loop=loop)

    def install(self, monkeypatch: Any) -> "ChaosOracle":
        from agent_bus import client as bus_client

        monkeypatch.setattr(bus_client, "ask_claude", self.stub)
        return self

    def close(self) -> None:
        self.stub.release_all()
