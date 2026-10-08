"""The three screens: Today, Backlog and Item.

Criterion 12's clauses for these three sections, and requirement 5's readings.
Every assertion runs the router directly -- rows in, markup out -- because a
test that has to start a socket to read a page is a test that flakes.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from sd_db import reads, transition, update_assignment, upsert_shadow
from sd_db.writes import record_cost
from sd_dashboard.server import NotFound, route

from support import ScreenCase


def listing_content(page, name):
    """Read the displayed list, separately from Capture's all-item selector."""
    return re.search(r'<section[^>]*data-listing="' + name + r'"[^>]*>.*?</section>', page).group(0)


def maintenance_children(checkout):
    """The maintenance or gc children one commit in `checkout` starts, from trace2.

    An unconfigured repository starts `git maintenance run --auto --detach`
    after a commit. That child holds `.git/objects/maintenance.lock` while it
    runs, and a `copytree` of the checkout races it (sd:1454).
    """
    with tempfile.TemporaryDirectory() as scratch:
        trace = Path(scratch) / "trace.json"
        subprocess.run(["git", "-C", str(checkout), "commit", "--allow-empty", "-qm", "probe"],
                       check=True, capture_output=True,
                       env={**os.environ, "GIT_TRACE2_EVENT": str(trace)})
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    return [event["argv"] for event in events if event.get("event") == "child_start"
            and {"maintenance", "gc"} & set(event.get("argv", []))]


