"""Windows 用 Codex proxy のバイト転送を確認する。"""

import io
import unittest
from unittest.mock import Mock, patch

from claude_bridge.app_server import AppServerClient
from claude_bridge.proxy_websocket import ProxySocket, ProxyWebSocket


class ProxySocketTest(unittest.TestCase):
    def test_recv_keeps_unread_bytes_in_order(self):
        transport = ProxySocket("codex.cmd")
        transport.chunks.put(b"abcdef")
        self.assertEqual(transport.recv(2), b"ab")
        self.assertEqual(transport.recv(4), b"cdef")

    def test_forwards_bytes_to_codex_proxy(self):
        process = Mock()
        process.stdin = io.BytesIO()
        process.stdout = Mock()
        process.stdout.read1.side_effect = [b"reply", b""]
        process.wait.return_value = 0
        with patch("claude_bridge.proxy_websocket.subprocess.Popen", return_value=process) as popen:
            transport = ProxySocket("codex.cmd")
            transport.connect("C:/codex/control.sock")
            transport.settimeout(1)
            transport.sendall(b"request")
            self.assertEqual(transport.recv(65536), b"reply")
            self.assertEqual(transport.recv(65536), b"")
            self.assertEqual(process.stdin.getvalue(), b"request")
            transport.close()

        popen.assert_called_once()
        self.assertEqual(
            popen.call_args.args[0],
            ["codex.cmd", "app-server", "proxy", "--sock", "C:/codex/control.sock"],
        )
        process.wait.assert_called()

    def test_uses_proxy_for_websocket(self):
        with (
            patch("claude_bridge.proxy_websocket.shutil.which", return_value="codex.cmd"),
            patch("claude_bridge.proxy_websocket.ProxySocket") as socket,
            patch("claude_bridge.proxy_websocket.UdsWebSocket") as websocket,
        ):
            ProxyWebSocket("C:/codex/control.sock")
        socket.assert_called_once_with("codex.cmd")
        websocket.assert_called_once_with(
            "C:/codex/control.sock", transport=socket.return_value
        )

    def test_windows_client_chooses_proxy(self):
        with patch("claude_bridge.app_server.os.name", "nt"):
            client = AppServerClient("C:/codex/control.sock")
        self.assertIs(client._connect_transport, ProxyWebSocket)


if __name__ == "__main__":
    unittest.main()
