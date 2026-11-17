import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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


class NativeRemoteTests(unittest.TestCase):
    def test_disables_only_shared_remote_persistently_without_stopping_turns(self):
        for status in ("connected", "connecting", "errored"):
            with self.subTest(status=status):
                client = Mock()
                client.call.side_effect = [{"status": status}, {"status": "disabled"}]
                self.assertTrue(setup.disable_shared_remote(client))
                self.assertEqual(client.call.call_args_list, [
                    unittest.mock.call("remoteControl/status/read", {}),
                    unittest.mock.call("remoteControl/disable", {}),
                ])

    def test_already_disabled_is_left_alone(self):
        client = Mock()
        client.call.return_value = {"status": "disabled"}
        self.assertFalse(setup.disable_shared_remote(client))
        client.call.assert_called_once_with("remoteControl/status/read", {})

    def test_native_setup_disables_shared_remote_and_closes_client(self):
        with patch("sys.argv", ["app_tools_setup", "--native"]), \
                patch.object(setup, "AppServerClient") as factory, \
                patch.object(setup, "configure_native_tools", return_value=False) as configure, \
                patch.object(setup, "disable_shared_remote") as disable, \
                patch("builtins.print"):
            setup.main()
        configure.assert_called_once()
        disable.assert_called_once_with(factory.return_value)
        factory.return_value.close.assert_called_once()


class NativeAppToolsTests(unittest.TestCase):
    def test_moves_permissions_without_reusing_legacy_launcher(self):
        home = Path("/tmp/codex-native-test")
        plugin_name = "codex-app-tools@openai-bundled"
        permissions = {"automation_update": {"approval_mode": "prompt"}}
        existing_override = {"tools": {"create_thread": {"approval_mode": "prompt"}}}
        unrelated = {"command": "/other-mcp", "enabled": True}
        config = {
            "mcp_servers": {"codex_app": {
                "command": "/legacy-launcher", "enabled": False, "tools": permissions,
                "default_tools_approval_mode": "approve",
            }, "other": unrelated},
            "plugins": {plugin_name: {"enabled": True, "mcp_servers": {"codex_app": existing_override}}},
        }
        client = Mock()
        client.call.return_value = {"layers": [{
            "name": {"type": "user", "file": str(home / "config.toml"), "profile": None},
            "version": "before", "config": config,
        }]}
        self.assertTrue(setup.configure_native_tools(client, home))
        write = client.call.call_args_list[1]
        self.assertEqual(write.args[0], "config/batchWrite")
        self.assertEqual(write.args[1]["expectedVersion"], "before")
        servers, plugin = write.args[1]["edits"]
        self.assertEqual(servers["value"], {"other": unrelated})
        self.assertEqual(plugin["keyPath"], f'plugins."{plugin_name}".mcp_servers.codex_app')
        self.assertEqual(plugin["value"], {
            "default_tools_approval_mode": "approve", "tools": {
                **permissions, **existing_override["tools"],
            },
        })
        self.assertEqual(client.call.call_args_list[-1].args[0], "config/mcpServer/reload")

    def test_native_setup_without_legacy_entry_is_idempotent(self):
        home = Path("/tmp/codex-native-test")
        client = Mock()
        client.call.return_value = {"layers": [{
            "name": {"type": "user", "file": str(home / "config.toml"), "profile": None},
            "version": "before", "config": {"mcp_servers": {}},
        }]}
        self.assertFalse(setup.configure_native_tools(client, home))
        client.call.assert_called_once_with("config/read", {"includeLayers": True})
