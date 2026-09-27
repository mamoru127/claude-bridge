import copy
import importlib.util
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from claude_bridge import app_tools_setup as setup
from claude_bridge.app_server import AppServerError


SPEC = importlib.util.spec_from_file_location("app_tools_launcher", setup.LAUNCHER)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class AppToolsSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.codex_home = self.home / "custom-codex"
        self.resources = self.home / "ChatGPT.app/Contents/Resources"
        self.plugin = self.resources / "plugins/openai-bundled/plugins/codex-app-tools"
        self.plugin.mkdir(parents=True)
        defaults = {"command": "./scripts/launch_codex_app_tools_mcp", "args": ["./server.mjs"],
                    "cwd": ".", "enabled": True, "env_vars": ["HOME"],
                    "tools": {"create_thread": {"approval_mode": "prompt"}}}
        (self.plugin / "desktop-mcp.json").write_text(json.dumps({"mcpServers": {"codex_app": defaults}}))
        (self.plugin / "server.mjs").touch()
        node = self.resources / "cua_node/bin/node"
        node.parent.mkdir(parents=True)
        node.touch()
        self.config = {"model_provider": "claude_bridge", "mcp_servers": {"other": {"url": "https://example.com"}}}
        self.server = {"command": "/old/python", "args": ["/old/launcher"],
                       "omit_tools_from": ["deferred", "direct"],
                       "tools": {"create_thread": {"approval_mode": "prompt"}},
                       "tool_timeout_sec": 123}
        self.config["mcp_servers"]["codex_app"] = self.server
        self.tools = dict.fromkeys(setup.REQUIRED_TOOLS, {})
        self.client = Mock()
        self.client.call.side_effect = self.call

    def call(self, method, params):
        if method == "config/read":
            return {"config": copy.deepcopy(self.config), "layers": [{
                "name": {"type": "user", "file": str(self.codex_home / "config.toml"), "profile": None},
                "version": "version-before-edit", "config": copy.deepcopy(self.config),
            }]}
        if method == "config/batchWrite":
            self.assertEqual(params["expectedVersion"], "version-before-edit")
            self.assertEqual(params["filePath"], str(self.codex_home / "config.toml"))
            self.assertEqual(len(params["edits"]), 1)
            edit = params["edits"][0]
            self.assertEqual(edit["keyPath"], "mcp_servers.codex_app")
            self.assertEqual(edit["mergeStrategy"], "replace")
            self.config["mcp_servers"]["codex_app"] = copy.deepcopy(edit["value"])
        elif method == "mcpServerStatus/list":
            return {"data": [{"name": "codex_app", "tools": self.tools}], "nextCursor": None}
        elif method != "config/mcpServer/reload":
            self.fail(f"Unexpected RPC: {method}")
        return {}

    def install(self):
        setup.install_tools(self.client, self.home, self.codex_home, self.resources)

    def test_install_repairs_transport_and_deferred_exposure_preserving_permissions(self):
        self.install()
        server = self.config["mcp_servers"]["codex_app"]
        installed = self.home / ".local/share/codex-shared-connection/launch-app-tools.py"
        self.assertEqual(installed.read_bytes(), setup.LAUNCHER.read_bytes())
        self.assertEqual(server["args"], [str(installed)])
        self.assertEqual(server["command"], "/usr/bin/python3")
        self.assertEqual(server["cwd"], str(self.plugin))
        self.assertEqual(server["omit_tools_from"], ["direct"])
        self.assertEqual(server["tools"], self.server["tools"])
        self.assertEqual(server["tool_timeout_sec"], 123)
        self.assertEqual(self.config["model_provider"], "claude_bridge")
        self.assertEqual(self.config["mcp_servers"]["other"], {"url": "https://example.com"})
        self.client.reset_mock()
        self.install()
        self.assertNotIn("config/batchWrite", [call.args[0] for call in self.client.call.call_args_list])

    def test_new_registration_uses_bundled_approval_defaults(self):
        del self.config["mcp_servers"]["codex_app"]
        self.install()
        self.assertEqual(self.config["mcp_servers"]["codex_app"]["tools"]["create_thread"], {"approval_mode": "prompt"})

    def test_missing_app_fails_before_mutation(self):
        (self.plugin / "server.mjs").unlink()
        with self.assertRaises(FileNotFoundError):
            self.install()
        self.client.call.assert_not_called()
        self.assertFalse((self.home / ".local").exists())

    def test_http_registration_is_not_overwritten(self):
        self.server["url"] = "https://example.com/mcp"
        with self.assertRaisesRegex(RuntimeError, "STDIO"):
            self.install()
        self.assertFalse((self.home / ".local").exists())

    def test_concurrent_config_edit_does_not_reload_or_claim_success(self):
        def reject_write(method, params):
            if method == "config/batchWrite":
                raise AppServerError("version mismatch")
            return self.call(method, params)
        self.client.call.side_effect = reject_write
        with self.assertRaisesRegex(AppServerError, "version mismatch"):
            self.install()
        self.assertNotIn("config/mcpServer/reload", [c.args[0] for c in self.client.call.call_args_list])

    def test_missing_tools_fails_after_reload(self):
        del self.tools["create_thread"]
        with self.assertRaisesRegex(RuntimeError, "create_thread"):
            self.install()
        self.client.call.assert_any_call("config/mcpServer/reload", {})

    def test_check_is_read_only_and_rejects_deferred_exclusion(self):
        with self.assertRaisesRegex(RuntimeError, "excluded"):
            setup.verify_tools(self.client)
        self.assertEqual([c.args[0] for c in self.client.call.call_args_list], ["config/read"])

    def test_explicit_tool_disabling_is_preserved_and_reported(self):
        for field, value in [("enabled_tools", []), ("disabled_tools", ["create_thread"]),
                             ("tools", {"create_thread": {"enabled": False}})]:
            with self.subTest(field=field):
                server = {field: value}
                self.client.call.return_value = {"config": {"mcp_servers": {"codex_app": server}}}
                self.client.call.side_effect = None
                with self.assertRaisesRegex(RuntimeError, "disabled"):
                    setup.verify_tools(self.client)

    def test_check_follows_server_pagination(self):
        self.client.call.side_effect = [
            {"config": {}}, {"data": [], "nextCursor": "next"},
            {"data": [{"name": "codex_app", "tools": self.tools}], "nextCursor": None},
        ]
        setup.verify_tools(self.client)
        self.assertEqual(self.client.call.call_args.args[1]["cursor"], "next")


