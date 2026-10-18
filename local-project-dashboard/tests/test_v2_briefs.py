"""The v2 Briefs page (sd:2112, sd:3459).

What this slice promises: `/briefs` answers under the shared policy and loads
its script before `shell.js`; `/api/briefs` needs a session, takes no query,
and lists the routine briefs the workflow database holds, read through the
pack's `routine_briefs.rows`, capped in rows, with every row it could not read
counted and the watchdog said to be unread. A failed read is an error with its
cause, never an empty page. Each row carries its item id; Read opens that
item's page for every row, and Open in Obsidian only where the `brief-copies`
heartbeat vouches for the vault copy. The page claims no failure and no
follow-up, and sends nothing until Capture.

`briefs.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` uses, with the real reader (`read.js`, sd:2418). The browser half -- the look at 375 px, the
cadence lanes, focus -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from sd_db import routine_briefs
from sd_dashboard import briefs_screen, server, v2

from support import NOW, ScreenCase
from test_v2_read import READ_SHELL
from test_v2_today import OSASCRIPT, Refused
from test_v2_tasks import SHELL, STAND_IN
from test_v2_shell_shared import cell_grammar
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers

V2 = Path(v2.__file__).resolve().parent
BRIEFS_JS = (V2 / "static" / "briefs.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
REPO = "/repos/system"
FOLDER = "Briefs/system"

#: The brief commands this port declares: id, object type, label, key, risk.
COMMANDS = [
    ["brief.read", "brief", "Read", "r", "safe"],
    ["brief.task", "brief", "Make task", "k", "safe"],
    ["brief.open", "brief", "Open in Obsidian", "o", "safe"],
]

#: The briefs a small store holds: stem, when first filed, the body's first line.
SEEDS = [
    ("2026-09-06 - Intel Brief", "2026-09-06T09:00:10+00:00", "Three items worth reading today."),
    ("2026-09-06 - Fun Events", "2026-09-06T11:02:00+00:00", "First lines of the brief."),
    ("2026-09-04 - Intel Brief", "2026-09-04T09:01:00+00:00", "First lines of the brief."),
    # Filed after its day: the day stands and the time is not given.
    ("2026-09-03 - Weekly Digest", "2026-09-05T08:00:00+00:00", "First lines of the brief."),
    # Outside 7 days, inside 30.
    ("2026-08-20 - Intel Brief", "2026-08-20T09:00:00+00:00", "First lines of the brief."),
]
#: Every row's last change, then the render that wrote the copies; one brief changed after it.
UPDATED, RENDERED, LATER = "2026-09-06T11:30:00+00:00", "2026-09-06T11:45:00+00:00", "2026-09-06T11:50:00+00:00"
NO_COPY = "2026-09-04 - Intel Brief"


def beat(state="ok", stamp=RENDERED, **more):
    """A `brief-copies` heartbeat as `copies_state` returns it."""
    return (stamp, {"at": stamp, "ok": state != "failed", "state": state, "folder": {REPO: FOLDER}, "previous_folder": {},
                    "written": 4, "refused": [], "reason": None, **more})


def link(stem):
    return "obsidian://open?vault=Vault&file=" + urllib.parse.quote(f"{FOLDER}/{stem}")


class BriefCase(ScreenCase):
    """A store holding `SEEDS`, each last changed at `UPDATED` but `NO_COPY`, changed at `LATER`."""

    def setUp(self):
        super().setUp()
        self.repo(REPO)
        self.ids = {stem: routine_briefs.add(self.connection, stem, f"---\nseries: x\n---\n\n{lead}\n", repo=REPO,
                                             who="test", at=at)["item"] for stem, at, lead in SEEDS}
        # Seeding, as `ScreenCase.age` backdates history: the clock that wrote the rows is the real one.
        self.connection.execute("UPDATE item SET updated_at = CASE title WHEN ? THEN ? ELSE ? END WHERE source = 'brief'",
                                (NO_COPY, LATER, UPDATED))
        self.connection.commit()

    def doc(self, copies=beat, vault="Vault", **more):
        found = copies() if callable(copies) else copies
        return briefs_screen.document(self.connection, now=NOW, copies_reader=lambda _: found, vault=lambda: vault, **more)


class TheDocument(BriefCase):
    def test_rows_come_from_the_database_with_their_item_and_keep_their_day(self):
        doc = self.doc()
        self.assertEqual(doc["reader"], {"state": "read", "reason": "", "source": "workflow database", "total": 5, "shown": 5,
                                         "skipped": 0, "capped": False,
                                         "copies": {"state": "ok", "reason": "", "at": RENDERED}})
        self.assertEqual(doc["watchdog"], {"available": False, "reason": briefs_screen.WATCHDOG_REASON})
        self.assertEqual([b["id"] for b in doc["briefs"]],
                         ["2026-09-06 - Fun Events", "2026-09-06 - Intel Brief", "2026-09-04 - Intel Brief",
                          "2026-09-03 - Weekly Digest", "2026-08-20 - Intel Brief"])
        self.assertEqual({b["id"]: b["item"] for b in doc["briefs"]}, self.ids)
        first = doc["briefs"][1]
        self.assertEqual((first["src"], first["day"], first["at"], first["words"]),
                         ("Intel Brief", "2026-09-06", "2026-09-06T09:00:10Z", 5))
        self.assertEqual(first["lead"], "Three items worth reading today.")
        digest = doc["briefs"][3]
        self.assertEqual((digest["day"], digest["at"], digest["src"]), ("2026-09-03", None, "Weekly Digest"))

    def test_open_needs_an_ok_render_newer_than_the_row_and_read_needs_nothing(self):
        doc = self.doc()
        by = {b["id"]: b for b in doc["briefs"]}
        self.assertEqual(by["2026-09-06 - Intel Brief"]["open"], link("2026-09-06 - Intel Brief"))
        self.assertEqual(by["2026-09-06 - Intel Brief"]["rel"], f"{FOLDER}/2026-09-06 - Intel Brief.md")
        self.assertEqual(by["2026-09-06 - Intel Brief"]["copy"], "")
        # Changed after the render: its copy may be older, so no link; the row and its item stay.
        late = by[NO_COPY]
        self.assertEqual((late["open"], late["rel"], late["item"]), (None, None, self.ids[NO_COPY]))
        self.assertIn("not newer than this brief", late["copy"])

    def test_each_heartbeat_withholds_open_unless_it_vouches_for_the_copy(self):
        stem = "2026-09-06 - Intel Brief"
        cases = {
            "none": (None, "unknown", "no brief render"),
            "failed": (beat("failed", reason="the vault is not a directory"), "failed", "the vault is not a directory"),
            "refused": (beat("failed", refused=[stem]), "failed", "1 copies refused"),
            "off": (beat("off", folder={REPO: None}), "off", "obsidian channel"),
            "older": (beat(stamp="2026-09-06T11:00:00+00:00"), "ok", ""),
            "other repo": (beat(folder={"/repos/other": FOLDER}), "ok", ""),
            "odd state": (beat("rendering"), "unknown", "rendering"),
        }
        for name, (found, state, why) in cases.items():
            with self.subTest(name):
                doc = self.doc(found)
                self.assertEqual(doc["reader"]["copies"]["state"], state)
                self.assertIn(why, doc["reader"]["copies"]["reason"])
                row = next(b for b in doc["briefs"] if b["id"] == stem)
                self.assertEqual((row["open"], row["rel"]), (None, None))
                self.assertTrue(row["copy"])
                self.assertEqual(len(doc["briefs"]), 5, "a heartbeat never hides a row")
        # An ok render that lists the stem as refused, and no vault name: no link either way.
        row = next(b for b in self.doc(beat(refused=[stem]))["briefs"] if b["id"] == stem)
        self.assertEqual(row["open"], None)
        self.assertIn("refused", row["copy"])
        row = next(b for b in self.doc(vault=None)["briefs"] if b["id"] == stem)
        self.assertEqual(row["open"], None)

    def test_a_heartbeat_that_cannot_be_read_is_unknown_and_the_rows_stay(self):
        def refuse(_):
            raise sqlite3.OperationalError("database is locked")
        doc = briefs_screen.document(self.connection, now=NOW, copies_reader=refuse, vault=lambda: "Vault")
        self.assertEqual(doc["reader"]["copies"]["state"], "unknown")
        self.assertIn("database is locked", doc["reader"]["copies"]["reason"])
        self.assertEqual((doc["reader"]["state"], len(doc["briefs"])), ("read", 5))
        self.assertTrue(all(b["open"] is None for b in doc["briefs"]))

    def test_a_reader_that_fails_is_an_error_with_its_cause_never_no_rows(self):
        # Failure table, "page: read rows": a raising reader is state error with the cause, never zero briefs.
        def refuse(_):
            raise sqlite3.OperationalError("database is locked")
        doc = self.doc(reader=refuse)
        self.assertEqual((doc["reader"]["state"], doc["briefs"]), ("error", []))
        self.assertIn("database is locked", doc["reader"]["reason"])
        self.assertEqual(doc["reader"]["source"], "workflow database")

    def test_a_library_without_routine_briefs_is_an_error_naming_it(self):
        with mock.patch.dict(sys.modules, {"sd_db.routine_briefs": None}):
            doc = briefs_screen.document(self.connection, now=NOW)
        self.assertEqual((doc["reader"]["state"], doc["briefs"]), ("error", []))
        self.assertIn("no routine_briefs", doc["reader"]["reason"])
        self.assertEqual(doc["reader"]["copies"]["state"], "unknown")

    def test_a_row_that_is_not_a_brief_is_counted_and_skipped(self):
        good = routine_briefs.rows(self.connection)
        one = good[0]
        bad = [None, {"title": "x"}, {**one, "brief": {**one["brief"], "at": "yesterday"}},
               {**one, "brief": {**one["brief"], "at": "2026-09-06T09:00:00"}},  # no zone: not an instant
               {**one, "brief": {**one["brief"], "words": True}}, {**one, "brief": {**one["brief"], "day": "2026-02-30"}},
               {**one, "id": "7"}, dict(one)]
        doc = self.doc(reader=lambda _: good + bad)
        self.assertEqual((doc["reader"]["state"], doc["reader"]["skipped"], len(doc["briefs"])), ("partial", 8, 5))

    def test_the_page_takes_at_most_its_row_cap_and_cuts_each_lead(self):
        one = routine_briefs.rows(self.connection)[0]
        many = [{**one, "id": 1000 + i, "title": f"2026-09-01 - K{i:03d}",
                 "brief": {**one["brief"], "series": f"K{i:03d}", "day": "2026-09-01", "lead": "y" * 500}}
                for i in range(briefs_screen.ROWS + 1)]
        doc = self.doc(reader=lambda _: many)
        self.assertEqual(len(doc["briefs"]), briefs_screen.ROWS)
        self.assertEqual((doc["reader"]["capped"], doc["reader"]["total"], doc["reader"]["shown"]), (True, None, briefs_screen.ROWS))
        self.assertEqual(len(doc["briefs"][0]["lead"]), briefs_screen.LEAD)
        self.assertFalse(self.doc()["reader"]["capped"])

    def test_a_real_render_vouches_for_its_copies_and_no_local_path_is_sent(self):
        vault = Path(self.tmp.name) / "Vault"
        vault.mkdir()
        self.connection.execute("UPDATE item SET updated_at = '2026-01-01T00:00:00+00:00' WHERE source = 'brief'")
        self.connection.commit()
        self.assertEqual(routine_briefs.render(self.connection, vault)["state"], "ok")
        with mock.patch.dict(os.environ, {"OBSIDIAN_VAULT": str(vault)}):
            doc = briefs_screen.document(self.connection, now=NOW)
        self.assertEqual(doc["reader"]["copies"]["state"], "ok")
        for row in doc["briefs"]:
            self.assertTrue((vault / row["rel"]).is_file(), row["rel"])
            self.assertEqual(row["open"], link(row["id"]))
        self.assertNotIn(self.tmp.name, json.dumps(doc))
        self.assertNotIn(str(Path(self.tmp.name).resolve()), json.dumps(doc))


class TheItemPage(BriefCase):
    """Read's target: `/item/<id>` renders the brief's Markdown, and offers no report control and no Fix."""

    def test_the_item_page_renders_the_markdown_and_no_report_source(self):
        page = self.render(f"/item/{self.ids[NO_COPY]}")
        self.assertIn("<p>First lines of the brief.</p>", page)
        # Its frontmatter is not text: stripped as a piece's draft's is.
        self.assertNotIn("series: x", page)
        self.assertNotIn("Report source", page)
        self.assertNotIn("acknowledge", page.lower())

    def test_an_obsidian_link_in_a_brief_is_a_link_and_javascript_stays_text(self):
        # REGRESSION (sd:5000). The page showed a brief's vault links as
        # source text; Today's and Briefs' Read both open this page.
        url = link("2026-09-06 - Intel Brief")
        item = routine_briefs.add(self.connection, "2026-09-07 - Intel Brief",
                                  f"---\nseries: x\n---\n\nSee [yesterday]({url}) and [this](javascript:alert(1)).\n",
                                  repo=REPO, who="test", at="2026-09-07T09:00:00+00:00")["item"]
        self.connection.commit()
        page = self.render(f"/item/{item}")
        hrefs = re.findall(r'<a href="([^"]*)"', page)
        self.assertIn(url.replace("&", "&amp;"), hrefs)
        self.assertFalse([h for h in hrefs if h.lower().startswith("javascript")], hrefs)
        self.assertIn("[this](javascript:alert(1))", page)

    def test_report_controls_draw_nothing_for_a_brief(self):
        from sd_dashboard import reports_screen
        from sd_db import workflow

        state = workflow.item_state(self.connection, self.ids[NO_COPY])
        self.assertEqual(reports_screen.report_controls(self.connection, state["item"], state["revision"]), "")

    def test_fix_starts_no_session_for_a_brief_even_when_open(self):
        # A brief stays done; were one moved, Fix still runs no herdr launch and writes nothing.
        from sd_dashboard import fix_session
        from sd_db import workflow

        item = self.ids[NO_COPY]
        self.connection.execute("UPDATE item SET status = 'planning' WHERE id = ?", (item,))
        self.connection.commit()
        revision = workflow.item_state(self.connection, item)["revision"]
        with mock.patch.object(fix_session, "checkout", lambda row: Path(self.tmp.name)), \
                mock.patch.object(fix_session.subprocess, "run") as launched, \
                self.assertRaisesRegex(workflow.WorkflowError, "routine brief"):
            fix_session.start(self.connection, item, expected_revision=revision, who="dashboard")
        launched.assert_not_called()


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/briefs")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Briefs · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "briefs.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)
        self.assertNotIn("resource=briefs", body)

    def test_the_rail_opens_the_new_page_and_the_resources_view_is_gone(self):
        from sd_dashboard import reports_screen

        self.assertEqual(v2.SECTIONS.get("Briefs"), "/briefs")
        self.assertNotIn("Briefs", v2.CLASSIC)
        self.assertNotIn("briefs", dict(reports_screen.VIEWS))
        self.assertNotIn("briefs", reports_screen.VIEW_SECONDS)

    def test_the_data_route_needs_a_session_takes_no_query_and_reads_the_store(self):
        self.repo(REPO)
        routine_briefs.add(self.connection, SEEDS[0][0], "---\nseries: x\n---\n\nBody.\n", repo=REPO, who="test", at=SEEDS[0][1])
        self.assertEqual(self.request("/api/briefs")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/briefs?range=30d", headers=cookie)[0], 400)
        status, _, body = self.request("/api/briefs", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual([b["id"] for b in doc["briefs"]], [SEEDS[0][0]])
        self.assertEqual(doc["reader"]["copies"]["state"], "unknown")
        self.assertFalse(doc["watchdog"]["available"])


# The stand-in additions briefs.js needs beyond test_v2_tasks: the shell's row, url, capture, chat, and window.open.
SHELL_MORE = r"""
URLSearchParams = function (q) { var m = []; (q || '').replace(/^\?/, '').split('&').filter(Boolean).forEach(p => { var kv = p.split('='); m.push([kv[0], decodeURIComponent(kv[1] || '')]); });
  this.get = k => { var e = m.find(x => x[0] === k); return e ? e[1] : null; }; this.getAll = k => m.filter(x => x[0] === k).map(x => x[1]); };
