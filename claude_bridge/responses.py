"""OpenAI Responses API のリクエスト解釈とレスポンス生成。"""

import json
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import BridgeConfig
from .errors import BridgeError

# Codex がモデルごとに読むメタデータ。tools / tool_choice を無視する実装に合わせた値を返す。
REASONING_LEVELS = ("low", "medium", "high")

SYSTEM_ROLES = ("system", "developer")
MESSAGE_ROLES = ("user", "assistant")
TEXT_PART_TYPES = ("input_text", "output_text", "text")
IMAGE_PART_TYPE = "input_image"
# Claude が受け付ける画像形式。ここで弾かないと CLI 実行後の 502 になり原因が分かりにくい。
IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")

# 無音が続く間に送るイベントの型。Responses の型と衝突しない名前にする。
KEEPALIVE_EVENT = "bridge.keepalive"

logger = logging.getLogger("claude_bridge")


@dataclass(frozen=True)
class Message:
    """会話の 1 発言。parts にはテキスト `str` と画像ブロック dict が入力順に並ぶ。"""

    role: str
    parts: tuple


@dataclass(frozen=True)
class ParsedRequest:
    """Claude CLI へ渡す形に正規化したリクエスト。"""

    model: str
    # 会話の発言列。Claude へ実際に渡す範囲はセッションの続き具合で決まるため、
    # ここでは連結せず発言のまま持つ。
    messages: tuple[Message, ...]
    system_prompt: str | None
    stream: bool
    # 会話を識別するキー。Codex はスレッドごとに固定の値を送る。
    session_key: str | None
    # Codex へ受け渡すツール定義。Codex が送ってきた形のまま持ち、MCP へ写して Claude へ見せる。
    tools: tuple[dict, ...] = ()
    # 遅延読み込みのツールを引き出すために投げる検索語。None なら投げる必要がない。
    tool_search_query: str | None = None


def requested_model(payload: object, default_model: str) -> str:
    """中継先の判定に使うモデル名。値の妥当性は parse_request があらためて検証する。"""

    if isinstance(payload, dict) and isinstance(payload.get("model"), str):
        return payload["model"]
    return default_model


def parse_request(
    payload: object,
    default_model: str,
    passthrough_tools: Sequence[str] = (),
    tool_search_query: str = "",
) -> ParsedRequest:
    """Responses API のリクエストを検証し、プロンプトへ正規化する。"""

    if not isinstance(payload, dict):
        raise _invalid("リクエストボディは JSON オブジェクトである必要があります")

    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise _invalid("stream は真偽値で指定してください", param="stream")

    # tool_choice / previous_response_id は Codex が常に送るため受理するが、値は読まない。
    # 会話の対応づけには prompt_cache_key だけを使う。
    model = payload.get("model")
    if model is not None and not isinstance(model, str):
        raise _invalid("model は文字列で指定してください", param="model")

    session_key = payload.get("prompt_cache_key")
    if session_key is not None and not isinstance(session_key, str):
        raise _invalid(
            "prompt_cache_key は文字列で指定してください", param="prompt_cache_key"
        )

    system_sections = []
    instructions = payload.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise _invalid("instructions は文字列で指定してください", param="instructions")
        if instructions.strip():
            system_sections.append(instructions)

    messages = []
    payload_input = payload.get("input")
    if isinstance(payload_input, str):
        if not payload_input.strip():
            raise _invalid("input が空です", param="input")
        messages.append(Message("user", (payload_input,)))
    elif isinstance(payload_input, list):
        for index, item in enumerate(payload_input):
            parsed = _parse_input_item(item, index)
            if parsed is None:
                continue
            role, parts = parsed
            # テキストが空白だけで画像もないメッセージは、送っても伝わる内容がないので捨てる。
            if not any(isinstance(part, dict) or part.strip() for part in parts):
                continue
            if role in SYSTEM_ROLES:
                system_sections.append(_system_text(parts, f"input[{index}].content"))
            else:
                messages.append(Message(role, tuple(parts)))
    else:
        raise _invalid("input は文字列または配列で指定してください", param="input")

    if not messages:
        raise _invalid(
            "input に user もしくは assistant のテキストか画像が必要です", param="input"
        )

    tools, search_query = _passthrough_tools(
        payload_input, passthrough_tools, tool_search_query
    )
    return ParsedRequest(
        model=model or default_model,
        messages=tuple(messages),
        system_prompt="\n\n".join(system_sections) or None,
        stream=stream,
        session_key=session_key,
        tools=tools,
        tool_search_query=search_query,
    )


