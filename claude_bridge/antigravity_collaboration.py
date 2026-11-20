"""Codex が提示した collaboration ツールを agy の ACP セッションに登録する。"""

import hashlib
import json
import os
import sys

from .config import PASSTHROUGH_DEFS_ENV


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