OUT.captures = []; OUT.opened = []; OUT.chat = []; OUT.urls = []; OUT.panes = [];
window.shell.row = () => ROW; window.shell.url = q => OUT.urls.push(q); window.shell.capture = (o, t) => OUT.captures.push([o && o.id, t]);
window.shell.openChat = () => {}; window.shell.send = t => OUT.chat.push(t); window.shell.openPane = p => OUT.panes.push(p);
window.open = (u, t, f) => OUT.opened.push([u, t, f]);
"""


class TheScript(BriefCase):
    """briefs.js against the document `briefs_screen` builds from the seeded store, read at NOW."""

    def setUp(self):
        super().setUp()
        self.page_doc = self.doc()

    def run_page(self, body, answer=None, search="", row=None):
        answer = answer or f"(p) => p === '/api/items' ? [201, {{ item: {{ id: 77, title: 'Read it' }} }}] : [200, {json.dumps(self.page_doc)}]"
        script = (STAND_IN + f"location.search = {json.dumps(search)}; var ROW = {json.dumps(row)};\n" + MARKUP_JS
                  + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_MORE + READ_SHELL + f"\nANSWER = {answer};\n" + BRIEFS_JS
                  + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.attention = window.PAGE_ATTENTION;"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("""R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]);
R.primary = REG.filter(c => c.primary && c.primary({})).map(c => c.id); R.gated = REG.filter(c => c.when).map(c => c.id);""")
        self.assertEqual(out["R"]["reg"], COMMANDS)
        # Read is every row's primary action and is never off, so a row without a vault copy offers it too (the shell draws
        # the primary in each row: the browser check on the pull request).
        self.assertEqual((out["R"]["primary"], out["R"]["gated"]), (["brief.read"], ["brief.open"]))

    def test_the_rows_in_range_are_drawn_each_with_read_and_nothing_claims_a_failure(self):
        out = self.run_page("R.rows = ELS.rows.html; R.ann = ELS.annunciator.html; R.lanes = ELS.lanes.html; R.sub = ELS.sub.textContent;")
        rows = out["R"]["rows"]
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', rows),
                         ["2026-09-06 - Fun Events", "2026-09-06 - Intel Brief", "2026-09-04 - Intel Brief", "2026-09-03 - Weekly Digest"])
        self.assertIn("Three items worth reading today.", rows)
        self.assertIn('<time datetime="2026-09-06T09:00:10Z"', rows)
        self.assertIn("09-03 –", rows)
        self.assertNotRegex(rows, r'class="unread"|class="dot"|▲|■')
        ann = out["R"]["ann"]
        self.assertNotRegex(ann + out["R"]["lanes"], r'data-state="(caution|warning)"|class="fail"')
        self.assertRegex(ann, r'title="' + re.escape(briefs_screen.WATCHDOG_REASON) + '" data-state="unknown"')
        self.assertIn("<b>4</b> in 7 days", ann)
        self.assertEqual(cell_grammar(self, ann), [("div", "ok"), ("div", "unknown"), ("div", "ok"), ("button", None)])
        self.assertIn("Vault copies", ann)
        self.assertEqual(len(re.findall(r'class="lane-btn"', out["R"]["lanes"])), 3)
        self.assertEqual(out["R"]["sub"], "5 briefs")
        self.assertEqual(out["states"][0]["kind"], "loading")
        self.assertIsNone(out["states"][-1])
        self.assertEqual(out["attention"], {"state": "unknown", "n": 0, "what": "watchdog not read"})

    def test_read_opens_the_item_page_for_a_row_with_and_without_a_vault_copy(self):
        out = self.run_page(f"""shellRun(cmd('brief.read'), C.get({json.dumps(NO_COPY)})); R.one = location.href;
