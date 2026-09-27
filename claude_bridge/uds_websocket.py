"""codex app-server の制御ソケット（UNIX ドメインソケット）へ WebSocket で繋ぐ。

app-server を `--listen unix://` で起動すると、届いた接続はまず WebSocket へ切り替えられ、
その上を JSON-RPC のテキストが流れる。生のバイトを書くと切断されるため、
ハンドシェイクとフレーム化がいる。必要な範囲だけを標準ライブラリで実装する。

1 本の接続を、送信側（呼び出しスレッド）と受信側（読み取りスレッド）が同時に使う。
送信だけロックで直列化する。受信は読み取りスレッド 1 本だけが行う。
"""

import base64
import os
import socket
import struct
import threading

# app-server は接続先のパスを見ないが、Codex アプリ本体と同じ経路名を使う。
HANDSHAKE_PATH = "/rpc"
HANDSHAKE_TIMEOUT_SECONDS = 10.0
_RECV_CHUNK = 65536

_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class UdsWebSocket:
    """制御ソケット 1 本ぶんの接続。"""

    def __init__(self, socket_path: str, transport=None) -> None:
        self._socket = transport or socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._send_lock = threading.Lock()
        self._buffer = b""
        try:
            self._socket.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
            self._socket.connect(socket_path)
            self._handshake()
            # 受け取りは待ち続ける。締め切りは呼び出し側が持っている。
            self._socket.settimeout(None)
        except OSError:
            self._socket.close()
            raise

    def send(self, text: str) -> None:
        """テキストを 1 メッセージとして送る。"""

        self._send_frame(_OP_TEXT, text.encode("utf-8"))

    def messages(self):
        """テキストメッセージを届いた順に返す。接続が閉じたら止まる。

        ping には pong を返す。返さないと相手から切られる。
        """

        opcode = _OP_CONTINUATION
        chunks: list[bytes] = []
        while True:
            try:
                final, frame_opcode, payload = self._read_frame()
            except OSError:
                return
            if frame_opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                continue
            if frame_opcode == _OP_PONG:
                continue
            if frame_opcode == _OP_CLOSE:
                return
            if frame_opcode != _OP_CONTINUATION:
                opcode = frame_opcode
                chunks = []
            chunks.append(payload)
            if not final:
                continue
            body = b"".join(chunks)
            chunks = []
            if opcode == _OP_TEXT:
                yield body.decode("utf-8")

    def close(self) -> None:
        """読み書きを止めてから閉じる。待っている読み取りスレッドをここで終わらせる。"""

        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            # 相手が先に閉じていれば shutdown は失敗する。閉じる作業は続ける。
            pass
        self._socket.close()

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {HANDSHAKE_PATH} HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._socket.sendall(request.encode("ascii"))
        while b"\r\n\r\n" not in self._buffer:
            self._buffer += self._read_some()
        head, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101" not in status:
            raise OSError(f"WebSocket へ切り替えられません: {status}")

    def _read_some(self) -> bytes:
        chunk = self._socket.recv(_RECV_CHUNK)
        if not chunk:
            raise OSError("codex app-server が接続を閉じました")
        return chunk

    def _need(self, size: int) -> None:
        while len(self._buffer) < size:
            self._buffer += self._read_some()

    def _read_frame(self) -> tuple[bool, int, bytes]:
        """フレーム 1 つを読む。返すのは (最終フレームか, opcode, 中身)。"""

        self._need(2)
        first, second = self._buffer[0], self._buffer[1]
        final = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        index = 2
        if length == 126:
            self._need(4)
            length = struct.unpack("!H", self._buffer[2:4])[0]
            index = 4
        elif length == 127:
            self._need(10)
            length = struct.unpack("!Q", self._buffer[2:10])[0]
            index = 10
        if second & 0x80:
            # サーバーはマスクしない決まり。来たら読み違えているので続けない。
            raise OSError("codex app-server がマスク付きフレームを送りました")
        self._need(index + length)
        payload = self._buffer[index : index + length]
        self._buffer = self._buffer[index + length :]
        return final, opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        # クライアントからのフレームはマスクが必須。
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        with self._send_lock:
            self._socket.sendall(bytes(header) + masked)
