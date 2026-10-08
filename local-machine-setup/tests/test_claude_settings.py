"""The tooling stage merges the settings baseline into Claude Code and opencode.

It adds missing entries only, keeps the operator's own, backs the file up
first, does nothing on a dry run, and a second run changes nothing. The
stage runs from a copy of this folder against a synthetic home.
"""

import json
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
OPERATOR = {"permissions": {"deny": ["Bash(aws iam:*)", "Read(~/.ssh/**)"]},
            "attribution": {"commit": "mine"},
            "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "own"}]}]}}


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class SyntheticHome(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = pathlib.Path(tmp.name)
        self.repo = base / "repo"
        self.folder = self.repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, self.repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        write_exec(self.repo / "local-statusline/statusline.sh", "#!/bin/sh\nexit 0\n")
        self.home = base / "home"
        self.claude = self.home / ".claude"
        (self.claude / "plugins/cache/claude-hud/claude-hud/0.10.0").mkdir(parents=True)
        self.settings = self.claude / "settings.json"
        self.settings.write_text(json.dumps(OPERATOR) + "\n")
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.stubs = base / "stubs"
        write_exec(self.stubs / "claude", "#!/bin/sh\nexit 0\n")
        fixture_config.seal(self, self.stubs)
        self.cwd = tmp.name

    def run_tooling(self, *flags):
        env = {"HOME": str(self.home), "MACHINE_SETUP_STATE": str(self.state),
               "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
               **fixture_config.env(), "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX}
        result = subprocess.run([str(self.folder / "machine-setup.sh"), "update", "tooling", *flags],
                                env=env, capture_output=True, text=True, cwd=self.cwd,
                                stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout



class ClaudeSettingsBaseline(SyntheticHome):
    def backups(self):
        return sorted(self.claude.glob("settings.json.bak-*"))

    def test_a_dry_run_names_each_missing_entry_and_writes_nothing(self):
        out = self.run_tooling()

        self.assertIn("MISSING settings.json permissions.deny Read(//**/.env)", out)
        self.assertIn("MISSING settings.json attribution.pr", out)
        self.assertIn("MISSING settings.json includeCoAuthoredBy", out)
        self.assertIn("MISSING settings.json hooks.SessionStart sd today 2>/dev/null | head -40", out)
        self.assertNotIn("permissions.deny Read(~/.ssh/**)", out)
        self.assertEqual(json.loads(self.settings.read_text()), OPERATOR)
        self.assertEqual(self.backups(), [])

    def test_apply_adds_the_baseline_and_keeps_the_operators_entries(self):
        self.run_tooling("--apply")

        merged = json.loads(self.settings.read_text())
        deny = merged["permissions"]["deny"]
        self.assertEqual(deny[:2], OPERATOR["permissions"]["deny"])
        self.assertIn("Read(~/.aws/credentials)", deny)
        self.assertEqual(deny.count("Read(~/.ssh/**)"), 1)
        self.assertEqual(merged["attribution"], {"commit": "mine", "pr": ""})
        self.assertIs(merged["includeCoAuthoredBy"], False)
        groups = merged["hooks"]["SessionStart"]
        self.assertEqual(groups[0], OPERATOR["hooks"]["SessionStart"][0])
        self.assertEqual(groups[1], {"hooks": [{"type": "command", "command": "sd today 2>/dev/null | head -40"}]})
        [backup] = self.backups()
        self.assertEqual(json.loads(backup.read_text()), OPERATOR)

    def test_a_second_apply_changes_nothing(self):
        self.run_tooling("--apply")
        after_first = self.settings.read_bytes()

        out = self.run_tooling("--apply")

        self.assertIn("ok      settings.json baseline", out)
        self.assertNotIn("MISSING settings.json", out)
        self.assertEqual(self.settings.read_bytes(), after_first)
        self.assertEqual(len(self.backups()), 1)

    def test_invalid_json_is_reported_and_left_alone(self):
        self.settings.write_text("{not json\n")

        out = self.run_tooling("--apply")

        self.assertIn("DIFFERS settings.json is not valid JSON", out)
        self.assertEqual(self.settings.read_text(), "{not json\n")


OPENCODE = """{
  // providers: see https://example.test/opencode
  "$schema": "https://opencode.ai/config.json",
  "agent": {"build": {"permission": {"edit": "ask"}}},
}
"""


class OpencodeReadDeny(SyntheticHome):
    def setUp(self):
        super().setUp()
        write_exec(self.stubs / "opencode", "#!/bin/sh\nexit 0\n")
        self.config = self.home / ".config/opencode/opencode.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(OPENCODE)

    def backups(self):
        return sorted(self.config.parent.glob("opencode.json.bak-*"))

    def test_a_dry_run_names_each_missing_deny_and_writes_nothing(self):
        out = self.run_tooling()

        self.assertIn("MISSING opencode.json permission.read *.env", out)
        self.assertIn("MISSING opencode.json permission.read *.ssh/*", out)
        self.assertEqual(self.config.read_text(), OPENCODE)
        self.assertEqual(self.backups(), [])

    def test_apply_adds_the_denies_and_keeps_comments_and_entries(self):
        self.run_tooling("--apply")

        text = self.config.read_text()
        self.assertIn("// providers: see https://example.test/opencode", text)
        self.assertTrue(text.endswith(OPENCODE[1:]))
        self.assertIn('"*secrets/*": "deny"', text)
        self.assertIn('"*.config/gh/hosts.yml": "deny"', text)
        [backup] = self.backups()
        self.assertEqual(backup.read_text(), OPENCODE)

        out = self.run_tooling("--apply")

        self.assertIn("ok      opencode.json read denies", out)
        self.assertEqual(self.config.read_text(), text)
        self.assertEqual(len(self.backups()), 1)

    def test_an_own_permission_block_is_reported_and_left_alone(self):
        own = '{"permission": {"read": {"*.env": "ask"}}}\n'
        self.config.write_text(own)

        out = self.run_tooling("--apply")

        self.assertIn("DIFFERS opencode.json has its own permission block", out)
        self.assertEqual(self.config.read_text(), own)


if __name__ == "__main__":
    unittest.main()
