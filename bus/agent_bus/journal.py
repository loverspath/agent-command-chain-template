"""Node-level durable SQLite WAL journal (`<cfg_dir>/journal.db`) for agent_bus.

Provides transactional persistence shared across all node labels and CLI processes:
- `outbox`: pre-send durable queue with per-hub ACK tracking (`pending|done|expired`)
  and one-time atomic migration from legacy `<cfg_dir>/outbox.jsonl` -> `.migrated`.
- `inbox`: durable receive & at-most-once execution state machine
  (`queued|skipped|running|done|uncertain|expired`) with atomic claim and crash recovery.
- `cursors`: monotonic per-hub (and secondary label) sequence watermarks.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import json
import os
import pathlib
import random
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Iterator, TypeVar

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

from .config import (
    DEFAULT_EVENT_DEADLINE,
    DEFAULT_TASK_DEADLINE,
    MAX_HOP,
    cfg_dir,
    journal_db_path,
)

T = TypeVar("T")

OUTBOX_STATES = ("pending", "done", "expired")
INBOX_STATES = ("queued", "skipped", "running", "done", "uncertain", "expired")

_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created REAL NOT NULL,
    deadline REAL NOT NULL,
    acked_hubs TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'done', 'expired'))
);
CREATE INDEX IF NOT EXISTS idx_outbox_state_created ON outbox(state, created);
CREATE INDEX IF NOT EXISTS idx_outbox_state_deadline ON outbox(state, deadline);

CREATE TABLE IF NOT EXISTS inbox (
    id TEXT PRIMARY KEY,
    "from" TEXT NOT NULL,
    corr_id TEXT,
    kind TEXT NOT NULL DEFAULT 'task',
    text TEXT NOT NULL DEFAULT '',
    received_at REAL NOT NULL,
    deadline REAL NOT NULL,
    state TEXT NOT NULL
        CHECK (state IN ('queued', 'skipped', 'running', 'done', 'uncertain', 'expired')),
    result_status TEXT,
    result_text TEXT,
    updated_at REAL NOT NULL,
    hop INTEGER NOT NULL DEFAULT 0,
    owner_worker TEXT,
    owner_pid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_inbox_state_received ON inbox(state, received_at);
CREATE INDEX IF NOT EXISTS idx_inbox_state_deadline ON inbox(state, deadline);

CREATE TABLE IF NOT EXISTS cursors (
    hub_key TEXT PRIMARY KEY,
    seq INTEGER NOT NULL DEFAULT 0
);
"""

_ACTIVE_WORKERS_LOCK = threading.RLock()
_ACTIVE_WORKERS: set[str] = set()


def new_message_id() -> str:
    """Issue a 32-char lowercase hex UUIDv4 identifier (F31)."""
    return uuid.uuid4().hex


def compute_deadline(
    *,
    kind: str = "task",
    base_ts: float | None = None,
    explicit_deadline: float | int | None = None,
) -> float:
    """Resolve business deadline (`+86400s` for `event`, `+3600s` otherwise)."""
    if explicit_deadline is not None:
        return float(explicit_deadline)
    origin_ts = float(base_ts) if base_ts is not None else time.time()
    ttl = DEFAULT_EVENT_DEADLINE if str(kind or "task").strip().lower() == "event" else DEFAULT_TASK_DEADLINE
    return origin_ts + float(ttl)


def cursor_hub_key(base_hub_key: str, label: str = "main") -> str:
    """Scope cursor keys so secondary `--label` consumers never clobber `main`."""
    clean_hub = str(base_hub_key or "").strip() or "default"
    clean_label = str(label or "main").strip() or "main"
    return clean_hub if clean_label == "main" else f"{clean_hub}#{clean_label}"


def _is_sqlite_busy_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _with_sqlite_retry(fn: Callable[[], T], *, max_retries: int = 80) -> T:
    for attempt in range(max_retries):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_busy_error(exc) or attempt == max_retries - 1:
                raise
            time.sleep(min(0.1, 0.004 * (attempt + 1) + random.uniform(0.0, 0.008)))
    raise RuntimeError("unreachable")


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    stat_path = pathlib.Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            raw = stat_path.read_text(encoding="utf-8", errors="replace")
            state_ch = raw.rsplit(")", 1)[1].strip().split()[0]
            if state_ch == "Z":
                return False
        except Exception:
            pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM


