"""Populate list previews for tasks started with codex_app.create_thread output.

Codex 0.153.4 does not project the initial tool output into threads.preview, while
thread/list excludes empty previews. Remove this local projection once app-server
handles that input itself. This uses state_5/thread_history_1's paginated schema;
schema changes require updating this module, not modifying conversation history.
This fixes the useStateDbOnly list path. JSONL-scanning lists still require an
app-server fix to recognize the initial tool output; do not fabricate user input.
"""

import json
import sqlite3
from pathlib import Path
from xml.etree import ElementTree


class ThreadPreviewWriter:
    def __init__(self, codex_home: Path) -> None:
        self.state = sqlite3.connect(
            f"{(codex_home / 'state_5.sqlite').as_uri()}?mode=rw",
            uri=True,
            timeout=1,
        )
        try:
            self.history = sqlite3.connect(
                f"{(codex_home / 'thread_history_1.sqlite').as_uri()}?mode=ro",
                uri=True,
                timeout=1,
            )
        except Exception:
            self.state.close()
            raise
        self.history.execute("PRAGMA query_only = ON")
        self.versions: tuple[int, int] | None = None

    def close(self) -> None:
        self.history.close()
        self.state.close()

    def scan(self) -> list[str]:
        versions = (
            self.state.execute("PRAGMA data_version").fetchone()[0],
            self.history.execute("PRAGMA data_version").fetchone()[0],
        )
        if versions == self.versions:
            return []
        candidates = self.state.execute(
            "SELECT id FROM threads WHERE thread_source = 'agent_created_thread' "
            "AND preview = '' AND first_user_message = '' AND archived = 0"
        ).fetchall()
        previews = []
        for (thread_id,) in candidates:
            row = self.history.execute(
                "SELECT item_json FROM thread_items WHERE thread_id = ? "
                "ORDER BY rollout_ordinal LIMIT 1",
                (thread_id,),
            ).fetchone()
            if row is None:
                continue
            item = json.loads(row[0])
            if (
                item.get("type") != "functionCallOutput"
                or item.get("namespace") != "codex_app"
                or item.get("name") != "create_thread"
            ):
                continue
            output = item.get("output")
            if not isinstance(output, str):
                continue
            preview = output.strip()
            if preview.startswith("<codex_delegation>"):
                try:
                    preview = ElementTree.fromstring(preview).findtext("input", "").strip()
                except ElementTree.ParseError:
                    continue
            if preview:
                previews.append((thread_id, preview))

        updated = []
        with self.state:
            for thread_id, preview in previews:
                result = self.state.execute(
                    "UPDATE threads SET preview = ? WHERE id = ? AND preview = '' "
                    "AND first_user_message = '' "
                    "AND thread_source = 'agent_created_thread' AND archived = 0",
                    (preview, thread_id),
                )
                if result.rowcount:
                    updated.append(thread_id)
        self.versions = versions
        return updated
