import importlib.util
import plistlib
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "launchd/merge-plist-env.py"
SPEC = importlib.util.spec_from_file_location("merge_plist_env", SCRIPT)
merger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merger)


class PlistEnvMergeTests(unittest.TestCase):
    def test_adds_local_settings_and_overrides_template_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plist_path = root / "job.plist"
            plist_path.write_bytes(plistlib.dumps({
                "Label": "local.claude-bridge",
                "EnvironmentVariables": {"HOME": "/Users/x", "CLAUDE_CODE_EFFORT_LEVEL": "high"},
            }))
            env_path = root / "bridge.env"
            env_path.write_text(
                "# このマシンだけの設定\n"
                "\n"
                "CLAUDE_BRIDGE_ADD_DIRS=/Users/x/a:/Users/x/b c\n"
                "CLAUDE_CODE_EFFORT_LEVEL = \"max\"\n",
                encoding="utf-8",
            )
            merger.merge(plist_path, env_path)
            env = plistlib.loads(plist_path.read_bytes())["EnvironmentVariables"]
            self.assertEqual(env, {
                "HOME": "/Users/x",
                "CLAUDE_CODE_EFFORT_LEVEL": "max",
                "CLAUDE_BRIDGE_ADD_DIRS": "/Users/x/a:/Users/x/b c",
            })

    def test_rejects_lines_that_are_not_assignments(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "bridge.env"
            env_path.write_text("export CLAUDE_BRIDGE_PORT 8787\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "bridge.env:1"):
                merger.read_env(env_path)


if __name__ == "__main__":
    unittest.main()
