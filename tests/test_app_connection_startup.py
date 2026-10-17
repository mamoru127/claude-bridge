import importlib.util
import json
import signal
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "launchd/ensure-app-connection.py"
SPEC = importlib.util.spec_from_file_location("app_connection_startup", SCRIPT)
startup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(startup)
ENDPOINT = "ws+unix://localhost/tmp/codex.sock:/rpc"
ROWS = [["10", "1", str(startup.APP / "Contents/MacOS/ChatGPT")],
        ["11", "10", "/Applications/ChatGPT.app/Contents/Resources/codex"]]


class AppConnectionStartupTests(unittest.TestCase):
    def test_correct_connection_leaves_running_app_untouched(self):
        with patch.object(startup, "processes", return_value=ROWS), patch.object(
            startup, "has_connection", return_value=True
        ), patch.object(startup.subprocess, "run") as run, patch.object(startup.os, "kill") as kill:
            run.return_value.returncode = 0
            startup.ensure_connection(ENDPOINT, Path("/tmp/codex"))
        kill.assert_not_called()
        self.assertEqual(run.call_count, 2)

    def test_waits_for_active_turn_before_restarting_restored_app(self):
        with patch.object(startup, "processes", side_effect=[ROWS, ROWS, ROWS, [], ROWS]), patch.object(
            startup, "has_connection", side_effect=[False, False, True]
        ), patch.object(startup, "busy_threads", side_effect=[["active"], [], []]), patch.object(
            startup.time, "sleep"
        ) as sleep, patch.object(startup.os, "kill") as kill, patch.object(
            startup.subprocess, "run"
        ) as run, patch.object(startup, "verify_shared_transport") as verify:
            run.return_value.returncode = 0
            startup.ensure_connection(ENDPOINT, Path("/tmp/codex"))
        sleep.assert_called_once_with(5)
        kill.assert_called_once_with(10, signal.SIGTERM)
        self.assertIn(f"{startup.VARIABLE}={ENDPOINT}", run.call_args.args[0])
        self.assertIn("--env", run.call_args.args[0])
        verify.assert_called_once()

    def test_missing_app_is_launched_with_explicit_connection(self):
        with patch.object(startup, "processes", side_effect=[[], ROWS]), patch.object(
            startup, "has_connection", return_value=True
        ), patch.object(startup.os, "kill") as kill, patch.object(startup.subprocess, "run") as run, patch.object(
            startup, "verify_shared_transport"
        ):
            run.return_value.returncode = 0
            startup.ensure_connection(ENDPOINT, Path("/tmp/codex"))
        kill.assert_not_called()
        self.assertEqual(run.call_args.args[0][-1], f"{startup.VARIABLE}={ENDPOINT}")

    def test_does_not_force_quit_if_app_does_not_exit(self):
        with patch.object(startup, "processes", return_value=ROWS), patch.object(
            startup, "has_connection", return_value=False
        ), patch.object(startup, "busy_threads", return_value=[]), patch.object(
            startup.time, "sleep"
        ), patch.object(startup.os, "kill") as kill, patch.object(startup.subprocess, "run") as run:
            run.return_value.returncode = 0
            with self.assertRaisesRegex(RuntimeError, "did not exit"):
                startup.ensure_connection(ENDPOINT, Path("/tmp/codex"))
        kill.assert_called_once_with(10, signal.SIGTERM)
        self.assertEqual(run.call_count, 2)

    def test_active_and_partial_history_prevent_restart_but_completed_turn_releases_it(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            history = home / "rollout.jsonl"
            with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
                db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT)")
                db.execute("INSERT INTO threads VALUES (?, ?)", ("thread-1", str(history)))
            records = []
            for event, expected in [("task_started", ["thread-1"]), ("task_complete", [])]:
                records.append(json.dumps({"type": "event_msg", "payload": {"type": event}}) + "\n")
                history.write_text("".join(records))
                result = subprocess.CompletedProcess([], 0, "n/tmp/thread-1.lock\n", "")
                with patch.object(startup.subprocess, "run", return_value=result):
                    self.assertEqual(startup.busy_threads(ROWS, 10, home), expected)
            with history.open("a") as output:
                output.write('{"type":"event_msg"')
            with patch.object(startup.subprocess, "run", return_value=result):
                self.assertEqual(startup.busy_threads(ROWS, 10, home), ["thread-1"])
