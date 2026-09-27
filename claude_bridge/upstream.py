"""Claude 以外のモデル向けのリクエストを、上流の Codex へそのまま中継する。

Codex は `requires_openai_auth = true` のプロバイダへ ChatGPT の認証ヘッダを付けて送る。
その認証をそのまま転送するため、ブリッジ自身は資格情報を持たない。
"""

import http.client
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
UPSTREAM_CLIENT_VERSION = "0.147.0"

# 旧ブリッジは UUID4 のみを付けていた。既存タスクも切り替えられるよう、その形式も扱う。
BRIDGE_ITEM_ID = re.compile(r"(?:rs|msg|fc|tsc)_(?:bridge_)?[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}")


def responses_body(payload: object, original: bytes) -> bytes:
    """ローカル生成の項目を、上流の保存済み ID として参照させない。"""

    if not isinstance(payload, dict) or not isinstance(payload.get("input"), list):
        return original
    items = []
    for item in payload["input"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not BRIDGE_ITEM_ID.fullmatch(item["id"])
            or item.get("encrypted_content")
        ):
            items.append(item)
        elif item.get("type") == "reasoning" and item["id"].startswith("rs_"):
            # CLI の進捗表示には上流で復元できる推論状態がない。
            continue
        elif item.get("type") in ("message", "function_call", "tool_search_call"):
            # 本文・phase・call_id と呼び出し結果の対応は保持し、保存先を示す ID だけ外す。
            items.append({key: value for key, value in item.items() if key != "id"})
        else:
            items.append(item)
    if items == payload["input"]:
        return original
    return json.dumps({**payload, "input": items}, ensure_ascii=False).encode("utf-8")


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

    connection, response = open_upstream(config, models_request_path(path), "GET", headers, None)
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


def models_request_path(path: str) -> str:
    """上流が必須とする client_version をモデル一覧要求へ補う。"""

    parts = urlsplit(path)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(name == "client_version" for name, _ in query):
        return path
    query.append(("client_version", UPSTREAM_CLIENT_VERSION))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
