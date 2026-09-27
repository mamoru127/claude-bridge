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
DEFAULT_MODEL = "claude-opus-5-5"
# ツールを何度も使う長い作業では 10 分では足りない。Codex 側の待機上限（60 分）に合わせる。
DEFAULT_TIMEOUT_SECONDS = 3600.0
# CLI へ 1 回で渡す入力の上限。32MB は Claude API 側のリクエスト上限で、これを超える分を
# 渡しても CLI 実行で失敗する。リクエスト全体ではなく、組み立てた入力そのもの（続きのターン
# では差分の発言と system プロンプト、ツール結果の画像は parse の時点で落としたあと）に
# 対して判定する。CLI が最終的に API へ送る本文には転記やツール定義も載るため、この上限は
# その本文の予測ではなく、渡す側で掛ける歯止めとして扱う。
DEFAULT_MAX_REQUEST_BYTES = 32 * 1024 * 1024
# ブリッジが受信するリクエスト本文の上限。OpenAI の画像入力はリクエスト全体で 512MB まで
# 受け付ける。Codex は会話履歴を毎ターン丸ごと送り直すため、ローカルの CLI で実行する
# リクエストもこの上限で受け取る。
DEFAULT_MAX_UPSTREAM_REQUEST_BYTES = 512 * 1024 * 1024
# 常駐 app-server の制御ソケット。`codex app-server --listen unix://` が作る既定の場所で、
# Codex アプリも同じ場所へ繋ぐ。両者が 1 つの app-server を共有すると、
# ブリッジが始めたターンの実行中表示がアプリ側にも出る。
APP_SERVER_SOCKET_RELATIVE_PATH = "app-server-control/app-server-control.sock"
# MCP アダプタが接続するブリッジ本体。アダプタは別プロセスなので URL で指す。
DEFAULT_BRIDGE_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

# ChatGPT アカウントで認証する Codex の上流。--upstream-base-url を指定したときだけ使う。
DEFAULT_UPSTREAM_BASE_URL = "https://chatgpt.com/backend-api/codex"

# API で受け付けるモデル名。GET /v1/models に載り、中継が有効ならこれ以外は上流へ中継する。
# ここに載せた名前はそのまま CLI の --model へ渡すため、Claude CLI が解釈できる名前
# （`opus` などの別名、または `claude-opus-5-5` のような正式名）だけを並べる。
# 上流に実在するモデル名を選ぶと、その名前が上流一覧から除かれてアプリで選べなくなる。
DEFAULT_CLAUDE_MODELS = ("claude-opus-5-5", "claude-fable-5-1")

# Antigravity CLI（`agy`）の実行ファイル。未インストールなら該当モデルの実行時だけ失敗する。
DEFAULT_ANTIGRAVITY_PATH = "agy"
# Antigravity CLI が受け付けるモデル名。思考の強さは名前の high / medium で決まるため、
# ブリッジ側で effort を渡し分けることはしない。ここに載せた名前をそのまま --model へ渡す。
DEFAULT_ANTIGRAVITY_MODELS = ("gemini-3.8-flash-high", "gemini-3.8-flash-medium")

# モデル名から決まる実行先。空文字は「ローカルの CLI では処理せず上流へ中継する」を表す。
CLAUDE_BACKEND = "claude"
ANTIGRAVITY_BACKEND = "antigravity"
UPSTREAM_BACKEND = ""

# --mcp-config で登録する MCP サーバー名。ツール名は mcp__<この名前>__<tool> になる。
MCP_SERVER_NAME = "codex_app_server"
# Codex から届いたツールを Claude へ見せるための MCP サーバー名。定義はリクエストごとに変わる。
PASSTHROUGH_SERVER_NAME = "codex_native"

# Codex から届いた function ツールを Claude へ受け渡す。定義と実行権限の正本は
# あくまで親 Codex であり、ブリッジ側で MCP 名を二重管理しない。"*" は、親がその
# リクエストで提示した全 function と tool_search を意味する。custom など引数スキーマを
# MCP へ写せない種類は responses 側で構造的に除外する。
DEFAULT_PASSTHROUGH_TOOLS = ("*",)

