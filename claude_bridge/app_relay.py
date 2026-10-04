"""Codex アプリと共有 app-server の間に入り、作業中のスレッドへ届いた指示をブリッジへ知らせる。

アプリが作業中のスレッドへ送った指示（turn/steer）は、app-server が次のモデル呼び出しへ
差し込むまで、通知にも履歴にも現れない。Claude CLI はターン全体を 1 回の呼び出しで
走らせるため、そのままでは作業がすべて終わるまで届かない。

この中継はアプリからの接続を受け、そのまま共有 app-server へ繋ぐ。行き来するバイトは
変えずに流し、横で WebSocket のメッセージを読む。作業中のスレッドへの指示が受理されたら
ブリッジへ知らせ、ブリッジが Claude の作業をそこで区切る。Codex は区切られた応答の後、
指示を載せた次のリクエストを送ってくる。

ブリッジとは別のプロセスで動かす。ブリッジを入れ替えてもアプリの接続は切れず、
ブリッジが止まっている間も、知らせが届かないだけで中継は続く。
"""

import argparse
import json
import logging
import os
import socket
import sys
import threading
import urllib.request

from .config import DEFAULT_BRIDGE_URL, STEERED_PATH, default_app_server_socket

logger = logging.getLogger("claude_bridge.app_relay")

_RECV_CHUNK = 65536
_HEAD_END = b"\r\n\r\n"
_HEAD_LIMIT = 65536
_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
# 作業中のスレッドへ指示を差し込む要求。作業中でなければ app-server が新しいターンにする。
# 今のアプリは入力欄の指示を thread/queue/add でキューに積み、app-server がそれを
# 作業中のターンへ差し込む。
STEER_METHODS = ("turn/steer", "turn/start", "thread/queue/add")
NOTIFY_TIMEOUT_SECONDS = 5.0


class FrameReader:
    """WebSocket のフレーム列を少しずつ受け取り、完結したテキストメッセージを取り出す。"""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._opcode = None
        self._parts: list[bytes] = []
        self._reported_compression = False

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data
        messages = []
        while True:
            frame = self._next_frame()
            if frame is None:
                return messages
            final, opcode, payload = frame
            if opcode in (_OP_TEXT, _OP_BINARY):
                self._opcode, self._parts = opcode, [payload]
            elif opcode == _OP_CONTINUATION and self._opcode is not None:
                self._parts.append(payload)
            else:
                # ping・pong・close は読み飛ばす。中継は同じバイトをそのまま流している。
                continue
            if final:
                # JSON-RPC をバイナリ形式で流すクライアントもある。どちらも JSON として読む。
                messages.append(b"".join(self._parts))
                self._opcode, self._parts = None, []

    def _next_frame(self):
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        final = bool(buffer[0] & 0x80)
        if buffer[0] & 0x40 and not self._reported_compression:
            # 圧縮されたフレームは中身を読めない。気づけるよう一度だけ残す。
            self._reported_compression = True
            logger.info("compressed WebSocket frame seen; messages on this connection are unreadable")
        opcode = buffer[0] & 0x0F
        masked = bool(buffer[1] & 0x80)
        length = buffer[1] & 0x7F
        position = 2
        if length == 126:
            if len(buffer) < 4:
                return None
            length = int.from_bytes(buffer[2:4], "big")
            position = 4
        elif length == 127:
            if len(buffer) < 10:
                return None
            length = int.from_bytes(buffer[2:10], "big")
            position = 10
        mask = b""
        if masked:
            if len(buffer) < position + 4:
                return None
            mask = bytes(buffer[position : position + 4])
            position += 4
        if len(buffer) < position + length:
            return None
        payload = bytes(buffer[position : position + length])
        del buffer[: position + length]
        if masked and length:
            payload = _unmask(payload, mask)
        return final, opcode, payload


