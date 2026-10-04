import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from claude_bridge import app_tools_setup as setup
from claude_bridge.app_server import AppServerError


class DisableAppToolsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.codex_home = Path(self.directory.name) / "custom-codex"
        self.server = {"command": "/usr/bin/python3", "args": ["/launcher"], "enabled": True,
                       "tools": {"create_thread": {"approval_mode": "prompt"}}}
        self.config = {"model_provider": "claude_bridge", "mcp_servers": {"codex_app": self.server}}
        self.client = Mock()
        self.client.call.side_effect = self.call

    def call(self, method, params):
        if method == "config/read":
            return {"layers": [{
                "name": {"type": "user", "file": str(self.codex_home / "config.toml"), "profile": None},
                "version": "version-before-edit", "config": copy.deepcopy(self.config),
            }]}
        if method == "config/batchWrite":
            self.assertEqual(params["expectedVersion"], "version-before-edit")
            self.assertEqual(params["filePath"], str(self.codex_home / "config.toml"))
            (edit,) = params["edits"]
            self.assertEqual(edit["keyPath"], "mcp_servers.codex_app.enabled")
            self.config["mcp_servers"]["codex_app"]["enabled"] = edit["value"]
        elif method != "config/mcpServer/reload":
            self.fail(f"Unexpected RPC: {method}")
        return {}

    def methods(self):
        return [call.args[0] for call in self.client.call.call_args_list]

    def test_disables_enabled_entry_keeping_permissions_and_reloads(self):
        self.assertTrue(setup.disable_tools(self.client, self.codex_home))
        server = self.config["mcp_servers"]["codex_app"]
        self.assertIs(server["enabled"], False)
        self.assertEqual(server["tools"], {"create_thread": {"approval_mode": "prompt"}})
        self.assertEqual(self.methods()[-1], "config/mcpServer/reload")

    def test_already_disabled_or_missing_entry_is_left_alone(self):
        for config in [{"mcp_servers": {"codex_app": {"enabled": False}}}, {"mcp_servers": {}}]:
            with self.subTest(config=config):
                self.config = config
                self.client.reset_mock()
                self.assertFalse(setup.disable_tools(self.client, self.codex_home))
                self.assertEqual(self.methods(), ["config/read"])

    def test_concurrent_config_edit_does_not_reload(self):
        def reject_write(method, params):
            if method == "config/batchWrite":
                raise AppServerError("version mismatch")
            return self.call(method, params)
        self.client.call.side_effect = reject_write
        with self.assertRaisesRegex(AppServerError, "version mismatch"):
            setup.disable_tools(self.client, self.codex_home)
        self.assertNotIn("config/mcpServer/reload", self.methods())