@dataclasses.dataclass
class WorkerSession:
    """Tracks liveness of a `run_node` worker across both threads/tasks and OS processes."""

    worker_id: str
    label: str
    pid: int
    lock_path: pathlib.Path
    _lock_fd: int | None = dataclasses.field(default=None, repr=False)
    _closed: bool = dataclasses.field(default=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with _ACTIVE_WORKERS_LOCK:
            _ACTIVE_WORKERS.discard(self.worker_id)
        if self._lock_fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._lock_fd)
            except Exception:
                pass
            self._lock_fd = None
        with contextlib.suppress(OSError):
            self.lock_path.unlink()


def register_worker_session(config_dir: pathlib.Path, label: str) -> WorkerSession:
    pid = os.getpid()
    clean_label = "".join(c for c in str(label or "main") if c.isalnum() or c in "-_")[:24] or "main"
    worker_id = f"{clean_label}-{pid}-{uuid.uuid4().hex[:12]}"
    locks_dir = pathlib.Path(config_dir) / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / f"worker-{worker_id}.lock"
    fd: int | None = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        os.write(fd, f"{pid} {clean_label} {worker_id}\n".encode("utf-8"))
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
            fd = None
    with _ACTIVE_WORKERS_LOCK:
        _ACTIVE_WORKERS.add(worker_id)
    return WorkerSession(
        worker_id=worker_id,
        label=clean_label,
        pid=pid,
        lock_path=lock_path,
        _lock_fd=fd,
    )


def is_worker_owner_alive(
    config_dir: pathlib.Path,
    owner_worker: str | None,
    owner_pid: int | None,
) -> bool:
    """Return True iff the worker that claimed a `running` inbox item is still alive."""
    if not owner_worker:
        return False
    cur_pid = os.getpid()
    if owner_pid is not None and int(owner_pid) == cur_pid:
        with _ACTIVE_WORKERS_LOCK:
            return owner_worker in _ACTIVE_WORKERS

    lock_path = pathlib.Path(config_dir) / "locks" / f"worker-{owner_worker}.lock"
    if fcntl is not None:
        if not lock_path.exists():
            return False
        fd: int | None = None
        try:
            fd = os.open(str(lock_path), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    return True
                raise
            # Lock acquired -> previous holder process has died (even via SIGKILL).
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                lock_path.unlink()
            return False
        except FileNotFoundError:
            return False
        except Exception:
            pass
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)

    if owner_pid is not None:
        return _is_pid_alive(int(owner_pid))
    return False


def _needs_terminal_reply(row: sqlite3.Row | dict[str, Any]) -> bool:
    sender = str(row["from"] or "").strip()
    kind = str(row["kind"] or "task").strip().lower()
    hop = int(row["hop"] or 0)
    if not sender:
        return False
    if hop + 1 > MAX_HOP:
        return False
    if kind == "event":
        return False
    return True


def _build_reply_payload(
    row: sqlite3.Row | dict[str, Any],
    *,
    status: str,
    text: str,
    reply_id: str | None = None,
) -> dict[str, Any]:
    rid = reply_id or new_message_id()
    corr_id = str(row["corr_id"] or row["id"] or "")
    orig_kind = str(row["kind"] or "task").strip().lower()
    return {
        "v": 2,
        "op": "send",
        "id": rid,
        "corr_id": corr_id or None,
        "kind": "rpc" if orig_kind == "rpc" else "task",
        "status": status,
        "to": str(row["from"]),
        "text": text,
        "hop": int(row["hop"] or 0) + 1,
    }


