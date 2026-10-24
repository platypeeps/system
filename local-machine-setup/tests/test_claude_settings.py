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
EXAMPLE = FOLDER.parent / "local-statusline" / "claude-hud.config.example.json"
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
        shutil.copy(EXAMPLE, self.repo / "local-statusline" / EXAMPLE.name)
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

    def test_a_fresh_baseline_blocks_github_issues_and_claude_mem_work_state(self):
        """No GitHub issues, and the tracker holds work state (rv-local-sync)."""
        self.settings.write_text("{}\n")
        self.run_tooling("--apply")

        deny = json.loads(self.settings.read_text())["permissions"]["deny"]
        for rule in ("Bash(gh issue create:*)", "Bash(gh issue transfer:*)",
                     "Bash(gh api *repos/*/issues --method POST*)", "Bash(gh api *repos/*/issues -X POST*)",
                     "mcp__github__sub_issue_write",
                     "mcp__plugin_claude-mem_mcp-search__work_state_write",
                     "mcp__plugin_claude-mem_mcp-search__work_state_read"):
            self.assertIn(rule, deny)

    def test_a_fresh_baseline_leaves_issue_updates_to_the_pack_guard(self):
        """sd:4810: a user deny beats any project allow, so the pack's sd-issue-guard gates issue_write per repo."""
        self.settings.write_text("{}\n")
        self.run_tooling("--apply")

        self.assertNotIn("mcp__github__issue_write", json.loads(self.settings.read_text())["permissions"]["deny"])

    def test_apply_drops_the_retired_issue_write_deny_and_keeps_the_operators(self):
        """A machine set up before sd:4810 carries the old rule; the baseline takes it back out."""
        old = {**OPERATOR, "permissions": {"deny": [*OPERATOR["permissions"]["deny"], "mcp__github__issue_write"]}}
        self.settings.write_text(json.dumps(old) + "\n")

        out = self.run_tooling()

        self.assertIn("STALE   settings.json permissions.deny mcp__github__issue_write (retired)", out)
        self.assertEqual(json.loads(self.settings.read_text()), old)

        self.run_tooling("--apply")

        deny = json.loads(self.settings.read_text())["permissions"]["deny"]
        self.assertNotIn("mcp__github__issue_write", deny)
        self.assertEqual(deny[:2], OPERATOR["permissions"]["deny"])
        self.assertIn("mcp__github__sub_issue_write", deny)
        [backup] = self.backups()
        self.assertEqual(json.loads(backup.read_text()), old)

        out = self.run_tooling("--apply")

        self.assertIn("ok      settings.json baseline", out)

    def test_a_fresh_baseline_runs_the_checkout_guard_before_writes(self):
        """sd:6001: agents do not write in the system and pack checkouts."""
        guard = self.folder / "checkout_guard.py"
        command = f"test ! -f {guard} || python3 {guard}"
        self.settings.write_text("{}\n")

        self.assertIn(f"MISSING settings.json hooks.PreToolUse {command}", self.run_tooling())
        self.run_tooling("--apply")

        groups = json.loads(self.settings.read_text())["hooks"]["PreToolUse"]
        self.assertEqual(groups, [{"matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
                                   "hooks": [{"type": "command", "command": command}]}])
        self.assertIn("ok      settings.json baseline", self.run_tooling("--apply"))

    def test_the_guard_command_allows_when_its_file_is_gone(self):
        """A moved or deleted checkout must not block every tool call: exit 2 would."""
        missing = self.folder / "gone/checkout_guard.py"
        result = subprocess.run(["sh", "-c", f"test ! -f {missing} || python3 {missing}"],
                                input="{}", capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)

    def test_a_fresh_baseline_turns_off_the_background_updater(self):
        """sd:3033: Claude Code updates in the weekly upgrade only."""
        self.settings.write_text("{}\n")
        self.run_tooling("--apply")

        self.assertEqual(json.loads(self.settings.read_text())["env"], {"DISABLE_AUTOUPDATER": "1"})

    def test_an_operators_updater_setting_is_left_alone(self):
        self.settings.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "0", "OTHER": "x"}}) + "\n")
        out = self.run_tooling("--apply")

        self.assertNotIn("env.DISABLE_AUTOUPDATER", out)
        self.assertEqual(json.loads(self.settings.read_text())["env"], {"DISABLE_AUTOUPDATER": "0", "OTHER": "x"})

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


class ClaudeMemWorkState(SyntheticHome):
    """sd:5995: sd is the one work-state store, so claude-mem's own is off."""

    def setUp(self):
        super().setUp()
        self.mem = self.home / ".claude-mem/settings.json"
        self.mem.parent.mkdir()
        self.mine = {"CLAUDE_MEM_WORKER_PORT": "37701", "CLAUDE_MEM_WORK_STATE_ENABLED": "true"}
        self.mem.write_text(json.dumps(self.mine, indent=2) + "\n")

    def test_a_dry_run_names_the_switch_and_writes_nothing(self):
        out = self.run_tooling()

        self.assertIn('DIFFERS {} CLAUDE_MEM_WORK_STATE_ENABLED is "true", want "false"'.format(self.mem), out)
        self.assertEqual(json.loads(self.mem.read_text()), self.mine)

    def test_apply_turns_work_state_off_and_keeps_the_other_keys(self):
        self.run_tooling("--apply")

        self.assertEqual(json.loads(self.mem.read_text()),
                         {**self.mine, "CLAUDE_MEM_WORK_STATE_ENABLED": "false"})
        self.assertEqual(len(list(self.mem.parent.glob("settings.json.bak-*"))), 1)
        self.assertIn('ok      {} CLAUDE_MEM_WORK_STATE_ENABLED = "false"'.format(self.mem), self.run_tooling())

    def test_an_unset_switch_is_missing(self):
        """claude-mem's default is on, so a file without the key is drift."""
        self.mem.write_text("{}\n")

        self.assertIn('MISSING {} CLAUDE_MEM_WORK_STATE_ENABLED = "false"'.format(self.mem), self.run_tooling())

    def test_no_claude_mem_settings_is_skipped_and_nothing_is_created(self):
        self.mem.unlink()

        out = self.run_tooling("--apply")

        self.assertIn(f"SKIP    no {self.mem} (claude-mem not set up)", out)
        self.assertFalse(self.mem.exists())


OPENCODE = """{
  // providers: see https://example.test/opencode
  "$schema": "https://opencode.ai/config.json",
  "agent": {"build": {"permission": {"edit": "ask"}}},
}
"""


class OpeningBrace(unittest.TestCase):
    """CodeQL alerts 38 and 39: the old pattern backtracked exponentially on a run of `//`."""

    def test_a_long_run_of_comment_starts_without_a_brace_fails_fast(self):
        import time
        import claude_settings

        started = time.monotonic()
        self.assertIsNone(claude_settings.OPENING_BRACE.match("//" * 5000 + "x"))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_comments_and_space_before_the_brace_are_kept(self):
        import claude_settings

        text = ' \n// a {\n/* b { */\n\t{"x": 1}'
        self.assertEqual(claude_settings.OPENING_BRACE.match(text).group(1), ' \n// a {\n/* b { */\n\t')


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
