"""Antigravity CLI（`agy`）を headless で実行し、途中経過と最終テキストを逐次取り出す。

headless の契約は次のとおり（agy 1.1.13 の実出力で確認）。

    agy -p <prompt> --model gemini-3.8-flash-high --output-format stream-json

標準出力は NDJSON で、各行は `event` で種別を示し、本体を同じ名前のキーへ入れ子で持つ。

    {"event":"init","conversation_id":"<id>","init":{"model":...,"cwd":...,"tools":[...]}}
    {"event":"step_update","step_update":{"step_index":2,"state":"ACTIVE",
                                          "step_type":"agent_response","text_delta":"こん"}}
    {"event":"result","result":{"conversation_id":"<id>","status":"SUCCESS","response":"..."}}

`agent_response` の `text_delta` は本物の増分で、`ACTIVE` と最後の `DONE` を連結すると
`result` の `response` と一致する。会話の継続は `result` の `conversation_id` を
`--conversation` へ渡す。

認証は Antigravity CLI 側の Google アカウントのキャッシュを使う。ブリッジは認証を代行せず、
API キー課金の経路になる環境変数を agy へ渡さない（BILLING_ENV_KEYS）。
"""

import json
import math
import os
import re
import subprocess
import time
from collections.abc import Iterator, Mapping
from functools import lru_cache

from .cli_process import STDERR_SNIPPET_BYTES, CliKind, launch, read_lines, snippet
from .config import BridgeConfig

ANTIGRAVITY = CliKind("Antigravity CLI", "antigravity_cli")

# result の status がこの値のときだけ、応答が確定したものとして扱う。
# 実際に返る値は SUCCESS / ERROR / CANCELED / INTERRUPTED / INVALID などの大文字。
RESULT_SUCCESS = "SUCCESS"

# agy を API キー経路（従量課金）へ切り替えうる環境変数。Google アカウントの月額枠だけを
# 使うため、ブリッジから起動する agy の環境からは必ず取り除く。agy 側は設定の
# modelProvider を gemini にしない限りこれらを使わないが、課金経路の判断を設定ファイルの
# 状態に依存させない。API キーで使いたい場合はブリッジを経由せず agy を直接実行する。
BILLING_ENV_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY")

# ツール進捗の 1 行説明に載せる引数の長さ。
TOOL_DETAIL_CHARS = 200

# agy の -p は OS のコマンドライン引数になる。Windows の約 32K 文字制限にも
# 実行ファイル名・他の引数の余裕を残す。stdin 入力へ移行したら撤廃できる。
MAX_PROMPT_ARGUMENT_BYTES = 16_000


def agy_env(env: Mapping[str, str] | None = None) -> dict:
    """agy へ渡す環境変数。API キー課金の経路になる変数だけを取り除く。"""

    env = os.environ if env is None else env
    return {name: value for name, value in env.items() if name not in BILLING_ENV_KEYS}


