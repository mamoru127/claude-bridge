import errno
import io
import json
import logging
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from claude_bridge.claude_cli import build_command, stream_claude, transcript_id
from claude_bridge import cli_process
from claude_bridge.cli_process import final_text
from claude_bridge.config import (
    ANTIGRAVITY_BACKEND,
    CLAUDE_BACKEND,
    BridgeConfig,
    build_config,
)
from claude_bridge.errors import BridgeError
from claude_bridge import server as server_module
from claude_bridge.responses import (
    KEEPALIVE_EVENT,
    Message,
    ResponseStream,
    build_content,
    build_models,
    parse_request,
)
from claude_bridge.server import BridgeServer, Keepalive
from claude_bridge.sessions import Session, SessionRegistry
from claude_bridge.upstream import models_request_path, responses_body


# Claude CLI を直接呼ぶテストで使う、テキストだけの content。
TEXT_CONTENT = ({"type": "text", "text": "やあ"},)

# 1x1 の PNG。画像の中身は検証しないので最小のもので足りる。
PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_DATA_URL = f"data:image/png;base64,{PNG_BASE64}"


def image_part(url: str = PNG_DATA_URL) -> dict:
    return {"type": "input_image", "image_url": url}


def cli_lines(*events: dict) -> bytes:
    """stream-json（JSONL）形式の標準出力を組み立てる。"""

    return b"".join(json.dumps(event).encode("utf-8") + b"\n" for event in events)


def result_event(result: str, is_error: bool = False) -> dict:
    return {
        "type": "result",
        "subtype": "error" if is_error else "success",
        "is_error": is_error,
        "result": result,
        "session_id": "test-session",
    }


def tool_use_event(name: str, arguments: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": "toolu_1", "name": name, "input": arguments}]
        },
    }


