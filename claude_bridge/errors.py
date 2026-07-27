"""ブリッジ内で共通利用するエラー型。OpenAI error 形式へそのまま変換できる。"""


class BridgeError(Exception):
    """HTTP ステータスと OpenAI error 形式の情報を持つエラー。"""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        error_type: str,
        code: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.error_type = error_type
        self.code = code
        self.param = param

    def to_payload(self) -> dict:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }
