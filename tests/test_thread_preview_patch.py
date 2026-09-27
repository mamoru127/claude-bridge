import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from claude_bridge.thread_preview_patch import ThreadPreviewWriter


class ThreadPreviewWriterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = sqlite3.connect(self.root / "state_5.sqlite")
        self.history = sqlite3.connect(self.root / "thread_history_1.sqlite")
        self.addCleanup(self.state.close)
        self.addCleanup(self.history.close)
        self.state.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, preview TEXT, "
            "first_user_message TEXT, thread_source TEXT, archived INTEGER)"
        )
        self.history.execute(
            "CREATE TABLE thread_items "
            "(thread_id TEXT, rollout_ordinal INTEGER, item_json TEXT)"
        )
        self.writer = ThreadPreviewWriter(self.root)
        self.addCleanup(self.writer.close)

    def add_task(self, thread_id, *, preview="", source="agent_created_thread", archived=0):
        self.state.execute(
            "INSERT INTO threads VALUES (?, ?, ?, '', ?, ?)",
            (thread_id, "表示名", preview, source, archived),
        )
        self.state.commit()

    def add_item(self, thread_id, output, *, tool="create_thread", ordinal=1):
        item = {
            "type": "functionCallOutput",
            "namespace": "codex_app",
            "name": tool,
            "output": output,
        }
        self.history.execute(
            "INSERT INTO thread_items VALUES (?, ?, ?)",
            (thread_id, ordinal, json.dumps(item)),
        )
        self.history.commit()

    def test_created_task_becomes_listable_without_changing_history_or_user_message(self):
        self.add_task("created")
        self.add_item(
            "created",
            "<codex_delegation><source_thread_id>parent</source_thread_id>"
            "<input>A &amp; B の &lt;input&gt; を修正\n続き</input></codex_delegation>",
        )
        original_history = self.history.execute("SELECT * FROM thread_items").fetchall()

        self.assertEqual(self.writer.scan(), ["created"])
        self.assertEqual(
            self.state.execute("SELECT name, preview, first_user_message FROM threads").fetchone(),
            ("表示名", "A & B の <input> を修正\n続き", ""),
        )
        self.assertEqual(
            self.state.execute("SELECT id FROM threads WHERE preview <> ''").fetchall(),
            [("created",)],
        )
        self.assertEqual(self.history.execute("SELECT * FROM thread_items").fetchall(), original_history)
        self.assertEqual(self.writer.scan(), [])

    def test_preserves_empty_unstarted_regular_archived_and_existing_tasks(self):
        for thread_id, settings in [
            ("empty", {}), ("unstarted", {}), ("regular", {"source": "user"}),
            ("archived", {"archived": 1}), ("existing", {"preview": "元の概要"}),
            ("other_tool", {}), ("later_output", {}),
        ]:
            self.add_task(thread_id, **settings)
        self.add_item("empty", "  ")
        for thread_id in ["regular", "archived", "existing"]:
            self.add_item(thread_id, "置き換えない")
        self.add_item("other_tool", "対象外", tool="read_thread")
        self.add_item("later_output", "対象外", tool="read_thread")
        self.add_item("later_output", "最初の依頼ではない", ordinal=2)
        original = self.state.execute("SELECT * FROM threads").fetchall()

        self.assertEqual(self.writer.scan(), [])
        self.assertEqual(self.state.execute("SELECT * FROM threads").fetchall(), original)

    def test_picks_up_creation_and_delayed_history_after_startup(self):
        self.assertEqual(self.writer.scan(), [])
        self.add_task("delayed")
        self.assertEqual(self.writer.scan(), [])
        self.add_item("delayed", "新しい依頼")
        self.assertEqual(self.writer.scan(), ["delayed"])

    def test_malformed_delegation_does_not_stop_later_previews(self):
        self.add_task("broken")
        self.add_item("broken", "<codex_delegation><input>途中")
        self.add_task("valid")
        self.add_item("valid", "有効な依頼")

        self.assertEqual(self.writer.scan(), ["valid"])
        self.assertEqual(self.writer.scan(), [])
        self.assertEqual(
            self.state.execute("SELECT preview FROM threads WHERE id = 'broken'").fetchone()[0],
            "",
        )

    def test_preserves_preview_set_while_reading_history(self):
        self.add_task("concurrent")
        self.add_item("concurrent", "初期依頼")

        def set_preview(statement):
            if statement.startswith("SELECT item_json"):
                self.state.execute("UPDATE threads SET preview = 'ユーザーの新しい発言'")
                self.state.commit()

        self.writer.history.set_trace_callback(set_preview)
        self.assertEqual(self.writer.scan(), [])
        self.assertEqual(
            self.state.execute("SELECT preview FROM threads").fetchone()[0],
            "ユーザーの新しい発言",
        )


if __name__ == "__main__":
    unittest.main()
