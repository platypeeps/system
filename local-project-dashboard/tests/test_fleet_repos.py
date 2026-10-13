"""The fleet's repos read: the checkout fleet as git reports it (sd:719 step 5).

The rows come from `fleet.py` run as a child under the collectors' `Budget`,
against a fixture root of `git init` checkouts. Management and Today read them.
These tests read the document itself: the classic Operations > Repos area that
drew it was retired in sd:3402.
"""

import os
from unittest.mock import patch

from sd_dashboard import fleet

from fleet_support import FleetCase, SlowStart


class ReposRead(FleetCase):

    def repos(self):
        return fleet.collect("repos")

    def column(self, document, field):
        """Every row's `field`, in the document's order; `name` carries its group, as Management shows it."""
        if field == "name":
            return [row["name"] if row["group"] == "." else f"{row['group']}/{row['name']}" for row in document["repos"]]
        return [row[field] for row in document["repos"]]

    def test_the_page_lists_every_checkout_with_its_branch_and_dirt(self):
        grouped = self.checkout("widgets", "acme", branch="feat/wider", when="2026-09-02T10:00:00+00:00")
        solo = self.checkout("solo")
        (solo / "scratch.txt").write_text("untracked\n")
        (self.root / "acme" / "not-a-checkout").mkdir()
        (self.root / ".hidden").mkdir()
        document = self.repos()
        self.assertEqual(self.column(document, "name"), ["acme/widgets", "solo"])
        self.assertEqual(self.column(document, "branch"), ["feat/wider", "main"])
        self.assertEqual(self.column(document, "dirty"), [0, 1])
        self.assertEqual(self.column(document, "path"), [str(grouped), str(solo)])
        self.assertIn("first commit in widgets", self.column(document, "subject"))

    def test_divergence_is_unknown_without_an_upstream_and_counted_with_one(self):
        origin = self.checkout("origin", "remote")
        clone = self.root / "clone"
        self.git(self.root, "clone", "-q", str(origin), str(clone))
        self.configure(clone)
        (clone / "README").write_text("more\n")
        self.git(clone, "commit", "-q", "-am", "ahead by one")
        document = self.repos()
        by_name = dict(zip(self.column(document, "name"), zip(self.column(document, "ahead"), self.column(document, "behind"))))
        self.assertEqual(sorted(by_name), ["clone", "remote/origin"])
        self.assertEqual(by_name["clone"], (1, 0))
        self.assertEqual(by_name["remote/origin"], (None, None))

    def test_one_status_answers_the_branch_the_dirt_and_the_divergence(self):
        # Branch, dirt and divergence were three git commands per checkout,
        # and starting git is most of what the fleet read costs (sd:2195).
        origin = self.checkout("origin", "remote")
        clone = self.root / "clone"
        self.git(self.root, "clone", "-q", str(origin), str(clone))
        self.configure(clone)
        (clone / "README").write_text("more\n")
        self.git(clone, "commit", "-q", "-am", "ahead by one")
        (clone / "README").write_text("edited\n")
        (clone / "new.txt").write_text("untracked\n")
        calls = self.root.parent / "git.calls"
        self.shim("git", f'echo "$*" >> "{calls}"; exec /usr/bin/git "$@"')
        row = fleet.facts(fleet._collectors(), clone)
        self.assertEqual((row["branch"], row["dirty"], row["ahead"], row["behind"]), ("main", 2, 1, 0))
        self.assertEqual((row["default"], row["behind_default"]), ("main", 0))
        self.assertEqual(len(calls.read_text().splitlines()), 5)
        # A detached checkout is named as `rev-parse --abbrev-ref` named it.
        self.git(clone, "checkout", "-q", "--detach")
        self.assertEqual(fleet.facts(fleet._collectors(), clone)["branch"], "HEAD")

    def test_a_pinned_checkout_counts_its_lag_behind_origin_apart(self):
        """sd:3539: a detached HEAD is pinned, as repo-sync reads it; its lag
        behind origin's default is `pinned_behind`, never `behind_default`."""
        origin = self.checkout("origin", "remote")
        clone = self.root / "clone"
        self.git(self.root, "clone", "-q", str(origin), str(clone))
        self.configure(clone)
        self.git(clone, "checkout", "-q", "--detach")
        (origin / "README").write_text("merged later\n")
        self.git(origin, "commit", "-q", "-am", "a merge the hub has not taken")
        self.git(clone, "fetch", "-q", "origin")
        row = fleet.facts(fleet._collectors(), clone)
        self.assertEqual((row["branch"], row["default"], row["behind_default"], row["pinned_behind"]),
                         ("HEAD", "main", None, 1))
        self.git(clone, "checkout", "-q", "main")
        row = fleet.facts(fleet._collectors(), clone)
        self.assertEqual((row["behind_default"], row["pinned_behind"]), (1, None))

    def test_a_missing_root_is_said_rather_than_shown_as_an_empty_fleet(self):
        with patch.dict("os.environ", {"REPO_ROOT": str(self.root / "elsewhere")}):
            document = self.repos()
        self.assertFalse(document["rootExists"])
        self.assertEqual(document["repos"], [])

    def cut(self):
        """The note a row gets when the child's deadline cuts its git.

        The two hang tests run under the production budget, not a short one:
        the checkout that answers has to be read inside the same deadline, and
        under gate load a 2-second one cut it too, so both rows came back
        unread and sorted by name (sd:2621). A machine that cannot read one
        checkout in this budget fails the real page as well.
        """
        return f"git ran past the budget of {fleet.FLEET_SECONDS - fleet.FLEET_MARGIN:g} seconds and was stopped"

    def test_a_hung_checkout_is_cut_at_the_budget_and_the_page_says_which(self):
        self.checkout("fine")
        stuck = self.checkout("stuck")
        self.hanging("git", when=f"*{stuck}*")
        document = self.repos()
        # The checkouts that answered are rows; the one that did not is a row
        # that says so, not a row with a branch it made up.
        self.assertEqual(self.column(document, "name"), ["fine", "stuck"])
        self.assertEqual(self.column(document, "branch"), ["main", "?"])
        self.assertEqual(self.column(document, "error"), ["", self.cut()])
        # Cut by the child at its own deadline, so the hung git is gone with
        # it rather than left holding a pipe (the `collectors.run` docstring).
        self.assertGone("git")

    def test_a_child_slow_to_start_still_answers_before_the_page_kills_it(self):
        # The child's deadline is counted from the page's start, not its own:
        # an interpreter that took a second to start under load spent that
        # second of the margin, the page's kill arrived first, and the page
        # said "stopped at its budget" with no row at all (sd:2244).
        #
        # sd:2667. The slow start is charged to the page's clock instead of
        # slept: a real 1.2 s start against a 2 s deadline left the child no
        # time to start git on a loaded machine, or none to answer at all.
        # Charged the whole budget, the child counting from the page's start
        # finds its deadline spent and answers at once, whatever the load;
        # one counting from its own start runs the hung git for 11 seconds.
        stuck = self.checkout("stuck")
        self.hanging("git", when=f"*{stuck}*")
        with patch.object(fleet, "time", SlowStart(fleet.FLEET_SECONDS)):
            document = self.repos()
        self.assertEqual(self.column(document, "name"), ["stuck"])
        # The row names the child's budget, not the page's.
        self.assertEqual(self.column(document, "error"),
                         [f"git was not started: the budget of {fleet.FLEET_SECONDS - fleet.FLEET_MARGIN:g} seconds was spent"])
        self.assertFalse((self.root.parent / "git.pids").exists(), "the child started git past its deadline")

    def messy(self, name="messy", *, when="2026-09-02T10:00:00+00:00"):
        """A checkout whose `git status --porcelain` outgrows the 64 KB ceiling: 1500 untracked paths of 59 bytes a line."""
        path = self.checkout(name, when=when)
        for index in range(1500):
            (path / f"untracked-{index:04d}-{'x' * 40}").touch()
        return path

    def test_a_status_that_outgrows_the_ceiling_is_a_truncated_row_and_not_a_refusal(self):
        self.checkout("tidy")
        self.messy()
        document = self.repos()
        self.assertEqual(self.column(document, "name"), ["messy", "tidy"])
        # The rest of the checkout was still read: the cut is the status's.
        self.assertEqual(self.column(document, "branch"), ["main", "main"])
        dirty = self.column(document, "dirty")
        self.assertEqual(dirty[1], 0)
        self.assertGreater(dirty[0], 0)
        self.assertLess(dirty[0], 1500)
        self.assertEqual(self.column(document, "truncated"), [["status"], []])
        self.assertEqual(self.column(document, "error"), ["", ""])

    def test_the_child_reads_each_git_answer_to_the_ceiling_and_no_further(self):
        messy = self.messy()
        collectors = fleet._collectors()
        text, cut, code = fleet.read_output(collectors, ["git", "-C", str(messy), "status", "--porcelain"])
        self.assertTrue(cut)
        # What was held is the ceiling and nothing past it; a stripped tail
        # newline is the only slack.
        self.assertLessEqual(len(text.encode()), collectors.BUDGET_BYTES)
        self.assertGreaterEqual(len(text.encode()), collectors.BUDGET_BYTES - 64)
        text, cut, code = fleet.read_output(collectors, ["git", "-C", str(messy), "rev-parse", "--abbrev-ref", "HEAD"])
        self.assertEqual((text, cut, code), ("main", False, 0))

    def test_a_git_that_goes_quiet_and_hangs_is_cut_at_the_deadline_too(self):
        self.checkout("fine")
        stuck = self.checkout("stuck")
        self.hanging("git", when=f"*{stuck}*", quiet=True)
        document = self.repos()
        # stdout closed and the process stayed: that is a hang, said as one,
        # not a command that answered and not "was not started" for the next.
        self.assertEqual(self.column(document, "name"), ["fine", "stuck"])
        self.assertEqual(self.column(document, "error"), ["", self.cut()])
        self.assertGone("git")

    def test_a_checkout_that_cannot_be_read_is_unknown_and_not_clean(self):
        gone = self.checkout("gone")
        collectors = fleet._collectors()
        # `.git` removed between the walk and the read: not a checkout any more.
        import shutil
        shutil.rmtree(gone / ".git")
        self.assertIsNone(fleet.facts(collectors, gone))
        row = fleet._repo_row(collectors, ".", gone)
        self.assertIsNone(row["dirty"])
        self.assertEqual(row["branch"], "?")
        self.assertIn(".git", row["error"])
        # A git that exits non-zero on a command whose answer the row needs is
        # the same unknown, with the exit in the reason.
        broken = self.checkout("broken")
        self.shim("git", f'case "$*" in *{broken}*log*) exit 3 ;; *) exec /usr/bin/git "$@" ;; esac')
        row = fleet._repo_row(collectors, ".", broken)
        self.assertIsNone(row["dirty"])
        self.assertIn("git log failed (exit 3)", row["error"])
        document = self.repos()
        by_name = dict(zip(self.column(document, "name"), self.column(document, "dirty")))
        self.assertIsNone(by_name["broken"])

    def test_a_git_that_cannot_be_started_is_an_unread_row_and_not_a_clean_one(self):
        # `read_output` answers `code=None` for a command that never started.
        # `if needed and code:` read that as success, so the row showed `?`
        # for the branch, no dirt count and an empty note -- a checkout
        # nobody read, counted as one that was.
        self.checkout("stuck")
        # No `git` anywhere on PATH, so `Popen` raises. A shim that fails to
        # exec does not stage this: the PATH search treats that failure as
        # "not here" and goes on to the real git. The child itself is started
        # by absolute path and does not need PATH.
        with patch.dict(os.environ, {"PATH": str(self.bin)}):
            document = self.repos()
        self.assertEqual(self.column(document, "dirty"), [None])
        self.assertIn("git status did not run", document["repos"][0]["error"])

    def test_a_cut_log_is_named_and_leaves_the_dirt_count_exact(self):
        # `truncated` was one boolean, so any cut command reported "git status
        # was cut" and turned an exact dirt count into a floor. A 70 KB commit
        # subject cuts `log` and nothing else. Read through `facts` rather
        # than the page: what `log` kept is 64 KB, which is the page's whole
        # document budget, so the row cannot also be rendered.
        collectors = fleet._collectors()
        wordy = self.checkout("wordy")
        self.git(wordy, "commit", "-q", "--allow-empty", "-m", "x" * (70 * 1024),
                 when="2026-09-03T10:00:00+00:00")
        row = fleet.facts(collectors, wordy)
        self.assertEqual(row["truncated"], ["log"])
        self.assertEqual(row["dirty"], 0)

    def test_a_collector_that_dies_leaves_a_refusal_and_not_a_calm_read(self):
        self.checkout("fine")
        with patch.object(fleet, "FLEET", self.root / "no-such-child.py"), self.assertRaises((OSError, ValueError)):
            self.repos()

    def test_the_fleet_declares_one_budget_the_child_reads_inside(self):
        self.assertEqual(fleet.FLEET_SECONDS, 12.0)
        self.assertLess(0, fleet.FLEET_MARGIN)
        self.assertLess(fleet.FLEET_MARGIN, fleet.FLEET_SECONDS)
        self.assertEqual(set(fleet.AREAS), {"repos", "sessions"})
        # The ceiling the row names is the collectors' one number, in the words `Budget.terms` uses.
        self.assertEqual(fleet.CEILING, f"{fleet._collectors().BUDGET_BYTES // 1024} KB")
