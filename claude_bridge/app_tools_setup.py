"""Configure desktop app tools for the selected app-server connection.

ChatGPT only accepts app tools connections whose parent or grandparent process is
the app itself (`dynamic_app_tools_peer_rejected reason=untrusted-process-ancestry`).
The shared app-server is started by launchd, so its explicit codex_app entry is
disabled in shared mode. In native mode, move that entry's tool permissions to
the bundled plugin, which ChatGPT enables on its own app-server. Thread creation
and messaging can also use the bridge's codex_app_server MCP.
"""

import argparse
import os
from pathlib import Path

from .app_server import AppServerClient


def disable_tools(client, codex_home: Path) -> bool:
    config_path = str(codex_home / "config.toml")
    layers = client.call("config/read", {"includeLayers": True})["layers"]
    layer = next(layer for layer in layers if layer["name"] == {
        "type": "user", "file": config_path, "profile": None,
    })
    server = layer["config"].get("mcp_servers", {}).get("codex_app")
    if server is None or server.get("enabled") is False:
        return False
    client.call("config/batchWrite", {
        "filePath": config_path, "expectedVersion": layer["version"],
        "edits": [{"keyPath": "mcp_servers.codex_app.enabled", "value": False, "mergeStrategy": "replace"}],
    })
    client.call("config/mcpServer/reload", {})
    return True


def configure_native_tools(client, codex_home: Path) -> bool:
    """Move legacy app-tool permissions to the app-managed bundled plugin.

    The plugin is disabled by default outside the app. ChatGPT enables it on
    its own app-server and supplies the listener environment at startup.
    An explicit user MCP entry with the same name shadows that plugin.
    """
    config_path = str(codex_home / "config.toml")
    layers = client.call("config/read", {"includeLayers": True})["layers"]
    layer = next(layer for layer in layers if layer["name"] == {
        "type": "user", "file": config_path, "profile": None,
    })
    config = layer["config"]
    servers = config.get("mcp_servers", {})
    legacy = servers.get("codex_app")
    if legacy is None:
        return False
    plugin_name = "codex-app-tools@openai-bundled"
    plugin = config.get("plugins", {}).get(plugin_name, {})
    override = dict(plugin.get("mcp_servers", {}).get("codex_app", {}))
    for key in ("default_tools_approval_mode", "enabled_tools", "disabled_tools", "omit_tools_from"):
        if key in legacy and key not in override:
            override[key] = legacy[key]
    override["tools"] = {**legacy.get("tools", {}), **override.get("tools", {})}
    edits = [{
        "keyPath": "mcp_servers", "value": {key: value for key, value in servers.items() if key != "codex_app"},
        "mergeStrategy": "replace",
    }, {
        "keyPath": f'plugins."{plugin_name}".mcp_servers.codex_app',
        "value": override, "mergeStrategy": "replace",
    }]
    client.call("config/batchWrite", {
        "filePath": config_path, "expectedVersion": layer["version"], "edits": edits,
    })
    client.call("config/mcpServer/reload", {})
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--codex-home", type=Path)
    args = parser.parse_args()
    mode_path = Path.home() / ".config/claude-bridge/app-connection-mode"
    native = args.native or (mode_path.exists() and mode_path.read_text().strip() == "native")
    codex_home = args.codex_home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    client = AppServerClient(str(codex_home / "app-server-control/app-server-control.sock"), timeout_seconds=30)
    try:
        if native:
            changed = configure_native_tools(client, codex_home)
        else:
            changed = disable_tools(client, codex_home)
    finally:
        client.close()
    if native:
        print("アプリ用ツールの権限設定を標準プラグインへ移しました。" if changed else "標準プラグインを使う設定です。")
    else:
        print("codex_app を無効にしました。" if changed else "codex_app はすでに無効か、未登録です。")


if __name__ == "__main__":
    main()
