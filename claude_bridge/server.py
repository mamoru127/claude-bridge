"""127.0.0.1 で待ち受けるローカル HTTP サーバー。"""

import hmac
import json
import logging
import subprocess
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .app_server import TURN_METHOD, AppServerClient, AppServerError
from .claude_cli import run_claude, stream_claude
from .config import (
    APP_SERVER_PATH,
    HEALTH_PATH,
    IMAGE_PATHS,
    MODELS_PATH,
    PASSTHROUGH_PATH,
    RESPONSES_PATH,
    BridgeConfig,
)
from .errors import BridgeError
from .responses import (
    ParsedRequest,
    ResponseStream,
    build_models,
    parse_request,
    requested_model,
)
from .sessions import PendingCalls, SessionRegistry
from .upstream import (
    RELAY_CHUNK_BYTES,
    fetch_models,
    open_upstream,
    request_headers,
    response_headers,
)

logger = logging.getLogger("claude_bridge")

SSE_DONE = "data: [DONE]\n\n"

# 書き込みが途切れている間に合図を送る間隔。クライアントの待機上限より十分短くする。
KEEPALIVE_INTERVAL_SECONDS = 15


def sse(events: list[tuple[str, dict]]) -> str:
    """イベント列を SSE の本文にする。"""

    return "".join(
        f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        for event_type, payload in events
    )


