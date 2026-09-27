"""Exercise the real Windows URL association used for Remote threads."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from claude_bridge.mobile_browser_patch import MobileThreadWatcher

if sys.platform == "win32":
    import winreg


THREAD_ID = "01a02c76-328c-7c21-b5e4-b7466f62f05d"
REGISTRY_PATH = r"Software\Classes\codex"


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class WindowsUrlHandlerTest(unittest.TestCase):
    def test_remote_thread_opens_registered_url_handler(self) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH):
                self.skipTest("Existing Codex URL association must not be replaced")
        except FileNotFoundError:
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "opened.txt"
            handler = Path(__file__).with_name("windows_url_handler.py")
            command = f'"{sys.executable}" "{handler}" "%1" "{marker}"'
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
                winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "URL:Codex test")
                winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, REGISTRY_PATH + r"\shell\open\command"
            ) as key:
                winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)
            try:
                sessions = root / "sessions"
                watcher = MobileThreadWatcher(sessions)
                day_directory = watcher._day_directory()
                day_directory.mkdir(parents=True)
                watcher.prime()
                (day_directory / "rollout-mobile.jsonl").write_text(
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "id": THREAD_ID,
                                "originator": "codex_chatgpt_ios_remote",
                            },
                        }
                    ) + "\n",
                    encoding="utf-8",
                )
                watcher.scan()
                for _ in range(100):
                    if marker.exists():
                        break
                    time.sleep(0.1)
                self.assertEqual(marker.read_text(encoding="utf-8"), f"codex://threads/{THREAD_ID}")
            finally:
                for suffix in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH + suffix)


if __name__ == "__main__":
    unittest.main()
