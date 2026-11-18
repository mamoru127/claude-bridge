"""Antigravity CLI（`agy acp`）を受け持つモデルの実行・常駐多重化・会話継続・モデル一覧の検証。

擬似応答は agy 1.5.11 の `agy acp --dangerously-skip-permissions` 実出力を写したもの。
起動時に 1 回だけ `initialize` を行い、会話の開始に `session/new`、再起動後の引き継ぎに
`session/resume`、モデル選択に `session/set_model`、各ターンに `session/prompt` を使う。
途中経過は `session/update` 通知で届き、中断は `session/cancel` 通知で行う。
"""

import io
import json
import queue
import subprocess
import threading
import time
import unittest

from claude_bridge.antigravity_cli import (
    BILLING_ENV_KEYS,
    AntigravityClient,
    agy_env,
    build_command,
    stream_antigravity,
)
from claude_bridge.claude_cli import transcript_id
from claude_bridge.cli_process import final_text
from claude_bridge.config import (
    ANTIGRAVITY_BACKEND,
    CLAUDE_BACKEND,
    DEFAULT_ANTIGRAVITY_MODELS,
    build_config,
)
from claude_bridge.errors import BridgeError
from claude_bridge.responses import Message, build_models, build_prompt
from claude_bridge.server import BridgeServer
from claude_bridge.sessions import Session

from tests.test_app_server_mcp import FakeAppServer
from tests.test_bridge import (
    PNG_DATA_URL,
    BridgeHTTPTestCase,
    FakeProcess,
    FakeRunner,
    HangingRunner,
    cli_lines,
    conversation,
    image_part,
    make_config,
)
from tests.test_bridge import result_event as claude_result

HIGH = "gemini-3.8-flash-high"
MEDIUM = "gemini-3.8-flash-medium"

_STREAM_END = object()


def update_notice(session_id: str, session_update: str, **fields) -> dict:
    """`session/update` 通知 1 件。"""

    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": session_update,
                **fields,
            },
        },
    }


def message_chunk(text: str, session_id: str = "conv-1") -> dict:
    return update_notice(
        session_id, "agent_message_chunk", content={"type": "text", "text": text}
    )


def thought_chunk(text: str, session_id: str = "conv-1") -> dict:
    return update_notice(
        session_id, "agent_thought_chunk", content={"type": "text", "text": text}
    )


def usage_update(used: int = 0, session_id: str = "conv-1") -> dict:
    return update_notice(session_id, "usage_update", used=used, size=1048576)


def tool_call_notice(
    name: str,
    args: dict | None = None,
    call_id: str = "call_1",
    session_id: str = "conv-1",
) -> dict:
    payload = args or {}
    return update_notice(
        session_id,
        "tool_call",
        toolCallId=call_id,
        name=name,
        args=payload,
        rawInput=payload,
        status="in_progress",
    )


def tool_update_notice(
    name: str = "",
    status: str = "completed",
    call_id: str = "call_1",
    session_id: str = "conv-1",
    args: dict | None = None,
    error: object = None,
    raw_output: object = None,
    title: str | None = None,
) -> dict:
    fields: dict = {"toolCallId": call_id, "status": status}
    if name:
        fields["name"] = name
    if args is not None:
        fields["args"] = args
    if error is not None:
        fields["error"] = error
    if raw_output is not None:
        fields["rawOutput"] = raw_output
    if title is not None:
        fields["title"] = title
    return update_notice(session_id, "tool_call_update", **fields)


def prompt_turn(
    response: str = "こんにちは",
    *updates: dict,
    stop_reason: str = "end_turn",
    error: object = None,
    hold: bool = False,
    ignore_cancel: bool = False,
) -> dict:
    """FakeAcpRunner に積む 1 回分の `session/prompt` の振る舞い。"""

    return {
        "updates": list(updates),
        "response": response,
        "stop_reason": stop_reason,
        "error": error,
        "hold": hold,
        "ignore_cancel": ignore_cancel,
    }


class FakeAcpProcess:
    """`agy acp` の JSON-RPC 会話を模倣する擬似子プロセス。"""

    class _Stdin:
        def __init__(self, owner: "FakeAcpProcess") -> None:
            self._owner = owner
            self._buffer = b""
            self.closed = False

        def write(self, data: bytes) -> int:
            if self.closed:
                raise BrokenPipeError("stdin closed")
            self._buffer += data
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if line.strip():
                    self._owner._on_stdin_line(line)
            return len(data)

        def flush(self) -> None:
            if self.closed:
                raise BrokenPipeError("stdin closed")

        def close(self) -> None:
            if not self.closed:
                self.closed = True
                self._owner._on_stdin_closed()

    class _Stdout:
        def __init__(self, out_queue: queue.Queue) -> None:
            self._queue = out_queue

        def __iter__(self):
            while True:
                item = self._queue.get()
                if item is _STREAM_END:
                    return
                yield item

    def __init__(self, runner: "FakeAcpRunner", returncode: int = 0, stderr: bytes = b"") -> None:
        self._runner = runner
        self._out: queue.Queue = queue.Queue()
        self.stdin = self._Stdin(self)
        self.stdout = self._Stdout(self._out)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.killed = 0
        self.waited = 0
        self.requests: list[dict] = []
        self._held_prompts: dict[str, tuple[int, bool]] = {}
        self._lock = threading.Lock()

    def emit(self, message: dict | bytes) -> None:
        raw = message if isinstance(message, bytes) else (
            json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        )
        self._out.put(raw)

    def close_stdout(self, returncode: int | None = None) -> None:
        if returncode is not None:
            self.returncode = returncode
        self._out.put(_STREAM_END)

    def wait(self) -> int:
        self.waited += 1
        return self.returncode

    def kill(self) -> None:
        self.killed += 1
        self._out.put(_STREAM_END)

    def _on_stdin_closed(self) -> None:
        if not self._held_prompts:
            self._out.put(_STREAM_END)

    def _on_stdin_line(self, line: bytes) -> None:
        message = json.loads(line.decode("utf-8"))
        self.requests.append(message)
        self._runner.all_requests.append(message)
        self._runner.handle(self, message)

    def hold_prompt(self, session_id: str, request_id: int, ignore_cancel: bool) -> None:
        with self._lock:
            self._held_prompts[session_id] = (request_id, ignore_cancel)
        self._runner.held_event.set()

    def cancel_held_prompt(self, session_id: str) -> None:
        with self._lock:
            held = self._held_prompts.get(session_id)
            if held is None or held[1]:
                return
            del self._held_prompts[session_id]
        self.emit({"jsonrpc": "2.0", "id": held[0], "result": {"stopReason": "cancelled"}})


