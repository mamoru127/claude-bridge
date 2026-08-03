"""常駐している `codex app-server` と JSON-RPC で話すクライアント。

`--listen unix://` で待ち受けている 1 つの app-server へ、制御ソケット経由で繋ぐ。
自分では app-server を起動しない。Codex アプリも同じ app-server へ繋がせると、
ターンの実行中かどうかがアプリ側にも伝わる。スレッドの状態はそれを走らせている
プロセスのメモリにしかないため、別プロセスを立てるとアプリからは「未ロード」に見える。

このクライアントは常駐するブリッジ本体が持つ。MCP アダプタは起動と終了を繰り返すため、
アダプタが持つと再起動のたびに接続ごと待ち合わせが消える。

接続は 1 本だが、待たずに始めたターンの通知と、別の呼び出しの応答が混ざって届く。
そのため受け取るのは専用スレッド 1 本だけにして、id で応答を、threadId で通知を
待ち手へ振り分ける。応答待ちの間はロックを持たないので、長いターンが他の RPC を止めない。
"""

import json
import queue
import threading
import time

from .uds_websocket import UdsWebSocket

CLIENT_INFO = {"name": "claude-bridge", "version": "0.1.0"}
# thread/turns/list は experimentalApi を宣言しないと -32600 で拒否される。
CLIENT_CAPABILITIES = {"experimentalApi": True}
DEFAULT_TIMEOUT_SECONDS = 60.0
# ターンはモデルの実行そのものなので、通常の RPC よりずっと長く待つ。
DEFAULT_TURN_TIMEOUT_SECONDS = 900.0
TURN_METHOD = "turn/start"
TURN_DONE_METHODS = ("turn/completed", "turn/failed")
AGENT_MESSAGE_DELTA_METHOD = "item/agentMessage/delta"
CLOSED_MESSAGE = "codex app-server との接続が切れました"


class AppServerError(Exception):
    """app-server への接続・通信・RPC 応答に失敗した。"""


def _parse(text: str) -> dict:
    """受け取ったメッセージ 1 件を JSON オブジェクトとして読む。"""

    try:
        message = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AppServerError("codex app-server の出力が JSON ではありません") from error
    if not isinstance(message, dict):
        raise AppServerError("codex app-server の出力が JSON オブジェクトではありません")
    return message


class _Connection:
    """接続 1 本と、それに紐づく待ち合わせ口。

    繋ぎ直すと待ち合わせもすべてやり直しになるため、接続と同じ寿命で持つ。
    """

    def __init__(self, transport) -> None:
        self.transport = transport
        # 読み取りが終わった理由。設定されている接続は使い回さない。
        self.failure: str | None = None
        self._responses: dict[int, queue.Queue] = {}
        self._notices: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()

    def expect_response(self, request_id: int) -> queue.Queue:
        """この id の応答を受け取る箱を用意する。"""

        box: queue.Queue = queue.Queue()
        with self._lock:
            self._responses[request_id] = box
        return box

    def watch(self, thread_id: str | None) -> queue.Queue:
        """このスレッドの通知を受け取る箱を用意する。"""

        box: queue.Queue = queue.Queue()
        with self._lock:
            if thread_id in self._notices:
                raise AppServerError(f"このスレッドのターンはすでに待機中です: {thread_id}")
            self._notices[thread_id] = box
        return box

    def forget_response(self, request_id: int) -> None:
        """この id の応答を待つのをやめる。後から届いた応答は捨てられる。"""

        with self._lock:
            self._responses.pop(request_id, None)

    def unwatch(self, thread_id: str | None) -> None:
        with self._lock:
            self._notices.pop(thread_id, None)

    def route(self, message: dict) -> None:
        """応答は id の待ち手へ、通知は threadId の待ち手へ渡す。待ち手がなければ捨てる。

        待たずに始めたターンの通知は誰も待っていないため、ここで捨てないと溜まり続ける。
        """

        with self._lock:
            if message.get("method"):
                # サーバー起点の要求も method を持つ。threadId の待ち手がいなければ捨てる。
                box = self._notices.get((message.get("params") or {}).get("threadId"))
            else:
                box = self._responses.pop(message.get("id"), None)
        if box is not None:
            box.put(message)

    def fail(self, reason: str) -> None:
        """読み取りが終わったことを、待っている全員へ伝える。"""

        with self._lock:
            self.failure = reason
            boxes = [*self._responses.values(), *self._notices.values()]
            self._responses.clear()
        for box in boxes:
            box.put(None)


