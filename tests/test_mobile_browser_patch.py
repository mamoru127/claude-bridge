import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from claude_bridge.mobile_browser_patch import (
    MobileThreadWatcher,
    inspect_session,
    open_in_chatgpt,
)


THREAD_ID = "01a02c76-328c-7c21-b5e4-b7466f62f05d"


def write_session(path: Path, originator: str, thread_id: str = THREAD_ID) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": thread_id, "originator": originator},
            }
        )
        + "\n",
        encoding="utf-8",
    )


class InspectSessionTests(unittest.TestCase):
    def test_recognizes_ios_and_android_remote_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            originators = (
                "codex_chatgpt_ios_remote",
                "codex_chatgpt_android_remote",
            )
            for index, originator in enumerate(originators):
                path = root / f"rollout-{index}.jsonl"
                write_session(path, originator)
                self.assertEqual(inspect_session(path), (True, THREAD_ID))

    def test_ignores_non_mobile_and_invalid_thread_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desktop = root / "rollout-desktop.jsonl"
            invalid = root / "rollout-invalid.jsonl"
            write_session(desktop, "codex_chatgpt_desktop")
            write_session(invalid, "codex_chatgpt_ios_remote", "not-a-uuid")
            self.assertEqual(inspect_session(desktop), (True, None))
            self.assertEqual(inspect_session(invalid), (True, None))

    def test_retries_an_empty_or_incomplete_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-new.jsonl"
            path.touch()
            self.assertEqual(inspect_session(path), (False, None))
            path.write_text("{", encoding="utf-8")
            self.assertEqual(inspect_session(path), (False, None))


class MobileThreadWatcherTests(unittest.TestCase):
    def test_opens_each_new_mobile_thread_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = MobileThreadWatcher(Path(directory), opener=Mock())
            day_directory = watcher._day_directory()
            day_directory.mkdir(parents=True)
            watcher.prime()

            path = day_directory / "rollout-mobile.jsonl"
            write_session(path, "codex_chatgpt_ios_remote")
            watcher.scan()
            watcher.scan()

            watcher.opener.assert_called_once_with(THREAD_ID)

    def test_retries_when_open_command_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            opener = Mock(side_effect=[subprocess.TimeoutExpired("open", 5), None])
            watcher = MobileThreadWatcher(Path(directory), opener=opener)
            day_directory = watcher._day_directory()
            day_directory.mkdir(parents=True)
            watcher.prime()

            path = day_directory / "rollout-mobile.jsonl"
            write_session(path, "codex_chatgpt_ios_remote")
            watcher.scan()
            watcher.scan()

            self.assertEqual(opener.call_count, 2)

    def test_retries_until_session_metadata_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = MobileThreadWatcher(Path(directory), opener=Mock())
            day_directory = watcher._day_directory()
            day_directory.mkdir(parents=True)
            watcher.prime()

            path = day_directory / "rollout-mobile.jsonl"
            path.touch()
            watcher.scan()
            watcher.opener.assert_not_called()
            write_session(path, "codex_chatgpt_android_remote")
            watcher.scan()

            watcher.opener.assert_called_once_with(THREAD_ID)


class OpenInChatGPTTests(unittest.TestCase):
    def test_uses_background_codex_thread_url(self) -> None:
        run = Mock()
        open_in_chatgpt(THREAD_ID, run=run)
        run.assert_called_once_with(
            ["/usr/bin/open", "-g", f"codex://threads/{THREAD_ID}"],
            check=True,
            timeout=5,
        )


if __name__ == "__main__":
    unittest.main()