class FakeAcpRunner:
    """`agy acp` を模倣するランナー。1 プロセスで複数セッション・複数ターンに応答する。"""

    def __init__(
        self,
        turns: list[dict] | None = None,
        session_ids: list[str] | None = None,
        resumable: dict[str, str] | set[str] | None = None,
        init_error: object = None,
        init_result: object = None,
        set_model_error: object = None,
        new_session_error: object = None,
    ) -> None:
        self._turns = list(turns) if turns is not None else [prompt_turn()]
        self._session_ids = list(session_ids or [])
        self._next_num = 1
        self._resumable = resumable
        self._init_error = init_error
        self._init_result = (
            {"protocolVersion": 1, "agentInfo": {"name": "Antigravity ACP", "version": "1.5.11"}}
            if init_result is None
            else init_result
        )
        self._set_model_error = set_model_error
        self._new_session_error = new_session_error
        self.calls: list[dict] = []
        self.processes: list[FakeAcpProcess] = []
        self.all_requests: list[dict] = []
        self.known_sessions: set[str] = set()
        self.held_event = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, command, **kwargs) -> FakeAcpProcess:
        self.calls.append({"command": command, **kwargs})
        process = FakeAcpProcess(self)
        self.processes.append(process)
        return process

    def command(self, index: int = 0) -> list[str]:
        return self.calls[index]["command"]

    def methods(self) -> list[str]:
        return [req["method"] for req in self.all_requests if "method" in req]

    def requests_for(self, method: str) -> list[dict]:
        return [req for req in self.all_requests if req.get("method") == method]

    def prompt(self, index: int = 0) -> str:
        prompts = self.requests_for("session/prompt")
        blocks = prompts[index]["params"]["prompt"]
        return "".join(block["text"] for block in blocks if block.get("type") == "text")

    def models_set(self) -> list[str]:
        return [req["params"]["modelId"] for req in self.requests_for("session/set_model")]

    def handle(self, process: FakeAcpProcess, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "session/cancel":
            process.cancel_held_prompt(params.get("sessionId", ""))
            return

        if method == "initialize":
            if self._init_error is not None:
                process.emit({"jsonrpc": "2.0", "id": request_id, "error": self._init_error})
            else:
                process.emit({"jsonrpc": "2.0", "id": request_id, "result": self._init_result})
            return

        if method == "session/new":
            if self._new_session_error is not None:
                process.emit(
                    {"jsonrpc": "2.0", "id": request_id, "error": self._new_session_error}
                )
                return
            with self._lock:
                if self._session_ids:
                    session_id = self._session_ids.pop(0)
                else:
                    session_id = f"conv-{self._next_num}"
                    self._next_num += 1
                self.known_sessions.add(session_id)
            process.emit({"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": session_id}})
            return

        if method == "session/resume":
            target = params.get("sessionId", "")
            resolved = self._resolve_resume(target)
            if resolved is None:
                process.emit(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": f"Failed to read session {target}"},
                    }
                )
                return
            self.known_sessions.add(resolved)
            process.emit({"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": resolved}})
            return

        if method == "session/set_model":
            if self._set_model_error is not None:
                process.emit({"jsonrpc": "2.0", "id": request_id, "error": self._set_model_error})
            else:
                process.emit({"jsonrpc": "2.0", "id": request_id, "result": {}})
            return

        if method == "session/prompt":
            session_id = params["sessionId"]
            with self._lock:
                turn = self._turns.pop(0) if len(self._turns) > 1 else self._turns[0]
            for raw_notice in turn["updates"]:
                notice = _rebind_session(raw_notice, session_id)
                process.emit(notice)
            if turn["hold"]:
                process.hold_prompt(session_id, request_id, turn["ignore_cancel"])
                return
            if turn["error"] is not None:
                process.emit({"jsonrpc": "2.0", "id": request_id, "error": turn["error"]})
                return
            if turn["response"] and not any(
                (item.get("params") or {}).get("update", {}).get("sessionUpdate")
                == "agent_message_chunk"
                for item in turn["updates"]
            ):
                process.emit(message_chunk(turn["response"], session_id=session_id))
            process.emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"stopReason": turn["stop_reason"]},
                }
            )

    def _resolve_resume(self, target: str) -> str | None:
        if isinstance(self._resumable, dict):
            return self._resumable.get(target)
        if isinstance(self._resumable, set):
            return target if target in self._resumable else None
        if target in self.known_sessions:
            return target
        return target


def _rebind_session(notice: dict, session_id: str) -> dict:
    """テストで session_id を省略した通知だけ、今回のセッション ID へ揃える。"""

    params = notice.get("params")
    if not isinstance(params, dict) or params.get("sessionId") != "conv-1":
        return notice
    return {**notice, "params": {**params, "sessionId": session_id}}


def agy_config(**overrides):
    """agy を実行する設定。config.model には振り分け後の実行モデル名が入る。"""

    return make_config(**{"model": HIGH, **overrides})


class AntigravityCommandTest(unittest.TestCase):
    def test_acp_command_uses_stdio_server_with_skip_permissions(self):
        command = build_command(agy_config(antigravity_path="/opt/bin/agy"), Session())

        self.assertEqual(
            command,
            [
                "/opt/bin/agy",
                "acp",
                "--dangerously-skip-permissions",
            ],
        )

    def test_add_dirs_are_not_passed_to_agy(self):
        # agy の --add-dir はワークスペースの追加で、そこの AGENTS.md と skills がどの
        # スレッドのモデル呼び出しにも毎回載る。操作は --dangerously-skip-permissions で
        # 既に通るため渡さない。
        command = build_command(
            agy_config(add_dirs=("/workspace", "/Users/example/.gemini")),
            Session(),
        )

        self.assertNotIn("--add-dir", command)
        self.assertNotIn("/workspace", command)
        self.assertIn("--dangerously-skip-permissions", command)

    def test_executable_path_is_not_shell_interpreted(self):
        config = agy_config(antigravity_path="/opt/my tools/agy; rm -rf /")
        self.assertEqual(build_command(config, Session())[0], "/opt/my tools/agy; rm -rf /")


