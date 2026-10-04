import importlib.util
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "launchd/install-app-connection.py"
SPEC = importlib.util.spec_from_file_location("app_connection_install", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class AppConnectionInstallTests(unittest.TestCase):
    def test_persists_same_endpoint_for_current_session_and_login_without_server_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            installer.subprocess, "run"
        ) as run:
            root = Path(directory)
            codex_home = root / "custom-codex"
            first = installer.install_connection(root, codex_home, 123)
            second = installer.install_connection(root, codex_home, 123)
            self.assertEqual(first, second)
            self.assertEqual(
                first, f"ws+unix://localhost{codex_home}/app-server-control/app-server-control.sock:/rpc"
            )
            path = root / "Library/LaunchAgents" / f"{installer.LABEL}.plist"
            job = plistlib.loads(path.read_bytes())
            self.assertTrue(job["RunAtLoad"])
            self.assertEqual(job["ProgramArguments"], run.call_args_list[0].args[0])
            self.assertEqual(job["ProgramArguments"][-1], first)
            self.assertEqual(len(list(path.parent.glob("*.plist"))), 1)
            for call in run.call_args_list:
                args = call.args[0]
                self.assertNotIn("kickstart", args)
                if args[1] == "bootout":
                    self.assertEqual(args[2], f"gui/123/{installer.LABEL}")

    def test_invalid_socket_url_path_does_not_modify_environment_or_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            installer.subprocess, "run"
        ) as run:
            root = Path(directory)
            for codex_home in [Path("relative"), root / "space here", root / "colon:here"]:
                with self.subTest(codex_home=codex_home), self.assertRaises(ValueError):
                    installer.install_connection(root, codex_home, 123)
            run.assert_not_called()
            self.assertFalse((root / "Library").exists())

    def test_launchctl_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            installer.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "launchctl")
        ):
            root = Path(directory)
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install_connection(root, root / ".codex", 123)
