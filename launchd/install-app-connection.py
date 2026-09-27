#!/usr/bin/env python3
"""Persist the desktop app's connection to the existing shared app-server."""

import os
import plistlib
import subprocess
from pathlib import Path
from urllib.parse import quote


LABEL = "local.codex-shared-connection"


def install_connection(user_home: Path, codex_home: Path, uid: int) -> str:
    socket_path = codex_home / "app-server-control/app-server-control.sock"
    # ws+unix splits at ':' and doesn't unescape its socket pathname.
    if not socket_path.is_absolute() or quote(str(socket_path), safe="/") != str(socket_path):
        raise ValueError("CODEX_HOME must be an absolute URL-safe path for ws+unix")
    # A localhost hostname also avoids the app's proxy for non-local WS URLs.
    url = f"ws+unix://localhost{socket_path}:/rpc"
    arguments = ["/bin/launchctl", "setenv", "CODEX_APP_SERVER_WS_URL", url]
    plist_path = user_home / "Library/LaunchAgents" / f"{LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": arguments,
        "RunAtLoad": True,
    }))
    domain = f"gui/{uid}"
    # Apply now as well as at the next login. No app-server restart is needed.
    subprocess.run(arguments, check=True)
    # An unregistered job is expected on the first installation.
    subprocess.run(
        ["/bin/launchctl", "bootout", f"{domain}/{LABEL}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist_path)], check=True)
    return url


if __name__ == "__main__":
    home_dir = Path.home()
    endpoint = install_connection(
        home_dir, Path(os.environ.get("CODEX_HOME", str(home_dir / ".codex"))), os.getuid(),
    )
    print(f"ChatGPT shared app-server: {endpoint}")
    print("Restart ChatGPT after stopping its active tasks to apply the connection setting.")
