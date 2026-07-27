"""Codex のツールを Claude へ見せ、呼び出しを Codex へ返す受け渡しの検証。

ツール定義とツール名は本物の Codex リクエストを捕獲して確かめた形に合わせている。
Codex は top-level の tools ではなく input の additional_tools で送り、スレッド操作は
codex_app の namespace 配下に入る。呼び出しも名前を繋げず namespace を別項目で返す。
"""

import dataclasses
import json
import unittest
import urllib.request

from claude_bridge.claude_cli import build_mcp_servers
from claude_bridge.config import (
    PASSTHROUGH_DEFS_ENV,
    PASSTHROUGH_SERVER_NAME,
    PASSTHROUGH_TOKEN_ENV,
)
from claude_bridge.passthrough_adapter import (
    PassthroughError,
    build_handler,
    load_tools,
    tool_definitions,
)
from claude_bridge.responses import parse_request
from claude_bridge.sessions import PendingCalls

from tests.test_bridge import BridgeHTTPTestCase, FakeRunner, make_config

CREATE_THREAD = {
    "type": "function",
    "name": "create_thread",
    "description": "スレッドを作る",
    "parameters": {
        "type": "object",
        "properties": {"prompt": {"type": "string"}, "target": {"type": "object"}},
        "required": ["prompt", "target"],
    },
}
CODEX_APP = {
    "type": "namespace",
    "name": "codex_app",
    "description": "スレッドの操作",
    "tools": [CREATE_THREAD],
}
EXEC_COMMAND = {"type": "function", "name": "exec_command", "description": "実行"}
TOOL_SEARCH = {"type": "tool_search", "execution": "client", "description": "探索"}
APPLY_PATCH = {"type": "custom", "name": "apply_patch", "description": "差分適用"}
# 受け渡し後の create_thread。namespace は parse_request が添える。
NAMESPACED_CREATE = dict(CREATE_THREAD, namespace="codex_app")


def additional_tools(*tools: dict) -> dict:
    return {"type": "additional_tools", "role": "developer", "tools": list(tools)}


def tool_search_output(*tools: dict) -> dict:
    return {
        "type": "tool_search_output",
        "call_id": "call_search",
        "status": "completed",
        "execution": "client",
        "tools": list(tools),
    }


def payload_with(*tools: dict, text: str = "スレッドを作って") -> dict:
    return {
        "model": "gpt-5",
        "input": [
            {"type": "message", "role": "user", "content": text},
            additional_tools(*tools),
        ],
    }


