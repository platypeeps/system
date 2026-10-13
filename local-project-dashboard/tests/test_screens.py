"""The three screens: Today, Backlog and Item.

Criterion 12's clauses for these three sections, and requirement 5's readings.
Every assertion runs the router directly -- rows in, markup out -- because a
test that has to start a socket to read a page is a test that flakes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import reads, transition, update_assignment, upsert_shadow
from sd_db.writes import record_cost
from sd_dashboard.server import NotFound, route

from support import ScreenCase

DASHBOARD = Path(__file__).resolve().parents[1] / "sd_dashboard"


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


class OperationsUsageNumbers(ScreenCase):
    def setUp(self):
        super().setUp()
        self.repo()
        self.sent = self.item("finished, unsent", status="planning", repo="/repos/system")
        transition(self.connection, self.sent, "ready_to_send", who="sd-ship")
        self.running = self.item("in progress", status="planning", repo="/repos/system")
        transition(self.connection, self.running, "in_progress", who="the operator")
        self.later = self.item("planning, not due", status="planning")

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

    def test_usage_contains_provider_ranks(self):
        self.connection.execute(
            "INSERT INTO provider (name, enabled, author_rank) VALUES ('opus', 1, 1)"
        )
        self.connection.commit()
        page = self.render("/operations", {"area": ["usage"]})
        self.assertIn("Providers this month", page)
        self.assertIn("opus", page)
        self.assertIn("$/pass", page)


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

    def test_the_page_renders_inside_the_v2_shell(self):
        """sd:3586 PR 1: the item page is a v2 shell page under Tasks, on foundation tokens, with no classic chrome."""
        self.note(self.id, "a followup")
        page = self.render(f"/item/{self.id}")
        self.assertIn('<body data-page="Tasks" data-scope="tasks">', page)
        self.assertIn('<div class="shell"><main id="main" class="classic">', page)
        for asset in ("fonts.css", "tokens.css", "theme.js", "markup.js", "shell.css", "classic.css", "icons.js", "shell.js"):
            self.assertRegex(page, rf'"/ui/{re.escape(asset)}\?v=[0-9a-f]{{16}}"', asset)
        self.assertNotIn("/static/dashboard.css", page)
        self.assertNotIn('class="nav"', page)
        self.assertNotIn("page-footer", page)
        # The forms still post through dashboard.js, loaded before shell.js, which runs last; Details is the shell's last child.
        scripts = re.findall(r'<script src="([^"?]+)', page)
        self.assertEqual(scripts[-3:], ["/ui/sections.js", "/static/dashboard.js", "/ui/shell.js"])
        self.assertNotIn(" defer", page)
        self.assertRegex(page, r'<div class="details" id="details"><p class="note">[^<]+</p></div></div><script')
        self.assertEqual(page.count("<h1>"), 1)
        self.assertIn('data-palette-open aria-keyshortcuts="p"', page)
        self.assertIn('<form method="post" action="/api/items/', page)
        self.assertIn("<summary>CLI equivalents</summary>", page)

    def test_its_stylesheet_names_foundation_tokens(self):
        """sd:3586 PR 1: classic.css has no colour literal and names only declared tokens."""
        strip = lambda text: re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        css = strip((DASHBOARD / "v2/static/classic.css").read_text(encoding="utf-8"))
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b|\b(rgba?|hsla?|oklch|oklab|lab|lch|color)\(", css))
        declared = set(re.findall(r"(--[\w-]+)\s*:", (DASHBOARD / "v2/static/tokens.css").read_text(encoding="utf-8")))
        self.assertEqual(sorted(set(re.findall(r"var\((--[\w-]+)", css)) - declared), [])

    def test_the_title_is_escaped(self):
        nasty = self.item("<script>window.pwned=1</script>")
        page = self.render(f"/item/{nasty}")
        self.assertNotIn("<script>window.pwned", page)
        self.assertIn("&lt;script&gt;", page)


#: dashboard.js under a stand-in document: one filter field, no palette dialog, the shell's chord state settable.
KEYS_HARNESS = r"""
var window = this, OUT = { focused: 0, prevented: [] }, LISTENERS = {}, STATE = { chording: false, dialog: false };
window.shell = { chording: () => STATE.chording };
var field = { focus() { OUT.focused += 1; } };
var document = {
  querySelectorAll: () => [],
  querySelector: selector => selector === "[data-listing-filter]" ? field
    : selector === "dialog[open]" ? (STATE.dialog ? {} : null) : null,
  getElementById: () => null,
  addEventListener: (type, listener) => (LISTENERS[type] = LISTENERS[type] || []).push(listener),
};
var location = { pathname: "/item/1", search: "" };
SCRIPT
function press(key, target) {
  var event = { key, target: target || { tagName: "BODY" }, preventDefault() { OUT.prevented.push(key); } };
  (LISTENERS.keydown || []).forEach(listener => listener(event));
}
press("j"); press("k"); press("Enter"); press("?"); press("Escape");
OUT.afterShellKeys = OUT.focused;
press("/"); OUT.afterSlash = OUT.focused;
press("/", { tagName: "INPUT" }); OUT.whileTyping = OUT.focused;
STATE.chording = true; press("/"); STATE.chording = false; OUT.whileChording = OUT.focused;
STATE.dialog = true; press("/"); STATE.dialog = false; OUT.underADialog = OUT.focused;
OUT.pageKeys = window.PAGE_KEYS;
JSON.stringify(OUT);
"""


class TheServerDrawnScreensKeys(unittest.TestCase):
    """sd:3586 PR 6: every server-drawn screen is a shell page, so dashboard.js keeps only / and p for the shell."""

    def test_slash_focuses_the_filter_and_the_shell_keys_stay_the_shells(self):
        script = (DASHBOARD / "static/dashboard.js").read_text(encoding="utf-8")
        result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", KEYS_HARNESS.replace("SCRIPT", script)],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        # j, k, Enter, ? and Escape are shell.js's: this script neither acts on them nor swallows them.
        self.assertEqual(out["afterShellKeys"], 0)
        self.assertEqual(out["prevented"], ["/"])
        self.assertEqual(out["afterSlash"], 1)
        # Typing, a pending g-chord and an open dialog keep the key.
        self.assertEqual([out["whileTyping"], out["whileChording"], out["underADialog"]], [1, 1, 1])
        self.assertEqual(out["pageKeys"], [["/", "focus the notes filter"], ["p", "run a registered command"]])
        self.assertNotRegex(script, r"keymap|data-active|\bshelled\b")


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
