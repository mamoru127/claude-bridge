"""Antigravity CLI（`agy`）を常駐 ACP（Agent Client Protocol）サーバーとして動かし、
複数スレッド・複数ターンの実行を 1 本の stdio JSON-RPC 接続で捌く。

ACP の契約は次のとおり（agy 1.5.11 の実機で確認）。

    agy acp --dangerously-skip-permissions

起動直後に 1 回だけ `initialize` を送り、以降は会話ごとに `session/new` または `session/resume`、
モデルの切り替えに `session/set_model`、各ターンに `session/prompt` を送る。

経過は `session/update` 通知で届き、`sessionUpdate` が種別を示す。
- `agent_message_chunk`: 最終回答の増分（`content.text`）
- `agent_thought_chunk`: 思考の増分（`content.text`）
- `tool_call` / `tool_call_update`: ツール実行の開始と成否
ターンの完了は `session/prompt` の応答（`result.stopReason`）で確定する。

会話まわりの挙動（agy 1.5.11 の実機で確認）。
- 初回の `session/new` だけが認証とモデル一覧の取得を伴い、同じ `agy acp` プロセス上の
  2 本目以降の `session/new` や続きの `session/prompt` はコールドスタートなしで即座に始まる。
- `session/prompt` が最初の `session/update` を出した時点で、発言は会話へ記録されている。
  途中で中断するときはプロセスを落とさず `session/cancel` 通知を送ると、その会話のターンだけが
  `stopReason="cancelled"` で止まり、他のスレッドの実行や常駐プロセスはそのまま保たれる。
- `agy acp` が再起動したあとでも、確定済みの会話 ID は `session/resume` で引き継げる。
  存在しない ID を `session/resume` に渡すと JSON-RPC エラーが返る（`session/load` は空で通るため
  使わない）。

認証は Antigravity CLI 側の Google アカウントのキャッシュを使う。ブリッジは認証を代行せず、
API キー課金の経路になる環境変数を agy へ渡さない（BILLING_ENV_KEYS）。
"""

import contextlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping

from .antigravity_collaboration import AcpCollaboration
from .cli_process import STDERR_SNIPPET_BYTES, CliKind, StderrReader, kill_tree, launch, snippet
from .config import BridgeConfig
from .errors import BridgeError

ANTIGRAVITY = CliKind("Antigravity CLI", "antigravity_cli")

# agy を API キー経路（従量課金）へ切り替えうる環境変数。Google アカウントの月額枠だけを
# 使うため、ブリッジから起動する agy の環境からは必ず取り除く。
BILLING_ENV_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY")

CLIENT_INFO = {"name": "claude-bridge", "version": "0.1.0"}
PROTOCOL_VERSION = 1

# `session/cancel` や `stdin` クローズに対して、プロセス強制終了へ切り替えるまでの猶予。
# 正常な `agy acp` は数ミリ秒で応答するため、無応答で固まったときだけここを使い切る。
CANCEL_GRACE_SECONDS = 0.2

# `agy acp` はバックグラウンドタスクやサブエージェントの完了待ちへ入った時点で
# `session/prompt` に `stopReason="end_turn"` を返すが、完了通知による自動再開の経過は
# 同じ接続の `session/update` として後から届く。非同期タスクが残っている間はここで
# 待機し、再開ステップの `usage_update` 後に後続の `tool_call` が来ないことを確かめて終える。
ASYNC_TOOL_NAMES = frozenset({"run_command", "schedule", "run_workflow"})
SUBAGENT_TOOL_NAMES = frozenset({"invoke_subagent", "send_message", "swarm"})
TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed", "cancelled"})
WAKEUP_SETTLE_SECONDS = 0.5
WAKEUP_POLL_SECONDS = 0.5
SUBAGENT_START_GRACE_SECONDS = 5.0
SUBAGENT_IDLE_GRACE_SECONDS = 1.5
DEFAULT_BRAIN_DIR = os.path.join(
    os.path.expanduser("~"), ".gemini", "antigravity-cli", "brain"
)

_SUBAGENT_ID_RE = re.compile(r'"conversationId"\s*:\s*"([^"\s]+)"')
_SENT_TO_RE = re.compile(r'Message sent to "([^"\s]+)"')
_SENDER_RE = re.compile(r"\[Message\]\s+timestamp=\S+\s+sender=([^\s/]+)(?:\s|$)")
_BG_TASK_ID_RE = re.compile(r"Tool is running as a background task with task id:\s*([^\s]+)")
_TASK_DESC_RE = re.compile(r"^Task Description:\s*(.+)$", re.MULTILINE)
_TASK_SENDER_RE = re.compile(r"\[Message\]\s+timestamp=\S+\s+sender=([^\s]+)(?:\s|$)")

# ツール進捗の 1 行説明に載せる引数の長さ。
TOOL_DETAIL_CHARS = 200
TOOL_ARGUMENT_KEYS = (
    "CommandLine",
    "Path",
    "AbsolutePath",
    "command",
    "file_path",
    "pattern",
    "path",
    "url",
    "query",
)