def _unmask(payload: bytes, mask: bytes) -> bytes:
    # 1 バイトずつ XOR すると大きなメッセージで遅い。整数 1 個として一度に XOR する。
    length = len(payload)
    key = (mask * (length // 4 + 1))[:length]
    value = int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")
    return value.to_bytes(length, "big")


def strip_extensions(head: bytes) -> bytes:
    """ハンドシェイクから拡張の申し出を除く。圧縮されると中身を読めない。"""

    lines = head.split(b"\r\n")
    kept = [line for line in lines if not line.lower().startswith(b"sec-websocket-extensions:")]
    return b"\r\n".join(kept)


class SteerWatch:
    """1 本の接続で、指示の要求と、それへの受理応答を突き合わせる。"""

    def __init__(self, notify) -> None:
        self._notify = notify
        self._pending: dict = {}
        self._reported_unreadable = False
        self._lock = threading.Lock()

    def from_app(self, text: bytes) -> None:
        message = _decode(text)
        if message is None:
            if not self._reported_unreadable:
                self._reported_unreadable = True
                logger.info("app message not JSON (%d bytes, starts %r)", len(text), text[:8])
            return
        method = message.get("method")
        logger.debug("app request method=%s", method)
        if method == "initialize":
            params = message.get("params")
            client = params.get("clientInfo") if isinstance(params, dict) else None
            logger.info("app client connected: %s",
                        client.get("name") if isinstance(client, dict) else None)
        if isinstance(method, str) and method.startswith(("turn/", "thread/queue/")):
            # アプリが指示の送り方を変えたときに気づけるよう、指示らしい要求は名前を残す。
            params = message.get("params")
            logger.info("app sent %s for thread %s", method,
                        params.get("threadId") if isinstance(params, dict) else None)
        if method not in STEER_METHODS:
            return
        params = message.get("params")
        thread_id = params.get("threadId") if isinstance(params, dict) else None
        request_id = message.get("id")
        if isinstance(thread_id, str) and isinstance(request_id, (str, int)):
            with self._lock:
                self._pending[_id_key(request_id)] = thread_id

    def from_server(self, text: bytes) -> None:
        # 待っている要求がなければ読まない。app-server からは大きな応答も流れてくる。
        with self._lock:
            if not self._pending:
                return
        message = _decode(text)
        if message is None or "method" in message or "id" not in message:
            return
        with self._lock:
            thread_id = self._pending.pop(_id_key(message["id"]), None)
        # 受理されてから区切る。先に区切ると、指示が差し込まれる前にターンが終わる。
        if thread_id is not None:
            logger.info("instruction for thread %s %s", thread_id,
                        "accepted" if "result" in message else "rejected")
        if thread_id is not None and "result" in message:
            self._notify(thread_id)


def _decode(text: bytes):
    try:
        message = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return message if isinstance(message, dict) else None


def _id_key(request_id) -> str:
    return json.dumps(request_id)


class BridgeNotifier:
    """ブリッジへ知らせる。届かなくても中継は止めない。"""

    def __init__(self, bridge_url: str, opener=urllib.request.urlopen) -> None:
        self._url = bridge_url.rstrip("/") + STEERED_PATH
        self._opener = opener

    def __call__(self, thread_id: str) -> None:
        threading.Thread(target=self._send, args=(thread_id,), daemon=True).start()

    def _send(self, thread_id: str) -> None:
        request = urllib.request.Request(
            self._url,
            data=json.dumps({"threadId": thread_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=NOTIFY_TIMEOUT_SECONDS) as response:
                response.read()
        except OSError as error:
            logger.warning("bridge notification failed: %s", error)


class AppRelay:
    """アプリからの接続を受け、共有 app-server へ中継する。"""

    def __init__(self, listen_path: str, upstream_path: str, notify) -> None:
        self._upstream_path = upstream_path
        self._notify = notify
        if os.path.exists(listen_path):
            # 前回の中継が残したソケット。繋がる相手はいないので作り直す。
            os.unlink(listen_path)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(listen_path)
        os.chmod(listen_path, 0o600)
        self._server.listen()
        self.listen_path = listen_path

    def serve_forever(self) -> None:
        while True:
            try:
                app, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._relay, args=(app,), daemon=True).start()

    def close(self) -> None:
        self._server.close()

    def _relay(self, app: socket.socket) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(self._upstream_path)
        except OSError as error:
            logger.warning("app-server unavailable: %s", error)
            app.close()
            upstream.close()
            return
        watch = SteerWatch(self._notify)
        server_side = threading.Thread(
            target=self._pump, args=(upstream, app, watch.from_server, False), daemon=True
        )
        server_side.start()
        self._pump(app, upstream, watch.from_app, True)
        server_side.join()

    def _pump(self, source, target, on_message, rewrite_head: bool) -> None:
        """source から target へ流す。最初の HTTP ヘッダーの後は WebSocket として読む。"""

        reader = FrameReader()
        head = b""
        in_head = True
        try:
            while True:
                chunk = source.recv(_RECV_CHUNK)
                if not chunk:
                    return
                if in_head:
                    head += chunk
                    if _HEAD_END not in head:
                        if len(head) > _HEAD_LIMIT:
                            return
                        continue
                    head, rest = head.split(_HEAD_END, 1)
                    if rewrite_head:
                        lines = head.split(b"\r\n")
                        names = [line.split(b":", 1)[0].decode("latin-1") for line in lines[1:]]
                        logger.info("app connection opened: %s headers=%s",
                                    lines[0][:80].decode("latin-1"), ",".join(names))
                        head = strip_extensions(head)
                    target.sendall(head + _HEAD_END + rest)
                    in_head = False
                    chunk = rest
                else:
                    target.sendall(chunk)
                for message in reader.feed(chunk):
                    on_message(message)
        except OSError:
            return
        finally:
            for side in (source, target):
                try:
                    side.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            source.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude_bridge.app_relay",
        description="Codex アプリと共有 app-server の間で、作業中のスレッドへの指示をブリッジへ知らせる",
    )
    parser.add_argument("--listen", required=True, help="アプリが繋ぐ UNIX ソケットのパス")
    parser.add_argument(
        "--upstream", default=default_app_server_socket(), help="共有 app-server の制御ソケット"
    )
    parser.add_argument(
        "--bridge-url",
        default=os.environ.get("CLAUDE_BRIDGE_URL") or DEFAULT_BRIDGE_URL,
        help="知らせを送るブリッジの URL",
    )
    args = parser.parse_args(argv)
    level = os.environ.get("CLAUDE_BRIDGE_RELAY_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    relay = AppRelay(args.listen, args.upstream, BridgeNotifier(args.bridge_url))
    logger.info("relaying %s -> %s", args.listen, args.upstream)
    relay.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
