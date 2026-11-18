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
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping

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
        with self._lock:
            if self._cancelled:
                raise _TurnCancelled()
            self._box = box
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TurnTimeout()
            try:
                item = box.get(timeout=remaining)
            except queue.Empty as error:
                raise _TurnTimeout() from error
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

    def __init__(self, config: BridgeConfig, runner=subprocess.Popen) -> None:
        self._default_config = config
        self._runner = runner
        self._conn: _AcpConnection | None = None
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def stream(
        self, config: BridgeConfig, prompt: str, session
    ) -> Iterator[tuple[str, str]]:
        """1 ターンを実行し、`reasoning`・`delta`・最終 `text` を発生順に返す。"""

        waiter = _Waiter()
        deadline = time.monotonic() + config.timeout_seconds
        grace = min(CANCEL_GRACE_SECONDS, max(0.02, config.timeout_seconds))
        session.start(None, stop=waiter.cancel)

        try:
            conn, reused = self._connect(config, waiter, deadline)
            try:
                session_id = self._prepare_session(conn, config, session, waiter, deadline, grace)
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
                session_id = self._prepare_session(conn, config, session, waiter, deadline, grace)
            yield from self._run_prompt(
                conn, session_id, prompt, session, waiter, deadline, grace
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
    ) -> str:
        if session.resume:
            session_id = self._resume_session(conn, config, session, waiter, deadline, grace)
        else:
            session_id = self._new_session(conn, config, waiter, deadline, grace)

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
    ) -> str:
        response = conn.call(
            "session/new",
            {"cwd": config.working_dir, "mcpServers": []},
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
    ) -> str:
        target_id = session.handle
        if conn.has_session(target_id):
            return target_id
        response = conn.call(
            "session/resume",
            {"sessionId": target_id, "cwd": config.working_dir, "mcpServers": []},
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
    ) -> Iterator[tuple[str, str]]:
        turn_box = conn.begin_prompt(session_id)
        prompt_id: int | None = None
        prompt_done = False
        confirmed = False
        chunks: list[str] = []
        thoughts: list[str] = []
        started_tools: set[str] = set()
        tool_names: dict[str, str] = {}
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
                        update, chunks, thoughts, started_tools, tool_names
                    )
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

                yield from _flush_thoughts(thoughts)
                text = "".join(chunks)
                if not text.strip() and tool_error:
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
            conn.end_prompt(session_id, prompt_id)


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
        if delta:
            chunks.append(delta)
            events.append(("delta", delta))
        return events, ""

    if kind not in ("tool_call", "tool_call_update"):
        return [], ""

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