shellRun(cmd('brief.read'), C.get('2026-09-06 - Intel Brief')); R.two = location.href;
R.cli = cmd('brief.read').cli(C.get({json.dumps(NO_COPY)})); R.exec = cmd('brief.read').executes;
R.det = ELS.details.html;""", row=NO_COPY)
        self.assertEqual((out["R"]["one"], out["R"]["two"]),
                         (f"/item/{self.ids[NO_COPY]}", f"/item/{self.ids['2026-09-06 - Intel Brief']}"))
        self.assertEqual(out["R"]["cli"], f"sd reports brief show '{NO_COPY}'")
        self.assertFalse(out["R"]["exec"])
        self.assertEqual(out["posts"], [])
        # Details says why there is no copy.
        self.assertIn("<dt>Vault copy</dt><dd>the last render is not newer than this brief", out["R"]["det"])

    def test_the_vault_copies_lamp_says_what_the_heartbeat_says(self):
        for found, state, text in ((beat("failed", reason="the vault is not a directory"), "caution", "failed"),
                                   (beat("off", folder={REPO: None}), "ok", "off"), (None, "unknown", "unknown")):
            with self.subTest(text):
                doc = self.doc(found)
                ann = self.run_page("R.ann = ELS.annunciator.html;", answer=f"() => [200, {json.dumps(doc)}]")["R"]["ann"]
                lamps = cell_grammar(self, ann)
                self.assertEqual(lamps[2], ("div", state))
                self.assertIn(f"<b>{text}</b>", ann)

    def test_the_range_and_the_address_choose_the_rows(self):
        out = self.run_page("R.rows = ELS.rows.html;", search="?range=30d&src=Intel%20Brief")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]),
                         ["2026-09-06 - Intel Brief", "2026-09-04 - Intel Brief", "2026-08-20 - Intel Brief"])
        out = self.run_page("R.rows = ELS.rows.html;", search="?range=24h")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["2026-09-06 - Fun Events", "2026-09-06 - Intel Brief"])

    def test_a_source_with_a_comma_survives_the_address(self):
        # Review 39d6c524a35c: a source is the free end of a stem, so the address repeats `src` rather than joining.
        routine_briefs.add(self.connection, "2026-09-05 - Daily, Special", "---\nseries: x\n---\n\nBody.\n", repo=REPO,
                           who="test", at="2026-09-05T07:00:00+00:00")
        out = self.run_page("R.rows = ELS.rows.html;", answer=f"() => [200, {json.dumps(self.doc())}]",
                            search="?src=Daily%2C%20Special&src=Fun%20Events")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]),
                         ["2026-09-06 - Fun Events", "2026-09-05 - Daily, Special"])
        self.assertEqual(out["urls"][-1], [["src", "Daily, Special"], ["src", "Fun Events"]])

    def test_a_search_from_the_address_matches_as_typing_it_does(self):
        # Review 27bec38c4a96: the address keeps the case the reader typed; the match ignores case either way.
        out = self.run_page("R.rows = ELS.rows.html;", search="?q=Intel")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["2026-09-06 - Intel Brief", "2026-09-04 - Intel Brief"])

    def refresh(self, body, later):
        """Load, then click Observed with each answer of `later` held; `body` releases them and reads the page."""
        return self.run_page(f"""const A = ANSWER, LATER = {json.dumps(later)}, HELD = [];
