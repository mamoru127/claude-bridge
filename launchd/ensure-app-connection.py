#!/usr/bin/env python3
"""Launch ChatGPT with its shared connection, including after login restoration."""

import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


APP = Path("/Applications/ChatGPT.app")
VARIABLE = "CODEX_APP_SERVER_WS_URL"
TERMINAL_EVENTS = {"task_complete", "turn_aborted"}


def processes():
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,comm="], check=True,
        capture_output=True, text=True, timeout=10,
    )
    return [line.strip().split(None, 2) for line in result.stdout.splitlines() if line.strip()]


def app_pid(rows):
    executable = str(APP / "Contents/MacOS/ChatGPT")
    return next((int(pid) for pid, _, command in rows if command == executable), None)


def has_connection(pid, endpoint):
    result = subprocess.run(
        ["ps", "eww", "-p", str(pid), "-o", "command="], check=True,
        capture_output=True, text=True, timeout=10,
    )
    return f"{VARIABLE}={endpoint}" in result.stdout.split()


def busy_threads(rows, pid, codex_home):
    children = {
        child for child, parent, command in rows
        if int(parent) == pid and Path(command).name == "codex"
    }
    if not children:
        return []
    result = subprocess.run(
        ["lsof", "-Fn", "-a", "-p", ",".join(sorted(children)),
         "+D", str(codex_home / "thread-writer-locks")],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise RuntimeError("Could not inspect app-server writer locks")
    thread_ids = {
        Path(line[1:]).stem for line in result.stdout.splitlines()
        if line.startswith("n") and line.endswith(".lock")
    }
    busy = []
    with closing(sqlite3.connect(f"file:{codex_home / 'state_5.sqlite'}?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only = ON")
        for thread_id in sorted(thread_ids):
            row = db.execute("SELECT rollout_path FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"No history for locked thread {thread_id}")
            state = None
            with Path(row[0]).open() as history:
                for line in history:
                    # A writer can be appending the last JSONL record right now.
                    if not line.endswith("\n"):
                        state = "task_started"
                        break
                    item = json.loads(line)
                    payload = item.get("payload", {})
                    kind = payload.get("type")
                    if item.get("type") == "event_msg" and kind in TERMINAL_EVENTS | {"task_started"}:
                        state = kind
            if state not in TERMINAL_EVENTS:
                busy.append(thread_id)
    return busy


def verify_shared_transport(started_at):
    threshold = datetime.fromtimestamp(started_at, timezone.utc).isoformat(timespec="milliseconds")[:23]
    log_root = Path.home() / "Library/Logs/com.openai.codex"
    for _ in range(60):
        logs = sorted(log_root.rglob("*-t0-*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in logs[:3]:
            with path.open("rb") as log:
                log.seek(max(0, path.stat().st_size - 65536))
                lines = log.read().decode(errors="replace").splitlines()
            if any(line[:23] >= threshold and
                   "Starting app-server connection hostId=local transport=websocket" in line
                   for line in lines):
                logging.info("Verified the app's local transport is the shared WebSocket")
                return
        time.sleep(1)
    raise RuntimeError("The app did not confirm a shared WebSocket connection in its startup log")


def ensure_connection(endpoint, codex_home):
    # Manual launches later in this login session must use the same endpoint too.
    subprocess.run(["/bin/launchctl", "setenv", VARIABLE, endpoint], check=True, timeout=10)
    codex = APP / "Contents/Resources/codex-cli/bin/codex"
    for _ in range(60):
        result = subprocess.run(
            [str(codex), "app-server", "daemon", "version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            break
        time.sleep(2)
    else:
        raise RuntimeError("Shared app-server did not become ready")

    previous_busy = None
    while True:
        rows = processes()
        pid = app_pid(rows)
        if pid is None:
            break
        if has_connection(pid, endpoint):
            logging.info("ChatGPT already uses the shared connection (pid=%s)", pid)
            return
        busy = busy_threads(rows, pid, codex_home)
        if busy:
            if busy != previous_busy:
                logging.info("Waiting for active tasks before restarting ChatGPT: %s", ", ".join(busy))
                previous_busy = busy
            time.sleep(5)
            continue
        # Recheck the process identity and task state immediately before signalling.
        rows = processes()
        if app_pid(rows) != pid or busy_threads(rows, pid, codex_home):
            continue
        logging.info("Restarting idle ChatGPT with the explicit shared connection (pid=%s)", pid)
        os.kill(pid, signal.SIGTERM)
        for _ in range(60):
            if app_pid(processes()) != pid:
                break
            time.sleep(1)
        else:
            raise RuntimeError("ChatGPT did not exit; no forced termination was attempted")
        break

    started_at = time.time()
    subprocess.run(
        ["/usr/bin/open", "-g", "-a", str(APP), "--env", f"{VARIABLE}={endpoint}"],
        check=True, timeout=15,
    )
    for _ in range(60):
        pid = app_pid(processes())
        if pid is not None and has_connection(pid, endpoint):
            logging.info("Verified ChatGPT launched with the shared connection (pid=%s)", pid)
            verify_shared_transport(started_at)
            return
        time.sleep(1)
    raise RuntimeError("ChatGPT did not launch with the requested shared connection")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ensure_connection(args.endpoint, args.codex_home)
