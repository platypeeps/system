"""Exercise prompt distribution in a copied component and isolated home."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


FOLDER = Path(__file__).resolve().parent.parent
BEGIN = b"<!-- shared-prompt:start -->"
END = b"<!-- shared-prompt:end -->"


class PromptDistribution(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.component = self.root / "component"
        shutil.copytree(FOLDER, self.component, ignore=shutil.ignore_patterns("tests"))
        self.script = self.component / "agent-prompt.sh"
        self.source = self.component / "prompt/shared.md"
        self.fixture_home = self.root / "home"
        self.state = self.root / "state"
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.fixture_home),
            "COPILOT_HOME": str(self.fixture_home / ".copilot"),
            "AGENT_PROMPT_STATE": str(self.state),
        }
        target_lines = self.run_script("targets").stdout.splitlines()
        self.targets = {
            label: Path(path) for label, path in (line.split(None, 1) for line in target_lines)
        }
        for target in self.targets.values():
            target.parent.mkdir(parents=True, exist_ok=True)

    def run_script(self, *args, expected=0):
        result = subprocess.run(
            ["/bin/sh", str(self.script), *args],
            env=self.env,
            capture_output=True,
            text=True,
            # A hang guard, not a speed check: a refresh takes under 1s alone and
            # passed 30s in a loaded gate (sd:3005, 2026-10-08).
            timeout=180,
        )
        self.assertEqual(expected, result.returncode, result.stdout + result.stderr)
        return result

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for directory in (self.fixture_home, self.state)
            if directory.exists()
            for path in directory.rglob("*")
            if path.is_file()
        }

    def move_source(self):
        self.source.write_bytes(self.source.read_bytes() + b"\n# Fixture source change\n")

    def test_current_source_reaches_every_discovered_target(self):
        self.assertIn("claude", self.targets)
        self.assertIn("codex", self.targets)
        self.run_script("refresh", "--apply")
        for label, target in self.targets.items():
            with self.subTest(target=label):
                content = target.read_bytes()
                self.assertIn(self.source.read_bytes(), content)
                self.assertEqual(1, content.count(BEGIN))
                self.assertEqual(1, content.count(END))
                self.assertTrue((self.state / (label + ".sha")).is_file())
        self.assertNotIn("STALE", self.run_script("status").stdout)

    def test_refresh_preserves_outside_marker_bytes(self):
        self.run_script("refresh", "--apply")
        before_after = {}
        for label, target in self.targets.items():
            prefix = ("# Personal " + label + "\r\n\r\n").encode()
            suffix = b"\r\n\r\nPersonal tail\r\n\t\r\n"
            target.write_bytes(prefix + target.read_bytes().rstrip(b"\n") + suffix)
            before_after[label] = (prefix, suffix)
        self.move_source()
        self.run_script("refresh", "--apply")
        for label, target in self.targets.items():
            content = target.read_bytes()
            with self.subTest(target=label):
                self.assertEqual(before_after[label][0], content.split(BEGIN, 1)[0])
                self.assertEqual(before_after[label][1], content.split(END, 1)[1])

    def test_append_preserves_existing_bytes(self):
        prefixes = (b"Personal\r\n\r\n\r\n", b"\n\n", b"Personal without newline")
        for label, prefix in zip(self.targets, prefixes):
            self.targets[label].write_bytes(prefix)
        self.run_script("refresh", "--apply")
        for label, prefix in zip(self.targets, prefixes):
            with self.subTest(target=label):
                self.assertTrue(self.targets[label].read_bytes().startswith(prefix))

    def test_initial_dry_run_creates_no_files(self):
        before = self.snapshot()
        self.assertIn("DRY RUN", self.run_script("refresh").stdout)
        self.assertEqual(before, self.snapshot())

    def test_stale_dry_run_preserves_files_and_hashes(self):
        self.run_script("refresh", "--apply")
        self.move_source()
        before = self.snapshot()
        self.assertIn("would write", self.run_script("refresh").stdout)
        self.assertEqual(before, self.snapshot())

    def test_conflicting_managed_block_refuses_without_changes(self):
        self.run_script("refresh", "--apply")
        target = self.targets["claude"]
        target.write_bytes(target.read_bytes().replace(BEGIN, BEGIN + b"\nPersonal edit", 1))
        before = self.snapshot()
        result = self.run_script("refresh", "--apply", expected=1)
        self.assertIn("DIFFERS", result.stdout)
        self.assertEqual(before, self.snapshot())

    def test_unknown_managed_block_refuses_without_changes(self):
        target = self.targets["claude"]
        target.write_bytes(BEGIN + b"\nUnrecorded personal block\n" + END + b"\n")
        before = self.snapshot()
        result = self.run_script("refresh", "--apply", expected=1)
        self.assertIn("UNKNOWN", result.stdout)
        self.assertEqual(before, self.snapshot())

    def test_later_conflicts_report_all_without_writing_any_target(self):
        self.run_script("refresh", "--apply")
        self.move_source()
        for label in ("codex", "copilot"):
            target = self.targets[label]
            target.write_bytes(target.read_bytes().replace(BEGIN, BEGIN + b"\nPersonal edit", 1))
        before = self.snapshot()
        for args in (("refresh",), ("refresh", "--apply")):
            with self.subTest(args=args):
                result = self.run_script(*args, expected=1)
                self.assertIn("DIFFERS codex", result.stdout)
                self.assertIn("DIFFERS copilot", result.stdout)
                self.assertEqual(before, self.snapshot())

    def test_later_unknown_block_refuses_before_any_write(self):
        self.run_script("refresh", "--apply")
        self.move_source()
        target = self.targets["opencode"]
        target.write_bytes(BEGIN + b"\nUnrecorded personal block\n" + END + b"\n")
        (self.state / "opencode.sha").unlink()
        before = self.snapshot()
        self.assertIn("UNKNOWN opencode", self.run_script("refresh", "--apply", expected=1).stdout)
        self.assertEqual(before, self.snapshot())

    def test_refresh_allows_non_utf8_unmanaged_bytes(self):
        self.run_script("refresh", "--apply")
        target = self.targets["codex"]
        prefix, suffix = b"Personal prefix: \xff\r\n", b"\r\nPersonal suffix: \xfe\r\n"
        target.write_bytes(prefix + target.read_bytes().rstrip(b"\n") + suffix)
        self.move_source()
        self.assertEqual("", self.run_script("refresh", "--apply").stderr)
        content = target.read_bytes()
        self.assertEqual(prefix, content.split(BEGIN, 1)[0])
        self.assertEqual(suffix, content.split(END, 1)[1])
        self.assertEqual("", self.run_script("status").stderr)
        self.assertEqual("", self.run_script("diff").stderr)

    def test_non_utf8_append_records_real_hash_and_preserves_later_edit(self):
        target = self.targets["claude"]
        prefix = b"Personal legacy bytes: \xff\x00\r\n"
        target.write_bytes(prefix)
        self.assertEqual("", self.run_script("refresh", "--apply").stderr)
        content = target.read_bytes()
        self.assertTrue(content.startswith(prefix))
        managed = content[content.index(BEGIN):content.index(END) + len(END)]
        self.assertEqual(hashlib.sha256(managed).hexdigest(), (self.state / "claude.sha").read_text().strip())
        target.write_bytes(content.replace(BEGIN, BEGIN + b"\nPersonal edit", 1))
        before = self.snapshot()
        self.assertIn("DIFFERS claude", self.run_script("refresh", "--apply", expected=1).stdout)
        self.assertEqual(before, self.snapshot())

    def test_managed_crlf_is_an_edit_not_current_or_stale(self):
        self.run_script("refresh", "--apply")
        target = self.targets["codex"]
        target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
        self.assertIn("DIFFERS codex", self.run_script("status").stdout)
        self.move_source()
        before = self.snapshot()
        self.assertIn("DIFFERS codex", self.run_script("refresh", "--apply", expected=1).stdout)
        self.assertEqual(before, self.snapshot())

    def test_force_replaces_managed_edit_and_preserves_non_utf8_outside(self):
        self.run_script("refresh", "--apply")
        target = self.targets["codex"]
        prefix, suffix = b"Personal \xff\r\n", b"\r\nPersonal \xfe\r\n"
        edited = target.read_bytes().rstrip(b"\n").replace(BEGIN, BEGIN + b"\nPersonal edit", 1)
        target.write_bytes(prefix + edited + suffix)
        self.assertEqual("", self.run_script("refresh", "--apply", "--force").stderr)
        content = target.read_bytes()
        self.assertNotIn(b"Personal edit", content)
        self.assertEqual(prefix, content.split(BEGIN, 1)[0])
        self.assertEqual(suffix, content.split(END, 1)[1])

    def test_malformed_block_does_not_write_targets_or_hashes(self):
        self.run_script("refresh", "--apply")
        self.move_source()
        target = self.targets["codex"]
        target.write_bytes(target.read_bytes().replace(END, b"Missing end marker"))
        before = self.snapshot()
        result = self.run_script("refresh", "--apply", expected=1)
        self.assertIn("managed block", result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_target_read_failure_refuses_before_writing_or_recording_hashes(self):
        self.run_script("refresh", "--apply")
        self.move_source()
        target = self.targets["codex"]
        target.unlink()
        target.mkdir()
        before = self.snapshot()
        result = self.run_script("refresh", "--apply", expected=1)
        self.assertIn(str(target), result.stderr)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(target.is_dir())

    def test_capture_preserves_non_utf8_outside_and_records_managed_bytes(self):
        self.run_script("refresh", "--apply")
        target = self.targets["claude"]
        content = b"Personal \xff\r\n" + target.read_bytes()
        content = content.replace(END, b"# Captured personal policy\n" + END)
        target.write_bytes(content)
        before = self.snapshot()
        original_source = self.source.read_bytes()
        self.run_script("capture", "claude")
        self.assertEqual(original_source, self.source.read_bytes())
        self.assertEqual(before, self.snapshot())
        self.assertEqual("", self.run_script("capture", "claude", "--apply").stderr)
        self.assertEqual(content, target.read_bytes())
        self.assertIn(b"# Captured personal policy\n", self.source.read_bytes())
        self.assertNotIn("DIFFERS claude", self.run_script("status").stdout)

    def test_malformed_capture_does_not_empty_source_or_hash_record(self):
        self.run_script("refresh", "--apply")
        target = self.targets["claude"]
        target.write_bytes(target.read_bytes().replace(END, b"Missing end marker"))
        before = self.snapshot()
        original_source = self.source.read_bytes()
        self.run_script("capture", "claude", "--apply", expected=1)
        self.assertEqual(original_source, self.source.read_bytes())
        self.assertEqual(before, self.snapshot())

    def test_status_flags_each_claude_rule_file_the_block_replaces(self):
        """sd:3031: shared.md holds the user rules, so a ~/.claude/rules file is drift."""
        self.run_script("refresh", "--apply")
        self.assertNotIn("rules/", self.run_script("status").stdout)
        rules = self.targets["claude"].parent / "rules"
        rules.mkdir()
        (rules / "merge-lane.md").write_text("# Merge lane (integrator sessions)\n")
        (rules / "notes.txt").write_text("not a rule file\n")

        out = self.run_script("status").stdout

        self.assertIn(f"DIFFERS {rules / 'merge-lane.md'} — shared.md holds the user rules", out)
        self.assertNotIn("notes.txt", out)
        self.assertEqual("# Merge lane (integrator sessions)\n", (rules / "merge-lane.md").read_text())


class SharedPolicy(unittest.TestCase):
    def test_defaults_are_self_contained(self):
        source = (FOLDER / "prompt/shared.md").read_text()
        self.assertIn("STE-Concise", source)
        self.assertIn("20 words", source)
        self.assertIn("active voice", source)
        self.assertIn("Archify", source)
        self.assertNotIn("output-styles/", source)
        self.assertNotIn("**any private repo**", source)


if __name__ == "__main__":
    unittest.main()
