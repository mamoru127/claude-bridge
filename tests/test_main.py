"""Windows 起動時に共有 app-server が使えることを確認する。"""

import unittest
from unittest.mock import Mock, patch

from claude_bridge.__main__ import start_windows_app_server
from claude_bridge.config import BridgeConfig


class WindowsAppServerStartupTest(unittest.TestCase):
    def test_starts_and_checks_shared_daemon(self):
        config = BridgeConfig(codex_mcp=True, app_server_socket="C:/codex/app-server.sock")
        client = Mock()
        with (
            patch("claude_bridge.__main__.shutil.which", return_value="C:/bin/codex.cmd"),
            patch("claude_bridge.__main__.subprocess.run") as run,
            patch("claude_bridge.__main__.AppServerClient", return_value=client) as make_client,
        ):
            start_windows_app_server(config, platform="nt")
        run.assert_called_once_with(
            ["C:/bin/codex.cmd", "app-server", "daemon", "start"], check=True
        )
        make_client.assert_called_once_with("C:/codex/app-server.sock", timeout_seconds=10)
        client.call.assert_called_once_with("thread/list", {"limit": 1})
        client.close.assert_called_once()

    def test_no_daemon_needed_for_other_platform_or_disabled_mcp(self):
        with patch("claude_bridge.__main__.subprocess.run") as run:
            start_windows_app_server(BridgeConfig(codex_mcp=True), platform="posix")
            start_windows_app_server(BridgeConfig(codex_mcp=False), platform="nt")
        run.assert_not_called()

    def test_missing_codex_does_not_start_bridge(self):
        with patch("claude_bridge.__main__.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Codex CLI"):
                start_windows_app_server(BridgeConfig(codex_mcp=True), platform="nt")


if __name__ == "__main__":
    unittest.main()
