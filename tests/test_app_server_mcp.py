"""Codex app-server MCP アダプタのテスト。

実際の `codex` も Claude CLI も起動しない。app-server への接続はすべて差し替える。
turn/start は公開しているが、テストでは差し替えた接続が応答するだけで
外部サービスは呼ばない。
"""

import io
import json
import os
import queue
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import claude_bridge
from claude_bridge import mcp_adapter
from claude_bridge.app_server import AppServerClient, AppServerError
from claude_bridge.claude_cli import build_command, build_mcp_servers
from claude_bridge.config import APP_SERVER_PATH, MCP_SERVER_NAME, BridgeConfig, build_config
from claude_bridge.server import BridgeServer
from claude_bridge.sessions import Session

# responder がこれを返すと、接続が閉じたことになる。
EOF = object()
# 差し替えた接続では実際には使わないが、繋ぎ先は必ず指定する。
SOCKET = "/tmp/claude-bridge-test/app-server-control.sock"


def make_config(**overrides) -> BridgeConfig:
    defaults = {"host": "127.0.0.1", "port": 0, "model": "opus", "working_dir": "."}
    return BridgeConfig(**{**defaults, **overrides})


class FakeTransport:
    """制御ソケット接続の差し替え。送られた要求を responder に渡し、応答を受け取り側へ流す。"""

    def __init__(self, responder):
        self.sent = []
        self.closed = 0
        self._responder = responder
        self._incoming: queue.Queue = queue.Queue()

    def send(self, text: str) -> None:
        message = json.loads(text)
        self.sent.append(message)
        for reply in self._responder(message):
            if reply is EOF:
                self._incoming.put(None)
            elif isinstance(reply, str):
                self._incoming.put(reply)
            else:
                self._incoming.put(json.dumps(reply))

    def messages(self):
        while True:
            text = self._incoming.get()
            if text is None:
                return
            yield text

    def close(self) -> None:
        self.closed += 1
        self._incoming.put(None)


def echo_responder(results: dict):
    """id 付きの要求に results[method] を返し、通知には何も返さない responder。"""

    def respond(message):
        if "id" not in message:
            return []
        method = message["method"]
        if method not in results:
            return [{"id": message["id"], "error": {"code": -32601, "message": method}}]
        return [{"id": message["id"], "result": results[method]}]

    return respond


class FakeConnect:
    """UdsWebSocket の差し替え。繋ぎ先のソケットパスを記録する。"""

    def __init__(self, responder=None, raises=None):
        self.responder = responder or echo_responder({"initialize": {"codexHome": "/tmp"}})
        self.raises = raises
        self.calls = []
        self.transports = []

    def __call__(self, socket_path):
        self.calls.append(socket_path)
        if self.raises:
            raise self.raises
        transport = FakeTransport(self.responder)
        self.transports.append(transport)
        return transport


class FakeClient:
    """BridgeRpcClient の差し替え。転送された method/params と待機指定を記録する。"""

    def __init__(self, result=None, raises=None):
        self.result = {"data": []} if result is None else result
        self.raises = raises
        self.calls = []
        self.waits = []

    def call(self, method, params, wait=None):
        self.calls.append((method, params))
        self.waits.append(wait)
        if self.raises:
            raise self.raises
        return self.result


def run_in_thread(target) -> queue.Queue:
    """戻り値も例外も箱へ入れるスレッド。テスト終了後の時間切れでログを汚さない。"""

    box: queue.Queue = queue.Queue()

    def run() -> None:
        try:
            box.put(target())
        except BaseException as error:  # noqa: BLE001
            box.put(error)

    threading.Thread(target=run, daemon=True).start()
    return box


def call_tool(client, name, arguments=None, request_id=7):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return mcp_adapter.handle(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}, client
    )


class McpProtocolTest(unittest.TestCase):
    def test_initialize_returns_tool_capability(self):
        response = mcp_adapter.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, FakeClient()
        )

        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        result = response["result"]
        self.assertEqual(result["protocolVersion"], mcp_adapter.MCP_PROTOCOL_VERSION)
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["serverInfo"]["name"], MCP_SERVER_NAME)

    def test_initialize_does_not_connect(self):
        # ハンドシェイクだけでは繋がない。tools/call で初めて接続する。
        connect = FakeConnect()
        client = AppServerClient(SOCKET, connect=connect)

        mcp_adapter.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, client)

        self.assertEqual(connect.calls, [])

    def test_notifications_get_no_response(self):
        for method in ("notifications/initialized", "notifications/cancelled"):
            with self.subTest(method=method):
                message = {"jsonrpc": "2.0", "method": method}
                self.assertIsNone(mcp_adapter.handle(message, FakeClient()))

    def test_tools_list_exposes_thread_rpcs(self):
        response = mcp_adapter.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, FakeClient()
        )
        tools = response["result"]["tools"]

        self.assertEqual(
            [tool["name"] for tool in tools],
            [
                "create_thread",
                "thread_start",
                "thread_name_set",
                "turn_start",
                "turn_start_async",
                "thread_list",
                "thread_read",
                "thread_turns_list",
                "thread_search",
            ],
        )
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["description"])
                self.assertEqual(tool["inputSchema"]["type"], "object")
                self.assertIsInstance(tool["inputSchema"]["properties"], dict)
                self.assertIsInstance(tool["inputSchema"]["required"], list)

    def test_read_only_tools_declare_required_arguments(self):
        schemas = {
            tool["name"]: tool["inputSchema"] for tool in mcp_adapter.tool_definitions()
        }

        self.assertEqual(schemas["thread_read"]["required"], ["threadId"])
        self.assertEqual(schemas["thread_turns_list"]["required"], ["threadId"])
        self.assertEqual(schemas["thread_search"]["required"], ["searchTerm"])
        self.assertEqual(schemas["create_thread"]["required"], ["cwd", "title", "prompt"])
        # thread/start は必須引数なしで呼べる。
        self.assertEqual(schemas["thread_start"]["required"], [])
        self.assertEqual(schemas["turn_start"]["required"], ["threadId", "text"])
        self.assertEqual(schemas["thread_name_set"]["required"], ["threadId", "name"])

    def test_unsupported_rpcs_are_not_exposed(self):
        # thread/items/list は app-server が未実装。呼べば必ず失敗するので公開しない。
        self.assertNotIn(
            "thread/items/list", {tool.get("method") for tool in mcp_adapter.TOOLS}
        )

    def test_destructive_rpcs_are_not_exposed(self):
        # 破壊的操作は公開しない。
        methods = {tool.get("method") for tool in mcp_adapter.TOOLS}
        for absent in ("turn/steer", "thread/delete", "command/exec"):
            self.assertNotIn(absent, methods)

    def test_only_turn_start_runs_a_turn(self):
        # turn 扱いにするのは turn/start を投げる 2 つだけ。他は素の call で転送する。
        turn_tools = {tool["name"]: tool["wait"] for tool in mcp_adapter.TOOLS if tool.get("turn")}
        self.assertEqual(turn_tools, {"turn_start": True, "turn_start_async": False})

    def test_unknown_method_returns_method_not_found(self):
        response = mcp_adapter.handle(
            {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}, FakeClient()
        )

        self.assertEqual(response["error"]["code"], mcp_adapter.METHOD_NOT_FOUND)
        self.assertNotIn("result", response)