class ParseToolsTest(unittest.TestCase):
    def test_reads_tools_from_additional_tools_item(self):
        payload = payload_with(EXEC_COMMAND, CODEX_APP)

        request = parse_request(payload, "gpt-5", ["create_thread"])

        self.assertEqual([tool["name"] for tool in request.tools], ["create_thread"])

    def test_namespaced_tool_carries_its_namespace(self):
        request = parse_request(payload_with(CODEX_APP), "gpt-5", ["create_thread"])

        self.assertEqual(request.tools[0]["namespace"], "codex_app")

    def test_top_level_function_has_no_namespace(self):
        request = parse_request(payload_with(EXEC_COMMAND), "gpt-5", ["exec_command"])

        self.assertNotIn("namespace", request.tools[0])

    def test_ignores_top_level_tools_key(self):
        """Codex は tools キーを送らない。読んでしまうと実在しない定義を写すことになる。"""

        payload = {"model": "gpt-5", "input": "やあ", "tools": [CREATE_THREAD]}

        self.assertEqual(parse_request(payload, "gpt-5", ["create_thread"]).tools, ())

    def test_duplicate_name_keeps_only_the_first_definition(self):
        """MCP のツール名は 1 意なので、同名が残ると渡す namespace が定まらない。"""

        other = {"type": "namespace", "name": "other", "tools": [CREATE_THREAD]}

        request = parse_request(payload_with(CODEX_APP, other), "gpt-5", ["create_thread"])

        self.assertEqual(len(request.tools), 1)
        self.assertEqual(request.tools[0]["namespace"], "codex_app")

    def test_keeps_nothing_without_allowlist(self):
        self.assertEqual(parse_request(payload_with(CODEX_APP), "gpt-5", []).tools, ())

    def test_ignores_entries_without_argument_schema(self):
        """custom と tool_search は引数スキーマを持たないため MCP へ写せない。"""

        payload = payload_with(TOOL_SEARCH, APPLY_PATCH)

        self.assertEqual(parse_request(payload, "gpt-5", ["apply_patch"]).tools, ())

    def test_reads_tools_from_tool_search_output(self):
        """遅延読み込みのツールは additional_tools ではなく検索結果として会話へ入る。"""

        payload = payload_with(TOOL_SEARCH)
        payload["input"].append(tool_search_output(CODEX_APP))

        request = parse_request(payload, "gpt-5", ["create_thread"], "スレッド")

        self.assertEqual(request.tools[0]["namespace"], "codex_app")
        self.assertIsNone(request.tool_search_query)

    def test_requests_search_when_allowed_tool_is_missing(self):
        request = parse_request(payload_with(TOOL_SEARCH), "gpt-5", ["create_thread"], "スレッド")

        self.assertEqual(request.tool_search_query, "スレッド")
        self.assertEqual(request.tools, ())

    def test_no_search_without_tool_search_entry(self):
        """tool_search を送ってこない相手に検索を返しても実行されない。"""

        request = parse_request(payload_with(EXEC_COMMAND), "gpt-5", ["create_thread"], "スレッド")

        self.assertIsNone(request.tool_search_query)

    def test_no_second_search_after_one_call(self):
        """取れなかった場合に投げ続けると、本題へ進まないまま会話が止まる。"""

        payload = payload_with(TOOL_SEARCH)
        payload["input"].append(
            {"type": "tool_search_call", "call_id": "call_1", "arguments": {"query": "スレッド"}}
        )

        request = parse_request(payload, "gpt-5", ["create_thread"], "スレッド")

        self.assertIsNone(request.tool_search_query)

    def test_no_search_when_query_is_empty(self):
        request = parse_request(payload_with(TOOL_SEARCH), "gpt-5", ["create_thread"], "")

        self.assertIsNone(request.tool_search_query)

    def test_tool_output_becomes_user_message(self):
        payload = {
            "input": [
                {"type": "message", "role": "user", "content": "起動して"},
                {"type": "function_call_output", "call_id": "call_1", "output": "起動しました"},
            ]
        }

        request = parse_request(payload, "gpt-5")

        self.assertEqual(request.messages[-1].role, "user")
        self.assertIn("call_id=call_1", request.messages[-1].parts[0])
        self.assertIn("起動しました", request.messages[-1].parts[0])

    def test_non_string_tool_output_is_serialised(self):
        payload = {
            "input": [
                {"type": "message", "role": "user", "content": "やあ"},
                {"type": "function_call_output", "call_id": "c", "output": {"id": 7}},
            ]
        }

        request = parse_request(payload, "gpt-5")

        self.assertIn('"id": 7', request.messages[-1].parts[0])


class McpServerTest(unittest.TestCase):
    def test_registers_passthrough_server_with_definitions(self):
        servers = build_mcp_servers(make_config(), (NAMESPACED_CREATE,), "tok")

        server = servers[PASSTHROUGH_SERVER_NAME]
        self.assertEqual(server["env"][PASSTHROUGH_TOKEN_ENV], "tok")
        self.assertEqual(json.loads(server["env"][PASSTHROUGH_DEFS_ENV]), [NAMESPACED_CREATE])

    def test_extra_server_cannot_replace_the_reserved_name(self):
        """同名の追加定義に差し替えられると、token を持たないサーバーが動いてしまう。"""

        config = dataclasses.replace(
            make_config(), extra_mcp_servers={PASSTHROUGH_SERVER_NAME: {"command": "偽物"}}
        )

        servers = build_mcp_servers(config, (NAMESPACED_CREATE,), "tok")

        self.assertEqual(servers[PASSTHROUGH_SERVER_NAME]["env"][PASSTHROUGH_TOKEN_ENV], "tok")

    def test_skips_server_without_tools_or_token(self):
        self.assertNotIn(PASSTHROUGH_SERVER_NAME, build_mcp_servers(make_config(), (), "tok"))
        self.assertNotIn(
            PASSTHROUGH_SERVER_NAME, build_mcp_servers(make_config(), (NAMESPACED_CREATE,), "")
        )


class FakeEnqueueClient:
    def __init__(self, call_id: str = "call_x", raises=None):
        self.call_id = call_id
        self.raises = raises
        self.calls = []

    def enqueue(self, name: str, arguments: dict, namespace: str | None = None) -> str:
        if self.raises:
            raise self.raises
        self.calls.append((name, arguments, namespace))
        return self.call_id


