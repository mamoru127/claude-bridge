"""Claude 以外のモデル向けのリクエストを、上流の Codex へそのまま中継する。

Codex は `requires_openai_auth = true` のプロバイダへ ChatGPT の認証ヘッダを付けて送る。
その認証をそのまま転送するため、ブリッジ自身は資格情報を持たない。
"""

import http.client
import json
from urllib.parse import urlsplit

from .config import API_PREFIX, BridgeConfig
from .errors import BridgeError

# 接続単位のヘッダ。中継先とは別の接続を張るため転送しない。
HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# リクエスト側は http.client が張り直すため、受信値を転送しない。
REBUILT_REQUEST_HEADERS = frozenset({"host", "content-length"})

RELAY_CHUNK_BYTES = 64 * 1024


def request_headers(headers) -> dict[str, str]:
    """受信ヘッダのうち中継してよいものを返す。Authorization もそのまま渡す。"""

    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in HOP_HEADERS and name.lower() not in REBUILT_REQUEST_HEADERS
    }


def response_headers(response: http.client.HTTPResponse) -> list[tuple[str, str]]:
    """上流のレスポンスヘッダのうち、そのまま返してよいものを返す。"""

    return [
        (name, value)
        for name, value in response.getheaders()
        if name.lower() not in HOP_HEADERS
    ]


def open_upstream(
    config: BridgeConfig,
    path: str,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    """上流へ接続し、本文を読み取れる状態のレスポンスを返す。呼び出し側が close する。"""

    target = urlsplit(config.upstream_base_url)
    connection_class = (
        http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(target.netloc, timeout=config.timeout_seconds)
    try:
        connection.request(method, upstream_path(target.path, path), body=body, headers=headers)
        return connection, connection.getresponse()
    except (OSError, http.client.HTTPException) as error:
        connection.close()
        raise BridgeError(
            f"上流へ中継できません: {config.upstream_base_url}: {error}",
            status=502,
            error_type="api_error",
            code="upstream_unreachable",
        ) from error


def upstream_path(base_path: str, path: str) -> str:
    """受信パスの `/v1` を、中継先 URL のパスへ置き換える。"""

    return base_path.rstrip("/") + path[len(API_PREFIX) :]


def fetch_models(config: BridgeConfig, path: str, headers: dict[str, str]) -> dict:
    """上流のモデル一覧を取得する。中継が有効なときだけ呼ばれる。"""

    connection, response = open_upstream(config, path, "GET", headers, None)
    try:
        raw = response.read()
        status = response.status
    finally:
        connection.close()

    if status != 200:
        raise BridgeError(
            f"上流のモデル一覧を取得できませんでした (status={status})",
            status=502,
            error_type="api_error",
            code="upstream_models_failed",
        )
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BridgeError(
            "上流のモデル一覧を JSON として解釈できません",
            status=502,
            error_type="api_error",
            code="upstream_models_invalid",
        ) from error
    if not isinstance(payload, dict):
        raise BridgeError(
            "上流のモデル一覧がオブジェクトではありません",
            status=502,
            error_type="api_error",
            code="upstream_models_invalid",
        )
    return payload