class McpToolCallTest(unittest.TestCase):
    def test_tool_call_forwards_method_and_arguments(self):
        client = FakeClient(result={"data": [{"id": "th_1"}]})

        response = call_tool(client, "thread_list", {"limit": 2})

        self.assertEqual(client.calls, [("thread/list", {"limit": 2})])
        content = response["result"]["content"][0]
        self.assertEqual(content["type"], "text")
        self.assertEqual(json.loads(content["text"]), {"data": [{"id": "th_1"}]})
        self.assertFalse(response["result"]["isError"])

    def test_thread_start_is_forwarded_verbatim(self):
        client = FakeClient(result={"threadId": "th_new"})

        response = call_tool(client, "thread_start", {"cwd": "/tmp", "model": "opus"})

        self.assertEqual(client.calls, [("thread/start", {"cwd": "/tmp", "model": "opus"})])
        self.assertIn("th_new", response["result"]["content"][0]["text"])

    def test_create_thread_creates_names_and_starts_without_waiting(self):
        client = mock.Mock()
        client.call.side_effect = [
            {"thread": {"id": "th_new"}},
            {},
            {"threadId": "th_new", "turn": {"id": "tn_new"}},
        ]

        response = call_tool(
            client,
            "create_thread",
            {
                "cwd": "/tmp/project",
                "title": "調査タスク",
                "prompt": "調べてください",
                "model": "gpt-5.6-sol",
            },
        )

        self.assertEqual(
            client.call.call_args_list,
            [
                mock.call("thread/start", {"cwd": "/tmp/project", "model": "gpt-5.6-sol"}),
                mock.call("thread/name/set", {"threadId": "th_new", "name": "調査タスク"}),
                mock.call(
                    "turn/start",
                    {
                        "threadId": "th_new",
                        "input": [{"type": "text", "text": "調べてください"}],
                    },
                    wait=False,
                ),
            ],
        )
        self.assertEqual(
            json.loads(response["result"]["content"][0]["text"]),
            {"threadId": "th_new", "turn": {"id": "tn_new"}},
        )
        self.assertFalse(response["result"]["isError"])

    def test_create_thread_rejects_missing_thread_id(self):
        client = FakeClient(result={"thread": {}})

        response = call_tool(
            client,
            "create_thread",
            {"cwd": "/tmp/project", "title": "調査", "prompt": "調べて"},
        )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("thread.id", response["result"]["content"][0]["text"])
        self.assertEqual(client.calls, [("thread/start", {"cwd": "/tmp/project"})])

    def test_missing_arguments_become_empty_params(self):
        client = FakeClient()

        call_tool(client, "thread_list")

        self.assertEqual(client.calls, [("thread/list", {})])

    def test_turn_start_wraps_the_text_into_input_items(self):
        client = FakeClient(result={"agentMessage": "こんばんは"})

        response = call_tool(client, "turn_start", {"threadId": "th_1", "text": "こんばんは"})

        self.assertEqual(
            client.calls,
            [("turn/start", {"threadId": "th_1", "input": [{"type": "text", "text": "こんばんは"}]})],
        )
        # 完了まで待つ側なので wait を立てて転送する。
        self.assertEqual(client.waits, [True])
        self.assertIn("こんばんは", response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])

    def test_turn_start_async_does_not_wait_for_completion(self):
        client = FakeClient(result={"threadId": "th_1", "turn": {"turnId": "tn_1"}})

        response = call_tool(client, "turn_start_async", {"threadId": "th_1", "text": "やって"})

        self.assertEqual(
            client.calls,
            [("turn/start", {"threadId": "th_1", "input": [{"type": "text", "text": "やって"}]})],
        )
        # 待たない指定で転送する。ここが実行をアダプタの寿命から切り離す要。
        self.assertEqual(client.waits, [False])
        self.assertIn("tn_1", response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])

    def test_turn_start_failure_is_reported_as_tool_error(self):
        client = FakeClient(raises=AppServerError("ターンが失敗しました"))

        response = call_tool(client, "turn_start", {"threadId": "th_1", "text": "hi"})

        self.assertTrue(response["result"]["isError"])
        self.assertNotIn("error", response)

    def test_unknown_tool_is_reported_as_tool_error(self):
        client = FakeClient()

        response = call_tool(client, "thread_delete")

        self.assertTrue(response["result"]["isError"])
        self.assertNotIn("error", response)
        self.assertEqual(client.calls, [])

    def test_non_object_arguments_are_rejected(self):
        client = FakeClient()

        response = call_tool(client, "thread_list", ["limit"])

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(client.calls, [])

    def test_non_object_params_are_rejected(self):
        # params が dict 以外（文字列・配列・数値）でも落ちずに tool error を返す。
        for params in ("thread_list", ["thread_list"], 5, True):
            with self.subTest(params=params):
                client = FakeClient()
                response = mcp_adapter.handle(
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": params}, client
                )

                self.assertEqual(response["id"], 4)
                self.assertTrue(response["result"]["isError"])
                self.assertNotIn("error", response)
                self.assertEqual(client.calls, [])

    def test_missing_params_are_treated_as_empty(self):
        # params なし・null・空 dict は「ツール名なし」として扱う既存挙動を維持する。
        for message in (
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call"},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": None},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {}},
        ):
            with self.subTest(params=message.get("params")):
                client = FakeClient()
                response = mcp_adapter.handle(message, client)

                self.assertTrue(response["result"]["isError"])
                self.assertEqual(client.calls, [])

    def test_non_object_params_over_stdio_keeps_serving(self):
        # dict 以外の params で MCP プロセス全体が停止しないこと。
        client = FakeClient()
        raw = (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":"thread_list"}\n'
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        )
        stdout = io.BytesIO()

        mcp_adapter.serve(io.BytesIO(raw), stdout, client)

        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2])
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertIn("tools", responses[1]["result"])
        self.assertEqual(client.calls, [])

    def test_app_server_failure_is_reported_as_tool_error(self):
        client = FakeClient(raises=AppServerError("codex app-server が応答しません"))

        response = call_tool(client, "thread_read", {"threadId": "th_1"})

        self.assertTrue(response["result"]["isError"])
        self.assertIn("codex app-server", response["result"]["content"][0]["text"])


