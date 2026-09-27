"""sd:1752: the clone's check reads the primary checkout's untracked `CLAUDE.local.md`.

Three fleet rows on 2026-09-27 ran the repository's default test command in
the clone, because the overrides that name the right one live in an untracked
file the clone never had. Real clones and supervisors through
`test_runtime.Fixture`; the check is a stub that passes only with the block.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_runner import gitops

from . import test_runtime
from .test_runtime import git

BLOCK = ("<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\ntest: python3 -m pytest -n 4\n"
         "<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n")
#: Only the clone's own excludes count: the operator's global file also names `CLAUDE.local.md`.
NO_GLOBAL = {"GIT_CONFIG_GLOBAL": os.devnull}


class Overrides(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for name in ("root", "db", "checkout", "runner", "provider"):
            setattr(self, name, getattr(self.fixture, name))
        self.seen = self.root / "check-saw"
        check = self.root / "check.py"
        check.write_text("import sys\nfrom pathlib import Path\n"
                         "local = Path('CLAUDE.local.md')\n"
                         f"Path({str(self.seen)!r}).write_text(local.read_text() if local.is_file() else '')\n"
                         "if 'pytest -n 4' not in (local.read_text() if local.is_file() else ''):\n"
                         "    sys.stderr.write('pytest: error: unrecognized arguments: -n\\n')\n    sys.exit(4)\n")
        self.check = [sys.executable, str(check)]

    def run_fixture(self):
        request = self.fixture.claim()
        with patch.dict(os.environ, NO_GLOBAL):
            result = self.runner.execute(self.db, request, command=[sys.executable, str(self.provider)],
                                         environment={"PATH": os.environ["PATH"], "HOME": str(self.root)}, check=self.check)
        return request, result

    def test_the_check_reads_the_primary_block_and_the_note_names_its_source(self):
        (self.checkout / "CLAUDE.local.md").write_text(BLOCK)
        request, result = self.run_fixture()
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(self.seen.read_text(), BLOCK)
        self.assertIn(f"local overrides copied from {self.checkout / 'CLAUDE.local.md'}", result["detail"])
        note = self.db.execute("SELECT body FROM note WHERE item=? AND kind='exec'", (self.fixture.item,)).fetchone()
        self.assertIn("local overrides copied from", note["body"])
        retained = Path(result["retained_path"])
        self.assertEqual(git(retained, "ls-tree", "-r", "--name-only", "HEAD", "--", "CLAUDE.local.md"), "",
                         "the copy is never committed")
        self.assertEqual(gitops.remote_head(retained, "work/item"), git(retained, "rev-parse", "HEAD"))

    def test_without_a_primary_block_the_run_is_unchanged(self):
        request, result = self.run_fixture()
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertIn("hard stop: failing test", result["detail"])
        self.assertNotIn("local overrides", result["detail"])
        self.assertEqual(self.seen.read_text(), "")


class Carry(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.checkout = self.fixture.checkout
        self.request = self.fixture.claim()
        self.clone = Path(self.request["run"]["work_path"])
        self.enterContext(patch.dict(os.environ, NO_GLOBAL))

    def prepare(self):
        gitops.clone(self.request)
        return gitops.branch(self.request)

    def test_the_copy_is_excluded_so_the_clone_is_clean(self):
        (self.checkout / "CLAUDE.local.md").write_text(BLOCK)
        (self.checkout / "other-untracked.txt").write_text("stays in the checkout\n")
        result = self.prepare()
        self.assertEqual(result["local_overrides"], str(self.checkout / "CLAUDE.local.md"))
        self.assertEqual((self.clone / "CLAUDE.local.md").read_text(), BLOCK)
        self.assertFalse((self.clone / "other-untracked.txt").exists(), "only CLAUDE.local.md is carried")
        self.assertFalse(gitops.dirty(self.clone), git(self.clone, "status", "--porcelain"))
        ignored = git(self.clone, "check-ignore", "-v", "CLAUDE.local.md")
        self.assertTrue(ignored.startswith(".git/info/exclude:"), ignored)
        before = (self.clone / ".git/info/exclude").read_text()
        self.assertEqual(gitops.carry_local_overrides(self.checkout, self.clone), None, "an existing copy is kept")
        self.assertEqual((self.clone / ".git/info/exclude").read_text(), before)

    def test_a_checkout_without_the_file_leaves_the_clone_as_it_was(self):
        result = self.prepare()
        self.assertIsNone(result["local_overrides"])
        self.assertFalse((self.clone / "CLAUDE.local.md").exists())
        exclude = self.clone / ".git/info/exclude"
        self.assertNotIn("CLAUDE.local.md", exclude.read_text() if exclude.exists() else "")

    def test_a_tracked_file_on_the_branch_is_not_replaced(self):
        (self.checkout / "CLAUDE.local.md").write_text("tracked block\n")
        git(self.checkout, "add", "CLAUDE.local.md")
        git(self.checkout, "commit", "-qm", "track the block")
        (self.checkout / "CLAUDE.local.md").write_text(BLOCK)
        result = self.prepare()
        self.assertIsNone(result["local_overrides"])
        self.assertEqual((self.clone / "CLAUDE.local.md").read_text(), "tracked block\n")


if __name__ == "__main__":
    unittest.main()