# 特定名のツールだけを設定した運用向けに、ブリッジが先回りして投げる検索語も
# 残す。既定は Claude 自身が受け渡された tool_search で必要なツールを探すため空。
DEFAULT_TOOL_SEARCH_QUERY = ""

# 検索が返す件数の上限。既定のままだと DEFAULT_PASSTHROUGH_TOOLS より少なく打ち切られる。
TOOL_SEARCH_LIMIT = 20

API_PREFIX = "/v1"
RESPONSES_PATH = "/v1/responses"
MODELS_PATH = "/v1/models"
IMAGE_GENERATIONS_PATH = "/v1/images/generations"
IMAGE_EDITS_PATH = "/v1/images/edits"
IMAGE_PATHS = frozenset({IMAGE_GENERATIONS_PATH, IMAGE_EDITS_PATH})
HEALTH_PATH = "/health"
# MCP アダプタが Codex app-server の RPC を投げる先。常駐するブリッジ本体が中継する。
APP_SERVER_PATH = "/app-server/rpc"
# 受け渡しツールのアダプタが、Claude からの呼び出しを預ける先。
PASSTHROUGH_PATH = "/codex-tool/call"
# 受け渡しアダプタへツール定義と預かり口を渡す環境変数。
# 定義はリクエストごとに変わるため、起動時の設定（CLAUDE_BRIDGE_PASSTHROUGH_TOOLS）とは別名にする。
PASSTHROUGH_DEFS_ENV = "CLAUDE_BRIDGE_PASSTHROUGH_DEFS"
PASSTHROUGH_TOKEN_ENV = "CLAUDE_BRIDGE_PASSTHROUGH_TOKEN"


def default_app_server_socket(env=None) -> str:
    """常駐 app-server の制御ソケットの既定の場所。CODEX_HOME に従う。"""

    env = os.environ if env is None else env
    codex_home = env.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(codex_home, APP_SERVER_SOCKET_RELATIVE_PATH)