def _passthrough_tools(
    payload_input: object, allowed: Sequence[str], search_query: str = ""
) -> tuple[tuple[dict, ...], str | None]:
    """Codex が受け渡す名前で送ってきた function 定義と、必要なら投げる検索語を返す。

    Codex はツールを top-level の tools ではなく、input に混ぜた additional_tools
    アイテムで送る。codex_app のような namespace 配下のツールは、呼び出しに
    namespace 名が要るため添えて平坦に並べる。

    遅延読み込み（deferLoading）のツールは additional_tools に載らず、tool_search を
    通して初めて tool_search_output として会話へ入る。そのため両方から拾う。

    MCP のツール名は 1 つの意味しか持てないため、同じ名前は最初の 1 件だけを採る。
    残すと Claude からは区別できないまま、どちらの namespace へ渡るかが定まらない。
    """

    if not allowed or not isinstance(payload_input, list):
        return (), None
    offered: list[str] = []
    found: dict[str, dict] = {}
    searchable = False
    searched = False
    for item in payload_input:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "tool_search_call":
            # 一度投げた検索は繰り返さない。取れなかった場合に投げ続けると会話が進まない。
            searched = True
            continue
        if kind not in ("additional_tools", "tool_search_output"):
            continue
        for tool in item.get("tools") or []:
            if kind == "additional_tools":
                offered.extend(_tool_names(tool))
                searchable = searchable or _is_tool_search(tool)
            for expanded in _expand_tool(tool, allowed):
                found.setdefault(expanded["name"], expanded)
    missing = [name for name in allowed if name not in found]
    query = search_query if (missing and searchable and not searched and search_query) else None
    logger.info(
        "codex tools offered=%s passed=%s search=%s",
        ",".join(offered),
        ",".join(found),
        query or "-",
    )
    return tuple(found.values()), query


def _is_tool_search(tool: object) -> bool:
    """遅延読み込みのツールを引き出す tool_search が使えるかどうか。"""

    return isinstance(tool, dict) and tool.get("type") == "tool_search"


def _tool_names(tool: object) -> list[str]:
    """Codex が送ってきたツールの名前。どれが届いたかを記録するために使う。"""

    if not isinstance(tool, dict):
        return []
    if tool.get("type") == "namespace":
        return [
            f"{tool.get('name')}.{child.get('name')}"
            for child in tool.get("tools") or []
            if isinstance(child, dict)
        ]
    return [str(tool.get("name") or tool.get("type"))]


def _expand_tool(tool: object, allowed: Sequence[str]) -> list[dict]:
    """ツール 1 件を、受け渡す function 定義の並びにする。対象外なら空。

    Claude へ写せるのは引数スキーマを持つ function だけなので、custom や tool_search は
    受け渡しの対象にしない。
    """

    if not isinstance(tool, dict):
        return []
    if tool.get("type") == "namespace":
        return [
            dict(child, namespace=tool.get("name"))
            for child in tool.get("tools") or []
            if _is_allowed_function(child, allowed)
        ]
    return [tool] if _is_allowed_function(tool, allowed) else []


def _is_allowed_function(tool: object, allowed: Sequence[str]) -> bool:
    return (
        isinstance(tool, dict)
        and tool.get("type") == "function"
        and tool.get("name") in allowed
    )