C.select = id => (OUT.selects = OUT.selects || []).push(id);
ANSWER = (path, body) => {{ if (path !== '/api/briefs') return A(path, body); const doc = LATER.shift();
  return new Promise(r => HELD.push(() => r(Array.isArray(doc) ? doc : [200, doc]))); }};
const click = () => ELS.annunciator.listeners.click[0]({{ target: {{ closest: s => s === '#refresh' ? {{}} : null }} }});
{body}""")

    def subset(self, keep):
        """The page's document with only the briefs `keep` names."""
        rows = [b for b in self.page_doc["briefs"] if b["id"] in keep]
        return {**self.page_doc, "briefs": rows, "reader": {**self.page_doc["reader"], "total": len(rows), "shown": len(rows)}}

    def refused(self):
        def refuse(_):
            raise sqlite3.OperationalError("database is locked")
        return self.doc(reader=refuse)

    def test_an_older_refresh_that_answers_last_does_not_win(self):
        # Review ede2f75d05ab: two Observed clicks overlap; the newer read stands whatever order they answer in.
        older = self.subset({"2026-09-06 - Intel Brief"})
        out = self.refresh("""click(); click(); await flush();
HELD[1](); await flush(); HELD[0](); await flush();
R.rows = ELS.rows.html;""", [older, self.page_doc])
        self.assertEqual(len(re.findall(r'<tr data-id=', out["R"]["rows"])), 4)
        self.assertIsNone(out["states"][-1])

    def test_a_refresh_with_no_rows_clears_the_selection_and_the_details(self):
        # Review d1f41457d2b0: Details must not offer Read or Make task on a brief the last read no longer has.
        for doc in (self.subset(set()), self.refused(), [500, {"error": "boom"}]):
            out = self.refresh("""R.before = window.PAGE_LIST.current();
click(); await flush(); HELD[0](); await flush();
R.det = ELS.details.html; R.now = window.PAGE_LIST.current(); R.rows = ELS.rows.html;""", [doc])
            self.assertEqual(out["R"]["rows"], "")
            self.assertEqual(out["R"]["before"], "2026-09-06 - Fun Events")
            self.assertIsNone(out["R"]["now"])
            self.assertEqual(out["selects"][-1], None)
            self.assertNotIn('class="bar"', out["R"]["det"])
            self.assertIn("Select a brief to see it.", out["R"]["det"])

    def test_a_brief_the_refresh_drops_runs_no_command(self):
        # sd:2418: the shell's object map only grows, so a brief the refresh no longer lists must not still offer Read.
        fewer = self.subset({b["id"] for b in self.page_doc["briefs"]} - {"2026-09-06 - Intel Brief"})
        out = self.refresh("""click(); await flush(); HELD[0](); await flush();
const o = C.get('2026-09-06 - Intel Brief'); R.dropped = [o.type, o.label, REG.filter(c => c.on === o.type).length];
R.kept = C.get('2026-09-06 - Fun Events').type;""", [fewer])
        self.assertEqual(out["R"]["dropped"], ["not listed", "2026-09-06 - Intel Brief (no longer listed)", 0],
                         "a brief the refresh dropped kept a command object")
        self.assertEqual(out["R"]["kept"], "brief")

    def test_make_task_opens_the_capture_form_and_only_capture_files_it(self):
        out = self.run_page("""shellRun(cmd('brief.task'), C.get('2026-09-04 - Intel Brief')); await flush();
R.cli = cmd('brief.task').cli(C.get('2026-09-04 - Intel Brief'));
R.hook = typeof window.SHELL_CAPTURE;""")
        self.assertEqual(out["captures"], [["2026-09-04 - Intel Brief", "2026-09-04 - Intel Brief"]])
        self.assertEqual(out["R"]["cli"], "sd task add '2026-09-04 - Intel Brief'")
        # sd:3520: the shell's default capture files it (test_v2_shell_shared); Briefs keeps no hook of its own.
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["R"]["hook"], "undefined")
        self.assertEqual(out["confirms"], [])

    def test_open_in_obsidian_is_the_copy_link_and_off_with_the_reason_without_one(self):
        out = self.run_page(f"""shellRun(cmd('brief.open'), C.get('2026-09-06 - Intel Brief'));
shellRun(cmd('brief.open'), C.get({json.dumps(NO_COPY)}));
R.cli = cmd('brief.open').cli(C.get('2026-09-06 - Intel Brief')); R.exec = cmd('brief.open').executes;""")
        self.assertEqual(out["opened"], [[link("2026-09-06 - Intel Brief"), "_blank", "noopener"]])
        self.assertEqual(out["toasts"][0][0], "Obsidian opens the copy")
        self.assertRegex(out["toasts"][1][0], r"^off: the last render is not newer than this brief")
        self.assertEqual(out["R"]["cli"], f"open '{link('2026-09-06 - Intel Brief')}'")
        self.assertFalse(out["R"]["exec"])
        self.assertEqual(out["posts"], [])

    def test_details_show_the_brief_and_the_address_row_opens_it(self):
        out = self.run_page("R.det = ELS.details.html;", row="2026-09-03 - Weekly Digest")
        det = out["R"]["det"]
        self.assertIn("<h2>2026-09-03 - Weekly Digest</h2>", det)
        self.assertIn("time not known: the note changed after its day", det)
        self.assertIn(f"<code>{FOLDER}/2026-09-03 - Weekly Digest.md</code>", det)
        self.assertIn(f"<dd>#{self.ids['2026-09-03 - Weekly Digest']}</dd>", det)
        self.assertIn(briefs_screen.WATCHDOG_REASON, det)
        self.assertEqual(out["panes"], ["tab-details"])

    def test_a_skipped_row_is_named_apart_and_is_not_a_cap(self):
        # Review 66a49b9f8bec: only a cut is "capped"; a rejected row is skipped.
        doc = self.doc(reader=lambda c: routine_briefs.rows(c) + [{"title": "x"}])
        self.assertEqual((doc["reader"]["capped"], doc["reader"]["skipped"]), (False, 1))
        out = self.run_page("R.sub = ELS.sub.textContent; R.ann = ELS.annunciator.html;", answer=f"() => [200, {json.dumps(doc)}]")
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("1 row skipped: not a brief.", out["states"][-1]["text"])
        self.assertNotIn("Showing the newest", out["states"][-1]["text"])
        self.assertNotIn("at least", out["R"]["ann"])
        self.assertEqual(out["R"]["sub"], "5 briefs, 1 skipped")

    def test_a_failed_read_a_reader_refusal_and_a_capped_read_each_say_so(self):
        out = self.run_page("R.rows = ELS.rows.html;", answer="() => [500, { error: 'boom' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])
        out = self.run_page("R.rows = ELS.rows.html;", answer=f"() => [200, {json.dumps(self.refused())}]")
        self.assertEqual((out["states"][-1]["kind"], out["R"]["rows"]), ("error", ""))
        self.assertIn("database is locked", out["states"][-1]["text"])
        capped = {**self.page_doc, "reader": {**self.page_doc["reader"], "capped": True, "total": None}}
        out = self.run_page("R.sub = ELS.sub.textContent;", answer=f"() => [200, {json.dumps(capped)}]")
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("Showing the newest 5; older briefs are in the database.", out["states"][-1]["text"])
        self.assertEqual(out["R"]["sub"], "newest 5 briefs read")

    def test_a_lamp_with_no_read_behind_it_is_unknown_and_a_cut_count_says_at_least(self):
        # Lead check: missing evidence never shows as healthy, and a count from a capped list says it is a floor.
        for answer in (f"() => [200, {json.dumps(self.refused())}]", "() => [500, { error: 'boom' }]"):
            ann = self.run_page("R.ann = ELS.annunciator.html;", answer=answer)["R"]["ann"]
            # The Briefs lamp; the copies lamp has its own read, and a failed answer leaves it unknown too.
            self.assertEqual(cell_grammar(self, ann)[0], ("div", "unknown"))
            self.assertIn("<b>not read</b>", ann)
        self.assertEqual(cell_grammar(self, ann)[2], ("div", "unknown"))
        cut = self.subset({b["id"] for b in self.page_doc["briefs"][:4]})
        cut["reader"] = {**cut["reader"], "capped": True, "total": None}
        out = self.run_page("""R.ann = ELS.annunciator.html;
ELS.lanes.listeners.click[0]({ target: { closest: s => s === '.lane-btn' ? { dataset: { src: 'Intel Brief' } } : null } });
R.det = ELS.details.html;""", answer=f"() => [200, {json.dumps(cut)}]")
        self.assertIn("at least <b>4</b> in 7 days", out["R"]["ann"])
        self.assertIn("Counts before 09-03 are not read.", out["states"][-1]["text"])
        self.assertIn("<dd>2 of the newest 4</dd>", out["R"]["det"])
        full = self.run_page("R.ann = ELS.annunciator.html;")["R"]["ann"]
        self.assertNotIn("at least", full)

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        self.assertNotIn("innerHTML", BRIEFS_JS)
        self.assertNotIn("setAttribute('style'", BRIEFS_JS)
        self.assertNotRegex(BRIEFS_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", BRIEFS_JS)
        self.assertNotRegex(BRIEFS_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", BRIEFS_JS)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "briefs", "Briefs", "/briefs", ("/api/briefs",)


if __name__ == "__main__":
    unittest.main()