class McpServeLoopTest(unittest.TestCase):
    def serve(self, raw: bytes, client=None):
        stdout = io.BytesIO()
        mcp_adapter.serve(io.BytesIO(raw), stdout, client or FakeClient())
        return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]

    def test_handshake_then_tool_call_over_stdio(self):
        client = FakeClient(result={"data": []})
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "thread_list", "arguments": {}},
            },
        ]
        raw = b"".join((json.dumps(line) + "\n").encode("utf-8") for line in lines)

        responses = self.serve(raw, client)

        # 通知には応答しないので 3 件。
        self.assertEqual([response["id"] for response in responses], [1, 2, 3])
        self.assertEqual(client.calls, [("thread/list", {})])

    def test_blank_lines_are_skipped(self):
        raw = b'\n\n{"jsonrpc":"2.0","id":1,"method":"initialize"}\n\n'

        self.assertEqual([response["id"] for response in self.serve(raw)], [1])

    def test_broken_json_line_returns_parse_error_and_keeps_serving(self):
        raw = b'{oops\n{"jsonrpc":"2.0","id":9,"method":"tools/list"}\n'

        responses = self.serve(raw)

        self.assertEqual(responses[0]["error"]["code"], mcp_adapter.PARSE_ERROR)
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[1]["id"], 9)

    def test_non_object_json_line_returns_parse_error(self):
        responses = self.serve(b'["tools/list"]\n')

        self.assertEqual(responses[0]["error"]["code"], mcp_adapter.PARSE_ERROR)