def _parse_input_item(item: object, index: int) -> tuple[str, list] | None:
    """message アイテムを (role, parts) にする。message 以外は None を返して無視する。"""

    param = f"input[{index}]"
    if not isinstance(item, dict):
        raise _invalid("input の要素はオブジェクトである必要があります", param=param)

    # 受け渡したツールの実行結果は Codex が function_call_output として返す。Claude 側では
    # 依頼を出したところでターンが終わっているため、続きのターンへ user の発言として渡す。
    if item.get("type") == "function_call_output":
        return "user", [_tool_output_text(item)]

    # Codex は additional_tools や function_call など、会話ではないアイテムを input に混ぜて送る。
    # function_call は Claude 自身の依頼で転記済みなので、結果以外は読まずに捨てる。
    if item.get("type", "message") != "message":
        return None

    role = item.get("role")
    if role not in SYSTEM_ROLES + MESSAGE_ROLES:
        raise _invalid(f"role={role} は未対応です", param=f"{param}.role")

    return role, _parse_content(item.get("content"), f"{param}.content")


def _tool_output_text(item: dict) -> str:
    """function_call_output を Claude へ見せる本文にする。call_id で依頼と対応づける。"""

    output = item.get("output")
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False)
    return f"[Codex ツール結果] call_id={item.get('call_id')}\n{output}"


def _parse_content(content: object, param: str) -> list:
    """content を、テキストは `str`、画像は Claude の image ブロックにした並びへ変換する。

    連続するテキストは 1 本にまとめる。ブロックが分かれるのは画像が挟まる位置だけになる。
    """

    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        raise _invalid("content は文字列または配列で指定してください", param=param)

    parts: list = []
    for index, part in enumerate(content):
        part_param = f"{param}[{index}]"
        if not isinstance(part, dict):
            raise _invalid("content の要素はオブジェクトである必要があります", param=part_param)
        part_type = part.get("type")
        if part_type in TEXT_PART_TYPES:
            text = part.get("text")
            if not isinstance(text, str):
                raise _invalid("content の text は文字列である必要があります", param=part_param)
            if parts and isinstance(parts[-1], str):
                parts[-1] += "\n" + text
            else:
                parts.append(text)
        elif part_type == IMAGE_PART_TYPE:
            parts.append(_image_block(part, part_param))
        else:
            raise BridgeError(
                f"content type={part_type} は未対応です。テキストと画像のみ扱えます",
                status=400,
                error_type="invalid_request_error",
                code="unsupported_content_part",
                param=part_param,
            )
    return parts


def _image_block(part: dict, param: str) -> dict:
    """`input_image` を Claude の image ブロックにする。"""

    url = part.get("image_url")
    if not isinstance(url, str) or not url:
        raise _invalid("input_image は image_url を文字列で指定してください", param=param)
    return {"type": "image", "source": _image_source(url, param)}


def _image_source(url: str, param: str) -> dict:
    """data URL は中身を埋め込み、http(s) は URL のまま渡す。どちらも Claude が受け付ける形。"""

    if url.startswith("data:"):
        header, separator, data = url[len("data:") :].partition(",")
        media_type, _, encoding = header.partition(";")
        if not separator or encoding != "base64" or media_type not in IMAGE_MEDIA_TYPES:
            raise _invalid(
                f"data URL は base64 の {' / '.join(IMAGE_MEDIA_TYPES)} で指定してください",
                param=param,
            )
        return {"type": "base64", "media_type": media_type, "data": data}
    if urlsplit(url).scheme in ("http", "https"):
        return {"type": "url", "url": url}
    raise _invalid("image_url は data URL か http(s) の URL で指定してください", param=param)


def _system_text(parts: list, param: str) -> str:
    """system / developer の内容。`--append-system-prompt` は文字列なので画像は置けない。"""

    if any(not isinstance(part, str) for part in parts):
        raise _invalid("system / developer のメッセージに画像は指定できません", param=param)
    return "\n".join(parts)


