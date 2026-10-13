"""The classic screens whose controls moved are retired (sd:3402, slice 10 of sd:1333).

What this slice promises: each retired address answers 404, as `/classic/backlog` does (`test_backlog_retired`), the
palette no longer offers it, and no page, script or server module links to it. A link built from a variable
(`/operations?area=${...}`) counts as a link to a retired area: only the kept areas may follow `area=`. The kept
screens still answer: Contributions (classic), Usage, Services, Resources, Trackers, Sessions and Protection hold data
no v2 page shows; Services holds each service's last request and Sessions the live worktree rows.
Commands retired because Activity reads the same journal, `runner_exec.execution_journal` under the same cap, as
`runner_exec.executions`, which the classic area listed; the last test holds that.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner_exec

from sd_dashboard import activity_screen, operations_screen, pages, server, v2

from support import NOW, ScreenCase

PACKAGE = Path(server.__file__).resolve().parent

RETIRED_CLASSIC = ("designs", "documents", "skills", "today", "writing")
RETIRED_AREAS = ("jobs", "ports", "progress", "repos", "reports", "commands")
KEPT_CLASSIC = ("contributions",)
KEPT_AREAS = ("usage", "services", "resources", "trackers", "sessions")


def sources():
    """Every server module, page template and page script: where a link to a retired screen could live."""
    yield from PACKAGE.rglob("*.py")
    yield from (PACKAGE / "v2").glob("*.html")
    yield from (PACKAGE / "v2" / "static").glob("*.js")
    yield from (PACKAGE / "static").glob("*.js")


class TheRetiredScreens(ScreenCase):
    def test_each_retired_address_answers_404(self):
        for name in RETIRED_CLASSIC:
            with self.subTest(path=f"/classic/{name}"), self.assertRaises(server.NotFound):
                self.render(f"/classic/{name}")
        for area in RETIRED_AREAS:
            with self.subTest(area=area), self.assertRaises(server.NotFound):
                self.render("/operations", {"area": [area]})

    def test_the_palette_offers_no_retired_address(self):
        offered = set(v2.CLASSIC.values()) | set(v2.SCREENS.values())
        retired = {f"/classic/{name}" for name in RETIRED_CLASSIC} | {f"/operations?area={area}" for area in RETIRED_AREAS}
        self.assertEqual(offered & retired, set())
        self.assertEqual([entry.path for page in v2.registry.PAGES for entry in page.takes], ["/classic/contributions"])

    def test_no_page_script_or_module_links_to_a_retired_address(self):
        # A bare "/classic/" or "?area=" names no screen. Operations' own nav is built from AREAS, the kept areas.
        self.assertEqual(tuple(key for key, _ in operations_screen.AREAS), KEPT_AREAS)
        found = []
        for path in sources():
            text, name = path.read_text(encoding="utf-8"), path.relative_to(PACKAGE).as_posix()
            for match in re.finditer(r"/classic/([A-Za-z0-9_${}-]+)", text):
                if match[1] not in KEPT_CLASSIC:
                    found.append(f"{name}: {match[0]}")
            for match in re.finditer(r"/operations\?area=([^\"'&#\s`).]+)", text):
                if match[1] not in KEPT_AREAS and (name, match[1]) != ("operations_screen.py", "{key}"):
                    found.append(f"{name}: {match[0]}")
        self.assertEqual(found, [])

    def test_the_kept_screens_still_answer(self):
        # Resources reads sd_tile in a child, which runs launchctl; the test reads a fixture instead.
        patcher = patch("sd_dashboard.reports_screen.collect", lambda area: "")
        patcher.start()
        self.addCleanup(patcher.stop)
        for area in KEPT_AREAS:
            with self.subTest(area=area):
                # Every area draws inside the v2 shell (sd:3586), whose titles end "· system".
                self.assertIn("<title>Operations · system</title>", self.render("/operations", {"area": [area]}))
        self.assertIn("<title>Operations · system</title>", self.render("/operations"))

    def test_every_kept_area_renders_inside_the_v2_shell_under_its_section(self):
        """sd:3586 PR 5: Usage under Metrics, Services under Management and Resources under Research join Trackers and
        Sessions under Today; dashboard.css keeps no rule only these areas drew."""
        patcher = patch("sd_dashboard.reports_screen.collect", lambda area: "")
        patcher.start()
        self.addCleanup(patcher.stop)
        sections = {"usage": "Metrics", "services": "Management", "resources": "Research", "trackers": "Today",
                    "sessions": "Today"}
        for area in KEPT_AREAS:
            with self.subTest(area=area):
                body = self.render("/operations", {"area": [area]})
                self.assertIn(f'<body data-page="{sections[area]}" data-scope="{sections[area].lower()}">', body)
                self.assertIn('<div class="shell"><main id="main" class="classic">', body)
                self.assertNotIn("/static/dashboard.css", body)
                self.assertEqual(body.count("<h1>"), 1)
        classic = (PACKAGE / "static/dashboard.css").read_text(encoding="utf-8")
        classic = re.sub(r"/\*.*?\*/", "", classic, flags=re.DOTALL)
        for selector in (".tiles", ".tile-", ".chart-burn", ".gauge-", ".usage-", ".operations-grid", ".operation-card",
                         ".operation-actions", ".operation-request", ".view-toggle"):
            self.assertNotIn(selector, classic, selector)


class EveryPickerAsksForAKnownScreen(ScreenCase):
    """sd:4002: a page's picker names a palette screen; `runner_exec.SCREENS` is the one list, and an unknown name made
    /api/palette answer 400, so the picker listed nothing."""

    def test_every_server_drawn_page_names_a_screen_runner_exec_knows(self):
        patcher = patch("sd_dashboard.reports_screen.collect", lambda area: "")
        patcher.start()
        self.addCleanup(patcher.stop)
        item = self.item("Picker fixture")
        rendered = {f"/operations?area={area}": self.render("/operations", {"area": [area]}) for area in KEPT_AREAS}
        rendered |= {f"/item/{item}": self.render(f"/item/{item}"), "/protection": self.render("/protection"),
                     "/classic/contributions": self.render("/classic/contributions"),
                     "error page": pages.error_page(404, "Nothing here.")}
        for name, body in rendered.items():
            with self.subTest(page=name):
                screens = re.findall(r'data-screen="([^"]*)"', body)
                self.assertEqual(len(screens), 1)
                self.assertIn(screens[0], runner_exec.SCREENS)


class ActivityTakesCommands(ScreenCase):
    """Activity lists every execution the classic Commands area listed, so that area could go."""

    def test_activity_lists_every_execution_the_classic_area_listed(self):
        # Finished, failed, unfinished, and one a month older than Activity's 24-hour window.
        item = self.item("Run fixture")
        for started, ended, code in ((NOW, NOW, 0), (NOW, NOW, 2), (NOW, None, None),
                                     ("2026-08-06T12:00:00Z", "2026-08-06T12:00:00Z", 0)):
            note = self.note(item, "{}", kind="exec", started=started, ended=ended, exit_code=code, session="dashboard")
            body = json.dumps({"version": 1, "note": note, "command": ["sd-check"], "scope": "repo"})
            # The library stamps a note at the real clock; the fixture's clock is NOW.
            self.connection.execute("UPDATE note SET body = ?, timestamp = ? WHERE id = ?", (body, started, note))
        self.connection.commit()
        listed = {row["id"] for row in runner_exec.executions(self.connection)}
        shown = {event["note"] for event in activity_screen.document(self.connection, now=NOW, jobs_backend=None)["events"]
                 if event["k"] == "command"}
        self.assertEqual(len(listed), 4)
        self.assertEqual(shown, listed)


if __name__ == "__main__":
    unittest.main()
