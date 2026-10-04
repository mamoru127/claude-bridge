"""作業中のスレッドへ指示が差し込まれたことを、app-server 自身の記録から見つける。

作業中のスレッドへの指示（turn/steer）は、app-server が次のモデル呼び出しへ差し込むまで
通知にも履歴にも現れない。iPhone のリモートからの指示はクラウド経由で app-server に直接
入るため、アプリとの間の中継も通らない。どの経路で届いた指示も、app-server は受理した
時点で自分の記録（CODEX_HOME の logs_N.sqlite）に、スレッド ID 付きで書き込む。
ここではその記録を読み取り専用で見張り、差し込みの行を見つけたら知らせる。

記録の文面は app-server の内部形式なので、Codex の更新で変わりうる。変わった場合は
見つからなくなるだけで、指示は従来どおり作業の後に届く。
"""

import glob
import logging
import os
import re
import sqlite3
import threading

logger = logging.getLogger("claude_bridge.steer_log")

POLL_SECONDS = 0.5
# 受理した指示を処理へ渡すときの行。差し込みは mode: Steer、新しいターンは別の mode になる。
_TARGET = "codex_core::session::handlers"
_TURN_INPUT = "%op: TurnInput%"
_STEER = "%mode: Steer%"
_LOG_FILE = re.compile(r"logs_(\d+)\.sqlite$")


def latest_log_database(codex_home: str) -> str | None:
    """いちばん新しい版の記録ファイル。版が上がると logs_N の N が増える。"""

    candidates = []
    for path in glob.glob(os.path.join(codex_home, "logs_*.sqlite")):
        match = _LOG_FILE.search(path)
        if match:
            candidates.append((int(match.group(1)), path))
    return max(candidates)[1] if candidates else None


class SteerLogWatcher:
    """記録に差し込みの行が増えたら、on_steer(thread_id, steered_at) を呼ぶ。"""

    def __init__(self, codex_home: str, on_steer, poll_seconds: float = POLL_SECONDS) -> None:
        self._codex_home = codex_home
        self._on_steer = on_steer
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._path = None
        self._connection = None
        self._cursor = None
        self._reported_error = False

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, name="steer-log", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            try:
                self.poll()
                self._reported_error = False
            except sqlite3.Error as error:
                # 書き込み中のロックや版の切り替え。次の回で開き直す。
                if not self._reported_error:
                    self._reported_error = True
                    logger.warning("app-server log unavailable: %s", error)
                self._close()

    def poll(self) -> int:
        """新しく増えた差し込みを知らせる。知らせた数を返す。"""

        path = latest_log_database(self._codex_home)
        if path is None:
            return 0
        if path != self._path:
            # 版が変わった。行番号は新しいファイルで振り直されるので、位置も読み直す。
            self._close()
            self._path, self._cursor = path, None
        if self._connection is None:
            self._connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        latest = self._connection.execute("SELECT max(id) FROM logs").fetchone()[0] or 0
        if self._cursor is None or latest < self._cursor:
            # 起動前の指示はもう Codex へ渡っている。今から後だけを見る。
            self._cursor = latest
            return 0
        rows = self._connection.execute(
            "SELECT thread_id, ts, ts_nanos FROM logs WHERE id > ? AND id <= ? AND target = ?"
            " AND feedback_log_body LIKE ? AND feedback_log_body LIKE ? ORDER BY id",
            (self._cursor, latest, _TARGET, _TURN_INPUT, _STEER),
        ).fetchall()
        self._cursor = latest
        for thread_id, seconds, nanos in rows:
            if thread_id:
                self._on_steer(thread_id, seconds + (nanos or 0) / 1e9)
        return len(rows)

    def _close(self) -> None:
        # 位置は残す。開き直した後、その間に増えた行も見落とさない。
        if self._connection is not None:
            self._connection.close()
        self._connection = None
