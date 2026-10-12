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

from sd_db import runner_exec

from sd_dashboard import activity_screen, operations_screen, server, v2

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
        for area in KEPT_AREAS:
            with self.subTest(area=area):
                self.assertIn("<title>Operations — sd</title>", self.render("/operations", {"area": [area]}))
        self.assertIn("<title>Operations — sd</title>", self.render("/operations"))


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
