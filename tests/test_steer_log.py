import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from claude_bridge.sessions import Session
from claude_bridge.steer_log import SteerLogWatcher, latest_log_database

STEER_BODY = (
    'session_loop{thread_id=th-1}: Submission sub=Submission { id: "s", op: TurnInput {'
    ' request: TurnInputRequest { input: UserInput { content: [Text { text: "x" }] } },'
    ' mode: Steer { expected_turn_id: "t" }, reply: Sender } }'
)
START_BODY = STEER_BODY.replace('mode: Steer { expected_turn_id: "t" }', "mode: StartIfIdle")
TARGET = "codex_core::session::handlers"


class SteerLogWatcherTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.database = self.create("logs_2.sqlite")
        self.steers = []
        self.watcher = SteerLogWatcher(self.home, lambda *steer: self.steers.append(steer))

    def create(self, name):
        path = os.path.join(self.home, name)
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, ts_nanos INTEGER,"
            " target TEXT, feedback_log_body TEXT, thread_id TEXT)"
        )
        connection.commit()
        self.addCleanup(connection.close)
        return connection

    def write(self, body, thread_id="th-1", target=TARGET, database=None, ts=100):
        database = database or self.database
        database.execute(
            "INSERT INTO logs (ts, ts_nanos, target, feedback_log_body, thread_id)"
            " VALUES (?, 500000000, ?, ?, ?)",
            (ts, target, body, thread_id),
        )
        database.commit()

    def test_reports_steers_written_after_it_starts(self):
        self.write(STEER_BODY, thread_id="before")
        self.watcher.poll()

        self.write(STEER_BODY)
        self.write(START_BODY, thread_id="start")
        self.write(STEER_BODY, thread_id="other-target", target="codex_core::other")

        self.assertEqual(self.watcher.poll(), 1)
        # 起動前の指示は Codex へ渡り済み。新しいターンや別の行は差し込みではない。
        self.assertEqual(self.steers, [("th-1", 100.5)])

    def test_switches_to_a_newer_log_version(self):
        self.watcher.poll()
        newer = self.create("logs_3.sqlite")
        self.assertEqual(latest_log_database(self.home), os.path.join(self.home, "logs_3.sqlite"))

        self.watcher.poll()
        self.write(STEER_BODY, database=newer)
        self.watcher.poll()

        self.assertEqual(self.steers, [("th-1", 100.5)])


class _Process:
    def __init__(self):
        self.killed = False

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class HandBackTimingTest(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.process = _Process()
        self.session.start(self.process)

    def test_run_started_before_the_instruction_is_handed_back(self):
        self.assertTrue(self.session.hand_back(time.time() + 1))
        self.assertTrue(self.session.handed_back())

    def test_run_started_after_the_instruction_keeps_going(self):
        # Codex がその指示を載せて送ってきた実行。止めると指示への応答まで切れる。
        self.assertFalse(self.session.hand_back(time.time() - 1))
        self.assertFalse(self.session.handed_back())


if __name__ == "__main__":
    unittest.main()
