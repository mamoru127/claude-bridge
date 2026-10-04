"""アプリと共有 app-server の間の中継。codex は起動せず、相手役のソケットで確かめる。"""

import json
import os
import queue
import shutil
import socket
import struct
import tempfile
import threading
import unittest

from claude_bridge.app_relay import AppRelay, FrameReader, strip_extensions
from claude_bridge.uds_websocket import UdsWebSocket

from tests.test_uds_websocket import FakeAppServer, encode_frame


def masked_frame(opcode: int, payload: bytes, final: bool = True) -> bytes:
    """アプリ側のフレーム。クライアントは必ずマスクする。"""

    mask = b"\x01\x02\x03\x04"
    frame = bytearray(encode_frame(opcode, payload, final))
    header_length = len(frame) - len(payload)
    frame[1] |= 0x80
    body = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return bytes(frame[:header_length]) + mask + body


class FrameReaderTest(unittest.TestCase):
    def test_reads_masked_text_split_across_chunks(self):
        data = masked_frame(0x1, b'{"id": 1}')
        reader = FrameReader()

        messages = [message for byte in data for message in reader.feed(bytes([byte]))]

        self.assertEqual(messages, [b'{"id": 1}'])

    def test_joins_fragments_and_skips_control_frames(self):
        reader = FrameReader()
        data = (
            encode_frame(0x1, b"ab", final=False)
            + encode_frame(0x9, b"ping")
            + encode_frame(0x0, b"cd")
        )

        self.assertEqual(reader.feed(data), [b"abcd"])

    def test_reads_extended_lengths(self):
        medium, large = b"m" * 300, b"l" * 70000
        reader = FrameReader()

        self.assertEqual(
            reader.feed(masked_frame(0x1, medium) + encode_frame(0x1, large)), [medium, large]
        )


class StripExtensionsTest(unittest.TestCase):
    def test_compression_offer_is_removed(self):
        head = (
            b"GET /rpc HTTP/1.1\r\nHost: localhost\r\n"
            b"Sec-WebSocket-Extensions: permessage-deflate\r\nUpgrade: websocket"
        )

        self.assertEqual(
            strip_extensions(head), b"GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket"
        )


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Python に AF_UNIX がない")
class AppRelayTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.upstream = FakeAppServer(directory)
        self.addCleanup(self.upstream.stop)
        self.notified = queue.Queue()
        self.relay = AppRelay(
            os.path.join(directory, "relay.sock"), self.upstream.path, self.notified.put
        )
        self.addCleanup(self.relay.close)
        threading.Thread(target=self.relay.serve_forever, daemon=True).start()
        self.app = UdsWebSocket(self.relay.listen_path)
        self.addCleanup(self.app.close)
        self.replies = queue.Queue()
        threading.Thread(target=self.read_replies, daemon=True).start()

    def read_replies(self):
        for text in self.app.messages():
            self.replies.put(json.loads(text))

    def request(self, message: dict) -> dict:
        """アプリから 1 件送り、app-server に届いた中身を返す。"""

        self.app.send(json.dumps(message))
        _, _, payload = self.upstream.received.get(timeout=5)
        return json.loads(payload)

    def reply(self, message: dict) -> dict:
        """app-server から 1 件返し、アプリに届いた中身を返す。"""

        self.upstream.send_text(json.dumps(message))
        return self.replies.get(timeout=5)

    def test_accepted_steer_is_reported_after_the_server_accepts_it(self):
        steer = {"id": 7, "method": "turn/steer", "params": {"threadId": "th-1", "input": []}}

        self.assertEqual(self.request(steer), steer)
        # 受理される前に区切ると、指示が差し込まれる前にターンが終わる。
        self.assertTrue(self.notified.empty())
        self.assertEqual(self.reply({"id": 7, "result": {"turnId": "t"}})["id"], 7)

        self.assertEqual(self.notified.get(timeout=5), "th-1")

    def test_queued_instruction_is_reported_after_the_server_accepts_it(self):
        queued = {"id": 8, "method": "thread/queue/add", "params": {"threadId": "th-2"}}

        self.request(queued)
        self.reply({"id": 8, "result": {}})

        self.assertEqual(self.notified.get(timeout=5), "th-2")

    def test_rejected_steer_is_not_reported(self):
        self.request({"id": "a", "method": "turn/steer", "params": {"threadId": "th-1"}})
        self.reply({"id": "a", "error": {"code": -32600, "message": "no active turn"}})

        # 次の応答まで流してから確かめる。中継は順番どおりに処理する。
        self.request({"id": "b", "method": "thread/list", "params": {}})
        self.reply({"id": "b", "result": {"data": []}})
        self.assertTrue(self.notified.empty())

    def test_other_traffic_passes_through_unchanged(self):
        notice = {"method": "item/agentMessage/delta", "params": {"threadId": "th-1"}}

        self.assertEqual(
            self.request({"id": 1, "method": "thread/read", "params": {"threadId": "th-1"}})[
                "method"
            ],
            "thread/read",
        )
        self.assertEqual(self.reply(notice), notice)
        self.assertEqual(self.reply({"id": 1, "result": {"thread": {}}})["id"], 1)
        self.assertTrue(self.notified.empty())


if __name__ == "__main__":
    unittest.main()
