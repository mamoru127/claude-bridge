"""Codex が提示した collaboration ツールを agy の ACP セッションに登録する。"""

import hashlib
import json
import os
import sys

from .config import PASSTHROUGH_DEFS_ENV
from .responses import Message


class AcpCollaboration:
    def __init__(self, tools: tuple[dict, ...]) -> None:
        # agy は同名の MCP を再開時にも再利用する。異なる定義には別名を割り当てる。
        definitions = json.dumps(sorted(tools, key=lambda tool: tool["name"]), sort_keys=True)
        digest = hashlib.sha256(definitions.encode()).hexdigest()[:12]
        self.server_name = f"codex_collab_{digest}"
        self.servers = [
            {
                "name": self.server_name,
                "command": sys.executable,
                "args": ["-m", "claude_bridge.passthrough_adapter", "--acp"],
                "env": [
                    {"name": "PYTHONPATH", "value": os.path.dirname(os.path.dirname(__file__))},
                    {"name": PASSTHROUGH_DEFS_ENV, "value": definitions},
                ],
            }
        ]
        self._names = {
            f"mcp_{self.server_name}_{tool['name']}": tool["name"] for tool in tools
        }
        self._started: dict[str, tuple[str, dict]] = {}
        self.calls: dict[str, tuple[str, dict]] = {}

    def observe(self, update: dict) -> None:
        """このターンの ACP 通知で完了した呼び出しだけを、ID ごとに一度採る。"""
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id or call_id in self.calls:
            return
        name = self._names.get(update.get("name"))
        args = update.get("rawInput", update.get("args"))
        if name and isinstance(args, dict):
            # toolSummary は agy が表示用に追加した引数。
            self._started[call_id] = name, {k: v for k, v in args.items() if k != "toolSummary"}
        if update.get("status") in ("completed", "failed", "cancelled"):
            call = self._started.pop(call_id, None)
            if call and update["status"] == "completed":
                self.calls[call_id] = call


_HISTORY_HEADER = "Parent conversation (JSON records, chronological order):\n"
_TASK_HEADER = "\nChild assignment:\n"


def _history_record(item: Message | dict) -> dict:
    if isinstance(item, Message):
        # Gemini の入力はここに来る前にテキストのみであることを検証済み。
        return {"role": item.role, "text": "\n".join(item.parts)}
    keys = ("type", "namespace", "name", "call_id", "arguments", "input", "output")
    record = {key: item[key] for key in keys if key in item}
    # 先行する別モデル起動の依頼文には、その時点の履歴が既に添付されている。
    # 次の子には元の依頼だけを記録し、同じ履歴を入れ子にして増幅させない。
    if item.get("namespace") == "collaboration" and item.get("name") == "spawn_agent":
        try:
            args = json.loads(item["arguments"])
        except (KeyError, TypeError, ValueError):
            return record
        message = args.get("message") if isinstance(args, dict) else None
        if isinstance(message, str) and message.startswith(_HISTORY_HEADER):
            _, separator, task = message.partition(_TASK_HEADER)
            if separator:
                record["arguments"] = {**args, "message": task, "fork_turns": "all"}
    return record


def app_spawn_arguments(name: str, arguments: dict, history: tuple[Message | dict, ...]) -> dict:
    """Gemini の MCP で明示されたモデル指定と all を、アプリの起動形式へ変換する。"""
    if name != "spawn_agent" or arguments.get("fork_turns") != "all" or not arguments.get("model"):
        return arguments
    if not history or not isinstance(arguments.get("message"), str):
        return arguments

    records = [_history_record(item) for item in history]
    transcript = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    return {
        **arguments,
        "fork_turns": "none",
        "message": _HISTORY_HEADER + transcript + _TASK_HEADER + arguments["message"],
    }
