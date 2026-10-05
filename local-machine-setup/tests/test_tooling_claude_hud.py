"""The tooling stage sets up the Claude Code HUD on every profile.

The HUD is two pieces: the claude-hud plugin, and a statusLine in
~/.claude/settings.json that runs local-statusline. A machine missing either
is drift, and `update tooling --apply` installs what is missing.

The script runs from a copy of this folder under a temporary root. `claude`
is a stub on PATH that logs its arguments, and local-statusline/statusline.sh
is a stub that logs its verb, so nothing reaches the operator's ~/.claude.
"""

import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

CLAUDE_STUB = r"""#!/bin/sh
echo "$*" >> "$CLAUDE_LOG"
exit 0
"""

STATUSLINE_STUB = r"""#!/bin/sh
echo "$*" >> "$STATUSLINE_LOG"
exit 0
"""

MARKETPLACE_ADD = "plugin marketplace add jarrodwatts/claude-hud"
PLUGIN_INSTALL = "plugin install claude-hud@claude-hud"


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class ToolingClaudeHudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.repo = base / "repo"
        self.folder = self.repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, self.repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        write_exec(self.repo / "local-statusline/statusline.sh", STATUSLINE_STUB)
        self.home = base / "home"
        self.claude_dir = self.home / ".claude"
        self.claude_dir.mkdir(parents=True)
        self.settings = self.claude_dir / "settings.json"
        self.settings.write_text("{}\n")
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.claude_log = base / "claude.log"
        self.statusline_log = base / "statusline.log"
        self.stubs = base / "stubs"
        write_exec(self.stubs / "claude", CLAUDE_STUB)
        fixture_config.seal(self, self.stubs)

    def run_tooling(self, *flags):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
            "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
            "CLAUDE_LOG": str(self.claude_log),
            "STATUSLINE_LOG": str(self.statusline_log),
        }
        result = subprocess.run([str(self.folder / "machine-setup.sh"), "update", "tooling", *flags],
                                env=env, capture_output=True, text=True, cwd=self.tmp.name,
                                stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def calls(self, log):
        return log.read_text().splitlines() if log.exists() else []

    def install_plugin(self):
        (self.claude_dir / "plugins/cache/claude-hud/claude-hud/0.10.0").mkdir(parents=True)
        (self.claude_dir / "plugins/marketplaces/claude-hud").mkdir(parents=True)

    def wire_statusline(self):
        command = f"sh {self.repo}/local-statusline/statusline.sh render"
        self.settings.write_text('{"statusLine": {"type": "command", "command": "%s"}}\n' % command)

    def test_a_dry_run_names_both_missing_pieces_and_changes_nothing(self):
        out = self.run_tooling()

        self.assertIn("MISSING claude-hud plugin", out)
        self.assertIn("MISSING statusLine -> local-statusline", out)
        self.assertEqual(self.calls(self.claude_log), [])
        self.assertEqual(self.calls(self.statusline_log), [])

    def test_apply_installs_the_plugin_and_points_the_status_line(self):
        self.run_tooling("--apply")

        self.assertEqual(self.calls(self.claude_log), [MARKETPLACE_ADD, PLUGIN_INSTALL])
        self.assertEqual(self.calls(self.statusline_log), ["install"])

    def test_a_machine_with_both_pieces_reports_ok_and_runs_nothing(self):
        self.install_plugin()
        self.wire_statusline()

        out = self.run_tooling("--apply")

        self.assertIn("ok      claude-hud plugin", out)
        self.assertIn("ok      statusLine -> local-statusline", out)
        self.assertNotIn("MISSING", out)
        self.assertEqual(self.calls(self.claude_log), [])
        self.assertEqual(self.calls(self.statusline_log), [])

    def test_a_known_marketplace_is_not_added_again(self):
        (self.claude_dir / "plugins/marketplaces/claude-hud").mkdir(parents=True)

        self.run_tooling("--apply")

        self.assertEqual(self.calls(self.claude_log), [PLUGIN_INSTALL])

    def test_a_status_line_pointing_elsewhere_is_missing(self):
        self.install_plugin()
        self.settings.write_text('{"statusLine": {"type": "command", "command": "other"}}\n')

        out = self.run_tooling()

        self.assertIn("ok      claude-hud plugin", out)
        self.assertIn("MISSING statusLine -> local-statusline", out)

    def test_a_machine_without_claude_skips_the_hud(self):
        (self.stubs / "claude").unlink()

        out = self.run_tooling("--apply")

        self.assertIn("SKIP    claude not installed", out)
        self.assertNotIn("MISSING", out)
        self.assertEqual(self.calls(self.statusline_log), [])

    def test_no_settings_file_skips_the_status_line(self):
        self.install_plugin()
        self.settings.unlink()

        out = self.run_tooling("--apply")

        self.assertIn("SKIP    statusLine", out)
        self.assertEqual(self.calls(self.statusline_log), [])


if __name__ == "__main__":
    unittest.main()