class AppServerClientTest(unittest.TestCase):
    def test_connects_to_the_configured_control_socket(self):
        connect = FakeConnect(echo_responder({"initialize": {}, "thread/list": {"data": []}}))
        client = AppServerClient("/tmp/probe/app-server-control.sock", connect=connect)

        client.call("thread/list", {"limit": 1})

        # 常駐している app-server へ繋ぐだけで、自分では起動しない。
        self.assertEqual(connect.calls, ["/tmp/probe/app-server-control.sock"])

    def test_socket_is_shared_across_calls(self):
        connect = FakeConnect(echo_responder({"initialize": {}, "thread/list": {"data": []}}))
        client = AppServerClient(SOCKET, connect=connect)

        client.call("thread/list", {})
        client.call("thread/list", {})

        # 繋ぎ直すとアプリ側から見た状態も途切れる。1 本を使い回す。
        self.assertEqual(len(connect.calls), 1)

    def test_handshake_precedes_the_first_call(self):
        connect = FakeConnect(echo_responder({"initialize": {}, "thread/list": {"data": []}}))
        client = AppServerClient(SOCKET, connect=connect)

        client.call("thread/list", {})

        sent = connect.transports[0].sent
        self.assertEqual(
            [message["method"] for message in sent],
            ["initialize", "initialized", "thread/list"],
        )
        self.assertEqual(sent[0]["params"]["clientInfo"]["name"], "claude-bridge")
        # thread/turns/list はこの宣言がないと -32600 で拒否される。
        self.assertEqual(sent[0]["params"]["capabilities"], {"experimentalApi": True})
        # 通知には id を付けない。
        self.assertNotIn("id", sent[1])
        self.assertEqual(sent[2]["params"], {})

    def test_process_is_reused_across_calls(self):
        connect = FakeConnect(
            echo_responder({"initialize": {}, "thread/list": {"data": []}, "thread/read": {}})
        )
        client = AppServerClient(SOCKET, connect=connect)

        client.call("thread/list", {})
        client.call("thread/read", {"threadId": "th_1"})

        self.assertEqual(len(connect.calls), 1)
        methods = [message["method"] for message in connect.transports[0].sent]
        self.assertEqual(methods, ["initialize", "initialized", "thread/list", "thread/read"])

    def test_request_ids_are_unique_and_matched(self):
        connect = FakeConnect(echo_responder({"initialize": {}, "thread/list": {"data": []}}))
        client = AppServerClient(SOCKET, connect=connect)

        client.call("thread/list", {})
        client.call("thread/list", {})

        ids = [message["id"] for message in connect.transports[0].sent if "id" in message]
        self.assertEqual(ids, [1, 2, 3])

    def test_notifications_and_server_requests_are_skipped(self):
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                return [{"id": message["id"], "result": {}}]
            return [
                {"method": "remoteControl/status/changed", "params": {"status": "disabled"}},
                {"id": 999, "method": "loginChatGptComplete", "params": {}},
                {"id": message["id"], "result": {"data": ["ok"]}},
            ]

        client = AppServerClient(SOCKET, connect=FakeConnect(responder))

        self.assertEqual(client.call("thread/list", {}), {"data": ["ok"]})

    def test_run_turn_waits_for_completion_and_joins_the_deltas(self):
        # turn/start の応答は即返り、本文と完了は通知で届く。
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                return [{"id": message["id"], "result": {}}]
            return [
                {"id": message["id"], "result": {"turn": {"id": "tu_1", "status": "inProgress"}}},
                {"method": "turn/started", "params": {"threadId": "th_1"}},
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "th_1", "delta": "こん"},
                },
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "th_1", "delta": "ばんは"},
                },
                {
                    "method": "turn/completed",
                    "params": {"threadId": "th_1", "turn": {"id": "tu_1", "status": "completed"}},
                },
            ]

        client = AppServerClient(SOCKET, connect=FakeConnect(responder))

        result = client.run_turn({"threadId": "th_1", "input": []})

        self.assertEqual(result["threadId"], "th_1")
        self.assertEqual(result["agentMessage"], "こんばんは")
        self.assertEqual(result["turn"], {"id": "tu_1", "status": "completed"})
        self.assertFalse(result["failed"])

    def test_start_turn_returns_without_waiting_for_completion(self):
        # 受理応答だけ返し、turn/completed は待たない。ターンは app-server 側で走り続ける。
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                return [{"id": message["id"], "result": {}}]
            return [{"id": message["id"], "result": {"turn": {"id": "tu_1", "status": "inProgress"}}}]

        client = AppServerClient(SOCKET, connect=FakeConnect(responder))

        result = client.start_turn({"threadId": "th_1", "input": []})

        self.assertEqual(
            result, {"threadId": "th_1", "turn": {"id": "tu_1", "status": "inProgress"}}
        )

    def test_a_waiting_turn_does_not_block_other_rpcs(self):
        # 完了通知を送らないターンを待っている間も、別の呼び出しは通る。
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "turn/start":
                return [{"id": message["id"], "result": {"turn": {"id": "tu_1"}}}]
            return [{"id": message["id"], "result": {"data": ["ok"]}}]

        client = AppServerClient(SOCKET, connect=FakeConnect(responder), turn_timeout_seconds=5.0)
        turn = run_in_thread(lambda: client.run_turn({"threadId": "th_1", "input": []}))

        # ターンは待機したまま。ロックを握っていれば、ここで turn 側の時間切れまで止まる。
        self.assertEqual(client.call("thread/list", {}), {"data": ["ok"]})
        self.assertTrue(turn.empty())

    def test_notifications_without_a_waiter_are_dropped(self):
        # 待たずに始めたターンの通知は誰も読まない。溜め込まずに捨てる。
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "turn/start":
                return [
                    {"id": message["id"], "result": {"turn": {"id": "tu_1"}}},
                    {
                        "method": "item/agentMessage/delta",
                        "params": {"threadId": "th_1", "delta": "経過"},
                    },
                    {"method": "turn/completed", "params": {"threadId": "th_1", "turn": {}}},
                ]
            return [{"id": message["id"], "result": {"data": ["ok"]}}]

        client = AppServerClient(SOCKET, connect=FakeConnect(responder))

        client.start_turn({"threadId": "th_1", "input": []})
        # 捨てた通知が次の応答に混ざらないことを、続く呼び出しで確かめる。
        self.assertEqual(client.call("thread/list", {}), {"data": ["ok"]})

    def test_waiting_twice_on_the_same_thread_is_rejected(self):
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "turn/start":
                return [{"id": message["id"], "result": {"turn": {"id": "tu_1"}}}]
            return [{"id": message["id"], "result": {}}]

        connect = FakeConnect(responder)
        client = AppServerClient(SOCKET, connect=connect, turn_timeout_seconds=5.0)
        run_in_thread(lambda: client.run_turn({"threadId": "th_1", "input": []}))
        # 受け口は turn/start を送る前に用意されるので、送信が見えた時点で待機中。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            sent = [m["method"] for m in connect.transports[0].sent] if connect.transports else []
            if "turn/start" in sent:
                break

        with self.assertRaises(AppServerError) as raised:
            client.run_turn({"threadId": "th_1", "input": []})

        self.assertIn("すでに待機中", str(raised.exception))

    def test_run_turn_ignores_notifications_from_another_thread(self):
        # 待たずに始めた別スレッドのターンが同じ stdio へ通知を流しても取り違えない。
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                return [{"id": message["id"], "result": {}}]
            return [
                {"id": message["id"], "result": {"turn": {"id": "tu_2"}}},
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "th_other", "delta": "混ざる"},
                },
                {
                    "method": "turn/completed",
                    "params": {"threadId": "th_other", "turn": {"id": "tu_1"}},
                },
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "th_2", "delta": "正しい"},
                },
                {
                    "method": "turn/completed",
                    "params": {"threadId": "th_2", "turn": {"id": "tu_2"}},
                },
            ]

        client = AppServerClient(SOCKET, connect=FakeConnect(responder))

        result = client.run_turn({"threadId": "th_2", "input": []})

        self.assertEqual(result["threadId"], "th_2")
        self.assertEqual(result["agentMessage"], "正しい")
        self.assertEqual(result["turn"], {"id": "tu_2"})

    def test_run_turn_reports_a_failed_turn(self):
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                return [{"id": message["id"], "result": {}}]
            return [
                {"id": message["id"], "result": {"turn": {"id": "tu_1"}}},
                {"method": "turn/failed", "params": {"threadId": "th_1", "turn": {"id": "tu_1"}}},
            ]

        client = AppServerClient(SOCKET, connect=FakeConnect(responder))

        self.assertTrue(client.run_turn({"threadId": "th_1", "input": []})["failed"])

    def test_run_turn_rejects_an_error_response(self):
        connect = FakeConnect(echo_responder({"initialize": {}}))
        client = AppServerClient(SOCKET, connect=connect)

        with self.assertRaises(AppServerError) as raised:
            client.run_turn({"threadId": "th_1", "input": []})

        self.assertIn("turn/start", str(raised.exception))
        # 受理されなかったのはこのターンだけ。接続と他のターンは残す。
        self.assertEqual(connect.transports[0].closed, 0)

    def test_run_turn_uses_the_turn_timeout(self):
        # 完了通知を送らない app-server。turn 用の待ち時間で切れる。
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                return [{"id": message["id"], "result": {}}]
            return [{"id": message["id"], "result": {"turn": {}}}]

        client = AppServerClient(
            SOCKET, connect=FakeConnect(responder), timeout_seconds=30.0, turn_timeout_seconds=0.2
        )

        with self.assertRaises(AppServerError) as raised:
            client.run_turn({"threadId": "th_1", "input": []})

        self.assertIn("0.2", str(raised.exception))

    def test_rpc_error_becomes_app_server_error(self):
        connect = FakeConnect(echo_responder({"initialize": {}}))
        client = AppServerClient(SOCKET, connect=connect)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/read", {"threadId": "missing"})

        self.assertIn("thread/read", str(raised.exception))

    def test_disconnect_before_response_is_reported(self):
        # initialize には答えるが、次の要求では応答せず切れる app-server。
        def responder(message):
            if message.get("method") == "initialize":
                return [{"id": message["id"], "result": {}}]
            return [EOF]

        client = AppServerClient(SOCKET, connect=FakeConnect(responder), timeout_seconds=5.0)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/list", {})

        self.assertIn("接続が切れました", str(raised.exception))

    def test_timeout_is_reported(self):
        # initialize にも答えない app-server。最初の呼び出しでタイムアウトする。
        client = AppServerClient(SOCKET, connect=FakeConnect(lambda message: []), timeout_seconds=0.2)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/list", {})

        self.assertIn("応答しません", str(raised.exception))

    def test_connect_failure_is_reported(self):
        # app-server が常駐していなければソケットが無い。繋げないことを隠さず伝える。
        connect = FakeConnect(raises=FileNotFoundError())
        client = AppServerClient("/no/such/app-server.sock", connect=connect)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/list", {})

        self.assertIn("/no/such/app-server.sock", str(raised.exception))

    def test_non_json_output_is_reported(self):
        # JSON でないテキストを送ってくる app-server（文字列はそのまま受け取り側へ流れる）。
        client = AppServerClient(SOCKET, connect=FakeConnect(lambda message: ["not json"]))

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/list", {})

        self.assertIn("JSON", str(raised.exception))

    def test_close_disconnects_without_stopping_the_app_server(self):
        connect = FakeConnect(echo_responder({"initialize": {}, "thread/list": {}}))
        client = AppServerClient(SOCKET, connect=connect)
        client.call("thread/list", {})

        client.close()

        # 閉じるのは自分の接続だけ。app-server は常駐したままにする。
        self.assertEqual(connect.transports[0].closed, 1)

    def test_close_before_start_is_a_no_op(self):
        connect = FakeConnect()
        AppServerClient(SOCKET, connect=connect).close()

        self.assertEqual(connect.calls, [])

    def test_close_is_idempotent(self):
        connect = FakeConnect(echo_responder({"initialize": {}, "thread/list": {}}))
        client = AppServerClient(SOCKET, connect=connect)
        client.call("thread/list", {})

        client.close()
        client.close()

        self.assertEqual(connect.transports[0].closed, 1)

    def test_mcp_adapter_close_disconnects_after_serving(self):
        # mcp_adapter.main の finally 相当。後始末で接続を残さない。
        connect = FakeConnect(echo_responder({"initialize": {}, "thread/list": {}}))
        client = AppServerClient(SOCKET, connect=connect)
        raw = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"thread_list"}}\n'
        try:
            mcp_adapter.serve(io.BytesIO(raw), io.BytesIO(), client)
        finally:
            client.close()

        self.assertEqual(connect.transports[0].closed, 1)

    def test_failed_handshake_is_not_reused_and_restarts_next_call(self):
        attempts = []

        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                attempts.append(message["method"])
                # 1 回目のハンドシェイクだけ失敗させる。
                if len(attempts) == 1:
                    return [{"id": message["id"], "error": {"code": -32000, "message": "boom"}}]
                return [{"id": message["id"], "result": {}}]
            return [{"id": message["id"], "result": {"data": []}}]

        connect = FakeConnect(responder)
        client = AppServerClient(SOCKET, connect=connect)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/list", {})
        self.assertIn("initialize", str(raised.exception))
        # 壊れた接続は残さない。
        self.assertEqual(connect.transports[0].closed, 1)

        # 次の call は繋ぎ直して成功する。前の接続の切断も持ち込まない。
        self.assertEqual(client.call("thread/list", {}), {"data": []})
        self.assertEqual(len(connect.calls), 2)
        methods = [message["method"] for message in connect.transports[1].sent]
        self.assertEqual(methods, ["initialize", "initialized", "thread/list"])

    def test_handshake_timeout_leaves_the_client_restartable(self):
        # initialize に一切答えない app-server。タイムアウト後も状態は未接続へ戻る。
        connect = FakeConnect(lambda message: [])
        client = AppServerClient(SOCKET, connect=connect, timeout_seconds=0.2)

        with self.assertRaises(AppServerError):
            client.call("thread/list", {})

        self.assertEqual(connect.transports[0].closed, 1)
        with self.assertRaises(AppServerError):
            client.call("thread/list", {})
        self.assertEqual(len(connect.calls), 2)

    def test_handshake_disconnect_closes_the_connection(self):
        # initialize の途中で切れる接続。
        connect = FakeConnect(lambda message: [EOF])
        client = AppServerClient(SOCKET, connect=connect)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/list", {})

        self.assertIn("接続が切れました", str(raised.exception))
        self.assertEqual(connect.transports[0].closed, 1)

    def test_successful_calls_keep_the_connection_alive(self):
        connect = FakeConnect(echo_responder({"initialize": {}, "thread/list": {"data": []}}))
        client = AppServerClient(SOCKET, connect=connect)

        client.call("thread/list", {})
        client.call("thread/list", {})

        self.assertEqual(connect.transports[0].closed, 0)

    def test_rpc_error_keeps_the_process_running(self):
        # RPC のエラー応答は通信の失敗ではない。進捗確認の 1 回の失敗で
        # 走っているターンを落とさないよう、接続は残す。
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                return [{"id": message["id"], "result": {}}]
            if message["method"] == "thread/turns/list":
                return [{"id": message["id"], "error": {"code": -32600, "message": "boom"}}]
            return [{"id": message["id"], "result": {"data": []}}]

        connect = FakeConnect(responder)
        client = AppServerClient(SOCKET, connect=connect)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/turns/list", {"threadId": "th_1"})
        self.assertIn("thread/turns/list", str(raised.exception))
        self.assertEqual(connect.transports[0].closed, 0)

        # 同じ接続をそのまま使い続ける。ハンドシェイクもやり直さない。
        self.assertEqual(client.call("thread/list", {}), {"data": []})
        self.assertEqual(len(connect.calls), 1)

    def test_request_timeout_keeps_the_process_running(self):
        # 1 件の時間切れで接続を切ると、走っているターンが道連れになる。
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                return [{"id": message["id"], "result": {}}]
            if message["method"] == "thread/search":
                return []
            return [{"id": message["id"], "result": {"data": []}}]

        connect = FakeConnect(responder)
        client = AppServerClient(SOCKET, connect=connect, timeout_seconds=0.2)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/search", {"searchTerm": "x"})

        self.assertIn("応答しません", str(raised.exception))
        self.assertEqual(connect.transports[0].closed, 0)
        # 時間切れした要求の応答が後から来ても、次の呼び出しに混ざらない。
        self.assertEqual(client.call("thread/list", {}), {"data": []})
        self.assertEqual(len(connect.calls), 1)

    def test_a_timed_out_request_stops_being_tracked(self):
        # 待つのをやめた要求を接続に残すと、応答しない相手ほど溜まっていく。
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                return [{"id": message["id"], "result": {}}]
            return []

        client = AppServerClient(SOCKET, connect=FakeConnect(responder), timeout_seconds=0.1)

        for _ in range(3):
            with self.assertRaises(AppServerError):
                client.call("thread/list", {})

        self.assertEqual(client._conn._responses, {})  # noqa: SLF001

    def test_an_rpc_error_does_not_interrupt_a_waiting_turn(self):
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                return [{"id": message["id"], "result": {}}]
            if message["method"] == "turn/start":
                return [{"id": message["id"], "result": {"turn": {"id": "tu_1"}}}]
            if message["method"] == "thread/read":
                return [{"id": message["id"], "error": {"code": -32600, "message": "boom"}}]
            # 進捗確認が失敗した後で、ターンは無事に完了する。
            return [
                {"id": message["id"], "result": {"data": []}},
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "th_1", "delta": "完了"},
                },
                {"method": "turn/completed", "params": {"threadId": "th_1", "turn": {}}},
            ]

        connect = FakeConnect(responder)
        client = AppServerClient(SOCKET, connect=connect, turn_timeout_seconds=5.0)
        turn = run_in_thread(lambda: client.run_turn({"threadId": "th_1", "input": []}))

        with self.assertRaises(AppServerError):
            client.call("thread/read", {"threadId": "th_1"})
        client.call("thread/list", {})

        result = turn.get(timeout=5.0)
        self.assertIsInstance(result, dict, msg=f"ターンが中断された: {result}")
        self.assertEqual(result["agentMessage"], "完了")

    def test_request_disconnect_reconnects_on_next_call(self):
        handshakes = []

        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                handshakes.append(message["method"])
                return [{"id": message["id"], "result": {}}]
            if len(handshakes) == 1:
                return [EOF]
            return [{"id": message["id"], "result": {"data": []}}]

        connect = FakeConnect(responder)
        client = AppServerClient(SOCKET, connect=connect, timeout_seconds=5.0)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/list", {})
        self.assertIn("接続が切れました", str(raised.exception))
        self.assertEqual(connect.transports[0].closed, 1)

        # 切れた接続を次の接続へ持ち込まない。
        self.assertEqual(client.call("thread/list", {}), {"data": []})
        self.assertEqual(len(connect.calls), 2)

    def test_broken_json_reconnects_on_next_call(self):
        handshakes = []

        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                handshakes.append(message["method"])
                return [{"id": message["id"], "result": {}}]
            if len(handshakes) == 1:
                return ["not json"]
            return [{"id": message["id"], "result": {"data": []}}]

        connect = FakeConnect(responder)
        client = AppServerClient(SOCKET, connect=connect)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/list", {})
        self.assertIn("JSON", str(raised.exception))
        self.assertEqual(connect.transports[0].closed, 1)

        self.assertEqual(client.call("thread/list", {}), {"data": []})
        self.assertEqual(len(connect.calls), 2)

    def test_disconnect_during_a_call_reports_the_reason(self):
        # 応答を待っている間に接続が切れたら、切れたことを理由として返す。
        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                return [{"id": message["id"], "result": {}}]
            return [EOF]

        connect = FakeConnect(responder)
        client = AppServerClient(SOCKET, connect=connect, timeout_seconds=5.0)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/read", {"threadId": "th_1"})

        self.assertIn("接続が切れました", str(raised.exception))
        self.assertEqual(connect.transports[0].closed, 1)

    def test_connect_failure_leaves_nothing_to_close(self):
        # 接続自体が失敗した経路では掴んだものが無く、close も走らない。
        connect = FakeConnect(raises=FileNotFoundError())
        client = AppServerClient(SOCKET, connect=connect)

        with self.assertRaises(AppServerError):
            client.call("thread/list", {})

        self.assertEqual(connect.transports, [])
        with self.assertRaises(AppServerError):
            client.call("thread/list", {})
        self.assertEqual(len(connect.calls), 2)

    def test_tool_error_after_a_broken_request_recovers_on_the_next_call(self):
        # MCP 経路でも、1 回目の失敗が 2 回目の tools/call を壊さない。
        handshakes = []

        def responder(message):
            if "id" not in message:
                return []
            if message["method"] == "initialize":
                handshakes.append(message["method"])
                return [{"id": message["id"], "result": {}}]
            if len(handshakes) == 1:
                return [EOF]
            return [{"id": message["id"], "result": {"data": []}}]

        client = AppServerClient(SOCKET, connect=FakeConnect(responder), timeout_seconds=5.0)

        first = call_tool(client, "thread_list", {})
        second = call_tool(client, "thread_list", {})

        self.assertTrue(first["result"]["isError"])
        self.assertFalse(second["result"]["isError"])
        self.assertEqual(json.loads(second["result"]["content"][0]["text"]), {"data": []})