def build_content(messages: Sequence[Message]) -> tuple[dict, ...]:
    """発言列を Claude CLI へ渡す content ブロック列にする。

    テキストは 1 本のプロンプトへ連結し、画像が挟まる位置でブロックを切る。
    """

    labelled = len(messages) > 1 or messages[0].role != "user"
    blocks: list[dict] = []
    text = ""
    for index, message in enumerate(messages):
        if index:
            text += "\n\n"
        if labelled:
            text += f"[{message.role}]\n"
        for part in message.parts:
            if isinstance(part, str):
                text += part
                continue
            blocks += _text_blocks(text)
            text = ""
            blocks.append(part)
    return tuple(blocks + _text_blocks(text))


def _text_blocks(text: str) -> list[dict]:
    """区間のテキストブロック。空白だけのブロックは中身がないので送らない。"""

    return [{"type": "text", "text": text}] if text.strip() else []


def build_response(model: str, text: str) -> dict:
    """Responses API 形式の成功レスポンスを組み立てる。"""

    return ResponseStream(model).final_response(text)


def build_models(config: BridgeConfig, upstream: dict | None = None) -> dict:
    """GET /v1/models が返すモデル一覧。

    `data` は OpenAI 標準の形。`models` は Codex が読む形（`data` しかないと
    `missing field models` で解釈に失敗する）。中継が有効なときは上流の一覧を先に並べ、
    Claude が受け持つ名前を後ろへ足す。同じ名前は Claude 側だけを残す。

    載せるのは `claude_models` だけ。起動時の `--model` は CLI に渡す実行モデルであって
    API で受け付ける名前ではないため、混ぜると中継が有効なときに上流へ流れて失敗する。
    """

    created = int(time.time())
    claude_ids = list(dict.fromkeys(config.claude_models))
    upstream_data, upstream_models = _upstream_entries(upstream, claude_ids)

    # 雛形は上流のエントリ。中継が無効なら空で、Claude 側の項目だけを並べる。
    template = upstream_models[0] if upstream_models else {}
    models = upstream_models + [_codex_model(model_id, template) for model_id in claude_ids]
    return {
        "object": "list",
        "data": upstream_data
        + [
            {"id": model_id, "object": "model", "created": created, "owned_by": "anthropic"}
            for model_id in claude_ids
        ],
        # 上流とブリッジで priority が重複しないよう、並べた順に振り直す。
        "models": [{**item, "priority": priority} for priority, item in enumerate(models, start=1)],
    }


def _upstream_entries(upstream: dict | None, claude_ids: list[str]) -> tuple[list, list]:
    """上流の一覧から、Claude が受け持つ名前を除いたものを返す。"""

    if upstream is None:
        return [], []
    return (
        [item for item in _dict_items(upstream, "data") if item.get("id") not in claude_ids],
        [item for item in _dict_items(upstream, "models") if item.get("slug") not in claude_ids],
    )


def _dict_items(payload: dict, name: str) -> list[dict]:
    value = payload.get(name)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _codex_model(slug: str, template: dict) -> dict:
    """Codex が読むモデルメタデータ。ブリッジが実装していない機能を打ち消した値で埋める。

    Codex は必須項目が欠けたエントリを 1 つ見つけるだけで一覧全体の解釈に失敗する。
    必須項目は Codex の版で増えるため自前では列挙せず、上流のエントリを雛形にして
    受け継ぎ、ブリッジが実装していない機能だけを下の値で打ち消す。
    """

    return {
        **template,
        "slug": slug,
        # WebSocket は実装していないため、雛形が要求していても使わせない。
        "prefer_websockets": False,
        "display_name": f"Claude CLI ({slug})",
        "description": "claude-bridge 経由の Claude CLI。入力はテキストと画像、出力はテキスト。",
        "visibility": "list",
        "supported_in_api": True,
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [
            {"effort": effort, "description": effort} for effort in REASONING_LEVELS
        ],
        # ここはリクエストの reasoning summary パラメータをどう送るかの宣言。
        # ブリッジはリクエストのパラメータを読まず、実行中のツール使用を常に reasoning として
        # 送るため、要求の有無で内容が変わらない。要求させない値にしておく。
        "supports_reasoning_summary_parameter": False,
        "default_reasoning_summary": "none",
        "context_window": 200000,
        "max_context_window": 200000,
    }


