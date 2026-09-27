"""会話ごとに CLI のセッションを対応づけるレジストリ。

Codex はスレッドごとに固定の `prompt_cache_key` を送る。これを CLI 側の会話 ID へ対応づけると、
2 回目以降は差分の発言だけを渡して続きから実行できる（Claude は `--resume`、Antigravity は
`--conversation`）。CLI 側の転記には読み込んだファイルの中身やツールの実行結果も残るため、
中断されたターンの続きから動く。
"""

import hashlib
import json
import select
import socket
import threading
import uuid
from collections import OrderedDict

from .responses import Message

# 保持する会話の数。超えた分は古い会話から捨て、次に来たときは新しいセッションを開始する。
DEFAULT_MAX_SESSIONS = 64


class Session:
    """1 会話ぶんの CLI セッション。実行中は 1 リクエストだけが排他で保持する。"""

    def __init__(self) -> None:
        # 今回の実行を前回の続きとして走らせるか。prepare が判定する。
        self.resume = False
        # 続きに使える識別子。Claude は転記 ID、Antigravity は CLI が発行する conversation_id。
        # 空なら続きにできない（まだ 1 度も実行を完了していない）。
        self.handle = ""
        self.lock = threading.Lock()
        # 直前に会話を確定させた CLI と、その CLI が把握済みの発言数・その範囲の指紋。
        self._backend = ""
        self._sent = 0
        self._digest = ""
        self._pending = (0, "", "")
        self._process_lock = threading.Lock()
        self._process = None
        # 今の実行の結果を待っている相手。take_over が生死を見るためだけに持つ。
        self._client = None
        # この会話の引き継ぎを予約した実行がいるか。引き継いだ側が借りるまでの間だけ真。
        self._handed_over = False

    def prepare(self, messages: tuple[Message, ...], backend: str) -> tuple[Message, ...]:
        """今回 CLI へ渡す発言を決める。

        渡した範囲と履歴の先頭が一致し、前回と同じ CLI なら続き。食い違う場合は Codex 側で
        履歴が置き換わっているか実行する CLI が変わっているため、会話を開始し直して全履歴を渡す。

        ここでは会話の状態を書き換えない。書き換えるのは confirm だけなので、起動に失敗した
        実行や別 CLI への切り替えに失敗した実行は、直前に確定していた会話をそのまま残す。
        """

        self.resume = (
            bool(self.handle)
            and self._backend == backend
            and self._digest == _digest(messages[: self._sent])
        )
        self._pending = (len(messages), _digest(messages), backend)
        return self._unseen(messages)

    def _unseen(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        """CLI がまだ受け取っていない発言。"""

        if not self.resume:
            return messages
        # assistant の発言は CLI 自身のもので転記済み。差分が空になる再送（中断後の再試行）
        # では、直近の発言をもう一度渡して続きを促す。
        return tuple(m for m in messages[self._sent :] if m.role == "user") or messages[-1:]

    def start(self, process) -> None:
        """CLI が起動した時点で呼ぶ。中断できるようプロセスを保持する。"""

        with self._process_lock:
            self._process = process

    def try_hold(self, client) -> bool:
        """空いていればこの会話を借りる。借りられたかを返す。

        借りるのと相手の登録は、take_over と同じロックの中でまとめて行う。分けると、その
        間に届いたリクエストが、相手のいない実行だと誤って判定して引き継ぎ待ちに入る。
        """

        with self._process_lock:
            if self._handed_over:
                return False
            if not self.lock.acquire(blocking=False):
                return False
            self._client = client
            self._handed_over = False
            return True

    def attach(self, client) -> None:
        """引き継いだ会話へ相手を登録する。ロックを取ってから呼ぶ。

        引き継ぎの予約（`_handed_over`）が済んでいる間は他のリクエストが割り込めないため、
        借りるのと同時でなくてよい。
        """

        with self._process_lock:
            self._client = client
            self._handed_over = False

    def confirm(self, handle: str) -> None:
        """この会話を続けられることが確定した時点で呼ぶ。渡した範囲も同時に確定する。

        確定する時点は CLI で違う。Claude は起動できれば転記が作られるので起動直後、
        Antigravity は実行が正常に終わり `conversation_id` を受け取ったとき。
        """

        self.handle = handle
        self._sent, self._digest, self._backend = self._pending

    def finish(self) -> None:
        """実行の終了。以降この会話を止める対象はない。"""

        with self._process_lock:
            self._process = None
            self._client = None

    def is_idle(self) -> bool:
        """引き継ぎ予約も実行もないときだけ、レジストリから破棄できる。"""

        with self._process_lock:
            return not self._handed_over and not self.lock.locked()

    def take_over(self) -> bool:
        """相手のいなくなった実行から会話を引き継ぐ。引き継げるかを返す。

        相手が繋がったままなら引き継がない。その結果を待っている相手がいるため、落とすと
        待っている側が中身のないエラーを受け取る。

        CLI がまだ起動していない実行も、相手が消えていれば対象になる。落とすものはないが、
        ロックが空くのを待てば会話を続けられる。ここで諦めると、本来は続きにできる再送が
        別の会話として枝分かれし、相手のいない先行 CLI もそのまま走り続ける。

        引き継げるのは 1 つの実行だけ。後片付けが終わるまでの間に届いた次のリクエストまで
        待ちに入ると、引き継いだ側のターンが終わるまで無音で待つことになる。
        """

        with self._process_lock:
            if self._handed_over or connected(self._client):
                return False
            if self._process is not None:
                self._process.kill()
                self._process = None
            self._handed_over = True
            return True


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
        self,
        token: str,
        name: str,
        arguments: dict,
        namespace: str | None = None,
        passthrough_type: str | None = None,
    ) -> str:
        """呼び出しを 1 件預かり、Codex と対応づける call_id を返す。"""

        call_id = f"call_{uuid.uuid4().hex}"
        call = {"call_id": call_id, "name": name, "arguments": arguments}
        if namespace:
            call["namespace"] = namespace
        if passthrough_type:
            call["passthrough_type"] = passthrough_type
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

    def acquire(self, key: str | None, client=None) -> Session:
        """この実行で使うセッションを排他で借りる。

        会話が空いていればそれを借りる。使用中の場合は、先行する実行の相手で分かれる。

        切断後の再送や割り込みでは、先行するリクエストの相手がすでにいない。その CLI を
        落とし、空いたところで会話を引き継ぐ。転記が残っているので続きから実行できる。

        相手が残っている実行は、同じ会話へ並行して届いた別のリクエスト（サイド会話など）の
        隣で動いている本物のターン。落とせば待っている相手が中身のないエラーを受け取り、
        終わるまで待てば、長いターンの裏で無音のまま待機上限を超える。どちらも取らず、
        その場限りのセッションで並行して実行する。並行して届いた側は履歴が枝分かれしていて
        どのみち転記を続けられないため、続きにできる相手を奪わない形で走らせる。

        会話を識別できない相手（キーなし）にも、その場限りのセッションを渡す。

        client は今回のリクエストの相手。次のリクエストが来たときの生死判定に使う。
        渡さない場合は、相手を特定できない実行としていつでも明け渡してよいものとして扱う。
        """

        if key is None:
            return _standalone(client)
        with self._lock:
            session = self._register(key)
            if session.try_hold(client):
                return session
            if not session.take_over():
                return _standalone(client)
        # 引き継ぐ実行が後片付けを終えるまでは会話が空かない。待つのはその間だけ。
        session.lock.acquire()
        session.attach(client)
        return session

    def _register(self, key: str) -> Session:
        # 呼び出し側が _lock を保持する。借用と引き継ぎ予約まで同じ排他区間で行う。
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
            idle = next((k for k, s in self._sessions.items() if s.is_idle()), None)
            if idle is None:
                return
            del self._sessions[idle]

    def release(self, session: Session) -> None:
        session.finish()
        session.lock.release()


def _standalone(client) -> Session:
    """その場限りのセッション。会話へ登録しないので、続きにも引き継ぎにも使われない。"""

    session = Session()
    session.try_hold(client)
    return session


def connected(client) -> bool:
    """相手がまだ繋がっているか。

    切断された接続は読める状態になり、読んでも中身が空になる。まだ送るものがない相手は
    読める状態にならないため、ここで待たずに判定できる。中身は覗くだけで取り出さない。
    """

    if client is None:
        return False
    try:
        readable, _, _ = select.select([client], [], [], 0)
        return not readable or bool(client.recv(1, socket.MSG_PEEK))
    except OSError:
        return False


def _digest(messages: tuple[Message, ...]) -> str:
    """発言列の指紋。同じ会話の続きかどうかの判定にだけ使う。"""

    payload = json.dumps(
        [[message.role, list(message.parts)] for message in messages],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
