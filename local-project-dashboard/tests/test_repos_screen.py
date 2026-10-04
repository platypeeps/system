"""Operations > Repos: the checkout fleet as git reports it (sd:719 step 5).

The rows come from `fleet.py` run as a child under the collectors' `Budget`,
against a fixture root of `git init` checkouts. Nothing here opens a database
of its own; the criterion 2 grep in `tests/test_markup.py` is the check.
"""

import os
import re
import sys
from unittest.mock import patch

from sd_dashboard import fleet, operations_screen, repos_screen

from fleet_support import FleetCase


class ReposArea(FleetCase):

    def repos(self, **parameters):
        return self.render("/operations", {"area": ["repos"], **{key: [value] for key, value in parameters.items()}})

    def cells(self, page, css):
        """The text of every `css` cell, in table order, tags stripped."""
        return [re.sub(r"<[^>]+>", "", cell).strip()
                for cell in re.findall(rf'<td class="{css}">(.*?)</td>', page, re.DOTALL)]

    def test_the_page_lists_every_checkout_with_its_branch_and_dirt(self):
        grouped = self.checkout("widgets", "acme", branch="feat/wider", when="2026-09-02T10:00:00+00:00")
        solo = self.checkout("solo")
        (solo / "scratch.txt").write_text("untracked\n")
        (self.root / "acme" / "not-a-checkout").mkdir()
        (self.root / ".hidden").mkdir()
        page = self.repos()
        self.assertEqual(self.cells(page, "repo-name"), ["acme/widgets", "solo"])
        self.assertEqual(self.cells(page, "repo-branch"), ["feat/wider", "main"])
        self.assertEqual(self.cells(page, "repo-dirty"), ["0", "1"])
        self.assertEqual(self.cells(page, "repo-path"), [str(grouped), str(solo)])
        self.assertIn("first commit in widgets", page)
        self.assertNotIn("not-a-checkout", page)
        self.assertNotIn(".hidden", page)
        self.assertIn("2 checkouts under ", page)
        self.assertIn(" · 1 dirty · 0 ahead", page)

    def test_divergence_is_unknown_without_an_upstream_and_counted_with_one(self):
        origin = self.checkout("origin", "remote")
        clone = self.root / "clone"
        self.git(self.root, "clone", "-q", str(origin), str(clone))
        self.configure(clone)
        (clone / "README").write_text("more\n")
        self.git(clone, "commit", "-q", "-am", "ahead by one")
        page = self.repos()
        by_name = dict(zip(self.cells(page, "repo-name"), self.cells(page, "repo-divergence")))
        self.assertEqual(sorted(by_name), ["clone", "remote/origin"])
        self.assertEqual(by_name["clone"], "+1 / −0")
        self.assertEqual(by_name["remote/origin"], "unknown")
        self.assertIn(" · 1 ahead", page)

    def test_the_area_is_in_the_navigation_and_the_subtitle_names_it(self):
        page = self.repos()
        navigation = re.search(r'<nav[^>]*aria-label="Operations areas"[^>]*>(.*?)</nav>', page).group(1)
        self.assertIn('href="/operations?area=repos"', navigation)
        self.assertIn(">Repos</a>", navigation)
        self.assertIn(("repos", "Repos"), operations_screen.AREAS)
        subtitle = re.search(r'<p class="subtitle">([^<]*)</p>', page).group(1)
        self.assertIn("repos", subtitle)

    def test_a_missing_root_is_said_rather_than_shown_as_an_empty_fleet(self):
        with patch.dict("os.environ", {"REPO_ROOT": str(self.root / "elsewhere")}):
            page = self.repos()
        self.assertIn("does not exist", page)
        self.assertNotIn("0 checkouts under", page)

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
        page = self.repos()
        # The checkouts that answered are on the page; the one that did not
        # is a row that says so, not a row with a branch it made up.
        self.assertEqual(self.cells(page, "repo-name"), ["fine", "stuck"])
        self.assertEqual(self.cells(page, "repo-branch"), ["main", "?"])
        self.assertEqual(self.cells(page, "repo-note"), ["", self.cut()])
        self.assertIn("1 checkout could not be read", page)
        # Cut by the child at its own deadline, so the hung git is gone with
        # it rather than left holding a pipe (the `collectors.run` docstring).
        self.assertGone("git")

    def test_a_child_slow_to_start_still_answers_before_the_page_kills_it(self):
        # The child's deadline is counted from the page's start, not its own:
        # an interpreter that took a second to start under load spent that
        # second of the margin, the page's kill arrived first, and the page
        # said "stopped at its budget" with no row at all (sd:2244).
        stuck = self.checkout("stuck")
        self.hanging("git", when=f"*{stuck}*")
        slow = self.shim("slow-python", f'sleep 1.2; exec "{sys.executable}" "$@"')
        with patch.object(fleet, "FLEET_SECONDS", 3.0), patch.object(fleet, "FLEET_MARGIN", 1.0), \
                patch.object(sys, "executable", str(slow)):
            page = self.repos()
        self.assertNotIn("Repos could not be observed", page)
        self.assertEqual(self.cells(page, "repo-name"), ["stuck"])
        # Started or refused depends on how long the start took; either way
        # the row names the child's budget, not the page's.
        self.assertRegex(self.cells(page, "repo-note")[0], r"^git .*the budget of 2 seconds")
        self.assertGone("git")

    def messy(self, name="messy", *, when="2026-09-02T10:00:00+00:00"):
        """A checkout whose `git status --porcelain` outgrows the 64 KB ceiling: 1500 untracked paths of 59 bytes a line."""
        path = self.checkout(name, when=when)
        for index in range(1500):
            (path / f"untracked-{index:04d}-{'x' * 40}").touch()
        return path

    def test_a_status_that_outgrows_the_ceiling_is_a_truncated_row_and_not_a_refusal(self):
        self.checkout("tidy")
        self.messy()
        page = self.repos()
        self.assertEqual(self.cells(page, "repo-name"), ["messy", "tidy"])
        # The rest of the checkout was still read: the cut is the status's.
        self.assertEqual(self.cells(page, "repo-branch"), ["main", "main"])
        dirty = self.cells(page, "repo-dirty")
        self.assertEqual(dirty[1], "0")
        self.assertRegex(dirty[0], r"^≥ \d+$")
        floor = int(dirty[0][2:])
        self.assertGreater(floor, 0)
        self.assertLess(floor, 1500)
        self.assertEqual(self.cells(page, "repo-note"), ["git status was cut at 64 KB; the dirt count is a floor", ""])
        self.assertNotIn("could not be read", page)
        self.assertNotIn("Repos could not be observed", page)

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
        page = self.repos()
        # stdout closed and the process stayed: that is a hang, said as one,
        # not a command that answered and not "was not started" for the next.
        self.assertEqual(self.cells(page, "repo-name"), ["fine", "stuck"])
        self.assertEqual(self.cells(page, "repo-note"), ["", self.cut()])
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
        page = self.repos()
        by_name = dict(zip(self.cells(page, "repo-name"), self.cells(page, "repo-dirty")))
        self.assertEqual(by_name["broken"], "?")
        self.assertIn("1 checkout could not be read", page)
        self.assertIn(" · 0 dirty · ", page)

    def test_a_remote_is_linked_only_with_an_https_host(self):
        linked = self.checkout("linked")
        self.git(linked, "remote", "add", "origin", "https://github.com/acme/linked.git")
        page = self.repos()
        self.assertIn('<a href="https://github.com/acme/linked" rel="noopener noreferrer">linked</a>', page)
        row = {"name": "x", "group": ".", "web": "https://@/x"}
        self.assertEqual(repos_screen._link(row), "<span>x</span>")
        row["web"] = "http://github.com/acme/x"
        self.assertEqual(repos_screen._link(row), "<span>x</span>")
        row["web"] = "https://github.com/acme/x"
        self.assertIn('href="https://github.com/acme/x"', repos_screen._link(row))
        # The malformed case the #427 nit named and the others did not cover:
        # `urlsplit` raises for an unclosed IPv6 authority, and a raised
        # parser is not a safe URL.
        row["web"] = "https://[oops/acme/x"
        self.assertEqual(repos_screen._link(row), "<span>x</span>")

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
            page = self.repos()
        self.assertEqual(self.cells(page, "repo-dirty"), ["?"])
        self.assertIn("git rev-parse did not run", page)
        self.assertIn("1 checkout could not be read", page)

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
        self.assertEqual(repos_screen._dirty(row), "0")
        self.assertEqual(repos_screen._note({**row, "error": ""}), "git log was cut at 64 KB")

    def test_a_collector_that_dies_leaves_a_refusal_and_not_a_calm_page(self):
        self.checkout("fine")
        with patch.object(fleet, "FLEET", self.root / "no-such-child.py"):
            page = self.repos()
        self.assertIn("Repos could not be observed: ", page)
        self.assertNotIn('<td class="repo-name">', page)

    def test_the_fleet_declares_one_budget_the_child_reads_inside(self):
        self.assertEqual(fleet.FLEET_SECONDS, 12.0)
        self.assertLess(0, fleet.FLEET_MARGIN)
        self.assertLess(fleet.FLEET_MARGIN, fleet.FLEET_SECONDS)
        self.assertEqual(set(fleet.AREAS), {"repos", "sessions"})
        # The ceiling the row names is the collectors' one number, in the words `Budget.terms` uses.
        self.assertEqual(fleet.CEILING, f"{fleet._collectors().BUDGET_BYTES // 1024} KB")
