"""ブリッジサーバーの起動エントリポイント。"""

import logging
import os
import shutil
import subprocess
import sys

from .app_server import AppServerClient
from .config import RESPONSES_PATH, build_config
from .server import BridgeServer

logger = logging.getLogger("claude_bridge")


def start_windows_app_server(config, *, platform=os.name) -> None:
    """Windows では Codex の管理デーモンをブリッジと一緒に起動する。"""

    if platform != "nt" or not config.codex_mcp:
        return
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("Codex CLI が見つかりません。Codex をインストールしてください")
    subprocess.run([codex, "app-server", "daemon", "start"], check=True)
    client = AppServerClient(config.app_server_socket, timeout_seconds=10)
    try:
        client.call("thread/list", {"limit": 1})
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = build_config(argv)
    start_windows_app_server(config)

    server = BridgeServer(config)
    host, port = server.server_address[:2]
    logger.info(
        "listening on http://%s:%s%s (claude=%s antigravity=%s model=%s cwd=%s "
        "codex_mcp=%s)",
        host,
        port,
        RESPONSES_PATH,
        config.claude_path,
        config.antigravity_path if config.antigravity_models else "disabled",
        config.model,
        config.working_dir,
        config.app_server_socket if config.codex_mcp else "disabled",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