class AdapterTest(unittest.TestCase):
    def handle(self, message: dict, client=None):
        client = client or FakeEnqueueClient()
        return build_handler([NAMESPACED_CREATE], client)(message)

    def test_lists_tools_from_codex_schema(self):
        definitions = tool_definitions([NAMESPACED_CREATE])

        self.assertEqual(definitions[0]["name"], "create_thread")
        self.assertEqual(definitions[0]["inputSchema"], CREATE_THREAD["parameters"])
        self.assertIn("次のターン", definitions[0]["description"])

    def test_tool_without_schema_gets_empty_object(self):
        self.assertEqual(
            tool_definitions([EXEC_COMMAND])[0]["inputSchema"],
            {"type": "object", "properties": {}},
        )

    def test_load_tools_ignores_blank_and_nameless(self):
        self.assertEqual(load_tools(""), [])
        self.assertEqual(load_tools('[{"description": "名前なし"}]'), [])

    def test_call_enqueues_with_namespace_and_reports_call_id(self):
        client = FakeEnqueueClient("call_7")

        response = self.handle(
            {"id": 1, "method": "tools/call",
             "params": {"name": "create_thread", "arguments": {"prompt": "やって"}}},
            client,
        )

        self.assertEqual(client.calls, [("create_thread", {"prompt": "やって"}, "codex_app")])
        self.assertIn("call_7", response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])

    def test_call_without_namespace_passes_none(self):
        client = FakeEnqueueClient()

        build_handler([EXEC_COMMAND], client)(
            {"id": 1, "method": "tools/call", "params": {"name": "exec_command"}}
        )

        self.assertEqual(client.calls, [("exec_command", {}, None)])

    def test_unknown_tool_is_reported_as_error(self):
        response = self.handle({"id": 1, "method": "tools/call", "params": {"name": "nope"}})

        self.assertTrue(response["result"]["isError"])

    def test_enqueue_failure_is_reported_as_error(self):
        response = self.handle(
            {"id": 1, "method": "tools/call", "params": {"name": "create_thread"}},
            FakeEnqueueClient(raises=PassthroughError("預けられません")),
        )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("預けられません", response["result"]["content"][0]["text"])

    def test_notification_gets_no_response(self):
        self.assertIsNone(self.handle({"method": "notifications/initialized"}))


class PendingCallsTest(unittest.TestCase):
    def test_collects_calls_until_closed(self):
        pending = PendingCalls()
        token = pending.open()

        call_id = pending.add(token, "create_thread", {"prompt": "やって"}, "codex_app")
        calls = pending.close(token)

        self.assertEqual(calls[0]["call_id"], call_id)
        self.assertEqual(calls[0]["name"], "create_thread")
        self.assertEqual(calls[0]["namespace"], "codex_app")
        self.assertEqual(pending.close(token), [])

    def test_call_without_namespace_omits_the_field(self):
        pending = PendingCalls()
        token = pending.open()

        pending.add(token, "exec_command", {})

        self.assertNotIn("namespace", pending.close(token)[0])

    def test_add_after_close_is_rejected(self):
        pending = PendingCalls()
        token = pending.open()
        pending.close(token)

        with self.assertRaises(KeyError):
            pending.add(token, "create_thread", {})


