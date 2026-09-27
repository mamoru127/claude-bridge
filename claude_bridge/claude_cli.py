"""Claude CLI を非対話で実行し、途中経過と最終テキストを逐次取り出す。"""

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator

from .cli_process import CliKind, launch, read_lines, snippet, text_snippet, write_stdin
from .config import (
    MCP_SERVER_NAME,
    PASSTHROUGH_DEFS_ENV,
    PASSTHROUGH_SERVER_NAME,
    PASSTHROUGH_TOKEN_ENV,
    BridgeConfig,
)

CLAUDE = CliKind("Claude CLI", "claude_cli")

# ツール使用の表示に載せる主要な引数。最初に見つかったものを 1 つだけ使う。
TOOL_ARGUMENT_KEYS = ("command", "file_path", "pattern", "path", "url", "description")
TOOL_DETAIL_CHARS = 200


def transcript_id(session) -> str:
    """今回の実行で使う転記 ID。続きなら確定済みの ID、新規なら発行し直した ID。"""

    return session.handle if session.resume else str(uuid.uuid4())


def build_command(
    config: BridgeConfig,
    system_prompt: str | None,
    session,
    session_id: str,
    tools: tuple[dict, ...] = (),
    token: str = "",
) -> list[str]:
    """実行するコマンド配列を組み立てる。シェルは介さない。"""

    # --strict-mcp-config: --mcp-config で明示した分だけを読む。
    # 既定では --mcp-config を渡さないので、ユーザー設定の MCP サーバーは一切起動しない。
    # stream-json は途中経過を JSONL で出す形式で、--verbose の併用が必須。
    # --input-format: 入力側も JSONL にする。テキストだけの文字列入力では画像を渡せない。
    # --thinking-display: --print では thinking 本文が省略されるため、明示して中身を出させる。
    # --permission-mode: 非対話実行中に承認待ちで止まらないよう、全ツールを自動承認する。
    # --session-id / --resume: 会話の続きは同じ転記へ書き足す。新規は ID を固定して次回に備える。
    command = [
        config.claude_path,
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
        config.model,
        "--resume" if session.resume else "--session-id",
        session_id,
    ]
    # --add-dir は可変長引数。後続は必ず `-` 始まりか終端になるため、ここで切れる。
    if config.add_dirs:
        command += ["--add-dir", *config.add_dirs]
    servers = build_mcp_servers(config, tools, token)
    if servers:
        command += [
            "--mcp-config",
            json.dumps({"mcpServers": servers}, ensure_ascii=False),
            # サーバー名を丸ごと許可する記法。--print では権限確認に答えられないため事前に許可する。
            "--allowedTools",
            ",".join(f"mcp__{name}" for name in servers),
        ]
    if system_prompt:
        command += ["--append-system-prompt", system_prompt]
    return command


def build_mcp_servers(
    config: BridgeConfig, tools: tuple[dict, ...] = (), token: str = ""
) -> dict:
    """--mcp-config へ渡すサーバー定義。

    同梱アダプタ、Codex から届いたツールの受け渡し、起動時に読んだ追加設定を合わせる。
    受け渡しはリクエストごとにツールが変わるため、定義を環境変数で渡して都度起動する。
    """

    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base_env = {
        "PYTHONPATH": package_parent,
        "CLAUDE_BRIDGE_URL": config.bridge_url,
    }
    # 追加定義を先に置き、ブリッジが用意するサーバーを後から重ねる。逆にすると
    # 同名の追加定義が token 付きのアダプタを差し替え、受け渡しが動かなくなる。
    servers: dict = dict(config.extra_mcp_servers)
    if config.codex_mcp:
        servers[MCP_SERVER_NAME] = {
            "command": sys.executable,
            "args": ["-m", "claude_bridge.mcp_adapter"],
            # Claude CLI の cwd は --working-dir なので、import 先を明示する。
            # app-server はブリッジ本体が持つため、アダプタへは接続先だけを渡す。
            "env": base_env,
        }
    if tools and token:
        servers[PASSTHROUGH_SERVER_NAME] = {
            "command": sys.executable,
            "args": ["-m", "claude_bridge.passthrough_adapter"],
            "env": {
                **base_env,
                PASSTHROUGH_DEFS_ENV: json.dumps(list(tools), ensure_ascii=False),
                PASSTHROUGH_TOKEN_ENV: token,
            },
        }
    return servers