class NodeJournal:
    """Process-safe and thread-safe SQLite WAL journal at `<cfg_dir>/journal.db`."""

    def __init__(
        self,
        config_dir: pathlib.Path | None = None,
        *,
        busy_timeout_ms: int = 30000,
    ) -> None:
        self.config_dir = pathlib.Path(config_dir).expanduser() if config_dir is not None else cfg_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.path = journal_db_path(self.config_dir)
        self.busy_timeout_ms = max(10000, int(busy_timeout_ms))
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            str(self.path),
            timeout=max(30.0, self.busy_timeout_ms / 1000.0),
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._initialize_and_migrate()

    def __enter__(self) -> "NodeJournal":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                with contextlib.suppress(Exception):
                    self._conn.close()

    @contextlib.contextmanager
    def _immediate_tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            _with_sqlite_retry(lambda: self._conn.execute("BEGIN IMMEDIATE"))
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(Exception):
                    self._conn.execute("ROLLBACK")
                raise

    def _ensure_extra_inbox_columns(self, conn: sqlite3.Connection) -> None:
        cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(inbox)").fetchall()}
        if "hop" not in cols:
            conn.execute("ALTER TABLE inbox ADD COLUMN hop INTEGER NOT NULL DEFAULT 0")
        if "owner_worker" not in cols:
            conn.execute("ALTER TABLE inbox ADD COLUMN owner_worker TEXT")
        if "owner_pid" not in cols:
            conn.execute("ALTER TABLE inbox ADD COLUMN owner_pid INTEGER")

    def _initialize_and_migrate(self) -> None:
        def _setup() -> None:
            with self._lock:
                self._conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms};")
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute("PRAGMA synchronous=NORMAL;")
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _JOURNAL_SCHEMA.strip().split(";"):
                        s = stmt.strip()
                        if s:
                            self._conn.execute(s)
                    self._ensure_extra_inbox_columns(self._conn)
                    self._migrate_legacy_outbox_in_tx(self._conn)
                    self._conn.execute("COMMIT")
                except BaseException:
                    with contextlib.suppress(Exception):
                        self._conn.execute("ROLLBACK")
                    raise

        _with_sqlite_retry(_setup)

        # Finalize atomic rename outside or right after commit if file still exists.
        legacy_path = self.config_dir / "outbox.jsonl"
        migrated_path = self.config_dir / "outbox.jsonl.migrated"
        if legacy_path.exists():
            with self._lock, contextlib.suppress(FileNotFoundError):
                os.replace(legacy_path, migrated_path)

    def _migrate_legacy_outbox_in_tx(self, conn: sqlite3.Connection) -> int:
        legacy_path = self.config_dir / "outbox.jsonl"
        migrated_path = self.config_dir / "outbox.jsonl.migrated"
        if not legacy_path.exists():
            return 0

        now = time.time()
        migrated_count = 0
        raw_text = legacy_path.read_text(encoding="utf-8", errors="replace")
        for line in raw_text.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if not isinstance(item, dict):
                continue
            payload = dict(item)
            mid = str(payload.get("id") or new_message_id())
            payload["id"] = mid
            payload.setdefault("v", 2)
            payload.setdefault("op", "send")
            created = float(payload.get("created") or payload.get("ts") or now)
            kind = str(payload.get("kind") or "task")
            deadline = compute_deadline(
                kind=kind,
                base_ts=created,
                explicit_deadline=payload.get("deadline"),
            )
            state = "expired" if now >= deadline else "pending"
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO outbox (id, payload, created, deadline, acked_hubs, state)
                VALUES (?, ?, ?, ?, '[]', ?)
                """,
                (mid, json.dumps(payload, ensure_ascii=False), created, deadline, state),
            )
            if cur.rowcount:
                migrated_count += 1

        with contextlib.suppress(FileNotFoundError):
            os.replace(legacy_path, migrated_path)
        return migrated_count

    # ------------------------------------------------------------------
    # Outbox operations
    # ------------------------------------------------------------------

    def enqueue_outbox(
        self,
        payload: dict[str, Any],
        *,
        created: float | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Persist an outbound message in `outbox` (`state='pending'`, `acked_hubs='[]'`)."""
        now = float(created) if created is not None else time.time()
        doc = dict(payload)
        mid = str(doc.get("id") or "").strip() or new_message_id()
        doc["id"] = mid
        doc.setdefault("v", 2)
        doc.setdefault("op", "send")
        if doc.get("corr_id") is None and doc.get("kind") in ("rpc", "task", None):
            # Preserve explicit None only when caller deliberately omitted corr_id;
            # keep existing payload keys untouched otherwise.
            pass
        eff_deadline = compute_deadline(
            kind=str(doc.get("kind") or "task"),
            base_ts=now,
            explicit_deadline=deadline if deadline is not None else doc.get("deadline"),
        )
        init_state = "expired" if now >= eff_deadline else "pending"

        def _op() -> dict[str, Any]:
            with self._immediate_tx() as conn:
                existing = conn.execute(
                    "SELECT id, payload, created, deadline, acked_hubs, state FROM outbox WHERE id = ?",
                    (mid,),
                ).fetchone()
                if existing is not None:
                    try:
                        saved_doc = json.loads(existing["payload"])
                        if isinstance(saved_doc, dict):
                            return saved_doc
                    except Exception:
                        pass
                    return doc

                conn.execute(
                    """
                    INSERT INTO outbox (id, payload, created, deadline, acked_hubs, state)
                    VALUES (?, ?, ?, ?, '[]', ?)
                    """,
                    (mid, json.dumps(doc, ensure_ascii=False), now, eff_deadline, init_state),
                )
                return doc

        return _with_sqlite_retry(_op)

    def expire_outbox(self, now: float | None = None) -> int:
        """Transition any `pending` outbox entries past `deadline` to `expired`."""
        ts = float(now) if now is not None else time.time()

        def _op() -> int:
            with self._immediate_tx() as conn:
                cur = conn.execute(
                    "UPDATE outbox SET state = 'expired' WHERE state = 'pending' AND deadline <= ?",
                    (ts,),
                )
                return int(cur.rowcount or 0)

        return _with_sqlite_retry(_op)

    def list_outbox_pending(
        self,
        *,
        hub_key: str | None = None,
        limit: int | None = None,
        expire_now: bool = True,
    ) -> list[dict[str, Any]]:
        """Return unexpired `pending` outbox payload dicts (excluding `hub_key` if already ACKed)."""
        if expire_now:
            self.expire_outbox()

        def _op() -> list[dict[str, Any]]:
            with self._lock:
                sql = "SELECT id, payload, acked_hubs, created, deadline FROM outbox WHERE state = 'pending' ORDER BY created ASC, rowid ASC"
                params: tuple[Any, ...] = ()
                if limit is not None and limit > 0:
                    sql += " LIMIT ?"
                    params = (int(limit),)
                rows = self._conn.execute(sql, params).fetchall()
                items: list[dict[str, Any]] = []
                for r in rows:
                    if hub_key:
                        try:
                            acked = json.loads(r["acked_hubs"] or "[]")
                        except Exception:
                            acked = []
                        if isinstance(acked, list) and hub_key in acked:
                            continue
                    try:
                        doc = json.loads(r["payload"])
                    except Exception:
                        continue
                    if isinstance(doc, dict):
                        doc.setdefault("id", r["id"])
                        items.append(doc)
                return items

        return _with_sqlite_retry(_op)

    def mark_outbox_acked(
        self,
        mid: str,
        hub_key: str = "default",
        *,
        required_hubs: list[str] | None = None,
    ) -> bool:
        """Record storage ACK from `hub_key`; in single-hub mode (I1) transitions to `done`."""
        clean_hub = str(hub_key or "default")

        def _op() -> bool:
            with self._immediate_tx() as conn:
                row = conn.execute(
                    "SELECT acked_hubs, state FROM outbox WHERE id = ?",
                    (mid,),
                ).fetchone()
                if row is None:
                    return False
                try:
                    acked = json.loads(row["acked_hubs"] or "[]")
                    if not isinstance(acked, list):
                        acked = []
                except Exception:
                    acked = []
                if clean_hub not in acked:
                    acked.append(clean_hub)

                if required_hubs:
                    all_acked = all(h in acked for h in required_hubs)
                else:
                    all_acked = len(acked) >= 1

                new_state = "done" if all_acked else str(row["state"])
                if row["state"] == "done":
                    new_state = "done"
                conn.execute(
                    "UPDATE outbox SET acked_hubs = ?, state = ? WHERE id = ?",
                    (json.dumps(acked, ensure_ascii=False), new_state, mid),
                )
                return new_state == "done"

        return _with_sqlite_retry(_op)

    def mark_outbox_done(self, mid: str) -> bool:
        def _op() -> bool:
            with self._immediate_tx() as conn:
                cur = conn.execute("UPDATE outbox SET state = 'done' WHERE id = ?", (mid,))
                return bool(cur.rowcount)

        return _with_sqlite_retry(_op)

    def mark_outbox_expired(self, mid: str) -> bool:
        def _op() -> bool:
            with self._immediate_tx() as conn:
                cur = conn.execute(
                    "UPDATE outbox SET state = 'expired' WHERE id = ? AND state = 'pending'",
                    (mid,),
                )
                return bool(cur.rowcount)

        return _with_sqlite_retry(_op)

    def get_outbox_row(self, mid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM outbox WHERE id = ?", (mid,)).fetchone()
            return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Inbox & execution state machine operations
    # ------------------------------------------------------------------

    def has_inbox_id(self, mid: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM inbox WHERE id = ?", (mid,)).fetchone()
            return row is not None

    def get_inbox_row(self, mid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM inbox WHERE id = ?", (mid,)).fetchone()
            return dict(row) if row is not None else None

    def receive_inbox(
        self,
        *,
        mid: str,
        sender: str,
        corr_id: str | None,
        kind: str,
        text: str,
        hop: int = 0,
        received_at: float | None = None,
        deadline: float | None = None,
        eligible: bool = False,
    ) -> tuple[bool, str]:
        """Atomically `INSERT OR IGNORE` incoming message into `inbox`.

        Returns `(inserted_or_promoted, effective_state)`.
        - New row: inserted as `'queued'` if `eligible` else `'skipped'`.
        - Duplicate `id`: does NOT overwrite `'queued'|'running'|'done'|'uncertain'|'expired'`.
          Only if a previous `watch`-mode label recorded `'skipped'` and a worker label
          now evaluates `eligible=True` does it promote `'skipped' -> 'queued'`.
        """
        now = float(received_at) if received_at is not None else time.time()
        eff_deadline = compute_deadline(kind=kind, base_ts=now, explicit_deadline=deadline)
        init_state = "queued" if eligible else "skipped"

        def _op() -> tuple[bool, str]:
            with self._immediate_tx() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO inbox
                        (id, "from", corr_id, kind, text, received_at, deadline, state,
                         result_status, result_text, updated_at, hop, owner_worker, owner_pid)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL)
                    """,
                    (
                        mid,
                        sender,
                        corr_id,
                        kind,
                        text,
                        now,
                        eff_deadline,
                        init_state,
                        now,
                        int(hop),
                    ),
                )
                if cur.rowcount:
                    return True, init_state

                existing = conn.execute(
                    "SELECT state FROM inbox WHERE id = ?",
                    (mid,),
                ).fetchone()
                if existing is None:
                    return False, init_state
                cur_state = str(existing["state"])
                if cur_state == "skipped" and eligible:
                    conn.execute(
                        "UPDATE inbox SET state = 'queued', text = ?, updated_at = ? WHERE id = ? AND state = 'skipped'",
                        (text, now, mid),
                    )
                    return True, "queued"
                return False, cur_state

        return _with_sqlite_retry(_op)

    def expire_queued_inbox(self, now: float | None = None) -> list[dict[str, Any]]:
        """Atomically transition expired `queued` items (`now >= deadline`) to `expired`
        and enqueue `status='expired'` reply notifications into `outbox`."""
        ts = float(now) if now is not None else time.time()

        def _op() -> list[dict[str, Any]]:
            replies: list[dict[str, Any]] = []
            with self._immediate_tx() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM inbox
                    WHERE state = 'queued' AND deadline <= ?
                    ORDER BY received_at ASC, rowid ASC
                    """,
                    (ts,),
                ).fetchall()
                for row in rows:
                    mid = str(row["id"])
                    err_text = f"[expired] request {mid} exceeded deadline"
                    upd = conn.execute(
                        """
                        UPDATE inbox
                        SET state = 'expired',
                            result_status = 'expired',
                            result_text = ?,
                            updated_at = ?
                        WHERE id = ? AND state = 'queued'
                        """,
                        (err_text, ts, mid),
                    )
                    if not upd.rowcount:
                        continue
                    if _needs_terminal_reply(row):
                        rep = _build_reply_payload(row, status="expired", text=err_text)
                        rep_deadline = compute_deadline(kind=rep["kind"], base_ts=ts)
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO outbox (id, payload, created, deadline, acked_hubs, state)
                            VALUES (?, ?, ?, ?, '[]', 'pending')
                            """,
                            (rep["id"], json.dumps(rep, ensure_ascii=False), ts, rep_deadline),
                        )
                        replies.append(rep)
            return replies

        return _with_sqlite_retry(_op)

    def claim_next_queued(
        self,
        *,
        worker_id: str,
        worker_pid: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim 1 unexpired `queued` item (`queued -> running`) via `BEGIN IMMEDIATE`."""
        ts = float(now) if now is not None else time.time()
        pid = int(worker_pid) if worker_pid is not None else os.getpid()

        def _op() -> dict[str, Any] | None:
            with self._immediate_tx() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM inbox
                    WHERE state = 'queued' AND deadline > ?
                    ORDER BY received_at ASC, rowid ASC
                    LIMIT 1
                    """,
                    (ts,),
                ).fetchone()
                if row is None:
                    return None
                mid = str(row["id"])
                upd = conn.execute(
                    """
                    UPDATE inbox
                    SET state = 'running',
                        updated_at = ?,
                        owner_worker = ?,
                        owner_pid = ?
                    WHERE id = ? AND state = 'queued'
                    """,
                    (ts, worker_id, pid, mid),
                )
                if not upd.rowcount:
                    return None
                claimed = conn.execute("SELECT * FROM inbox WHERE id = ?", (mid,)).fetchone()
                return dict(claimed) if claimed is not None else None

        return _with_sqlite_retry(_op)

    def complete_inbox_and_enqueue_reply(
        self,
        mid: str,
        *,
        result_status: str,
        result_text: str,
        reply_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Single atomic transaction: mark `inbox` `done` AND persist reply into `outbox`."""
        ts = float(now) if now is not None else time.time()

        def _op() -> dict[str, Any] | None:
            with self._immediate_tx() as conn:
                row = conn.execute("SELECT * FROM inbox WHERE id = ?", (mid,)).fetchone()
                if row is None:
                    return None
                conn.execute(
                    """
                    UPDATE inbox
                    SET state = 'done',
                        result_status = ?,
                        result_text = ?,
                        updated_at = ?,
                        owner_worker = NULL,
                        owner_pid = NULL
                    WHERE id = ?
                    """,
                    (result_status, result_text, ts, mid),
                )
                if not _needs_terminal_reply(row):
                    return None
                rep = _build_reply_payload(
                    row,
                    status=result_status,
                    text=result_text,
                    reply_id=reply_id,
                )
                rep_deadline = compute_deadline(kind=rep["kind"], base_ts=ts)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO outbox (id, payload, created, deadline, acked_hubs, state)
                    VALUES (?, ?, ?, ?, '[]', 'pending')
                    """,
                    (rep["id"], json.dumps(rep, ensure_ascii=False), ts, rep_deadline),
                )
                return rep

        return _with_sqlite_retry(_op)

    def recover_orphaned_running(self, now: float | None = None) -> list[dict[str, Any]]:
        """Transition orphaned `running` rows (whose worker died) to `uncertain` (`exec_uncertain`)
        and enqueue `status='exec_uncertain'` notifications in `outbox`, leaving live sibling
        label workers' `running` rows untouched."""
        ts = float(now) if now is not None else time.time()

        def _op() -> list[dict[str, Any]]:
            replies: list[dict[str, Any]] = []
            with self._immediate_tx() as conn:
                rows = conn.execute(
                    "SELECT * FROM inbox WHERE state = 'running' ORDER BY received_at ASC, rowid ASC"
                ).fetchall()
                for row in rows:
                    owner_w = row["owner_worker"]
                    owner_p = row["owner_pid"]
                    if is_worker_owner_alive(self.config_dir, owner_w, owner_p):
                        continue
                    mid = str(row["id"])
                    err_text = f"[exec_uncertain] worker terminated during execution of {mid}"
                    upd = conn.execute(
                        """
                        UPDATE inbox
                        SET state = 'uncertain',
                            result_status = 'exec_uncertain',
                            result_text = ?,
                            updated_at = ?,
                            owner_worker = NULL,
                            owner_pid = NULL
                        WHERE id = ? AND state = 'running'
                        """,
                        (err_text, ts, mid),
                    )
                    if not upd.rowcount:
                        continue
                    if _needs_terminal_reply(row):
                        rep = _build_reply_payload(row, status="exec_uncertain", text=err_text)
                        rep_deadline = compute_deadline(kind=rep["kind"], base_ts=ts)
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO outbox (id, payload, created, deadline, acked_hubs, state)
                            VALUES (?, ?, ?, ?, '[]', 'pending')
                            """,
                            (rep["id"], json.dumps(rep, ensure_ascii=False), ts, rep_deadline),
                        )
                        replies.append(rep)
            return replies

        return _with_sqlite_retry(_op)

    # ------------------------------------------------------------------
    # Cursors & CLI diagnostics
    # ------------------------------------------------------------------

    def get_cursor(self, hub_key: str, *, label: str = "main", fallback_seq: int = 0) -> int:
        scoped_key = cursor_hub_key(hub_key, label=label)
        clean_label = str(label or "main").strip() or "main"
        with self._lock:
            row = self._conn.execute(
                "SELECT seq FROM cursors WHERE hub_key = ?",
                (scoped_key,),
            ).fetchone()
            if row is not None:
                return int(row["seq"] or 0)
            # Only seed from legacy `cursor-<label>` on first upgrade when no journal cursor
            # exists yet for this label; any subsequent new hub_key/hub_id starts at since=0 (H10).
            if clean_label == "main":
                any_row = self._conn.execute(
                    "SELECT 1 FROM cursors WHERE instr(hub_key, '#') = 0 LIMIT 1"
                ).fetchone()
            else:
                suffix = f"#{clean_label}"
                any_row = self._conn.execute(
                    "SELECT 1 FROM cursors WHERE hub_key LIKE ? LIMIT 1",
                    (f"%{suffix}",),
                ).fetchone()
            if any_row is not None:
                return 0
            return max(0, int(fallback_seq))

    def advance_cursor(self, hub_key: str, seq: int, *, label: str = "main") -> int:
        """Monotonically advance `cursors[hub_key]` = `max(existing, seq)`."""
        scoped_key = cursor_hub_key(hub_key, label=label)
        target_seq = max(0, int(seq))

        def _op() -> int:
            with self._immediate_tx() as conn:
                conn.execute(
                    """
                    INSERT INTO cursors (hub_key, seq)
                    VALUES (?, ?)
                    ON CONFLICT(hub_key) DO UPDATE SET
                        seq = CASE WHEN excluded.seq > cursors.seq THEN excluded.seq ELSE cursors.seq END
                    """,
                    (scoped_key, target_seq),
                )
                row = conn.execute(
                    "SELECT seq FROM cursors WHERE hub_key = ?",
                    (scoped_key,),
                ).fetchone()
                return int(row["seq"]) if row is not None else target_seq

        return _with_sqlite_retry(_op)

    def status_summary(self, *, expire_outbox_now: bool = False) -> dict[str, Any]:
        """Return full counts for `outbox`, `inbox`, `uncertain` entries, and `cursors`."""
        if expire_outbox_now:
            self.expire_outbox()

        with self._lock:
            outbox_counts = {st: 0 for st in OUTBOX_STATES}
            for r in self._conn.execute(
                "SELECT state, COUNT(*) AS cnt FROM outbox GROUP BY state"
            ).fetchall():
                st = str(r["state"])
                if st in outbox_counts:
                    outbox_counts[st] = int(r["cnt"])

            inbox_counts = {st: 0 for st in INBOX_STATES}
            for r in self._conn.execute(
                "SELECT state, COUNT(*) AS cnt FROM inbox GROUP BY state"
            ).fetchall():
                st = str(r["state"])
                if st in inbox_counts:
                    inbox_counts[st] = int(r["cnt"])

            uncertain_rows = self._conn.execute(
                """
                SELECT id, "from", corr_id, kind, received_at, deadline, result_status, result_text, updated_at
                FROM inbox
                WHERE state = 'uncertain'
                ORDER BY updated_at ASC, rowid ASC
                """
            ).fetchall()
            uncertain_items = [
                {
                    "id": str(r["id"]),
                    "from": str(r["from"]),
                    "corr_id": r["corr_id"],
                    "kind": str(r["kind"]),
                    "received_at": float(r["received_at"]),
                    "deadline": float(r["deadline"]),
                    "result_status": r["result_status"],
                    "result_text": r["result_text"],
                    "updated_at": float(r["updated_at"]),
                }
                for r in uncertain_rows
            ]

            cursors = {
                str(r["hub_key"]): int(r["seq"])
                for r in self._conn.execute("SELECT hub_key, seq FROM cursors ORDER BY hub_key ASC").fetchall()
            }

            return {
                "db_path": str(self.path),
                "outbox": outbox_counts,
                "inbox": inbox_counts,
                "uncertain_items": uncertain_items,
                "cursors": cursors,
            }
