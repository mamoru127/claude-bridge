"""Install and verify the desktop app tools on the existing shared app-server."""

import argparse
import json
import os
import shutil
from pathlib import Path

from .app_server import AppServerClient


RESOURCES = Path("/Applications/ChatGPT.app/Contents/Resources")
LAUNCHER = Path(__file__).resolve().parents[1] / "launchd/launch-app-tools.py"
REQUIRED_TOOLS = {"create_thread", "list_projects", "list_threads", "read_thread", "set_thread_title"}


def verify_tools(client):
    config = client.call("config/read", {"includeLayers": False})["config"]
    server = config.get("mcp_servers", {}).get("codex_app", {})
    if not server.get("enabled", True) or "deferred" in server.get("omit_tools_from", []):
        raise RuntimeError("codex_app is disabled or excluded from deferred tools in effective config")
    disabled = REQUIRED_TOOLS.intersection(server.get("disabled_tools", []))
    if server.get("enabled_tools") is not None:
        disabled |= REQUIRED_TOOLS - set(server["enabled_tools"])
    disabled |= {name for name in REQUIRED_TOOLS if server.get("tools", {}).get(name, {}).get("enabled") is False}
    if disabled:
        raise RuntimeError(f"Required codex_app tools are disabled: {', '.join(sorted(disabled))}")
    cursor = None
    while True:
        page = client.call("mcpServerStatus/list", {
            "detail": "toolsAndAuthOnly", "limit": 100, "cursor": cursor,
        })
        for server in page["data"]:
            if server["name"] == "codex_app":
                missing = REQUIRED_TOOLS - server["tools"].keys()
                if missing:
                    raise RuntimeError(f"codex_app is missing required tools: {', '.join(sorted(missing))}")
                return
        cursor = page.get("nextCursor")
        if not cursor:
            raise RuntimeError("codex_app is not available on the shared app-server")


def install_tools(client, user_home: Path, codex_home: Path, resources: Path = RESOURCES):
    plugin = resources / "plugins/openai-bundled/plugins/codex-app-tools"
    defaults = json.loads((plugin / "desktop-mcp.json").read_text())["mcpServers"]["codex_app"]
    for path in [resources / "cua_node/bin/node", plugin / "server.mjs", LAUNCHER]:
        if not path.is_file():
            raise FileNotFoundError(path)
    config_path = str(codex_home / "config.toml")
    layers = client.call("config/read", {"includeLayers": True})["layers"]
    layer = next(layer for layer in layers if layer["name"] == {
        "type": "user", "file": config_path, "profile": None,
    })
    existing = layer["config"].get("mcp_servers", {}).get("codex_app", {})
    if "url" in existing:
        raise RuntimeError("codex_app must be a local STDIO server, not an HTTP server")
    launcher = user_home / ".local/share/codex-shared-connection/launch-app-tools.py"
    # Keep explicit tool permissions and filters; import defaults only for
    # previously unset fields. The installer owns transport and exposure.
    server = {**defaults, **existing, "command": "/usr/bin/python3", "args": [str(launcher)],
              "cwd": str(plugin), "enabled": True}
    server["tools"] = {**defaults.get("tools", {}), **existing.get("tools", {})}
    server["omit_tools_from"] = [x for x in existing.get("omit_tools_from", []) if x != "deferred"]
    launcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LAUNCHER, launcher)
    if server != existing:
        client.call("config/batchWrite", {
            "filePath": config_path, "expectedVersion": layer["version"],
            "edits": [{"keyPath": "mcp_servers.codex_app", "value": server, "mergeStrategy": "replace"}],
        })
    client.call("config/mcpServer/reload", {})
    verify_tools(client)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check tools without editing or reloading configuration")
    args = parser.parse_args()
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    client = AppServerClient(str(codex_home / "app-server-control/app-server-control.sock"), timeout_seconds=30)
    try:
        if args.check:
            verify_tools(client)
        else:
            install_tools(client, home, codex_home)
    finally:
        client.close()
    print("Shared app-server exposes: " + ", ".join(sorted(REQUIRED_TOOLS)))
    print("This checks the server catalog; an already-running turn may need a new turn to load the tools.")


if __name__ == "__main__":
    main()
