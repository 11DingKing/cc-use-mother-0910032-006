"""只追加（append-only）分录存储。

安全保证：

1. SQLite 触发器在任何连接上禁止 ``UPDATE``/``DELETE``（含 temp 触发路径），
   分录表只能 INSERT；
2. 每条分录含 ``prev_hash``，分录哈希 = SHA-256(规范字段)，形成哈希链，
   任何脱库篡改都会在 :meth:`EntryStore.verify_chain` 暴露；
3. 幂等键唯一：同一 idem_key 第二次写入必须命中同一条分录，重试不产生新分录；
4. 所有命令在一个事务里提交（要么完整落账，要么完全不发生）。

SQLite 单库写在 ``BEGIN IMMEDIATE`` 下串行化，天然覆盖多线程并发下单；
进程内再用一把可重入锁保护"读状态—评估—落账"这一临界区。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Mapping

from .models import Entry, EntryType

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id   TEXT NOT NULL UNIQUE,
    entry_type TEXT NOT NULL,
    timestamp  TEXT NOT NULL,
    actor      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    idem_key   TEXT,
    order_id   TEXT,
    prev_hash  TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_idem ON entries(idem_key)
    WHERE idem_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entries_order ON entries(order_id);
CREATE INDEX IF NOT EXISTS idx_entries_type ON entries(entry_type);

-- 账本不可变：任何更新/删除直接回滚
CREATE TRIGGER IF NOT EXISTS entries_no_update
BEFORE UPDATE ON entries
BEGIN
    SELECT RAISE(ABORT, 'entries 表只允许追加，禁止 UPDATE');
END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete
BEFORE DELETE ON entries
BEGIN
    SELECT RAISE(ABORT, 'entries 表只允许追加，禁止 DELETE');
END;
"""

CANONICAL_FIELDS = (
    "seq",
    "entry_id",
    "entry_type",
    "timestamp",
    "actor",
    "payload",
    "idem_key",
    "order_id",
    "prev_hash",
)


def _canonical(
    seq: int,
    entry_id: str,
    entry_type: str,
    timestamp: str,
    actor: str,
    payload: str,
    idem_key: str | None,
    order_id: str | None,
    prev_hash: str,
) -> bytes:
    """规范序列化（sort_keys + 无空白），保证哈希跨进程稳定。"""
    doc = {
        "seq": seq,
        "entry_id": entry_id,
        "entry_type": entry_type,
        "timestamp": timestamp,
        "actor": actor,
        "payload": json.loads(payload),
        "idem_key": idem_key,
        "order_id": order_id,
        "prev_hash": prev_hash,
    }
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class AppendViolation(RuntimeError):
    """违反只追加约束（外部直接改库等）。"""


class EntryStore:
    """SQLite 只追加分录存储，线程安全。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,  # 显式事务
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ 基础

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def lock(self) -> threading.RLock:
        """命令临界区锁（store + 内存状态一起保护）。"""
        return self._lock

    def latest_hash(self, conn: sqlite3.Connection | None = None) -> str:
        c = conn or self._conn
        row = c.execute("SELECT entry_hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
        return row["entry_hash"] if row else ""

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]

    # ------------------------------------------------------------------ 写入

    def append(
        self,
        *,
        entry_id: str,
        entry_type: EntryType,
        actor: str,
        payload: Mapping[str, Any] | None = None,
        idem_key: str | None = None,
        order_id: str | None = None,
        timestamp: str | None = None,
    ) -> Entry:
        """追加单条分录。带 idem_key 时重试返回已存在的同一条分录。

        仅用于独立事务场景；批量命令请用 :meth:`transaction` + ``insert``。
        """
        with self._lock:
            with self.transaction() as conn:
                return self.insert(
                    conn,
                    entry_id=entry_id,
                    entry_type=entry_type,
                    actor=actor,
                    payload=payload or {},
                    idem_key=idem_key,
                    order_id=order_id,
                    timestamp=timestamp,
                )

    def insert(
        self,
        conn: sqlite3.Connection,
        *,
        entry_id: str,
        entry_type: EntryType,
        actor: str,
        payload: Mapping[str, Any] | None = None,
        idem_key: str | None = None,
        order_id: str | None = None,
        timestamp: str | None = None,
    ) -> Entry:
        """在给定事务内插入分录；idem_key 命中时返回旧分录（重试不扩敞口）。"""
        payload = payload or {}
        if idem_key is not None:
            old = conn.execute(
                "SELECT * FROM entries WHERE idem_key = ?", (idem_key,)
            ).fetchone()
            if old is not None:
                return self._row_to_entry(old)

        ts = timestamp or utcnow()
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM entries"
        ).fetchone()
        seq = row["next_seq"]
        prev_hash = self.latest_hash(conn)
        payload_text = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(
            _canonical(seq, entry_id, entry_type.value, ts, actor, payload_text,
                       idem_key, order_id, prev_hash)
        ).hexdigest()
        conn.execute(
            """INSERT INTO entries
               (seq, entry_id, entry_type, timestamp, actor, payload,
                idem_key, order_id, prev_hash, entry_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (seq, entry_id, entry_type.value, ts, actor, payload_text,
             idem_key, order_id, prev_hash, digest),
        )
        return Entry(
            seq=seq,
            entry_id=entry_id,
            entry_type=entry_type,
            timestamp=ts,
            actor=actor,
            payload=dict(payload),
            idem_key=idem_key,
            order_id=order_id,
            prev_hash=prev_hash,
            entry_hash=digest,
        )

    # ------------------------------------------------------------------ 事务

    def transaction(self) -> "TransactionCtx":
        return TransactionCtx(self._conn, self._lock)

    # ------------------------------------------------------------------ 读取

    def iter_entries(self) -> Iterator[Entry]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM entries ORDER BY seq").fetchall()
        for row in rows:
            yield self._row_to_entry(row)

    def entries_for_order(self, order_id: str) -> list[Entry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM entries WHERE order_id = ? ORDER BY seq", (order_id,)
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def find_by_idem(self, idem_key: str) -> Entry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM entries WHERE idem_key = ?", (idem_key,)
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def verify_chain(self) -> list[int]:
        """顺序重放哈希链，返回异常分录的 seq 列表（空列表 = 链完整）。"""
        broken: list[int] = []
        prev = ""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM entries ORDER BY seq").fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                digest = hashlib.sha256(
                    _canonical(row["seq"], row["entry_id"], row["entry_type"],
                               row["timestamp"], row["actor"],
                               json.dumps(payload, ensure_ascii=False, sort_keys=True),
                               row["idem_key"], row["order_id"], row["prev_hash"])
                ).hexdigest()
                if row["prev_hash"] != prev or digest != row["entry_hash"]:
                    broken.append(row["seq"])
                prev = row["entry_hash"]
        return broken

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> Entry:
        return Entry(
            seq=row["seq"],
            entry_id=row["entry_id"],
            entry_type=EntryType(row["entry_type"]),
            timestamp=row["timestamp"],
            actor=row["actor"],
            payload=json.loads(row["payload"]),
            idem_key=row["idem_key"],
            order_id=row["order_id"],
            prev_hash=row["prev_hash"],
            entry_hash=row["entry_hash"],
        )


class TransactionCtx:
    """``BEGIN IMMEDIATE`` 事务上下文：立即取写锁，并发下单串行落账。"""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")
        finally:
            self._lock.release()
