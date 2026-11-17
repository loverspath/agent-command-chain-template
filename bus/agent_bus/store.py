"""SQLite WAL durable storage for agent_bus (messages, leased deliveries, RPC results, nonces)."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import threading
import time
import uuid
from typing import Any

from .config import LEASE_TTL, MAX_DELIVERY_ATTEMPTS, QUEUE_TTL

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    corr_id TEXT,
    sender TEXT NOT NULL,
    fnode TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'task',
    rkind TEXT DEFAULT NULL,
    status TEXT NOT NULL DEFAULT 'ok',
    hop INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL,
    expires_at REAL NOT NULL,
    deadline REAL DEFAULT NULL,
    trust TEXT NOT NULL DEFAULT 'internal',
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_expires ON messages(expires_at);
CREATE INDEX IF NOT EXISTS idx_messages_corr ON messages(corr_id);
CREATE INDEX IF NOT EXISTS idx_messages_target ON messages(target);

CREATE TABLE IF NOT EXISTS deliveries (
    seq INTEGER NOT NULL,
    consumer TEXT NOT NULL,
    state TEXT NOT NULL,
    lease_until REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (seq, consumer)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_consumer_state ON deliveries(consumer, state, lease_until);

CREATE TABLE IF NOT EXISTS rpc_results (
    corr_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL DEFAULT 0,
    sender TEXT NOT NULL,
    status TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rpc_expires ON rpc_results(expires_at);

CREATE TABLE IF NOT EXISTS nonces (
    nonce_key TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nonces_expires ON nonces(expires_at);

CREATE TABLE IF NOT EXISTS fleet_status (
    node TEXT PRIMARY KEY,
    rev INTEGER NOT NULL,
    commit_sha TEXT NOT NULL DEFAULT '',
    target_commit TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL,
    config_changed INTEGER NOT NULL DEFAULT 0,
    code_changed INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);
"""

NON_TERMINAL_RPC_STATUSES = frozenset({"", "pending", "running"})
TERMINAL_RPC_STATUSES = {
    "ok",
    "exec_error",
    "auth_error",
    "exec_uncertain",
    "expired",
    "dead",
}
VALID_RKINDS = frozenset({"request", "result", "event"})


class IdConflictError(ValueError):
    """Raised when an existing message `id` is re-submitted with different `target` or `text`."""