class FakeHttpResponse(io.BytesIO):
    """urlopen の戻り値の差し替え。with で使えれば足りる。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class BridgeRpcClientTest(unittest.TestCase):
    """MCP アダプタからブリッジ本体への転送。app-server は起動しない。"""

    def make_client(self, payload=None, error=None, **kwargs):
        seen = {}

        def opener(request, timeout=None):
            seen["request"] = request
            seen["timeout"] = timeout
            if error is not None:
                raise error
            return FakeHttpResponse(json.dumps(payload).encode("utf-8"))

        return mcp_adapter.BridgeRpcClient(opener=opener, **kwargs), seen

    def test_posts_method_params_and_wait_to_the_bridge(self):
        client, seen = self.make_client({"result": {"data": []}}, base_url="http://127.0.0.1:9911")

        result = client.call("thread/list", {"limit": 2})

        request = seen["request"]
        self.assertEqual(request.full_url, "http://127.0.0.1:9911" + APP_SERVER_PATH)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            json.loads(request.data),
            {"method": "thread/list", "params": {"limit": 2}, "wait": False},
        )
        self.assertEqual(result, {"data": []})

    def test_wait_is_forwarded_for_synchronous_turns(self):
        client, seen = self.make_client({"result": {}})

        client.call("turn/start", {"threadId": "th_1"}, wait=True)

        self.assertTrue(json.loads(seen["request"].data)["wait"])

    def test_trailing_slash_in_the_base_url_does_not_duplicate(self):
        client, seen = self.make_client({"result": {}}, base_url="http://127.0.0.1:9911/")

        client.call("thread/list", {})

        self.assertEqual(seen["request"].full_url, "http://127.0.0.1:9911" + APP_SERVER_PATH)

    def test_api_key_is_sent_as_a_bearer_token(self):
        client, seen = self.make_client({"result": {}}, api_key="secret")

        client.call("thread/list", {})

        self.assertEqual(seen["request"].get_header("Authorization"), "Bearer secret")

    def test_no_authorization_header_without_an_api_key(self):
        client, seen = self.make_client({"result": {}})

        client.call("thread/list", {})

        self.assertIsNone(seen["request"].get_header("Authorization"))

    def test_http_error_body_becomes_the_message(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8787" + APP_SERVER_PATH,
            502,
            "Bad Gateway",
            {},
            io.BytesIO(json.dumps({"error": {"message": "app-server が落ちました"}}).encode()),
        )
        client, _ = self.make_client(error=error)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/list", {})

        self.assertIn("app-server が落ちました", str(raised.exception))

    def test_unreadable_http_error_falls_back_to_the_status_code(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8787" + APP_SERVER_PATH, 500, "Boom", {}, io.BytesIO(b"not json")
        )
        client, _ = self.make_client(error=error)

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/list", {})

        self.assertIn("500", str(raised.exception))

    def test_connection_failure_names_the_bridge_url(self):
        client, _ = self.make_client(error=urllib.error.URLError("refused"))

        with self.assertRaises(AppServerError) as raised:
            client.call("thread/list", {})

        self.assertIn(APP_SERVER_PATH, str(raised.exception))


class FakeAppServer:
    """BridgeServer が持つ AppServerClient の差し替え。"""

    def __init__(self, raises=None):
        self.raises = raises
        self.calls = []
        self.waited = []
        self.started = []

    def call(self, method, params):
        self.calls.append((method, params))
        if self.raises:
            raise self.raises
        return {"method": method}

    def run_turn(self, params):
        self.waited.append(params)
        if self.raises:
            raise self.raises
        return {"waited": True}

    def start_turn(self, params):
        self.started.append(params)
        if self.raises:
            raise self.raises
        return {"started": True}

    def close(self):
        pass


class AppServerRpcEndpointTest(unittest.TestCase):
    """ブリッジ本体の /app-server/rpc。ここが実行をアダプタの寿命から切り離す。"""

    def start_server(self, app_server, config=None) -> str:
        server = BridgeServer(config or make_config())
        server.app_server.close()
        server.app_server = app_server
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.daemon = True
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def post(self, base_url: str, payload, headers=None):
        request = urllib.request.Request(
            base_url + APP_SERVER_PATH,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_non_turn_rpc_is_forwarded_verbatim(self):
        app_server = FakeAppServer()
        base_url = self.start_server(app_server)

        status, payload = self.post(base_url, {"method": "thread/list", "params": {"limit": 3}})

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"result": {"method": "thread/list"}})
        self.assertEqual(app_server.calls, [("thread/list", {"limit": 3})])

    def test_turn_without_wait_does_not_block(self):
        app_server = FakeAppServer()
        base_url = self.start_server(app_server)

        status, payload = self.post(
            base_url, {"method": "turn/start", "params": {"threadId": "th_1"}}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"result": {"started": True}})
        self.assertEqual(app_server.started, [{"threadId": "th_1"}])
        self.assertEqual(app_server.waited, [])

    def test_turn_with_wait_waits_for_completion(self):
        app_server = FakeAppServer()
        base_url = self.start_server(app_server)

        status, payload = self.post(
            base_url, {"method": "turn/start", "params": {"threadId": "th_1"}, "wait": True}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"result": {"waited": True}})
        self.assertEqual(app_server.waited, [{"threadId": "th_1"}])
        self.assertEqual(app_server.started, [])

    def test_missing_params_become_an_empty_object(self):
        app_server = FakeAppServer()
        base_url = self.start_server(app_server)

        self.post(base_url, {"method": "thread/list"})

        self.assertEqual(app_server.calls, [("thread/list", {})])

    def test_app_server_failure_is_reported_as_bad_gateway(self):
        app_server = FakeAppServer(raises=AppServerError("codex app-server が応答しません"))
        base_url = self.start_server(app_server)

        status, payload = self.post(base_url, {"method": "thread/list", "params": {}})

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "app_server_failed")
        self.assertIn("応答しません", payload["error"]["message"])

    def test_malformed_requests_are_rejected(self):
        app_server = FakeAppServer()
        base_url = self.start_server(app_server)

        for body in ([], {"params": {}}, {"method": ""}, {"method": 1}, {"method": "a", "params": 1}):
            with self.subTest(body=body):
                status, payload = self.post(base_url, body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(app_server.calls, [])

    def test_api_key_is_required_when_configured(self):
        app_server = FakeAppServer()
        base_url = self.start_server(app_server, make_config(api_key="secret"))

        status, _ = self.post(base_url, {"method": "thread/list", "params": {}})
        self.assertEqual(status, 401)

        status, _ = self.post(
            base_url, {"method": "thread/list", "params": {}}, {"Authorization": "Bearer secret"}
        )
        self.assertEqual(status, 200)


class ClaudeCliMcpArgumentTest(unittest.TestCase):
    def test_disabled_by_default(self):
        command = build_command(make_config(), None, Session(), "sid-1")

        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn("--mcp-config", command)
        self.assertNotIn("--allowedTools", command)

    def test_enabled_adds_mcp_config_and_allowed_tools(self):
        command = build_command(make_config(codex_mcp=True), None, Session(), "sid-1")

        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(command[command.index("--allowedTools") + 1], f"mcp__{MCP_SERVER_NAME}")
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn("--allow-dangerously-skip-permissions", command)
        self.assertEqual(
            command[command.index("--permission-mode") + 1], "bypassPermissions"
        )

    def test_mcp_config_registers_only_the_bundled_adapter(self):
        config = make_config(codex_mcp=True, host="127.0.0.1", port=9911)
        command = build_command(config, None, Session(), "sid-1")
        payload = json.loads(command[command.index("--mcp-config") + 1])

        servers = payload["mcpServers"]
        self.assertEqual(list(servers), [MCP_SERVER_NAME])
        entry = servers[MCP_SERVER_NAME]
        self.assertEqual(entry["args"], ["-m", "claude_bridge.mcp_adapter"])
        # app-server はブリッジ本体が持つので、アダプタへ渡すのは接続先だけ。
        self.assertEqual(entry["env"]["CLAUDE_BRIDGE_URL"], "http://127.0.0.1:9911")
        self.assertNotIn("CLAUDE_BRIDGE_CODEX_PATH", entry["env"])
        self.assertTrue(entry["env"]["PYTHONPATH"])

    def test_bridge_url_brackets_an_ipv6_host(self):
        # 括弧で囲まないと、ポートと区別できない URL になる。
        self.assertEqual(make_config(host="::1", port=8787).bridge_url, "http://[::1]:8787")
        self.assertEqual(
            make_config(host="localhost", port=8787).bridge_url, "http://localhost:8787"
        )

    def test_mcp_config_is_a_single_argument(self):
        # JSON をシェルで組み立てず、引数配列の 1 要素として渡す。
        command = build_command(make_config(codex_mcp=True), None, Session(), "sid-1")
        value = command[command.index("--mcp-config") + 1]

        self.assertEqual(command.count(value), 1)
        self.assertIsInstance(json.loads(value), dict)

    def test_system_prompt_still_follows_mcp_arguments(self):
        command = build_command(make_config(codex_mcp=True), "校正して", Session(), "sid-1")

        self.assertEqual(command[-2:], ["--append-system-prompt", "校正して"])

    def test_pythonpath_points_at_the_package_parent(self):
        servers = build_mcp_servers(make_config(codex_mcp=True))
        package_parent = servers[MCP_SERVER_NAME]["env"]["PYTHONPATH"]

        self.assertEqual(package_parent, os.path.dirname(claude_bridge.__path__[0]))


class CodexMcpConfigTest(unittest.TestCase):
    def test_disabled_without_explicit_opt_in(self):
        config = build_config([], env={})

        self.assertFalse(config.codex_mcp)
        self.assertTrue(
            config.app_server_socket.endswith("app-server-control/app-server-control.sock")
        )

    def test_enabled_by_flag(self):
        self.assertTrue(build_config(["--enable-codex-mcp"], env={}).codex_mcp)

    def test_enabled_by_env(self):
        for value in ("1", "true", "yes", "on", "TRUE"):
            with self.subTest(value=value):
                config = build_config([], env={"CLAUDE_BRIDGE_ENABLE_CODEX_MCP": value})
                self.assertTrue(config.codex_mcp)

    def test_other_env_values_leave_it_disabled(self):
        for value in ("", "0", "false", "no", "off", "maybe"):
            with self.subTest(value=value):
                config = build_config([], env={"CLAUDE_BRIDGE_ENABLE_CODEX_MCP": value})
                self.assertFalse(config.codex_mcp)

    def test_app_server_socket_from_env_and_flag(self):
        env = {"CLAUDE_BRIDGE_APP_SERVER_SOCKET": "/tmp/from-env.sock"}
        self.assertEqual(build_config([], env=env).app_server_socket, "/tmp/from-env.sock")
        overridden = build_config(["--app-server-socket", "/tmp/from-flag.sock"], env=env)
        self.assertEqual(overridden.app_server_socket, "/tmp/from-flag.sock")

    def test_app_server_socket_follows_codex_home(self):
        # CODEX_HOME を変えたら、Codex アプリが繋ぐ先と同じ場所を指す。
        config = build_config([], env={"CODEX_HOME": "/tmp/codex-home"})

        self.assertEqual(
            config.app_server_socket,
            "/tmp/codex-home/app-server-control/app-server-control.sock",
        )


class ExtraMcpConfigFileTest(unittest.TestCase):
    """--mcp-config-file で指定した MCP サーバーを追加で渡せる。"""

    def write_config_file(self, payload: object) -> str:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, "mcp.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def write_server_file(self) -> str:
        return self.write_config_file({"mcpServers": {"alchemist-mcp": {"command": "npx"}}})

    def test_servers_are_added_and_allowed(self):
        config = build_config(
            ["--enable-codex-mcp", "--mcp-config-file", self.write_server_file()], env={}
        )
        command = build_command(config, None, Session(), "sid-1")
        servers = json.loads(command[command.index("--mcp-config") + 1])["mcpServers"]

        # 同梱アダプタと追加分が両方載り、権限確認なしで使えるよう両方許可される。
        self.assertEqual(set(servers), {MCP_SERVER_NAME, "alchemist-mcp"})
        allowed = command[command.index("--allowedTools") + 1]
        self.assertEqual(set(allowed.split(",")), {f"mcp__{MCP_SERVER_NAME}", "mcp__alchemist-mcp"})

    def test_usable_without_the_codex_adapter(self):
        config = build_config(["--mcp-config-file", self.write_server_file()], env={})
        command = build_command(config, None, Session(), "sid-1")
        servers = json.loads(command[command.index("--mcp-config") + 1])["mcpServers"]

        self.assertEqual(set(servers), {"alchemist-mcp"})
        self.assertEqual(command[command.index("--allowedTools") + 1], "mcp__alchemist-mcp")

    def test_unusable_file_is_rejected_at_startup(self):
        """壊れた設定はリクエスト時ではなく起動時に落とす。"""

        paths = {
            "存在しない": "/no/such/mcp.json",
            "mcpServers なし": self.write_config_file({"servers": {}}),
            "型違い": self.write_config_file({"mcpServers": []}),
            # 同名を許すと同梱アダプタの定義を黙って置き換えてしまう。
            "同梱アダプタと同名": self.write_config_file(
                {"mcpServers": {MCP_SERVER_NAME: {"command": "npx"}}}
            ),
        }
        for label, path in paths.items():
            with self.subTest(label), self.assertRaises(ValueError):
                build_config(["--mcp-config-file", path], env={})

    def test_path_comes_from_env(self):
        env = {"CLAUDE_BRIDGE_MCP_CONFIG_FILE": self.write_server_file()}
        self.assertEqual(set(build_config([], env=env).extra_mcp_servers), {"alchemist-mcp"})
        self.assertEqual(build_config([], env={}).extra_mcp_servers, {})

    def test_home_relative_path_is_expanded(self):
        """launchd の環境変数では `~` が展開されないため、設定側で解決する。"""

        path = self.write_server_file()
        with mock.patch.dict(os.environ, {"HOME": os.path.dirname(path)}):
            config = build_config(["--mcp-config-file", "~/mcp.json"], env={})

        self.assertEqual(set(config.extra_mcp_servers), {"alchemist-mcp"})


if __name__ == "__main__":
    unittest.main()
