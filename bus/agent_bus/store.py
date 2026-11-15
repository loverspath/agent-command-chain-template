"""SQLite WAL durable storage for agent_bus (messages, leased deliveries, RPC results, nonces)."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
import time
from typing import Any

from .config import LEASE_TTL, MAX_DELIVERY_ATTEMPTS, QUEUE_TTL

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    corr_id TEXT,
    sender TEXT NOT NULL,
    fnode TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'task',
    status TEXT NOT NULL DEFAULT 'ok',
    hop INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL,
    expires_at REAL NOT NULL,
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
"""

TERMINAL_RPC_STATUSES = {"ok", "exec_error", "auth_error"}


def _row_to_msg(row: sqlite3.Row) -> dict[str, Any]:
    d = {
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
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO messages
                        (seq, id, corr_id, sender, fnode, target, kind, status, hop, ts, expires_at, trust, text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        seq if seq > 0 else None,
                        mid,
                        m.get("corr_id"),
                        frm,
                        fnode,
                        target,
                        str(m.get("kind") or "task"),
                        str(m.get("status") or "ok"),
                        hop,
                        ts,
                        ts + ttl,
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
        status: str = "ok",
        corr_id: str | None = None,
        ttl: float = QUEUE_TTL,
        trust: str = "internal",
        ts: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Insert a message idempotently by `mid`. Returns `(msg_dict, is_duplicate)`."""
        now = ts if ts is not None else time.time()
        expires_at = now + max(1.0, float(ttl))
        with self._lock:
            existing = self._conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
            if existing is not None:
                return _row_to_msg(existing), True

            cur = self._conn.execute(
                """
                INSERT INTO messages
                    (id, corr_id, sender, fnode, target, kind, status, hop, ts, expires_at, trust, text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mid, corr_id, sender, fnode, target, kind, status, hop, now, expires_at, trust, text),
            )
            seq = int(cur.lastrowid)
            if corr_id and kind != "ack":
                existing_rpc = self._conn.execute(
                    "SELECT status FROM rpc_results WHERE corr_id = ?", (corr_id,)
                ).fetchone()
                if existing_rpc is None:
                    # First message carrying this corr_id is the request itself: mark pending.
                    self._upsert_rpc_result_locked(
                        corr_id=corr_id,
                        seq=seq,
                        sender=sender,
                        status="pending",
                        text="",
                        ts=now,
                        expires_at=expires_at,
                    )
                elif mid != corr_id:
                    # Subsequent message with same corr_id is the reply/result.
                    self._upsert_rpc_result_locked(
                        corr_id=corr_id,
                        seq=seq,
                        sender=sender,
                        status=status,
                        text=text,
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
            "SELECT status FROM rpc_results WHERE corr_id = ?", (corr_id,)
        ).fetchone()
        if existing is not None and existing["status"] in TERMINAL_RPC_STATUSES and status not in TERMINAL_RPC_STATUSES:
            return
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
                "SELECT fnode FROM messages WHERE id = ? LIMIT 1", (corr_id,)
            ).fetchone()
            return str(row["fnode"]) if row is not None else None

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
                # or internal node traffic; only direct node/consumer targets or corr_id replies
                # to requests originally sent by this external node.
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
                                    WHERE orig.id = m.corr_id AND orig.fnode = ?
                                )
                            )
                          )
                      AND (
                            (m.seq > ? AND d.state IS NULL)
                            OR d.state = 'pending'
                          )
                    ORDER BY m.seq ASC
                    LIMIT ?
                """
                params = (consumer, now, consumer, node, consumer, node, since, limit)
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
            cur = self._conn.execute("DELETE FROM messages WHERE expires_at < ?", (ts,))
            self._conn.execute("DELETE FROM rpc_results WHERE expires_at < ?", (ts,))
            self._conn.execute("DELETE FROM nonces WHERE expires_at < ?", (ts,))
            self._conn.execute(
                "DELETE FROM deliveries WHERE seq NOT IN (SELECT seq FROM messages)"
            )
            self._conn.commit()
            return int(cur.rowcount or 0)