class Today(ScreenCase):
    def setUp(self):
        super().setUp()
        self.repo()
        self.sent = self.item("finished, unsent", status="planning", repo="/repos/system")
        transition(self.connection, self.sent, "ready_to_send", who="sd-ship")
        self.running = self.item("in progress", status="planning", repo="/repos/system")
        transition(self.connection, self.running, "in_progress", who="the operator")
        self.later = self.item("planning, not due", status="planning")

    def test_the_screen_renders_the_library_query_in_the_library_order(self):
        """Criterion 4's shape, on the half this repository can assert.

        `sd today` is the pack's verb and lands in the pack's half of this
        pull request, so the two-surface comparison cannot run here. What runs
        here is the half that makes it possible: the screen renders
        `reads.today_items` and does not sort it again, asserted by reading
        the ids off the page in the order they appear.
        """
        expected = [row["id"] for row in reads.today_items(self.connection, now=self.now)]
        self.assertEqual(expected, [self.sent, self.running])

        page = self.render("/classic/today")
        positions = [page.index(f'href="/item/{item}"') for item in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn(f'href="/item/{self.later}"', page)

    def test_finished_unsent_comes_first_with_days_since_ready(self):
        page = listing_content(self.render("/classic/today"), "today")
        self.assertLess(page.index("finished, unsent"), page.index("in progress"))
        self.assertIn("In status", page)

    def test_usage_has_the_four_weekly_numbers_with_inputs_in_a_detail_view(self):
        """Criterion 15's rendering clause, and requirement 5's hover rule.

        The prd offers "on hover or in a detail view" and requirement 5
        forbids a hover-only affordance, so only one of the two is allowed:
        the inputs are in a `details` element, and there is no `title`
        attribute anywhere on the page to hold a tooltip.
        """
        page = self.render("/operations", {"area": ["usage"]})
        for label in ("cost per shipped item", "framework commits per shipped item",
                      "assignment to merge", "reverts"):
            self.assertIn(label, page)
        self.assertGreaterEqual(page.count("<details"), 4)
        self.assertGreaterEqual(page.count("<summary>Inputs</summary>"), 4)
        self.assertNotIn(" title=", page)

    def test_the_cost_tile(self):
        self.assertIn("cost-tile", self.render("/operations", {"area": ["usage"]}))

    def test_open_followups(self):
        self.note(self.running, "chase the reviewer")
        page = self.render("/classic/today")
        self.assertIn("chase the reviewer", page)

    def test_the_runner_board_renders_rows_the_runner_does_not_yet_write(self):
        """The board exists before the runner does, which is the point of it."""
        first = self.assignment(self.running, status="running", provider="opus")
        second = self.assignment(self.sent, status="queued", after=first)
        page = self.render("/classic/today")
        self.assertIn("The runner", page)
        for lane in ("queued", "running", "blocked", "done"):
            self.assertIn(f"{lane} (", page)
        self.assertIn(f"waiting on assignment {first}", page)
        self.assertIn(str(second), page)

    def test_a_queued_row_says_which_repository_holds_it(self):
        held = self.assignment(self.running, status="running")
        other = self.item("second in the same repo", repo="/repos/system")
        self.assignment(other, status="queued")
        page = self.render("/classic/today")
        self.assertIn(f"held by assignment {held}", page)

    def test_the_timeline_is_svg_the_server_rendered(self):
        one = self.assignment(self.running, status="running", budget_minutes=30)
        update_assignment(self.connection, one, started=f"{self.now[:10]}T09:00:00Z")
        page = self.render("/classic/today")
        self.assertIn("chart-timeline", page)
        self.assertIn("<svg", page)
        self.assertNotIn("<canvas", page)

    def test_usage_contains_provider_ranks_and_today_omits_the_usage_section(self):
        self.connection.execute(
            "INSERT INTO provider (name, enabled, author_rank) VALUES ('opus', 1, 1)"
        )
        self.connection.commit()
        page = self.render("/operations", {"area": ["usage"]})
        self.assertIn("Providers this month", page)
        self.assertIn("opus", page)
        self.assertIn("$/pass", page)
        today = self.render("/classic/today")
        for label in ("Week, cost and provider details", "Providers this month",
                      "cost per shipped item", "cost-tile"):
            self.assertNotIn(label, today)

    def test_an_empty_today_says_so(self):
        from sd_db import progress, upsert_repo

        upsert_repo(self.connection, "/repos/system", status_source="row")
        progress.cancel_work(self.connection, self.sent, reason="Fixture complete", who="operator")
        progress.cancel_work(self.connection, self.running, reason="Fixture complete", who="operator")
        page = self.render("/classic/today")
        self.assertIn("Nothing is due and nothing is in progress.", page)


class OperationsUsage(ScreenCase):
    def test_relocated_details_keep_fixture_totals_provider_values_and_clock(self):
        self.provider("fixture-provider", author_rank=2, reviewer_rank=1)
        self.connection.execute("INSERT INTO bill VALUES ('fixture-plan', 'api', 100)")
        first = self.item("First delivery")
        second = self.item("Second delivery")
        self.connection.execute("UPDATE item SET shipped_at = '2026-09-04T12:00:00Z'")
        assignment = self.assignment(first, provider="fixture-provider")
        other_assignment = self.assignment(second)
        update_assignment(self.connection, assignment, started="2026-09-04T10:00:00Z")
        update_assignment(self.connection, other_assignment, started="2026-09-04T08:00:00Z")
        record_cost(self.connection, source="run", provider="fixture-provider", bill="fixture-plan",
                    assignment=assignment, pass_="review-1", usd=9)
        record_cost(self.connection, source="reserved", bill="fixture-plan", usd=2)
        self.connection.execute("UPDATE cost SET timestamp = '2026-09-04T12:00:00Z'")
        self.connection.commit()
        before = tuple(self.connection.iterdump())

        page = self.render("/operations", {"area": ["usage"]})
        for expected in ("Week, cost and provider details", 'class="tile-value">$4.50',
                         'class="tile-value">3.0 h', "shipped items</dt><dd>2",
                         "fixture-plan: $9.00 of $100.00 (+$2.00 reserved)",
                         "<td>fixture-provider</td><td>2</td><td>1</td><td>1</td><td>0</td><td>$9.00"):
            self.assertIn(expected, page)
        self.assertNotIn("Age in status", page)
        self.assertNotIn("Jobs needing attention", page)
        relocated = self.render("/classic/today")
        self.assertNotIn("Week, cost and provider details", relocated)
        self.assertNotIn("fixture-plan:", relocated)
        self.assertEqual(tuple(self.connection.iterdump()), before)


class Backlog(ScreenCase):
    def setUp(self):
        super().setUp()
        self.repo()
        self.ids = []
        for index in range(6):
            item = self.item(f"backlog {index}", repo="/repos/system", priority=index % 4)
            self.ids.append(item)
        transition(self.connection, self.ids[1], "ready_to_send", who="a test")
        transition(self.connection, self.ids[2], "blocked", who="a test")
        transition(self.connection, self.ids[3], "in_progress", who="a test")

    def test_urgency_is_derived_exactly_as_the_prd_says(self):
        rows = {row["id"]: row for row in reads.backlog_items(self.connection, now=self.now)}
        soon = self.item("due in three days", due="2026-09-08")
        rows = {row["id"]: row for row in reads.backlog_items(self.connection, now=self.now)}
        self.assertTrue(reads.is_urgent(rows[soon], now=self.now))
        self.assertFalse(reads.is_urgent(rows[self.ids[0]], now=self.now))

    def test_ready_to_send_is_its_own_series_in_the_histogram(self):
        page = self.render("/operations", {"area": ["progress"]})
        self.assertIn("chart-bar-ready-to-send", page)


class Item(ScreenCase):
    def setUp(self):
        super().setUp()
        self.repo()
        self.id = self.item("an item with a history", repo="/repos/system")

    def test_notes_in_order(self):
        self.note(self.id, "first")
        self.note(self.id, "second", kind="decision")
        page = self.render(f"/item/{self.id}")
        self.assertLess(page.index("first"), page.index("second"))

    def test_assignments_and_their_cost(self):
        one = self.assignment(self.id, status="done", provider="opus")
        record_cost(self.connection, source="run", provider="opus", assignment=one, usd=1.5)
        record_cost(self.connection, source="bound", provider="opus", assignment=one, usd=0.5)
        page = self.render(f"/item/{self.id}")
        self.assertIn("$2.00", page)
        self.assertIn("estimated", page)

    def test_the_shadow_when_it_has_one(self):
        from sd_db import set_item_fields

        set_item_fields(self.connection, self.id, source="github",
                        external_id="https://example.invalid/i/1")
        upsert_shadow(self.connection, tracker="github",
                      url="https://example.invalid/i/1", state="open", title="an issue")
        page = self.render(f"/item/{self.id}")
        self.assertIn("External context", page)
        self.assertIn("Sync health", page)
        self.assertIn("Last successful sync", page)
        self.assertIn("https://example.invalid/i/1", page)

    def test_the_artifact_is_rendered_through_the_subset(self):
        from sd_db import set_item_fields

        set_item_fields(self.connection, self.id, repo=None, path=None,
                        body="# Heading\n\n<script>x</script>\n")
        page = self.render(f"/item/{self.id}")
        self.assertIn("<h3>Heading</h3>", page)
        self.assertNotIn("<script>x</script>", page)

    def test_a_missing_item_is_a_404_and_not_a_stack_trace(self):
        with self.assertRaises(NotFound):
            route(self.connection, "/item/9999", {}, now=self.now)
        with self.assertRaises(NotFound):
            route(self.connection, "/item/../../etc/passwd", {}, now=self.now)

    def test_the_title_is_escaped(self):
        nasty = self.item("<script>window.pwned=1</script>")
        page = self.render(f"/item/{nasty}")
        self.assertNotIn("<script>window.pwned", page)
        self.assertIn("&lt;script&gt;", page)


class TheItemPageUnderASecondHome(ScreenCase):
    """sd:1439 criterion 4: a row written under `HOME=A` and read under `B`.

    The item's artifact is read with `git -C <repo> show`. The row holds the
    `~/` key, so the page reads the checkout under this process's home; a
    site that opened the stored value as a path would fall back to the row's
    body, which here says so.
    """

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name).resolve()
        first = root / "a"
        checkout = first / "repos/one"
        prd = checkout / "docs/work/2026-09-24-x/prd.md"
        prd.parent.mkdir(parents=True)
        prd.write_text("# Read from git\n", encoding="utf-8")
        # No background maintenance: its lock file races the copytree below (sd:1454).
        for argv in (["init", "-q", "-b", "main"], ["config", "maintenance.auto", "false"],
                     ["config", "gc.auto", "0"], ["config", "user.email", "fixture@example.invalid"],
                     ["config", "user.name", "Fixture"], ["add", "-A"], ["commit", "-qm", "x"]):
            subprocess.run(["git", "-C", str(checkout), *argv], check=True, capture_output=True)
        second = root / "b"
        shutil.copytree(first, second, symlinks=True)
        shutil.rmtree(first)
        patcher = mock.patch.dict(os.environ, {"HOME": str(second)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo("~/repos/one")
        self.id = self.item("an item from the other home", repo="~/repos/one",
                            path="docs/work/2026-09-24-x/prd.md", body="the row's own body")

    def test_the_fixture_checkout_starts_no_background_maintenance(self):
        # sd:1454: a detached `git maintenance` held objects/maintenance.lock
        # while `copytree` walked this fixture, and CI raised shutil.Error.
        self.assertEqual(maintenance_children(Path(os.environ["HOME"]) / "repos/one"), [])

    def test_the_artifact_is_read_from_the_checkout_under_this_home(self):
        page = self.render(f"/item/{self.id}")
        self.assertIn("Read from git", page)
        self.assertNotIn("the row&#x27;s own body", page)
        self.assertNotIn("the row's own body", page)


class TheHistogramBarIsAFilter(ScreenCase):
    """Requirement 5: "a bar is a filter on the list". Follow the bar and look.

    The bar used to carry its own *label* into the list's text filter --
    `q=3-6d`, matched against the rendered cells, where `In status` renders
    `5d` and never `3-6d`. Every bar filtered to an empty page, and no test
    noticed because no test followed a bar. `test_v2_tasks.TheOperationsBars`
    follows every bar into Tasks; this class checks what each bar carries.
    """

    #: One row per bucket, plus two that make the `ready_to_send` series real.
    DAYS = (0, 2, 5, 10, 20, 40, 90)

    def setUp(self):
        super().setUp()
        for days in self.DAYS:
            self.age(self.item(f"aged {days} days", status="planning"), days)
        for days in (5, 20):
            row = self.item(f"unsent for {days} days", status="planning")
            transition(self.connection, row, "ready_to_send", who="sd-ship")
            self.age(row, days)

    # -- reading the page --------------------------------------------------

    def progress(self, parameters=None):
        return self.render("/operations", {"area": ["progress"], **(parameters or {})})

    def bars(self, page: str) -> list[tuple[str, str, str]]:
        """Every histogram bar as (href, bucket key, series), off the markup."""
        found = []
        for opening in re.findall(r"<a\s[^>]*>", page):
            href = re.search(r'href="([^"]*)"', opening)
            age = re.search(r'data-age="([^"]*)"', opening)
            series = re.search(r'data-series="([^"]*)"', opening)
            if href and age and series:
                found.append((html.unescape(href.group(1)), age.group(1), series.group(1)))
        return found

    # -- the assertions ----------------------------------------------------

    def test_the_fixture_actually_spans_buckets(self):
        """Guard on the test itself: buckets that all collapse to one prove
        nothing about a filter, and every freshly seeded row is zero days old
        unless the fixture backdates it."""
        rows = reads.backlog_items(self.connection, now=self.now)
        buckets = {reads.age_bucket(row, now=self.now) for row in rows}
        self.assertGreaterEqual(len(buckets), 5, sorted(buckets))

    def test_a_bar_carries_a_facet_and_not_a_search_term(self):
        """The regression in the form it was found in.

        `q=3-6d` is the defect: a range label in a substring filter over the
        rendered cells. The assertion is that no bar puts its label in `q`,
        and that what it does carry is a bucket key the screen validates.
        """
        keys = {str(lower) for lower, _ in reads.age_bounds()}
        for href, key, _ in self.bars(self.progress()):
            query = parse_qs(urlsplit(href).query)
            self.assertNotIn("q", query, f"{href} filters by text")
            self.assertEqual(query.get("age"), [key])
            self.assertEqual(query.get("active"), ["1"])
            self.assertIn(key, keys)

    def test_progress_scope_ignores_backlog_filters_and_excludes_done_and_parked(self):
        done = self.item("recently completed", status="done")
        self.age(done, 5)
        parked = self.item("parked idea", kind="idea")
        self.age(parked, 20)
        self.connection.execute("UPDATE item SET parked_at=? WHERE id=?", (self.now, parked))
        self.connection.commit()
        page = self.progress({"q": ["unsent"], "status": ["ready_to_send"], "age": ["3"]})
        self.assertIn("All active items across repositories", page)
        self.assertIn("Task filters do not affect this chart", page)
        self.assertIn('role="img"', page)
        totals = re.findall(r'<text[^>]*class="chart-value"[^>]*>([0-9]+)</text>', page)
        self.assertEqual([int(value) for value in totals], [1, 1, 2, 1, 2, 1, 1])
        self.assertEqual(self.bars(page), self.bars(self.progress()))
        for href, _, _ in self.bars(page):
            self.assertNotIn("q=", href)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
