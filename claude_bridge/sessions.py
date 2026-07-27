"""会話ごとに Claude CLI のセッションを対応づけるレジストリ。

Codex はスレッドごとに固定の `prompt_cache_key` を送る。これを Claude CLI のセッション ID へ
対応づけると、2 回目以降は差分の発言だけを渡して `--resume` で続けられる。CLI 側の転記には
読み込んだファイルの中身やツールの実行結果も残るため、中断されたターンの続きから動く。
"""

import hashlib
import json
import threading
import uuid
from collections import OrderedDict

from .responses import Message, build_content

# 保持する会話の数。超えた分は古い会話から捨て、次に来たときは新しいセッションを開始する。
DEFAULT_MAX_SESSIONS = 64


class Session:
    """1 会話ぶんの Claude セッション。実行中は 1 リクエストだけが排他で保持する。"""

    def __init__(self) -> None:
        self.session_id = str(uuid.uuid4())
        # 今回の実行を `--resume` で続けられるか。prepare が判定する。
        self.resume = False
        self.lock = threading.Lock()
        # CLI を起動した実績と、Claude が把握済みの発言数・その範囲の指紋。
        self._started = False
        self._sent = 0
        self._digest = ""
        self._pending = (0, "")
        self._process_lock = threading.Lock()
        self._process = None

    def prepare(self, messages: tuple[Message, ...]) -> tuple[dict, ...]:
        """今回 Claude へ渡す content を決める。

        渡した範囲と履歴の先頭が一致すれば続き。食い違う場合は Codex 側で履歴が置き換わって
        いるため、セッションを開始し直して全履歴を渡す。
        """

        self.resume = self._started and self._digest == _digest(messages[: self._sent])
        if self._started and not self.resume:
            self.session_id = str(uuid.uuid4())
            self._started = False
        self._pending = (len(messages), _digest(messages))
        return build_content(self._unseen(messages))

    def _unseen(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        """Claude がまだ受け取っていない発言。"""

        if not self.resume:
            return messages
        # assistant の発言は Claude 自身のもので転記済み。差分が空になる再送（中断後の再試行）
        # では、直近の発言をもう一度渡して続きを促す。
        return tuple(m for m in messages[self._sent :] if m.role == "user") or messages[-1:]

    def start(self, process) -> None:
        """CLI が起動した時点で呼ぶ。渡した範囲を確定し、中断できるようプロセスを保持する。"""

        self._started = True
        self._sent, self._digest = self._pending
        with self._process_lock:
            self._process = process

    def finish(self) -> None:
        """実行の終了。以降この会話を止める対象はない。"""

        with self._process_lock:
            self._process = None

    def cancel(self) -> None:
        """実行中のリクエストがあれば CLI を落とし、この会話を明け渡させる。"""

        with self._process_lock:
            if self._process is not None:
                self._process.kill()


class PendingCalls:
    """Claude が預けたツール呼び出しを、実行 1 回ぶんずつ保持する。

    Claude は MCP アダプタ（別プロセス）から預けるため、対応づけには実行ごとの token を使う。
    ターンが終わったら取り出して Codex へ渡し、その場で捨てる。
    """

    def __init__(self) -> None:
        self._calls: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def open(self) -> str:
        """この実行の預かり口を用意し、アダプタへ渡す token を返す。"""

        token = uuid.uuid4().hex
        with self._lock:
            self._calls[token] = []
        return token

    def add(
        self, token: str, name: str, arguments: dict, namespace: str | None = None
    ) -> str:
        """呼び出しを 1 件預かり、Codex と対応づける call_id を返す。"""

        call_id = f"call_{uuid.uuid4().hex}"
        call = {"call_id": call_id, "name": name, "arguments": arguments}
        if namespace:
            call["namespace"] = namespace
        with self._lock:
            if token not in self._calls:
                raise KeyError(token)
            self._calls[token].append(call)
        return call_id

    def close(self, token: str) -> list[dict]:
        """預かった呼び出しを取り出し、預かり口を閉じる。"""

        with self._lock:
            return self._calls.pop(token, [])


class SessionRegistry:
    """会話キーと Session の対応。同じ会話を同時に実行させない。"""

    def __init__(self, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.Lock()
        self._max_sessions = max_sessions

    def acquire(self, key: str | None) -> Session:
        """会話のセッションを排他で借りる。

        実行中のリクエストがあれば CLI を落として明け渡させる。切断後の再送や割り込みでは
        先行するリクエストの相手がすでにいないため、待たずに新しい方へ引き継ぐ。
        会話を識別できない相手（キーなし）には、その場限りのセッションを渡す。
        """

        session = Session() if key is None else self._register(key)
        session.cancel()
        session.lock.acquire()
        return session

    def _register(self, key: str) -> Session:
        with self._lock:
            # 今回の会話をいったん外してから空きを作る。捨てる対象に含めないため。
            session = self._sessions.pop(key, None) or Session()
            self._evict_idle()
            self._sessions[key] = session
        return session

    def _evict_idle(self) -> None:
        """上限に収まるよう古い会話から捨てる。

        実行中の会話は残す。捨てると次のリクエストが新しいセッションを作り、同じ会話を
        同時に走らせてしまう。実行中ばかりで空きを作れない場合は、上限を一時的に超える。
        """

        while len(self._sessions) >= self._max_sessions:
            idle = next((k for k, s in self._sessions.items() if not s.lock.locked()), None)
            if idle is None:
                return
            del self._sessions[idle]

    def release(self, session: Session) -> None:
        session.finish()
        session.lock.release()


def _digest(messages: tuple[Message, ...]) -> str:
    """発言列の指紋。同じ会話の続きかどうかの判定にだけ使う。"""

    payload = json.dumps(
        [[message.role, list(message.parts)] for message in messages],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
