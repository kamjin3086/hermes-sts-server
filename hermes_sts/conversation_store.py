from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ConversationStore:
    """SQLite-backed conversation store.

    Follows the same persistent-connection + threading.Lock pattern as
    ``SqliteMemoryProvider`` in ``memory.py``.
    """

    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        self._closed = False
        path = Path(db_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_tables()

    # ------------------------------------------------------------------
    # Internal helpers (mirror memory.py pattern)
    # ------------------------------------------------------------------

    def _ensure_tables(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'active',
                    title TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    ended_at REAL,
                    ended_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    created_at REAL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                );
                CREATE INDEX IF NOT EXISTS idx_convmsg_conv_seq
                    ON conversation_messages(conversation_id, seq);
            """)
            self._conn.commit()

    def _db_fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.fetchall()

    def _db_fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.fetchone()

    def _db_execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection. Idempotent."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_conversation(self) -> str:
        """Create a new active conversation.

        If another active conversation already exists, it is archived with
        reason ``"superseded"`` before creating the new one.
        """
        conv_id = f"conv_{uuid.uuid4().hex}"
        now = time.time()
        active = self.get_active_conversation()
        if active is not None:
            self.archive_conversation(active["id"], "superseded")
        self._db_execute(
            "INSERT INTO conversations (id, status, created_at, updated_at) VALUES (?, 'active', ?, ?)",
            (conv_id, now, now),
        )
        return conv_id

    def get_active_conversation(self) -> dict | None:
        """Return the single active conversation, or ``None``."""
        row = self._db_fetchone(
            "SELECT c.id, c.title, c.created_at, c.updated_at, "
            "(SELECT COUNT(*) FROM conversation_messages m WHERE m.conversation_id = c.id) AS message_count "
            "FROM conversations c WHERE c.status = 'active' LIMIT 1"
        )
        return dict(row) if row is not None else None

    def append_message(
        self,
        conv_id: str,
        role: str,
        content: str,
        *,
        set_title_if_first: bool = False,
    ) -> None:
        """Append a message to a conversation.

        If *set_title_if_first* is ``True``, *role* is ``"user"``, and the
        conversation does not yet have a title, the first 30 characters of
        *content* are used as the title.
        """
        now = time.time()
        max_row = self._db_fetchone(
            "SELECT COALESCE(MAX(seq), 0) AS max_seq "
            "FROM conversation_messages WHERE conversation_id = ?",
            (conv_id,),
        )
        next_seq = (max_row["max_seq"] if max_row is not None else 0) + 1
        self._db_execute(
            "INSERT INTO conversation_messages (conversation_id, role, content, seq, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conv_id, role, content, next_seq, now),
        )
        self._db_execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conv_id),
        )
        if set_title_if_first and role == "user":
            conv = self.get_conversation(conv_id)
            if conv and conv.get("title") is None:
                self.update_title(conv_id, content[:30])

    def get_messages(self, conv_id: str, limit: int = 0) -> list[dict[str, Any]]:
        """Return messages for a conversation ordered by sequence.

        When *limit* is 0 (default) all messages are returned.  When *limit*
        is positive only the last *limit* messages are returned.
        """
        if limit > 0:
            rows = self._db_fetchall(
                "SELECT * FROM conversation_messages "
                "WHERE conversation_id = ? ORDER BY seq DESC LIMIT ?",
                (conv_id, limit),
            )
            rows = list(reversed(rows))
        else:
            rows = self._db_fetchall(
                "SELECT * FROM conversation_messages "
                "WHERE conversation_id = ? ORDER BY seq",
                (conv_id,),
            )
        return [dict(r) for r in rows]

    def archive_conversation(self, conv_id: str, ended_reason: str) -> None:
        """Mark a conversation as archived."""
        now = time.time()
        self._db_execute(
            "UPDATE conversations SET status = 'archived', ended_at = ?, ended_reason = ? WHERE id = ?",
            (now, ended_reason, conv_id),
        )

    def list_conversations(
        self, status: str | None = None, limit: int = 10, offset: int = 0
    ) -> list[dict[str, Any]]:
        """List conversations ordered by ``updated_at`` descending.

        Each item includes a ``message_count`` key.  When *status* is
        ``None``, both active and archived conversations are returned.
        """
        if status:
            rows = self._db_fetchall(
                "SELECT c.*, "
                "(SELECT COUNT(*) FROM conversation_messages m WHERE m.conversation_id = c.id) AS message_count "
                "FROM conversations c WHERE c.status = ? ORDER BY c.updated_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        else:
            rows = self._db_fetchall(
                "SELECT c.*, "
                "(SELECT COUNT(*) FROM conversation_messages m WHERE m.conversation_id = c.id) AS message_count "
                "FROM conversations c ORDER BY c.updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return [dict(r) for r in rows]

    def get_conversation(self, conv_id: str) -> dict[str, Any] | None:
        """Return a conversation by id, or ``None``."""
        row = self._db_fetchone(
            "SELECT * FROM conversations WHERE id = ?",
            (conv_id,),
        )
        return dict(row) if row is not None else None

    def update_title(self, conv_id: str, title: str) -> None:
        """Update the title of a conversation."""
        now = time.time()
        self._db_execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, conv_id),
        )

    def reload_history_into(self, conv_id: str, llm_provider: Any, max_messages: int = 0) -> None:
        """Load conversation messages into an LLM provider's history.

        Reads messages ordered by sequence.  When *max_messages* > 0 only the
        last *max_messages* messages are loaded.  Always resets
        ``last_llm_call_started_at`` to the current monotonic time.
        """
        rows = self._db_fetchall(
            "SELECT role, content FROM conversation_messages "
            "WHERE conversation_id = ? ORDER BY seq",
            (conv_id,),
        )
        if max_messages > 0:
            rows = rows[-max_messages:]
        llm_provider.history = [{"role": r["role"], "content": r["content"]} for r in rows]
        llm_provider.last_llm_call_started_at = time.monotonic()

    def maybe_archive_on_idle(self, idle_threshold_seconds: float) -> bool:
        """Archive the active conversation if it has been idle too long.

        Returns ``True`` if the conversation was archived, ``False``
        otherwise.  A threshold <= 0 disables idle archiving.
        """
        if idle_threshold_seconds <= 0:
            return False
        active = self.get_active_conversation()
        if active is None:
            return False
        elapsed = time.time() - active["updated_at"]
        if elapsed < idle_threshold_seconds:
            return False
        reason = f"idle_{int(elapsed)}s"
        self.archive_conversation(active["id"], reason)
        return True
