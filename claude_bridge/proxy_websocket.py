"""Codex CLI の proxy を介して Windows から制御ソケットへ接続する。"""

import queue
import shutil
import subprocess
import threading

from .uds_websocket import UdsWebSocket


class ProxySocket:
    """WebSocket のバイト列を Codex CLI の stdio proxy へ渡す。"""

    def __init__(self, codex: str) -> None:
        self.codex = codex
        self.process = None
        self.timeout = None
        self.chunks = queue.Queue()
        self.pending = b""

    def connect(self, socket_path: str) -> None:
        self.process = subprocess.Popen(
            [self.codex, "app-server", "proxy", "--sock", socket_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            while chunk := self.process.stdout.read1(65536):
                self.chunks.put(chunk)
        finally:
            self.chunks.put(b"")

    def settimeout(self, value) -> None:
        self.timeout = value

    def sendall(self, data: bytes) -> None:
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except ValueError as error:
            raise OSError("Codex proxy が終了しました") from error

    def recv(self, size: int) -> bytes:
        if self.pending:
            chunk, self.pending = self.pending, b""
        else:
            try:
                chunk = self.chunks.get(timeout=self.timeout)
            except queue.Empty as error:
                raise TimeoutError("Codex proxy が応答しません") from error
        if len(chunk) > size:
            chunk, self.pending = chunk[:size], chunk[size:]
        return chunk

    def shutdown(self, _how) -> None:
        if self.process is not None and not self.process.stdin.closed:
            self.process.stdin.close()

    def close(self) -> None:
        if self.process is None:
            return
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()


def ProxyWebSocket(socket_path: str) -> UdsWebSocket:
    codex = shutil.which("codex")
    if not codex:
        raise OSError("Codex CLI が見つかりません")
    return UdsWebSocket(socket_path, transport=ProxySocket(codex))
