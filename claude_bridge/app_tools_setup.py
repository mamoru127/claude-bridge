"""Keep the desktop app tools (codex_app) disabled on the shared app-server.

ChatGPT only accepts app tools connections whose parent or grandparent process is
the app itself (`dynamic_app_tools_peer_rejected reason=untrusted-process-ancestry`).
The shared app-server is started by launchd, so codex_app always fails to start
there and every thread start leaves a rejection in the app log. Thread creation
and messaging go through the bridge's codex_app_server MCP instead. The entry is
disabled rather than removed so its tool permissions survive if the app ever
accepts the shared server again.
"""

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


def main():
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    client = AppServerClient(str(codex_home / "app-server-control/app-server-control.sock"), timeout_seconds=30)
    try:
        changed = disable_tools(client, codex_home)
    finally:
        client.close()
    print("codex_app を無効にしました。" if changed else "codex_app はすでに無効か、未登録です。")


if __name__ == "__main__":
    main()
