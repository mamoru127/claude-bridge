"""Antigravity CLI（agy）を受け持つモデルの実行・会話継続・モデル一覧の検証。

擬似出力は agy 1.1.13 の実出力を写したもの。各行は `event` で種別を示し、本体を同じ名前の
キーへ入れ子で持つ。`agent_response` の `text_delta` は本物の増分で、`ACTIVE` と最後の
`DONE` を連結すると `result` の `response` と一致する。
"""

import json
import subprocess
import time
import unittest

from claude_bridge.antigravity_cli import (
    BILLING_ENV_KEYS,
    MAX_PROMPT_ARGUMENT_BYTES,
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
from claude_bridge.sessions import Session

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


def agy_lines(*events: dict) -> bytes:
    """headless の NDJSON 出力を組み立てる。"""

    return b"".join(json.dumps(event).encode("utf-8") + b"\n" for event in events)


def init_event(conversation_id: str = "conv-1") -> dict:
    return {
        "event": "init",
        "conversation_id": conversation_id,
        "init": {
            "model": HIGH,
            "cwd": "/tmp",
            "tools": ["run_command", "view_file"],
            "permission_mode": "request-review",
        },
    }


def step(step_type: str, index: int = 2, state: str = "DONE", **fields) -> dict:
    return {
        "event": "step_update",
        "step_update": {
            "conversation_id": "conv-1",
            "step_index": index,
            "state": state,
            "step_type": step_type,
            **fields,
        },
    }


def delta_event(text: str, state: str = "DONE") -> dict:
    return step("agent_response", index=2, state=state, text_delta=text)


def tool_event(
    name: str,
    parameters: dict | None = None,
    index: int = 3,
    state: str = "ACTIVE",
    error: str | None = None,
) -> dict:
    info = {"name": name, "parameters": parameters or {}}
    if error is not None:
        info["error"] = {"type": "TOOL_ERROR", "message": error}
    return step("tool", index=index, state=state, tool_name=name, tool_info=info)


def result_event(
    response: str = "こんにちは",
    conversation_id: str = "conv-1",
    status: str = "SUCCESS",
    error: object = None,
) -> dict:
    return {
        "event": "result",
        "result": {
            "conversation_id": conversation_id,
            "status": status,
            "response": response,
            "duration_seconds": 3.2,
            "num_turns": 1,
            "error": error,
            "usage": {"input_tokens": 10, "output_tokens": 3},
        },
    }


def agy_output(*events: dict) -> bytes:
    """init と result を含む、いちばん普通の 1 ターン分の出力。"""

    return agy_lines(init_event(), *events, result_event())


class SequenceRunner(FakeRunner):
    """呼び出しごとに別の標準出力を返すランナー。複数ターンの検証に使う。"""

    def __init__(self, outputs: list[bytes]):
        super().__init__(stdout=outputs[0])
        self._outputs = outputs

    def make_process(self) -> FakeProcess:
        index = min(len(self.processes), len(self._outputs) - 1)
        return FakeProcess(self._outputs[index])

    def command(self, index: int = 0) -> list[str]:
        return self.calls[index]["command"]

    def prompt(self, index: int = 0) -> str:
        command = self.command(index)
        return command[command.index("-p") + 1]


def agy_config(**overrides):
    """agy を実行する設定。config.model には振り分け後の実行モデル名が入る。"""

    return make_config(**{"model": HIGH, **overrides})


class AntigravityCommandTest(unittest.TestCase):
    def test_first_turn_has_no_conversation_flag(self):
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)

        command = build_command(agy_config(antigravity_path="/opt/bin/agy"), "やあ", session)

        self.assertEqual(
            command,
            [
                "/opt/bin/agy",
                "-p",
                "やあ",
                "--model",
                HIGH,
                "--output-format",
                "stream-json",
                "--dangerously-skip-permissions",
                # 既定の 5 分ではブリッジのタイムアウトより先に打ち切られる。
                "--print-timeout",
                "3600000ms",
            ],
        )

    def test_resumed_turn_passes_the_conversation_id(self):
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        session.confirm("conv-9")
        session.prepare(
            conversation(("user", "1問目"), ("assistant", "回答"), ("user", "2問目")),
            ANTIGRAVITY_BACKEND,
        )

        command = build_command(agy_config(), "2問目", session)

        self.assertEqual(command[-2:], ["--conversation", "conv-9"])

    def test_add_dirs_are_passed_to_agy(self):
        command = build_command(
            agy_config(add_dirs=("/workspace", "/Users/example/.gemini")),
            "やあ",
            Session(),
        )

        self.assertEqual(
            command[-4:],
            ["--add-dir", "/workspace", "--add-dir", "/Users/example/.gemini"],
        )

    def test_fractional_timeout_is_not_truncated_to_zero(self):
        # 秒は小数で指定できる。切り捨てると agy 側が即時打ち切りになる。
        command = build_command(agy_config(timeout_seconds=0.5), "やあ", Session())

        self.assertEqual(command[command.index("--print-timeout") + 1], "500ms")

    def test_executable_path_is_not_shell_interpreted(self):
        config = agy_config(antigravity_path="/opt/my tools/agy; rm -rf /")
        self.assertEqual(build_command(config, "やあ", Session())[0], "/opt/my tools/agy; rm -rf /")

    def test_prompt_argument_limit_is_checked_before_launch(self):
        allowed = "a" * MAX_PROMPT_ARGUMENT_BYTES
        self.assertEqual(build_command(agy_config(), allowed, Session())[2], allowed)
        with self.assertRaises(BridgeError) as raised:
            build_command(agy_config(), allowed + "a", Session())
        self.assertEqual(raised.exception.status, 413)
        self.assertEqual(raised.exception.code, "antigravity_cli_prompt_too_large")
        with self.assertRaises(BridgeError):
            build_command(agy_config(), "あ" * (MAX_PROMPT_ARGUMENT_BYTES // 3 + 1), Session())


class AntigravityStreamTest(unittest.TestCase):
    def run_stream(self, runner, config=None, session=None) -> list[tuple[str, str]]:
        return list(
            stream_antigravity(config or agy_config(), "やあ", session or Session(), runner)
        )

    def test_deltas_and_tools_are_reported_in_order(self):
        runner = FakeRunner(
            agy_lines(
                init_event(),
                step("user_input", index=0),
                delta_event("こん", state="ACTIVE"),
                tool_event("view_file", {"AbsolutePath": "/tmp/app.py"}),
                tool_event("view_file", {"AbsolutePath": "/tmp/app.py"}, state="DONE"),
                delta_event("にちは"),
                step("checkpoint", index=4),
                result_event("こんにちは"),
            )
        )

        self.assertEqual(
            self.run_stream(runner),
            [
                ("delta", "こん"),
                # ツールの step は開始と終了で 2 回届くが、見せるのは開始の 1 回だけ。
                ("reasoning", "**view_file を実行中**\n/tmp/app.py"),
                ("delta", "にちは"),
                ("text", "こんにちは"),
            ],
        )

    def test_stdin_is_closed_because_the_prompt_goes_through_argv(self):
        runner = FakeRunner(agy_output())

        self.run_stream(runner)

        self.assertEqual(runner.calls[0]["stdin"], subprocess.DEVNULL)
        self.assertEqual(runner.calls[0]["stdout"], subprocess.PIPE)
        self.assertEqual(runner.calls[0]["stderr"], subprocess.PIPE)

    def test_api_key_env_is_not_passed_to_the_cli(self):
        # 月額枠だけを使う。API キー課金の経路になる変数は子へ渡さない。
        runner = FakeRunner(agy_output())

        self.run_stream(runner)

        passed = runner.calls[0]["env"]
        for name in BILLING_ENV_KEYS:
            self.assertNotIn(name, passed)
        # 認証のキャッシュ探索に要る HOME などは残す。
        self.assertIn("PATH", passed)

    def test_billing_env_keys_are_removed_from_a_polluted_environment(self):
        cleaned = agy_env(
            {"PATH": "/usr/bin", "HOME": "/Users/x", "GEMINI_API_KEY": "sk-test",
             "GOOGLE_API_KEY": "sk-test", "GOOGLE_GENAI_API_KEY": "sk-test"}
        )

        self.assertEqual(cleaned, {"PATH": "/usr/bin", "HOME": "/Users/x"})

    def test_tool_without_arguments_shows_only_the_name(self):
        runner = FakeRunner(agy_lines(tool_event("list_dir"), result_event()))

        self.assertEqual(self.run_stream(runner)[0], ("reasoning", "**list_dir を実行中**"))

    def test_denied_tool_is_shown_as_a_failure(self):
        # headless では権限を対話で確認できず自動拒否される。原因が見えないと本文が
        # 空になった理由が分からない。
        runner = FakeRunner(
            agy_lines(
                tool_event("run_command", {"CommandLine": "ls -la"}),
                tool_event(
                    "run_command",
                    {"CommandLine": "ls -la"},
                    state="ERROR",
                    error="User denied permission to run command:\nls -la",
                ),
                result_event("できませんでした"),
            )
        )

        self.assertEqual(
            self.run_stream(runner),
            [
                ("reasoning", "**run_command を実行中**\nls -la"),
                ("reasoning", "**run_command が失敗**\nUser denied permission to run command:\nls -la"),
                ("text", "できませんでした"),
            ],
        )

    def test_tool_detail_is_truncated(self):
        runner = FakeRunner(
            agy_lines(tool_event("run_command", {"CommandLine": "x" * 5000}), result_event())
        )

        self.assertLess(len(self.run_stream(runner)[0][1]), 300)

    def test_empty_delta_is_skipped(self):
        runner = FakeRunner(agy_lines(delta_event(""), result_event("done")))

        self.assertEqual(self.run_stream(runner), [("text", "done")])

    def test_unknown_step_kinds_are_skipped(self):
        runner = FakeRunner(
            agy_lines(step("unknown", index=1), step("user_input", index=0), result_event("done"))
        )

        self.assertEqual(self.run_stream(runner), [("text", "done")])

    def test_unknown_event_kinds_are_skipped(self):
        runner = FakeRunner(agy_lines({"event": "telemetry", "telemetry": {}}, result_event("done")))

        self.assertEqual(self.run_stream(runner), [("text", "done")])

    def test_line_without_event_is_rejected(self):
        runner = FakeRunner(b'{"step_update": {}}\n')

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.code, "antigravity_cli_invalid_output")

    def test_denied_tool_with_no_answer_is_reported_as_a_failure(self):
        # ツール権限の自動拒否では status が SUCCESS のまま本文だけが空になる。
        # 空の回答をそのまま Codex へ渡すと、実行できなかった理由が分からない。
        runner = FakeRunner(
            agy_lines(
                tool_event("run_command", {"CommandLine": "ls -la"}),
                tool_event(
                    "run_command",
                    {"CommandLine": "ls -la"},
                    state="ERROR",
                    error="User denied permission to run command",
                ),
                result_event(""),
            )
        )

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(raised.exception.code, "antigravity_cli_tool_failed")
        self.assertIn("User denied permission", raised.exception.message)

    def test_empty_answer_without_a_tool_failure_is_returned_as_is(self):
        # 失敗していないのに本文が空なら、それが CLI の答え。エラーへ作り替えない。
        runner = FakeRunner(agy_lines(result_event("")))

        self.assertEqual(self.run_stream(runner), [("text", "")])

    def test_failed_tool_does_not_hide_a_normal_answer(self):
        # ツールが拒否されても本文が返っていれば、そのまま最終回答として扱う。
        runner = FakeRunner(
            agy_lines(
                tool_event("run_command", {"CommandLine": "ls"}, state="ERROR", error="denied"),
                result_event("できませんでした"),
            )
        )

        self.assertEqual(self.run_stream(runner)[-1], ("text", "できませんでした"))

    def test_result_confirms_the_conversation_id(self):
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        runner = FakeRunner(agy_lines(result_event("done", conversation_id="conv-42")))

        self.run_stream(runner, session=session)

        self.assertEqual(session.handle, "conv-42")

    def test_error_result_becomes_a_bridge_error(self):
        runner = FakeRunner(
            agy_lines(result_event("", status="ERROR", error={"message": "quota exhausted"}))
        )

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(raised.exception.code, "antigravity_cli_error_result")
        self.assertIn("quota exhausted", raised.exception.message)

    def test_non_zero_exit_after_result_does_not_confirm_the_conversation(self):
        # result まで届いても終了コードが異常なら、そのターンは失敗として返す。
        # 続き扱いにすると、次の再送で直近の発言だけを送り直す状態になる。
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        runner = FakeRunner(
            agy_lines(result_event("done", conversation_id="conv-3")), returncode=1
        )

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner, session=session)

        self.assertEqual(raised.exception.code, "antigravity_cli_failed")
        self.assertEqual(session.handle, "")

    def test_error_result_does_not_confirm_the_conversation(self):
        # 失敗したターンを続きにすると、届かなかった発言が会話から抜け落ちる。
        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        runner = FakeRunner(agy_lines(result_event("", status="ERROR", error="boom")))

        with self.assertRaises(BridgeError):
            self.run_stream(runner, session=session)

        self.assertEqual(session.handle, "")

    def test_missing_conversation_id_is_rejected(self):
        runner = FakeRunner(agy_lines(result_event("done", conversation_id="")))

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.code, "antigravity_cli_invalid_output")

    def test_non_string_response_is_rejected(self):
        runner = FakeRunner(
            agy_lines({"event": "result", "result": {"conversation_id": "c", "status": "SUCCESS"}})
        )

        with self.assertRaises(BridgeError) as raised:
            self.run_stream(runner)

        self.assertEqual(raised.exception.code, "antigravity_cli_invalid_output")

    def test_missing_result_is_rejected(self):
        runner = FakeRunner(agy_lines(init_event(), delta_event("途中")))

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

    def test_closing_the_stream_terminates_the_cli_process(self):
        # クライアント切断でジェネレータが破棄されても、CLI を裏で走らせ続けない。
        runner = FakeRunner(agy_lines(delta_event("途中"), result_event("done")))
        events = stream_antigravity(agy_config(), "やあ", Session(), runner)
        next(events)
        events.close()

        self.assertEqual(runner.processes[0].killed, 1)

    def test_normal_completion_does_not_kill_the_cli_process(self):
        runner = FakeRunner(agy_output())

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
    def test_oversized_prompt_does_not_cancel_a_running_turn(self):
        runner = HangingRunner()
        base_url = self.start_server(agy_config(), runner)
        self.post_and_disconnect(
            base_url, {"model": HIGH, "input": "1問目", "prompt_cache_key": "thread-1"}
        )
        for _ in range(500):
            if runner.processes:
                break
            time.sleep(0.01)
        process = runner.processes[0]

        status, payload = self.post(
            base_url,
            {"model": HIGH, "input": "a" * (MAX_PROMPT_ARGUMENT_BYTES + 1),
             "prompt_cache_key": "thread-1"},
        )

        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "antigravity_cli_prompt_too_large")
        self.assertEqual(process.killed, 0)
        self.assertEqual(len(runner.processes), 1)
        process.kill()

    def test_non_stream_returns_the_result_response(self):
        runner = SequenceRunner([agy_output(delta_event("こんにちは"))])
        base_url = self.start_server(agy_config(), runner)

        status, payload = self.post(base_url, {"model": HIGH, "input": "やあ"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")
        self.assertEqual(payload["model"], HIGH)
        self.assertEqual(runner.prompt(), "やあ")
        # アプリが選んだモデルがそのまま agy へ届く。
        self.assertEqual(runner.command()[runner.command().index("--model") + 1], HIGH)

    def test_medium_model_reaches_the_cli(self):
        runner = SequenceRunner([agy_output()])
        base_url = self.start_server(make_config(model=MEDIUM), runner)

        self.post(base_url, {"model": MEDIUM, "input": "やあ"})

        self.assertEqual(runner.command()[runner.command().index("--model") + 1], MEDIUM)

    def test_stream_sends_increments_then_the_final_text(self):
        runner = SequenceRunner(
            [
                agy_lines(
                    init_event(),
                    delta_event("こん"),
                    tool_event("read_file", {"file_path": "app.py"}),
                    delta_event("にちは"),
                    result_event("こんにちは"),
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
        # 増分をいくら送っても本文アイテムは 1 個だけ開く。
        self.assertEqual(types.count("response.output_item.added"), 1)
        done = next(data for name, data in events if name == "response.output_text.done")
        self.assertEqual(done["text"], "こんにちは")
        completed = next(data for name, data in events if name == "response.completed")
        self.assertEqual(completed["response"]["output_text"], "こんにちは")

    def test_stream_shows_tool_progress_as_reasoning(self):
        runner = SequenceRunner([agy_output(tool_event("run_command", {"command": "ls -la"}))])
        base_url = self.start_server(agy_config(), runner)

        _, events = self.post_stream(base_url, {"model": HIGH, "input": "やあ"})

        summaries = [
            data["item"]["summary"][0]["text"]
            for name, data in events
            if name == "response.output_item.done" and data["item"]["type"] == "reasoning"
        ]
        self.assertEqual(summaries, ["**run_command を実行中**\nls -la"])

    def test_stream_failure_is_reported_as_response_failed(self):
        runner = SequenceRunner([agy_lines(result_event("", status="error", error="quota"))])
        base_url = self.start_server(agy_config(), runner)

        _, events = self.post_stream(base_url, {"model": HIGH, "input": "やあ"})

        failed = next(data for name, data in events if name == "response.failed")
        self.assertEqual(failed["response"]["error"]["code"], "antigravity_cli_error_result")

    def test_system_instructions_reach_the_prompt(self):
        runner = SequenceRunner([agy_output()])
        base_url = self.start_server(agy_config(), runner)

        self.post(
            base_url,
            {"model": HIGH, "input": "やあ", "instructions": "あなたは助手です"},
        )

        self.assertEqual(runner.prompt(), "[system]\nあなたは助手です\n\nやあ")

    def test_image_input_is_rejected_with_400(self):
        runner = SequenceRunner([agy_output()])
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
        # 実行できないと分かった時点で止め、CLI は起動しない。
        self.assertEqual(runner.calls, [])

    def test_rejected_image_keeps_the_claude_conversation(self):
        # 拒否されるリクエストで会話の続き具合を壊すと、同じスレッドの Claude が
        # 転記を引き継げなくなる。
        runner = SequenceRunner([cli_lines(claude_result("回答")), agy_output()])
        base_url = self.start_server(make_config(), runner)

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
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("--resume", runner.command(1))

    def test_codex_tools_are_not_offered(self):
        # agy の headless 契約にはリクエスト単位のツール登録の口がないため、
        # 受け渡しも tool_search も行わずそのまま実行する。
        runner = SequenceRunner([agy_output()])
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
    """会話キーごとの継続。Claude の --session-id / --resume と混ざらないこと。"""

    def post_turn(self, base_url: str, payload: dict, key: str = "thread-1"):
        return self.post(base_url, {**payload, "prompt_cache_key": key})

    def second_turn_input(self) -> list:
        return [
            {"role": "user", "content": "1問目"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "2問目"},
        ]

    def test_second_turn_continues_with_the_conversation_id(self):
        runner = SequenceRunner(
            [
                agy_lines(result_event("回答", conversation_id="conv-7")),
                agy_lines(result_event("回答2", conversation_id="conv-7")),
            ]
        )
        base_url = self.start_server(agy_config(), runner)

        self.post_turn(base_url, {"model": HIGH, "input": "1問目"})
        self.post_turn(base_url, {"model": HIGH, "input": self.second_turn_input()})

        self.assertNotIn("--conversation", runner.command(0))
        self.assertEqual(runner.command(1)[-2:], ["--conversation", "conv-7"])
        # 続きでは転記済みの発言を送り返さない。
        self.assertEqual(runner.prompt(1), "2問目")

    def test_failed_first_turn_resends_the_whole_history(self):
        runner = SequenceRunner(
            [
                agy_lines(result_event("", status="error", error="boom")),
                agy_lines(result_event("回答", conversation_id="conv-8")),
            ]
        )
        base_url = self.start_server(agy_config(), runner)

        self.post_turn(base_url, {"model": HIGH, "input": "1問目"})
        self.post_turn(base_url, {"model": HIGH, "input": self.second_turn_input()})

        self.assertNotIn("--conversation", runner.command(1))
        self.assertEqual(
            runner.prompt(1), "[user]\n1問目\n\n[assistant]\n回答\n\n[user]\n2問目"
        )

    def test_rewritten_history_starts_a_new_conversation(self):
        runner = SequenceRunner(
            [
                agy_lines(result_event("回答", conversation_id="conv-7")),
                agy_lines(result_event("回答2", conversation_id="conv-9")),
            ]
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

        self.assertNotIn("--conversation", runner.command(1))

    def test_resent_request_repeats_the_latest_message(self):
        runner = SequenceRunner([agy_lines(result_event("回答", conversation_id="conv-7"))] * 2)
        base_url = self.start_server(agy_config(), runner)

        self.post_turn(base_url, {"model": HIGH, "input": "1問目"})
        self.post_turn(base_url, {"model": HIGH, "input": "1問目"})

        self.assertEqual(runner.command(1)[-2:], ["--conversation", "conv-7"])
        self.assertEqual(runner.prompt(1), "1問目")

    def test_switching_backend_restarts_the_conversation(self):
        """同じスレッドで Claude と Antigravity を切り替えても、相手の会話 ID を持ち込まない。"""

        session = Session()
        session.prepare(conversation(("user", "1問目")), ANTIGRAVITY_BACKEND)
        session.confirm("conv-7")

        messages = conversation(("user", "1問目"), ("assistant", "回答"), ("user", "2問目"))
        unseen = session.prepare(messages, CLAUDE_BACKEND)

        self.assertFalse(session.resume)
        # 続きにできないので全履歴を渡し、転記 ID も新しく発行する。
        self.assertEqual(unseen, messages)
        self.assertNotEqual(transcript_id(session), "conv-7")

    def test_failed_switch_keeps_the_previous_conversation(self):
        """切り替えた先で実行が失敗しても、直前に確定していた会話は残す。"""

        session = Session()
        session.prepare(conversation(("user", "1問目")), CLAUDE_BACKEND)
        session.confirm("claude-transcript")

        # Antigravity を選んだが agy が入っておらず起動に失敗した。
        runner = FakeRunner(raises=FileNotFoundError("agy"))
        messages = conversation(("user", "1問目"), ("assistant", "回答"), ("user", "2問目"))
        session.prepare(messages, ANTIGRAVITY_BACKEND)
        with self.assertRaises(BridgeError):
            list(stream_antigravity(agy_config(), "2問目", session, runner))

        # 同じスレッドで Claude へ戻せば、元の転記の続きから実行できる。
        unseen = session.prepare(messages, CLAUDE_BACKEND)

        self.assertTrue(session.resume)
        self.assertEqual(transcript_id(session), "claude-transcript")
        self.assertEqual(unseen, messages[-1:])


class AntigravityModelsTest(BridgeHTTPTestCase):
    def test_models_include_both_clis(self):
        base_url = self.start_server(make_config(claude_models=("claude-opus-5-5",)), FakeRunner())

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
        # Claude 側の表示は変えない。
        self.assertEqual(entries["claude-opus-5-5"]["display_name"], "Claude CLI (claude-opus-5-5)")
        self.assertIn("画像", entries["claude-opus-5-5"]["description"])

    def test_antigravity_entries_keep_the_upstream_template_fields(self):
        # Codex は必須項目が欠けたエントリを 1 つ見つけるだけで一覧全体の解釈に失敗する。
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

        # 画像を扱えるのは Claude だけ。雛形の宣言をそのまま受け継がない。
        self.assertFalse(entries[HIGH]["supports_vision"])
        self.assertTrue(entries["claude-opus-5-5"]["supports_vision"])
        self.assertFalse(entries[HIGH]["prefer_websockets"])
        self.assertIsNone(entries[HIGH]["tool_mode"])
        # priority は上流と重複しないよう並べ直す。
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
        # 空の環境変数は既定値として扱われる（--passthrough-tools と同じ扱い）ため、
        # 無効化は引数で明示する。
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
    """Antigravity を足しても Claude 側の経路が変わらないこと。"""

    def test_claude_still_uses_session_flags(self):
        runner = FakeRunner(cli_lines(claude_result("こんにちは")))
        base_url = self.start_server(make_config(), runner)

        status, payload = self.post(
            base_url, {"model": "claude-opus-5-5", "input": "やあ", "prompt_cache_key": "t1"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["output_text"], "こんにちは")
        self.assertIn("--session-id", runner.calls[0]["command"])
        self.assertNotIn("--conversation", runner.calls[0]["command"])

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