def text_event(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def thinking_event(thinking: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": thinking, "signature": "sig"}]},
    }


class FakeStdin(io.BytesIO):
    """close() 後も書き込み内容を確認できる標準入力。"""

    written = b""

    def close(self) -> None:
        self.written = self.getvalue()
        super().close()


class FakeProcess:
    """subprocess.Popen の差し替え。用意した JSONL を標準出力として返す。"""

    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdin = FakeStdin()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.killed = 0
        self.waited = 0

    def wait(self) -> int:
        self.waited += 1
        return self.returncode

    def kill(self) -> None:
        self.killed += 1


class HangingProcess(FakeProcess):
    """kill されるまで標準出力が終わらないプロセス。タイムアウト経路を作る。"""

    class Stdout:
        def __init__(self):
            self.released = threading.Event()

        def __iter__(self):
            self.released.wait(10)
            return iter(())

    def __init__(self):
        super().__init__(b"", returncode=-9)
        self.stdout = self.Stdout()

    def kill(self) -> None:
        self.killed += 1
        self.stdout.released.set()


class FakeRunner:
    """subprocess.Popen の差し替え。呼び出し内容と生成したプロセスを記録する。"""

    def __init__(self, stdout: bytes | None = None, stderr: bytes = b"", returncode: int = 0,
                 raises=None):
        self.stdout = cli_lines(result_event("こんにちは")) if stdout is None else stdout
        self.stderr = stderr
        self.returncode = returncode
        self.raises = raises
        self.calls = []
        self.processes = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        if self.raises:
            raise self.raises
        process = self.make_process()
        self.processes.append(process)
        return process

    def make_process(self) -> FakeProcess:
        return FakeProcess(self.stdout, self.stderr, self.returncode)

    def stdin_message(self, index: int = 0) -> dict:
        """標準入力へ渡した stream-json の 1 行。"""

        return json.loads(self.processes[index].stdin.written.decode("utf-8"))

    def content(self, index: int = 0) -> list:
        """標準入力へ渡した content ブロック列。"""

        return self.stdin_message(index)["message"]["content"]

    def prompt(self, index: int = 0) -> str:
        """content のうちテキストブロックだけを連結したもの。"""

        return "".join(
            block["text"] for block in self.content(index) if block["type"] == "text"
        )


class WindowsCliLaunchTest(unittest.TestCase):
    def test_npm_command_is_resolved_before_launch(self):
        runner = FakeRunner()
        with mock.patch.object(cli_process.os, "name", "nt"), mock.patch.object(
            cli_process.shutil, "which", return_value=r"C:\Tools\claude.cmd"
        ):
            cli_process.launch(cli_process.CliKind("Claude CLI", "claude_cli"), ["claude", "--print"],
                               r"C:\work", runner)
        self.assertEqual(runner.calls[0]["command"], [r"C:\Tools\claude.cmd", "--print"])

class HangingRunner(FakeRunner):
    """kill されるまで終わらないプロセスを返すランナー。"""

    def make_process(self) -> FakeProcess:
        return HangingProcess()


class BlockingRunner(FakeRunner):
    """テストが解放するまで CLI 起動を止めるランナー。ストリーム途中の状態を作る。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, command, **kwargs):
        self.entered.set()
        self.release.wait(10)
        return super().__call__(command, **kwargs)


def run_claude_stream(*args, **kwargs) -> str:
    """stream_claude を最後まで読み、確定した最終テキストだけを返す。"""

    return final_text(stream_claude(*args, **kwargs))


class LogWaiter(logging.Handler):
    """別スレッドで出るログを待ち合わせるためのハンドラ。"""

    def __init__(self, needle: str):
        super().__init__()
        self.needle = needle
        self.seen = threading.Event()

    def emit(self, record):
        if self.needle in record.getMessage():
            self.seen.set()


def parse_sse(raw: bytes) -> list[tuple[str, object]]:
    """SSE 本文を (event 名, data) の列に分解する。data が [DONE] のときは文字列のまま返す。"""

    events = []
    for block in raw.decode("utf-8").split("\n\n"):
        if not block.strip():
            continue
        fields = dict(line.split(": ", 1) for line in block.split("\n"))
        data = fields["data"]
        events.append((fields.get("event"), data if data == "[DONE]" else json.loads(data)))
    return events


def make_config(**overrides) -> BridgeConfig:
    defaults = {"host": "127.0.0.1", "port": 0, "model": "opus", "working_dir": "."}
    return BridgeConfig(**{**defaults, **overrides})


class BridgeHTTPTestCase(unittest.TestCase):
    """実際に HTTP サーバーを起動して検証する共通基盤。"""

    def start_server(self, config: BridgeConfig, runner) -> str:
        server = BridgeServer(config, runner=runner)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.daemon = True
        thread.start()
        # addCleanup は LIFO のため、shutdown → server_close → join の順で実行される。
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def post(self, base_url: str, payload, headers=None, path="/v1/responses"):
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            base_url + path,
            data=body,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def post_and_disconnect(self, base_url: str, payload) -> None:
        """応答を待たずに接続を切る。相手の消えた実行を作るために使う。"""

        url = urllib.parse.urlsplit(base_url)
        body = json.dumps(payload).encode("utf-8")
        sock = socket.create_connection((url.hostname, url.port))
        sock.sendall(
            f"POST /v1/responses HTTP/1.1\r\nHost: {url.netloc}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        )
        sock.close()

    def post_stream(self, base_url: str, payload, headers=None):
        """stream=true を送り、Content-Type と SSE イベント列を返す。"""

        request = urllib.request.Request(
            base_url + "/v1/responses",
            data=json.dumps({**payload, "stream": True}).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.headers["Content-Type"], parse_sse(response.read())

    def post_bytes(self, base_url: str, path: str, body: bytes, content_type: str, headers=None):
        """任意の Content-Type と生バイト列を POST し、応答も生のまま返す。"""

        request = urllib.request.Request(
            base_url + path,
            data=body,
            headers={"Content-Type": content_type, **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def get_json(self, base_url: str, path: str, headers=None):
        request = urllib.request.Request(base_url + path, headers=headers or {}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def post_raw_length(self, base_url: str, length_header: str, body: bytes = b"{}"):
        """Content-Length を任意の文字列にして送る。urllib では偽装できないため生ソケットを使う。"""

        parsed = urllib.parse.urlsplit(base_url)
        request = (
            f"POST /v1/responses HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {length_header}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + body
        with socket.create_connection((parsed.hostname, parsed.port), timeout=10) as connection:
            connection.sendall(request)
            chunks = []
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        head, _, raw_body = b"".join(chunks).partition(b"\r\n\r\n")
        status = int(head.split(b"\r\n", 1)[0].split(b" ")[1])
        return status, json.loads(raw_body)


class ResponsesEndpointTest(BridgeHTTPTestCase):
    def test_returns_openai_responses_payload(self):
        runner = FakeRunner()
        base_url = self.start_server(make_config(), runner)

        status, payload = self.post(base_url, {"model": "gpt-5", "input": "やあ"})

        self.assertEqual(status, 200)
        self.assertTrue(payload["id"].startswith("resp_"))
        self.assertEqual(payload["object"], "response")
        self.assertIsInstance(payload["created_at"], int)
        self.assertEqual(payload["model"], "gpt-5")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["output_text"], "こんにちは")

        message = payload["output"][0]
        self.assertEqual(message["type"], "message")
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"][0]["type"], "output_text")
        self.assertEqual(message["content"][0]["text"], "こんにちは")

    def test_model_falls_back_to_the_first_api_model_name_when_absent(self):
        # 既定は API で受け付ける名前。CLI の実行モデル名（--model）を既定にすると中継先が狂う。
        base_url = self.start_server(make_config(model="opus"), FakeRunner())

        status, payload = self.post(base_url, {"input": "やあ"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "claude-opus-5-5")

    def test_listed_model_selects_the_cli_model(self):
        # 一覧に載せた名前はアプリの選択がそのまま実行モデルになる。
        runner = FakeRunner()
        base_url = self.start_server(make_config(model="opus"), runner)

        status, payload = self.post(base_url, {"model": "claude-fable-5-1", "input": "やあ"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "claude-fable-5-1")
        command = runner.calls[0]["command"]
        self.assertEqual(command[command.index("--model") + 1], "claude-fable-5-1")

    def test_unlisted_model_does_not_reach_the_cli(self):
        # 中継が無効だと一覧外の名前も届く。任意の文字列を CLI へ流さず起動時の固定値で実行する。
        runner = FakeRunner()
        base_url = self.start_server(make_config(model="opus"), runner)

        status, payload = self.post(base_url, {"model": "gpt-5.4", "input": "やあ"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "gpt-5.4")
        command = runner.calls[0]["command"]
        self.assertEqual(command[command.index("--model") + 1], "opus")
        self.assertNotIn("gpt-5.4", " ".join(command))

    def test_invokes_claude_cli_without_shell(self):
        runner = FakeRunner()
        base_url = self.start_server(
            make_config(model="opus", working_dir=".", timeout_seconds=12.0), runner
        )

        self.post(
            base_url,
            {
                "instructions": "あなたは校正者です",
                "input": [
                    {
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "日本語で答える"}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "本文です"}],
                    },
                ],
            },
        )

        call = runner.calls[0]
        self.assertEqual(
            call["command"][:14],
            [
                "claude",
                "--print",
                "--output-format",
                "stream-json",
                "--input-format",
                "stream-json",
                "--verbose",
                "--thinking-display",
                "summarized",
                "--strict-mcp-config",
                "--permission-mode",
                "bypassPermissions",
                "--model",
                # model 省略時は一覧の先頭が選ばれ、その名前で実行する。
                "claude-opus-5-5",
            ],
        )
        system_prompt = call["command"].index("--append-system-prompt")
        self.assertEqual(call["command"][system_prompt + 1], "あなたは校正者です\n\n日本語で答える")
        self.assertNotIn("--dangerously-skip-permissions", call["command"])
        self.assertNotIn("--allow-dangerously-skip-permissions", call["command"])
        self.assertEqual(
            call["command"][call["command"].index("--permission-mode") + 1],
            "bypassPermissions",
        )
        # シェル経由での実行をしていないこと（shell=True を渡していない）。
        self.assertNotIn("shell", call)
        self.assertIsInstance(call["command"], list)
        self.assertEqual(runner.prompt(), "本文です")
        self.assertEqual(call["cwd"], ".")

    def test_image_is_passed_to_the_cli_as_stream_json(self):
        runner = FakeRunner()
        base_url = self.start_server(make_config(), runner)

        status, _ = self.post(
            base_url,
            {
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "この画像は？"},
                            image_part(),
                        ],
                    }
                ]
            },
        )

        self.assertEqual(status, 200)
        message = runner.stdin_message()
        self.assertEqual(message["type"], "user")
        self.assertEqual(message["message"]["role"], "user")
        self.assertEqual(
            message["message"]["content"],
            [
                {"type": "text", "text": "この画像は？"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": PNG_BASE64,
                    },
                },
            ],
        )

    def test_multiple_messages_are_labelled(self):
        runner = FakeRunner()
        base_url = self.start_server(make_config(), runner)

        self.post(
            base_url,
            {
                "input": [
                    {"role": "user", "content": "1問目"},
                    {"role": "assistant", "content": "回答"},
                    {"role": "user", "content": "2問目"},
                ]
            },
        )

        self.assertEqual(
            runner.prompt(),
            "[user]\n1問目\n\n[assistant]\n回答\n\n[user]\n2問目",
        )
        self.assertNotIn("--append-system-prompt", runner.calls[0]["command"])

    def test_stream_false_is_accepted(self):
        base_url = self.start_server(make_config(), FakeRunner())

        status, payload = self.post(base_url, {"input": "やあ", "stream": False})

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")

    def test_response_does_not_fabricate_conversation_state_or_tools(self):
        base_url = self.start_server(make_config(), FakeRunner())

        _, payload = self.post(
            base_url,
            {
                "input": "やあ",
                "tools": [{"type": "function", "name": "shell"}],
                "tool_choice": "auto",
                "previous_response_id": "resp_1",
            },
        )

        # これらを受け取っても、会話状態やツール呼び出しは偽装しない。
        for absent in ("previous_response_id", "conversation", "tools", "tool_choice", "usage"):
            self.assertNotIn(absent, payload)
        self.assertEqual([item["type"] for item in payload["output"]], ["message"])

    def test_tools_are_ignored_and_not_passed_to_the_cli(self):
        runner = FakeRunner()
        base_url = self.start_server(make_config(), runner)

        status, payload = self.post(
            base_url,
            {
                "input": "やあ",
                "tools": [
                    {"type": "function", "name": "shell", "parameters": {"type": "object"}},
                    {"type": "local_shell"},
                ],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")
        # ツール定義は CLI の引数にもプロンプトにも一切現れない。
        call = runner.calls[0]
        self.assertNotIn("shell", " ".join(call["command"]))
        self.assertEqual(runner.prompt(), "やあ")

    def test_empty_tools_array_is_accepted(self):
        # Codex はツール不要の要求でも tools を送る。空配列でも通す。
        base_url = self.start_server(make_config(), FakeRunner())

        status, payload = self.post(base_url, {"input": "やあ", "tools": []})

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")

    def test_tool_choice_is_ignored_and_not_passed_to_the_cli(self):
        runner = FakeRunner()
        base_url = self.start_server(make_config(), runner)

        status, payload = self.post(base_url, {"input": "やあ", "tool_choice": "auto"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")
        # ツール呼び出しは行わないため、レスポンスにも CLI 引数にも現れない。
        self.assertNotIn("tool_choice", payload)
        self.assertEqual([item["type"] for item in payload["output"]], ["message"])
        call = runner.calls[0]
        self.assertNotIn("tool_choice", " ".join(call["command"]))
        self.assertEqual(runner.prompt(), "やあ")

    def test_previous_response_id_is_ignored_and_not_passed_to_the_cli(self):
        runner = FakeRunner()
        base_url = self.start_server(make_config(), runner)

        status, payload = self.post(
            base_url, {"input": "やあ", "previous_response_id": "resp_1"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")
        # 会話状態は復元しないため、レスポンスにも CLI へも引き継がない。
        self.assertNotIn("previous_response_id", payload)
        call = runner.calls[0]
        self.assertNotIn("resp_1", " ".join(call["command"]))
        self.assertEqual(runner.prompt(), "やあ")

    def test_ignored_params_are_accepted_regardless_of_value(self):
        # null・空文字・偽値でも、キーの有無を問わず受理してそのまま捨てる。
        runner = FakeRunner()
        base_url = self.start_server(make_config(), runner)

        for param, value in (
            ("tool_choice", None),
            ("tool_choice", False),
            ("previous_response_id", ""),
            ("previous_response_id", None),
        ):
            with self.subTest(param=param, value=value):
                status, payload = self.post(base_url, {"input": "やあ", param: value})

                self.assertEqual(status, 200)
                self.assertEqual(payload["output_text"], "こんにちは")
                self.assertNotIn(param, payload)
                self.assertEqual(runner.prompt(-1), "やあ")

    def test_query_string_is_ignored_when_routing_post(self):
        base_url = self.start_server(make_config(), FakeRunner())

        status, payload = self.post(
            base_url, {"input": "やあ"}, path="/v1/responses?client_version=0.144.5"
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")

    def test_codex_mcp_is_off_by_default(self):
        runner = FakeRunner()
        base_url = self.start_server(make_config(), runner)

        self.post(base_url, {"input": "やあ"})

        # 既定では外部 MCP サーバーを一切起動しない。
        command = runner.calls[0]["command"]
        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn("--mcp-config", command)

    def test_codex_mcp_opt_in_reaches_the_cli_and_still_returns_text(self):
        runner = FakeRunner()
        base_url = self.start_server(make_config(codex_mcp=True), runner)

        status, payload = self.post(base_url, {"input": "スレッド一覧を見せて"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")
        # ツールを使っても Responses API のレスポンスは最終テキストだけ。
        self.assertEqual([item["type"] for item in payload["output"]], ["message"])
        self.assertNotIn("tools", payload)
        command = runner.calls[0]["command"]
        self.assertIn("--mcp-config", command)
        self.assertIn("--strict-mcp-config", command)

    def test_health_endpoint(self):
        base_url = self.start_server(make_config(), FakeRunner())

        with urllib.request.urlopen(base_url + "/health", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"status": "ok"})


class StreamingEndpointTest(BridgeHTTPTestCase):
    EXPECTED_ORDER = [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]

    def test_stream_true_returns_sse_event_sequence(self):
        base_url = self.start_server(make_config(), FakeRunner())

        content_type, events = self.post_stream(base_url, {"model": "gpt-5", "input": "やあ"})

        self.assertTrue(content_type.startswith("text/event-stream"))
        self.assertEqual([name for name, _ in events], self.EXPECTED_ORDER + [None])
        # event 名と data.type は一致し、最後は [DONE] で終わる。
        for name, data in events[:-1]:
            self.assertEqual(data["type"], name)
        self.assertEqual(events[-1][1], "[DONE]")

    def test_stream_events_share_ids_and_sequence_numbers(self):
        base_url = self.start_server(make_config(), FakeRunner())

        _, events = self.post_stream(base_url, {"input": "やあ"})
        payloads = [data for _, data in events[:-1]]

        self.assertEqual(
            [data["sequence_number"] for data in payloads], list(range(1, len(payloads) + 1))
        )
        response_ids = {data["response"]["id"] for data in payloads if "response" in data}
        item_ids = {data["item_id"] for data in payloads if "item_id" in data}
        item_ids |= {data["item"]["id"] for data in payloads if "item" in data}
        self.assertEqual(len(response_ids), 1)
        self.assertEqual(len(item_ids), 1)
        self.assertTrue(response_ids.pop().startswith("resp_"))

    def test_stream_delivers_whole_text_as_single_delta(self):
        base_url = self.start_server(make_config(), FakeRunner())

        _, events = self.post_stream(base_url, {"model": "opus", "input": "やあ"})
        by_type = {name: data for name, data in events if name}

        self.assertEqual(by_type["response.output_text.delta"]["delta"], "こんにちは")
        self.assertEqual(by_type["response.output_text.done"]["text"], "こんにちは")
        completed = by_type["response.completed"]["response"]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["model"], "opus")
        self.assertEqual(completed["output_text"], "こんにちは")
        message = completed["output"][0]
        self.assertEqual(message["status"], "completed")
        self.assertEqual(message["phase"], "final_answer")
        self.assertEqual(message["content"][0]["text"], "こんにちは")

    def test_stream_status_is_in_progress_before_completion(self):
        base_url = self.start_server(make_config(), FakeRunner())

        _, events = self.post_stream(base_url, {"input": "やあ"})
        by_type = {name: data for name, data in events if name}

        self.assertEqual(by_type["response.created"]["response"]["status"], "in_progress")
        self.assertEqual(by_type["response.created"]["response"]["output"], [])
        self.assertEqual(by_type["response.output_item.added"]["item"]["status"], "in_progress")
        self.assertEqual(
            by_type["response.output_item.added"]["item"]["phase"], "final_answer"
        )
        self.assertEqual(by_type["response.content_part.added"]["part"]["text"], "")

    def test_cli_failure_becomes_sse_failed_event(self):
        runner = FakeRunner(stdout=b"", returncode=1, stderr=b"credit balance too low")
        base_url = self.start_server(make_config(), runner)

        _, events = self.post_stream(base_url, {"input": "やあ"})
        names = [name for name, _ in events]

        self.assertEqual(names, ["response.created", "response.in_progress", "response.failed", None])
        error = dict(events)["response.failed"]["response"]["error"]
        self.assertEqual(error["code"], "claude_cli_failed")
        self.assertEqual(error["type"], "api_error")
        self.assertIn("credit balance too low", error["message"])

    def test_tool_use_is_streamed_as_reasoning_before_the_text(self):
        # 実行中の作業を Codex に見せるため、ツール使用を reasoning として先に流す。
        runner = FakeRunner(
            stdout=cli_lines(
                {"type": "system", "subtype": "init"},
                tool_use_event("Bash", {"command": "wc -l README.md"}),
                tool_use_event("Read", {"file_path": "/tmp/README.md"}),
                result_event("377 行です"),
            )
        )
        base_url = self.start_server(make_config(), runner)

        _, events = self.post_stream(base_url, {"input": "行数を数えて"})
        names = [name for name, _ in events]
        reasoning = [
            data["item"]
            for name, data in events
            if name == "response.output_item.done" and data["item"]["type"] == "reasoning"
        ]

        # 表示に使われるのは summary。Codex は text だけのアイテムを読まない。
        self.assertEqual(
            [item["summary"] for item in reasoning],
            [
                [{"type": "summary_text", "text": "**Bash を実行中**\nwc -l README.md"}],
                [{"type": "summary_text", "text": "**Read を実行中**\n/tmp/README.md"}],
            ],
        )
        # reasoning は本文の前に届き、最終テキストは従来どおり 1 個の delta で流れる。
        self.assertLess(
            names.index("response.output_item.done"), names.index("response.output_text.delta")
        )
        self.assertEqual(dict(events)["response.output_text.done"]["text"], "377 行です")

    def test_thinking_is_streamed_as_reasoning_and_empty_thinking_is_ignored(self):
        # thinking 本文もツール使用と同じ reasoning として流す。空の思考は表示する内容がない。
        runner = FakeRunner(
            stdout=cli_lines(
                thinking_event("行数の数え方を決める"),
                thinking_event(""),
                tool_use_event("Read", {"file_path": "/tmp/README.md"}),
                result_event("377 行です"),
            )
        )
        base_url = self.start_server(make_config(), runner)

        _, events = self.post_stream(base_url, {"input": "行数を数えて"})
        reasoning = [
            data["item"]["summary"]
            for name, data in events
            if name == "response.output_item.done" and data["item"]["type"] == "reasoning"
        ]

        self.assertEqual(
            reasoning,
            [
                [{"type": "summary_text", "text": "行数の数え方を決める"}],
                [{"type": "summary_text", "text": "**Read を実行中**\n/tmp/README.md"}],
            ],
        )
        self.assertEqual(dict(events)["response.output_text.done"]["text"], "377 行です")

    def test_work_comments_are_streamed_as_messages_without_repeating_the_result(self):
        # 作業の区切りで書く本文は、記録に残る message として最終テキストより先に流す。
        # 最後の 1 件は result と同じ本文なので、そちらだけを最終テキストとして送る。
        runner = FakeRunner(
            stdout=cli_lines(
                text_event("まず行数を数えます"),
                tool_use_event("Bash", {"command": "wc -l README.md"}),
                text_event("377 行でした"),
                result_event("377 行でした"),
            )
        )
        base_url = self.start_server(make_config(), runner)

        _, events = self.post_stream(base_url, {"input": "行数を数えて"})
        messages = [
            data["item"]
            for name, data in events
            if name == "response.output_item.done" and data["item"]["type"] == "message"
        ]

        self.assertEqual(
            [item["content"][0]["text"] for item in messages],
            ["まず行数を数えます", "377 行でした"],
        )
        self.assertEqual(
            [item["phase"] for item in messages], ["commentary", "final_answer"]
        )
        # アイテム ID を分けないと、Codex 側で同じ 1 件の更新として扱われる。
        self.assertEqual(len({item["id"] for item in messages}), 2)
        # 完了した response の output にも、作業コメントが本文と同じ並びで載る。
        completed = dict(events)["response.completed"]["response"]
        self.assertEqual([item["type"] for item in completed["output"]], ["message", "message"])
        self.assertEqual(
            [item["phase"] for item in completed["output"]],
            ["commentary", "final_answer"],
        )

        # added と done で phase が変わると、Codex App が途中コメントを最終回答として扱う。
        message_events = [
            data["item"]
            for name, data in events
            if name in {"response.output_item.added", "response.output_item.done"}
            and data["item"]["type"] == "message"
        ]
        self.assertEqual(
            [item["phase"] for item in message_events],
            ["commentary", "commentary", "final_answer", "final_answer"],
        )

    def test_work_comment_is_kept_when_it_differs_from_the_result(self):
        # 最終テキストと違う本文は、重複ではないので落とさない。
        runner = FakeRunner(
            stdout=cli_lines(text_event("確認しました"), result_event("377 行です"))
        )
        base_url = self.start_server(make_config(), runner)

        _, events = self.post_stream(base_url, {"input": "行数を数えて"})
        texts = [
            data["item"]["content"][0]["text"]
            for name, data in events
            if name == "response.output_item.done" and data["item"]["type"] == "message"
        ]

        self.assertEqual(texts, ["確認しました", "377 行です"])

    def test_reasoning_and_message_share_one_output_index_sequence(self):
        # output_index は reasoning と本文で 0 から通し番号にする。
        runner = FakeRunner(
            stdout=cli_lines(
                tool_use_event("Bash", {"command": "ls"}),
                tool_use_event("Read", {"file_path": "/tmp/a"}),
                result_event("done"),
            )
        )
        base_url = self.start_server(make_config(), runner)

        _, events = self.post_stream(base_url, {"input": "やあ"})
        indexes = [
            (data["item"]["type"], data["output_index"])
            for name, data in events
            if name == "response.output_item.done"
        ]

        self.assertEqual(indexes, [("reasoning", 0), ("reasoning", 1), ("message", 2)])
        added = [data for name, data in events if name == "response.output_item.added"]
        self.assertEqual([data["output_index"] for data in added], [2])

    def test_reasoning_events_join_the_response_sequence(self):
        # reasoning も Responses のイベント列の一部。sequence_number は通しの連番になる。
        runner = FakeRunner(
            stdout=cli_lines(tool_use_event("Bash", {"command": "ls"}), result_event("done"))
        )
        base_url = self.start_server(make_config(), runner)

        _, events = self.post_stream(base_url, {"input": "やあ"})
        payloads = [data for name, data in events if name]

        self.assertEqual(
            [name for name, _ in events],
            self.EXPECTED_ORDER[:2] + ["response.output_item.done"]
            + self.EXPECTED_ORDER[2:] + [None],
        )
        self.assertEqual(
            [data["sequence_number"] for data in payloads], list(range(1, len(payloads) + 1))
        )
        # 完了した response の output はメッセージだけで、reasoning は含めない。
        completed = dict(events)["response.completed"]["response"]
        self.assertEqual([item["type"] for item in completed["output"]], ["message"])

    def test_stream_with_tool_choice_still_streams(self):
        # tool_choice は無視するだけなので、stream=true でも通常どおり SSE を返す。
        base_url = self.start_server(make_config(), FakeRunner())

        content_type, events = self.post_stream(
            base_url, {"input": "やあ", "tool_choice": "auto"}
        )

        self.assertTrue(content_type.startswith("text/event-stream"))
        self.assertEqual([name for name, _ in events], self.EXPECTED_ORDER + [None])
        self.assertEqual(dict(events)["response.output_text.done"]["text"], "こんにちは")

    def test_invalid_request_with_stream_still_returns_json_error(self):
        # ヘッダ送出前の検証エラーは従来どおり JSON で返す。
        base_url = self.start_server(make_config(), FakeRunner())

        status, body = self.post(base_url, {"input": [], "stream": True})

        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "invalid_request_error")

    def test_stream_with_tools_still_streams(self):
        base_url = self.start_server(make_config(), FakeRunner())

        content_type, events = self.post_stream(
            base_url, {"input": "やあ", "tools": [{"type": "function", "name": "shell"}]}
        )

        self.assertTrue(content_type.startswith("text/event-stream"))
        self.assertEqual([name for name, _ in events], self.EXPECTED_ORDER + [None])

    def test_client_disconnect_does_not_break_server(self):
        runner = BlockingRunner()
        base_url = self.start_server(make_config(), runner)
        waiter = LogWaiter("stream aborted by client")
        logger = logging.getLogger("claude_bridge")
        previous_level = logger.level
        logger.addHandler(waiter)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.setLevel, previous_level)
        self.addCleanup(logger.removeHandler, waiter)

        parsed = urllib.parse.urlsplit(base_url)
        body = json.dumps({"input": "やあ", "stream": True}).encode("utf-8")
        connection = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
        connection.sendall(
            f"POST /v1/responses HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode("utf-8")
            + body
        )
        self.assertTrue(runner.entered.wait(10))
        # SO_LINGER=0 で RST を送り、CLI 完了後の書き込みを確実に失敗させる。
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        connection.close()
        runner.release.set()

        self.assertTrue(waiter.seen.wait(10))
        # 切断後もサーバーは次のリクエストを処理できる。
        status, payload = self.post(base_url, {"input": "やあ"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")


class ModelsEndpointTest(BridgeHTTPTestCase):
    def test_lists_claude_models(self):
        base_url = self.start_server(make_config(claude_models=("claude-opus-5-5",)), FakeRunner())

        status, payload = self.get_json(base_url, "/v1/models")

        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "list")
        entry = next(item for item in payload["data"] if item["id"] == "claude-opus-5-5")
        self.assertEqual(entry["object"], "model")
        self.assertEqual(entry["owned_by"], "anthropic")
        self.assertIsInstance(entry["created"], int)

    def test_model_list_request_adds_client_version(self):
        self.assertEqual(
            models_request_path("/v1/models"),
            "/v1/models?client_version=0.147.0",
        )

    def test_model_list_request_keeps_client_version(self):
        self.assertEqual(
            models_request_path("/v1/models?client_version=0.144.5"),
            "/v1/models?client_version=0.144.5",
        )

    def test_claude_models_are_listed_in_order_without_duplicates(self):
        payload = build_models(
            make_config(claude_models=("claude-opus-5-5", "sonnet"), antigravity_models=())
        )
        ids = [item["id"] for item in payload["data"]]

        self.assertEqual(ids, ["claude-opus-5-5", "sonnet"])

    def test_codex_metadata_is_returned_alongside_data(self):
        # Codex は data ではなく models を読むため、同じ別名を両方へ載せる。
        payload = build_models(make_config(model="opus"))

        self.assertEqual(
            [item["slug"] for item in payload["models"]],
            [item["id"] for item in payload["data"]],
        )
        entry = payload["models"][0]
        self.assertTrue(entry["supported_in_api"])
        # reasoning summary はリクエストの要求と無関係に送るので、要求させない値になっている。
        self.assertFalse(entry["supports_reasoning_summary_parameter"])

    def test_code_mode_declaration_is_not_inherited_from_the_template(self):
        # 雛形が code mode 専用だと、Codex はツールを exec のコード実行側へ寄せ、
        # 受け渡しの対象も tool_search も function tool として送らなくなる。
        upstream = {
            "data": [{"id": "gpt-5.6-sol"}],
            "models": [{"slug": "gpt-5.6-sol", "tool_mode": "code_mode_only"}],
        }
        payload = build_models(make_config(), upstream)

        entry = next(item for item in payload["models"] if item["slug"] == "claude-opus-5-5")
        self.assertIsNone(entry["tool_mode"])

    def test_custom_model_is_added_to_the_list(self):
        payload = build_models(make_config(claude_models=("claude-opus-4-5",)))

        self.assertIn("claude-opus-4-5", [item["id"] for item in payload["data"]])

    def test_default_models_replace_opus_5(self):
        payload = build_models(make_config())

        self.assertIn("claude-opus-5-5", [item["id"] for item in payload["data"]])
        self.assertIn("claude-opus-5-5", [item["slug"] for item in payload["models"]])
        self.assertNotIn("claude-opus-5", [item["id"] for item in payload["data"]])
        self.assertNotIn("claude-opus-5", [item["slug"] for item in payload["models"]])

    def test_fable_is_listed_by_default(self):
        # アプリのモデル選択から Fable を選べるようにする。名前は CLI の --model にも渡る。
        payload = build_models(make_config())

        self.assertIn("claude-fable-5-1", [item["id"] for item in payload["data"]])
        self.assertIn("claude-fable-5-1", [item["slug"] for item in payload["models"]])

    def test_duplicate_claude_models_are_listed_once(self):
        config = make_config(
            claude_models=("claude-opus-5-5", "claude-opus-5-5"), antigravity_models=()
        )
        ids = [item["id"] for item in build_models(config)["data"]]

        self.assertEqual(ids, ["claude-opus-5-5"])

    def test_cli_model_is_not_listed_when_not_a_claude_model(self):
        # --model は CLI に渡す実行モデルであって、API で受け付ける名前ではない。
        payload = build_models(make_config(model="opus", claude_models=("claude-opus-5-5",)))

        self.assertNotIn("opus", [item["id"] for item in payload["data"]])
        self.assertNotIn("opus", [item["slug"] for item in payload["models"]])

    def test_every_listed_model_runs_on_a_local_cli(self):
        # 一覧に載る名前をアプリが選んだとき、上流へ中継されて失敗しないこと。
        config = make_config(
            model="opus",
            claude_models=("claude-opus-5-5", "sonnet"),
            upstream_base_url="https://upstream.example",
        )

        for item in build_models(config)["data"]:
            self.assertIn(
                config.backend(item["id"]), (CLAUDE_BACKEND, ANTIGRAVITY_BACKEND)
            )

    def test_codex_query_string_is_ignored_when_routing(self):
        # Codex が実際に送る形。クエリ付きでもモデル一覧を返す。
        base_url = self.start_server(make_config(model="opus"), FakeRunner())

        status, payload = self.get_json(base_url, "/v1/models?client_version=0.144.5")

        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "list")
        self.assertEqual(payload["data"][0]["id"], "claude-opus-5-5")

    def test_query_string_is_not_logged(self):
        base_url = self.start_server(make_config(), FakeRunner())
        with self.assertLogs("claude_bridge", level="INFO") as logs:
            self.get_json(base_url, "/v1/models?private=secret-token")
        output = "\n".join(logs.output)
        self.assertNotIn("secret-token", output)
        self.assertIn("/v1/models", output)

    def test_health_accepts_query_string(self):
        base_url = self.start_server(make_config(), FakeRunner())

        status, payload = self.get_json(base_url, "/health?probe=1")

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok"})

    def test_unknown_get_path_is_rejected(self):
        base_url = self.start_server(make_config(), FakeRunner())

        status, body = self.get_json(base_url, "/v1/embeddings")

        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_unknown_get_path_with_query_is_rejected(self):
        base_url = self.start_server(make_config(), FakeRunner())

        status, body = self.get_json(base_url, "/v1/embeddings?client_version=0.144.5")

        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
        # クエリ文字列はエラー本文へ載せない。
        self.assertNotIn("client_version", body["error"]["message"])


class RequestValidationTest(BridgeHTTPTestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.base_url = self.start_server(make_config(), self.runner)

    def assert_rejected(self, payload, status=400, code=None):
        got_status, body = self.post(self.base_url, payload)
        self.assertEqual(got_status, status)
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        if code:
            self.assertEqual(body["error"]["code"], code)
        self.assertEqual(self.runner.calls, [])
        return body

    def test_non_boolean_stream_is_rejected(self):
        body = self.assert_rejected({"input": "やあ", "stream": "true"})
        self.assertEqual(body["error"]["param"], "stream")

    def test_negative_content_length_is_rejected_before_reading(self):
        status, body = self.post_raw_length(self.base_url, "-1")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertEqual(self.runner.calls, [])

    def test_non_numeric_content_length_is_rejected(self):
        status, body = self.post_raw_length(self.base_url, "abc")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertEqual(self.runner.calls, [])

    def test_non_string_instructions_are_rejected(self):
        body = self.assert_rejected({"input": "やあ", "instructions": {"text": "x"}})
        self.assertEqual(body["error"]["param"], "instructions")

    def test_missing_input_is_rejected(self):
        self.assert_rejected({"model": "gpt-5"})

    def test_empty_input_list_is_rejected(self):
        self.assert_rejected({"input": []})

    def test_unsupported_content_part_is_rejected(self):
        self.assert_rejected(
            {
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_audio", "audio_url": "http://x"}],
                    }
                ]
            },
            code="unsupported_content_part",
        )

    def test_input_with_only_non_message_items_is_rejected(self):
        # 無視した結果メッセージが 1 つも残らないので、input が空のときと同じ扱いになる。
        self.assert_rejected({"input": [{"type": "function_call", "name": "f", "arguments": "{}"}]})

    def test_unknown_role_is_rejected(self):
        self.assert_rejected({"input": [{"role": "tool", "content": "x"}]})

    def test_broken_json_is_rejected(self):
        request = urllib.request.Request(
            self.base_url + "/v1/responses",
            data=b"{invalid",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(raised.exception.code, 400)

    def test_non_json_content_type_is_rejected(self):
        status, body = self.post(
            self.base_url, {"input": "やあ"}, headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(status, 415)
        self.assertEqual(body["error"]["code"], "unsupported_media_type")

    def test_unknown_path_is_rejected(self):
        status, body = self.post(self.base_url, {"input": "やあ"}, path="/v1/chat/completions")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_unknown_path_with_query_is_rejected(self):
        status, body = self.post(
            self.base_url, {"input": "やあ"}, path="/v1/chat/completions?client_version=0.144.5"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
        self.assertEqual(self.runner.calls, [])

    def test_oversized_body_is_rejected(self):
        server_url = self.start_server(make_config(max_request_bytes=64), FakeRunner())
        status, body = self.post(server_url, {"input": "x" * 200})
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "request_too_large")

    def test_accumulated_history_images_do_not_block_the_next_turn(self):
        # Codex は毎ターン履歴を丸ごと送り直す。上限を履歴ぶんに掛けると、画像を渡すたびに
        # 会話が上限へ近づき、いずれそのスレッドの全ターンが 413 になる。CLI へ渡すのは
        # 未送信の発言だけなので、渡す分で判定すれば画像が積み上がっても実行できる。
        runner = FakeRunner()
        # 画像 1 枚は通り、2 枚ぶんは通らない上限。
        server_url = self.start_server(make_config(max_request_bytes=12000), runner)

        def image(fill: str) -> dict:
            return {
                "type": "input_image",
                "image_url": "data:image/png;base64," + fill * 8192,
            }

        self.post(server_url, {"input": "1問目", "prompt_cache_key": "thread-1"})
        first_history = [
            {"role": "user", "content": [{"type": "input_text", "text": "1問目"}]},
            {"role": "assistant", "content": "回答"},
        ]
        status, _ = self.post(
            server_url,
            {
                "input": [
                    *first_history,
                    {
                        "role": "user",
                        "content": [image("A"), {"type": "input_text", "text": "1枚目"}],
                    },
                ],
                "prompt_cache_key": "thread-1",
            },
        )
        self.assertEqual(status, 200)

        status, _ = self.post(
            server_url,
            {
                "input": [
                    *first_history,
                    {
                        "role": "user",
                        "content": [image("A"), {"type": "input_text", "text": "1枚目"}],
                    },
                    {"role": "assistant", "content": "見ました"},
                    {
                        "role": "user",
                        "content": [image("B"), {"type": "input_text", "text": "2枚目"}],
                    },
                ],
                "prompt_cache_key": "thread-1",
            },
        )

        self.assertEqual(status, 200)
        # 渡したのは新しい 1 枚だけ。1 枚目は転記済みなので数えない。
        self.assertEqual(runner.prompt(2), "2枚目")
        self.assertEqual(
            [block["type"] for block in runner.content(2)], ["image", "text"]
        )

    def test_tool_result_images_are_not_counted_toward_the_limit(self):
        # ツール結果の画像は Claude へ渡さずテキストへ置き換えるため、上限にも数えない。
        runner = FakeRunner()
        server_url = self.start_server(make_config(max_request_bytes=1024), runner)

        status, _ = self.post(
            server_url,
            {
                "input": [
                    {"role": "user", "content": "この画像を見て"},
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": [
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64," + "A" * 4096,
                            },
                            {"type": "input_text", "text": "生成しました"},
                        ],
                    },
                ]
            },
        )

        self.assertEqual(status, 200)
        self.assertIn("生成しました", runner.prompt(0))
        self.assertNotIn("A" * 4096, runner.prompt(0))

    def test_the_limit_is_measured_on_the_assembled_cli_input(self):
        # 判定するのは CLI へ渡す stream-json の 1 行そのもの。本文だけを別に見積もると、
        # JSON の枠のぶん実際より小さく数えてしまい、上限際のリクエストが素通りする。
        runner = FakeRunner()
        server_url = self.start_server(make_config(max_request_bytes=100), runner)

        status, body = self.post(server_url, {"input": "あ" * 30})

        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "request_too_large")
        self.assertEqual(runner.calls, [])

    def test_system_prompt_is_counted_toward_the_limit(self):
        # system プロンプトは --append-system-prompt として毎ターン CLI へ渡し直すため、
        # 発言と同じく Claude API のリクエスト上限に効く。数えないと上限を素通りして、
        # 413 ではなく CLI の起動失敗になる。
        runner = FakeRunner()
        server_url = self.start_server(make_config(max_request_bytes=1024), runner)

        status, body = self.post(
            server_url,
            {"instructions": "い" * 2048, "input": "やあ"},
        )

        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "request_too_large")
        self.assertEqual(runner.calls, [])

    def test_oversized_input_does_not_cancel_the_running_turn(self):
        # 同じ会話の次のリクエストは、会話を借りる時点で相手の消えた実行を落とす。実行できない
        # 入力でそこまで進むと、中断済みのターンを弾くためだけに落としてしまう。
        # 続き具合によらず超えると分かる入力は、会話へ触る前に弾く。
        runner = HangingRunner()
        server_url = self.start_server(make_config(max_request_bytes=1024), runner)

        # 引き継ぎの対象になる実行、つまり相手の消えた実行を先に作る。
        self.post_and_disconnect(
            server_url, {"input": "1問目", "prompt_cache_key": "thread-1"}
        )
        for _ in range(500):
            if runner.processes:
                break
            time.sleep(0.01)
        process = runner.processes[0]

        status, body = self.post(
            server_url, {"input": "あ" * 2048, "prompt_cache_key": "thread-1"}
        )

        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "request_too_large")
        self.assertEqual(process.killed, 0)
        self.assertEqual(len(runner.processes), 1)
        # 先行ターンを終わらせてからテストを閉じる。
        process.kill()

    def test_parallel_turn_on_the_same_thread_runs_alongside_the_running_one(self):
        # 同じスレッドから並行して届くリクエスト（/side など）で、動いているターンを落とさない。
        # 落とすと先に走っていた側が exit=-9 の 502 を受け取る。
        runner = HangingRunner()
        server_url = self.start_server(make_config(), runner)

        for index, text in enumerate(("1問目", "2問目"), start=1):
            thread = threading.Thread(
                target=self.post,
                args=(server_url, {"input": text, "prompt_cache_key": "thread-1"}),
                daemon=True,
            )
            thread.start()
            self.addCleanup(thread.join, 5)
            for _ in range(500):
                if len(runner.processes) == index:
                    break
                time.sleep(0.01)

        # 2 本目は待たずに走り出し、1 本目は生きたまま。
        self.assertEqual(len(runner.processes), 2)
        self.assertEqual(runner.processes[0].killed, 0)

        # 両方を閉じてからテストを抜ける。
        for process in runner.processes:
            process.kill()

    def test_oversized_local_model_body_is_rejected_when_upstream_is_enabled(self):
        server_url = self.start_server(
            make_config(
                max_request_bytes=64,
                max_upstream_request_bytes=1024,
                upstream_base_url="http://127.0.0.1:1",
            ),
            FakeRunner(),
        )
        status, body = self.post(
            server_url,
            {"model": "claude-opus-5-5", "input": "x" * 200},
        )
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "request_too_large")


class ClaudeCliFailureTest(BridgeHTTPTestCase):
    def assert_failure(self, runner, status, code):
        base_url = self.start_server(make_config(), runner)
        got_status, body = self.post(base_url, {"input": "やあ"})
        self.assertEqual(got_status, status)
        self.assertEqual(body["error"]["code"], code)
        return body

    def test_timeout_returns_504(self):
        # 出力が終わらないプロセスは時間切れで kill し、タイムアウトとして返す。
        runner = HangingRunner()
        base_url = self.start_server(make_config(timeout_seconds=0.2), runner)

        status, body = self.post(base_url, {"input": "やあ"})

        self.assertEqual(status, 504)
        self.assertEqual(body["error"]["code"], "claude_cli_timeout")
        self.assertEqual(body["error"]["type"], "timeout_error")
        self.assertEqual(runner.processes[0].killed, 1)

    def test_missing_cli_returns_500(self):
        runner = FakeRunner(raises=FileNotFoundError("claude"))
        self.assert_failure(runner, 500, "claude_cli_not_found")

    def test_permission_error_returns_500(self):
        runner = FakeRunner(
            raises=PermissionError(errno.EACCES, "Permission denied", "/usr/bin/claude")
        )
        body = self.assert_failure(runner, 500, "claude_cli_launch_failed")
        self.assertEqual(body["error"]["type"], "api_error")

    def test_other_os_error_returns_500(self):
        # 実行形式が不正など、起動段階のその他の OSError も OpenAI error 形式で返す。
        runner = FakeRunner(raises=OSError(errno.ENOEXEC, "Exec format error"))
        self.assert_failure(runner, 500, "claude_cli_launch_failed")

    def test_non_zero_exit_returns_502(self):
        runner = FakeRunner(stdout=b"", returncode=1, stderr=b"credit balance too low")
        body = self.assert_failure(runner, 502, "claude_cli_failed")
        self.assertIn("credit balance too low", body["error"]["message"])

    def test_error_result_returns_502(self):
        runner = FakeRunner(cli_lines(result_event("上限に達しました", is_error=True)))
        self.assert_failure(runner, 502, "claude_cli_error_result")

    def test_invalid_json_output_returns_502(self):
        runner = FakeRunner(b"not json at all\n")
        self.assert_failure(runner, 502, "claude_cli_invalid_output")

    def test_missing_result_event_returns_502(self):
        # 正常終了しても result 行が来なければ最終テキストが確定しない。
        runner = FakeRunner(cli_lines({"type": "system", "subtype": "init"}))
        self.assert_failure(runner, 502, "claude_cli_invalid_output")


class ClaudeCliUnitTest(unittest.TestCase):
    def test_build_command_omits_system_prompt_when_empty(self):
        self.assertEqual(
            build_command(
                make_config(claude_path="/opt/homebrew/bin/claude"), None, Session(), "sid-1"
            ),
            [
                "/opt/homebrew/bin/claude",
                "--print",
                "--output-format",
                "stream-json",
                # 入力側も JSONL にする。文字列入力では画像を渡せない。
                "--input-format",
                "stream-json",
                "--verbose",
                # --print では thinking 本文が省略されるため、明示して中身を出させる。
                "--thinking-display",
                "summarized",
                "--strict-mcp-config",
                "--permission-mode",
                "bypassPermissions",
                "--model",
                "opus",
                # 新規の会話は ID を固定して開始し、次のリクエストから続きを書き足せるようにする。
                "--session-id",
                "sid-1",
            ],
        )

    def test_build_command_uses_configured_claude_path_verbatim(self):
        # パス文字列は引数配列にそのまま入り、シェル解釈されない。
        config = make_config(claude_path="/opt/my tools/claude; rm -rf /")
        self.assertEqual(
            build_command(config, None, Session(), "sid-1")[0], "/opt/my tools/claude; rm -rf /"
        )

    def test_run_claude_uses_configured_working_dir(self):
        runner = FakeRunner()
        config = make_config(working_dir="/tmp")

        run_claude_stream(config, TEXT_CONTENT, None, Session(), runner)

        call = runner.calls[0]
        self.assertEqual(call["cwd"], "/tmp")
        # 標準入出力は全てパイプで受け取る。stderr を読み捨てないと子プロセスが詰まる。
        self.assertEqual(call["stdin"], subprocess.PIPE)
        self.assertEqual(call["stdout"], subprocess.PIPE)
        self.assertEqual(call["stderr"], subprocess.PIPE)

    def test_stderr_snippet_is_truncated(self):
        runner = FakeRunner(stdout=b"", returncode=1, stderr=b"x" * 50000)
        with self.assertRaises(BridgeError) as raised:
            run_claude_stream(make_config(), TEXT_CONTENT, None, Session(), runner)
        self.assertLess(len(raised.exception.message), 3000)

    def test_stream_claude_reports_thinking_and_tool_use_then_text(self):
        runner = FakeRunner(
            cli_lines(
                {"type": "system", "subtype": "init"},
                # assistant / result 以外の行は読み飛ばす。
                {"type": "stream_event", "event": {"type": "content_block_delta"}},
                thinking_event("まず一覧を確認する"),
                tool_use_event("Bash", {"command": "ls -la"}),
                result_event("一覧です"),
            )
        )

        events = list(stream_claude(make_config(), TEXT_CONTENT, None, Session(), runner))

        # 思考もツール使用も同じ「途中経過」として扱う。
        self.assertEqual(
            events,
            [
                ("reasoning", "まず一覧を確認する"),
                ("reasoning", "**Bash を実行中**\nls -la"),
                ("text", "一覧です"),
            ],
        )

    def test_stream_claude_reports_text_blocks_as_messages(self):
        # 本文ブロックは message。最後の 1 件は result と同じ本文なので落とす。
        runner = FakeRunner(
            cli_lines(
                text_event("まず一覧を確認します"),
                tool_use_event("Bash", {"command": "ls -la"}),
                text_event("一覧です"),
                result_event("一覧です"),
            )
        )

        events = list(stream_claude(make_config(), TEXT_CONTENT, None, Session(), runner))

        self.assertEqual(
            events,
            [
                ("message", "まず一覧を確認します"),
                ("reasoning", "**Bash を実行中**\nls -la"),
                ("text", "一覧です"),
            ],
        )

    def test_empty_text_block_is_skipped(self):
        # 空白だけの本文ブロックは見せる内容がないので送らない。
        runner = FakeRunner(cli_lines(text_event("  \n "), result_event("done")))

        events = list(stream_claude(make_config(), TEXT_CONTENT, None, Session(), runner))

        self.assertEqual(events, [("text", "done")])

    def test_empty_thinking_block_is_skipped(self):
        # 思考が省略された行は本文が空になる。見せる内容がないので送らない。
        runner = FakeRunner(cli_lines(thinking_event(""), result_event("done")))

        events = list(stream_claude(make_config(), TEXT_CONTENT, None, Session(), runner))

        self.assertEqual(events, [("text", "done")])

    def test_stream_claude_survives_mcp_tool_use(self):
        # MCP ツールを使うと assistant / user に tool_use・tool_result が挟まる。
        runner = FakeRunner(
            cli_lines(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "スレッドを調べます"},
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "mcp__codex_app_server__thread_list",
                                "input": {"limit": 2},
                            },
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "{}"}
                        ]
                    },
                },
                result_event("スレッドは 2 件です"),
            )
        )

        events = list(stream_claude(make_config(), TEXT_CONTENT, None, Session(), runner))

        # 同じ assistant 行に並ぶ本文とツール使用は、書かれた順で message と reasoning になる。
        # 引数に主要なキーがなければツール名だけを見せる。
        self.assertEqual(
            events,
            [
                ("message", "スレッドを調べます"),
                ("reasoning", "**mcp__codex_app_server__thread_list を実行中**"),
                ("text", "スレッドは 2 件です"),
            ],
        )

    def test_tool_detail_is_truncated(self):
        runner = FakeRunner(
            cli_lines(tool_use_event("Bash", {"command": "x" * 5000}), result_event("done"))
        )

        events = list(stream_claude(make_config(), TEXT_CONTENT, None, Session(), runner))

        self.assertLess(len(events[0][1]), 300)

    def test_result_requires_string_result(self):
        runner = FakeRunner(cli_lines({"type": "result", "is_error": False, "result": None}))
        with self.assertRaises(BridgeError) as raised:
            run_claude_stream(make_config(), TEXT_CONTENT, None, Session(), runner)
        self.assertEqual(raised.exception.status, 502)

    def test_run_claude_returns_text(self):
        runner = FakeRunner()
        self.assertEqual(run_claude_stream(make_config(), TEXT_CONTENT, None, Session(), runner), "こんにちは")

    def test_invalid_json_line_terminates_the_cli_process(self):
        runner = FakeRunner(b"{\n")
        with self.assertRaises(BridgeError):
            run_claude_stream(make_config(), TEXT_CONTENT, None, Session(), runner)
        self.assert_terminated(runner.processes[0])

    def test_closing_the_stream_terminates_the_cli_process(self):
        # クライアント切断でジェネレータが破棄されても、CLI を裏で走らせ続けない。
        runner = FakeRunner(
            cli_lines(tool_use_event("Bash", {"command": "ls"}), result_event("一覧です"))
        )
        events = stream_claude(make_config(), TEXT_CONTENT, None, Session(), runner)
        next(events)
        events.close()

        self.assert_terminated(runner.processes[0])

    def test_normal_completion_does_not_kill_the_cli_process(self):
        runner = FakeRunner()

        run_claude_stream(make_config(), TEXT_CONTENT, None, Session(), runner)

        process = runner.processes[0]
        self.assertEqual(process.killed, 0)
        self.assertEqual(process.waited, 1)

    def assert_terminated(self, process) -> None:
        """子プロセスを落として回収したこと。"""

        self.assertEqual(process.killed, 1)
        self.assertEqual(process.waited, 1)

    def test_launch_os_error_becomes_bridge_error(self):
        runner = FakeRunner(raises=PermissionError(errno.EACCES, "Permission denied"))
        with self.assertRaises(BridgeError) as raised:
            run_claude_stream(make_config(claude_path="/opt/claude"), TEXT_CONTENT, None, Session(), runner)
        self.assertEqual(raised.exception.status, 500)
        self.assertEqual(raised.exception.code, "claude_cli_launch_failed")
        self.assertIn("/opt/claude", raised.exception.message)


class ParseRequestUnitTest(unittest.TestCase):
    def test_agent_task_and_followup_reach_cli_session(self):
        task = {
            "type": "agent_message",
            "author": "/root",
            "recipient": "/root/editor",
            "content": [{"type": "input_text", "text": "この文章を添削してください。"}],
        }
        for backend in (CLAUDE_BACKEND, ANTIGRAVITY_BACKEND):
            with self.subTest(backend=backend):
                session = Session()
                initial = parse_request({"input": [task]}, "test-model")
                sent = session.prepare(initial.messages, backend)
                self.assertIn("この文章を添削", build_content(sent)[0]["text"])
                self.assertIn("author=/root recipient=/root/editor", build_content(sent)[0]["text"])
                session.confirm("test-session")
                followup = {**task, "content": [{"type": "input_text", "text": "もっと短く。"}]}
                parsed = parse_request({"input": [task, {
                    "role": "assistant", "content": "添削しました。",
                }, followup]}, "test-model")
                sent = session.prepare(parsed.messages, backend)
                self.assertTrue(session.resume)
                self.assertEqual(len(sent), 1)
                self.assertEqual(build_content(sent)[0]["text"],
                                 "[Codex agent message] author=/root recipient=/root/editor\nもっと短く。")

    def test_agent_report_preserves_author_and_text_order(self):
        parsed = parse_request({"input": [{
            "type": "agent_message", "author": "/root/editor", "recipient": "/root",
            "content": [{"type": "input_text", "text": "報告："},
                        {"type": "input_text", "text": "添削完了。"}],
        }]}, "test-model")
        self.assertEqual(build_content(parsed.messages)[0]["text"],
                         "[Codex agent message] author=/root/editor recipient=/root\n報告：\n添削完了。")

    def test_invalid_agent_message_is_rejected_not_silently_dropped(self):
        task = {"type": "agent_message", "author": "/root", "recipient": "/root/editor",
                "content": [{"type": "input_text", "text": "依頼"}]}
        for change in ({"author": None}, {"recipient": ""}, {"content": []},
                       {"content": [{"type": "encrypted_content", "encrypted_content": "opaque"}]},
                       {"content": [image_part()]}):
            with self.subTest(change=change), self.assertRaises(BridgeError) as raised:
                parse_request({"input": [{"role": "user", "content": "環境"},
                                         {**task, **change}]}, "test-model")
            self.assertEqual(raised.exception.status, 400)

    def test_string_input_becomes_prompt(self):
        parsed = parse_request({"input": "やあ"}, "opus")
        self.assertEqual(build_content(parsed.messages), ({"type": "text", "text": "やあ"},))
        self.assertIsNone(parsed.system_prompt)
        self.assertEqual(parsed.model, "opus")

    def test_body_must_be_object(self):
        with self.assertRaises(BridgeError) as raised:
            parse_request(["やあ"], "opus")
        self.assertEqual(raised.exception.status, 400)

    def test_stream_defaults_to_false(self):
        self.assertFalse(parse_request({"input": "やあ"}, "opus").stream)
        self.assertFalse(parse_request({"input": "やあ", "stream": False}, "opus").stream)
        self.assertTrue(parse_request({"input": "やあ", "stream": True}, "opus").stream)

    def test_tools_are_ignored_regardless_of_value(self):
        # ツールは実行しないので、値を検証せずそのまま捨てる。
        for tools in ([], [{"type": "function", "name": "shell"}], None, "auto"):
            with self.subTest(tools=tools):
                parsed = parse_request({"input": "やあ", "tools": tools}, "opus")
                self.assertEqual(build_content(parsed.messages), ({"type": "text", "text": "やあ"},))
                self.assertIsNone(parsed.system_prompt)

    def test_non_message_input_items_are_ignored(self):
        # Codex は additional_tools などを input に混ぜて送る。message だけ拾う。
        parsed = parse_request(
            {
                "input": [
                    {"type": "additional_tools", "tools": [{"name": "shell"}]},
                    {"type": "message", "role": "user", "content": "やあ"},
                    {"type": "function_call", "name": "f", "arguments": "{}"},
                ]
            },
            "opus",
        )
        self.assertEqual(build_content(parsed.messages), ({"type": "text", "text": "やあ"},))
        self.assertIsNone(parsed.system_prompt)

    def test_image_keeps_its_position_between_texts(self):
        parsed = parse_request(
            {
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "前"},
                            image_part(),
                            {"type": "input_text", "text": "後"},
                        ],
                    }
                ]
            },
            "opus",
        )
        self.assertEqual(
            [block["type"] for block in build_content(parsed.messages)], ["text", "image", "text"]
        )
        self.assertEqual(build_content(parsed.messages)[0]["text"], "前")
        self.assertEqual(build_content(parsed.messages)[2]["text"], "後")

    def test_image_only_message_is_accepted(self):
        parsed = parse_request(
            {"input": [{"role": "user", "content": [image_part()]}]}, "opus"
        )
        self.assertEqual([block["type"] for block in build_content(parsed.messages)], ["image"])

    def test_image_in_a_labelled_conversation_keeps_the_role_label(self):
        parsed = parse_request(
            {
                "input": [
                    {"role": "user", "content": "1問目"},
                    {"role": "assistant", "content": "回答"},
                    {"role": "user", "content": [image_part(), {"type": "input_text", "text": "これは？"}]},
                ]
            },
            "opus",
        )
        self.assertEqual(
            [block["type"] for block in build_content(parsed.messages)], ["text", "image", "text"]
        )
        self.assertEqual(
            build_content(parsed.messages)[0]["text"], "[user]\n1問目\n\n[assistant]\n回答\n\n[user]\n"
        )
        self.assertEqual(build_content(parsed.messages)[2]["text"], "これは？")

    def test_http_image_url_is_passed_through(self):
        parsed = parse_request(
            {"input": [{"role": "user", "content": [image_part("https://example.com/a.png")]}]},
            "opus",
        )
        self.assertEqual(
            build_content(parsed.messages)[0]["source"],
            {"type": "url", "url": "https://example.com/a.png"},
        )

    def test_unsupported_image_inputs_are_rejected(self):
        cases = {
            "media_type": "data:image/bmp;base64,AAAA",
            "encoding": "data:image/png,AAAA",
            "scheme": "file:///tmp/a.png",
        }
        for name, url in cases.items():
            with self.subTest(name=name), self.assertRaises(BridgeError) as raised:
                parse_request(
                    {"input": [{"role": "user", "content": [image_part(url)]}]}, "opus"
                )
            self.assertEqual(raised.exception.status, 400)
            self.assertEqual(raised.exception.param, "input[0].content[0]")

    def test_image_without_url_is_rejected(self):
        with self.assertRaises(BridgeError) as raised:
            parse_request(
                {"input": [{"role": "user", "content": [{"type": "input_image"}]}]}, "opus"
            )
        self.assertEqual(raised.exception.status, 400)

    def test_image_in_a_system_message_is_rejected(self):
        # --append-system-prompt は文字列しか渡せないため、画像は置けない。
        with self.assertRaises(BridgeError) as raised:
            parse_request(
                {
                    "input": [
                        {"role": "developer", "content": [image_part()]},
                        {"role": "user", "content": "やあ"},
                    ]
                },
                "opus",
            )
        self.assertEqual(raised.exception.status, 400)

    def test_tool_choice_and_previous_response_id_do_not_reach_the_cli(self):
        # 受理はするが、CLI へ渡すプロンプトにも system prompt にも残さない。
        parsed = parse_request(
            {"input": "やあ", "tool_choice": "auto", "previous_response_id": "resp_1"}, "opus"
        )
        self.assertEqual(build_content(parsed.messages), ({"type": "text", "text": "やあ"},))
        self.assertIsNone(parsed.system_prompt)


class ConfigTest(unittest.TestCase):
    def test_defaults_bind_to_loopback(self):
        config = build_config([], env={})
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8787)

    def test_default_claude_model_does_not_collide_with_upstream_names(self):
        # 上流に実在する名前を使うと、その名前が上流一覧から除かれてアプリで選べなくなる。
        config = build_config([], env={})
        self.assertEqual(config.model, "claude-opus-5-5")
        self.assertEqual(
            config.claude_models,
            ("claude-opus-5-5", "claude-fable-5-1"),
        )

    def test_empty_claude_models_is_rejected(self):
        # model 省略時の既定に先頭を使うため、1 件以上あることを起動時に保証する。
        with self.assertRaises(ValueError):
            build_config(["--claude-models", " , "], env={})

    def test_env_overrides(self):
        config = build_config(
            [],
            env={
                "CLAUDE_BRIDGE_PORT": "9000",
                "CLAUDE_BRIDGE_MODEL": "sonnet",
                "CLAUDE_BRIDGE_TIMEOUT_SECONDS": "30",
                "CLAUDE_BRIDGE_MAX_UPSTREAM_REQUEST_BYTES": "123456",
            },
        )
        self.assertEqual(config.port, 9000)
        self.assertEqual(config.model, "sonnet")
        self.assertEqual(config.timeout_seconds, 30.0)
        self.assertEqual(config.max_upstream_request_bytes, 123456)

    def test_non_loopback_bind_is_rejected(self):
        with self.assertRaises(ValueError):
            build_config(["--host", "0.0.0.0"], env={})
        with self.assertRaises(ValueError):
            build_config(["--host", "192.0.2.1"], env={})

    def test_missing_working_dir_is_rejected(self):
        with self.assertRaises(ValueError):
            build_config(["--working-dir", "/no/such/directory"], env={})


class UpstreamRoutingTest(BridgeHTTPTestCase):
    """claude_models だけを Claude CLI が処理し、それ以外は上流へ素通しする。"""

    def start_upstream(self) -> tuple[str, list]:
        received = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append({"path": self.path, "body": json.loads(body)})
                payload = json.dumps({"output_text": "上流の応答"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}/backend-api/codex", received

    def test_gpt_model_is_relayed_upstream(self):
        upstream_url, received = self.start_upstream()
        runner = FakeRunner()
        base_url = self.start_server(make_config(upstream_base_url=upstream_url), runner)

        status, payload = self.post(base_url, {"model": "gpt-5.4", "input": "やあ"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "上流の応答")
        # 受信パスの /v1 を中継先のパスへ置き換え、本文はそのまま渡す。
        self.assertEqual(received[0]["path"], "/backend-api/codex/responses")
        self.assertEqual(received[0]["body"], {"model": "gpt-5.4", "input": "やあ"})
        # 上流へ流すリクエストでは Claude CLI を起動しない。
        self.assertEqual(runner.calls, [])

    def test_model_switch_preserves_messages_and_tools_without_local_item_ids(self):
        upstream_url, received = self.start_upstream()
        runner = FakeRunner()
        base_url = self.start_server(make_config(upstream_base_url=upstream_url), runner)
        stream = ResponseStream("claude-opus-5-5")
        progress = stream.reasoning("作業中")[0][1]["item"]
        legacy = {**progress, "id": "rs_1df8e19e9e87435197014a32736fece7"}
        message = stream.message("修正しました")[-1][1]["item"]
        call = stream.function_calls([
            {"call_id": "call_test", "name": "exec_command", "arguments": {"cmd": "pwd"}},
        ])[0][1]["item"]
        search = stream.tool_search_call("feedback")[0][1]["item"]
        result = {"type": "function_call_output", "call_id": "call_test", "output": "/workspace"}
        search_result = {"type": "tool_search_output", "call_id": search["call_id"], "tools": []}
        native = {"type": "reasoning", "id": "rs_openai", "encrypted_content": "opaque", "summary": []}
        # ID が旧形式と一致しても、暗号化された正規の推論状態は加工しない。
        native_uuid = {**native, "id": legacy["id"]}
        native_reference = {"type": "item_reference", "id": "rs_openai"}
        user = {"role": "user", "content": "続きを実行"}
        payload = {
            "model": "gpt-5.6-sol", "store": False,
            "input": [progress, legacy,
                      message, call, result, search, search_result, native, native_uuid, native_reference, user],
        }

        status, _ = self.post(base_url, payload)

        self.assertEqual(status, 200)
        expected = [
            {k: v for k, v in message.items() if k != "id"},
            {k: v for k, v in call.items() if k != "id"}, result,
            {k: v for k, v in search.items() if k != "id"}, search_result,
            native, native_uuid, native_reference, user,
        ]
        self.assertEqual(received[0]["body"], {**payload, "input": expected})
        self.assertEqual(len(payload["input"]), 11)
        self.assertIn("id", message)
        self.assertEqual(runner.calls, [])

    def test_native_history_is_relayed_byte_for_byte(self):
        for payload in (
            {"model": "gpt-5.6-sol", "input": "hello", "store": False},
            {"model": "gpt-5.6-sol", "input": [
                {"type": "reasoning", "id": "rs_native", "summary": []},
                {"type": "reasoning", "encrypted_content": "opaque", "summary": []},
                {"type": "item_reference", "id": "rs_native"},
                {"type": "item_reference", "id": "rs_1df8e19e9e87435197014a32736fece7"},
                {"type": "message", "id": "msg_native", "role": "assistant", "content": []},
            ]},
            {"input": [None, "invalid", {"id": 1}, {"type": "unknown", "id": "rs_1df8e19e9e87435197014a32736fece7"}]},
            [],
        ):
            with self.subTest(payload=payload):
                original = json.dumps(payload, indent=2).encode()
                self.assertIs(responses_body(payload, original), original)

    def test_gpt_body_can_exceed_local_cli_limit(self):
        upstream_url, received = self.start_upstream()
        base_url = self.start_server(
            make_config(
                upstream_base_url=upstream_url,
                max_request_bytes=64,
                max_upstream_request_bytes=1024,
            ),
            FakeRunner(),
        )

        status, payload = self.post(
            base_url,
            {"model": "gpt-5.4", "input": "x" * 200},
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "上流の応答")
        self.assertEqual(received[0]["body"]["input"], "x" * 200)

    def test_claude_model_is_handled_by_the_cli(self):
        upstream_url, received = self.start_upstream()
        runner = FakeRunner()
        base_url = self.start_server(make_config(upstream_base_url=upstream_url), runner)

        status, payload = self.post(base_url, {"model": "claude-opus-5-5", "input": "やあ"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")
        self.assertEqual(received, [])
        self.assertEqual(len(runner.calls), 1)

    def test_model_absent_is_handled_by_the_cli(self):
        # 既定は claude_models の先頭。CLI の実行モデル名を既定にすると上流へ流れてしまう。
        upstream_url, received = self.start_upstream()
        runner = FakeRunner()
        base_url = self.start_server(make_config(upstream_base_url=upstream_url), runner)

        status, payload = self.post(base_url, {"input": "やあ"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")
        self.assertEqual(payload["model"], "claude-opus-5-5")
        self.assertEqual(received, [])


class ImagePassthroughTest(BridgeHTTPTestCase):
    """画像 API はモデル判定せず、本文と認証を上流へそのまま中継する。"""

    def start_upstream(self, status=200, response_body=b'{"data":[]}'):
        received = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append(
                    {
                        "path": self.path,
                        "body": body,
                        "content_type": self.headers["Content-Type"],
                        "authorization": self.headers["Authorization"],
                    }
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

            def log_message(self, format: str, *args) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}/backend-api/codex", received

    def test_generations_json_is_relayed_with_query_and_authorization(self):
        upstream_url, received = self.start_upstream(response_body=b"generated")
        base_url = self.start_server(make_config(upstream_base_url=upstream_url), FakeRunner())
        body = b'{"model":"gpt-image-2","prompt":"orange banner"}'

        status, headers, response = self.post_bytes(
            base_url,
            "/v1/images/generations?client_version=0.146.0",
            body,
            "application/json",
            headers={"Authorization": "Bearer chatgpt-token"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(response, b"generated")
        self.assertEqual(
            received,
            [
                {
                    "path": "/backend-api/codex/images/generations?client_version=0.146.0",
                    "body": body,
                    "content_type": "application/json",
                    "authorization": "Bearer chatgpt-token",
                }
            ],
        )

    def test_edits_multipart_is_relayed_without_decoding(self):
        upstream_url, received = self.start_upstream(response_body=b"edited")
        base_url = self.start_server(make_config(upstream_base_url=upstream_url), FakeRunner())
        boundary = "image-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="prompt"\r\n\r\n'
            "make it orange\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        content_type = f"multipart/form-data; boundary={boundary}"

        status, _, response = self.post_bytes(
            base_url,
            "/v1/images/edits",
            body,
            content_type,
            headers={"Authorization": "Bearer chatgpt-token"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(response, b"edited")
        self.assertEqual(received[0]["path"], "/backend-api/codex/images/edits")
        self.assertEqual(received[0]["body"], body)
        self.assertEqual(received[0]["content_type"], content_type)
        self.assertEqual(received[0]["authorization"], "Bearer chatgpt-token")

    def test_image_body_can_exceed_local_cli_limit(self):
        upstream_url, received = self.start_upstream(response_body=b"generated")
        base_url = self.start_server(
            make_config(
                upstream_base_url=upstream_url,
                max_request_bytes=64,
                max_upstream_request_bytes=1024,
            ),
            FakeRunner(),
        )
        body = b'{"model":"gpt-image-2","prompt":"' + (b"x" * 200) + b'"}'

        status, _, response = self.post_bytes(
            base_url,
            "/v1/images/generations",
            body,
            "application/json",
            headers={"Authorization": "Bearer chatgpt-token"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(response, b"generated")
        self.assertEqual(received[0]["body"], body)

    def test_upstream_error_status_and_body_are_preserved(self):
        upstream_url, _ = self.start_upstream(status=400, response_body=b"upstream error")
        base_url = self.start_server(make_config(upstream_base_url=upstream_url), FakeRunner())

        status, headers, response = self.post_bytes(
            base_url,
            "/v1/images/generations",
            b"{}",
            "application/json",
            headers={"Authorization": "Bearer chatgpt-token"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(response, b"upstream error")

    def test_image_endpoint_requires_upstream(self):
        base_url = self.start_server(make_config(), FakeRunner())

        status, _, response = self.post_bytes(
            base_url, "/v1/images/generations", b"{}", "application/json"
        )

        self.assertEqual(status, 503)
        self.assertEqual(json.loads(response)["error"]["code"], "upstream_not_configured")


class AddDirTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paths = (os.path.join(directory.name, "one"), os.path.join(directory.name, "two"))
        for path in self.paths:
            os.mkdir(path)

    def test_add_dir_is_absent_by_default(self):
        self.assertNotIn(
            "--add-dir", build_command(make_config(), None, Session(), "sid-1")
        )

    def test_add_dirs_reach_the_cli_as_one_flag(self):
        # --add-dir は可変長引数なので、ディレクトリを続けて並べる。
        command = build_command(make_config(add_dirs=self.paths), None, Session(), "sid-1")
        index = command.index("--add-dir")

        self.assertEqual(command[index + 1 : index + 3], list(self.paths))
        self.assertEqual(command.count("--add-dir"), 1)

    def test_repeated_add_dir_arguments_are_collected(self):
        config = build_config(["--add-dir", self.paths[0], "--add-dir", self.paths[1]], env={})

        self.assertEqual(config.add_dirs, self.paths)

    def test_add_dirs_env_is_split_by_pathsep(self):
        config = build_config([], env={"CLAUDE_BRIDGE_ADD_DIRS": os.pathsep.join(self.paths)})

        self.assertEqual(config.add_dirs, self.paths)

    def test_missing_add_dir_is_rejected(self):
        with self.assertRaises(ValueError):
            build_config(["--add-dir", os.path.join(self.paths[0], "missing")], env={})


def conversation(*pairs: tuple[str, str]) -> tuple[Message, ...]:
    return tuple(Message(role, (text,)) for role, text in pairs)


class SessionTest(unittest.TestCase):
    """会話の続き具合に応じて、CLI へ渡す発言を絞り込む。"""

    def run_turn(
        self,
        session: Session,
        messages: tuple[Message, ...],
        backend: str = CLAUDE_BACKEND,
    ) -> tuple[dict, ...]:
        """prepare から CLI 起動までを進めた 1 ターン。"""

        unseen = session.prepare(messages, backend)
        session.start(FakeProcess(b""))
        session.confirm(transcript_id(session))
        return build_content(unseen)

    def test_first_turn_sends_whole_history_without_resume(self):
        session = Session()
        content = self.run_turn(session, conversation(("user", "1問目")))

        self.assertFalse(session.resume)
        self.assertEqual(content, ({"type": "text", "text": "1問目"},))

    def test_second_turn_resumes_with_only_the_new_message(self):
        session = Session()
        self.run_turn(session, conversation(("user", "1問目")))
        session_id = session.handle

        content = self.run_turn(
            session, conversation(("user", "1問目"), ("assistant", "回答"), ("user", "2問目"))
        )

        self.assertTrue(session.resume)
        # 転記に残る自分の発言は送り返さない。同じ ID の会話へ書き足す。
        self.assertEqual(content, ({"type": "text", "text": "2問目"},))
        self.assertEqual(session.handle, session_id)

    def test_rewritten_history_starts_a_new_session(self):
        session = Session()
        self.run_turn(session, conversation(("user", "1問目")))
        session_id = session.handle

        # Codex 側の圧縮などで先頭が置き換わった会話。続きにできないので開始し直す。
        content = self.run_turn(session, conversation(("user", "これまでの要約"), ("user", "2問目")))

        self.assertFalse(session.resume)
        self.assertNotEqual(session.handle, session_id)
        self.assertEqual(
            content, ({"type": "text", "text": "[user]\nこれまでの要約\n\n[user]\n2問目"},)
        )

    def test_resent_request_repeats_the_latest_message(self):
        session = Session()
        messages = conversation(("user", "1問目"))
        self.run_turn(session, messages)

        # 中断後の再送。差分は空だが、転記の続きを促すため直近の発言をもう一度渡す。
        content = self.run_turn(session, messages)

        self.assertTrue(session.resume)
        self.assertEqual(content, ({"type": "text", "text": "1問目"},))

    def test_failed_launch_keeps_the_session_unstarted(self):
        session = Session()
        session.prepare(conversation(("user", "1問目")), CLAUDE_BACKEND)

        # CLI が起動しなければ転記も作られないため、次回は --resume を使わない。
        unseen = session.prepare(
            conversation(("user", "1問目"), ("user", "2問目")), CLAUDE_BACKEND
        )

        self.assertFalse(session.resume)
        self.assertEqual(
            build_content(unseen),
            ({"type": "text", "text": "[user]\n1問目\n\n[user]\n2問目"},),
        )


class SessionRegistryTest(unittest.TestCase):
    def test_same_key_reuses_the_conversation(self):
        registry = SessionRegistry()
        first = registry.acquire("thread-1")
        registry.release(first)

        second = registry.acquire("thread-1")
        registry.release(second)

        self.assertIs(second, first)

    def test_missing_key_gets_a_throwaway_session(self):
        registry = SessionRegistry()
        first = registry.acquire(None)
        registry.release(first)

        second = registry.acquire(None)
        registry.release(second)

        self.assertIsNot(second, first)

    def test_oldest_conversation_is_dropped(self):
        registry = SessionRegistry(max_sessions=2)
        sessions = {}
        for key in ("thread-1", "thread-2", "thread-3"):
            sessions[key] = registry.acquire(key)
            registry.release(sessions[key])

        # 上限を超えた分は古い会話から捨て、次に来たときは新しいセッションから始める。
        self.assertIsNot(registry.acquire("thread-1"), sessions["thread-1"])
        self.assertIs(registry.acquire("thread-3"), sessions["thread-3"])

    def test_running_conversation_survives_a_full_registry(self):
        registry = SessionRegistry(max_sessions=1)
        running = registry.acquire("thread-1")

        # 実行中の会話を捨てると、同じ会話を二重に走らせてしまう。空きがなくても残す。
        registry.release(registry.acquire("thread-2"))
        registry.release(running)

        self.assertIs(registry.acquire("thread-1"), running)

    def test_reserved_handover_survives_release_and_eviction(self):
        registry = SessionRegistry(max_sessions=1)
        running = registry.acquire("thread-1")
        self.assertTrue(running.take_over())
        registry.release(running)

        # 先行ハンドラの解放後も、予約した会話を別の要求や容量整理へ渡さない。
        parallel = registry.acquire("thread-1")
        self.assertIsNot(parallel, running)
        registry.release(parallel)
        registry.release(registry.acquire("thread-2"))
        self.assertIs(registry._sessions["thread-1"], running)

        running.lock.acquire()
        running.attach(None)
        registry.release(running)

    def test_new_request_cancels_the_running_one(self):
        registry = SessionRegistry()
        running = registry.acquire("thread-1")
        process = FakeProcess(b"")
        running.start(process)

        taken = threading.Event()

        def take_over():
            registry.release(registry.acquire("thread-1"))
            taken.set()

        thread = threading.Thread(target=take_over)
        thread.start()
        self.addCleanup(thread.join, 5)
        for _ in range(500):
            if process.killed:
                break
            time.sleep(0.01)

        # 実行中の CLI は待たずに落とす。引き継ぎ側はロックが空くまで待つ。
        self.assertEqual(process.killed, 1)
        self.assertFalse(taken.is_set())
        registry.release(running)
        thread.join(5)
        self.assertTrue(taken.is_set())

    def test_holding_a_conversation_registers_the_client_at_once(self):
        # 借りてから相手を登録するまでを分けると、その間に届いたリクエストが「相手のいない
        # 実行」と誤って判定し、その場限りのセッションへ逃げずに引き継ぎ待ちへ入る。
        session = Session()
        client, peer = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(peer.close)

        self.assertTrue(session.try_hold(client))

        # 借りた直後から、相手のいる実行として見える。
        self.assertFalse(session.take_over())

    def test_parallel_request_gets_its_own_session(self):
        # Codex の /side のように、同じ prompt_cache_key で並行してリクエストが届く。相手が
        # 残っている実行を落とすと、その結果を待っている相手が中身のないエラーを受け取る。
        # 空くまで待たせると、長いターンの裏で無音のまま相手の待機上限を超える。どちらも
        # 取らず、その場限りのセッションで並行して実行する。
        registry = SessionRegistry()
        client, peer = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(peer.close)
        running = registry.acquire("thread-1", client)
        process = FakeProcess(b"")
        running.start(process)

        side = registry.acquire("thread-1")

        self.assertEqual(process.killed, 0)
        self.assertIsNot(side, running)
        registry.release(side)
        registry.release(running)
        # その場限りのセッションは会話へ残らない。次のリクエストは元の会話を続ける。
        self.assertIs(registry.acquire("thread-1"), running)

    def test_running_request_is_cancelled_once_its_client_is_gone(self):
        # 中断や切断後の再送では、先行する実行の相手がもういない。待っても誰も受け取らない
        # ので、その CLI を落として引き継ぐ。
        registry = SessionRegistry()
        client, peer = socket.socketpair()
        self.addCleanup(client.close)
        running = registry.acquire("thread-1", client)
        process = FakeProcess(b"")
        running.start(process)
        peer.close()

        thread = threading.Thread(
            target=lambda: registry.release(registry.acquire("thread-1"))
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        for _ in range(500):
            if process.killed:
                break
            time.sleep(0.01)

        self.assertEqual(process.killed, 1)
        registry.release(running)
        thread.join(5)

    def test_only_one_request_takes_over_a_dropped_run(self):
        # 落とした実行はもう引き継ぎの対象ではない。2 人目まで引き継ぎ待ちに入ると、
        # 引き継いだ側のターンが終わるまで無音で待たされる。
        registry = SessionRegistry()
        client, peer = socket.socketpair()
        self.addCleanup(client.close)
        running = registry.acquire("thread-1", client)
        process = FakeProcess(b"")
        running.start(process)
        peer.close()

        taken = []
        thread = threading.Thread(
            target=lambda: taken.append(registry.acquire("thread-1"))
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        for _ in range(500):
            if process.killed:
                break
            time.sleep(0.01)

        # 落としたのは 1 人目だけ。2 人目はその場限りのセッションで並行して走る。
        second = registry.acquire("thread-1")
        self.assertEqual(process.killed, 1)
        self.assertIsNot(second, running)
        registry.release(second)

        registry.release(running)
        thread.join(5)
        self.assertEqual(taken, [running])
        registry.release(running)

    def test_takeover_waits_for_a_run_that_has_not_started_its_cli(self):
        # CLI が動き出す前のターンでも、相手が消えていれば引き継ぐ。落とすものはないが、
        # ロックが空くのを待てば会話を続けられる。諦めて並行させると、本来は続きにできる
        # 再送が別の会話として枝分かれする。
        registry = SessionRegistry()
        client, peer = socket.socketpair()
        self.addCleanup(client.close)
        running = registry.acquire("thread-1", client)
        peer.close()

        taken = []
        thread = threading.Thread(
            target=lambda: taken.append(registry.acquire("thread-1"))
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        time.sleep(0.1)

        # 会話が空くまで待つ。その場限りのセッションへは逃げない。
        self.assertEqual(taken, [])
        registry.release(running)
        thread.join(5)
        self.assertEqual(taken, [running])
        registry.release(running)

    def test_finished_request_is_not_cancelled(self):
        registry = SessionRegistry()
        session = registry.acquire("thread-1")
        process = FakeProcess(b"")
        session.start(process)
        registry.release(session)

        registry.release(registry.acquire("thread-1"))

        self.assertEqual(process.killed, 0)


class SessionEndpointTest(BridgeHTTPTestCase):
    def post_turn(self, base_url: str, payload: dict, key: str | None) -> None:
        if key is not None:
            payload = {**payload, "prompt_cache_key": key}
        self.post(base_url, payload)

    def test_same_conversation_resumes_with_the_new_message_only(self):
        runner = FakeRunner()
        base_url = self.start_server(make_config(), runner)

        self.post_turn(base_url, {"input": "1問目"}, "thread-1")
        self.post_turn(
            base_url,
            {
                "input": [
                    {"role": "user", "content": "1問目"},
                    {"role": "assistant", "content": "回答"},
                    {"role": "user", "content": "2問目"},
                ]
            },
            "thread-1",
        )

        first, second = (call["command"] for call in runner.calls)
        self.assertEqual(first[first.index("--session-id") + 1], second[second.index("--resume") + 1])
        self.assertNotIn("--resume", first)
        self.assertNotIn("--session-id", second)
        self.assertEqual(runner.prompt(1), "2問目")

    def test_conversation_without_key_never_resumes(self):
        runner = FakeRunner()
        base_url = self.start_server(make_config(), runner)

        self.post_turn(base_url, {"input": "1問目"}, None)
        self.post_turn(base_url, {"input": "2問目"}, None)

        first, second = (call["command"] for call in runner.calls)
        self.assertNotIn("--resume", second)
        self.assertNotEqual(
            first[first.index("--session-id") + 1], second[second.index("--session-id") + 1]
        )

    def test_prompt_cache_key_must_be_a_string(self):
        runner = FakeRunner()
        base_url = self.start_server(make_config(), runner)

        status, payload = self.post(base_url, {"input": "やあ", "prompt_cache_key": 1})

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["param"], "prompt_cache_key")


class SlowProcess(FakeProcess):
    """標準出力が届くまで待たせるプロセス。無音区間を作る。"""

    class Stdout:
        def __init__(self, payload: bytes, delay: float):
            self.payload = payload
            self.delay = delay

        def __iter__(self):
            time.sleep(self.delay)
            return iter(io.BytesIO(self.payload))

    def __init__(self, payload: bytes, delay: float):
        super().__init__(b"")
        self.stdout = self.Stdout(payload, delay)


class KeepaliveTest(unittest.TestCase):
    """無音が続く間に書き足す合図。"""

    def test_writes_while_the_caller_is_silent(self):
        written = []

        with Keepalive(written.append, "PING", 0.02):
            time.sleep(0.15)

        self.assertTrue(written)
        self.assertEqual(set(written), {"PING"})

    def test_does_not_write_while_the_caller_keeps_writing(self):
        written = []

        with Keepalive(written.append, "PING", 1.0) as alive:
            for _ in range(6):
                time.sleep(0.01)
                alive.write("本文")

        self.assertEqual(written, ["本文"] * 6)

    def test_stops_writing_after_the_block_ends(self):
        written = []

        with Keepalive(written.append, "PING", 0.02):
            time.sleep(0.05)
        count = len(written)
        time.sleep(0.1)

        self.assertEqual(len(written), count)

    def test_gives_up_when_the_client_is_gone(self):
        def write(_chunk):
            raise BrokenPipeError

        # 合図の書き込みが失敗しても、呼び出し側へは伝播させずに合図だけをやめる。
        with Keepalive(write, "PING", 0.02):
            time.sleep(0.08)


class KeepaliveStreamTest(BridgeHTTPTestCase):
    """SSE に流れる合図。"""

    def setUp(self):
        original = server_module.KEEPALIVE_INTERVAL_SECONDS
        server_module.KEEPALIVE_INTERVAL_SECONDS = 0.02
        self.addCleanup(setattr, server_module, "KEEPALIVE_INTERVAL_SECONDS", original)

    def start_slow_server(self) -> str:
        runner = FakeRunner()
        runner.make_process = lambda: SlowProcess(runner.stdout, 0.2)
        return self.start_server(make_config(), runner)

    def test_silent_cli_still_sends_events(self):
        _, events = self.post_stream(self.start_slow_server(), {"input": "やあ"})
        names = [name for name, _ in events]

        self.assertIn(KEEPALIVE_EVENT, names)
        # 合図は本文より先に出て、最後は通常どおり完了する。
        self.assertLess(names.index(KEEPALIVE_EVENT), names.index("response.completed"))
        self.assertEqual(events[-1][1], "[DONE]")

    def test_keepalive_does_not_disturb_response_events(self):
        _, events = self.post_stream(self.start_slow_server(), {"input": "やあ"})
        payloads = [data for name, data in events if name and name != KEEPALIVE_EVENT]

        # sequence_number は Responses のイベントだけで連番になる。
        self.assertEqual(
            [data["sequence_number"] for data in payloads], list(range(1, len(payloads) + 1))
        )
        keepalives = [data for name, data in events if name == KEEPALIVE_EVENT]
        self.assertEqual(keepalives[0], {"type": KEEPALIVE_EVENT})


if __name__ == "__main__":
    unittest.main()