class AntigravityStreamTest(unittest.TestCase):
    def run_stream(self, runner, config=None, session=None) -> list[tuple[str, str]]:
        return list(
            stream_antigravity(config or agy_config(), "やあ", session or Session(), runner)
        )

    def test_deltas_thoughts_and_tools_are_reported_in_order(self):
        runner = FakeAcpRunner(
            [
                prompt_turn(
                    "",
                    thought_chunk("まずファイルを"),
                    thought_chunk("確認する"),
                    tool_call_notice("read_file", {"EndLine": 50, "Path": "/tmp/app.py"}, "c1"),
                    tool_update_notice("read_file", status="completed", call_id="c1"),
                    message_chunk("こん"),
                    message_chunk("にちは"),
                )
            ]
        )

        self.assertEqual(
            self.run_stream(runner),
            [
                ("reasoning", "まずファイルを確認する"),
                # 開始と完了が両方届いても、開始表示は 1 回だけ。引数は EndLine より Path を優先。
                ("reasoning", "**read_file を実行中**\n/tmp/app.py"),
                ("delta", "こん"),
                ("delta", "にちは"),
                ("text", "こんにちは"),
            ],
        )

    def test_acp_handshake_session_model_and_prompt_are_sent(self):
        runner = FakeAcpRunner([prompt_turn("応答")], session_ids=["conv-10"])

        self.run_stream(runner)

        self.assertEqual(
            runner.methods(),
            ["initialize", "session/new", "session/set_model", "session/prompt"],
        )
        self.assertEqual(runner.models_set(), [HIGH])
        self.assertEqual(runner.prompt(0), "やあ")
        self.assertEqual(runner.calls[0]["stdin"], subprocess.PIPE)
        self.assertEqual(runner.calls[0]["stdout"], subprocess.PIPE)
        self.assertEqual(runner.calls[0]["stderr"], subprocess.PIPE)

    def test_api_key_env_is_not_passed_to_the_cli(self):
        # 月額枠だけを使う。API キー課金の経路になる変数は子へ渡さない。
        runner = FakeAcpRunner()

        self.run_stream(runner)

        passed = runner.calls[0]["env"]
        for name in BILLING_ENV_KEYS:
            self.assertNotIn(name, passed)
        self.assertIn("PATH", passed)

    def test_billing_env_keys_are_removed_from_a_polluted_environment(self):
        cleaned = agy_env(
            {
                "PATH": "/usr/bin",
                "HOME": "/Users/x",
                "GEMINI_API_KEY": "sk-test",
                "GOOGLE_API_KEY": "sk-test",
                "GOOGLE_GENAI_API_KEY": "sk-test",
            }
        )

        self.assertEqual(cleaned, {"PATH": "/usr/bin", "HOME": "/Users/x"})

    def test_tool_without_arguments_shows_only_the_name(self):
        runner = FakeAcpRunner([prompt_turn("done", tool_call_notice("list_dir", {}))])

        self.assertEqual(self.run_stream(runner)[0], ("reasoning", "**list_dir を実行中**"))

    def test_failed_tool_is_shown_as_a_failure(self):
        runner = FakeAcpRunner(
            [
                prompt_turn(
                    "できませんでした",
                    tool_call_notice("run_command", {"CommandLine": "ls -la"}, call_id="c1"),
                    tool_update_notice(
                        call_id="c1",
                        status="failed",
                        error="User denied permission to run command:\nls -la",
                    ),
                )
            ]
        )

        self.assertEqual(
            self.run_stream(runner),
            [
                ("reasoning", "**run_command を実行中**\nls -la"),
                ("reasoning", "**run_command が失敗**\nUser denied permission to run command:\nls -la"),
                ("delta", "できませんでした"),
                ("text", "できませんでした"),
            ],
        )

    def test_failed_tool_falls_back_to_title_when_error_detail_is_absent(self):
        # agy acp の read_file 失敗は error や rawOutput を持たず title だけを返すことがある。
        runner = FakeAcpRunner(
            [
                prompt_turn(
                    "",
                    tool_call_notice("read_file", {"Path": "/tmp/missing.txt"}, call_id="c1"),
                    tool_update_notice(
                        "read_file", call_id="c1", status="failed", title="File missing.txt"
                    ),
                )
            ]
        )

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.code, "antigravity_cli_tool_failed")
        self.assertIn("File missing.txt", raised.exception.message)

    def test_tool_detail_is_truncated(self):
        runner = FakeAcpRunner(
            [prompt_turn("done", tool_call_notice("run_command", {"CommandLine": "x" * 5000}))]
        )

        self.assertLess(len(self.run_stream(runner)[0][1]), 300)

    def test_empty_delta_and_unknown_updates_are_skipped(self):
        runner = FakeAcpRunner(
            [
                prompt_turn(
                    "",
                    usage_update(100),
                    update_notice("conv-1", "available_commands_update", availableCommands=[]),
                    message_chunk(""),
                    message_chunk("done"),
                )
            ]
        )

        self.assertEqual(self.run_stream(runner), [("delta", "done"), ("text", "done")])

    def test_server_originated_rpc_request_is_rejected_without_blocking(self):
        runner = FakeAcpRunner([prompt_turn("done")])
        original_handle = runner.handle

        def handle_with_client_request(process, message):
            if message.get("method") == "session/prompt":
                process.emit(
                    {
                        "jsonrpc": "2.0",
                        "id": "srv-1",
                        "method": "fs/read_text_file",
                        "params": {"path": "/tmp/a"},
                    }
                )
            original_handle(process, message)

        runner.handle = handle_with_client_request

        self.assertEqual(self.run_stream(runner), [("delta", "done"), ("text", "done")])
        replies = [req for req in runner.all_requests if req.get("id") == "srv-1"]
        self.assertEqual(replies[0]["error"]["code"], -32601)

    def test_line_without_jsonrpc_shape_is_rejected(self):
        runner = FakeRunner(b'{"step_update": {}}\n')

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.code, "antigravity_cli_invalid_output")
        self.assertEqual(runner.processes[0].killed, 1)

    def test_denied_tool_with_no_answer_is_reported_as_a_failure(self):
        runner = FakeAcpRunner(
            [
                prompt_turn(
                    "",
                    tool_call_notice("run_command", {"CommandLine": "ls -la"}, call_id="c1"),
                    tool_update_notice(
                        "run_command",
                        call_id="c1",
                        status="failed",
                        error="User denied permission to run command",
                    ),
                )
            ]
        )

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(raised.exception.code, "antigravity_cli_tool_failed")
        self.assertIn("User denied permission", raised.exception.message)

    def test_empty_answer_without_a_tool_failure_is_returned_as_is(self):
        runner = FakeAcpRunner([prompt_turn("")])

        self.assertEqual(self.run_stream(runner), [("text", "")])

    def test_result_confirms_the_conversation_id(self):
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        runner = FakeAcpRunner([prompt_turn("done")], session_ids=["conv-42"])

        self.run_stream(runner, session=session)

        self.assertEqual(session.handle, "conv-42")

    def test_accepted_message_confirms_and_cancels_cleanly_before_the_result(self):
        # 最初の session/update が届いた時点で発言は会話へ載っている。
        # 中断時はプロセスを殺さず session/cancel を送り、同じ会話を次へ引き継ぐ。
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        runner = FakeAcpRunner(
            [prompt_turn("", message_chunk("途中"), hold=True)], session_ids=["conv-5"]
        )
        client = AntigravityClient(agy_config(), runner)
        self.addCleanup(client.close)
        events = client.stream(agy_config(), "1問目", session)

        self.assertEqual(next(events), ("delta", "途中"))
        events.close()

        self.assertIn("session/cancel", runner.methods())
        self.assertEqual(runner.processes[0].killed, 0)
        self.assertEqual(session.handle, "conv-5")
        messages = conversation(("user", "1問目"), ("user", "やっぱり2問目"))
        self.assertEqual(session.prepare(messages, ANTIGRAVITY_BACKEND), messages[-1:])
        self.assertTrue(session.resume)

    def test_stopping_before_the_message_is_accepted_does_not_confirm(self):
        # session/new 完了直後に接続が切れ、session/prompt の受理前に終わった場合は未確定のまま。
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        runner = FakeAcpRunner(new_session_error={"code": -32603, "message": "auth failed"})

        with self.assertRaises(BridgeError):
            self.run_stream(runner, session=session)

        self.assertEqual(session.handle, "")

    def test_failure_after_the_message_is_accepted_keeps_the_conversation(self):
        session = Session()
        messages = conversation(("user", "1問目"))
        session.prepare(messages, ANTIGRAVITY_BACKEND)
        runner = FakeAcpRunner(
            [prompt_turn("", usage_update(0), error={"code": -32603, "message": "quota"})],
            session_ids=["conv-5"],
        )

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner, session=session)

        self.assertEqual(raised.exception.code, "antigravity_cli_error_result")
        self.assertEqual(session.handle, "conv-5")
        self.assertEqual(session.prepare(messages, ANTIGRAVITY_BACKEND), messages[-1:])
        self.assertTrue(session.resume)

    def test_resuming_missing_conversation_is_rejected_and_resets_session(self):
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        session.confirm("conv-missing")
        messages = conversation(("user", "1問目"), ("assistant", "回答"), ("user", "2問目"))
        session.prepare(messages, ANTIGRAVITY_BACKEND)
        runner = FakeAcpRunner(resumable=set())

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner, session=session)

        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(raised.exception.code, "antigravity_cli_conversation_lost")
        self.assertEqual(session.prepare(messages, ANTIGRAVITY_BACKEND), messages)
        self.assertFalse(session.resume)

    def test_resuming_into_another_conversation_id_is_rejected(self):
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        session.confirm("conv-7")
        messages = conversation(("user", "1問目"), ("assistant", "回答"), ("user", "2問目"))
        session.prepare(messages, ANTIGRAVITY_BACKEND)
        runner = FakeAcpRunner(resumable={"conv-7": "conv-other"})

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner, session=session)

        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(raised.exception.code, "antigravity_cli_conversation_lost")
        self.assertEqual(session.prepare(messages, ANTIGRAVITY_BACKEND), messages)
        self.assertFalse(session.resume)

    def test_update_of_another_conversation_does_not_confirm(self):
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        runner = FakeAcpRunner(
            [
                prompt_turn(
                    "",
                    update_notice("sub-1", "usage_update", used=10, size=100),
                    error={"code": -32603, "message": "boom"},
                )
            ],
            session_ids=["conv-5"],
        )

        with self.assertRaises(BridgeError):
            self.run_stream(runner, session=session)

        self.assertEqual(session.handle, "")

    def test_resumed_turn_on_new_acp_process_calls_session_resume(self):
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        session.confirm("conv-7")
        session.prepare(
            conversation(("user", "1問目"), ("assistant", "回答"), ("user", "2問目")),
            ANTIGRAVITY_BACKEND,
        )
        runner = FakeAcpRunner([prompt_turn("回答2")], resumable={"conv-7": "conv-7"})

        self.assertEqual(
            self.run_stream(runner, session=session),
            [("delta", "回答2"), ("text", "回答2")],
        )
        self.assertEqual(
            runner.methods(),
            ["initialize", "session/resume", "session/set_model", "session/prompt"],
        )
        self.assertEqual(session.handle, "conv-7")

    def test_error_result_does_not_confirm_the_conversation(self):
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        runner = FakeAcpRunner([prompt_turn("", error={"message": "quota exhausted"})])

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner, session=session)

        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(raised.exception.code, "antigravity_cli_error_result")
        self.assertIn("quota exhausted", raised.exception.message)
        self.assertEqual(session.handle, "")

    def test_missing_session_id_is_rejected(self):
        runner = FakeAcpRunner(session_ids=[""])

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.code, "antigravity_cli_invalid_output")

    def test_missing_stop_reason_is_rejected(self):
        runner = FakeAcpRunner([prompt_turn("done", stop_reason="")])

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.code, "antigravity_cli_invalid_output")

    def test_eof_during_prompt_is_rejected(self):
        runner = FakeAcpRunner([prompt_turn("途中", hold=True)])
        original_handle = runner.handle

        def close_mid_prompt(process, message):
            original_handle(process, message)
            if message.get("method") == "session/prompt":
                process.close_stdout(returncode=0)

        runner.handle = close_mid_prompt

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(raised.exception.code, "antigravity_cli_invalid_output")

    def test_invalid_json_terminates_the_cli_process(self):
        runner = FakeRunner(b"{\n")

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.code, "antigravity_cli_invalid_output")
        self.assertEqual(runner.processes[0].killed, 1)

    def test_non_object_line_is_rejected(self):
        runner = FakeRunner(b"[1, 2]\n")

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.code, "antigravity_cli_invalid_output")

    def test_non_zero_exit_reports_stderr(self):
        runner = FakeRunner(stdout=b"", returncode=1, stderr=b"not logged in")

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(raised.exception.code, "antigravity_cli_failed")
        self.assertIn("not logged in", raised.exception.message)

    def test_timeout_kills_the_cli_process(self):
        runner = HangingRunner()

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner, config=agy_config(timeout_seconds=0.05))

        self.assertEqual(raised.exception.status, 504)
        self.assertEqual(raised.exception.code, "antigravity_cli_timeout")
        self.assertEqual(runner.processes[0].killed, 1)

    def test_closing_the_stream_kills_wedged_process_that_ignores_cancel(self):
        # session/cancel に応答せず固まっているプロセスは、猶予後に強制終了する。
        runner = FakeAcpRunner(
            [prompt_turn("", message_chunk("途中"), hold=True, ignore_cancel=True)]
        )
        events = stream_antigravity(agy_config(timeout_seconds=0.05), "やあ", Session(), runner)
        next(events)
        events.close()

        self.assertGreaterEqual(runner.processes[0].killed, 1)

    def test_normal_completion_does_not_kill_the_cli_process(self):
        runner = FakeAcpRunner([prompt_turn("こんにちは")])

        final_text(stream_antigravity(agy_config(), "やあ", Session(), runner))

        self.assertEqual(runner.processes[0].killed, 0)

    def test_missing_cli_is_reported_as_a_launch_error(self):
        runner = FakeRunner(raises=FileNotFoundError("agy"))

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.status, 500)
        self.assertEqual(raised.exception.code, "antigravity_cli_not_found")