_EOF = object()
_CANCEL = object()


class _TurnCancelled(Exception):
    """クライアント切断や途中の指示差し込みで、このターンが中断された。"""


class _TurnTimeout(Exception):
    """このターンが制限時間内に終わらなかった。"""


def agy_env(env: Mapping[str, str] | None = None) -> dict:
    """agy へ渡す環境変数。API キー課金の経路になる変数だけを取り除く。"""

    env = os.environ if env is None else env
    return {name: value for name, value in env.items() if name not in BILLING_ENV_KEYS}


def build_command(config: BridgeConfig, session=None) -> list[str]:
    """常駐させる `agy acp` のコマンド配列を組み立てる。シェルは介さない。

    モデルと会話 ID は起動引数ではなくセッションごとの RPC で指定する。
    `config.add_dirs` は渡さない。agy の `--add-dir` はワークスペースの追加で、そこにある
    `AGENTS.md` と skills が全スレッドへ毎回載る。操作は自動承認で既に通る。
    """

    del session
    return [
        config.antigravity_path,
        "acp",
        "--dangerously-skip-permissions",
    ]


class _Waiter:
    """1 ターンの待機口。切断や `hand_back` が届いたら、今待っている箱を即座に起こす。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._box: queue.Queue | None = None

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            box = self._box
        if box is not None:
            box.put(_CANCEL)

    def take(self, conn: "_AcpConnection", box: queue.Queue, deadline: float) -> dict:
        item = self.take_optional(conn, box, deadline, deadline)
        assert item is not None
        return item

    def take_optional(
        self,
        conn: "_AcpConnection",
        box: queue.Queue,
        wait_until: float,
        hard_deadline: float,
    ) -> dict | None:
        with self._lock:
            if self._cancelled:
                raise _TurnCancelled()
            self._box = box
        try:
            now = time.monotonic()
            if now >= hard_deadline:
                raise _TurnTimeout()
            remaining = min(wait_until, hard_deadline) - now
            if remaining <= 0:
                return None
            try:
                item = box.get(timeout=remaining)
            except queue.Empty as error:
                if time.monotonic() >= hard_deadline:
                    raise _TurnTimeout() from error
                return None
        finally:
            with self._lock:
                if self._box is box:
                    self._box = None

        if item is _CANCEL:
            raise _TurnCancelled()
        if item is _EOF:
            assert conn.failure is not None
            raise conn.failure
        return item


class _AcpConnection:
    """1 つの `agy acp` 子プロセスと、その JSON-RPC 振り分けスレッド。"""

    def __init__(self, process, stderr: StderrReader, key: tuple[str, str]) -> None:
        self.process = process
        self.stderr = stderr
        self.key = key
        self.failure: BridgeError | None = None
        self._closing = False
        self._killed = False
        self._last_id = 0
        self._responses: dict[int, queue.Queue] = {}
        self._turns: dict[str, queue.Queue] = {}
        # このプロセス上で開いた会話 ID と、最後に設定したモデル名。
        self._sessions: dict[str, str] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pump_done = threading.Event()
        threading.Thread(target=self._pump, daemon=True).start()

    def has_session(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    def mark_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.setdefault(session_id, "")

    def model_of(self, session_id: str) -> str:
        with self._lock:
            return self._sessions.get(session_id, "")

    def set_session_model(self, session_id: str, model: str) -> None:
        with self._lock:
            self._sessions[session_id] = model

    def begin_prompt(self, session_id: str) -> queue.Queue:
        box: queue.Queue = queue.Queue()
        with self._lock:
            self._turns[session_id] = box
            if self.failure is not None:
                box.put(_EOF)
        return box

    def end_prompt(self, session_id: str, request_id: int | None) -> None:
        with self._lock:
            self._turns.pop(session_id, None)
            if request_id is not None:
                self._responses.pop(request_id, None)

    def forget_response(self, request_id: int) -> None:
        with self._lock:
            self._responses.pop(request_id, None)

    def send_request(
        self, method: str, params: dict, box: queue.Queue | None = None
    ) -> tuple[int, queue.Queue]:
        if box is None:
            box = queue.Queue()
        with self._lock:
            self._last_id += 1
            request_id = self._last_id
            self._responses[request_id] = box
            if self.failure is not None:
                box.put(_EOF)
                return request_id, box
        self._write_json({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return request_id, box

    def notify(self, method: str, params: dict) -> None:
        with contextlib.suppress(BridgeError):
            self._write_json({"jsonrpc": "2.0", "method": method, "params": params})

    def call(
        self,
        method: str,
        params: dict,
        waiter: _Waiter,
        deadline: float,
        grace_seconds: float,
    ) -> dict:
        """単発 RPC を送り、応答メッセージ（`result` または `error` を持つ辞書）を返す。"""

        request_id, box = self.send_request(method, params)
        answered = False
        try:
            message = waiter.take(self, box, deadline)
            answered = True
            return message
        finally:
            if not answered and self.failure is None:
                if grace_seconds > 0 and self._drain_response(box, request_id, grace_seconds):
                    answered = True
                if not answered and self.failure is None:
                    self.kill()
            self.forget_response(request_id)

    def close(self) -> None:
        """常駐プロセスを終了させる。待機中のターンがなければ標準入力 EOF で穏やかに閉じる。"""

        with self._lock:
            if self._closing:
                return
            self._closing = True
            has_active = bool(self._turns or self._responses)
        with self._write_lock, contextlib.suppress(OSError, ValueError):
            self.process.stdin.close()
        if has_active:
            self.kill()
        elif not self._pump_done.wait(CANCEL_GRACE_SECONDS):
            self.kill()
        self._pump_done.wait(1.0)

    def kill(self) -> None:
        """無応答またはプロトコル違反の子プロセスを子孫ごと強制終了する。"""

        with self._lock:
            if self._killed:
                return
            self._killed = True
        with self._write_lock, contextlib.suppress(OSError, ValueError):
            self.process.stdin.close()
        kill_tree(self.process)

    def _drain_response(self, box: queue.Queue, request_id: int, grace_seconds: float) -> bool:
        end = time.monotonic() + grace_seconds
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            try:
                item = box.get(timeout=remaining)
            except queue.Empty:
                return False
            if item is _EOF:
                return True
            if isinstance(item, dict) and item.get("id") == request_id:
                return True

    def _write_json(self, payload: dict) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._write_lock:
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (OSError, ValueError) as error:
            failure = ANTIGRAVITY.error(f"へ書き込めません: {error}", "failed")
            self._fail(failure)
            self.kill()
            raise failure from error

    def _pump(self) -> None:
        failure: BridgeError | None = None
        try:
            for line in self.process.stdout:
                if not line.strip():
                    continue
                self._route(_decode_line(line))
        except BridgeError as error:
            failure = error
            self.kill()
        except OSError as error:
            if not self._closing:
                failure = ANTIGRAVITY.error(f"の出力を読み取れません: {error}", "failed")
        finally:
            returncode = self.process.wait()
            if failure is None and not self._closing:
                if returncode not in (0, None):
                    failure = ANTIGRAVITY.error(
                        f"が異常終了しました (exit={returncode}): {snippet(self.stderr.read())}",
                        "failed",
                    )
                else:
                    failure = ANTIGRAVITY.error("との接続が途中で切れました", "invalid_output")
            self._fail(failure or ANTIGRAVITY.error("との接続を閉じました", "cancelled"))
            self._pump_done.set()

    def _route(self, message: dict) -> None:
        # サーバー起点の要求に無応答だと相手が詰まる。未対応を即座に返して先へ進めさせる。
        if "method" in message and "id" in message:
            with contextlib.suppress(BridgeError):
                self._write_json(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": f"Unsupported client method: {message['method']}",
                        },
                    }
                )
            return

        box = None
        with self._lock:
            if "method" in message:
                if message.get("method") == "session/update":
                    params = message.get("params")
                    session_id = params.get("sessionId") if isinstance(params, dict) else None
                    if isinstance(session_id, str):
                        box = self._turns.get(session_id)
            else:
                box = self._responses.pop(message.get("id"), None)
        if box is not None:
            box.put(message)

    def _fail(self, error: BridgeError) -> None:
        with self._lock:
            if self.failure is not None:
                return
            self.failure = error
            boxes = [*self._responses.values(), *self._turns.values()]
            self._responses.clear()
            self._turns.clear()
        for box in boxes:
            box.put(_EOF)


class AntigravityClient:
    """常駐する `agy acp` への接続を保持し、複数スレッドのターンを多重化する。"""

    def __init__(
        self,
        config: BridgeConfig,
        runner=subprocess.Popen,
        *,
        brain_dir: str | None = DEFAULT_BRAIN_DIR,
        wakeup_settle_seconds: float = WAKEUP_SETTLE_SECONDS,
        wakeup_poll_seconds: float = WAKEUP_POLL_SECONDS,
        subagent_idle_grace_seconds: float = SUBAGENT_IDLE_GRACE_SECONDS,
    ) -> None:
        self._default_config = config
        self._runner = runner
        self._brain_dir = brain_dir
        self._wakeup_settle_seconds = wakeup_settle_seconds
        self._wakeup_poll_seconds = wakeup_poll_seconds
        self._subagent_idle_grace_seconds = subagent_idle_grace_seconds
        self._conn: _AcpConnection | None = None
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def stream(
        self, config: BridgeConfig, prompt: str, session,
        collaboration: AcpCollaboration | None = None,
    ) -> Iterator[tuple[str, str]]:
        """1 ターンを実行し、`reasoning`・`delta`・最終 `text` を発生順に返す。"""

        servers = collaboration.servers if collaboration else []
        waiter = _Waiter()
        deadline = time.monotonic() + config.timeout_seconds
        grace = min(CANCEL_GRACE_SECONDS, max(0.02, config.timeout_seconds))
        session.start(None, stop=waiter.cancel)

        try:
            conn, reused = self._connect(config, waiter, deadline)
            try:
                session_id = self._prepare_session(
                    conn, config, session, waiter, deadline, grace, servers
                )
            except BridgeError as error:
                # 長時間アイドル後に常駐プロセスが切れていた場合だけ、1 回だけ繋ぎ直す。
                # session/prompt 送信前なので二重実行にはならない。
                if (
                    not reused
                    or conn.failure is None
                    or error.code == "antigravity_cli_conversation_lost"
                ):
                    raise
                conn, _ = self._connect(config, waiter, deadline)
                session_id = self._prepare_session(
                    conn, config, session, waiter, deadline, grace, servers
                )
            yield from self._run_prompt(
                conn, session_id, prompt, session, waiter, deadline, grace, collaboration
            )
        except _TurnTimeout as error:
            raise ANTIGRAVITY.error(
                f"が {config.timeout_seconds} 秒以内に終了しませんでした",
                "timeout",
                status=504,
                error_type="timeout_error",
            ) from error
        except _TurnCancelled as error:
            raise ANTIGRAVITY.error("の実行が中断されました", "cancelled") from error

    def _connect(
        self, config: BridgeConfig, waiter: _Waiter, deadline: float
    ) -> tuple[_AcpConnection, bool]:
        key = (config.antigravity_path, config.working_dir)
        with self._lock:
            conn = self._conn
            if conn is not None and conn.failure is None and conn.key == key:
                return conn, True
            if conn is not None:
                self._conn = None
                conn.close()

            process, stderr = launch(
                ANTIGRAVITY,
                build_command(config),
                config.working_dir,
                self._runner,
                env=agy_env(),
            )
            conn = _AcpConnection(process, stderr, key)
            try:
                response = conn.call(
                    "initialize",
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "clientCapabilities": {},
                        "clientInfo": CLIENT_INFO,
                    },
                    waiter,
                    deadline,
                    grace_seconds=0.0,
                )
                if "error" in response:
                    raise ANTIGRAVITY.error(
                        f"の初期化が失敗しました: {_error_text(response['error'])}",
                        "error_result",
                    )
                if not isinstance(response.get("result"), dict):
                    raise ANTIGRAVITY.error(
                        "の initialize 応答にオブジェクトの result がありません",
                        "invalid_output",
                    )
            except BaseException:
                conn.kill()
                raise
            self._conn = conn
            return conn, False

    def _prepare_session(
        self,
        conn: _AcpConnection,
        config: BridgeConfig,
        session,
        waiter: _Waiter,
        deadline: float,
        grace: float,
        servers: list[dict],
    ) -> str:
        if session.resume:
            session_id = self._resume_session(conn, config, session, waiter, deadline, grace, servers)
        else:
            session_id = self._new_session(conn, config, waiter, deadline, grace, servers)

        if conn.model_of(session_id) != config.model:
            response = conn.call(
                "session/set_model",
                {"sessionId": session_id, "modelId": config.model},
                waiter,
                deadline,
                grace,
            )
            if "error" in response:
                raise ANTIGRAVITY.error(
                    f"がエラーを返しました: {_error_text(response['error'])}",
                    "error_result",
                )
            conn.set_session_model(session_id, config.model)
        return session_id

    def _new_session(
        self,
        conn: _AcpConnection,
        config: BridgeConfig,
        waiter: _Waiter,
        deadline: float,
        grace: float,
        servers: list[dict],
    ) -> str:
        response = conn.call(
            "session/new",
            {"cwd": config.working_dir, "mcpServers": servers},
            waiter,
            deadline,
            grace,
        )
        if "error" in response:
            raise ANTIGRAVITY.error(
                f"がエラーを返しました: {_error_text(response['error'])}", "error_result"
            )
        result = response.get("result")
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise ANTIGRAVITY.error("の出力に sessionId がありません", "invalid_output")
        conn.mark_session(session_id)
        return session_id

    def _resume_session(
        self,
        conn: _AcpConnection,
        config: BridgeConfig,
        session,
        waiter: _Waiter,
        deadline: float,
        grace: float,
        servers: list[dict],
    ) -> str:
        target_id = session.handle
        if conn.has_session(target_id):
            return target_id
        response = conn.call(
            "session/resume",
            {"sessionId": target_id, "cwd": config.working_dir, "mcpServers": servers},
            waiter,
            deadline,
            grace,
        )
        if "error" in response:
            session.reset()
            raise ANTIGRAVITY.error(
                "が続きの会話を見つけられませんでした。次の実行で会話を開始し直します",
                "conversation_lost",
            )
        result = response.get("result")
        resumed_id = result.get("sessionId") if isinstance(result, dict) else None
        if not isinstance(resumed_id, str) or not resumed_id:
            raise ANTIGRAVITY.error("の出力に sessionId がありません", "invalid_output")
        if resumed_id != target_id:
            session.reset()
            raise ANTIGRAVITY.error(
                "が続きの会話を見つけられませんでした。次の実行で会話を開始し直します",
                "conversation_lost",
            )
        conn.mark_session(target_id)
        return target_id

    def _run_prompt(
        self,
        conn: _AcpConnection,
        session_id: str,
        prompt: str,
        session,
        waiter: _Waiter,
        deadline: float,
        grace: float,
        collaboration: AcpCollaboration | None,
    ) -> Iterator[tuple[str, str]]:
        turn_box = conn.begin_prompt(session_id)
        prompt_id: int | None = None
        prompt_done = False
        waiting_wakeup = False
        confirmed = False
        chunks: list[str] = []
        thoughts: list[str] = []
        started_tools: set[str] = set()
        tool_names: dict[str, str] = {}
        pending_tools: set[str] = set()
        async_tool_used = False
        subagent_used = False
        subagent_marked_at = 0.0
        initial_root_steps = _count_transcript_lines(self._brain_dir, session_id)
        tool_error = ""

        try:
            prompt_id, _ = conn.send_request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
                turn_box,
            )
            while True:
                item = waiter.take(conn, turn_box, deadline)
                if item.get("method") == "session/update":
                    params = item.get("params")
                    if not isinstance(params, dict) or params.get("sessionId") != session_id:
                        continue
                    update = params.get("update")
                    if not isinstance(update, dict):
                        continue
                    if not confirmed:
                        session.confirm(session_id)
                        confirmed = True
                    events, failure = _collect_update_events(
                        update, chunks, thoughts, started_tools, tool_names, collaboration
                    )
                    _, used_async, used_subagent = _track_async_tool(
                        update, tool_names, pending_tools
                    )
                    if used_async:
                        async_tool_used = True
                    if used_subagent:
                        subagent_used = True
                        subagent_marked_at = time.monotonic()
                    tool_error = failure or tool_error
                    yield from events
                    continue

                if item.get("id") != prompt_id:
                    continue
                prompt_done = True
                if "error" in item:
                    raise ANTIGRAVITY.error(
                        f"がエラーを返しました: {_error_text(item['error'])}",
                        "error_result",
                    )
                result = item.get("result")
                if not isinstance(result, dict):
                    raise ANTIGRAVITY.error("の出力に result がありません", "invalid_output")
                stop_reason = result.get("stopReason")
                if not isinstance(stop_reason, str) or not stop_reason:
                    raise ANTIGRAVITY.error("の出力に stopReason がありません", "invalid_output")
                if stop_reason == "cancelled":
                    raise _TurnCancelled()

                if pending_tools or (
                    (async_tool_used or subagent_used)
                    and _has_pending_transcript_work(
                        self._brain_dir, session_id, initial_root_steps, subagent_marked_at
                    )
                ):
                    waiting_wakeup = True
                    tool_error = (
                        yield from self._wait_for_async_wakeups(
                            conn,
                            turn_box,
                            session_id,
                            waiter,
                            deadline,
                            chunks,
                            thoughts,
                            started_tools,
                            tool_names,
                            pending_tools,
                            subagent_marked_at,
                            initial_root_steps,
                            tool_error,
                            collaboration,
                        )
                    )
                    waiting_wakeup = False

                yield from _flush_thoughts(thoughts)
                text = "".join(chunks)
                if not text.strip() and tool_error and not (collaboration and collaboration.calls):
                    raise ANTIGRAVITY.error(
                        f"がツールを実行できず本文を返しませんでした: {tool_error}",
                        "tool_failed",
                    )
                session.confirm(session_id)
                yield "text", text
                return
        finally:
            if prompt_id is not None and not prompt_done and conn.failure is None:
                conn.notify("session/cancel", {"sessionId": session_id})
                prompt_done = _drain_cancelled_prompt(
                    turn_box, prompt_id, session_id, session, confirmed, grace
                )
                if not prompt_done and conn.failure is None:
                    conn.kill()
            elif waiting_wakeup and conn.failure is None:
                conn.notify("session/cancel", {"sessionId": session_id})
            conn.end_prompt(session_id, prompt_id)

    def _wait_for_async_wakeups(
        self,
        conn: _AcpConnection,
        turn_box: queue.Queue,
        session_id: str,
        waiter: _Waiter,
        deadline: float,
        chunks: list[str],
        thoughts: list[str],
        started_tools: set[str],
        tool_names: dict[str, str],
        pending_tools: set[str],
        subagent_marked_at: float,
        initial_root_steps: int,
        tool_error: str,
        collaboration: AcpCollaboration | None,
    ) -> Iterator[tuple[str, str]]:
        """`end_turn` 後に残っている非同期タスク・サブエージェントの完了と自動再開を待ち受ける。"""

        awaiting_wakeup = False
        settled_since: float | None = None
        idle_since: float | None = None

        while True:
            now = time.monotonic()
            if pending_tools:
                settled_since = None
                idle_since = None
                wait_until = deadline
            elif settled_since is not None:
                wait_until = settled_since + self._wakeup_settle_seconds
                if now >= wait_until:
                    if _has_pending_transcript_work(
                        self._brain_dir, session_id, initial_root_steps, subagent_marked_at
                    ):
                        settled_since = None
                        idle_since = None
                        continue
                    return tool_error
            elif awaiting_wakeup:
                wait_until = deadline
            else:
                if _has_pending_transcript_work(
                    self._brain_dir, session_id, initial_root_steps, subagent_marked_at
                ):
                    idle_since = None
                    wait_until = now + self._wakeup_poll_seconds
                else:
                    if idle_since is None:
                        idle_since = now
                    wait_until = idle_since + self._subagent_idle_grace_seconds
                    if now >= wait_until:
                        return tool_error

            item = waiter.take_optional(conn, turn_box, wait_until, deadline)
            if item is None:
                continue
            if item.get("method") != "session/update":
                continue
            params = item.get("params")
            if not isinstance(params, dict) or params.get("sessionId") != session_id:
                continue
            update = params.get("update")
            if not isinstance(update, dict):
                continue

            idle_since = None
            events, failure = _collect_update_events(
                update, chunks, thoughts, started_tools, tool_names, collaboration
            )
            finished_pending, _, used_subagent = _track_async_tool(
                update, tool_names, pending_tools
            )
            if used_subagent:
                subagent_marked_at = time.monotonic()
            if finished_pending:
                awaiting_wakeup = True
                settled_since = None
            tool_error = failure or tool_error
            yield from events

            kind = update.get("sessionUpdate")
            if kind == "usage_update":
                awaiting_wakeup = False
                settled_since = None if pending_tools else time.monotonic()
            elif kind in ("tool_call", "tool_call_update", "agent_thought_chunk", "agent_message_chunk"):
                awaiting_wakeup = True
                settled_since = None


def stream_antigravity(
    config: BridgeConfig,
    prompt: str,
    session,
    runner=subprocess.Popen,
) -> Iterator[tuple[str, str]]:
    """単発で ACP クライアントを起動して 1 ターン実行する。サーバー常駐時は AntigravityClient を使う。"""

    client = AntigravityClient(config, runner)
    try:
        yield from client.stream(config, prompt, session)
    finally:
        client.close()


def _drain_cancelled_prompt(
    turn_box: queue.Queue,
    prompt_id: int,
    session_id: str,
    session,
    confirmed: bool,
    grace_seconds: float,
) -> bool:
    """`session/cancel` 送信後、そのターンの完了応答が返るのを短時間だけ引き取る。"""

    end = time.monotonic() + grace_seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        try:
            item = turn_box.get(timeout=remaining)
        except queue.Empty:
            return False
        if item is _EOF:
            return True
        if not isinstance(item, dict):
            continue
        if item.get("method") == "session/update" and not confirmed:
            params = item.get("params")
            if (
                isinstance(params, dict)
                and params.get("sessionId") == session_id
                and isinstance(params.get("update"), dict)
            ):
                session.confirm(session_id)
                confirmed = True
        elif item.get("id") == prompt_id:
            return True


def _decode_line(line: bytes) -> dict:
    """標準出力 1 行を JSON-RPC メッセージ辞書として検証する。"""

    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ANTIGRAVITY.error(
            f"の出力が JSON ではありません: {snippet(line)}", "invalid_output"
        ) from error
    if not isinstance(payload, dict) or ("method" not in payload and "id" not in payload):
        raise ANTIGRAVITY.error(
            f"の出力が JSON-RPC ではありません: {snippet(line)}", "invalid_output"
        )
    return payload


def _collect_update_events(
    update: dict,
    chunks: list[str],
    thoughts: list[str],
    started_tools: set[str],
    tool_names: dict[str, str],
    collaboration: AcpCollaboration | None,
) -> tuple[list[tuple[str, str]], str]:
    """`session/update` 1 件から、クライアントへ流すイベント列とツール失敗理由を作る。"""

    kind = update.get("sessionUpdate")
    if kind == "agent_thought_chunk":
        text = _content_text(update.get("content"))
        if text:
            thoughts.append(text)
            if "\n" in text:
                paragraphs = "".join(thoughts).split("\n\n")
                thoughts[:] = [paragraphs.pop()]
                return [("reasoning", p.strip()) for p in paragraphs if p.strip()], ""
        return [], ""

    if kind == "agent_message_chunk":
        events = list(_flush_thoughts(thoughts))
        delta = _content_text(update.get("content"))
        # ツール受付後の agy の返事は、アプリが実行した結果より先に表示しない。
        if delta and not (collaboration and collaboration.calls):
            chunks.append(delta)
            events.append(("delta", delta))
        return events, ""

    if kind not in ("tool_call", "tool_call_update"):
        return [], ""

    if collaboration:
        collaboration.observe(update)
    events = list(_flush_thoughts(thoughts))
    raw_id = update.get("toolCallId")
    call_id = raw_id if isinstance(raw_id, str) and raw_id else ""
    raw_name = update.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name else ""
    if call_id and name:
        tool_names[call_id] = name
    elif call_id and not name:
        name = tool_names.get(call_id, "")
    if not name:
        return events, ""

    key = call_id or f"anon_{len(started_tools)}"
    if key not in started_tools:
        started_tools.add(key)
        events.append(("reasoning", f"**{name} を実行中**\n{_tool_detail(update)}".rstrip()))

    failure = ""
    if update.get("status") == "failed":
        failure = _tool_failure(update)
        events.append(("reasoning", f"**{name} が失敗**\n{failure}"))
    return events, failure


def _flush_thoughts(thoughts: list[str]) -> Iterator[tuple[str, str]]:
    if not thoughts:
        return
    text = "".join(thoughts).strip()
    thoughts.clear()
    if text:
        yield "reasoning", text


def _content_text(content: object) -> str:
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _tool_detail(update: dict) -> str:
    args = update.get("args")
    if not isinstance(args, dict) or not args:
        args = update.get("rawInput")
    if not isinstance(args, dict) or not args:
        return ""
    for key in TOOL_ARGUMENT_KEYS:
        if key in args and args[key] is not None:
            return str(args[key])[:TOOL_DETAIL_CHARS]
    return str(next(iter(args.values())))[:TOOL_DETAIL_CHARS]


def _tool_failure(update: dict) -> str:
    for candidate in (
        _nested_message(update.get("error")),
        _raw_output_error(update.get("rawOutput")),
        _content_list_text(update.get("content")),
        update.get("title"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:TOOL_DETAIL_CHARS]
    return "failed"


def _nested_message(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("message"), str):
        return value["message"]
    return ""


def _raw_output_error(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        return ""
    for field in ("error", "message", "output"):
        text = _nested_message(raw.get(field))
        if text.strip():
            return text
    return ""


def _content_list_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = _content_text(item) or _content_text(item.get("content"))
        if text:
            parts.append(text)
    return "\n".join(parts)


def _error_text(error: object) -> str:
    """JSON-RPC の `error` を読める文字列にする。"""

    if isinstance(error, dict) and isinstance(error.get("message"), str) and error["message"]:
        return error["message"][:STDERR_SNIPPET_BYTES]
    if not isinstance(error, str):
        error = json.dumps(error, ensure_ascii=False)
    return error[:STDERR_SNIPPET_BYTES]


def _is_daemon_tool(update: dict) -> bool:
    """常駐デーモン起動（`IsDaemon=true`）かどうか。終了を待たない対象を除外する。"""

    for key in ("args", "rawInput"):
        mapping = update.get(key)
        if isinstance(mapping, dict) and mapping.get("IsDaemon") is True:
            return True
    return False


def _track_async_tool(
    update: dict, tool_names: dict[str, str], pending_tools: set[str]
) -> tuple[bool, bool, bool]:
    """`tool_call` / `tool_call_update` から実行中の非同期ツールとサブエージェント利用を追跡する。"""

    if update.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
        return False, False, False
    raw_id = update.get("toolCallId")
    call_id = raw_id if isinstance(raw_id, str) and raw_id else ""
    raw_name = update.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name else tool_names.get(call_id, "")
    is_daemon = _is_daemon_tool(update)
    used_async = name in ASYNC_TOOL_NAMES and not is_daemon
    used_subagent = name in SUBAGENT_TOOL_NAMES
    finished_pending = False
    if call_id:
        status = update.get("status")
        if status in TERMINAL_TOOL_STATUSES:
            if call_id in pending_tools:
                pending_tools.discard(call_id)
                finished_pending = True
        elif used_async:
            pending_tools.add(call_id)
    return finished_pending, used_async, used_subagent


def _transcript_path(brain_dir: str | None, conversation_id: str) -> str:
    if not brain_dir or not conversation_id:
        return ""
    return os.path.join(
        brain_dir, conversation_id, ".system_generated", "logs", "transcript.jsonl"
    )


def _count_transcript_lines(brain_dir: str | None, conversation_id: str) -> int:
    path = _transcript_path(brain_dir, conversation_id)
    if not path:
        return 0
    try:
        with open(path, encoding="utf-8") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def _read_transcript_entries(path: str, start_line: int = 0) -> list[dict]:
    entries: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index < start_line or not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    entries.append(item)
    except OSError:
        return []
    return entries


def _is_idle_planner_step(entry: dict) -> bool:
    return (
        entry.get("type") == "PLANNER_RESPONSE"
        and entry.get("status") == "DONE"
        and not entry.get("tool_calls")
    )


def _is_fatal_error_step(entry: dict) -> bool:
    return (
        entry.get("type") == "ERROR_MESSAGE"
        and "attempt " not in str(entry.get("error", ""))
    )


def _unquote_arg(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            decoded = json.loads(text)
            if isinstance(decoded, str):
                return decoded.strip()
        except json.JSONDecodeError:
            pass
    return text


def _is_daemon_transcript_call(tool_call: object) -> bool:
    if not isinstance(tool_call, dict):
        return False
    args = tool_call.get("args")
    if not isinstance(args, dict):
        return False
    return _unquote_arg(args.get("IsDaemon")).lower() == "true"


def _is_conditional_timer_call(tool_call: object) -> bool:
    if not isinstance(tool_call, dict) or tool_call.get("name") != "schedule":
        return False
    args = tool_call.get("args")
    if not isinstance(args, dict):
        return False
    condition = _unquote_arg(args.get("TimerCondition")).lower()
    return bool(condition) and condition != "never"


def _daemon_command_descriptions(tool_calls: list) -> set[str]:
    descriptions: set[str] = set()
    for call in tool_calls:
        if not _is_daemon_transcript_call(call):
            continue
        args = call.get("args")
        if not isinstance(args, dict):
            continue
        cmd = _unquote_arg(args.get("CommandLine"))
        if cmd:
            descriptions.add(cmd.splitlines()[0].strip())
    return descriptions


def _has_pending_transcript_work(
    brain_dir: str | None,
    session_id: str,
    initial_root_steps: int,
    subagent_marked_at: float,
) -> bool:
    """転記ログから未通知のバックグラウンドタスク・未完了サブエージェント・親の継続処理中を判定する。"""

    root_path = _transcript_path(brain_dir, session_id)
    if not root_path:
        return False
    root_entries = _read_transcript_entries(root_path, initial_root_steps)
    if not root_entries:
        return False

    bg_tasks: dict[str, int] = {}
    replied_tasks: dict[str, int] = {}
    dispatched_subagents: dict[str, tuple[int, str]] = {}
    replied_subagents: dict[str, int] = {}
    last_tool_calls: list = []
    daemon_descriptions: set[str] = set()
    generic_offset = 0

    for offset, entry in enumerate(root_entries):
        step_index = entry.get("step_index")
        order = step_index if isinstance(step_index, int) else initial_root_steps + offset
        entry_type = entry.get("type")
        if entry_type == "PLANNER_RESPONSE":
            raw_calls = entry.get("tool_calls")
            last_tool_calls = raw_calls if isinstance(raw_calls, list) else []
            daemon_descriptions = _daemon_command_descriptions(last_tool_calls)
            generic_offset = 0
            continue

        content = entry.get("content")
        if not isinstance(content, str) or not content:
            if entry_type == "GENERIC":
                generic_offset += 1
            continue

        created_at = str(entry.get("created_at") or "")
        if entry_type == "GENERIC":
            matched_call = (
                last_tool_calls[generic_offset] if generic_offset < len(last_tool_calls) else None
            )
            generic_offset += 1
            for pattern in (_SUBAGENT_ID_RE, _SENT_TO_RE):
                for match in pattern.finditer(content):
                    dispatched_subagents[match.group(1)] = (order, created_at)
            task_match = _BG_TASK_ID_RE.search(content)
            if task_match:
                skip = _is_daemon_transcript_call(matched_call) or _is_conditional_timer_call(
                    matched_call
                )
                if not skip and daemon_descriptions:
                    desc_match = _TASK_DESC_RE.search(content)
                    if desc_match and desc_match.group(1).strip() in daemon_descriptions:
                        skip = True
                if not skip:
                    bg_tasks[task_match.group(1)] = order
        elif entry_type == "SYSTEM_MESSAGE":
            for match in _TASK_SENDER_RE.finditer(content):
                replied_tasks[match.group(1)] = order
            for match in _SENDER_RE.finditer(content):
                replied_subagents[match.group(1)] = order

    last_root = root_entries[-1]
    if _is_fatal_error_step(last_root):
        return False
    if not _is_idle_planner_step(last_root):
        return True

    for task_id, launched_order in bg_tasks.items():
        if replied_tasks.get(task_id, -1) <= launched_order:
            return True

    now = time.monotonic()
    for sub_id, (sent_order, sent_created_at) in dispatched_subagents.items():
        if replied_subagents.get(sub_id, -1) > sent_order:
            continue
        sub_path = _transcript_path(brain_dir, sub_id)
        sub_entries = _read_transcript_entries(sub_path)
        if not sub_entries:
            if now - subagent_marked_at < SUBAGENT_START_GRACE_SECONDS:
                return True
            continue
        last_sub = sub_entries[-1]
        last_created_at = str(last_sub.get("created_at") or "")
        if (
            sent_created_at
            and last_created_at
            and last_created_at < sent_created_at
            and now - subagent_marked_at < SUBAGENT_START_GRACE_SECONDS
        ):
            return True
        if _is_idle_planner_step(last_sub) or _is_fatal_error_step(last_sub):
            continue
        return True
    return False
