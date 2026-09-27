#!/usr/bin/python3
"""Connect the shared app-server's MCP to the running ChatGPT desktop app."""

import os
import re
import socket
from pathlib import Path


RESOURCES = Path("/Applications/ChatGPT.app/Contents/Resources")


def find_listener(logs: Path) -> str:
    checked = set()
    # Listener announcements can precede log rotation. Check live sockets, not
    # just the newest file, and never substitute the browser's separate pipe.
    for log in sorted(logs.glob("*/*/*/*-t0-i1-*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
        for candidate in reversed(re.findall(
            r"dynamic_app_tools_listening pipePath=([^\s]+)", log.read_text(),
        )):
            if candidate in checked:
                continue
            checked.add(candidate)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(1)
                try:
                    connection.connect(candidate)
                except OSError:
                    continue
            return candidate
    raise RuntimeError(
        "ChatGPT app tools listener is unavailable. Quit the old Codex app and open "
        "/Applications/ChatGPT.app; then reload MCP configuration."
    )


def main():
    pipe = find_listener(Path.home() / "Library/Logs/com.openai.codex")
    node = str(RESOURCES / "cua_node/bin/node")
    server = str(RESOURCES / "plugins/openai-bundled/plugins/codex-app-tools/server.mjs")
    # exec preserves the app-server parent. Launching a diagnostic MCP from a
    # shell/Python subprocess adds ancestors and does not test this trust path.
    os.execve(node, [node, server], {**os.environ, "CODEX_APP_TOOLS_PIPE_PATH": pipe})


if __name__ == "__main__":
    main()