class ResponseStream:
    """1 リクエスト分のレスポンスと SSE イベント列を組み立てる。

    ID・created_at・sequence_number・output_index を 1 リクエスト内で一貫させるため、
    状態を持つ。最終テキストは CLI の完了後に確定するので、全文を 1 個の delta として流す。
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.response_id = f"resp_{uuid.uuid4().hex}"
        self.item_id = f"msg_{uuid.uuid4().hex}"
        self.created_at = int(time.time())
        self._sequence = 0
        self._output_index = 0
        # 送出済みの function_call と作業コメント。完了レスポンスの output にも同じ並びで載せる。
        self._items: list[dict] = []

    def final_response(self, text: str) -> dict:
        """完了時の response オブジェクト。非ストリームのボディでもある。"""

        return self._response("completed", text)

    def started(self) -> list[tuple[str, dict]]:
        """CLI を起動する前に送る、リクエスト受理を示すイベント。"""

        return [
            self._event("response.created", response=self._response("in_progress", None)),
            self._event("response.in_progress", response=self._response("in_progress", None)),
        ]

    def keepalive(self) -> list[tuple[str, dict]]:
        """無音が続く間に送る合図。

        クライアントは受信が途切れたまま一定時間が過ぎると接続を切る。SSE のコメント行では
        イベントが 1 件も生まれず計測がリセットされないため、data を持つイベントとして送る。
        Responses のイベントではないので、受け取り側は未知の型として読み飛ばす。
        """

        return [(KEEPALIVE_EVENT, {"type": KEEPALIVE_EVENT})]

    def reasoning(self, text: str) -> list[tuple[str, dict]]:
        """実行中の作業を Codex に見せる reasoning アイテム。

        Codex が読むのは Responses のイベントだけなので、reasoning も本文と同じ
        `response.output_item.done` として output に並べる。表示に使われるのは
        `summary` で、`text` だけのアイテムは読まれない。
        """

        item = {
            "id": f"rs_{uuid.uuid4().hex}",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": text}],
        }
        return [self._event("response.output_item.done", output_index=self._next_index(), item=item)]

    def message(self, text: str) -> list[tuple[str, dict]]:
        """ターン途中の作業コメントを本文として送るイベント列。

        reasoning は実行中の表示にしか使われない。作業の区切りで Claude が書く説明は、
        commentary phase の message アイテムとして送り、会話の記録に残す。アイテム ID は
        最終テキストとも他の作業コメントとも別にする。
        """

        item_id = f"msg_{uuid.uuid4().hex}"
        self._items.append(self._item(text, item_id, "commentary"))
        return self._message_events(item_id, text, "commentary")

    def function_calls(self, calls: Sequence[dict]) -> list[tuple[str, dict]]:
        """Claude が依頼したツール呼び出しを、Codex が実行する形で output に並べる。

        Codex はこの項目を見てツールを実行し、結果を次のリクエストへ function_call_output
        として載せてくる。実行するのは Codex 自身なので、ブリッジは中身に触れない。

        namespace 配下のツールは、名前を繋げず namespace を別項目として添える。繋げた名前は
        Codex が `unsupported call` として実行しない。
        """

        events = []
        for call in calls:
            item = {
                "id": f"fc_{uuid.uuid4().hex}",
                "type": "function_call",
                "call_id": call["call_id"],
                "name": call["name"],
                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                "status": "completed",
            }
            if call.get("namespace"):
                item["namespace"] = call["namespace"]
            self._items.append(item)
            events.append(
                self._event(
                    "response.output_item.done", output_index=self._next_index(), item=item
                )
            )
        return events

    def tool_search_call(self, query: str) -> list[tuple[str, dict]]:
        """遅延読み込みのツールを引き出す検索を、Codex が実行する形で output に並べる。

        tool_search は function ではなく専用のアイテム型で、実行するのは Codex 自身。
        結果は次のリクエストへ tool_search_output として載り、そこで初めてツール定義が
        会話へ入る。Claude は動かさないので、このターンに本文はない。
        """

        item = {
            "id": f"tsc_{uuid.uuid4().hex}",
            "type": "tool_search_call",
            "call_id": f"call_{uuid.uuid4().hex}",
            "status": "completed",
            "execution": "client",
            "arguments": {"query": query},
        }
        self._items.append(item)
        return [
            self._event("response.output_item.done", output_index=self._next_index(), item=item)
        ]

    def calls_response(self) -> dict:
        """本文のない完了レスポンス。output にはツール呼び出しだけが並ぶ。"""

        return self._response("completed", None)

    def completed_calls(self) -> list[tuple[str, dict]]:
        """本文を出さずにターンを閉じるイベント。"""

        return [self._event("response.completed", response=self.calls_response())]

    def completed(self, text: str) -> list[tuple[str, dict]]:
        """CLI の出力全文を 1 個の delta として送り切るイベント列。"""

        return self._message_events(self.item_id, text, "final_answer") + [
            self._event("response.completed", response=self._response("completed", text))
        ]

    def _message_events(
        self, item_id: str, text: str, phase: str
    ) -> list[tuple[str, dict]]:
        """assistant メッセージ 1 件を、追加から完了まで通しで送るイベント列。"""

        output_index = self._next_index()
        place = {"item_id": item_id, "output_index": output_index, "content_index": 0}
        return [
            self._event(
                "response.output_item.added",
                output_index=output_index,
                item=self._item(None, item_id, phase),
            ),
            self._event("response.content_part.added", **place, part=_text_part("")),
            self._event("response.output_text.delta", **place, delta=text),
            self._event("response.output_text.done", **place, text=text),
            self._event("response.content_part.done", **place, part=_text_part(text)),
            self._event(
                "response.output_item.done",
                output_index=output_index,
                item=self._item(text, item_id, phase),
            ),
        ]

    def failed(self, error: BridgeError) -> list[tuple[str, dict]]:
        """ヘッダ送出後に CLI が失敗した場合のイベント。エラー本体は JSON と同じ形。"""

        response = self._response("failed", None)
        response["error"] = error.to_payload()["error"]
        return [self._event("response.failed", response=response)]

    def _event(self, event_type: str, **fields) -> tuple[str, dict]:
        self._sequence += 1
        return event_type, {"type": event_type, "sequence_number": self._sequence, **fields}

    def _next_index(self) -> int:
        """次に使う output_index。reasoning と本文で 0 から通し番号にする。"""

        index = self._output_index
        self._output_index += 1
        return index

    def _item(
        self, text: str | None, item_id: str | None = None, phase: str = "final_answer"
    ) -> dict:
        """assistant メッセージ。text=None は本文未確定（in_progress）。"""

        return {
            "id": item_id or self.item_id,
            "type": "message",
            "role": "assistant",
            "phase": phase,
            "status": "in_progress" if text is None else "completed",
            "content": [] if text is None else [_text_part(text)],
        }

    def _response(self, status: str, text: str | None) -> dict:
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": self._items + ([] if text is None else [self._item(text)]),
            "output_text": "" if text is None else text,
        }


def _text_part(text: str) -> dict:
    return {"type": "output_text", "text": text, "annotations": []}


def _invalid(message: str, param: str | None = None) -> BridgeError:
    return BridgeError(
        message,
        status=400,
        error_type="invalid_request_error",
        code="invalid_request",
        param=param,
    )