class AppServerClient:
    """常駐 `codex app-server` へ遅延接続し、複数の呼び出しを 1 本の接続に流す。"""

    def __init__(
        self,
        socket_path: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
        connect=UdsWebSocket,
    ) -> None:
        self._socket_path = socket_path
        self._timeout_seconds = timeout_seconds
        self._turn_timeout_seconds = turn_timeout_seconds
        self._connect_transport = connect
        self._conn: _Connection | None = None
        self._last_id = 0
        # 接続と送信の直列化だけに使う。応答待ちの間は持たない。
        # close をロック内から呼ぶので再入可能にする。
        self._lock = threading.RLock()

    def call(self, method: str, params: dict) -> object:
        """RPC を 1 件送り、結果を返す。未接続なら先に繋いでハンドシェイクする。"""

        with self._lock:
            conn = self._connect()
            pending = self._write(conn, method, params)
        return self._result(conn, pending, method)

    def run_turn(self, params: dict) -> dict:
        """turn/start を投げ、そのターンが終わるまで待って結果を返す。

        app-server は turn/start の応答を即返し、実行の経過と結果は通知で送る。
        最終メッセージを組み立てるため、turn/completed か turn/failed の通知まで読み続ける。
        """

        thread_id = params.get("threadId")
        with self._lock:
            conn = self._connect()
            # 受理応答より先に通知が届くため、送る前に受け口を用意する。
            notices = conn.watch(thread_id)
            pending = self._write(conn, TURN_METHOD, params)
        try:
            self._result(conn, pending, TURN_METHOD)
            return self._collect(conn, notices)
        finally:
            conn.unwatch(thread_id)

    def start_turn(self, params: dict) -> dict:
        """turn/start を投げ、受理応答だけ読んで返す。完了は待たない。

        ターンは app-server 側で走り続け、経過と結果はセッション履歴へ書かれる。
        app-server は常駐しているので、ブリッジを入れ替えてもターンは完走する。
        """

        with self._lock:
            conn = self._connect()
            pending = self._write(conn, TURN_METHOD, params)
        result = self._result(conn, pending, TURN_METHOD)
        turn = result.get("turn") if isinstance(result, dict) else None
        return {"threadId": params.get("threadId"), "turn": turn}

    def close(self) -> None:
        """接続を閉じ、未接続状態へ戻す。app-server 自体は常駐したままにする。"""

        with self._lock:
            conn, self._conn = self._conn, None
        if conn is None:
            return
        conn.transport.close()

    def _connect(self) -> _Connection:
        """生きている接続を返す。無ければ制御ソケットへ繋いでハンドシェイクする。"""

        conn = self._conn
        if conn is not None and conn.failure is None:
            return conn
        # 読み取りが終わった接続は使い回さない。close が切断と状態リセットを行う。
        self.close()
        try:
            transport = self._connect_transport(self._socket_path)
        except OSError as error:
            raise AppServerError(
                f"codex app-server へ接続できません: {self._socket_path}: {error}"
            )
        conn = _Connection(transport)
        self._conn = conn
        threading.Thread(target=self._pump, args=(conn,), daemon=True).start()
        try:
            pending = self._write(
                conn,
                "initialize",
                {"clientInfo": CLIENT_INFO, "capabilities": CLIENT_CAPABILITIES},
            )
            self._result(conn, pending, "initialize")
            self._send(conn, {"jsonrpc": "2.0", "method": "initialized"})
        except AppServerError:
            # ハンドシェイクに失敗した接続は使えない。守るべきターンもまだない。
            self._close_if_current(conn)
            raise
        return conn

    def _pump(self, conn: _Connection) -> None:
        """受け取りを 1 スレッドだけで行い、応答と通知を待ち手へ振り分ける。"""

        reason = CLOSED_MESSAGE
        try:
            for text in conn.transport.messages():
                conn.route(_parse(text))
        except AppServerError as error:
            reason = str(error)
        conn.fail(reason)

    def _write(self, conn: _Connection, method: str, params: dict) -> tuple[int, queue.Queue]:
        """要求を 1 件送り、その id と応答を受け取る箱を返す。呼び出し側がロックを持つ。"""

        self._last_id += 1
        request_id = self._last_id
        box = conn.expect_response(request_id)
        self._send(conn, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return request_id, box

    def _result(self, conn: _Connection, pending: tuple[int, queue.Queue], method: str) -> object:
        """応答 1 件を待つ。エラー応答は AppServerError にするが、接続は保つ。

        RPC のエラー応答は通信の失敗ではない。ここで接続を切ると、
        進捗を見るための 1 回の失敗で、走っているターンが全部止まる。
        """

        request_id, box = pending
        try:
            message = self._take(conn, box, self._timeout_seconds)
        finally:
            # 時間切れで待つのをやめた要求を、接続に残したままにしない。
            conn.forget_response(request_id)
        if "error" in message:
            raise AppServerError(f"{method} が失敗しました: {message['error']}")
        return message.get("result")

    def _collect(self, conn: _Connection, notices: queue.Queue) -> dict:
        """完了通知まで通知を読み続け、途中の本文を繋げて返す。"""

        deadline = time.monotonic() + self._turn_timeout_seconds
        deltas: list[str] = []
        while True:
            message = self._take(conn, notices, self._turn_timeout_seconds, deadline)
            payload = message.get("params") or {}
            method = message.get("method")
            if method == AGENT_MESSAGE_DELTA_METHOD:
                deltas.append(payload.get("delta") or "")
            elif method in TURN_DONE_METHODS:
                return {
                    "threadId": payload.get("threadId"),
                    "turn": payload.get("turn"),
                    "agentMessage": "".join(deltas),
                    "failed": method == "turn/failed",
                }

    def _take(
        self,
        conn: _Connection,
        box: queue.Queue,
        timeout_seconds: float,
        deadline: float | None = None,
    ) -> dict:
        """箱から次の 1 件を取る。時間切れと読み取り終了はどちらも AppServerError。"""

        if deadline is None:
            deadline = time.monotonic() + timeout_seconds
        remaining = deadline - time.monotonic()
        message = None
        if remaining > 0:
            try:
                message = box.get(timeout=remaining)
            except queue.Empty:
                message = None
        if message is not None:
            return message
        if conn.failure is not None:
            # 読み取りが終わった接続は誰も使えない。捨てて次の呼び出しで作り直す。
            self._close_if_current(conn)
            raise AppServerError(conn.failure)
        # 時間切れは呼び出し 1 件の失敗。接続を保ち、走っているターンは続けさせる。
        raise AppServerError(f"codex app-server が {timeout_seconds} 秒以内に応答しません")

    def _close_if_current(self, conn: _Connection) -> None:
        """壊れた接続を次の呼び出しへ持ち越さない。すでに作り直されていれば触らない。"""

        with self._lock:
            if self._conn is conn:
                self.close()

    def _send(self, conn: _Connection, message: dict) -> None:
        try:
            conn.transport.send(json.dumps(message))
        except OSError as error:
            self._close_if_current(conn)
            raise AppServerError(f"codex app-server へ書き込めません: {error}") from error
