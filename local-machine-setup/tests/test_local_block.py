"""The repos stage keeps the shared CLAUDE.local.md keys in every auto repo (sd:3030).

SHARED_LOCAL_KEYS in claude_settings.py is the one source. The stage writes
those keys into the SD-AI-COMMAND-PACK:LOCAL block of each auto repo checked
out here, and keeps the repo's own keys, its comments and every line outside
the block. A manual repo, a repo not checked out and a file git does not ignore
are never written. `sd-db.sh repo list` is a stub; the repos are fixtures.
"""

import pathlib
import subprocess

from tests import fixture_config
from tests import test_claude_settings as base

START = "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->"
END = "<!-- SD-AI-COMMAND-PACK:LOCAL:END -->"
OWN = f"""# Notes for this repo, outside the block.
{START}
# mode: full
mode: minimal
{END}
Free text after the block.
"""


class LocalBlock(base.SyntheticHome):
    def setUp(self):
        super().setUp()
        import claude_settings

        self.keys = claude_settings.SHARED_LOCAL_KEYS
        self.rows = []
        self.auto = self.checkout(self.home / "repos/auto-one")
        self.add_row("~/repos/auto-one", "auto")

    def checkout(self, path, ignored=True):
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True, stdin=subprocess.DEVNULL)
        if ignored:
            (path / ".gitignore").write_text("CLAUDE.local.md\n")
        return path

    def add_row(self, path, merge):
        self.rows.append(f"sd-db: {path}  https://example.test/x.git  file  yes  local  off  {merge}")
        base.write_exec(self.repo / "local-sd-db/sd-db.sh",
                        "#!/bin/sh\ncat <<'EOF'\n" + "\n".join(self.rows) + "\nEOF\n")

    def run_repos(self, *flags):
        env = {"HOME": str(self.home), "MACHINE_SETUP_STATE": str(self.state),
               "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
               **fixture_config.env(), "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX}
        result = subprocess.run([str(self.folder / "machine-setup.sh"), "update", "repos", *flags],
                                env=env, capture_output=True, text=True, cwd=self.cwd,
                                stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def shared_lines(self):
        return [f'{key}: "{value}"' for key, value in sorted(self.keys.items())]

    def test_a_dry_run_names_each_missing_key_and_writes_nothing(self):
        (self.auto / "CLAUDE.local.md").write_text(OWN)

        out = self.run_repos()

        for key in self.keys:
            self.assertIn(f"MISSING ~/repos/auto-one/CLAUDE.local.md {key}", out)
        self.assertEqual((self.auto / "CLAUDE.local.md").read_text(), OWN)

    def test_apply_adds_the_shared_keys_and_keeps_everything_else(self):
        (self.auto / "CLAUDE.local.md").write_text(OWN)

        self.run_repos("--apply")

        want = OWN.replace(f"mode: minimal\n{END}", "mode: minimal\n" + "\n".join(self.shared_lines()) + f"\n{END}")
        self.assertEqual((self.auto / "CLAUDE.local.md").read_text(), want)
        out = self.run_repos("--apply")
        self.assertIn("ok      ~/repos/auto-one/CLAUDE.local.md shared keys", out)
        self.assertEqual((self.auto / "CLAUDE.local.md").read_text(), want)

    def test_a_shared_key_that_moved_is_stale_and_the_source_wins(self):
        key = sorted(self.keys)[0]
        (self.auto / "CLAUDE.local.md").write_text(f"{START}\n{key}: 'old text'\nmode: minimal\n{END}\n")

        out = self.run_repos("--apply")

        self.assertIn(f"STALE   ~/repos/auto-one/CLAUDE.local.md {key}", out)
        text = (self.auto / "CLAUDE.local.md").read_text()
        self.assertIn(f'{key}: "{self.keys[key]}"', text)
        self.assertIn("mode: minimal", text)
        self.assertNotIn("old text", text)

    def test_a_repo_with_no_file_or_no_block_gets_the_block(self):
        other = self.checkout(self.home / "repos/auto-two")
        self.add_row(str(other), "auto")
        (other / "CLAUDE.local.md").write_text("Free text only.\n")

        self.run_repos("--apply")

        block = START + "\n" + "\n".join(self.shared_lines()) + "\n" + END + "\n"
        self.assertEqual((self.auto / "CLAUDE.local.md").read_text(), block)
        self.assertEqual((other / "CLAUDE.local.md").read_text(), "Free text only.\n\n" + block)

    def test_manual_absent_and_unignored_repos_are_not_written(self):
        manual = self.checkout(self.home / "repos/manual")
        self.add_row("~/repos/manual", "manual")
        self.add_row("~/repos/not-cloned", "auto")
        open_repo = self.checkout(self.home / "repos/not-ignored", ignored=False)
        self.add_row("~/repos/not-ignored", "auto")

        out = self.run_repos("--apply")

        self.assertFalse((manual / "CLAUDE.local.md").exists())
        self.assertNotIn("not-cloned", out)
        self.assertIn("DIFFERS ~/repos/not-ignored/CLAUDE.local.md is not ignored by git; not written", out)
        self.assertFalse((open_repo / "CLAUDE.local.md").exists())
        self.assertTrue((self.auto / "CLAUDE.local.md").exists())

    def test_bad_markers_are_reported_and_left_alone(self):
        text = f"{START}\nmode: minimal\n{START}\n{END}\n"
        (self.auto / "CLAUDE.local.md").write_text(text)

        out = self.run_repos("--apply")

        self.assertIn("DIFFERS ~/repos/auto-one/CLAUDE.local.md", out)
        self.assertEqual((self.auto / "CLAUDE.local.md").read_text(), text)

    def test_a_failed_repo_list_is_a_skip(self):
        base.write_exec(self.repo / "local-sd-db/sd-db.sh", "#!/bin/sh\nexit 1\n")

        out = self.run_repos("--apply")

        self.assertIn("SKIP    CLAUDE.local.md shared keys: sd-db.sh repo list failed", out)