class AntigravityPromptTest(unittest.TestCase):
    """Responses API の入力を agy の単一プロンプトへ写す。"""

    def test_single_user_message_is_passed_verbatim(self):
        self.assertEqual(build_prompt(conversation(("user", "やあ")), None), "やあ")

    def test_system_is_kept_as_a_labelled_section(self):
        prompt = build_prompt(conversation(("user", "やあ")), "あなたは助手です")

        self.assertEqual(prompt, "[system]\nあなたは助手です\n\nやあ")

    def test_conversation_roles_are_labelled(self):
        prompt = build_prompt(
            conversation(("user", "1問目"), ("assistant", "回答"), ("user", "2問目")), None
        )

        self.assertEqual(prompt, "[user]\n1問目\n\n[assistant]\n回答\n\n[user]\n2問目")

    def test_image_input_is_rejected(self):
        messages = (Message("user", ("これは何", {"type": "image", "source": {}})),)

        with self.assertRaises(BridgeError) as raised:
            build_prompt(messages, None)

        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(raised.exception.code, "unsupported_content_part")


class AntigravityEndpointTest(BridgeHTTPTestCase):
    def test_argon_accepts_long_system_and_history_without_truncation(self):
        runner = FakeAcpRunner([prompt_turn("こんにちは"), prompt_turn("こんにちは")])
        base_url = self.start_server(
            agy_config(antigravity_models=("gemini-4-argon-high",)), runner
        )
        instructions = "指示\n" * 12000
        messages = [
            {"role": "user", "content": "履歴" * 10000},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "はい"},
        ]
        for index, stream in enumerate((False, True)):
            request = {
                "model": "gemini-4-argon-high",
                "instructions": instructions,
                "input": messages,
            }
            if stream:
                _, events = self.post_stream(base_url, request)
                self.assertTrue(any(name == "response.completed" for name, _ in events))
            else:
                status, _ = self.post(base_url, request)
                self.assertEqual(status, 200)
            prompt = runner.prompt(index)
            self.assertIn(instructions, prompt)
            self.assertIn("履歴" * 10000, prompt)
            self.assertTrue(prompt.endswith("はい"))
            self.assertNotIn(prompt, runner.command(0))
        # 常駐 ACP なので 2 リクエストでも起動したプロセスは 1 本だけ。
        self.assertEqual(len(runner.processes), 1)

    def test_non_stream_returns_the_result_response(self):
        runner = FakeAcpRunner([prompt_turn("こんにちは")])
        base_url = self.start_server(agy_config(), runner)

        status, payload = self.post(base_url, {"model": HIGH, "input": "やあ"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")
        self.assertEqual(payload["model"], HIGH)
        self.assertEqual(runner.prompt(), "やあ")
        self.assertEqual(runner.models_set(), [HIGH])

    def test_medium_model_reaches_the_acp_session(self):
        runner = FakeAcpRunner([prompt_turn("こんにちは")])
        base_url = self.start_server(make_config(model=MEDIUM), runner)

        self.post(base_url, {"model": MEDIUM, "input": "やあ"})

        self.assertEqual(runner.models_set(), [MEDIUM])

    def test_stream_sends_increments_then_the_final_text(self):
        runner = FakeAcpRunner(
            [
                prompt_turn(
                    "",
                    message_chunk("こん"),
                    tool_call_notice("read_file", {"Path": "app.py"}),
                    message_chunk("にちは"),
                )
            ]
        )
        base_url = self.start_server(agy_config(), runner)

        content_type, events = self.post_stream(base_url, {"model": HIGH, "input": "やあ"})
        types = [name for name, _ in events if name]

        self.assertTrue(content_type.startswith("text/event-stream"))
        self.assertEqual(
            [data["delta"] for name, data in events if name == "response.output_text.delta"],
            ["こん", "にちは"],
        )
        self.assertEqual(types.count("response.output_item.added"), 1)
        done = next(data for name, data in events if name == "response.output_text.done")
        self.assertEqual(done["text"], "こんにちは")
        completed = next(data for name, data in events if name == "response.completed")
        self.assertEqual(completed["response"]["output_text"], "こんにちは")

    def test_stream_shows_tool_progress_as_reasoning(self):
        runner = FakeAcpRunner(
            [prompt_turn("完了", tool_call_notice("run_command", {"CommandLine": "ls -la"}))]
        )
        base_url = self.start_server(agy_config(), runner)

        _, events = self.post_stream(base_url, {"model": HIGH, "input": "やあ"})

        summaries = [
            data["item"]["summary"][0]["text"]
            for name, data in events
            if name == "response.output_item.done" and data["item"]["type"] == "reasoning"
        ]
        self.assertEqual(summaries, ["**run_command を実行中**\nls -la"])

    def test_stream_failure_is_reported_as_response_failed(self):
        runner = FakeAcpRunner([prompt_turn("", error={"message": "quota"})])
        base_url = self.start_server(agy_config(), runner)

        _, events = self.post_stream(base_url, {"model": HIGH, "input": "やあ"})

        failed = next(data for name, data in events if name == "response.failed")
        self.assertEqual(failed["response"]["error"]["code"], "antigravity_cli_error_result")

    def test_system_instructions_reach_the_prompt(self):
        runner = FakeAcpRunner()
        base_url = self.start_server(agy_config(), runner)

        self.post(
            base_url,
            {"model": HIGH, "input": "やあ", "instructions": "あなたは助手です"},
        )

        self.assertEqual(runner.prompt(), "[system]\nあなたは助手です\n\nやあ")

    def test_image_input_is_rejected_with_400(self):
        runner = FakeAcpRunner()
        base_url = self.start_server(agy_config(), runner)

        status, payload = self.post(
            base_url,
            {
                "model": HIGH,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "これは何"},
                            image_part(PNG_DATA_URL),
                        ],
                    }
                ],
            },
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "unsupported_content_part")
        self.assertEqual(runner.calls, [])

    def test_rejected_image_keeps_the_claude_conversation(self):
        claude_runner = FakeRunner(cli_lines(claude_result("回答")))
        base_url = self.start_server(make_config(), claude_runner)

        self.post(
            base_url,
            {"model": "claude-opus-5-5", "input": "1問目", "prompt_cache_key": "t1"},
        )
        status, _ = self.post(
            base_url,
            {
                "model": HIGH,
                "prompt_cache_key": "t1",
                "input": [{"role": "user", "content": [image_part(PNG_DATA_URL)]}],
            },
        )
        self.post(
            base_url,
            {
                "model": "claude-opus-5-5",
                "prompt_cache_key": "t1",
                "input": [
                    {"role": "user", "content": "1問目"},
                    {"role": "assistant", "content": "回答"},
                    {"role": "user", "content": "2問目"},
                ],
            },
        )

        self.assertEqual(status, 400)
        self.assertEqual(len(claude_runner.calls), 2)
        self.assertIn("--resume", claude_runner.calls[1]["command"])

    def test_codex_tools_are_not_offered(self):
        runner = FakeAcpRunner([prompt_turn("こんにちは")])
        base_url = self.start_server(agy_config(), runner)

        status, payload = self.post(
            base_url,
            {
                "model": HIGH,
                "input": [
                    {"role": "user", "content": "やあ"},
                    {
                        "type": "additional_tools",
                        "tools": [
                            {"type": "tool_search"},
                            {
                                "type": "function",
                                "name": "create_thread",
                                "description": "スレッドを作る",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        ],
                    },
                ],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")
        self.assertEqual([item["type"] for item in payload["output"]], ["message"])
        self.assertEqual(len(runner.calls), 1)


class AntigravityConversationTest(BridgeHTTPTestCase):
    """常駐 ACP 上での会話継続・モデル切り替え・中断・再接続。"""

    def post_turn(self, base_url: str, payload: dict, key: str = "thread-1"):
        return self.post(base_url, {**payload, "prompt_cache_key": key})

    def second_turn_input(self) -> list:
        return [
            {"role": "user", "content": "1問目"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "2問目"},
        ]

    def test_second_turn_reuses_resident_acp_process_and_session(self):
        runner = FakeAcpRunner(
            [prompt_turn("回答"), prompt_turn("回答2")], session_ids=["conv-7"]
        )
        base_url = self.start_server(agy_config(), runner)

        self.post_turn(base_url, {"model": HIGH, "input": "1問目"})
        self.post_turn(base_url, {"model": HIGH, "input": self.second_turn_input()})

        # 同じ常駐プロセス・同じモデルなら 2 ターン目は session/prompt だけが飛ぶ。
        self.assertEqual(len(runner.processes), 1)
        self.assertEqual(
            runner.methods(),
            [
                "initialize",
                "session/new",
                "session/set_model",
                "session/prompt",
                "session/prompt",
            ],
        )
        prompts = runner.requests_for("session/prompt")
        self.assertEqual(prompts[0]["params"]["sessionId"], "conv-7")
        self.assertEqual(prompts[1]["params"]["sessionId"], "conv-7")
        self.assertEqual(runner.prompt(1), "2問目")

    def test_switching_antigravity_model_mid_thread_updates_session_model(self):
        runner = FakeAcpRunner(
            [prompt_turn("回答"), prompt_turn("回答2")], session_ids=["conv-7"]
        )
        base_url = self.start_server(agy_config(), runner)

        self.post_turn(base_url, {"model": HIGH, "input": "1問目"})
        self.post_turn(base_url, {"model": MEDIUM, "input": self.second_turn_input()})

        self.assertEqual(len(runner.processes), 1)
        self.assertEqual(runner.models_set(), [HIGH, MEDIUM])
        self.assertEqual(runner.prompt(1), "2問目")

    def test_idle_acp_disconnect_reconnects_and_resumes_conversation(self):
        runner = FakeAcpRunner(
            [prompt_turn("回答"), prompt_turn("回答2")], session_ids=["conv-7"]
        )
        base_url = self.start_server(agy_config(), runner)

        self.post_turn(base_url, {"model": HIGH, "input": "1問目"})
        # アイドル中に常駐プロセスが終了した場合でも、次のターンで再起動して session/resume する。
        runner.processes[0].close_stdout(returncode=0)
        for _ in range(100):
            if runner.processes[0].waited:
                break
            time.sleep(0.01)

        status, payload = self.post_turn(
            base_url, {"model": HIGH, "input": self.second_turn_input()}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "回答2")
        self.assertEqual(len(runner.processes), 2)
        self.assertIn("session/resume", [r["method"] for r in runner.processes[1].requests])
        self.assertEqual(runner.prompt(1), "2問目")

    def test_midturn_steer_cancels_only_target_session_and_keeps_server_alive(self):
        runner = FakeAcpRunner(
            [
                prompt_turn("", usage_update(0), hold=True),
                prompt_turn("他スレッドの回答"),
                prompt_turn("差し込み後の回答"),
            ],
            session_ids=["conv-a", "conv-b"],
        )
        server = BridgeServer(agy_config(), runner=runner)
        server.app_server = FakeAppServer()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.daemon = True
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        base_url = f"http://{host}:{port}"

        steered_result = []
        t1 = threading.Thread(
            target=lambda: steered_result.append(
                self.post_stream(
                    base_url,
                    {"model": HIGH, "input": "1問目", "prompt_cache_key": "thread-a"},
                )
            ),
            daemon=True,
        )
        t1.start()
        self.assertTrue(runner.held_event.wait(5))

        # 別スレッドのターンは同じ常駐 ACP プロセスのまま完走する。
        status_b, body_b = self.post_turn(
            base_url, {"model": HIGH, "input": "別の質問"}, key="thread-b"
        )
        self.assertEqual(status_b, 200)
        self.assertEqual(body_b["output_text"], "他スレッドの回答")

        # thread-a だけ hand_back で区切る。プロセスは殺さず session/cancel が送られる。
        self.assertTrue(server.sessions.hand_back("thread-a"))
        t1.join(5)
        names = [name for name, _ in steered_result[0][1]]
        self.assertIn("response.completed", names)
        self.assertNotIn("response.failed", names)
        self.assertEqual(runner.processes[0].killed, 0)
        self.assertEqual(len(runner.processes), 1)

        # 差し込み後のリクエストは同じ conv-a の続きとして処理される。
        status_a2, body_a2 = self.post_turn(
            base_url,
            {
                "model": HIGH,
                "input": [
                    {"role": "user", "content": "1問目"},
                    {"role": "user", "content": "英語で続けて"},
                ],
            },
            key="thread-a",
        )
        self.assertEqual(status_a2, 200)
        self.assertEqual(body_a2["output_text"], "差し込み後の回答")
        self.assertEqual(runner.prompt(2), "英語で続けて")

    def test_system_instructions_are_not_repeated_on_later_turns(self):
        runner = FakeAcpRunner(
            [prompt_turn("回答"), prompt_turn("回答")], session_ids=["conv-7"]
        )
        base_url = self.start_server(agy_config(), runner)
        instructions = "あなたは助手です"

        self.post_turn(
            base_url, {"model": HIGH, "instructions": instructions, "input": "1問目"}
        )
        self.post_turn(
            base_url,
            {"model": HIGH, "instructions": instructions, "input": self.second_turn_input()},
        )

        self.assertEqual(runner.prompt(0), "[system]\nあなたは助手です\n\n1問目")
        self.assertEqual(runner.prompt(1), "2問目")

    def test_added_developer_message_is_sent_once(self):
        runner = FakeAcpRunner([prompt_turn("回答")] * 3, session_ids=["conv-7"])
        base_url = self.start_server(agy_config(), runner)
        instructions = "あなたは助手です"
        added = {"role": "developer", "content": "使える skill の一覧"}
        third_turn_input = [
            *self.second_turn_input(),
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "3問目"},
        ]

        self.post_turn(
            base_url, {"model": HIGH, "instructions": instructions, "input": "1問目"}
        )
        self.post_turn(
            base_url,
            {
                "model": HIGH,
                "instructions": instructions,
                "input": [added, *self.second_turn_input()],
            },
        )
        self.post_turn(
            base_url,
            {"model": HIGH, "instructions": instructions, "input": [added, *third_turn_input]},
        )

        self.assertEqual(runner.prompt(1), "[system]\n使える skill の一覧\n\n2問目")
        self.assertEqual(runner.prompt(2), "3問目")

    def test_changed_instructions_are_sent_again_in_full(self):
        runner = FakeAcpRunner([prompt_turn("回答")] * 2, session_ids=["conv-7"])
        base_url = self.start_server(agy_config(), runner)
        kept = {"role": "developer", "content": "日本語で答える"}

        self.post_turn(
            base_url,
            {
                "model": HIGH,
                "instructions": "計画だけを立てる",
                "input": [kept, {"role": "user", "content": "1問目"}],
            },
        )
        self.post_turn(
            base_url,
            {
                "model": HIGH,
                "instructions": "実装まで進める",
                "input": [kept, *self.second_turn_input()],
            },
        )

        self.assertEqual(
            runner.prompt(1), "[system]\n実装まで進める\n\n日本語で答える\n\n2問目"
        )

    def test_new_conversation_receives_the_instructions_again(self):
        runner = FakeAcpRunner(
            [prompt_turn("回答"), prompt_turn("回答2")], session_ids=["conv-7", "conv-9"]
        )
        base_url = self.start_server(agy_config(), runner)
        instructions = "あなたは助手です"

        self.post_turn(
            base_url, {"model": HIGH, "instructions": instructions, "input": "1問目"}
        )
        self.post_turn(
            base_url,
            {
                "model": HIGH,
                "instructions": instructions,
                "input": [
                    {"role": "user", "content": "これまでの要約"},
                    {"role": "user", "content": "2問目"},
                ],
            },
        )

        self.assertEqual(len(runner.requests_for("session/new")), 2)
        self.assertTrue(runner.prompt(1).startswith("[system]\nあなたは助手です\n\n"))

    def test_added_instructions_alone_decide_the_size_of_a_resumed_turn(self):
        runner = FakeAcpRunner([prompt_turn("回答")] * 2, session_ids=["conv-7"])
        base_url = self.start_server(agy_config(max_request_bytes=400), runner)
        instructions = "あ" * 100
        added = {"role": "developer", "content": "い" * 100}

        first, _ = self.post_turn(
            base_url, {"model": HIGH, "instructions": instructions, "input": "1問目"}
        )
        second, _ = self.post_turn(
            base_url,
            {
                "model": HIGH,
                "instructions": instructions,
                "input": [added, *self.second_turn_input()],
            },
        )

        self.assertEqual((first, second), (200, 200))
        self.assertEqual(runner.prompt(1), "[system]\n" + "い" * 100 + "\n\n2問目")

    def test_oversized_instructions_are_rejected_on_the_first_turn(self):
        runner = FakeAcpRunner()
        base_url = self.start_server(agy_config(max_request_bytes=400), runner)

        status, body = self.post_turn(
            base_url, {"model": HIGH, "instructions": "あ" * 200, "input": "1問目"}
        )

        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "request_too_large")
        self.assertEqual(runner.calls, [])

    def test_turn_stopped_after_the_message_was_accepted_is_continued(self):
        runner = FakeAcpRunner(
            [
                prompt_turn("", message_chunk("途中"), error={"message": "interrupted"}),
                prompt_turn("回答"),
            ],
            session_ids=["conv-8"],
        )
        base_url = self.start_server(agy_config(), runner)
        instructions = "あなたは助手です"

        first, _ = self.post_turn(
            base_url, {"model": HIGH, "instructions": instructions, "input": "1問目"}
        )
        second, payload = self.post_turn(
            base_url,
            {
                "model": HIGH,
                "instructions": instructions,
                "input": [
                    {"role": "user", "content": "1問目"},
                    {"role": "user", "content": "方針を変えて2問目"},
                ],
            },
        )

        self.assertEqual((first, second), (502, 200))
        self.assertEqual(payload["output_text"], "回答")
        self.assertEqual(runner.prompt(1), "方針を変えて2問目")

    def test_lost_conversation_after_restart_is_restarted_with_the_whole_history(self):
        runner = FakeAcpRunner(
            [prompt_turn("回答"), prompt_turn("回答2")],
            session_ids=["conv-7", "conv-9"],
            resumable=set(),
        )
        base_url = self.start_server(agy_config(), runner)
        instructions = "あなたは助手です"
        second = {
            "model": HIGH,
            "instructions": instructions,
            "input": self.second_turn_input(),
        }

        self.post_turn(
            base_url, {"model": HIGH, "instructions": instructions, "input": "1問目"}
        )
        # プロセス再起動後に conv-7 がディスクから消えていた状況を再現する。
        runner.processes[0].close_stdout(returncode=0)
        for _ in range(100):
            if runner.processes[0].waited:
                break
            time.sleep(0.01)

        lost, body = self.post_turn(base_url, second)
        retried, payload = self.post_turn(base_url, second)

        self.assertEqual(lost, 502)
        self.assertEqual(body["error"]["code"], "antigravity_cli_conversation_lost")
        self.assertEqual(retried, 200)
        self.assertEqual(payload["output_text"], "回答2")
        # やり直しでは同じ常駐プロセス（2 代目）のまま session/new で全履歴を送る。
        self.assertEqual(len(runner.processes), 2)
        self.assertEqual(
            runner.prompt(1),
            "[system]\nあなたは助手です\n\n[user]\n1問目\n\n[assistant]\n回答\n\n[user]\n2問目",
        )

    def test_failed_first_turn_resends_the_whole_history(self):
        runner = FakeAcpRunner(
            [prompt_turn("", error={"message": "boom"}), prompt_turn("回答")],
            session_ids=["conv-7", "conv-8"],
        )
        base_url = self.start_server(agy_config(), runner)

        self.post_turn(base_url, {"model": HIGH, "input": "1問目"})
        self.post_turn(base_url, {"model": HIGH, "input": self.second_turn_input()})

        self.assertEqual(len(runner.requests_for("session/new")), 2)
        self.assertEqual(
            runner.prompt(1), "[user]\n1問目\n\n[assistant]\n回答\n\n[user]\n2問目"
        )

    def test_rewritten_history_starts_a_new_conversation(self):
        runner = FakeAcpRunner(
            [prompt_turn("回答"), prompt_turn("回答2")], session_ids=["conv-7", "conv-9"]
        )
        base_url = self.start_server(agy_config(), runner)

        self.post_turn(base_url, {"model": HIGH, "input": "1問目"})
        self.post_turn(
            base_url,
            {
                "model": HIGH,
                "input": [
                    {"role": "user", "content": "これまでの要約"},
                    {"role": "user", "content": "2問目"},
                ],
            },
        )

        self.assertEqual(len(runner.requests_for("session/new")), 2)

    def test_resent_request_repeats_the_latest_message(self):
        runner = FakeAcpRunner([prompt_turn("回答")] * 2, session_ids=["conv-7"])
        base_url = self.start_server(agy_config(), runner)

        self.post_turn(base_url, {"model": HIGH, "input": "1問目"})
        self.post_turn(base_url, {"model": HIGH, "input": "1問目"})

        prompts = runner.requests_for("session/prompt")
        self.assertEqual(prompts[1]["params"]["sessionId"], "conv-7")
        self.assertEqual(runner.prompt(1), "1問目")

    def test_switching_backend_restarts_the_conversation(self):
        """同じスレッドで Claude と Antigravity を切り替えても、相手の会話 ID を持ち込まない。"""

        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        session.confirm("conv-7")

        messages = conversation(("user", "1問目"), ("assistant", "回答"), ("user", "2問目"))
        unseen = session.prepare(messages, CLAUDE_BACKEND)

        self.assertFalse(session.resume)
        self.assertEqual(unseen, messages)
        self.assertNotEqual(transcript_id(session), "conv-7")

    def test_failed_switch_keeps_the_previous_conversation(self):
        """切り替えた先で実行が失敗しても、直前に確定していた会話は残す。"""

        session = Session()
        session.prepare(conversation(("user", "1問目")), CLAUDE_BACKEND)
        session.confirm("claude-transcript")

        runner = FakeRunner(raises=FileNotFoundError("agy"))
        messages = conversation(("user", "1問目"), ("assistant", "回答"), ("user", "2問目"))
        session.prepare(messages, ANTIGRAVITY_BACKEND)
        with self.assertRaises(BridgeError):
            list(stream_antigravity(agy_config(), "2問目", session, runner))

        unseen = session.prepare(messages, CLAUDE_BACKEND)

        self.assertTrue(session.resume)
        self.assertEqual(transcript_id(session), "claude-transcript")
        self.assertEqual(unseen, messages[-1:])


class AntigravityModelsTest(BridgeHTTPTestCase):
    def test_default_models_are_published_and_run_through_antigravity(self):
        config = build_config(["--port", "0"], env={})
        runner = FakeAcpRunner([prompt_turn("こんにちは") for _ in DEFAULT_ANTIGRAVITY_MODELS])
        base_url = self.start_server(config, runner)
        status, catalog = self.get_json(base_url, "/v1/models")
        self.assertEqual(status, 200)
        entries = {item["id"]: item for item in catalog["data"]}
        self.assertNotIn("gemini-4-carbon-high", entries)
        self.assertNotIn("gemini-4-carbon-medium", entries)
        for model in DEFAULT_ANTIGRAVITY_MODELS:
            self.assertEqual(entries[model]["owned_by"], "google")
            self.assertEqual(config.backend(model), ANTIGRAVITY_BACKEND)

        for model in DEFAULT_ANTIGRAVITY_MODELS:
            status, payload = self.post(base_url, {"model": model, "input": "やあ"})
            self.assertEqual(status, 200)
            self.assertEqual(payload["model"], model)
        self.assertEqual(runner.models_set(), list(DEFAULT_ANTIGRAVITY_MODELS))
        self.assertEqual(len(runner.processes), 1)

    def test_legacy_carbon_requests_run_argon(self):
        config = build_config(
            ["--port", "0", "--upstream-base-url", "https://upstream.example"], env={}
        )
        runner = FakeAcpRunner([prompt_turn("こんにちは"), prompt_turn("こんにちは")])
        base_url = self.start_server(config, runner)

        for effort in ("high", "medium"):
            old_model = f"gemini-4-carbon-{effort}"
            status, _ = self.post(base_url, {"model": old_model, "input": "やあ"})
            self.assertEqual(status, 200)
        self.assertEqual(
            runner.models_set(), ["gemini-4-argon-high", "gemini-4-argon-medium"]
        )

    def test_models_include_both_clis(self):
        base_url = self.start_server(
            make_config(claude_models=("claude-opus-5-5",)), FakeRunner()
        )

        status, payload = self.get_json(base_url, "/v1/models")

        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in payload["data"]], ["claude-opus-5-5", HIGH, MEDIUM]
        )
        owners = {item["id"]: item["owned_by"] for item in payload["data"]}
        self.assertEqual(owners["claude-opus-5-5"], "anthropic")
        self.assertEqual(owners[HIGH], "google")

    def test_codex_metadata_describes_antigravity(self):
        payload = build_models(make_config(claude_models=("claude-opus-5-5",)))
        entries = {item["slug"]: item for item in payload["models"]}

        self.assertEqual(entries[HIGH]["display_name"], f"Antigravity CLI ({HIGH})")
        self.assertIn("Antigravity", entries[HIGH]["description"])
        self.assertIn("テキストのみ", entries[HIGH]["description"])
        self.assertEqual(
            entries["claude-opus-5-5"]["display_name"], "Claude CLI (claude-opus-5-5)"
        )
        self.assertIn("画像", entries["claude-opus-5-5"]["description"])

    def test_antigravity_entries_keep_the_upstream_template_fields(self):
        upstream = {
            "data": [{"id": "gpt-5.5", "object": "model"}],
            "models": [{"slug": "gpt-5.5", "priority": 1, "supports_vision": True}],
        }
        payload = build_models(
            make_config(
                claude_models=("claude-opus-5-5",), upstream_base_url="https://upstream.example"
            ),
            upstream,
        )
        entries = {item["slug"]: item for item in payload["models"]}

        self.assertFalse(entries[HIGH]["supports_vision"])
        self.assertTrue(entries["claude-opus-5-5"]["supports_vision"])
        self.assertFalse(entries[HIGH]["prefer_websockets"])
        self.assertIsNone(entries[HIGH]["tool_mode"])
        self.assertEqual([item["priority"] for item in payload["models"]], [1, 2, 3, 4])

    def test_antigravity_models_can_be_disabled(self):
        payload = build_models(make_config(antigravity_models=()))

        self.assertNotIn(HIGH, [item["id"] for item in payload["data"]])

    def test_unknown_models_still_relay_upstream(self):
        config = make_config(upstream_base_url="https://upstream.example")

        self.assertEqual(config.backend("gpt-5.5"), "")
        self.assertEqual(config.backend(HIGH), ANTIGRAVITY_BACKEND)
        self.assertEqual(config.backend("claude-opus-5-5"), CLAUDE_BACKEND)


