"""Operations > Repos: each checkout against its origin's default branch (sd:1676).

Primary checkouts lagged `origin/main` after merges, and three sessions share
them, so the dashboard flags the lag and never pulls. The fixture is a real
origin and clones of it under a temp root: behind, dirty, detached, on another
branch, without `origin/HEAD`, and with a stale or missing fetch.
"""

import os
import re
import time
from datetime import datetime, timedelta, timezone

from sd_dashboard import repos_screen

from fleet_support import FleetCase


class DefaultBranch(FleetCase):

    def setUp(self) -> None:
        super().setUp()
        self.origin = self.checkout("origin", "remote")

    def repos(self):
        return self.render("/operations", {"area": ["repos"]})

    def cells(self, page):
        """Checkout name to the Default branch cell's text, tags stripped."""
        names = [re.sub(r"<[^>]+>", "", cell).strip()
                 for cell in re.findall(r'<td class="repo-name">(.*?)</td>', page, re.DOTALL)]
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell)).strip()
                 for cell in re.findall(r'<td class="repo-default">(.*?)</td>', page, re.DOTALL)]
        return dict(zip(names, cells))

    def clone(self, name):
        path = self.root / name
        self.git(self.root, "clone", "-q", str(self.origin), str(path))
        self.configure(path)
        return path

    def advance(self, count):
        """`count` new commits on the origin's `main`, which the clones see only after a fetch."""
        for index in range(count):
            self.git(self.origin, "commit", "-q", "--allow-empty", "-m", f"merged {index}")

    def behind(self, name, count=2):
        path = self.clone(name)
        self.advance(count)
        self.git(path, "fetch", "-q")
        return path

    def test_a_clean_checkout_behind_its_default_shows_the_count_and_the_pull(self):
        path = self.behind("lagging")
        page = self.repos()
        cell = self.cells(page)["lagging"]
        self.assertTrue(cell.startswith("behind 2 "), cell)
        self.assertIn("2 commits behind origin/main; fetched 0 min ago.", cell)
        self.assertIn(f"<code>git -C {path} pull --ff-only</code>", page)
        self.assertIn(" · 1 behind default", page)

    def test_a_dirty_checkout_is_flagged_without_a_pull(self):
        path = self.behind("dirty")
        (path / "README").write_text("edited\n")
        page = self.repos()
        cell = self.cells(page)["dirty"]
        self.assertTrue(cell.startswith("behind 2 "), cell)
        self.assertIn("No pull offered: 1 uncommitted file.", cell)
        self.assertNotIn("pull --ff-only", page)

    def test_local_commits_on_the_default_branch_withhold_the_pull(self):
        path = self.behind("diverged")
        self.git(path, "commit", "-q", "--allow-empty", "-m", "local only")
        cell = self.cells(self.repos())["diverged"]
        self.assertIn("No pull offered: 1 local commit not on origin/main.", cell)

    def test_a_detached_head_says_it_cannot_count(self):
        path = self.behind("detached")
        self.git(path, "checkout", "-q", "--detach")
        page = self.repos()
        cell = self.cells(page)["detached"]
        self.assertTrue(cell.startswith("unknown "), cell)
        self.assertIn("HEAD is detached; the lag behind origin/main is counted on main only.", cell)
        self.assertNotIn("pull --ff-only", page)
        self.assertIn(" · 0 behind default", page)

    def test_another_branch_says_which_and_that_it_cannot_count(self):
        path = self.behind("parked")
        self.git(path, "switch", "-q", "-c", "feat/parked")
        page = self.repos()
        cell = self.cells(page)["parked"]
        self.assertTrue(cell.startswith("not on main "), cell)
        self.assertIn("On feat/parked; the lag behind origin/main is counted on main only.", cell)
        self.assertNotIn("pull --ff-only", page)

    def test_no_origin_head_and_no_origin_say_so(self):
        path = self.behind("headless")
        self.git(path, "remote", "set-head", "origin", "-d")
        cells = self.cells(self.repos())
        self.assertEqual(cells["headless"], "unknown No default branch: origin/HEAD is not set.")
        # The origin itself has no remote at all: the same unknown, never "current".
        self.assertEqual(cells["remote/origin"], "unknown No default branch: origin/HEAD is not set.")

    def test_a_stale_fetch_makes_a_zero_unknown_and_a_count_a_floor(self):
        lagging = self.behind("lagging")
        level = self.clone("level")
        self.git(level, "fetch", "-q")
        old = time.time() - 3 * 24 * 3600
        for path in (lagging, level):
            os.utime(path / ".git" / "FETCH_HEAD", (old, old))
        cells = self.cells(self.repos())
        self.assertTrue(cells["lagging"].startswith("behind at least 2 "), cells["lagging"])
        self.assertIn("fetched 3 d ago.", cells["lagging"])
        self.assertEqual(cells["level"], "unknown Level with origin/main as of the last fetch; fetched 3 d ago.")

    def test_a_fresh_fetch_at_zero_is_current(self):
        level = self.clone("level")
        self.git(level, "fetch", "-q")
        self.assertEqual(self.cells(self.repos())["level"], "current Level with origin/main; fetched 0 min ago.")

    def test_the_page_never_fetches(self):
        # A clone whose origin moved on, never fetched since: the page counts
        # from local refs and says no fetch is recorded, rather than fetching
        # in the request and reporting the true lag.
        path = self.clone("unfetched")
        self.advance(3)
        page = self.repos()
        self.assertEqual(self.cells(page)["unfetched"],
                         "unknown Level with origin/main as of the last fetch; no fetch recorded.")
        self.assertFalse((path / ".git" / "FETCH_HEAD").exists())
        # And a fetched one keeps its fetch time: the page read it, not rewrote it.
        lagging = self.behind("lagging", 1)
        old = time.time() - 7200
        os.utime(lagging / ".git" / "FETCH_HEAD", (old, old))
        self.repos()
        self.assertAlmostEqual((lagging / ".git" / "FETCH_HEAD").stat().st_mtime, old, delta=1)

    def test_the_state_is_derived_from_the_row_alone(self):
        now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        row = {"error": "", "default": "main", "branch": "main", "behind_default": 1, "dirty": 0,
               "ahead": 0, "truncated": [], "path": "/r/with space",
               "fetched_iso": (now - timedelta(hours=5)).isoformat()}
        self.assertEqual(repos_screen.primary(row, now),
                         ("behind", "behind 1", "1 commit behind origin/main; fetched 5 h ago.",
                          "git -C '/r/with space' pull --ff-only"))
        # A status cut at the ceiling does not prove a clean tree.
        state = repos_screen.primary({**row, "truncated": ["status"]}, now)
        self.assertEqual(state[3], "No pull offered: the tree's state was not fully read.")
        # An unread checkout says nothing here; its note says why.
        self.assertEqual(repos_screen.primary({**row, "error": "git log failed (exit 3)"}, now),
                         ("unknown", "?", "", ""))
        # The threshold: a day old still counts, a minute past it does not.
        edge = {**row, "behind_default": 0, "fetched_iso": (now - repos_screen.STALE_FETCH).isoformat()}
        self.assertEqual(repos_screen.primary(edge, now)[0], "current")
        edge["fetched_iso"] = (now - repos_screen.STALE_FETCH - timedelta(minutes=1)).isoformat()
        self.assertEqual(repos_screen.primary(edge, now)[0], "unknown")
