"""The v2 Skills page (sd:2123).

What this slice promises: `/skills` answers under the shared policy and loads the reader and its script before `shell.js`,
and the old screen moves to `/classic/skills`, which the palette lists. `/api/skills` is the catalog the old screen reads,
each skill's use in the last three Monday-start weeks from `skill_use`, the rows that name a path instead of a skill, the
rows by surface and the skills outside the pack that recorded use; it sends no local path and writes nothing. The page
registers the design's Skills commands: Try, Review, Promote and Demote ask first and post the old screen's routes with the
skill's revision; Run, Schedule and Scan are copy only and post nothing.

`skills.js` runs under JavaScriptCore (osascript) against the stand-in page and shell `test_v2_tasks` uses, with the real
reader (`read.js`). The browser half -- the look at 1440 and 375 px, the confirm dialog -- is a manual check.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import skills_catalog
from sd_db.writes import record_skill_use

from sd_dashboard import server, skills_screen, v2

from support import ScreenCase
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
PAGE_JS = (V2 / "static" / "skills.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

#: Wednesday; its week starts Monday 2026-09-21, so the three weeks start 09-07, 09-14 and 09-21.
NOW = "2026-09-23T12:00:00+00:00"

#: The design's Skills commands (design source products/system/designs/pages/skills.js): id, object type, label, key, risk.
#: Try, Review and Demote are `confirm` here, not the design's `undo`: no verb withdraws a trial or a queued request.
COMMANDS = [
    ["skill.try", "skill", "Try", "t", "confirm"],
    ["skill.review", "skill", "Review", "v", "confirm"],
    ["skill.promote", "skill", "Promote", "p", "safe"],
    ["skill.demote", "skill", "Demote", "d", "confirm"],
    ["skill.schedule", "skill", "Schedule", "s", "safe"],
    ["skill.apply", "skill", "Apply", "a", "confirm"],
    ["skill.scan", "use surfaces", "Scan", "s", "safe"],
]

SKILL = "---\nname: {name}\ndescription: The {name} fixture.\n---\n## When to use\nFor a bounded fixture.\n"


class Pack:
    """A pack checkout: `sd-used` and `sd-idle` on the `build` path, `sd-extra` in contrib."""

    def make_pack(self):
        self.root = Path(tempfile.mkdtemp(prefix="pack-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.root)], check=False))
        for name, source in (("sd-used", "skills"), ("sd-idle", "skills"), ("sd-extra", "contrib")):
            (self.root / source / name).mkdir(parents=True)
            (self.root / source / name / "SKILL.md").write_text(SKILL.format(name=name))
        (self.root / "skills/paths.json").write_text(json.dumps({"paths": {"build": {"summary": "plan to ship", "skills": ["sd-used", "sd-idle"]}}}))
        patcher = mock.patch.object(skills_catalog, "location", return_value=(self.root, {}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed_use(self):
        for stamp, skill, surface in (("2026-09-11T10:00:00Z", "sd-used", "claude"), ("2026-09-15T10:00:00Z", "sd-used", "claude"),
                                      ("2026-09-22T10:00:00Z", "sd-used", "claude"), ("2026-09-22T11:00:00Z", "sd-used", "codex"),
                                      ("2026-08-01T10:00:00Z", "sd-idle", "claude"), ("2026-09-22T12:00:00Z", "/Volumes/example/Models", "claude"),
                                      ("2026-09-16T12:00:00Z", "outside-skill", "claude"), ("2026-09-17T12:00:00Z", "outside-skill", "claude")):
            record_skill_use(self.connection, skill, surface=surface, mode="direct", timestamp=stamp)


class TheDocument(Pack, ScreenCase):
    def setUp(self):
        super().setUp()
        self.make_pack()
        self.seed_use()

    def test_use_is_counted_per_monday_week_and_by_what_it_names(self):
        doc = skills_screen.document(self.connection, now=NOW)
        self.assertEqual(doc["weeks"], ["2026-09-07", "2026-09-14", "2026-09-21"])
        by = {s["name"]: s for s in doc["skills"]}
        self.assertEqual(by["sd-used"]["weeks"], [1, 1, 2])
        # A use before the first week counts toward nothing drawn, and the last use still says when it was.
        self.assertEqual((by["sd-idle"]["weeks"], by["sd-idle"]["last"]), ([0, 0, 0], "2026-08-01T10:00:00+00:00"))
        self.assertEqual(doc["totals"], [1, 3, 3])
        self.assertEqual(doc["junk"], 1)
        self.assertEqual(doc["other"], [["outside-skill", 2]])
        self.assertEqual(doc["surfaces"], {"claude": 7, "codex": 1})
        self.assertEqual(doc["use"], {"first": "2026-08-01T10:00:00+00:00", "last": "2026-09-22T12:00:00+00:00", "rows": 8})
        self.assertEqual(by["sd-used"]["modes"], {"direct": 4})
        self.assertEqual({s["name"]: s["status"] for s in doc["skills"]}, {"sd-used": "path", "sd-idle": "path", "sd-extra": "contrib"})
        self.assertEqual(doc["paths"], {"build": "plan to ship"})

    def test_no_field_holds_a_local_path_or_a_file_digest(self):
        text = json.dumps(skills_screen.document(self.connection, now=NOW))
        self.assertNotIn(str(self.root), text)
        self.assertNotIn('"files"', text)
        self.assertNotIn('"root"', text)
        self.assertIn('"source": "skills/sd-used/SKILL.md"', text)

    def test_the_revision_is_the_one_the_write_routes_check(self):
        doc = skills_screen.document(self.connection, now=NOW)
        catalog = {s["name"]: s["revision"] for s in skills_catalog.catalog(self.connection, now=NOW)["skills"]}
        self.assertEqual({s["name"]: s["revision"] for s in doc["skills"]}, catalog)

    def test_the_document_writes_nothing(self):
        before = self.connection.total_changes
        skills_screen.document(self.connection, now=NOW)
        self.assertEqual(self.connection.total_changes, before)


class ThePage(Pack, BrowserSession):
    def setUp(self):
        super().setUp()
        self.make_pack()

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/skills")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Skills · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "skills.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_and_the_palette_keeps_the_old_one(self):
        self.assertEqual(v2.SECTIONS.get("Skills"), "/skills")
        self.assertNotIn("Skills", v2.CLASSIC)
        self.assertEqual(v2.SCREENS.get("Skills (classic)"), "/classic/skills")
        status, _, body = self.request("/classic/skills")
        self.assertEqual(status, 200)
        self.assertIn("Start 30-day trial", body)

    def test_the_data_route_needs_a_session_takes_no_query_and_says_why_it_failed(self):
        self.assertEqual(self.request("/api/skills")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/skills?facet=path", headers=cookie)[0], 400)
        status, _, body = self.request("/api/skills", headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(sorted(s["name"] for s in json.loads(body)["skills"]), ["sd-extra", "sd-idle", "sd-used"])
        (self.root / "skills/paths.json").write_text("{}")
        status, _, body = self.request("/api/skills", headers=cookie)
        self.assertEqual(status, 503)
        self.assertIn("paths.json", json.loads(body)["error"])

    def test_a_skill_the_page_reads_is_one_the_try_route_accepts(self):
        cookie = {"Cookie": self.cookie}
        skill = next(s for s in json.loads(self.request("/api/skills", headers=cookie)[2])["skills"] if s["name"] == "sd-extra")
        self.assertEqual(self.post("/api/skills/sd-extra/try", {"revision": skill["revision"]})[0], 200)
        again = next(s for s in json.loads(self.request("/api/skills", headers=cookie)[2])["skills"] if s["name"] == "sd-extra")
        self.assertEqual(again["status"], "trial")


SHELL_MORE = r"""
window.shell.row = () => null; window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
window.shell.setContext = () => {}; OUT.picks = []; C.pick = id => OUT.picks.push(id);
var pageQuery = document.querySelector;
document.querySelector = sel => sel === '.ledger thead' ? document.getElementById('thead') : pageQuery(sel);
"""


class TheScript(Pack, ScreenCase):
    """skills.js against the document `skills_screen` builds."""

    def setUp(self):
        super().setUp()
        self.make_pack()
        self.seed_use()
        self.doc = skills_screen.document(self.connection, now=NOW)

    def run_page(self, body, answer=None, search=""):
        answer = answer or "(path, body) => path === '/api/skills' ? [200, DOC] : [200, { item: { id: 42 } }]"
        script = (STAND_IN + f"location.search = {json.dumps(search)};\nvar DOC = {json.dumps(self.doc)};\n" + MARKUP_JS
                  + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_MORE + READ_SHELL + f"\nANSWER = {answer};\n" + PAGE_JS
                  + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.attention = window.PAGE_ATTENTION; OUT.html = Object.fromEntries(Object.entries(ELS).map(([k, e]) => [k, e.html]));"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    # The list grammar from shell.list (sd:2682, as Reports in sd:2527-2529). Each click goes to the listener the page put on its holder.
    CLICK = "ELS['%s'].listeners.click[0]({ target: { closest: s => s === %s ? { dataset: %s } : null } });"
    URL = "String(OUT.urls[OUT.urls.length - 1])"

    def many(self, n):
        """The document with n path skills named sd-000 onward, none used."""
        base = next(s for s in self.doc["skills"] if s["name"] == "sd-idle")
        self.doc["skills"] = [dict(base, name=f"sd-{i:03d}") for i in range(n)]

    def test_every_column_with_an_order_sorts_through_the_shared_header(self):
        """sd:2682: the header is shell.list's; aria-sort is on the sorted th alone, and the sort is in the URL."""
        click = self.CLICK % ("rows-head", "'button[data-sort]'", "{ sort: 'n' }")
        out = self.run_page(f"R.head = ELS['rows-head'].html; {click} R.head2 = ELS['rows-head'].html; R.rows = ELS.rows.html; R.url = {self.URL};"
                            f" {click} R.rows2 = ELS.rows.html; R.url2 = {self.URL};")
        sorted_th = r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"'
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["head"]), ["n", "st", "u"])
        self.assertEqual(re.findall(sorted_th, out["R"]["head"]), [("descending", "u")])
        self.assertEqual(re.findall(sorted_th, out["R"]["head2"]), [("ascending", "n")])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["sd-extra", "sd-idle", "sd-used"])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows2"]), ["sd-used", "sd-idle", "sd-extra"])
        self.assertEqual((out["R"]["url"], out["R"]["url2"]), ("sort=n&dir=asc", "sort=n&dir=desc"))

    def test_the_pager_numbers_pages_and_keeps_page_and_size_in_the_url(self):
        self.many(60)
        out = self.run_page("R.pager = ELS.pager.html;" + self.CLICK % ("pager", "'.list-pager [data-page]'", "{ page: '3' }")
                            + f" R.pager2 = ELS.pager.html; R.rows2 = ELS.rows.html; R.url2 = {self.URL};"
                            + self.CLICK % ("pager", "'.list-pager [data-size]'", "{ size: '50' }")
                            + f" R.pager3 = ELS.pager.html; R.url3 = {self.URL};", search="?page=2&sort=n")
        self.assertIn('<span class="range">26–50 of 60</span>', out["R"]["pager"])
        self.assertEqual(re.findall(r'data-page="(\d+)"', out["R"]["pager"]), ["1", "2", "3"])
        self.assertEqual(re.findall(r'data-size="(\d+)"', out["R"]["pager"]), ["25", "50", "100", "200"])
        self.assertIn('<span class="range">51–60 of 60</span>', out["R"]["pager2"])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows2"])[0], "sd-050")
        self.assertEqual(out["R"]["url2"], "page=3&sort=n&dir=asc")
        self.assertIn('<span class="range">1–50 of 60</span>', out["R"]["pager3"])
        self.assertEqual(out["R"]["url3"], "size=50&sort=n&dir=asc")

    def test_active_filters_show_as_chips_that_remove_one_or_clear_all(self):
        out = self.run_page("R.chips = ELS.chips.html;" + self.CLICK % ("chips", "'[data-unfilter]'", "{ unfilter: 'facet' }")
                            + f" R.chips2 = ELS.chips.html; R.url2 = {self.URL};"
                            + " ELS.chips.listeners.click[0]({ target: { closest: s => s === '[data-unfilter-all]' ? {} : null } });"
                            + f" R.chips3 = ELS.chips.html; R.url3 = {self.URL}; R.rows = ELS.rows.html;", search="?facet=path&q=sd")
        self.assertEqual(re.findall(r'data-unfilter="(\w+)"', out["R"]["chips"]), ["facet", "q"])
        self.assertIn("2 filters · 2 of 3", out["R"]["chips"])
        self.assertEqual(re.findall(r'data-unfilter="(\w+)"', out["R"]["chips2"]), ["q"])
        self.assertEqual(out["R"]["url2"], "q=sd")
        self.assertEqual((out["R"]["chips3"], out["R"]["url3"]), ("", ""))
        self.assertEqual(len(re.findall(r'<tr data-id=', out["R"]["rows"])), 3)

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]); R.bulk = REG.filter(c => c.bulk).map(c => c.id);")
        self.assertEqual(out["R"]["reg"], COMMANDS)
        self.assertEqual(out["R"]["bulk"], ["skill.try", "skill.review"])

    def test_try_asks_first_posts_the_skills_revision_and_rereads(self):
        revision = next(s["revision"] for s in self.doc["skills"] if s["name"] == "sd-extra")
        out = self.run_page("shellRun(cmd('skill.try'), C.get('sd-extra')); await flush();")
        self.assertEqual(out["confirms"], ["skill.try"])
        self.assertEqual(out["posts"], [["/api/skills/sd-extra/try", {"revision": revision}, 64]])
        self.assertEqual(out["gets"], ["/api/skills", "/api/skills"], "the page did not reread after the write")
        self.assertEqual(out["toasts"][-1], ["Trial of sd-extra starts at the next install", False])

    def test_review_and_demote_post_their_routes_and_name_the_queued_item(self):
        out = self.run_page("shellRun(cmd('skill.review'), C.get('sd-used')); await flush(); shellRun(cmd('skill.demote'), C.get('sd-idle')); await flush();")
        self.assertEqual([p[0] for p in out["posts"]], ["/api/skills/sd-used/review", "/api/skills/sd-idle/demote"])
        self.assertEqual([t[0] for t in out["toasts"]], ["Review of sd-used queued as item #42", "Removal of sd-idle from paths queued as item #42"])

    def test_a_refused_write_says_why_and_reads_again(self):
        answer = "(path) => path === '/api/skills' ? [200, DOC] : [409, { error: 'The skill changed', reload: true }]"
        out = self.run_page("shellRun(cmd('skill.try'), C.get('sd-extra')); await flush();", answer=answer)
        self.assertEqual(out["toasts"][-1], ["sd-extra not changed: The skill changed", False])
        self.assertEqual(len(out["gets"]), 2)
        self.assertEqual(out["states"][-2]["kind"], "loading", "a refused write reads again with load()")

    def test_off_commands_name_their_reason(self):
        out = self.run_page("""const w = (id, key) => cmd(id).when(C.get(key));
R.off = [w('skill.try', 'sd-used'), w('skill.promote', 'sd-used'), w('skill.demote', 'sd-extra'), w('skill.apply', 'sd-used'), w('skill.try', 'sd-extra')];""")
        self.assertEqual(out["R"]["off"], ["already installed · path", "on build already", "not on a path",
                                           "apply review notes from the review item", True])

    def test_the_copy_only_commands_post_nothing(self):
        out = self.run_page("""R.exec = ['skill.schedule', 'skill.scan'].map(id => cmd(id).executes);
shellRun(cmd('skill.schedule'), C.get('sd-used')); shellRun(cmd('skill.scan'), C.get('surfaces')); await flush();""")
        self.assertEqual(out["R"]["exec"], [False, False])
        self.assertEqual((out["posts"], out["confirms"]), ([], []))

    def test_the_page_draws_lamps_rows_and_the_rail_badge_from_the_document(self):
        out = self.run_page("R.sub = ELS.sub.textContent; R.tally = ELS.tally.textContent;")
        rows, lamps = out["html"]["rows"], out["html"]["annunciator"]
        # Sorted by use this week, most first; an unused path skill carries the caution glyph.
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', rows), ["sd-used", "sd-extra", "sd-idle"])
        self.assertIn('<td class="g g-caution" title="on a path, no recorded use">', rows)
        self.assertEqual(re.findall(r'data-facet="(\w+)" data-state="(\w+)"', lamps), [("path", "ok"), ("contrib", "ok"), ("unused", "caution")])
        self.assertIn('data-open="hygiene" data-state="caution"', lamps)
        self.assertIn('data-open="surfaces" data-state="ok"', lamps)
        self.assertEqual(out["R"]["sub"], "The sd pack catalog: 3 skills. 2 on paths, 0 on trial, 1 in contrib.")
        self.assertEqual(out["R"]["tally"], "3 uses this week · 1 catalog skills used")
        self.assertEqual(out["attention"], {"state": "caution", "n": 2, "what": "unused path skills and use-record faults"})
        self.assertIn("outside-skill (2)", out["html"]["foot"])

    def test_a_facet_filters_the_ledger_and_the_url_keeps_it(self):
        out = self.run_page("document.dispatchEvent(new CustomEvent('skills:facet', { detail: 'unused' })); R.rows = ELS.rows.html; R.url = String(OUT.urls[OUT.urls.length - 1]);")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["sd-idle"])
        self.assertEqual(str(out["R"]["url"]), "facet=unused")
        out = self.run_page("R.rows = ELS.rows.html;", search="?facet=contrib")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["sd-extra"])

    def test_a_failed_read_clears_the_page_and_says_so(self):
        out = self.run_page("R.rows = ELS.rows.html;", answer="() => [503, { error: 'skills/paths.json has no valid path inventory' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("The skills were not read: skills/paths.json has no valid path inventory", out["states"][-1]["text"])
        self.assertIn("Not read: skills/paths.json", out["R"]["rows"])
        self.assertEqual(out["attention"], {"state": "unknown", "n": 0, "what": "skills not read"})

    def test_details_and_the_use_record_notes_are_read_not_drawn(self):
        out = self.run_page("open('sd-used'); R.used = ELS.details.html; open('hygiene'); R.hygiene = ELS.details.html; open('surfaces'); R.surfaces = ELS.details.html;")
        self.assertIn("week of 09-21, 2", out["R"]["used"])
        self.assertIn("skills/sd-used/SKILL.md", out["R"]["used"])
        self.assertIn("1 of 8", out["R"]["hygiene"])
        self.assertIn("claude 7 · codex 1", out["R"]["surfaces"])
        self.assertNotIn("3DPrinter", out["R"]["hygiene"])

    def test_the_script_adds_no_sink_no_inline_style_and_no_reader_of_its_own(self):
        self.assertNotIn("innerHTML", PAGE_JS)
        self.assertNotRegex(PAGE_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", PAGE_JS)
        self.assertNotIn("SKILLS_DATA", PAGE_JS)
        self.assertIn("window.shell.read(", PAGE_JS)
        self.assertNotIn("++generation", PAGE_JS)
        self.assertIn("window.PAGE_LIST", PAGE_JS)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "skills", "Skills", "/skills", ("/api/skills",)


if __name__ == "__main__":
    unittest.main()