def resolve_model(path: str, model: str) -> str:
    """固定の公開名を、agy が現在提供する最新の Flash モデルへ解決する。"""

    if model not in ("gemini-flash-high", "gemini-flash-medium"):
        return model
    return _latest_flash_model(path, model.rsplit("-", 1)[1], int(time.monotonic() // 600))


@lru_cache(maxsize=16)
def _latest_flash_model(path: str, effort: str, refresh_bucket: int) -> str:
    try:
        result = subprocess.run(
            [path, "models"], capture_output=True, text=True, timeout=20, env=agy_env()
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ANTIGRAVITY.error(f"のモデル一覧を取得できません: {error}", "models_failed") from error
    if result.returncode:
        raise ANTIGRAVITY.error("のモデル一覧を取得できません", "models_failed")
    candidates = []
    for line in result.stdout.splitlines():
        name = line.split("\t", 1)[0]
        match = re.fullmatch(rf"gemini-(\d+(?:\.\d+)*)-flash-{effort}", name)
        if match:
            candidates.append((tuple(int(part) for part in match.group(1).split(".")), name))
    if not candidates:
        raise ANTIGRAVITY.error(f"に Flash ({effort}) のモデルがありません", "model_not_found")
    return max(candidates)[1]


def validate_prompt_argument(prompt: str) -> None:
    if len(prompt.encode("utf-8")) > MAX_PROMPT_ARGUMENT_BYTES:
        raise ANTIGRAVITY.error(
            f"への入力は {MAX_PROMPT_ARGUMENT_BYTES} バイト以内にしてください",
            "prompt_too_large",
            status=413,
            error_type="invalid_request_error",
        )


def build_command(config: BridgeConfig, prompt: str, session) -> list[str]:
    """実行するコマンド配列を組み立てる。シェルは介さない。

    `--conversation` は前回の実行が `conversation_id` を返したときだけ付ける。初回は付けずに
    実行し、`result` で受け取った ID をセッションへ記録して次のターンから続きにする。
    """

    validate_prompt_argument(prompt)

    command = [
        config.antigravity_path,
        "-p",
        prompt,
        "--model",
        resolve_model(config.antigravity_path, config.model),
        "--output-format",
        "stream-json",
        # bridge 経由では対話承認に答えられないため、Claude と同様にツール実行を止めない。
        # 操作範囲は working_dir と --add-dir で明示した信頼済みディレクトリに限定する。
        "--dangerously-skip-permissions",
        # --print-timeout の既定は 5 分で、ブリッジのタイムアウトより先に打ち切られる。
        # 長い作業が途中で切れないよう、歯止めをブリッジ側と同じ値にそろえる。
        # 秒は小数で指定できるため、切り捨てで 0 にならないようミリ秒へ切り上げて渡す。
        "--print-timeout",
        f"{math.ceil(config.timeout_seconds * 1000)}ms",
    ]
    for path in config.add_dirs:
        command += ["--add-dir", path]
    if session.resume:
        command += ["--conversation", session.handle]
    return command


def stream_antigravity(
    config: BridgeConfig,
    prompt: str,
    session,
    runner=subprocess.Popen,
) -> Iterator[tuple[str, str]]:
    """agy を headless で起動し、途中経過と最終テキストを発生順に返す。

    種別は表示だけの `reasoning`（ツール進捗）、最終回答の増分 `delta`、確定した最終テキストの
    `text`。`text` は `result` の `response` で、増分と食い違った場合もこちらが正となる。
    """

    command = build_command(config, prompt, session)
    # `-p` でプロンプトを渡すため標準入力は使わない。開けたままにすると子が待ち続ける。
    process, stderr = launch(
        ANTIGRAVITY,
        command,
        config.working_dir,
        runner,
        stdin=subprocess.DEVNULL,
        env=agy_env(),
    )
    session.start(process)

    result = None
    # ツールの step は開始と終了で 2 回届く。開始の 1 行だけを見せるため step_index を覚える。
    started_tools: set = set()
    # 失敗したツールの最後の理由。本文が空で終わった原因を示せる唯一の手がかりになる。
    tool_error = ""
    for line in read_lines(ANTIGRAVITY, process, stderr, config.timeout_seconds):
        if not line.strip():
            continue
        event, payload = _decode(line)
        if event == "step_update":
            failure = _tool_error(payload)
            tool_error = failure or tool_error
            yield from _step_events(payload, started_tools, failure)
        elif event == "result":
            result = payload

    # ここまで来れば read_lines が終了コードまで検査済み。
    if result is None:
        raise ANTIGRAVITY.error("の出力に result が含まれていません", "invalid_output")
    _check_status(result)
    text = _response_text(result, tool_error)
    # 実行が最後まで正常に終わって初めて、この会話を続きに使えるものとして確定する。
    session.confirm(_conversation_id(result))
    yield "text", text


def _decode(line: bytes) -> tuple[str, dict]:
    """NDJSON の 1 行を (イベント名, 本体) にする。本体はイベント名と同じキーの下にある。"""

    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ANTIGRAVITY.error(
            f"の出力が JSON ではありません: {snippet(line)}", "invalid_output"
        ) from error
    event = payload.get("event") if isinstance(payload, dict) else None
    if not isinstance(event, str) or not event:
        raise ANTIGRAVITY.error(
            f"の出力に event がありません: {snippet(line)}", "invalid_output"
        )
    body = payload.get(event)
    return event, body if isinstance(body, dict) else {}


def _step_events(step: dict, started_tools: set, failure: str) -> Iterator[tuple[str, str]]:
    """step_update から、最終回答の増分とツール進捗を取り出す。

    `step_type` が種別で、`agent_response` は `text_delta`、`tool` は `tool_name` と
    `tool_info` を持つ。`user_input` や `checkpoint` など、Codex へ見せる内容がない step は
    読み飛ばす。
    """

    step_type = step.get("step_type")
    if step_type == "agent_response":
        delta = step.get("text_delta")
        if delta:
            yield "delta", str(delta)
        return
    if step_type != "tool" or not step.get("tool_name"):
        return

    if step.get("step_index") not in started_tools:
        started_tools.add(step.get("step_index"))
        yield "reasoning", f"**{step['tool_name']} を実行中**\n{_tool_detail(step)}".rstrip()
    if failure:
        yield "reasoning", f"**{step['tool_name']} が失敗**\n{failure}"


def _tool_info(step: dict) -> dict:
    parameters = step.get("tool_info")
    return parameters if isinstance(parameters, dict) else {}


def _tool_error(step: dict) -> str:
    """失敗したツールの理由。失敗していなければ空文字。"""

    error = _tool_info(step).get("error")
    message = error.get("message") if isinstance(error, dict) else None
    return str(message)[:TOOL_DETAIL_CHARS] if message else ""


def _tool_detail(step: dict) -> str:
    """ツール進捗に添える引数。引数名はツールごとに違うため、先頭の値だけを見せる。"""

    parameters = _tool_info(step).get("parameters")
    if not isinstance(parameters, dict) or not parameters:
        return ""
    return str(next(iter(parameters.values())))[:TOOL_DETAIL_CHARS]


def _check_status(result: dict) -> None:
    """status が SUCCESS 以外なら実行の失敗として扱う。"""

    if result.get("status") != RESULT_SUCCESS:
        raise ANTIGRAVITY.error(
            f"がエラーを返しました (status={result.get('status')}): {_error_text(result)}",
            "error_result",
        )


def _response_text(result: dict, tool_error: str) -> str:
    """result から最終テキストを取り出す。

    headless ではツール権限を対話で確認できず自動拒否される。そのとき agy は status を
    SUCCESS のままにして本文だけを空で返すため、失敗したツールがあって本文も無い場合だけ、
    そのツールの理由を添えて失敗として返す。空の応答そのものを失敗の根拠にはしない。
    """

    response = result.get("response")
    if not isinstance(response, str):
        raise ANTIGRAVITY.error("の出力に文字列の response がありません", "invalid_output")
    if not response.strip() and tool_error:
        raise ANTIGRAVITY.error(
            f"がツールを実行できず本文を返しませんでした: {tool_error}", "tool_failed"
        )
    return response


def _error_text(result: dict) -> str:
    """result の error。文字列でもオブジェクトでも届くため、どちらも読める形にする。"""

    error = result.get("error")
    if not isinstance(error, str):
        error = json.dumps(error, ensure_ascii=False)
    return error[:STDERR_SNIPPET_BYTES]


def _conversation_id(result: dict) -> str:
    """継続に使う会話 ID。

    無いと次のターンを `--conversation` で続けられず、毎回新しい会話として全履歴を送り直す
    状態に黙って落ちる。契約違反として扱い、ターンを失敗させる。
    """

    conversation_id = result.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ANTIGRAVITY.error(
            "の result に conversation_id がありません", "invalid_output"
        )
    return conversation_id
