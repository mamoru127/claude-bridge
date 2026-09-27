"""Codex が送ってきたツールを、そのまま Claude へ見せる stdio MCP サーバー。

ツール定義は Codex のリクエスト由来なので、起動のたびに環境変数で受け取る。
Claude が呼ぶと、実行はせずブリッジ本体へ預けるだけで、受理した旨を返す。実行するのは
Codex 側で、結果は次のリクエストの function_call_output として会話へ戻ってくる。
"""

import json
import os
import sys
import urllib.error
import urllib.request

from .config import (
    DEFAULT_BRIDGE_URL,
    PASSTHROUGH_DEFS_ENV,
    PASSTHROUGH_PATH,
    PASSTHROUGH_SERVER_NAME,
    PASSTHROUGH_TOKEN_ENV,
)
from .mcp_adapter import MCP_PROTOCOL_VERSION, METHOD_NOT_FOUND, serve

SERVER_INFO = {"name": PASSTHROUGH_SERVER_NAME, "version": "0.1.0"}
# 預けるだけの HTTP なので、ブリッジが応答できない状態を長く待つ理由がない。
DEFAULT_TIMEOUT_SECONDS = 30.0


class PassthroughError(Exception):
    """呼び出しをブリッジへ預けられなかった。"""


class PendingCallClient:
    """Claude が呼んだツールを、ブリッジ本体の待ち行列へ預ける。"""

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BRIDGE_URL,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener=urllib.request.urlopen,
    ) -> None:
        self._token = token
        self._url = base_url.rstrip("/") + PASSTHROUGH_PATH
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def enqueue(
        self,
        name: str,
        arguments: dict,
        namespace: str | None = None,
        passthrough_type: str | None = None,
    ) -> str:
        """呼び出しを預け、Codex と対応づける call_id を受け取る。"""

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "token": self._token,
            "name": name,
            "arguments": arguments,
            "namespace": namespace,
            "passthrough_type": passthrough_type,
        }
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                return str(json.load(response)["result"]["call_id"])
        except urllib.error.HTTPError as error:
            raise PassthroughError(_http_error_message(error)) from error
        except (urllib.error.URLError, OSError, ValueError, LookupError) as error:
            raise PassthroughError(f"ブリッジへ接続できません: {self._url}: {error}") from error


def _http_error_message(error: urllib.error.HTTPError) -> str:
    try:
        return str(json.load(error)["error"]["message"])
    except (ValueError, LookupError, TypeError, OSError):
        return f"ブリッジがエラーを返しました: HTTP {error.code}"


def load_tools(raw: str) -> list[dict]:
    """環境変数の JSON から Codex のツール定義を読む。空なら公開するツールはない。"""

    if not raw.strip():
        return []
    tools = json.loads(raw)
    return [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]


def tool_definitions(tools: list[dict]) -> list[dict]:
    """Codex の function 定義を MCP のツール定義へ写す。

    引数スキーマは Codex が送ってきたものをそのまま使う。ブリッジが解釈すると、
    Codex 側の版が変わるたびに追随が必要になるため、中身には触れない。
    """

    return [
        {
            "name": tool["name"],
            "description": _description(tool),
            "inputSchema": tool.get("parameters") or {"type": "object", "properties": {}},
        }
        for tool in tools
    ]


def _description(tool: dict) -> str:
    """Claude へ見せる説明。実行が次のターンになることを必ず添える。"""

    return (
        f"{tool.get('description') or tool['name']}\n\n"
        "この操作は Codex 側で実行される。呼び出すと受理だけが返り、"
        "結果は次のターンに [Codex ツール結果] として届く。"
    )


def build_handler(tools: list[dict], client: PendingCallClient):
    """JSON-RPC メッセージ 1 件を処理する関数を作る。"""

    metadata = {
        tool["name"]: (tool.get("namespace"), tool.get("passthrough_type")) for tool in tools
    }

    def handle(message: dict, _client=None) -> dict | None:
        request_id = message.get("id")
        method = message.get("method")
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
            return _result(request_id, {"tools": tool_definitions(tools)})
        if method == "tools/call":
            return _call(request_id, message.get("params") or {}, metadata, client)
        return _error(request_id, METHOD_NOT_FOUND, f"未対応のメソッドです: {method}")

    return handle


def _call(
    request_id: object, params: object, metadata: dict, client: PendingCallClient
) -> dict:
    if not isinstance(params, dict):
        return _result(request_id, _content("params はオブジェクトで指定してください", True))
    name = params.get("name")
    if name not in metadata:
        return _result(request_id, _content(f"未対応のツールです: {name}", True))
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _result(request_id, _content("arguments はオブジェクトで指定してください", True))
    try:
        namespace, passthrough_type = metadata[name]
        call_id = client.enqueue(name, arguments, namespace, passthrough_type)
    except PassthroughError as error:
        return _result(request_id, _content(str(error), True))
    return _result(
        request_id,
        _content(
            f"{name} の実行を Codex へ依頼した（call_id={call_id}）。"
            "結果は次のターンに届くため、このターンでは待たずに進める。"
        ),
    )


def _content(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _result(request_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main() -> int:
    client = PendingCallClient(
        token=os.environ.get(PASSTHROUGH_TOKEN_ENV, ""),
        base_url=os.environ.get("CLAUDE_BRIDGE_URL") or DEFAULT_BRIDGE_URL,
        api_key=os.environ.get("CLAUDE_BRIDGE_API_KEY") or None,
    )
    tools = load_tools(os.environ.get(PASSTHROUGH_DEFS_ENV, ""))
    serve(sys.stdin.buffer, sys.stdout.buffer, client, build_handler(tools, client))
    return 0


if __name__ == "__main__":
    sys.exit(main())