class CallingRunner(FakeRunner):
    """Claude が受け渡しツールを 1 回呼ぶ様子を再現するランナー。"""

    def __init__(self, base_url: str, arguments: dict, **kwargs):
        super().__init__(**kwargs)
        self.base_url = base_url
        self.arguments = arguments
        self.enqueued = []

    def __call__(self, command, **kwargs):
        token = self._token(command)
        if token:
            self.enqueued.append(self._enqueue(token))
        return super().__call__(command, **kwargs)

    def _token(self, command: list) -> str:
        """--mcp-config へ書かれた受け渡しサーバーの token を取り出す。"""

        if "--mcp-config" not in command:
            return ""
        servers = json.loads(command[command.index("--mcp-config") + 1])["mcpServers"]
        server = servers.get(PASSTHROUGH_SERVER_NAME)
        return server["env"][PASSTHROUGH_TOKEN_ENV] if server else ""

    def _enqueue(self, token: str) -> dict:
        request = urllib.request.Request(
            self.base_url + "/codex-tool/call",
            data=json.dumps(
                {
                    "token": token,
                    "name": "create_thread",
                    "arguments": self.arguments,
                    "namespace": "codex_app",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)["result"]


class PassthroughEndpointTest(BridgeHTTPTestCase):
    """Claude の呼び出しが function_call として Codex へ返るところまで通す。"""

    def start(self, arguments: dict):
        # ランナーは自分が動くサーバーへ預けるため、起動後に接続先を教える。
        runner = CallingRunner("", arguments)
        base_url = self.start_server(make_config(), runner)
        runner.base_url = base_url
        return base_url, runner

    def request_payload(self):
        return payload_with(CODEX_APP)

    def test_json_response_carries_namespaced_function_call(self):
        base_url, runner = self.start({"prompt": "やって"})

        status, payload = self.post(base_url, self.request_payload())

        self.assertEqual(status, 200)
        calls = [item for item in payload["output"] if item["type"] == "function_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "create_thread")
        self.assertEqual(calls[0]["namespace"], "codex_app")
        self.assertEqual(json.loads(calls[0]["arguments"]), {"prompt": "やって"})
        self.assertEqual(calls[0]["call_id"], runner.enqueued[0]["call_id"])

    def test_stream_emits_function_call_item(self):
        base_url, _ = self.start({"prompt": "やって"})

        _, events = self.post_stream(base_url, self.request_payload())

        items = [
            payload["item"]
            for name, payload in events
            if name == "response.output_item.done" and payload["item"]["type"] == "function_call"
        ]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "create_thread")
        self.assertEqual(items[0]["namespace"], "codex_app")

    def test_no_passthrough_server_without_tools(self):
        base_url, runner = self.start({"prompt": "やって"})

        status, payload = self.post(base_url, {"model": "gpt-5", "input": "やあ"})

        self.assertEqual(status, 200)
        self.assertEqual(runner.enqueued, [])
        self.assertEqual([item["type"] for item in payload["output"]], ["message"])

    def test_call_with_unknown_token_is_rejected(self):
        base_url, _ = self.start({"prompt": "やって"})

        status, payload = self.post(
            base_url,
            {"token": "missing", "name": "create_thread", "arguments": {}},
            path="/codex-tool/call",
        )

        self.assertEqual(status, 400)
        self.assertIn("預かり口", payload["error"]["message"])

    def test_non_string_namespace_is_rejected(self):
        base_url, _ = self.start({"prompt": "やって"})

        status, _ = self.post(
            base_url,
            {"token": "t", "name": "create_thread", "arguments": {}, "namespace": 7},
            path="/codex-tool/call",
        )

        self.assertEqual(status, 400)


class ToolSearchEndpointTest(BridgeHTTPTestCase):
    """遅延読み込みのツールを引き出す検索が、Claude を動かさずに返るところまで通す。"""

    def start(self):
        runner = FakeRunner()
        config = make_config(
            passthrough_tools=("create_thread",), tool_search_query="スレッド操作"
        )
        return self.start_server(config, runner), runner

    def test_json_response_carries_tool_search_call(self):
        base_url, runner = self.start()

        status, payload = self.post(base_url, payload_with(TOOL_SEARCH))

        self.assertEqual(status, 200)
        self.assertEqual([item["type"] for item in payload["output"]], ["tool_search_call"])
        item = payload["output"][0]
        self.assertEqual(item["arguments"], {"query": "スレッド操作"})
        self.assertEqual(item["execution"], "client")
        # 検索だけのターンでは CLI を起動しない。動かすと本文のないターンが 1 回無駄になる。
        self.assertEqual(runner.calls, [])

    def test_stream_emits_tool_search_call_and_completes(self):
        base_url, runner = self.start()

        _, events = self.post_stream(base_url, payload_with(TOOL_SEARCH))

        names = [name for name, _ in events]
        self.assertIn("response.completed", names)
        items = [
            payload["item"]
            for name, payload in events
            if name == "response.output_item.done"
        ]
        self.assertEqual([item["type"] for item in items], ["tool_search_call"])
        self.assertEqual(runner.calls, [])

    def test_runs_claude_once_the_tool_is_found(self):
        base_url, runner = self.start()
        payload = payload_with(TOOL_SEARCH)
        payload["input"].append(tool_search_output(CODEX_APP))

        status, body = self.post(base_url, payload)

        self.assertEqual(status, 200)
        self.assertEqual([item["type"] for item in body["output"]], ["message"])
        self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