def is_terminal_rpc_status(status: str | None) -> bool:
    clean = str(status or "").strip().lower()
    return clean not in NON_TERMINAL_RPC_STATUSES


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _row_to_msg(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    d: dict[str, Any] = {
        "v": 2,
        "op": "msg",
        "seq": row["seq"],
        "id": row["id"],
        "corr_id": row["corr_id"],
        "from": row["sender"],
        "fnode": row["fnode"],
        "to": row["target"],
        "kind": row["kind"],
        "status": row["status"],
        "hop": row["hop"],
        "ts": row["ts"],
        "trust": row["trust"],
        "text": row["text"],
    }
    if "rkind" in keys and row["rkind"] is not None:
        d["rkind"] = row["rkind"]
    if "deadline" in keys and row["deadline"] is not None:
        d["deadline"] = float(row["deadline"])
    return d


class BusStore:
    """Thread-safe SQLite WAL store for durable bus messaging."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.executescript(_SCHEMA)
            self._migrate_schema_locked()
            self.hub_id: str = self._ensure_hub_id_locked()
            self._conn.commit()

    def _migrate_schema_locked(self) -> None:
        """Safely add v2 optional nullable columns on pre-existing v1/e7d0da2 databases."""
        msg_cols = {
            str(r["name"])
            for r in self._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "rkind" not in msg_cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN rkind TEXT")
        if "deadline" not in msg_cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN deadline REAL")

    def _ensure_hub_id_locked(self) -> str:
        """Transactionally initialize and return persistent `hub_meta.hub_id`."""
        row = self._conn.execute(
            "SELECT value FROM hub_meta WHERE key = 'hub_id'"
        ).fetchone()
        if row is not None and str(row["value"]).strip():
            return str(row["value"]).strip()
        generated = uuid.uuid4().hex
        self._conn.execute(
            "INSERT OR IGNORE INTO hub_meta (key, value) VALUES ('hub_id', ?)",
            (generated,),
        )
        saved = self._conn.execute(
            "SELECT value FROM hub_meta WHERE key = 'hub_id'"
        ).fetchone()
        return str(saved["value"]) if saved is not None else generated

    def get_hub_id(self) -> str:
        with self._lock:
            return self.hub_id

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM hub_meta WHERE key = ?", (str(key),)
            ).fetchone()
            return str(row["value"]) if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO hub_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(key), str(value)),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_max_seq(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM messages").fetchone()
            seq_row = self._conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='messages'"
            ).fetchone()
            return max(int(row["m"] or 0), int(seq_row["seq"] if seq_row else 0))

    def import_jsonl_if_empty(self, jsonl_path: pathlib.Path, ttl: float = QUEUE_TTL) -> int:
        """Seed SQLite from legacy messages.jsonl when starting with an empty DB."""
        if not jsonl_path.exists():
            return 0
        with self._lock:
            if self.get_max_seq() > 0:
                return 0
            now = time.time()
            max_seen_seq = 0
            imported = 0
            for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]:
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if m.get("op") != "msg":
                    continue
                seq = int(m.get("seq") or 0)
                max_seen_seq = max(max_seen_seq, seq)
                ts = float(m.get("ts") or 0.0)
                if now - ts >= ttl:
                    continue
                mid = str(m.get("id") or f"legacy-{seq}")
                frm = str(m.get("from") or "")
                fnode = str(m.get("fnode") or (frm.split("/")[0] if "/" in frm else frm))
                target = str(m.get("to") or "*")
                hop = int(m.get("hop") or 0)
                text = str(m.get("text") or "")
                raw_rk = m.get("rkind")
                rk_val = str(raw_rk).strip().lower() if raw_rk is not None else None
                if rk_val not in VALID_RKINDS:
                    rk_val = None
                raw_dl = m.get("deadline")
                dl_val = float(raw_dl) if raw_dl is not None else None
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO messages
                        (seq, id, corr_id, sender, fnode, target, kind, rkind, status, hop, ts, expires_at, deadline, trust, text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        seq if seq > 0 else None,
                        mid,
                        m.get("corr_id"),
                        frm,
                        fnode,
                        target,
                        str(m.get("kind") or "task"),
                        rk_val,
                        str(m.get("status") or "ok"),
                        hop,
                        ts,
                        ts + ttl,
                        dl_val,
                        str(m.get("trust") or "internal"),
                        text,
                    ),
                )
                imported += 1
            if max_seen_seq > 0:
                # Ensure AUTOINCREMENT never reuses old seq values even if all imported messages were expired.
                self._conn.execute("INSERT OR IGNORE INTO sqlite_sequence(name, seq) VALUES ('messages', ?)", (max_seen_seq,))
                self._conn.execute(
                    "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name='messages'",
                    (max_seen_seq,),
                )
            self._conn.commit()
            return imported

    def enqueue_message(
        self,
        *,
        mid: str,
        sender: str,
        fnode: str,
        target: str,
        text: str,
        hop: int = 0,
        kind: str = "task",
        rkind: str | None = None,
        status: str = "ok",
        corr_id: str | None = None,
        ttl: float = QUEUE_TTL,
        deadline: float | None = None,
        trust: str = "internal",
        ts: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Insert a message idempotently by `mid`.

        - Identical `(target, text)` SHA-256 -> returns `(existing_msg, True)` (`duplicate`).
        - Different `target` or `text` SHA-256 -> raises `IdConflictError` without mutating DB.
        """
        now = ts if ts is not None else time.time()
        expires_at = now + max(1.0, float(ttl))
        norm_rkind: str | None = None
        if rkind is not None:
            cand = str(rkind).strip().lower()
            if cand in VALID_RKINDS:
                norm_rkind = cand
        norm_deadline = float(deadline) if deadline is not None else None

        with self._lock:
            existing = self._conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
            if existing is not None:
                same_target = _sha256_text(str(existing["target"])) == _sha256_text(target)
                same_text = _sha256_text(str(existing["text"])) == _sha256_text(text)
                if not (same_target and same_text):
                    raise IdConflictError(
                        f"id_conflict: message id '{mid}' already exists with different target or text"
                    )
                return _row_to_msg(existing), True

            cur = self._conn.execute(
                """
                INSERT INTO messages
                    (id, corr_id, sender, fnode, target, kind, rkind, status, hop, ts, expires_at, deadline, trust, text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mid,
                    corr_id,
                    sender,
                    fnode,
                    target,
                    kind,
                    norm_rkind,
                    status,
                    hop,
                    now,
                    expires_at,
                    norm_deadline,
                    trust,
                    text,
                ),
            )
            seq = int(cur.lastrowid)
            if corr_id and kind != "ack":
                is_result = (norm_rkind == "result") or (norm_rkind is None and mid != corr_id)
                is_request = (not is_result) and (norm_rkind == "request" or mid == corr_id)
                if is_result:
                    # Terminal RPC upsert regardless of whether original request exists on this hub.
                    # Non-terminal `status == "running"` (or `"pending"`) is never treated as terminal.
                    if is_terminal_rpc_status(status):
                        self._upsert_rpc_result_locked(
                            corr_id=corr_id,
                            seq=seq,
                            sender=sender,
                            status=status,
                            text=text,
                            ts=now,
                            expires_at=expires_at,
                        )
                elif is_request:
                    # Record `pending` only when no unexpired RPC entry exists yet; NEVER overwrite
                    # an already-arrived terminal (or running) RPC result!
                    existing_rpc = self._conn.execute(
                        "SELECT status, expires_at FROM rpc_results WHERE corr_id = ?",
                        (corr_id,),
                    ).fetchone()
                    if existing_rpc is None or float(existing_rpc["expires_at"]) <= now:
                        self._upsert_rpc_result_locked(
                            corr_id=corr_id,
                            seq=seq,
                            sender=sender,
                            status="pending",
                            text="",
                            ts=now,
                            expires_at=expires_at,
                        )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM messages WHERE seq = ?", (seq,)).fetchone()
            return _row_to_msg(row), False

    def _upsert_rpc_result_locked(
        self,
        *,
        corr_id: str,
        seq: int,
        sender: str,
        status: str,
        text: str,
        ts: float,
        expires_at: float,
    ) -> None:
        existing = self._conn.execute(
            "SELECT status, expires_at FROM rpc_results WHERE corr_id = ?", (corr_id,)
        ).fetchone()
        if (
            existing is not None
            and float(existing["expires_at"]) > ts
            and is_terminal_rpc_status(str(existing["status"]))
        ):
            # Never overwrite an unexpired terminal result with progress/pending or late duplicates.
            return
        if is_terminal_rpc_status(status):
            self._conn.execute(
                """
                INSERT INTO rpc_results (corr_id, seq, sender, status, text, ts, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(corr_id) DO UPDATE SET
                    seq = CASE WHEN excluded.seq > 0 THEN excluded.seq ELSE rpc_results.seq END,
                    sender = excluded.sender,
                    status = excluded.status,
                    text = excluded.text,
                    ts = excluded.ts,
                    expires_at = excluded.expires_at
                """,
                (corr_id, seq, sender, status, text, ts, expires_at),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO rpc_results (corr_id, seq, sender, status, text, ts, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(corr_id) DO UPDATE SET
                    seq = CASE WHEN excluded.seq > 0 THEN excluded.seq ELSE rpc_results.seq END,
                    sender = excluded.sender,
                    status = excluded.status,
                    text = CASE WHEN excluded.text != '' THEN excluded.text ELSE rpc_results.text END,
                    ts = excluded.ts,
                    expires_at = excluded.expires_at
                """,
                (corr_id, seq, sender, status, text, ts, expires_at),
            )

    def record_progress(
        self,
        *,
        corr_id: str,
        sender: str,
        status: str = "running",
        text: str = "",
        ttl: float = QUEUE_TTL,
    ) -> None:
        now = time.time()
        with self._lock:
            self._upsert_rpc_result_locked(
                corr_id=corr_id,
                seq=0,
                sender=sender,
                status=status,
                text=text,
                ts=now,
                expires_at=now + max(60.0, float(ttl)),
            )
            self._conn.commit()

    def get_rpc_result(self, corr_id: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rpc_results WHERE corr_id = ? AND expires_at > ?",
                (corr_id, now),
            ).fetchone()
            if row is None:
                return None
            return {
                "corr_id": row["corr_id"],
                "seq": row["seq"],
                "sender": row["sender"],
                "status": row["status"],
                "text": row["text"],
                "ts": row["ts"],
            }

    def get_message_sender_node(self, corr_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT fnode FROM messages
                WHERE id = ? OR (corr_id = ? AND rkind = 'request')
                ORDER BY seq ASC LIMIT 1
                """,
                (corr_id, corr_id),
            ).fetchone()
            return str(row["fnode"]) if row is not None else None

    def can_external_access_rpc(self, corr_id: str, ext_node: str, *, now: float | None = None) -> bool:
        """Return True iff `ext_node` owns `corr_id` either via original request on this hub
        or via a result message targeting `ext_node` / `ext_node/*` (cross-hub R07/X08)."""
        ts = now if now is not None else time.time()
        clean_node = str(ext_node or "").strip().lower()
        if not clean_node or not corr_id:
            return False
        with self._lock:
            orig = self._conn.execute(
                """
                SELECT fnode FROM messages
                WHERE id = ? OR (corr_id = ? AND rkind = 'request')
                ORDER BY seq ASC LIMIT 1
                """,
                (corr_id, corr_id),
            ).fetchone()
            if orig is not None:
                return str(orig["fnode"]).lower() == clean_node

            res_row = self._conn.execute(
                """
                SELECT 1 FROM messages
                WHERE corr_id = ?
                  AND expires_at > ?
                  AND (rkind = 'result' OR (rkind IS NULL AND id != corr_id))
                  AND lower(fnode) != ?
                  AND (lower(target) = ? OR lower(target) LIKE ?)
                LIMIT 1
                """,
                (corr_id, ts, clean_node, clean_node, f"{clean_node}/%"),
            ).fetchone()
            return res_row is not None

    def get_message_routing(self, msg_id: str) -> tuple[str, str] | None:
        """Return (fnode, target) for an existing message by id or corr_id."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fnode, target FROM messages WHERE id = ? OR corr_id = ? ORDER BY seq ASC LIMIT 1",
                (msg_id, msg_id),
            ).fetchone()
            return (str(row["fnode"]), str(row["target"])) if row is not None else None

    def lease_live_delivery(
        self,
        seq: int,
        consumer: str,
        lease_ttl: float = LEASE_TTL,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO deliveries (seq, consumer, state, lease_until, attempts, updated_at)
                VALUES (?, ?, 'leased', ?, 1, ?)
                ON CONFLICT(seq, consumer) DO UPDATE SET
                    state = 'leased',
                    lease_until = excluded.lease_until,
                    attempts = deliveries.attempts + 1,
                    updated_at = excluded.updated_at
                """,
                (seq, consumer, now + lease_ttl, now),
            )
            self._conn.commit()

    def ack_deliveries(self, consumer: str, seqs: list[int]) -> int:
        if not seqs:
            return 0
        now = time.time()
        with self._lock:
            count = 0
            for seq in seqs:
                self._conn.execute(
                    """
                    INSERT INTO deliveries (seq, consumer, state, lease_until, attempts, updated_at)
                    VALUES (?, ?, 'acked', 0, 1, ?)
                    ON CONFLICT(seq, consumer) DO UPDATE SET
                        state = 'acked',
                        lease_until = 0,
                        updated_at = excluded.updated_at
                    """,
                    (int(seq), consumer, now),
                )
                count += 1
            self._conn.commit()
            return count

    def nack_delivery(self, consumer: str, seq: int, max_attempts: int = MAX_DELIVERY_ATTEMPTS) -> str:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM deliveries WHERE seq = ? AND consumer = ?",
                (int(seq), consumer),
            ).fetchone()
            attempts = int(row["attempts"]) if row is not None else 1
            new_state = "dead" if attempts >= max_attempts else "pending"
            self._conn.execute(
                """
                INSERT INTO deliveries (seq, consumer, state, lease_until, attempts, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(seq, consumer) DO UPDATE SET
                    state = ?,
                    lease_until = 0,
                    updated_at = excluded.updated_at
                """,
                (int(seq), consumer, new_state, attempts, now, new_state),
            )
            self._conn.commit()
            return new_state

    def expire_leases_locked(self, now: float, max_attempts: int = MAX_DELIVERY_ATTEMPTS) -> None:
        self._conn.execute(
            """
            UPDATE deliveries
            SET state = CASE WHEN attempts >= ? THEN 'dead' ELSE 'pending' END,
                lease_until = 0,
                updated_at = ?
            WHERE state = 'leased' AND lease_until > 0 AND lease_until < ?
            """,
            (max_attempts, now, now),
        )

    def poll_for_consumer(
        self,
        *,
        node: str,
        consumer: str,
        since: int = 0,
        limit: int = 50,
        trust: str = "internal",
        accept_inbox: bool = False,
        lease_ttl: float = LEASE_TTL,
        max_attempts: int = MAX_DELIVERY_ATTEMPTS,
        reclaim_on_reconnect: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch undelivered/pending messages for `consumer` and grant a delivery lease."""
        now = time.time()
        with self._lock:
            self.expire_leases_locked(now, max_attempts=max_attempts)

            # Backward-compat: if client reports cursor `since`, everything <= since is implicitly acked.
            if since > 0:
                self._conn.execute(
                    """
                    UPDATE deliveries
                    SET state = 'acked', lease_until = 0, updated_at = ?
                    WHERE consumer = ? AND seq <= ? AND state IN ('pending', 'leased')
                    """,
                    (now, consumer, since),
                )

            # On a brand-new WS connection for the same consumer key, any active lease > since
            # belonged to the dead previous socket and can be replayed immediately.
            if reclaim_on_reconnect:
                self._conn.execute(
                    """
                    UPDATE deliveries
                    SET state = CASE WHEN attempts >= ? THEN 'dead' ELSE 'pending' END,
                        lease_until = 0,
                        updated_at = ?
                    WHERE consumer = ? AND seq > ? AND state = 'leased'
                    """,
                    (max_attempts, now, consumer, since),
                )

            if trust == "external":
                # Strict namespace isolation: external consumers NEVER receive '*' broadcasts
                # or internal node traffic; only direct node/consumer targets, corr_id replies
                # to requests sent by this external node, or cross-hub result messages whose
                # target node is this external node.
                sql = """
                    SELECT m.*, d.state AS d_state, d.attempts AS d_attempts
                    FROM messages m
                    LEFT JOIN deliveries d ON d.seq = m.seq AND d.consumer = ?
                    WHERE m.expires_at > ?
                      AND m.kind != 'ack'
                      AND m.sender != ?
                      AND (
                            m.target IN (?, ?)
                            OR (
                                m.corr_id IS NOT NULL
                                AND EXISTS (
                                    SELECT 1 FROM messages orig
                                    WHERE (orig.id = m.corr_id OR (orig.corr_id = m.corr_id AND orig.rkind = 'request'))
                                      AND orig.fnode = ?
                                )
                            )
                            OR (
                                m.corr_id IS NOT NULL
                                AND (m.rkind = 'result' OR (m.rkind IS NULL AND m.id != m.corr_id))
                                AND m.fnode != ?
                                AND (m.target = ? OR m.target LIKE ?)
                            )
                          )
                      AND (
                            (m.seq > ? AND d.state IS NULL)
                            OR d.state = 'pending'
                          )
                    ORDER BY m.seq ASC
                    LIMIT ?
                """
                params: tuple[Any, ...] = (
                    consumer,
                    now,
                    consumer,
                    node,
                    consumer,
                    node,
                    node,
                    node,
                    f"{node}/%",
                    since,
                    limit,
                )
            elif accept_inbox:
                sql = """
                    SELECT m.*, d.state AS d_state, d.attempts AS d_attempts
                    FROM messages m
                    LEFT JOIN deliveries d ON d.seq = m.seq AND d.consumer = ?
                    WHERE m.expires_at > ?
                      AND m.kind != 'ack'
                      AND (
                            m.target IN (?, ?)
                            OR (m.target = '*' AND m.fnode != ?)
                            OR m.target = ?
                            OR (
                                m.target LIKE ?
                                AND m.target NOT LIKE ?
                                AND m.kind = 'event'
                            )
                          )
                      AND (
                            (m.seq > ? AND d.state IS NULL)
                            OR d.state = 'pending'
                          )
                    ORDER BY m.seq ASC
                    LIMIT ?
                """
                params = (
                    consumer,
                    now,
                    node,
                    consumer,
                    node,
                    f"{node}/inbox",
                    f"{node}/%",
                    f"{node}/cli-%",
                    since,
                    limit,
                )
            else:
                sql = """
                    SELECT m.*, d.state AS d_state, d.attempts AS d_attempts
                    FROM messages m
                    LEFT JOIN deliveries d ON d.seq = m.seq AND d.consumer = ?
                    WHERE m.expires_at > ?
                      AND m.kind != 'ack'
                      AND (
                            m.target IN (?, ?)
                            OR (m.target = '*' AND m.fnode != ?)
                          )
                      AND (
                            (m.seq > ? AND d.state IS NULL)
                            OR d.state = 'pending'
                          )
                    ORDER BY m.seq ASC
                    LIMIT ?
                """
                params = (consumer, now, node, consumer, node, since, limit)

            rows = self._conn.execute(sql, params).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                seq = int(row["seq"])
                attempts = int(row["d_attempts"] or 0) + 1
                self._conn.execute(
                    """
                    INSERT INTO deliveries (seq, consumer, state, lease_until, attempts, updated_at)
                    VALUES (?, ?, 'leased', ?, ?, ?)
                    ON CONFLICT(seq, consumer) DO UPDATE SET
                        state = 'leased',
                        lease_until = excluded.lease_until,
                        attempts = ?,
                        updated_at = excluded.updated_at
                    """,
                    (seq, consumer, now + lease_ttl, attempts, now, attempts),
                )
                msg = _row_to_msg(row)
                msg["replay"] = True
                out.append(msg)
            self._conn.commit()
            return out

    def get_queue_metrics(self, now: float | None = None) -> dict[str, Any]:
        """Compute live queue depth & age metrics (`pending`, `leased`, `oldest_pending_age`, `dead`)."""
        ts = now if now is not None else time.time()
        with self._lock:
            self.expire_leases_locked(ts, max_attempts=MAX_DELIVERY_ATTEMPTS)
            self._conn.commit()

            unleased_row = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt, MIN(m.ts) AS min_ts
                FROM messages m
                WHERE m.expires_at > ?
                  AND m.kind != 'ack'
                  AND NOT EXISTS (
                      SELECT 1 FROM deliveries d WHERE d.seq = m.seq
                  )
                """,
                (ts,),
            ).fetchone()
            pending_del_row = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt, MIN(m.ts) AS min_ts
                FROM deliveries d
                JOIN messages m ON m.seq = d.seq
                WHERE m.expires_at > ?
                  AND m.kind != 'ack'
                  AND d.state = 'pending'
                """,
                (ts,),
            ).fetchone()
            leased_row = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM deliveries d
                JOIN messages m ON m.seq = d.seq
                WHERE m.expires_at > ?
                  AND m.kind != 'ack'
                  AND d.state = 'leased'
                """,
                (ts,),
            ).fetchone()
            dead_row = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM deliveries d
                JOIN messages m ON m.seq = d.seq
                WHERE m.expires_at > ?
                  AND m.kind != 'ack'
                  AND d.state = 'dead'
                """,
                (ts,),
            ).fetchone()

            unleased_cnt = int(unleased_row["cnt"] or 0) if unleased_row is not None else 0
            pending_del_cnt = int(pending_del_row["cnt"] or 0) if pending_del_row is not None else 0
            pending_total = unleased_cnt + pending_del_cnt
            leased_total = int(leased_row["cnt"] or 0) if leased_row is not None else 0
            dead_total = int(dead_row["cnt"] or 0) if dead_row is not None else 0

            min_candidates = [
                float(r["min_ts"])
                for r in (unleased_row, pending_del_row)
                if r is not None and r["min_ts"] is not None
            ]
            oldest_pending_age: float | None = None
            if pending_total > 0 and min_candidates:
                oldest_pending_age = round(max(0.0, ts - min(min_candidates)), 3)

            return {
                "max_seq": self.get_max_seq(),
                "pending": pending_total,
                "leased": leased_total,
                "oldest_pending_age": oldest_pending_age,
                "dead": dead_total,
            }

    def check_and_record_nonce(self, nonce_key: str, ttl: float = 600.0) -> bool:
        """Return True if nonce is unused (and record it); False if already seen."""
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM nonces WHERE expires_at < ?", (now,))
            existing = self._conn.execute(
                "SELECT 1 FROM nonces WHERE nonce_key = ?", (nonce_key,)
            ).fetchone()
            if existing is not None:
                self._conn.commit()
                return False
            self._conn.execute(
                "INSERT INTO nonces (nonce_key, expires_at) VALUES (?, ?)",
                (nonce_key, now + ttl),
            )
            self._conn.commit()
            return True

    def prune_expired(self, now: float | None = None) -> int:
        ts = now if now is not None else time.time()
        with self._lock:
            cur = self._conn.execute("DELETE FROM messages WHERE expires_at <= ?", (ts,))
            self._conn.execute("DELETE FROM rpc_results WHERE expires_at <= ?", (ts,))
            self._conn.execute("DELETE FROM nonces WHERE expires_at <= ?", (ts,))
            self._conn.execute(
                "DELETE FROM deliveries WHERE seq NOT IN (SELECT seq FROM messages)"
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def upsert_fleet_status(
        self,
        *,
        node: str,
        rev: int,
        commit: str,
        result: str,
        updated_at: float | None = None,
        target_commit: str = "",
        config_changed: bool = False,
        code_changed: bool = False,
        attempts: int = 1,
    ) -> dict[str, Any]:
        """Persist monotonic per-node `fleet-ack` convergence status across hub restarts."""
        clean_node = str(node or "").strip().lower()
        if not clean_node:
            raise ValueError("node must be non-empty")
        ts = float(updated_at) if updated_at is not None else time.time()
        rev_int = int(rev)
        att_int = max(1, int(attempts))
        cfg_int = 1 if config_changed else 0
        code_int = 1 if code_changed else 0
        clean_commit = str(commit or "").strip()
        clean_target = str(target_commit or "").strip()
        clean_result = str(result or "ok").strip()

        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM fleet_status WHERE node = ?",
                (clean_node,),
            ).fetchone()
            if existing is not None and int(existing["rev"]) > rev_int:
                row = existing
            else:
                self._conn.execute(
                    """
                    INSERT INTO fleet_status
                        (node, rev, commit_sha, target_commit, result, config_changed, code_changed, attempts, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node) DO UPDATE SET
                        rev = excluded.rev,
                        commit_sha = excluded.commit_sha,
                        target_commit = excluded.target_commit,
                        result = excluded.result,
                        config_changed = excluded.config_changed,
                        code_changed = excluded.code_changed,
                        attempts = excluded.attempts,
                        updated_at = excluded.updated_at
                    """,
                    (
                        clean_node,
                        rev_int,
                        clean_commit,
                        clean_target,
                        clean_result,
                        cfg_int,
                        code_int,
                        att_int,
                        ts,
                    ),
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM fleet_status WHERE node = ?",
                    (clean_node,),
                ).fetchone()

            assert row is not None
            return {
                "node": str(row["node"]),
                "rev": int(row["rev"]),
                "commit": str(row["commit_sha"]),
                "target_commit": str(row["target_commit"]),
                "result": str(row["result"]),
                "config_changed": bool(row["config_changed"]),
                "code_changed": bool(row["code_changed"]),
                "attempts": int(row["attempts"]),
                "updated_at": float(row["updated_at"]),
            }

    def list_fleet_status(self) -> dict[str, dict[str, Any]]:
        """Return `{node: status_dict}` for all reported fleet nodes."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM fleet_status ORDER BY node ASC"
            ).fetchall()
            out: dict[str, dict[str, Any]] = {}
            for row in rows:
                out[str(row["node"])] = {
                    "rev": int(row["rev"]),
                    "commit": str(row["commit_sha"]),
                    "target_commit": str(row["target_commit"]),
                    "result": str(row["result"]),
                    "config_changed": bool(row["config_changed"]),
                    "code_changed": bool(row["code_changed"]),
                    "attempts": int(row["attempts"]),
                    "updated_at": float(row["updated_at"]),
                }
            return out
