"""Now: the pack's ranking on the Today page (sd:719 step 6).

The rank numbers and the band boundaries are the pack's (`dashboard/now.py`
and the `band` function of `dashboard/app.js` at 85c4fa1b), asserted as numbers so a
port that moved one is a red test and not a quieter page. The sources are
the shapes #427 and #411 built: the fleet child and the shadow table. Nothing
here opens a database of its own; the criterion 2 grep in
`tests/test_markup.py` is the check.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

from sd_db import contributions, progress, upsert_shadow
from sd_dashboard import fleet, now_screen

from fleet_support import FleetCase
from support import NOW, ScreenCase
from test_workflow_actions import BrowserSession

# The pack's numbers, copied by hand from `dashboard/now.py` at 85c4fa1b so
# a drift in the port is a red test and not a page that quietly ranks
# differently from the one it replaced.
PACK_RANKS = {"AHEAD": 3, "DIRTY": 4, "STALE": 2, "FRESH": 4, "ABANDONED": 3, "STALE_DAYS": 14}


def repo(name, *, ahead=0, dirty=0, branch="main", last="2026-09-01", error=""):
    return {"name": name, "group": ".", "path": f"/repos/{name}", "branch": branch,
            "dirty": dirty, "ahead": ahead, "behind": 0, "last": last, "last_iso": f"{last}T10:00:00+00:00",
            "subject": "a commit", "author": "someone", "web": "", "truncated": [], "error": error}


def tree(name, *, live=True):
    state = "live" if live else "abandoned"
    return {"repo": "system", "name": name, "path": f"/wt/{name}", "branch": name, "live": live, "state": state}


def fleet_document(area, repos=(), trees=()):
    if area == "repos":
        return {"root": "/repos", "rootExists": True, "repos": list(repos),
                "counts": {"repos": len(repos), "dirty": 0, "ahead": 0, "unread": 0}}
    return {"root": "/repos", "rootExists": True, "worktrees": list(trees), "processes": [],
            "processes_error": "", "abandoned": sum(1 for row in trees if not row["live"]),
            "counts": {"worktrees": len(trees), "processes": 0}}


def pull(number, *, first_seen, needs_you=True, repo="example/project"):
    row = {"tracker": "github", "repo": repo, "url": f"https://github.com/{repo}/pull/{number}",
           "number": number, "kind": "pull", "title": f"Pull {number}", "state": "open",
           "author": "someone", "first_seen": first_seen, "last_seen": NOW, "why": []}
    if needs_you is not None:
        row["needs_you"] = needs_you
    return row


class Ranking(ScreenCase):
    """The pure half: rows in, ranked rows out, the band from the rank alone."""

    def test_the_rank_numbers_and_the_band_boundaries_are_the_packs(self):
        for name, value in PACK_RANKS.items():
            self.assertEqual(getattr(now_screen, name), value, name)
        self.assertEqual(now_screen.DARK, 0)
        # The pack's `dashboard/app.js` `band`: <= 1 broken, <= 3 look, else queued.
        self.assertEqual([now_screen.band(rank) for rank in range(0, 6)],
                         ["broken", "broken", "look", "look", "queued", "queued"])
        self.assertEqual(now_screen.band(now_screen.DARK), "broken")
        self.assertEqual(now_screen.band(now_screen.STALE), "look")
        self.assertEqual(now_screen.band(now_screen.AHEAD), "look")
        self.assertEqual(now_screen.band(now_screen.ABANDONED), "look")
        # DIRTY and FRESH tie into one band on purpose (the pack's comment).
        self.assertEqual(now_screen.band(now_screen.DIRTY), "queued")
        self.assertEqual(now_screen.band(now_screen.FRESH), "queued")
        self.assertEqual(now_screen.BANDS, ("broken", "look", "queued"))

    def test_backbone_rows_fold_ahead_and_dirty_into_one_row_at_the_louder_rank(self):
        rows = now_screen.backbone_rows([
            repo("both", ahead=2, dirty=3, branch="feat/x"),
            repo("dirty-only", dirty=1),
            repo("clean"),
            repo("unread", dirty=None, ahead=None, error="git status failed (exit 128)"),
        ])
        self.assertEqual([(row["rank"], row["id"], row["source"]) for row in rows], [
            (now_screen.AHEAD, "ahead:both:2", "repos"),
            (now_screen.DIRTY, "dirty:dirty-only:1", "repos"),
        ])
        self.assertEqual(rows[0]["what"], "both has 2 unpushed commits")
        self.assertEqual(rows[0]["detail"], "feat/x · 3 dirty files")
        self.assertEqual(rows[1]["what"], "dirty-only has 1 uncommitted file")
        self.assertEqual(rows[1]["detail"], "main · last commit 2026-09-01")

    def test_pr_rows_take_the_pulls_that_need_you_and_rank_the_quiet_ones_louder(self):
        today = "2026-09-20"
        rows = now_screen.pr_rows([
            pull(1, first_seen="2026-09-01T09:00:00+00:00"),
            pull(2, first_seen="2026-09-18T09:00:00+00:00"),
            pull(3, first_seen="2026-09-01T09:00:00+00:00", needs_you=False),
            pull(4, first_seen="2026-09-01T09:00:00+00:00", needs_you=None),
            {**pull(5, first_seen="2026-09-01T09:00:00+00:00"), "kind": "issue"},
            pull(6, first_seen="not a date"),
        ], today)
        self.assertEqual([(row["rank"], row["id"]) for row in rows], [
            (now_screen.STALE, "pr:example/project#1:2"),
            (now_screen.FRESH, "pr:example/project#2:4"),
            (now_screen.FRESH, "pr:example/project#6:4"),
        ])
        self.assertEqual(rows[0]["what"], "example/project#1 open, first seen 19d ago")
        self.assertEqual(rows[1]["what"], "example/project#2 open")
        self.assertEqual(rows[0]["detail"], "Pull 1")
        self.assertTrue(all(row["source"] == "prs" for row in rows))
        # The boundary is the pack's STALE_DAYS, inclusive.
        edge = now_screen.pr_rows([pull(7, first_seen="2026-09-06T00:00:00+00:00")], today)
        self.assertEqual(edge[0]["rank"], now_screen.STALE)
        inside = now_screen.pr_rows([pull(8, first_seen="2026-09-07T00:00:00+00:00")], today)
        self.assertEqual(inside[0]["rank"], now_screen.FRESH)

    def test_session_rows_are_one_row_for_every_abandoned_worktree(self):
        self.assertEqual(now_screen.session_rows([tree("a"), tree("b")]), [])
        rows = now_screen.session_rows([tree("a", live=False), tree("b"), tree("c", live=False)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rank"], now_screen.ABANDONED)
        self.assertEqual(rows[0]["id"], "worktrees:2")
        self.assertEqual(rows[0]["what"], "2 abandoned worktrees")
        self.assertEqual(rows[0]["source"], "sessions")
        self.assertIn("git worktree prune", rows[0]["detail"])

    def test_merge_sorts_on_rank_then_id_so_ties_are_stable(self):
        rows = [{"rank": 4, "id": "b"}, {"rank": 0, "id": "z"}, {"rank": 4, "id": "a"}, {"rank": 2, "id": "m"}]
        self.assertEqual([row["id"] for row in now_screen.merge(rows)], ["z", "m", "a", "b"])

    def test_a_dark_row_is_rank_zero_and_names_the_collector_and_the_reason(self):
        row = now_screen.dark_row("repos", "fleet collector exited 3: disk on fire")
        self.assertEqual(row["rank"], now_screen.DARK)
        self.assertEqual(row["id"], "dark:repos")
        self.assertEqual(row["source"], "repos")
        self.assertEqual(row["what"], "repos could not be read")
        self.assertEqual(row["detail"], "fleet collector exited 3: disk on fire")
        self.assertEqual(now_screen.band(row["rank"]), "broken")


class Document(ScreenCase):
    """The merged document: three sources, each guarded, every row banded."""

    def fixture_fleet(self, repos=(), trees=()):
        return lambda area: fleet_document(area, repos=repos, trees=trees)

    def seed_pull(self, number, *, first_seen, needs_you=True):
        url = f"https://github.com/example/project/pull/{number}"
        if needs_you:
            contributions.capture(self.connection, title=f"Patch {number}", changes={"pull_url": url}, who="operator")
            observation = {"complete": True, "observed_at": "2026-09-09T12:00:00Z",
                "operator": {"id": "1", "login": "author"}, "author": {"id": "1", "login": "author"},
                "repo": "example/project", "title": f"Upstream {number}", "state": "open", "head": "a" * 40,
                "base": "b" * 40, "draft": False, "mergeable": "mergeable", "ci": "success", "ci_head": "a" * 40,
                "ci_ids": ["check:1"], "why": ["author"], "blocking_labels": [], "labels": [], "reviews": [],
                "events": [{"id": f"comment:{number}", "at": "2026-09-09T11:00:00Z", "actor_id": "2", "kind": "comment",
                    "maintainer": True, "mentions_operator": False, "url": url + "#issuecomment-1"}]}
            contributions.observe_pull(self.connection, url, observation,
                expected_revision=contributions.snapshot(self.connection, "github:" + url)["revision"])
        upsert_shadow(self.connection, tracker="github", url=url, repo="example/project", number=number,
                      kind="pull", title=f"Upstream {number}", state="open", author="author")
        # `upsert_shadow` stamps the real clock; the fixture backdates the
        # column, which is seeding and not a second writer.
        self.connection.execute("UPDATE shadow SET first_seen = ? WHERE url = ?", (first_seen, url))
        self.connection.commit()

    def test_the_document_merges_the_three_sources_and_bands_every_row(self):
        self.seed_pull(14, first_seen="2026-08-01T09:00:00+00:00")
        self.seed_pull(15, first_seen="2026-09-05T09:00:00+00:00")
        self.seed_pull(16, first_seen="2026-08-01T09:00:00+00:00", needs_you=False)
        rows = progress.tracker_items(self.connection, tracker="github")
        self.assertEqual({row["number"]: row.get("needs_you") for row in rows}, {14: True, 15: True, 16: None})
        with patch.object(progress, "tracker_items", wraps=progress.tracker_items) as shadow:
            document = now_screen.document(self.connection, now=NOW, fleet=self.fixture_fleet(
                repos=[repo("pushy", ahead=1), repo("messy", dirty=2)],
                trees=[tree("gone", live=False)]))
        shadow.assert_called_once_with(self.connection, tracker="github")
        self.assertEqual(set(document), {"rows", "sources", "now"})
        self.assertEqual(document["now"], NOW)
        self.assertEqual([(row["rank"], row["id"], row["band"]) for row in document["rows"]], [
            (2, "pr:example/project#14:2", "look"),
            (3, "ahead:pushy:1", "look"),
            (3, "worktrees:1", "look"),
            (4, "dirty:messy:2", "queued"),
            (4, "pr:example/project#15:4", "queued"),
        ])
        self.assertEqual(document["sources"], {"repos": "", "sessions": "", "prs": ""})
        for row in document["rows"]:
            self.assertEqual(set(row), {"rank", "band", "kind", "id", "what", "detail", "source"})

    def test_an_empty_fleet_and_no_pulls_is_an_empty_list_with_every_source_read(self):
        document = now_screen.document(self.connection, now=NOW, fleet=self.fixture_fleet())
        self.assertEqual(document["rows"], [])
        self.assertEqual(document["sources"], {"repos": "", "sessions": "", "prs": ""})

    def test_a_collector_that_refuses_is_a_rank_zero_row_and_the_others_still_render(self):
        def fleet_backend(area):
            if area == "repos":
                raise ValueError("fleet collection was stopped at its budget: python ran past 12 seconds")
            return fleet_document(area, trees=[tree("gone", live=False)])

        document = now_screen.document(self.connection, now=NOW, fleet=fleet_backend)
        self.assertEqual([(row["rank"], row["id"], row["band"]) for row in document["rows"]], [
            (0, "dark:repos", "broken"),
            (3, "worktrees:1", "look"),
        ])
        self.assertEqual(document["rows"][0]["what"], "repos could not be read")
        self.assertEqual(document["rows"][0]["detail"],
                         "fleet collection was stopped at its budget: python ran past 12 seconds")
        self.assertEqual(document["sources"]["repos"], document["rows"][0]["detail"])

    def test_an_incomplete_fleet_document_is_dark_and_never_read_as_calm(self):
        document = now_screen.document(self.connection, now=NOW,
                                       fleet=lambda area: {"root": "/repos", "rootExists": True})
        self.assertEqual([row["id"] for row in document["rows"]], ["dark:repos", "dark:sessions"])
        self.assertIn("incomplete", document["rows"][0]["detail"])

    def test_a_shadow_read_that_fails_is_a_rank_zero_row(self):
        with patch.object(progress, "tracker_items", side_effect=ValueError("shadow table unreadable")):
            document = now_screen.document(self.connection, now=NOW, fleet=self.fixture_fleet())
        self.assertEqual([(row["rank"], row["id"], row["detail"]) for row in document["rows"]],
                         [(0, "dark:prs", "shadow table unreadable")])


class DarkCollector(FleetCase):
    """A fixture collector that exits non-zero: the row the design owes (step 6)."""

    def test_a_fleet_child_that_exits_non_zero_is_a_visible_row_naming_it(self):
        script = Path(self.tmp.name) / "refusing_fleet.py"
        script.write_text("import sys\nprint('fixture collector refused: ' + sys.argv[1], file=sys.stderr)\nsys.exit(3)\n")
        with patch.object(fleet, "FLEET", script):
            document = now_screen.document(self.connection, now=NOW)
        self.assertEqual([(row["rank"], row["id"], row["band"]) for row in document["rows"]],
                         [(0, "dark:repos", "broken"), (0, "dark:sessions", "broken")])
        self.assertEqual(document["rows"][0]["detail"], "fixture collector refused: repos")
        self.assertEqual(document["rows"][1]["detail"], "fixture collector refused: sessions")

    def test_the_real_fleet_child_feeds_the_ranking(self):
        pushy = self.checkout("pushy")
        clone = self.root / "clone"
        self.git(self.root, "clone", "-q", str(pushy), str(clone))
        self.configure(clone)
        (clone / "README").write_text("more\n")
        self.git(clone, "commit", "-q", "-am", "ahead by one")
        messy = self.checkout("messy")
        (messy / "scratch").write_text("x\n")
        self.abandoned(messy, "gone", "feat/gone")
        document = now_screen.document(self.connection, now=NOW)
        self.assertEqual([(row["rank"], row["id"]) for row in document["rows"]], [
            (3, "ahead:clone:1"), (3, "worktrees:1"), (4, "dirty:messy:1"),
        ])
        self.assertEqual(document["sources"], {"repos": "", "sessions": "", "prs": ""})

    def test_a_fixture_commit_leaves_the_abandoned_registration_in_place(self):
        """sd:1272: git 2.54's auto maintenance prunes a registration with no
        directory and no index -- the one `abandoned` writes. After a fixture
        commit it ran detached, so under load it could land after
        `abandoned` and drop the `worktrees:1` row. `autoDetach` off makes
        that maintenance run inside the commit, so the race is deterministic."""
        messy = self.checkout("messy")
        self.git(messy, "config", "maintenance.autoDetach", "false")
        self.abandoned(messy, "gone", "feat/gone")
        (messy / "README").write_text("again\n")
        self.git(messy, "commit", "-q", "-am", "a later commit")
        self.assertTrue((messy / ".git" / "worktrees" / "gone").is_dir())
        document = now_screen.document(self.connection, now=NOW)
        self.assertIn((3, "worktrees:1"), [(row["rank"], row["id"]) for row in document["rows"]])


class TodayPage(ScreenCase):
    """The Now section sits at the top of Today and says how it is filled."""

    def test_now_is_the_first_section_of_today_and_names_its_sources(self):
        page = self.render("/")
        section = re.search(r'<section[^>]*id="now"[^>]*>.*?</section>', page, re.DOTALL)
        self.assertIsNotNone(section, "no Now section")
        self.assertLess(page.index('id="now"'), page.index("Your work today"))
        self.assertLess(page.index('id="now"'), page.index('id="capture"'))
        self.assertIn('data-now="/api/now"', section.group(0))
        self.assertIn("<h2>Now</h2>", section.group(0))
        self.assertIn("<noscript>", section.group(0))
        for area in ("Repos", "Sessions", "Trackers"):
            self.assertIn(area, section.group(0))
        self.assertIn("Reading the fleet", section.group(0))
        self.assertIn('data-now-refresh', section.group(0))

    def test_the_page_render_reads_no_fleet(self):
        with patch.object(fleet, "collect", side_effect=AssertionError("Today ran the fleet child")):
            self.render("/")

    def test_the_script_paints_the_band_the_server_computed(self):
        script = (Path(now_screen.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("data-now", script)
        self.assertIn("row.band", script)
        # The band is a server-side number's name; the script holds no threshold of its own.
        self.assertNotRegex(script, r"rank\s*<=?\s*[0-9]")


class NowApi(BrowserSession):
    fleet_backend = staticmethod(lambda area: fleet_document(area, repos=[repo("pushy", ahead=1)]))

    def test_the_document_is_served_to_a_session_and_refused_without_one(self):
        status, headers, body = self.request("/api/now", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        document = json.loads(body)
        self.assertEqual([(row["id"], row["band"]) for row in document["rows"]], [("ahead:pushy:1", "look")])
        self.assertEqual(self.request("/api/now")[0], 403)
        self.assertEqual(self.request("/api/now?x=1", headers={"Cookie": self.cookie})[0], 400)
        self.assertEqual(self.request("/api/now", headers={"Host": "evil.invalid", "Cookie": self.cookie})[0], 403)

    def test_the_read_writes_nothing(self):
        before = self.snapshot()
        self.assertEqual(self.request("/api/now", headers={"Cookie": self.cookie})[0], 200)
        self.assertEqual(self.snapshot(), before)
