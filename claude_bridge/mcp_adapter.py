"""Codex app-server の RPC を MCP tool として公開する stdio MCP サーバー。

Claude CLI に `--mcp-config` で明示的に渡されたときだけ起動する。
tools/call を受け取ったら、常駐するブリッジ本体の HTTP エンドポイントへ転送する。
app-server の子プロセスはブリッジ本体が持つ。このアダプタは起動と終了を繰り返すため、
ここで持つと再起動のたびに実行中のターンが道連れになる。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from .app_server import AppServerError
from .config import APP_SERVER_PATH, DEFAULT_BRIDGE_URL, MCP_SERVER_NAME

MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": MCP_SERVER_NAME, "version": "0.1.0"}

PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601

_LIMIT = {"type": "integer", "description": "取得件数の上限"}
_CURSOR = {"type": "string", "description": "前回の応答が返したページングカーソル"}
_SORT_DIRECTION = {"type": "string", "enum": ["asc", "desc"], "description": "並び順"}
_THREAD_ID = {"type": "string", "description": "スレッド ID"}

# 公開する RPC。読み取り系に加えて、スレッド作成とターン実行を公開する。
# app-server 経由で作ったスレッドは Codex アプリの一覧に載る（codex exec のセッションは載らない）。
# turn_start は外部サービスを呼び、指示すればさらに入れ子のターンも作れる点に注意。
# thread/items/list は app-server 側が未実装（is not supported yet）なので公開しない。
# 項目の中身は thread_read の includeTurns で読む。
TOOLS = (
    {
        "name": "thread_start",
        "method": "thread/start",
        "description": "Codex app-server に新しいスレッドを作成する。ターンは開始しない。",
        "properties": {
            "cwd": {"type": "string", "description": "スレッドの作業ディレクトリ"},
            "model": {"type": "string", "description": "使用するモデル名"},
            "modelProvider": {"type": "string", "description": "使用するモデルプロバイダ名"},
            "ephemeral": {"type": "boolean", "description": "履歴を残さないスレッドにするか"},
        },
    },
    {
        "name": "turn_start",
        "method": "turn/start",
        "turn": True,
        "wait": True,
        "description": "スレッドでターンを 1 回実行し、完了まで待って最終メッセージを返す。",
        "required": ["threadId", "text"],
        "properties": {
            "threadId": _THREAD_ID,
            "text": {"type": "string", "description": "送信するユーザーメッセージ"},
        },
    },
    {
        "name": "turn_start_async",
        "method": "turn/start",
        "turn": True,
        "wait": False,
        "description": (
            "スレッドでターンを開始し、完了を待たずに戻る。"
            "実行は Codex アプリのスレッドで続き、"
            "進捗と結果は thread_read（includeTurns=true）や thread_turns_list で確認する。"
            "長い作業や、複数スレッドを並行させたいときはこちらを使う。"
        ),
        "required": ["threadId", "text"],
        "properties": {
            "threadId": _THREAD_ID,
            "text": {"type": "string", "description": "送信するユーザーメッセージ"},
        },
    },
    {
        "name": "thread_list",
        "method": "thread/list",
        "description": "スレッド一覧を新しい順に取得する。",
        "properties": {
            "limit": _LIMIT,
            "cursor": _CURSOR,
            "searchTerm": {"type": "string", "description": "タイトルの部分一致フィルタ"},
            "archived": {"type": "boolean", "description": "真ならアーカイブ済みのみ"},
            "sortDirection": _SORT_DIRECTION,
        },
    },
    {
        "name": "thread_read",
        "method": "thread/read",
        "description": (
            "スレッド 1 件を読む。includeTurns を真にすると、"
            "各ターンの状態とメッセージ本文まで返るため、進捗と結果の確認に使う。"
        ),
        "required": ["threadId"],
        "properties": {
            "threadId": _THREAD_ID,
            "includeTurns": {"type": "boolean", "description": "ターンと項目も含めるか"},
        },
    },
    {
        "name": "thread_turns_list",
        "method": "thread/turns/list",
        "description": "スレッド内のターン（タスク単位の実行）を取得する。",
        "required": ["threadId"],
        "properties": {
            "threadId": _THREAD_ID,
            "limit": _LIMIT,
            "cursor": _CURSOR,
            "sortDirection": _SORT_DIRECTION,
        },
    },
    {
        "name": "thread_search",
        "method": "thread/search",
        "description": "スレッドを全文検索する。",
        "required": ["searchTerm"],
        "properties": {
            "searchTerm": {"type": "string", "description": "検索語"},
            "limit": _LIMIT,
            "cursor": _CURSOR,
        },
    },
)

TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}

# ブリッジ側のターン待機上限（900 秒）より長く取る。待機の打ち切りはブリッジ側で判断させる。
DEFAULT_RPC_TIMEOUT_SECONDS = 960.0


class BridgeRpcClient:
    """常駐するブリッジ本体へ Codex app-server の RPC を転送する。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BRIDGE_URL,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
        opener=urllib.request.urlopen,
    ) -> None:
        self._url = base_url.rstrip("/") + APP_SERVER_PATH
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def call(self, method: str, params: dict, wait: bool = False) -> object:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            self._url,
            data=json.dumps({"method": method, "params": params, "wait": wait}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                return json.load(response)["result"]
        except urllib.error.HTTPError as error:
            raise AppServerError(_http_error_message(error)) from error
        except (urllib.error.URLError, OSError, ValueError, LookupError) as error:
            raise AppServerError(f"ブリッジへ接続できません: {self._url}: {error}") from error


def _http_error_message(error: urllib.error.HTTPError) -> str:
    """ブリッジが返した JSON エラーから本文を取り出す。読めなければ状態コードだけ伝える。"""

    try:
        return str(json.load(error)["error"]["message"])
    except (ValueError, LookupError, TypeError, OSError):
        return f"ブリッジがエラーを返しました: HTTP {error.code}"


def tool_definitions() -> list[dict]:
    """tools/list が返す MCP のツール定義。"""

    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": {
                "type": "object",
                "properties": tool["properties"],
                "required": tool.get("required", []),
            },
        }
        for tool in TOOLS
    ]


