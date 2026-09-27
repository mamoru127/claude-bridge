"""CLI プロセスの起動と行読みに共通する処理。

CLI ごとに違うのは引数の組み立てと出力の解釈だけで、起動失敗・タイムアウト・異常終了・
クライアント切断時の子プロセス回収は同じ扱いになる。違いはエラー本文の CLI 名と
エラーコードの接頭辞だけなので、それを `CliKind` で受け取る。
"""

import contextlib
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass

from .errors import BridgeError

STDERR_SNIPPET_BYTES = 2000


@dataclass(frozen=True)
class CliKind:
    """エラー本文に出す CLI 名と、エラーコードの接頭辞。"""

    label: str
    prefix: str

    def error(
        self,
        message: str,
        code: str,
        status: int = 502,
        error_type: str = "api_error",
    ) -> BridgeError:
        """この CLI のエラー。コードは `<接頭辞>_<code>` になる。"""

        return BridgeError(
            f"{self.label}{message}",
            status=status,
            error_type=error_type,
            code=f"{self.prefix}_{code}",
        )


class StderrReader(threading.Thread):
    """stderr を別スレッドで読み切る。読まずに溜めると子プロセスが書き込みで詰まる。"""

    def __init__(self, stream) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.data = b""
        self.start()

    def run(self) -> None:
        self.data = self.stream.read()

    def read(self) -> bytes:
        self.join()
        return self.data


def launch(
    kind: CliKind,
    command: list[str],
    cwd: str,
    runner,
    stdin=subprocess.PIPE,
    env: dict | None = None,
) -> tuple[object, StderrReader]:
    """CLI を起動する。シェルは介さないため、パス文字列は引数配列にそのまま入る。

    env=None ならブリッジの環境をそのまま引き継ぐ。渡した場合はその内容だけを子へ渡す。
    """

    try:
        # Windows の npm CLI は .cmd になるため、CreateProcess に渡す前に PATH で解決する。
        if os.name == "nt":
            command = [shutil.which(command[0]) or command[0], *command[1:]]
        process = runner(
            command,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError as error:
        raise kind.error(f"が見つかりません: {command[0]}", "not_found", status=500) from error
    except OSError as error:
        # 権限不足・実行形式不正など、プロセス起動段階の OS エラーをまとめて扱う。
        raise kind.error(
            f"を起動できません: {command[0]}: {error}", "launch_failed", status=500
        ) from error
    return process, StderrReader(process.stderr)


def write_stdin(process, data: bytes) -> None:
    """プロンプトを標準入力から渡す。

    起動直後に終了していると書き込めない。その場合は read_lines が終了コードで異常終了として
    報告するため、ここでは書けなかったことだけを受け流す。
    """

    with contextlib.suppress(BrokenPipeError):
        process.stdin.write(data)
        process.stdin.close()


def read_lines(
    kind: CliKind, process, stderr: StderrReader, timeout_seconds: float
) -> Iterator[bytes]:
    """標準出力を 1 行ずつ返す。時間切れ・異常終了・中断時の子プロセス回収も扱う。

    出力の総量にも 1 行の長さにも上限を設けない。1 行ずつ読んで捨てるため総量はメモリに
    残らず、行の長さは読み終えてからしか判定できないので上限を置いても確保は防げない。
    1 行の大きさは CLI 側の打ち切りで決まる。歯止めは timeout_seconds が担う。
    """

    # Popen には timeout がないため、時間切れで子プロセスを落として標準出力を閉じさせる。
    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        process.kill()

    timer = threading.Timer(timeout_seconds, on_timeout)
    timer.start()
    returncode = None
    try:
        yield from process.stdout
        returncode = process.wait()
    finally:
        timer.cancel()
        # エラーやクライアント切断で読み取りを抜けた場合は、子プロセスを落として回収する。
        if returncode is None:
            process.kill()
            process.wait()

    if timed_out.is_set():
        raise kind.error(
            f"が {timeout_seconds} 秒以内に終了しませんでした",
            "timeout",
            status=504,
            error_type="timeout_error",
        )
    if returncode != 0:
        raise kind.error(
            f"が異常終了しました (exit={returncode}): {snippet(stderr.read())}", "failed"
        )


def final_text(events: Iterator[tuple[str, str]]) -> str:
    """CLI の出力列を最後まで読み、確定した最終テキストだけを返す。途中経過は捨てる。"""

    text = ""
    for kind, value in events:
        if kind == "text":
            text = value
    return text


def snippet(raw: bytes | None) -> str:
    if not raw:
        return "(出力なし)"
    return text_snippet(raw[:STDERR_SNIPPET_BYTES].decode("utf-8", errors="replace"))


def text_snippet(text: object) -> str:
    if not isinstance(text, str) or not text:
        return "(出力なし)"
    return text[:STDERR_SNIPPET_BYTES]
