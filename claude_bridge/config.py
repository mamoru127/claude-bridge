"""ブリッジの設定。環境変数とコマンドライン引数から組み立てる。"""

import argparse
import ipaddress
import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_CLAUDE_PATH = "claude"
DEFAULT_MODEL = "opus"
# ツールを何度も使う長い作業では 10 分では足りない。Codex 側の待機上限（60 分）に合わせる。
DEFAULT_TIMEOUT_SECONDS = 3600.0
# 画像は data URL の base64 で送られてくるため、本文はテキストだけの頃より桁違いに大きくなる。
# 実測では全画面スクリーンショット 1 枚で約 5MB あり、会話履歴として毎回送り直される。
# 32MB は Claude API 側のリクエスト上限でもあるので、これ以上受け取っても CLI 実行で失敗する。
DEFAULT_MAX_REQUEST_BYTES = 32 * 1024 * 1024
DEFAULT_CODEX_PATH = "codex"
# MCP アダプタが接続するブリッジ本体。アダプタは別プロセスなので URL で指す。
DEFAULT_BRIDGE_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

# ChatGPT アカウントで認証する Codex の上流。--upstream-base-url を指定したときだけ使う。
DEFAULT_UPSTREAM_BASE_URL = "https://chatgpt.com/backend-api/codex"

# API で受け付けるモデル名。GET /v1/models に載り、中継が有効ならこれ以外は上流へ中継する。
# CLI に渡す実行モデルは常に --model 固定なので、名前を増やしても実体は変わらない。
# 上流に実在するモデル名を選ぶと、その名前が上流一覧から除かれてアプリで選べなくなる。
DEFAULT_CLAUDE_MODELS = ("claude-opus-5",)

# --mcp-config で登録する MCP サーバー名。ツール名は mcp__<この名前>__<tool> になる。
MCP_SERVER_NAME = "codex_app_server"
# Codex から届いたツールを Claude へ見せるための MCP サーバー名。定義はリクエストごとに変わる。
PASSTHROUGH_SERVER_NAME = "codex_native"

# Codex へ受け渡すツール名。Codex が送ってきたものだけが対象で、定義（スキーマ）も Codex 由来。
# 既定はスレッド操作に限る。create_thread が作るのは利用者所有のスレッドで、Codex アプリの
# 一覧に並ぶため、スマホから進捗を追える。spawn_agent が作るサブエージェントは
# thread_source=subagent となって一覧に出ないため、受け渡さない。
# Claude 自身が持つ Bash や Read を置き換えないため、全件は通さない。
DEFAULT_PASSTHROUGH_TOOLS = (
    "create_thread",
    "fork_thread",
    "list_projects",
    "list_threads",
    "read_thread",
    "send_message_to_thread",
    "set_thread_title",
)

# スレッド操作は deferLoading 付きで登録されており、tool_search を通すまでリクエストへ載らない。
# tool_search はブリッジが自分で 1 回投げる。Claude へ見せても、結果が次のターンになる分だけ
# 往復が伸びるだけで、探す語は毎回同じだからである。
DEFAULT_TOOL_SEARCH_QUERY = "create thread fork thread list threads codex task management"

API_PREFIX = "/v1"
RESPONSES_PATH = "/v1/responses"
MODELS_PATH = "/v1/models"
HEALTH_PATH = "/health"
# MCP アダプタが Codex app-server の RPC を投げる先。常駐するブリッジ本体が中継する。
APP_SERVER_PATH = "/app-server/rpc"
# 受け渡しツールのアダプタが、Claude からの呼び出しを預ける先。
PASSTHROUGH_PATH = "/codex-tool/call"
# 受け渡しアダプタへツール定義と預かり口を渡す環境変数。
# 定義はリクエストごとに変わるため、起動時の設定（CLAUDE_BRIDGE_PASSTHROUGH_TOOLS）とは別名にする。
PASSTHROUGH_DEFS_ENV = "CLAUDE_BRIDGE_PASSTHROUGH_DEFS"
PASSTHROUGH_TOKEN_ENV = "CLAUDE_BRIDGE_PASSTHROUGH_TOKEN"


@dataclass(frozen=True)
class BridgeConfig:
    """HTTP サーバーと Claude CLI 実行の設定値。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    api_key: str | None = None
    claude_path: str = DEFAULT_CLAUDE_PATH
    model: str = DEFAULT_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    working_dir: str = "."
    # working_dir の外で Claude CLI にツール操作を許可する追加ディレクトリ。
    add_dirs: tuple[str, ...] = ()
    # 明示的に有効化したときだけ、Codex app-server の MCP アダプタを Claude CLI へ渡す。
    codex_mcp: bool = False
    codex_path: str = DEFAULT_CODEX_PATH
    # --mcp-config-file を起動時に読んだ結果。Claude CLI へ追加で渡すサーバー定義。
    extra_mcp_servers: dict = field(default_factory=dict)
    # 空文字なら中継しない（全モデルを Claude CLI で処理する）。
    upstream_base_url: str = ""
    claude_models: tuple[str, ...] = DEFAULT_CLAUDE_MODELS
    # Codex から届いたツールのうち、Claude へ見せて Codex へ返すもの。空なら受け渡さない。
    passthrough_tools: tuple[str, ...] = DEFAULT_PASSTHROUGH_TOOLS
    # 遅延読み込みのツールを引き出すためにブリッジが投げる検索語。空なら投げない。
    tool_search_query: str = DEFAULT_TOOL_SEARCH_QUERY

    @property
    def bridge_url(self) -> str:
        """MCP アダプタなど、同じマシンの別プロセスがブリッジへ接続するための URL。"""

        # IPv6 アドレスは括弧で囲む。囲まないとポートと区別できない URL になる。
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def is_loopback(self) -> bool:
        """bind 先がループバックだけを指すかどうか。ホスト名指定は非ループバック扱い。"""

        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    def routes_to_claude(self, model: str) -> bool:
        """このモデルを Claude CLI で処理するか。中継が無効なら全モデルが対象。"""

        return not self.upstream_base_url or model in self.claude_models


def _env_str(env: dict, name: str, default: str) -> str:
    value = env.get(name)
    return value if value else default


def _split(value: str, separator: str) -> list[str]:
    """区切り文字で分割し、空要素を捨てる。"""

    return [item.strip() for item in value.split(separator) if item.strip()]


def _env_flag(env: dict, name: str) -> bool:
    """オプトイン用の環境変数。明示的な真値のときだけ有効にする。"""

    return env.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_number(env: dict, name: str, default: float) -> float:
    value = env.get(name)
    if not value:
        return default
    return float(value)


def _abs_path(path: str) -> str:
    """設定で受け取ったパスを絶対パスにする。launchd 経由では `~` が展開されないため補う。"""

    return os.path.abspath(os.path.expanduser(path))


def _load_mcp_servers(path: str) -> dict:
    """--mcp-config-file を起動時に読む。壊れた設定をリクエスト時まで持ち越さない。

    検証するのは同梱アダプタと合成できる形かどうかまで。個々のサーバー定義の妥当性は
    Claude CLI の契約なので、ここに写して二重管理しない（起動しないと分からない）。
    """

    if not path:
        return {}
    path = _abs_path(path)
    try:
        with open(path, encoding="utf-8") as handle:
            servers = json.load(handle)["mcpServers"]
    except (OSError, ValueError, LookupError, TypeError) as error:
        raise ValueError(f"MCP 設定ファイルを読み込めません: {path} ({error})") from error
    if not isinstance(servers, dict):
        raise ValueError(f"mcpServers はオブジェクトで指定してください: {path}")
    # 同名だと同梱アダプタの定義を黙って置き換えてしまうため、追加ではなく誤設定として扱う。
    if MCP_SERVER_NAME in servers:
        raise ValueError(f"{MCP_SERVER_NAME} は再定義できません: {path}")
    return servers


def build_config(argv: list[str] | None = None, env: dict | None = None) -> BridgeConfig:
    """コマンドライン引数（環境変数を既定値とする）から設定を作る。"""

    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(
        prog="claude_bridge",
        description="Claude CLI を OpenAI Responses API 互換の HTTP で公開する",
    )
    parser.add_argument("--host", default=_env_str(env, "CLAUDE_BRIDGE_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port", type=int, default=int(_env_number(env, "CLAUDE_BRIDGE_PORT", DEFAULT_PORT))
    )
    parser.add_argument(
        "--claude-path",
        default=_env_str(env, "CLAUDE_BRIDGE_CLAUDE_PATH", DEFAULT_CLAUDE_PATH),
    )
    parser.add_argument("--model", default=_env_str(env, "CLAUDE_BRIDGE_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--timeout",
        type=float,
        default=_env_number(env, "CLAUDE_BRIDGE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
    )
    parser.add_argument(
        "--max-request-bytes",
        type=int,
        default=int(
            _env_number(env, "CLAUDE_BRIDGE_MAX_REQUEST_BYTES", DEFAULT_MAX_REQUEST_BYTES)
        ),
    )
    parser.add_argument(
        "--working-dir",
        default=_env_str(env, "CLAUDE_BRIDGE_WORKING_DIR", os.getcwd()),
    )
    parser.add_argument(
        "--add-dir",
        action="append",
        dest="add_dirs",
        help="Claude CLI にツール操作を許可する追加ディレクトリ（繰り返し指定可）",
    )
    parser.add_argument(
        "--upstream-base-url",
        default=_env_str(env, "CLAUDE_BRIDGE_UPSTREAM_BASE_URL", ""),
        help=(
            "Claude 以外のモデルの中継先。省略すると中継せず全モデルを Claude CLI で処理する"
            f"（Codex の既定は {DEFAULT_UPSTREAM_BASE_URL}）"
        ),
    )
    parser.add_argument(
        "--claude-models",
        default=_env_str(
            env, "CLAUDE_BRIDGE_CLAUDE_MODELS", ",".join(DEFAULT_CLAUDE_MODELS)
        ),
        help="Claude CLI で処理するモデル名のカンマ区切り。GET /v1/models が返す一覧そのもの",
    )
    parser.add_argument(
        "--enable-codex-mcp",
        action="store_true",
        default=_env_flag(env, "CLAUDE_BRIDGE_ENABLE_CODEX_MCP"),
        help="Codex app-server の MCP アダプタを Claude CLI へ渡す（既定は無効）",
    )
    parser.add_argument(
        "--codex-path",
        default=_env_str(env, "CLAUDE_BRIDGE_CODEX_PATH", DEFAULT_CODEX_PATH),
    )
    parser.add_argument(
        "--mcp-config-file",
        default=_env_str(env, "CLAUDE_BRIDGE_MCP_CONFIG_FILE", ""),
        help="追加で有効にする MCP サーバーを書いた JSON ファイル（mcpServers を持つ形式）",
    )
    parser.add_argument(
        "--passthrough-tools",
        default=_env_str(
            env, "CLAUDE_BRIDGE_PASSTHROUGH_TOOLS", ",".join(DEFAULT_PASSTHROUGH_TOOLS)
        ),
        help=(
            "Codex から届いたツールのうち Claude へ見せる名前のカンマ区切り。"
            "呼び出しは Codex 側で実行される。空文字で無効"
        ),
    )
    parser.add_argument(
        "--tool-search-query",
        default=_env_str(
            env, "CLAUDE_BRIDGE_TOOL_SEARCH_QUERY", DEFAULT_TOOL_SEARCH_QUERY
        ),
        help=(
            "遅延読み込みのツールを引き出すために Codex へ投げる検索語。"
            "空文字で無効"
        ),
    )
    args = parser.parse_args(argv)
    add_dirs = args.add_dirs or _split(env.get("CLAUDE_BRIDGE_ADD_DIRS", ""), os.pathsep)

    config = BridgeConfig(
        host=args.host,
        port=args.port,
        api_key=env.get("CLAUDE_BRIDGE_API_KEY") or None,
        claude_path=args.claude_path,
        model=args.model,
        timeout_seconds=args.timeout,
        max_request_bytes=args.max_request_bytes,
        working_dir=_abs_path(args.working_dir),
        add_dirs=tuple(_abs_path(path) for path in add_dirs),
        codex_mcp=args.enable_codex_mcp,
        codex_path=args.codex_path,
        extra_mcp_servers=_load_mcp_servers(args.mcp_config_file),
        upstream_base_url=args.upstream_base_url.rstrip("/"),
        claude_models=tuple(_split(args.claude_models, ",")),
        passthrough_tools=tuple(_split(args.passthrough_tools, ",")),
        tool_search_query=args.tool_search_query.strip(),
    )
    validate_config(config)
    return config


def validate_config(config: BridgeConfig) -> None:
    """起動前に安全性と実行可能性を検証する。"""

    if not os.path.isdir(config.working_dir):
        raise ValueError(f"作業ディレクトリが存在しません: {config.working_dir}")
    for directory in config.add_dirs:
        if not os.path.isdir(directory):
            raise ValueError(f"追加ディレクトリが存在しません: {directory}")
    # 空だと GET /v1/models が空になり、クライアントからモデルを選べなくなる。
    if not config.claude_models:
        raise ValueError("Claude で処理するモデル名を 1 つ以上指定してください")
    if config.upstream_base_url:
        upstream = urlsplit(config.upstream_base_url)
        if upstream.scheme not in ("http", "https") or not upstream.netloc:
            raise ValueError(f"中継先の URL が不正です: {config.upstream_base_url}")
        # 中継では Codex が送る Authorization をそのまま上流へ渡すため、独自キーの検証と両立しない。
        if config.api_key:
            raise ValueError("中継の有効時に CLAUDE_BRIDGE_API_KEY は併用できません")
    if config.timeout_seconds <= 0:
        raise ValueError("タイムアウトは正の秒数で指定してください")
    if config.max_request_bytes <= 0:
        raise ValueError("リクエストの上限サイズは正の値で指定してください")
    if not config.is_loopback() and not config.api_key:
        raise ValueError(
            "ループバック以外へ bind する場合は CLAUDE_BRIDGE_API_KEY の設定が必要です"
        )