def handle(message: dict, client: BridgeRpcClient) -> dict | None:
    """JSON-RPC メッセージ 1 件を処理する。応答不要なら None を返す。"""

    request_id = message.get("id")
    method = message.get("method")
    # 通知（id なし）には応答しない。notifications/initialized などが該当する。
    if request_id is None:
        return None

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "tools/list":
        return _result(request_id, {"tools": tool_definitions()})
    if method == "tools/call":
        return _call_tool(request_id, message.get("params") or {}, client)
    return _error(request_id, METHOD_NOT_FOUND, f"未対応のメソッドです: {method}")


def _call_tool(request_id: object, params: object, client: BridgeRpcClient) -> dict:
    if not isinstance(params, dict):
        return _result(request_id, _content("params はオブジェクトで指定してください", True))

    tool = TOOLS_BY_NAME.get(params.get("name"))
    if tool is None:
        return _result(request_id, _content(f"未対応のツールです: {params.get('name')}", True))

    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _result(request_id, _content("arguments はオブジェクトで指定してください", True))

    try:
        if tool.get("turn"):
            result = client.call(tool["method"], _turn_params(arguments), wait=tool["wait"])
        else:
            result = client.call(tool["method"], arguments)
    except AppServerError as error:
        # ツール実行の失敗は JSON-RPC エラーではなく isError で返すのが MCP の作法。
        return _result(request_id, _content(str(error), True))
    return _result(request_id, _content(json.dumps(result, ensure_ascii=False, indent=2)))


def _turn_params(arguments: dict) -> dict:
    """turn/start の params。input は要素配列で渡す決まりなので text を包む。"""

    return {
        "threadId": arguments.get("threadId"),
        "input": [{"type": "text", "text": arguments.get("text")}],
    }


def serve(stdin, stdout, client, handler=None) -> None:
    """stdin の JSON 行を処理し続ける。stdin が閉じたら戻る。

    handler を差し替えると、同じ入出力の作りで別のツール群を公開できる。
    """

    handler = handler or handle
    for raw in stdin:
        if not raw.strip():
            continue
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            _write(stdout, _error(None, PARSE_ERROR, "JSON として解釈できません"))
            continue
        if not isinstance(message, dict):
            _write(stdout, _error(None, PARSE_ERROR, "JSON オブジェクトではありません"))
            continue
        response = handler(message, client)
        if response is not None:
            _write(stdout, response)


def _write(stdout, message: dict) -> None:
    stdout.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    stdout.flush()


def _content(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _result(request_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude_bridge.mcp_adapter",
        description="Codex app-server の RPC を MCP tool として公開する",
    )
    parser.add_argument(
        "--bridge-url",
        default=os.environ.get("CLAUDE_BRIDGE_URL") or DEFAULT_BRIDGE_URL,
        help="接続するブリッジ本体の URL",
    )
    args = parser.parse_args(argv)

    client = BridgeRpcClient(
        base_url=args.bridge_url, api_key=os.environ.get("CLAUDE_BRIDGE_API_KEY") or None
    )
    serve(sys.stdin.buffer, sys.stdout.buffer, client)
    return 0


if __name__ == "__main__":
    sys.exit(main())