class AntigravityConfigTest(unittest.TestCase):
    def test_defaults(self):
        config = build_config([], env={})

        self.assertEqual(config.antigravity_path, "agy")
        self.assertEqual(config.antigravity_models, DEFAULT_ANTIGRAVITY_MODELS)
        self.assertEqual(
            config.antigravity_models,
            (
                "gemini-4-argon-high",
                "gemini-4-argon-medium",
                "gemini-4-hydrogen-high",
                "gemini-3.8-flash-high",
            ),
        )

    def test_env_overrides(self):
        config = build_config(
            [],
            env={
                "CLAUDE_BRIDGE_ANTIGRAVITY_PATH": "/opt/bin/agy",
                "CLAUDE_BRIDGE_ANTIGRAVITY_MODELS": "gemini-3.8-flash-high",
            },
        )

        self.assertEqual(config.antigravity_path, "/opt/bin/agy")
        self.assertEqual(config.antigravity_models, ("gemini-3.8-flash-high",))

    def test_empty_models_disable_antigravity(self):
        config = build_config(["--antigravity-models", ""], env={})

        self.assertEqual(config.antigravity_models, ())
        self.assertEqual(config.backend(HIGH), CLAUDE_BACKEND)

    def test_overlapping_model_names_are_rejected(self):
        with self.assertRaises(ValueError):
            build_config(
                [],
                env={
                    "CLAUDE_BRIDGE_CLAUDE_MODELS": "shared-name",
                    "CLAUDE_BRIDGE_ANTIGRAVITY_MODELS": "shared-name",
                },
            )

    def test_models_without_an_executable_path_are_rejected(self):
        with self.assertRaises(ValueError):
            build_config(["--antigravity-path", ""], env={})


class ClaudeRegressionTest(BridgeHTTPTestCase):
    """Antigravity を ACP 常駐に変えても Claude 側の経路が変わらないこと。"""

    def test_claude_still_uses_session_flags(self):
        runner = FakeRunner(cli_lines(claude_result("こんにちは")))
        base_url = self.start_server(make_config(), runner)

        status, payload = self.post(
            base_url, {"model": "claude-opus-5-5", "input": "やあ", "prompt_cache_key": "t1"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")
        self.assertIn("--session-id", runner.calls[0]["command"])
        self.assertNotIn("acp", runner.calls[0]["command"])

    def test_claude_stream_sends_the_whole_text_as_one_delta(self):
        runner = FakeRunner(cli_lines(claude_result("こんにちは")))
        base_url = self.start_server(make_config(), runner)

        _, events = self.post_stream(base_url, {"model": "claude-opus-5-5", "input": "やあ"})

        self.assertEqual(
            [data["delta"] for name, data in events if name == "response.output_text.delta"],
            ["こんにちは"],
        )


if __name__ == "__main__":
    unittest.main()
