"""Tests for ConversationStore persistence layer."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from hermes_sts.conversation_store import ConversationStore


class FakeLlm:
    """Minimal stand-in for an LLM provider with a writeable history list."""

    def __init__(self) -> None:
        self.history: list[dict] = []
        self.last_llm_call_started_at: float | None = None


class TestConversationStore(unittest.TestCase):
    def setUp(self) -> None:
        # Each test gets its own isolated temp directory + db file.
        self._tmp_dir = tempfile.mkdtemp(prefix="convstore_test_")
        self._stores: list[ConversationStore] = []

    def tearDown(self) -> None:
        for store in self._stores:
            store.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _new_store(self) -> ConversationStore:
        path = Path(self._tmp_dir) / f"store_{len(self._stores)}.sqlite3"
        store = ConversationStore(str(path))
        self._stores.append(store)
        return store

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_create_and_get_active(self) -> None:
        store = self._new_store()
        self.assertIsNone(store.get_active_conversation())

        first_id = store.create_conversation()
        self.assertTrue(first_id.startswith("conv_"))

        active = store.get_active_conversation()
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], first_id)
        self.assertEqual(active["message_count"], 0)
        self.assertEqual(store.get_conversation(first_id)["status"], "active")

        second_id = store.create_conversation()
        self.assertNotEqual(first_id, second_id)

        # The first conversation must have been archived (superseded).
        first = store.get_conversation(first_id)
        self.assertIsNotNone(first)
        self.assertEqual(first["status"], "archived")
        self.assertEqual(first["ended_reason"], "superseded")
        self.assertIsNotNone(first["ended_at"])

        # Only the new conversation is active now.
        active = store.get_active_conversation()
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], second_id)
        self.assertEqual(store.get_conversation(second_id)["status"], "active")

    def test_append_message_seq_increments(self) -> None:
        store = self._new_store()
        conv_id = store.create_conversation()

        before = store.get_conversation(conv_id)
        self.assertIsNotNone(before)
        updated_at_0 = before["updated_at"]

        # Force updated_at to advance deterministically.
        time.sleep(0.01)
        store.append_message(conv_id, "user", "hello")
        time.sleep(0.01)
        store.append_message(conv_id, "assistant", "hi there")
        time.sleep(0.01)
        store.append_message(conv_id, "user", "how are you?")

        msgs = store.get_messages(conv_id)
        self.assertEqual(len(msgs), 3)
        self.assertEqual([m["seq"] for m in msgs], [1, 2, 3])
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual([m["content"] for m in msgs], ["hello", "hi there", "how are you?"])

        after = store.get_conversation(conv_id)
        self.assertIsNotNone(after)
        self.assertGreater(after["updated_at"], updated_at_0)

    def test_archive_sets_ended(self) -> None:
        store = self._new_store()
        conv_id = store.create_conversation()

        conv = store.get_conversation(conv_id)
        self.assertIsNotNone(conv)
        self.assertIsNone(conv["ended_at"])
        self.assertIsNone(conv["ended_reason"])

        time.sleep(0.01)
        store.archive_conversation(conv_id, "test")

        conv = store.get_conversation(conv_id)
        self.assertIsNotNone(conv)
        self.assertEqual(conv["status"], "archived")
        self.assertIsNotNone(conv["ended_at"])
        self.assertGreater(conv["ended_at"], conv["created_at"])
        self.assertEqual(conv["ended_reason"], "test")

        # No active conversation remains.
        self.assertIsNone(store.get_active_conversation())

    def test_reload_history_into_overwrites(self) -> None:
        store = self._new_store()
        conv_id = store.create_conversation()
        store.append_message(conv_id, "user", "A1")
        store.append_message(conv_id, "assistant", "A2")
        store.append_message(conv_id, "user", "A3")

        llm = FakeLlm()
        llm.history = [{"role": "system", "content": "stale"}]
        llm.last_llm_call_started_at = None

        store.reload_history_into(conv_id, llm)

        self.assertEqual(
            llm.history,
            [
                {"role": "user", "content": "A1"},
                {"role": "assistant", "content": "A2"},
                {"role": "user", "content": "A3"},
            ],
        )
        self.assertIsNotNone(llm.last_llm_call_started_at)

        # Reload into an empty conversation overwrites history to empty.
        empty_conv_id = store.create_conversation()
        # Creating a new conversation archives the previous active one, so
        # empty_conv_id is now the active one with no messages.
        store.reload_history_into(empty_conv_id, llm)
        self.assertEqual(llm.history, [])

    def test_maybe_archive_on_idle_within_threshold_keeps_active(self) -> None:
        store = self._new_store()
        conv_id = store.create_conversation()
        store.append_message(conv_id, "user", "recent")

        # updated_at is essentially "now", so a large threshold keeps it active.
        archived = store.maybe_archive_on_idle(idle_threshold_seconds=3600)
        self.assertFalse(archived)

        conv = store.get_conversation(conv_id)
        self.assertIsNotNone(conv)
        self.assertEqual(conv["status"], "active")
        self.assertIsNone(conv["ended_at"])

        active = store.get_active_conversation()
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], conv_id)

    def test_maybe_archive_on_idle_over_threshold_archives(self) -> None:
        store = self._new_store()
        conv_id = store.create_conversation()
        store.append_message(conv_id, "user", "old")

        # Force the conversation's updated_at into the distant past so it is
        # definitely past any small threshold.
        with store._lock:
            store._conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (time.time() - 100000, conv_id),
            )
            store._conn.commit()

        archived = store.maybe_archive_on_idle(idle_threshold_seconds=1)
        self.assertTrue(archived)

        conv = store.get_conversation(conv_id)
        self.assertIsNotNone(conv)
        self.assertEqual(conv["status"], "archived")
        self.assertIsNotNone(conv["ended_at"])
        self.assertIn("idle", conv["ended_reason"])

        self.assertIsNone(store.get_active_conversation())

    def test_maybe_archive_on_idle_disabled_never_archives(self) -> None:
        store = self._new_store()
        conv_id = store.create_conversation()
        store.append_message(conv_id, "user", "stale")

        # Push updated_at far into the past.
        with store._lock:
            store._conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (time.time() - 100000, conv_id),
            )
            store._conn.commit()

        # threshold <= 0 disables idle archiving entirely.
        archived = store.maybe_archive_on_idle(idle_threshold_seconds=0)
        self.assertFalse(archived)

        archived = store.maybe_archive_on_idle(idle_threshold_seconds=-5)
        self.assertFalse(archived)

        conv = store.get_conversation(conv_id)
        self.assertIsNotNone(conv)
        self.assertEqual(conv["status"], "active")
        self.assertIsNone(conv["ended_at"])

    def test_concurrent_append_within_lock(self) -> None:
        store = self._new_store()
        conv_id = store.create_conversation()

        async def append_one(n: int) -> None:
            store.append_message(conv_id, "user", f"msg-{n}")

        async def run() -> None:
            await asyncio.gather(*[append_one(i) for i in range(5)])

        asyncio.run(run())

        msgs = store.get_messages(conv_id)
        seqs = [m["seq"] for m in msgs]
        self.assertEqual(seqs, [1, 2, 3, 4, 5])
        self.assertEqual(len(set(seqs)), len(seqs), "seq values must be unique")
        contents = sorted(m["content"] for m in msgs)
        self.assertEqual(contents, [f"msg-{i}" for i in range(5)])

    def test_wal_journal_mode_pragma(self) -> None:
        store = self._new_store()
        row = store._db_fetchone("PRAGMA journal_mode")
        self.assertIsNotNone(row)
        # PRAGMA journal_mode returns a single column named "journal_mode".
        mode = row[0] if row.keys() else row["journal_mode"]
        self.assertEqual(str(mode).lower(), "wal")


if __name__ == "__main__":
    unittest.main()