def stream_claude(
    config: BridgeConfig,
    content: tuple[dict, ...],
    system_prompt: str | None,
    session,
    runner=subprocess.Popen,
    tools: tuple[dict, ...] = (),
    token: str = "",
) -> Iterator[tuple[str, str]]:
    """Claude CLI を起動し、途中経過と最終テキストを発生順に返す。

    種別は表示だけの `reasoning`、記録に残す作業コメントの `message`、最終テキストの `text`。
    最終テキストは result でしか分からないため、増分（`delta`）は出さない。
    """

    session_id = transcript_id(session)
    command = build_command(config, system_prompt, session, session_id, tools, token)
    process, stderr = launch(CLAUDE, command, config.working_dir, runner)
    # 起動できた時点で、渡した範囲を会話へ確定させる。ここまで来れば CLI 側に転記が作られる。
    session.start(process)
    session.confirm(session_id)
    write_stdin(process, input_line(content))

    seen_result = False
    # 最後の作業コメントは result と同じ本文になる。1 件だけ保留し、重複する場合は捨てる。
    pending = None
    for line in read_lines(CLAUDE, process, stderr, config.timeout_seconds):
        for kind, value in _line_events(line):
            if kind == "message":
                if pending is not None:
                    yield "message", pending
                pending = value
                continue
            if pending is not None and not (
                kind == "text" and pending.strip() == value.strip()
            ):
                yield "message", pending
            pending = None
            seen_result = seen_result or kind == "text"
            yield kind, value

    if not seen_result:
        raise CLAUDE.error("の出力に result が含まれていません", "invalid_output")


def input_line(content: tuple[dict, ...]) -> bytes:
    """stream-json 形式の入力 1 行。会話全体を 1 個の user メッセージとして渡す。"""

    message = {"type": "user", "message": {"role": "user", "content": list(content)}}
    return json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"


def _line_events(line: bytes) -> Iterator[tuple[str, str]]:
    """stream-json の 1 行から、途中経過と最終テキストを取り出す。"""

    if not line.strip():
        return
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CLAUDE.error(
            f"の出力が JSON ではありません: {snippet(line)}", "invalid_output"
        ) from error

    # 思考とツール使用は表示だけの reasoning、作業の区切りで書く本文は記録に残る message。
    if payload.get("type") == "assistant":
        for block in payload["message"]["content"]:
            if block.get("type") == "tool_use":
                yield "reasoning", _tool_text(block)
            # 思考が省略された行は本文が空になる。見せる内容がないので飛ばす。
            elif block.get("type") == "thinking" and block["thinking"]:
                yield "reasoning", block["thinking"]
            elif block.get("type") == "text" and block["text"].strip():
                yield "message", block["text"]
    elif payload.get("type") == "result":
        yield "text", _result_text(payload)


def _tool_text(block: dict) -> str:
    """ツール使用の 1 行説明。ツール名と主要な引数だけを載せる。"""

    arguments = block.get("input") or {}
    detail = next(
        (str(arguments[key]) for key in TOOL_ARGUMENT_KEYS if key in arguments), ""
    )
    return f"**{block['name']} を実行中**\n{detail[:TOOL_DETAIL_CHARS]}".rstrip()


def _result_text(payload: dict) -> str:
    """result イベントから最終テキストを取り出す。"""

    if payload.get("is_error"):
        raise CLAUDE.error(
            f"がエラーを返しました: {text_snippet(payload.get('result'))}", "error_result"
        )
    result = payload.get("result")
    if not isinstance(result, str):
        raise CLAUDE.error("の出力に文字列の result がありません", "invalid_output")
    return result
