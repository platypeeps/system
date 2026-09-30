"""`jev label sd-review`: whether a review tier was deep enough, read from what
happened to the change afterwards (sd:2107).

Everything runs against fixtures. The database is a temporary one under a
temporary HOME, written and read through the real `sd-db.sh`; `gh` is a stub on
PATH that answers from a JSON file and fails on anything it was not given; the
checkout is a git repository built here with dated commits. Nothing reaches
GitHub, the operator's store or a model.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "jev.sh"
SD_DB_FOLDER = FOLDER.parent / "local-sd-db"

# The same fallback `test_jev_metering` uses: the installed `sd_db` when there
# is one, else the checkout's, so the suite never skips.
try:  # pragma: no cover - one branch per machine
    import sd_db  # noqa: F401
except ImportError:  # pragma: no cover - one branch per machine
    sys.path.insert(0, str(SD_DB_FOLDER))

from sd_db.database import connect  # noqa: E402
from sd_db.judgment import record  # noqa: E402
from sd_db.migrate import initialise  # noqa: E402
from sd_db.repos import add as add_repo  # noqa: E402

NOW = "2026-09-29T12:00:00+00:00"
STAGE = "JEV_SD_REVIEW"
SOURCE = "outcome.sd-review.14d"
MERGED = "2026-08-20T00:00:00Z"

#: The gh stub: answers `gh api PATH` from the fixture file, logs every call,
#: and fails on a path it was not given, so an unexpected call is loud.
GH_STUB = """#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["GH_STUB_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
paths = [a for a in args if not a.startswith("-")]
if not paths or paths[0] != "api":
    sys.stderr.write("gh stub: only `gh api` is stubbed\\n")
    sys.exit(2)
fixture = json.load(open(os.environ["GH_STUB_FIXTURE"]))
answer = fixture.get(paths[1])
if answer is None:
    sys.stderr.write("gh stub: HTTP 404 for " + paths[1] + "\\n")
    sys.exit(1)
if answer == "FAIL":
    sys.stderr.write("gh stub: HTTP 502\\n")
    sys.exit(1)
