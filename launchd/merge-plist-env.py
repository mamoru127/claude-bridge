#!/usr/bin/env python3
"""Add NAME=value lines from a machine-local env file to a plist's EnvironmentVariables.

Keeps personal settings (extra --add-dir paths, MCP config file, ...) out of the repository
while install.sh stays the single way to register the bridge.
"""

import plistlib
import re
import sys
from pathlib import Path

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not NAME.fullmatch(name):
            raise ValueError(f"{path}:{number}: NAME=value の形で書いてください")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def merge(plist_path: Path, env_path: Path) -> None:
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    plist.setdefault("EnvironmentVariables", {}).update(read_env(env_path))
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(f"Usage: {sys.argv[0]} PLIST ENV_FILE")
    merge(Path(sys.argv[1]), Path(sys.argv[2]))
