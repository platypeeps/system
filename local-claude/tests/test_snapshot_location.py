"""Where `claude.sh capture|status|restore` keep the MCP server record.

The record names one machine owner's servers, so it lives in
<config>/claude/mcp/ and never beside the script. These tests drive the real
script with HOME and SYSTEM_TOOLS_CONFIG pointed at a scratch directory, so
nothing reads or writes the operator's live configs or config folder.
"""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = FOLDER / "claude.sh"
LIVE_TOKEN = "ghp_" + "A1b2" * 8


class SnapshotLocation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.config = root / "config"
        self.env = dict(os.environ, HOME=str(self.home),
                        SYSTEM_TOOLS_CONFIG=str(self.config),
                        MACHINE_SETUP_PROFILE="testprofile")
        self.env.pop("GH_SNAPSHOT_TOKEN", None)

    def run_claude(self, *args):
        done = subprocess.run(["sh", str(SCRIPT), *args], capture_output=True,
                              text=True, env=self.env, cwd=self.tmp.name,
                              timeout=60)
        return done.returncode, done.stdout + done.stderr

    def write_live(self):
        live = {"mcpServers": {"demo": {
            "command": str(self.home / "bin" / "demo-mcp"),
            "env": {"GH_SNAPSHOT_TOKEN": LIVE_TOKEN}}}}
        (self.home / ".claude.json").write_text(json.dumps(live))

    def test_capture_writes_under_the_config_folder(self):
        self.write_live()
        code, output = self.run_claude("capture", "--apply")
        self.assertEqual(code, 0, output)
        snap = self.config / "claude" / "mcp" / "testprofile" / "claude-code.json"
        self.assertTrue(snap.is_file(), output)
        text = snap.read_text()
        self.assertNotIn(LIVE_TOKEN, text)
        self.assertEqual(json.loads(text)["demo"]["env"]["GH_SNAPSHOT_TOKEN"],
                         "${GH_SNAPSHOT_TOKEN}")
        self.assertEqual(json.loads(text)["demo"]["command"],
                         "${HOME}/bin/demo-mcp")
        self.assertFalse((FOLDER / "mcp").exists(),
                         "capture wrote a snapshot beside the script")

    def test_status_reads_the_config_folder(self):
        self.write_live()
        code, output = self.run_claude("status")
        self.assertEqual(code, 1, output)
        self.assertIn("no snapshot yet", output)
        self.assertEqual(self.run_claude("capture", "--apply")[0], 0)
        code, output = self.run_claude("status")
        self.assertEqual(code, 0, output)
        self.assertIn("1 server(s) match the snapshot", output)

    def test_restore_expands_from_the_config_folder(self):
        self.write_live()
        self.assertEqual(self.run_claude("capture", "--apply")[0], 0)
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {}}))
        self.env["GH_SNAPSHOT_TOKEN"] = "from-env"
        code, output = self.run_claude("restore", "--apply")
        self.assertEqual(code, 0, output)
        live = json.loads((self.home / ".claude.json").read_text())
        # Claude Code expands ${VAR} itself, so the placeholder stays.
        self.assertEqual(live["mcpServers"]["demo"]["env"]["GH_SNAPSHOT_TOKEN"],
                         "${GH_SNAPSHOT_TOKEN}")
        self.assertEqual(live["mcpServers"]["demo"]["command"],
                         str(self.home / "bin" / "demo-mcp"))


if __name__ == "__main__":
    unittest.main()