sys.stdout.write(json.dumps(answer))
"""


def git(cwd, *args, when=None):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1"}
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    return subprocess.run(["git", "-C", str(cwd), *args], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class LabelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.config = self.tmp / "config"
        self.config.mkdir()
        self.database = self.home / ".local/share/sd/sd.db"
        self.database.parent.mkdir(parents=True)
        initialise(self.database)
        self.checkout = self.home / "repos" / "widgets"
        self.build_checkout()
        connection = connect(self.database)
        try:
            add_repo(connection, self.checkout, home=self.home)
        finally:
            connection.close()
        self.stubs = self.tmp / "bin"
        self.stubs.mkdir()
        stub = self.stubs / "gh"
        stub.write_text(GH_STUB.format(python=sys.executable))
        stub.chmod(0o755)
        self.gh_log = self.tmp / "gh.log"
        self.gh_log.write_text("")
        self.fixture = {}

    def build_checkout(self):
        """A linear default branch with five merged changes and what came
        after each, dated so the fourteen-day window has something to see."""
        repo = self.checkout
        repo.mkdir(parents=True)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "tester@example.test")
        git(repo, "config", "user.name", "Tester")
        git(repo, "remote", "add", "origin", "https://github.com/example/widgets.git")
        self.merges = {}

        def commit(files, subject, when, key=None):
            for name in files:
                path = repo / name
                path.write_text(path.read_text() + subject + "\n"
                                if path.exists() else subject + "\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", subject, when=when)
            sha = git(repo, "rev-parse", "HEAD")
            if key:
                self.merges[key] = sha
            return sha

        commit(["a.py", "b.py", "c.py", "d.py", "e.py", "README.md"],
               "Start", "2026-08-01T00:00:00Z")
        commit(["a.py"], "Add the a feature (#1)", "2026-08-20T00:00:00Z", "right")
        commit(["b.py"], "Add the b feature (#2)", "2026-08-20T01:00:00Z", "wrong")
        commit(["c.py"], "Add the c feature (#3)", "2026-08-20T02:00:00Z", "deepest")
        commit(["d.py"], "Add the d feature (#4)", "2026-08-20T03:00:00Z", "late")
        commit(["c.py"], 'Revert "Add the c feature (#3)"', "2026-08-22T00:00:00Z")
        commit(["b.py"], "fix: the b edge case", "2026-08-23T00:00:00Z")
        commit(["README.md"], "fix: a typo in the a docs", "2026-08-24T00:00:00Z")
        commit(["a.py"], "Fixture data for a", "2026-08-25T00:00:00Z")
        commit(["d.py"], "fix(d): a late problem", "2026-09-10T00:00:00Z")
        commit(["e.py"], "Add the e feature (#5)", "2026-09-25T00:00:00Z", "fresh")
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.head = git(repo, "rev-parse", "HEAD")

    # -- fixtures ------------------------------------------------------------

    def judged(self, sha12, answer="2", subject=None, when="2026-08-19T12:00:00+00:00"):
        connection = connect(self.database)
        try:
            return record(
                connection, caller="sd-review", stage=STAGE, provider="typesafe",
                primitive="choice", outcome="ok", answer=answer, confidence=0.9,
                question_id=subject or f"sd-review-tier:example.widgets:{sha12}",
                now=when)
        finally:
            connection.close()

    def pull(self, sha12, number, files, merged_at=MERGED, key=None):
        base = "repos/example/widgets"
        self.fixture[f"{base}/commits/{sha12}/pulls"] = [{
            "number": number,
            "merged_at": merged_at,
            "merge_commit_sha": self.merges.get(key) if key else None,
            "base": {"ref": "main"},
        }]
        self.fixture[f"{base}/pulls/{number}/files"] = [
            {"filename": name} for name in files]
        self.fixture[f"{base}/branches/main"] = {"commit": {"sha": self.head}}

    def run_label(self, *extra):
        fixture = self.tmp / "gh.json"
        fixture.write_text(json.dumps(self.fixture))
        git_dir = os.path.dirname(shutil.which("git"))
        env = {
            "HOME": str(self.home),
            "PATH": os.pathsep.join([str(self.stubs), git_dir, "/usr/bin", "/bin"]),
            "PYTHON": sys.executable,
            "SD_DB_LIBRARY": "checkout",
            "SYSTEM_TOOLS_CONFIG": str(self.config),
            "GH_STUB_FIXTURE": str(fixture),
            "GH_STUB_LOG": str(self.gh_log),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "JEV_METER": "0",
        }
        return subprocess.run(
            ["sh", str(ENTRYPOINT), "label", "sd-review", "--now", NOW, *extra],
            env=env, capture_output=True, text=True, timeout=120)

    def labels(self):
        connection = connect(self.database, write=False)
        try:
            return {row["id"]: (row["override"], row["override_source"])
                    for row in connection.execute(
                        "SELECT id, override, override_source FROM judgment")}
        finally:
            connection.close()


class TheOutcomes(LabelCase):
    def test_a_merged_change_with_no_later_fix_is_right(self):
        """`fix: a typo in the a docs` touches only README.md, and `Fixture
        data for a` starts with `Fix` but is not a fix: neither counts."""
        row = self.judged("aaaaaaaaaaa1", answer="2")
        self.pull("aaaaaaaaaaa1", 1, ["a.py"], key="right")
        result = self.run_label("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"row {row} ", result.stdout)
        self.assertIn("right", result.stdout)
        self.assertEqual(self.labels()[row], ("2", SOURCE))

    def test_a_later_fix_touching_its_files_is_wrong_and_labels_one_deeper(self):
        row = self.judged("bbbbbbbbbbb2", answer="2")
        self.pull("bbbbbbbbbbb2", 2, ["b.py"], key="wrong")
        result = self.run_label("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("wrong", result.stdout)
        self.assertIn("fix: the b edge case", result.stdout)
        self.assertEqual(self.labels()[row], ("3", SOURCE))

    def test_a_revert_at_the_deepest_tier_is_not_the_tier_s_fault(self):
        row = self.judged("ccccccccccc3", answer="4")
        self.pull("ccccccccccc3", 3, ["c.py"], key="deepest")
        result = self.run_label("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deepest", result.stdout)
        self.assertEqual(self.labels()[row], ("4", SOURCE))

    def test_a_shallower_tier_with_a_revert_is_wrong(self):
        row = self.judged("ccccccccccc3", answer="3")
        self.pull("ccccccccccc3", 3, ["c.py"], key="deepest")
        self.run_label("--apply")
        self.assertEqual(self.labels()[row], ("4", SOURCE))

    def test_a_fix_after_the_window_does_not_count(self):
        row = self.judged("ddddddddddd4", answer="1")
        self.pull("ddddddddddd4", 4, ["d.py"], key="late")
        self.run_label("--apply")
        self.assertEqual(self.labels()[row], ("1", SOURCE))

    def test_the_deepest_position_can_be_named(self):
        row = self.judged("ccccccccccc3", answer="3")
        self.pull("ccccccccccc3", 3, ["c.py"], key="deepest")
        self.run_label("--apply", "--deepest", "3")
        self.assertEqual(self.labels()[row], ("3", SOURCE))


class WhatItSkips(LabelCase):
    def assert_skipped(self, row, reason, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next((line for line in result.stdout.splitlines()
                     if line.startswith(f"row {row} ")), "")
        self.assertIn(f"skip, {reason}", line, result.stdout)
        self.assertEqual(self.labels()[row], (None, None))

    def test_an_unmerged_change_is_skipped(self):
        row = self.judged("eeeeeeeeeee5", when="2026-09-20T00:00:00+00:00")
        self.pull("eeeeeeeeeee5", 5, ["e.py"], merged_at=None)
        self.assert_skipped(row, "not merged", self.run_label("--apply"))

    def test_an_unmerged_change_a_month_old_is_abandoned(self):
        row = self.judged("eeeeeeeeeee5", when="2026-08-01T00:00:00+00:00")
        self.pull("eeeeeeeeeee5", 5, ["e.py"], merged_at=None)
        self.assert_skipped(row, "abandoned", self.run_label("--apply"))

    def test_a_change_with_no_pull_request_is_not_merged(self):
        row = self.judged("eeeeeeeeeee5", when="2026-09-20T00:00:00+00:00")
        self.fixture["repos/example/widgets/commits/eeeeeeeeeee5/pulls"] = []
        self.assert_skipped(row, "not merged", self.run_label("--apply"))

    def test_a_change_merged_inside_the_window_is_skipped(self):
        row = self.judged("fffffffffff6")
        self.pull("fffffffffff6", 6, ["e.py"], merged_at="2026-09-25T00:00:00Z",
                  key="fresh")
        self.assert_skipped(row, "window open", self.run_label("--apply"))

    def test_a_repository_with_no_checkout_is_skipped(self):
        row = self.judged("aaaaaaaaaaa1",
                          subject="sd-review-tier:example.gadgets:aaaaaaaaaaa1")
        self.assert_skipped(row, "no checkout", self.run_label("--apply"))
        self.assertEqual(self.gh_log.read_text(), "")

    def test_gh_failing_skips_the_row_and_never_labels_it(self):
        row = self.judged("aaaaaaaaaaa1")
        self.fixture["repos/example/widgets/commits/aaaaaaaaaaa1/pulls"] = "FAIL"
        self.assert_skipped(row, "gh failed", self.run_label("--apply"))

    def test_a_checkout_behind_github_is_skipped(self):
        """A stale `origin/main` would read a missed fix as no fix."""
        row = self.judged("aaaaaaaaaaa1")
        self.pull("aaaaaaaaaaa1", 1, ["a.py"], key="right")
        self.fixture["repos/example/widgets/branches/main"] = {
            "commit": {"sha": "0" * 40}}
        self.assert_skipped(row, "checkout behind", self.run_label("--apply"))

    def test_a_row_with_no_answer_is_skipped(self):
        connection = connect(self.database)
        try:
            row = record(connection, caller="sd-review", stage=STAGE,
                         provider="typesafe", primitive="choice",
                         outcome="fallback", cause="unkeyed",
                         question_id="sd-review-tier:example.widgets:aaaaaaaaaaa1",
                         now="2026-08-19T00:00:00+00:00")
        finally:
            connection.close()
        self.assert_skipped(row, "no answer", self.run_label("--apply"))

    def test_a_row_without_a_subject_is_not_read(self):
        row = self.judged("aaaaaaaaaaa1", subject="sd-review-tier")
        result = self.run_label("--apply")
        self.assertNotIn(f"row {row} ", result.stdout)
        self.assertEqual(self.labels()[row], (None, None))


class TheDryRun(LabelCase):
    def test_without_apply_it_reports_and_writes_nothing(self):
        right = self.judged("aaaaaaaaaaa1", answer="2")
        self.pull("aaaaaaaaaaa1", 1, ["a.py"], key="right")
        wrong = self.judged("bbbbbbbbbbb2", answer="2")
        self.pull("bbbbbbbbbbb2", 2, ["b.py"], key="wrong")
        before = self.database.read_bytes()
        result = self.run_label()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would label", result.stdout)
        self.assertIn("2 to label, 0 skipped; dry run, --apply writes", result.stdout)
        self.assertEqual(self.labels(), {right: (None, None), wrong: (None, None)})
        self.assertEqual(before, self.database.read_bytes())

    def test_a_second_apply_labels_nothing_new(self):
        self.judged("aaaaaaaaaaa1", answer="2")
        self.pull("aaaaaaaaaaa1", 1, ["a.py"], key="right")
        self.run_label("--apply")
        result = self.run_label("--apply")
        self.assertIn("0 labelled, 0 skipped", result.stdout)


class TheCommand(unittest.TestCase):
    def test_label_is_in_the_help(self):
        out = subprocess.run(["sh", str(ENTRYPOINT), "help"], capture_output=True,
                             text=True).stdout
        self.assertIn("label sd-review", out)

    def test_an_unknown_rule_is_refused(self):
        result = subprocess.run(["sh", str(ENTRYPOINT), "label", "nothing"],
                                capture_output=True, text=True,
                                env={**os.environ, "PYTHON": sys.executable})
        self.assertEqual(result.returncode, 1)
        self.assertIn("sd-review", result.stderr)


if __name__ == "__main__":
    unittest.main()