@dataclass(frozen=True)
class BridgeConfig:
    """HTTP サーバーと Claude CLI 実行の設定値。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    api_key: str | None = None
    claude_path: str = DEFAULT_CLAUDE_PATH
    antigravity_path: str = DEFAULT_ANTIGRAVITY_PATH
    model: str = DEFAULT_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_upstream_request_bytes: int = DEFAULT_MAX_UPSTREAM_REQUEST_BYTES
    working_dir: str = "."
    # working_dir の外で Claude CLI にツール操作を許可する追加ディレクトリ。
    add_dirs: tuple[str, ...] = ()
    # 明示的に有効化したときだけ、Codex app-server の MCP アダプタを Claude CLI へ渡す。
    codex_mcp: bool = False
    app_server_socket: str = field(default_factory=default_app_server_socket)
    # --mcp-config-file を起動時に読んだ結果。Claude CLI へ追加で渡すサーバー定義。
    extra_mcp_servers: dict = field(default_factory=dict)
    # 空文字なら中継しない（全モデルを Claude CLI で処理する）。
    upstream_base_url: str = ""
    claude_models: tuple[str, ...] = DEFAULT_CLAUDE_MODELS
    # Antigravity CLI が受け持つモデル名。空にすると Antigravity は一切公開されない。
    antigravity_models: tuple[str, ...] = DEFAULT_ANTIGRAVITY_MODELS
    # Codex から届いたツールのうち、Claude へ見せて Codex へ返すもの。"*" は全件、空なら無効。
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

    @property
    def cli_models(self) -> tuple[str, ...]:
        """ローカルの CLI で処理するモデル名。GET /v1/models へ載せる一覧そのもの。"""

        return self.claude_models + self.antigravity_models

    def backend(self, model: str) -> str:
        """このモデルを実行する CLI。中継しない場合は空文字を返す。

        一覧に載せていない名前は、中継が有効なら上流へ、無効なら Claude CLI が受け持つ。
        Antigravity CLI は明示的に割り当てた名前だけを処理する（`agy` は Claude CLI の
        別名を解釈できないため、既定の受け皿にはできない）。
        """

        if model in self.antigravity_models:
            return ANTIGRAVITY_BACKEND
        if model in self.claude_models or not self.upstream_base_url:
            return CLAUDE_BACKEND
        return UPSTREAM_BACKEND

    def cli_model(self, model: str) -> str:
        """リクエストのモデル名に対して CLI の --model へ渡す実行モデル名を返す。

        渡してよいのは起動時に許可した名前だけ。中継が無効なときは一覧にない名前でも
        Claude CLI へ回るため、そこは起動時の `--model` で実行する。
        """

        return model if model in self.cli_models else self.model


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
    parser.add_argument(
        "--antigravity-path",
        default=_env_str(env, "CLAUDE_BRIDGE_ANTIGRAVITY_PATH", DEFAULT_ANTIGRAVITY_PATH),
        help="Antigravity CLI（agy）の実行ファイルパス",
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
        "--max-upstream-request-bytes",
        type=int,
        default=int(
            _env_number(
                env,
                "CLAUDE_BRIDGE_MAX_UPSTREAM_REQUEST_BYTES",
                DEFAULT_MAX_UPSTREAM_REQUEST_BYTES,
            )
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
        help="Claude CLI で処理するモデル名のカンマ区切り。GET /v1/models が返す一覧に載る",
    )
    parser.add_argument(
        "--antigravity-models",
        default=_env_str(
            env, "CLAUDE_BRIDGE_ANTIGRAVITY_MODELS", ",".join(DEFAULT_ANTIGRAVITY_MODELS)
        ),
        help=(
            "Antigravity CLI で処理するモデル名のカンマ区切り。GET /v1/models が返す一覧に載る。"
            "空文字で無効"
        ),
    )
    parser.add_argument(
        "--enable-codex-mcp",
        action="store_true",
        default=_env_flag(env, "CLAUDE_BRIDGE_ENABLE_CODEX_MCP"),
        help="Codex app-server の MCP アダプタを Claude CLI へ渡す（既定は無効）",
    )
    parser.add_argument(
        "--app-server-socket",
        default=_env_str(
            env, "CLAUDE_BRIDGE_APP_SERVER_SOCKET", default_app_server_socket(env)
        ),
        help="繋ぎ先の codex app-server 制御ソケット（`--listen unix://` で作られる）",
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
            "* は全 function と tool_search。呼び出しは Codex 側で実行される。空文字で無効"
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
        antigravity_path=args.antigravity_path,
        model=args.model,
        timeout_seconds=args.timeout,
        max_request_bytes=args.max_request_bytes,
        max_upstream_request_bytes=args.max_upstream_request_bytes,
        working_dir=_abs_path(args.working_dir),
        add_dirs=tuple(_abs_path(path) for path in add_dirs),
        codex_mcp=args.enable_codex_mcp,
        app_server_socket=args.app_server_socket,
        extra_mcp_servers=_load_mcp_servers(args.mcp_config_file),
        upstream_base_url=args.upstream_base_url.rstrip("/"),
        claude_models=tuple(_split(args.claude_models, ",")),
        antigravity_models=tuple(_split(args.antigravity_models, ",")),
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
    # model 省略時の既定にも使うため、Antigravity だけを公開する構成は取れない。
    if not config.claude_models:
        raise ValueError("Claude で処理するモデル名を 1 つ以上指定してください")
    # 同じ名前が両方にあると、どちらの CLI で実行するかが名前から決まらなくなる。
    overlap = set(config.claude_models) & set(config.antigravity_models)
    if overlap:
        raise ValueError(
            f"同じモデル名を Claude と Antigravity の両方へ割り当てられません: {','.join(sorted(overlap))}"
        )
    if config.antigravity_models and not config.antigravity_path:
        raise ValueError("Antigravity のモデルを公開する場合は実行ファイルパスが必要です")
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
    if config.max_upstream_request_bytes <= 0:
        raise ValueError("上流中継リクエストの上限サイズは正の値で指定してください")
    if not config.is_loopback() and not config.api_key:
        raise ValueError(
            "ループバック以外へ bind する場合は CLAUDE_BRIDGE_API_KEY の設定が必要です"
        )
