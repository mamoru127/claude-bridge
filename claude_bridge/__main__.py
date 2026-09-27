"""ブリッジサーバーの起動エントリポイント。"""

import logging
import sys

from .config import RESPONSES_PATH, build_config
from .server import BridgeServer

logger = logging.getLogger("claude_bridge")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = build_config(argv)

    server = BridgeServer(config)
    host, port = server.server_address[:2]
    logger.info(
        "listening on http://%s:%s%s (claude=%s antigravity=%s model=%s cwd=%s auth=%s "
        "codex_mcp=%s)",
        host,
        port,
        RESPONSES_PATH,
        config.claude_path,
        config.antigravity_path if config.antigravity_models else "disabled",
        config.model,
        config.working_dir,
        "bearer" if config.api_key else "none",
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
