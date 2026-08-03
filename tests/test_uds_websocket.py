"""制御ソケット用 WebSocket クライアントのテスト。

codex は起動しない。UNIX ソケットで待ち受ける相手を用意し、フレームを手で組んで確かめる。
"""

import os
import queue
import shutil
import socket
import struct
import tempfile
import threading
import unittest

from claude_bridge.uds_websocket import UdsWebSocket

_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


def encode_frame(opcode: int, payload: bytes, final: bool = True) -> bytes:
    """サーバー側のフレームを組む。サーバーはマスクしない。"""

    header = bytearray([(0x80 if final else 0x00) | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 1 << 16:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + payload


class FakeAppServer:
    """制御ソケットの相手役。ハンドシェイクに答え、届いたフレームを解いて記録する。"""

    def __init__(self, directory: str, status: str = "101 Switching Protocols") -> None:
        self.path = os.path.join(directory, "as.sock")
        self.status = status
        self.received: queue.Queue = queue.Queue()
        self.request_line: str | None = None
        self._connected = threading.Event()
        self._conn: socket.socket | None = None
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.path)
        self._listener.listen(1)
        threading.Thread(target=self._serve, daemon=True).start()

    def send_frame(self, opcode: int, payload: bytes, final: bool = True) -> None:
        self._connected.wait(timeout=5)
        assert self._conn is not None
        self._conn.sendall(encode_frame(opcode, payload, final))

    def send_text(self, text: str) -> None:
        self.send_frame(_OP_TEXT, text.encode("utf-8"))

    def stop(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._listener.close()

    def _serve(self) -> None:
        try:
            self._accept_and_read()
        except OSError:
            # stop() で閉じた後の読み取り。後片付けなので黙って終わる。
            pass

    def _accept_and_read(self) -> None:
        conn, _ = self._listener.accept()
        self._conn = conn
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buffer += chunk
        head, buffer = buffer.split(b"\r\n\r\n", 1)
        self.request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        conn.sendall(
            f"HTTP/1.1 {self.status}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n".encode()
        )
        self._connected.set()
        self._read_frames(conn, buffer)

    def _read_frames(self, conn: socket.socket, buffer: bytes) -> None:
        while True:
            while len(buffer) < 2:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer += chunk
            first, second = buffer[0], buffer[1]
            opcode = first & 0x0F
            length = second & 0x7F
            index = 2
            if length == 126:
                length = struct.unpack("!H", buffer[2:4])[0]
                index = 4
            elif length == 127:
                length = struct.unpack("!Q", buffer[2:10])[0]
                index = 10
            masked = bool(second & 0x80)
            need = index + (4 if masked else 0) + length
            while len(buffer) < need:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer += chunk
            if masked:
                mask = buffer[index : index + 4]
                index += 4
                raw = buffer[index : index + length]
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(raw))
            else:
                payload = buffer[index : index + length]
            buffer = buffer[index + length :]
            self.received.put((opcode, masked, payload))


class UdsWebSocketTest(unittest.TestCase):
    def setUp(self) -> None:
        # ソケットのパスには長さの上限がある。TMPDIR が長い環境を避ける。
        self.directory = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def read_in_background(self, client) -> queue.Queue:
        """受け取ったメッセージを箱へ入れ続ける。送信で詰まらせないために使う。"""

        received: queue.Queue = queue.Queue()
        threading.Thread(
            target=lambda: [received.put(message) for message in client.messages()],
            daemon=True,
        ).start()
        return received

    def test_handshake_requests_an_upgrade(self):
        server = FakeAppServer(self.directory)
        self.addCleanup(server.stop)

        client = UdsWebSocket(server.path)
        self.addCleanup(client.close)

        client.send("ping-me")
        opcode, masked, payload = server.received.get(timeout=5)
        self.assertEqual(server.request_line, "GET /rpc HTTP/1.1")
        self.assertEqual(opcode, _OP_TEXT)
        # クライアントから送るフレームはマスクが必須。
        self.assertTrue(masked)
        self.assertEqual(payload.decode("utf-8"), "ping-me")

    def test_rejects_a_response_that_is_not_an_upgrade(self):
        server = FakeAppServer(self.directory, status="500 Internal Server Error")
        self.addCleanup(server.stop)

        with self.assertRaises(OSError) as raised:
            UdsWebSocket(server.path)

        self.assertIn("500", str(raised.exception))

    def test_missing_socket_is_an_error(self):
        with self.assertRaises(OSError):
            UdsWebSocket(os.path.join(self.directory, "absent.sock"))

    def test_receives_text_messages_in_order(self):
        server = FakeAppServer(self.directory)
        self.addCleanup(server.stop)
        client = UdsWebSocket(server.path)
        self.addCleanup(client.close)

        server.send_text('{"id":1}')
        server.send_text('{"id":2}')

        messages = client.messages()
        self.assertEqual(next(messages), '{"id":1}')
        self.assertEqual(next(messages), '{"id":2}')

    def test_joins_continuation_frames(self):
        server = FakeAppServer(self.directory)
        self.addCleanup(server.stop)
        client = UdsWebSocket(server.path)
        self.addCleanup(client.close)

        server.send_frame(_OP_TEXT, b'{"id":', final=False)
        server.send_frame(0x0, b"3}", final=True)

        self.assertEqual(next(client.messages()), '{"id":3}')

    def test_handles_payloads_longer_than_a_short_header(self):
        server = FakeAppServer(self.directory)
        self.addCleanup(server.stop)
        client = UdsWebSocket(server.path)
        self.addCleanup(client.close)
        long_text = "x" * 70000
        # 送信バッファに収まらない大きさなので、先に読み手を動かしておく。
        received = self.read_in_background(client)

        client.send(long_text)
        server.send_text(long_text)

        _, _, payload = server.received.get(timeout=5)
        self.assertEqual(payload.decode("utf-8"), long_text)
        self.assertEqual(received.get(timeout=5), long_text)

    def test_answers_ping_with_pong(self):
        server = FakeAppServer(self.directory)
        self.addCleanup(server.stop)
        client = UdsWebSocket(server.path)
        self.addCleanup(client.close)
        received = self.read_in_background(client)

        server.send_frame(_OP_PING, b"keep-alive")
        server.send_text("after-ping")

        opcode, _, payload = server.received.get(timeout=5)
        self.assertEqual((opcode, payload), (_OP_PONG, b"keep-alive"))
        # ping で読み取りが止まらないこと。
        self.assertEqual(received.get(timeout=5), "after-ping")

    def test_close_frame_ends_the_stream(self):
        server = FakeAppServer(self.directory)
        self.addCleanup(server.stop)
        client = UdsWebSocket(server.path)
        self.addCleanup(client.close)

        server.send_frame(_OP_CLOSE, b"")

        self.assertEqual(list(client.messages()), [])

    def test_close_ends_a_waiting_reader(self):
        server = FakeAppServer(self.directory)
        self.addCleanup(server.stop)
        client = UdsWebSocket(server.path)
        done = threading.Event()
        threading.Thread(
            target=lambda: (list(client.messages()), done.set()), daemon=True
        ).start()

        client.close()

        self.assertTrue(done.wait(timeout=5))

    def test_disconnect_ends_the_stream(self):
        server = FakeAppServer(self.directory)
        client = UdsWebSocket(server.path)
        self.addCleanup(client.close)

        server.stop()

        self.assertEqual(list(client.messages()), [])


if __name__ == "__main__":
    unittest.main()