class AppToolsLauncherTests(unittest.TestCase):
    def test_finds_live_listener_in_rotated_logs_and_skips_dead_sockets(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as listener:
            root = Path(directory)
            live = str(root / "live.sock")
            listener.bind(live)
            listener.listen(4)
            logs = root / "logs/2026/09/08"
            logs.mkdir(parents=True)
            old = logs / "app-t0-i1-old.log"
            old.write_text(f"dynamic_app_tools_listening pipePath={live}\n")
            new = logs / "app-t0-i1-new.log"
            new.write_text(f"dynamic_app_tools_listening pipePath={root}/missing.sock\n"
                           f"browser-use native pipe listening pipePath={live}\n")
            os.utime(old, (1, 1))
            os.utime(new, (2, 2))
            self.assertEqual(launcher.find_listener(root / "logs"), live)

    def test_no_listener_reports_required_app(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "/Applications/ChatGPT.app"):
                launcher.find_listener(Path(directory))

    def test_exec_uses_bundled_node_and_preserves_parent_process(self):
        with patch.object(launcher, "find_listener", return_value="/live.sock"), patch.object(launcher.os, "execve") as execute:
            launcher.main()
        path, args, env = execute.call_args.args
        self.assertEqual(path, str(launcher.RESOURCES / "cua_node/bin/node"))
        self.assertEqual(args[0], path)
        self.assertEqual(env["CODEX_APP_TOOLS_PIPE_PATH"], "/live.sock")