class Keepalive:
    """本流の書き込みが途切れている間だけ合図を書き足す。

    CLI の読み取りは出力があるまで戻らないため、合図は別スレッドから送る。書き込みは lock で
    直列化する。本流が遅い相手への書き込みで詰まっている間は lock を取れず、合図も止まる。
    """

    def __init__(self, write, chunk: str, interval: float) -> None:
        self._write = write
        self._chunk = chunk
        self._interval = interval
        self._lock = threading.Lock()
        self._idle_since = time.monotonic()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "Keepalive":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stopped.set()
        self._thread.join()

    def write(self, chunk: str) -> None:
        """本流の書き込み。ここからの経過時間で無音を測る。"""

        with self._lock:
            self._idle_since = time.monotonic()
            self._write(chunk)

    def _run(self) -> None:
        while not self._stopped.wait(self._interval):
            with self._lock:
                if time.monotonic() - self._idle_since < self._interval:
                    continue
                self._idle_since = time.monotonic()
                try:
                    self._write(self._chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    # 相手が消えたことは本流の書き込みでも分かる。ここでは合図をやめるだけ。
                    return


class BridgeHandler(BaseHTTPRequestHandler):
    """Responses API の最小エンドポイントを処理する。"""

    protocol_version = "HTTP/1.1"
    server_version = "claude-bridge"
    sys_version = ""
    timeout = 60

    def route_path(self) -> str:
        """ルーティングに使うパス。Codex は `/v1/models?client_version=...` のようにクエリを付ける。"""

        return urlsplit(self.path).path

    def do_GET(self) -> None:
        path = self.route_path()
        if path == HEALTH_PATH:
            self._send_json(200, {"status": "ok"})
            return
        config = self.server.config
        try:
            self._authorize(config)
            if path != MODELS_PATH:
                raise self._not_found(path)
            payload = build_models(config, self._upstream_models(config))
        except BridgeError as error:
            self._send_error(error)
            return
        self._send_json(200, payload)

    def _upstream_models(self, config: BridgeConfig) -> dict | None:
        """中継が有効なときだけ、上流のモデル一覧を取得する。"""

        if not config.upstream_base_url:
            return None
        return fetch_models(config, self.path, request_headers(self.headers))

    def do_POST(self) -> None:
        config = self.server.config
        path = self.route_path()
        try:
            self._authorize(config)
            if path == APP_SERVER_PATH:
                self._send_json(200, {"result": self._app_server_rpc(config)})
                return
            if path == PASSTHROUGH_PATH:
                self._send_json(200, {"result": self._pending_call(config)})
                return
            if path in IMAGE_PATHS:
                if not config.upstream_base_url:
                    raise BridgeError(
                        "画像 API の中継先が設定されていません",
                        status=503,
                        error_type="api_error",
                        code="upstream_not_configured",
                    )
                self._relay(config, self._read_body(config))
                return
            if path != RESPONSES_PATH:
                raise self._not_found(path)
            body = self._read_json_body(config)
            payload = self._decode_json(body)
            # model 省略時の既定。config.model は CLI へ渡す実行モデル名なので使わない。
            default_model = config.claude_models[0]
            if not config.routes_to_claude(requested_model(payload, default_model)):
                self._relay(config, body)
                return
            request = parse_request(
                payload,
                default_model,
                config.passthrough_tools,
                config.tool_search_query,
            )
            # アプリが選んだモデルで実行する。CLI へ届くのは一覧に載せた名前だけ。
            config = replace(config, model=config.cli_model(request.model))
        except BridgeError as error:
            self._send_error(error)
            return

        # 同じ会話は 1 リクエストずつ実行する。先行するリクエストがあれば止めて引き継ぐ。
        # 検索だけのターンも同じ会話の 1 回なので、先行する実行を残したまま返さない。
        session = self.server.sessions.acquire(request.session_key)
        try:
            # 遅延読み込みのツールは、tool_search を 1 回通さないとリクエストへ載らない。
            # Claude を動かす前に検索だけ返し、ツールが揃った次のリクエストで本題へ進む。
            if request.tool_search_query:
                self._send_tool_search(request)
            elif request.stream:
                self._send_stream(config, request, session)
            else:
                self._send_once(config, request, session)
        finally:
            self.server.sessions.release(session)

    def _app_server_rpc(self, config: BridgeConfig) -> object:
        """MCP アダプタからの Codex app-server RPC を、常駐する子プロセスへ中継する。

        turn/start は wait を真にしたときだけ完了を待つ。既定は受理応答だけ返し、ターンは
        このプロセスの子で走り続けるため、アダプタが再起動しても実行は止まらない。
        """

        payload = self._decode_json(self._read_json_body(config))
        if not isinstance(payload, dict):
            raise self._invalid_rpc("リクエストはオブジェクトで指定してください")
        method = payload.get("method")
        if not isinstance(method, str) or not method:
            raise self._invalid_rpc("method は文字列で指定してください")
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise self._invalid_rpc("params はオブジェクトで指定してください")

        client = self.server.app_server
        try:
            if method != TURN_METHOD:
                return client.call(method, params)
            if payload.get("wait"):
                return client.run_turn(params)
            return client.start_turn(params)
        except AppServerError as error:
            raise BridgeError(
                str(error), status=502, error_type="api_error", code="app_server_failed"
            ) from error

    def _pending_call(self, config: BridgeConfig) -> dict:
        """受け渡しアダプタが預けたツール呼び出しを、実行中のターンへ記録する。

        実行はしない。ターンの終わりに function_call として Codex へ渡し、Codex が実行する。
        """

        payload = self._decode_json(self._read_json_body(config))
        if not isinstance(payload, dict):
            raise self._invalid_rpc("リクエストはオブジェクトで指定してください")
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise self._invalid_rpc("name は文字列で指定してください")
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise self._invalid_rpc("arguments はオブジェクトで指定してください")
        namespace = payload.get("namespace")
        if namespace is not None and not isinstance(namespace, str):
            raise self._invalid_rpc("namespace は文字列で指定してください")
        try:
            call_id = self.server.pending.add(
                str(payload.get("token")), name, arguments, namespace
            )
        except KeyError as error:
            # 終わったターンの預かり口へ届いた呼び出し。記録しても渡す先がない。
            raise self._invalid_rpc("この実行の預かり口はすでに閉じています") from error
        return {"call_id": call_id}

    def _invalid_rpc(self, message: str) -> BridgeError:
        return BridgeError(
            message, status=400, error_type="invalid_request_error", code="invalid_request"
        )

    def _relay(self, config: BridgeConfig, body: bytes) -> None:
        """Claude が受け持たないモデルは、受信ヘッダと本文のまま上流へ中継する。"""

        connection, response = open_upstream(
            config, self.path, self.command, request_headers(self.headers), body
        )
        try:
            # 本文長は上流のヘッダをそのまま返すが、SSE では長さが決まらないため接続を閉じる。
            self.close_connection = True
            self.send_response(response.status)
            for name, value in response_headers(response):
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while chunk := response.read1(RELAY_CHUNK_BYTES):
                self.wfile.write(chunk)
                self.wfile.flush()
            logger.info("relay completed status=%d", response.status)
        except (BrokenPipeError, ConnectionResetError):
            logger.info("relay aborted by client")
        finally:
            connection.close()

    def _send_tool_search(self, request: ParsedRequest) -> None:
        """tool_search だけを返す。Claude は動かさないため会話の状態も変えない。"""

        stream = ResponseStream(request.model)
        events = stream.tool_search_call(request.tool_search_query)
        logger.info("tool search issued query=%s", request.tool_search_query)
        if not request.stream:
            self._send_json(200, stream.calls_response())
            return
        try:
            self._send_sse_headers()
            self._write(sse(stream.started() + events + stream.completed_calls()))
            self._write(SSE_DONE)
        except (BrokenPipeError, ConnectionResetError):
            logger.info("stream aborted by client")

    def _send_once(self, config: BridgeConfig, request: ParsedRequest, session) -> None:
        token = self._open_pending(request)
        try:
            try:
                content = session.prepare(request.messages)
                text = run_claude(
                    config,
                    content,
                    request.system_prompt,
                    session,
                    self.server.runner,
                    request.tools,
                    token,
                )
            except BridgeError as error:
                self._send_error(error)
                return
            stream = ResponseStream(request.model)
            stream.function_calls(self.server.pending.close(token))
            self._log_completed(config, request, session, text)
            self._send_json(200, stream.final_response(text))
        finally:
            # 失敗や切断で渡しそびれた預かり口を残さない。渡し済みなら何も起きない。
            self.server.pending.close(token)

    def _open_pending(self, request: ParsedRequest) -> str:
        """受け渡すツールがあるときだけ預かり口を開ける。無ければアダプタも起動しない。"""

        return self.server.pending.open() if request.tools else ""

    def _send_stream(self, config: BridgeConfig, request: ParsedRequest, session) -> None:
        """SSE で返す。

        CLI の思考とツール使用を reasoning、作業の区切りで書く本文と最終テキストを message
        として送る。本文はいずれも 1 個の delta で送り切る。
        """

        stream = ResponseStream(request.model)
        keepalive = Keepalive(self._write, sse(stream.keepalive()), KEEPALIVE_INTERVAL_SECONDS)
        token = self._open_pending(request)
        try:
            self._send_sse_headers()
            with keepalive as alive:
                # CLI の完了を待つ前に受理を伝える。ここから先のエラーは SSE で返すしかない。
                alive.write(sse(stream.started()))
                text = ""
                try:
                    content = session.prepare(request.messages)
                    for kind, value in stream_claude(
                        config,
                        content,
                        request.system_prompt,
                        session,
                        self.server.runner,
                        request.tools,
                        token,
                    ):
                        if kind == "reasoning":
                            alive.write(sse(stream.reasoning(value)))
                        elif kind == "message":
                            alive.write(sse(stream.message(value)))
                        else:
                            text = value
                except BridgeError as error:
                    logger.warning("stream failed status=%d code=%s", error.status, error.code)
                    alive.write(sse(stream.failed(error)))
                else:
                    # 本文より先に出す。Codex は function_call を見てツールを実行し、
                    # 結果を次のリクエストへ載せてくる。
                    alive.write(sse(stream.function_calls(self.server.pending.close(token))))
                    alive.write(sse(stream.completed(text)))
                    self._log_completed(config, request, session, text)
                alive.write(SSE_DONE)
        except (BrokenPipeError, ConnectionResetError):
            logger.info("stream aborted by client")
        finally:
            # 中断や失敗で渡しそびれた預かり口を残さない。渡し済みなら何も起きない。
            self.server.pending.close(token)

    def _log_completed(
        self, config: BridgeConfig, request: ParsedRequest, session, text: str
    ) -> None:
        logger.info(
            "responses completed model=%s cli_model=%s stream=%s resume=%s output_chars=%d",
            request.model,
            config.model,
            request.stream,
            session.resume,
            len(text),
        )

    def _authorize(self, config: BridgeConfig) -> None:
        if not config.api_key:
            return
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), config.api_key):
            raise BridgeError(
                "Authorization ヘッダが不正です",
                status=401,
                error_type="invalid_request_error",
                code="invalid_api_key",
            )

    def _read_body(self, config: BridgeConfig) -> bytes:
        """中継でそのまま送れるよう、本文を生のバイト列で読む。"""

        return self.rfile.read(self._content_length(config))

    def _read_json_body(self, config: BridgeConfig) -> bytes:
        """Responses API と内部 RPC 用の JSON 本文を読む。"""

        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("application/json"):
            raise BridgeError(
                "Content-Type は application/json を指定してください",
                status=415,
                error_type="invalid_request_error",
                code="unsupported_media_type",
            )
        return self._read_body(config)

    def _decode_json(self, body: bytes) -> object:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BridgeError(
                "リクエストボディを JSON として解釈できません",
                status=400,
                error_type="invalid_request_error",
                code="invalid_json",
            ) from error

    def _content_length(self, config: BridgeConfig) -> int:
        """ボディを読む前に Content-Length を検証し、読み取ってよいバイト数を返す。"""

        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise BridgeError(
                "Content-Length が必要です",
                status=411,
                error_type="invalid_request_error",
                code="length_required",
            )
        try:
            length = int(length_header)
        except ValueError as error:
            raise self._invalid_length() from error
        # 負値は 0 バイト読み取りや無限読み取りを招くため、読み取り前に拒否する。
        if length < 0:
            raise self._invalid_length()
        if length > config.max_request_bytes:
            raise BridgeError(
                f"リクエストボディが上限 {config.max_request_bytes} バイトを超えました",
                status=413,
                error_type="invalid_request_error",
                code="request_too_large",
            )
        return length

    def _invalid_length(self) -> BridgeError:
        return BridgeError(
            "Content-Length は 0 以上の整数で指定してください",
            status=400,
            error_type="invalid_request_error",
            code="invalid_request",
        )

    def _not_found(self, path: str) -> BridgeError:
        # クエリ文字列は秘密値を含みうるため、エラー本文にもパスだけを載せる。
        return BridgeError(
            f"未対応のパスです: {path}",
            status=404,
            error_type="invalid_request_error",
            code="not_found",
        )

    def _send_error(self, error: BridgeError) -> None:
        logger.warning("request failed status=%d code=%s", error.status, error.code)
        self._send_json(error.status, error.to_payload())

    def _send_sse_headers(self) -> None:
        # 本文長が事前に決まらないため、接続を閉じることで終端を示す。
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def _write(self, chunk: str) -> None:
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # エラー時はボディ未読の可能性があるため接続を再利用しない。
        if status >= 400:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code="-", size="-") -> None:
        # 既定の実装はクエリ文字列込みのリクエスト行を出す。クエリは秘密値を含みうるため除く。
        self.log_message('"%s %s" %s', self.command, self.route_path(), code)

    def log_message(self, format: str, *args) -> None:
        # 既定の stderr 出力を抑え、秘密情報を含まないリクエスト行だけを記録する。
        logger.info("%s - %s", self.address_string(), format % args)


class BridgeServer(ThreadingHTTPServer):
    """設定と CLI 実行関数を保持する HTTP サーバー。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: BridgeConfig, runner=subprocess.Popen) -> None:
        super().__init__((config.host, config.port), BridgeHandler)
        self.config = config
        self.runner = runner
        self.sessions = SessionRegistry()
        # 受け渡しアダプタは別プロセスなので、預かった呼び出しはサーバー側で保持する。
        self.pending = PendingCalls()
        # app-server はここで持つ。実行中のターンをアダプタの再起動から切り離すための要。
        self.app_server = AppServerClient(socket_path=config.app_server_socket)

    def server_close(self) -> None:
        self.app_server.close()
        super().server_close()
