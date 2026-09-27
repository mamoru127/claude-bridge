import shutil
import subprocess
import unittest
from pathlib import Path


TEST_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "launchd"
    / "codex-app-server-parent.test.mjs"
)


class AppServerParentTests(unittest.TestCase):
    def test_update_shutdown_regressions(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")

        result = subprocess.run(
            [node, "--test", TEST_SCRIPT],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
