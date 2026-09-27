"""Open new mobile Remote threads in ChatGPT so they get an in-app Browser route."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID

from .thread_preview_patch import ThreadPreviewWriter


REMOTE_ORIGINATORS = frozenset(
    {
        "codex_chatgpt_android_remote",
        "codex_chatgpt_ios_remote",
    }
)
POLL_INTERVAL_SECONDS = 0.1
STARTUP_GRACE_SECONDS = 30
PREVIEW_INTERVAL_SECONDS = 1


def inspect_session(path: Path) -> tuple[bool, str | None]:
    """Return (ready, remote thread id) from a rollout's first event."""
    try:
        with path.open(encoding="utf-8") as session_file:
            first_line = session_file.readline()
    except FileNotFoundError:
        return False, None

    if not first_line:
        return False, None

    try:
        event = json.loads(first_line)
    except json.JSONDecodeError:
        return False, None

    if event.get("type") != "session_meta":
        return True, None

    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("originator") not in REMOTE_ORIGINATORS:
        return True, None

    thread_id = payload.get("id")
    if not isinstance(thread_id, str):
        return True, None
    try:
        UUID(thread_id)
    except ValueError:
        return True, None
    return True, thread_id


def open_in_chatgpt(
    thread_id: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    run(
        ["/usr/bin/open", "-g", f"codex://threads/{thread_id}"],
        check=True,
        timeout=5,
    )


class MobileThreadWatcher:
    def __init__(
        self,
        sessions_root: Path,
        *,
        opener: Callable[[str], None] = open_in_chatgpt,
    ) -> None:
        self.sessions_root = sessions_root
        self.opener = opener
        self.known_paths: set[Path] = set()
        self.day: str | None = None

    def _day_directory(self) -> Path:
        now = datetime.now().astimezone()
        return self.sessions_root / now.strftime("%Y/%m/%d")

    def prime(self) -> None:
        day_directory = self._day_directory()
        self.day = str(day_directory)
        cutoff = time.time() - STARTUP_GRACE_SECONDS
        self.known_paths = {
            path
            for path in day_directory.glob("rollout-*.jsonl")
            if path.stat().st_mtime < cutoff
        }

    def scan(self) -> None:
        day_directory = self._day_directory()
        if str(day_directory) != self.day:
            self.day = str(day_directory)
            self.known_paths.clear()

        new_paths = set(day_directory.glob("rollout-*.jsonl")) - self.known_paths
        for path in sorted(new_paths):
            ready, thread_id = inspect_session(path)
            if not ready:
                continue
            self.known_paths.add(path)
            if thread_id is None:
                continue
            try:
                self.opener(thread_id)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                self.known_paths.remove(path)
                print(f"failed to open mobile thread {thread_id}: {error}", flush=True)
                continue
            print(f"opened mobile thread in ChatGPT: {thread_id}", flush=True)

    def run_forever(self, preview_writer: ThreadPreviewWriter) -> None:
        self.prime()
        next_preview_scan = 0.0
        while True:
            self.scan()
            now = time.monotonic()
            if now >= next_preview_scan:
                for thread_id in preview_writer.scan():
                    print(f"saved created task preview: {thread_id}", flush=True)
                next_preview_scan = now + PREVIEW_INTERVAL_SECONDS
            time.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    codex_home = Path.home() / ".codex"
    preview_writer = ThreadPreviewWriter(codex_home)
    try:
        MobileThreadWatcher(codex_home / "sessions").run_forever(preview_writer)
    finally:
        preview_writer.close()


if __name__ == "__main__":
    main()
