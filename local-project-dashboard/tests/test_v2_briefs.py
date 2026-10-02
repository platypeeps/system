"""The v2 Briefs page (sd:2112).

What this slice promises: `/briefs` answers under the shared policy and loads
its script before `shell.js`; `/api/briefs` needs a session, takes no query,
and lists the brief notes the vault's Briefs folder holds, read by the child
Research > Resources > Briefs runs (`sd_tile.py briefs-rows`), capped in rows
and bytes, with every row it could not read counted and the watchdog said to
be unread. The page draws those rows, claims no failure and no follow-up,
registers two brief commands (Make task opens the capture form; Open in
Obsidian is the note's link), and sends nothing until Capture.

`briefs.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` uses. The browser half -- the look at 375 px, the
cadence lanes, focus -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import briefs_screen, server, v2

from support import NOW, ScreenCase
from test_v2_today import OSASCRIPT, Refused
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession

HERE = Path(__file__).resolve().parents[1]
V2 = Path(v2.__file__).resolve().parent
BRIEFS_JS = (V2 / "static" / "briefs.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
FOLDER = "System/AI Generated/Briefs"

#: The brief commands this port declares: id, object type, label, key, risk.
COMMANDS = [
    ["brief.task", "brief", "Make task", "k", "safe"],
    ["brief.open", "brief", "Open in Obsidian", "o", "safe"],
]


def load_tile():
    spec = importlib.util.spec_from_file_location("briefs_tile", HERE / "sd_tile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tile_row(stem, at, *, lead="First lines of the brief.", words=12, link=True):
    day = stem[:10] if re.match(r"\d{4}-\d{2}-\d{2}", stem) else ""
    kind = stem.split(" - ", 1)[1] if " - " in stem else "other"
    return {"stem": stem, "rel": f"{FOLDER}/{stem}.md", "kind": kind, "day": day, "at": at, "words": words,
            "lead": lead, "obsidian": f"obsidian://open?vault=Vault&file={stem}" if link else None}


#: What the tile answers for a small vault, newest first as `collect_briefs` sorts.
TILE = {"title": "Briefs", "total": 5, "shown": 5, "folder": FOLDER, "briefs": [
    tile_row("2026-09-06 - Intel Brief", "2026-09-06T09:00:10Z", lead="Three items worth reading today."),
    tile_row("2026-09-06 - Fun Events", "2026-09-06T11:02:00Z"),
    tile_row("2026-09-04 - Intel Brief", "2026-09-04T09:01:00Z", link=False),
    # Changed after its day: the day stands and the time is not known.
    tile_row("2026-09-03 - Weekly Digest", "2026-09-05T08:00:00Z"),
    # Outside 7 days, inside 30.
    tile_row("2026-08-20 - Intel Brief", "2026-08-20T09:00:00Z"),
]}


class TheTile(unittest.TestCase):
    """`sd_tile.py briefs-rows`: the classic reader's notes as rows, within both caps."""

    def fake(self, briefs):
        return mock.Mock(collect_briefs=lambda: {"briefs": briefs, "total": len(briefs), "root": "/home/example/Vault/x"})

    def test_rows_stop_at_the_byte_cap_and_say_where(self):
        tile = load_tile()
        long = [{**tile_row(f"2026-09-{i % 28 + 1:02d} - Kind {i}", "2026-09-01T00:00:00Z"), "lead": "é" * 200} for i in range(300)]
        out = tile.tab_brief_rows(self.fake(long))
        self.assertEqual(out["total"], 300)
        self.assertLess(out["shown"], tile.BRIEF_ROWS)
        self.assertEqual(out["shown"], len(out["briefs"]))
        self.assertLessEqual(len(json.dumps(out["briefs"])), tile.BRIEF_JSON_BYTES)
        # The whole payload fits the 64 KB a child may write.
        self.assertLess(len(json.dumps(out)), 64 * 1024)

    def test_the_byte_cap_counts_the_list_as_it_serializes(self):
        # Every cap from one row to twelve: the rows sent fit it as JSON, brackets and separators included, and one more
        # row would not have fit. Review 0d87b7eb24c1: counting rows alone let 200 rows serialize 200 bytes past the cap.
        tile = load_tile()
        rows = [tile_row(f"2026-09-01 - K{i:03d}", "2026-09-01T00:00:00Z", lead="x") for i in range(20)]
        one = len(json.dumps({key: rows[0].get(key) for key in tile.BRIEF_FIELDS}))
        for cap in range(one, 12 * one + 40):
            with self.subTest(cap=cap), mock.patch.object(tile, "BRIEF_JSON_BYTES", cap):
                sent = tile.tab_brief_rows(self.fake(rows))["briefs"]
                self.assertLessEqual(len(json.dumps(sent)), cap)
                more = sent + [{key: rows[len(sent)].get(key) for key in tile.BRIEF_FIELDS}]
                self.assertGreater(len(json.dumps(more)), cap)

    def test_rows_stop_at_the_row_cap_and_carry_no_absolute_path(self):
        tile = load_tile()
        short = [{**tile_row(f"2026-09-01 - K{i}", "2026-09-01T00:00:00Z", lead="x", link=False), "rel": "r", "root": "/home/example"}
                 for i in range(250)]
        out = tile.tab_brief_rows(self.fake(short))
        self.assertEqual((out["shown"], out["total"]), (tile.BRIEF_ROWS, 250))
        self.assertNotIn("/home/example", json.dumps(out))
        self.assertEqual(set(out["briefs"][0]), set(tile.BRIEF_FIELDS))

    def test_the_real_child_reads_a_vault(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "Vault" / FOLDER
            folder.mkdir(parents=True)
            (folder / "2026-09-06 - Intel Brief.md").write_text("---\ntitle: x\n---\n\n# Intel\n\nThree   items\nworth reading.\n")
            (folder / "notes.txt").write_text("not a brief")
            with mock.patch.dict(os.environ, {"VAULT": str(Path(directory) / "Vault")}):
                got = briefs_screen.collect()
        self.assertEqual((got["total"], got["shown"]), (1, 1))
        row = got["briefs"][0]
        self.assertEqual((row["stem"], row["kind"], row["day"], row["lead"]),
                         ("2026-09-06 - Intel Brief", "Intel Brief", "2026-09-06", "# Intel Three items worth reading."))
        self.assertRegex(row["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TheDocument(ScreenCase):
    def test_rows_keep_their_day_and_drop_a_time_off_their_day(self):
        doc = briefs_screen.document(now=NOW, reader=lambda: TILE)
        self.assertEqual(doc["reader"], {"state": "read", "reason": "", "source": "<vault>/System/AI Generated/Briefs",
                                         "total": 5, "shown": 5, "skipped": 0})
        self.assertEqual(doc["watchdog"], {"available": False, "reason": briefs_screen.WATCHDOG_REASON})
        ids = [b["id"] for b in doc["briefs"]]
        self.assertEqual(ids, ["2026-09-06 - Fun Events", "2026-09-06 - Intel Brief", "2026-09-04 - Intel Brief",
                               "2026-09-03 - Weekly Digest", "2026-08-20 - Intel Brief"])
        digest = doc["briefs"][3]
        self.assertEqual((digest["day"], digest["at"], digest["src"]), ("2026-09-03", None, "Weekly Digest"))
        self.assertEqual(doc["briefs"][2]["open"], None)

    def test_a_row_that_is_not_a_brief_is_counted_and_skipped(self):
        bad = [None, {"stem": "x"}, {**TILE["briefs"][0], "at": "yesterday"}, {**TILE["briefs"][0], "words": True},
               {**TILE["briefs"][0], "day": "06/09/2026"}, dict(TILE["briefs"][0]),
               {**tile_row("2026-09-05 - Odd", "2026-09-05T00:00:00Z"), "obsidian": "javascript:alert(1)"}]
        doc = briefs_screen.document(now=NOW, reader=lambda: {**TILE, "briefs": TILE["briefs"] + bad})
        self.assertEqual((doc["reader"]["state"], doc["reader"]["skipped"]), ("partial", 6))
        odd = next(b for b in doc["briefs"] if b["src"] == "Odd")
        self.assertIsNone(odd["open"])

    def test_the_page_takes_at_most_its_row_cap_and_cuts_each_lead(self):
        many = [tile_row(f"2026-09-01 - K{i:03d}", "2026-09-01T00:00:00Z", lead="y" * 500) for i in range(briefs_screen.ROWS + 5)]
        doc = briefs_screen.document(now=NOW, reader=lambda: {"briefs": many, "total": 900})
        self.assertEqual(len(doc["briefs"]), briefs_screen.ROWS)
        self.assertEqual((doc["reader"]["total"], doc["reader"]["shown"]), (900, briefs_screen.ROWS))
        self.assertEqual(len(doc["briefs"][0]["lead"]), briefs_screen.LEAD)

    def test_a_reader_that_fails_is_an_error_with_its_reason(self):
        def refuse():
            raise ValueError("briefs-rows: RuntimeError: Grant Full Disk Access to /usr/bin/python3")
        doc = briefs_screen.document(now=NOW, reader=refuse)
        self.assertEqual((doc["reader"]["state"], doc["briefs"]), ("error", []))
        self.assertIn("Grant Full Disk Access", doc["reader"]["reason"])

    def test_a_child_past_its_budget_is_refused_not_waited_on(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "tile.py"
            script.write_text("import time\ntime.sleep(30)\n")
            with mock.patch.object(briefs_screen, "SECONDS", 0.5), mock.patch.object(briefs_screen.reports_screen, "TILE", script):
                doc = briefs_screen.document(now=NOW)
        self.assertEqual(doc["reader"]["state"], "error")
        self.assertIn("budget", doc["reader"]["reason"])


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/briefs")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Briefs · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"]+)"', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "briefs.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_and_the_classic_table_stays(self):
        self.assertEqual(v2.SECTIONS.get("Briefs"), "/briefs")
        self.assertEqual(list(v2.SECTIONS)[:2], ["Today", "Briefs"])
        self.assertNotIn("Briefs", v2.CLASSIC)
        self.assertEqual(v2.CLASSIC["Research"], "/operations?area=resources")

    def test_the_data_route_needs_a_session_takes_no_query_and_reads_the_vault_child(self):
        with mock.patch.object(briefs_screen, "collect", lambda: TILE):
            self.assertEqual(self.request("/api/briefs")[0], 403)
            cookie = {"Cookie": self.cookie}
            self.assertEqual(self.request("/api/briefs?range=30d", headers=cookie)[0], 400)
            status, _, body = self.request("/api/briefs", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(len(doc["briefs"]), 5)
        self.assertFalse(doc["watchdog"]["available"])


# The stand-in additions briefs.js needs beyond test_v2_tasks: the shell's row, url, capture, chat, and window.open.
SHELL_MORE = r"""
OUT.captures = []; OUT.opened = []; OUT.chat = []; OUT.urls = []; OUT.panes = [];
window.shell.row = () => ROW; window.shell.url = q => OUT.urls.push(q); window.shell.capture = (o, t) => OUT.captures.push([o && o.id, t]);
window.shell.openChat = () => {}; window.shell.send = t => OUT.chat.push(t); window.shell.openPane = p => OUT.panes.push(p);
window.open = (u, t, f) => OUT.opened.push([u, t, f]);
"""


class TheScript(ScreenCase):
    """briefs.js against the document `briefs_screen` builds from the tile rows above, read at NOW."""

    def setUp(self):
        super().setUp()
        self.doc = briefs_screen.document(now=NOW, reader=lambda: TILE)

    def run_page(self, body, answer=None, search="", row=None):
        answer = answer or f"(p) => p === '/api/items' ? [201, {{ item: {{ id: 77, title: 'Read it' }} }}] : [200, {json.dumps(self.doc)}]"
        script = (STAND_IN + f"location.search = {json.dumps(search)}; var ROW = {json.dumps(row)};\n" + MARKUP_JS
                  + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_MORE + f"\nANSWER = {answer};\n" + BRIEFS_JS
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
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]);")
        self.assertEqual(out["R"]["reg"], COMMANDS)

    def test_the_rows_in_range_are_drawn_and_nothing_claims_a_failure(self):
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
        self.assertRegex(ann, r'data-state="unknown" title="' + re.escape(briefs_screen.WATCHDOG_REASON) + '"')
        self.assertIn("<b>4</b> in 7 days", ann)
        self.assertEqual(len(re.findall(r'class="lane-btn"', out["R"]["lanes"])), 3)
        self.assertEqual(out["R"]["sub"], "5 briefs")
        self.assertEqual(out["states"][0]["kind"], "loading")
        self.assertIsNone(out["states"][-1])
        self.assertEqual(out["attention"], {"state": "unknown", "n": 0, "what": "watchdog not read"})

    def test_the_range_and_the_address_choose_the_rows(self):
        out = self.run_page("R.rows = ELS.rows.html;", search="?range=30d&src=Intel%20Brief")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]),
                         ["2026-09-06 - Intel Brief", "2026-09-04 - Intel Brief", "2026-08-20 - Intel Brief"])
        out = self.run_page("R.rows = ELS.rows.html;", search="?range=24h")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["2026-09-06 - Fun Events", "2026-09-06 - Intel Brief"])

    def test_make_task_opens_the_capture_form_and_only_capture_files_it(self):
        out = self.run_page("""shellRun(cmd('brief.task'), C.get('2026-09-04 - Intel Brief')); await flush();
R.cli = cmd('brief.task').cli(C.get('2026-09-04 - Intel Brief'));
R.filed = await window.SHELL_CAPTURE({ kind: 'task', title: 'Read it' });""")
        self.assertEqual(out["captures"], [["2026-09-04 - Intel Brief", "2026-09-04 - Intel Brief"]])
        self.assertEqual(out["R"]["cli"], "sd task add '2026-09-04 - Intel Brief'")
        self.assertEqual(out["posts"], [["/api/items", {"title": "Read it"}, 64]])
        self.assertEqual(out["R"]["filed"], "Captured #77: Read it")
        self.assertEqual(out["confirms"], [])

    def test_open_in_obsidian_is_the_note_link_and_off_without_one(self):
        out = self.run_page("""shellRun(cmd('brief.open'), C.get('2026-09-06 - Intel Brief'));
shellRun(cmd('brief.open'), C.get('2026-09-04 - Intel Brief'));
R.cli = cmd('brief.open').cli(C.get('2026-09-06 - Intel Brief')); R.exec = cmd('brief.open').executes;""")
        self.assertEqual(out["opened"], [["obsidian://open?vault=Vault&file=2026-09-06 - Intel Brief", "_blank", "noopener"]])
        self.assertEqual([t[0] for t in out["toasts"]], ["Obsidian opens the note", "off: no vault link for this note"])
        self.assertEqual(out["R"]["cli"], "open 'obsidian://open?vault=Vault&file=2026-09-06 - Intel Brief'")
        self.assertFalse(out["R"]["exec"])
        self.assertEqual(out["posts"], [])

    def test_details_show_the_note_and_the_address_row_opens_it(self):
        out = self.run_page("R.det = ELS.details.html;", row="2026-09-03 - Weekly Digest")
        det = out["R"]["det"]
        self.assertIn("<h2>2026-09-03 - Weekly Digest</h2>", det)
        self.assertIn("time not known: the note changed after its day", det)
        self.assertIn(f"<code>{FOLDER}/2026-09-03 - Weekly Digest.md</code>", det)
        self.assertIn(briefs_screen.WATCHDOG_REASON, det)
        self.assertEqual(out["panes"], ["tab-details"])

    def test_a_failed_read_a_reader_refusal_and_a_capped_read_each_say_so(self):
        out = self.run_page("R.rows = ELS.rows.html;", answer="() => [500, { error: 'boom' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])
        refused = briefs_screen.document(now=NOW, reader=lambda: (_ for _ in ()).throw(ValueError("Grant Full Disk Access")))
        out = self.run_page("R.rows = ELS.rows.html;", answer=f"() => [200, {json.dumps(refused)}]")
        self.assertEqual((out["states"][-1]["kind"], out["R"]["rows"]), ("error", ""))
        self.assertIn("Grant Full Disk Access", out["states"][-1]["text"])
        capped = briefs_screen.document(now=NOW, reader=lambda: {**TILE, "total": 900})
        out = self.run_page("R.sub = ELS.sub.textContent;", answer=f"() => [200, {json.dumps(capped)}]")
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("Showing the newest 5 of 900", out["states"][-1]["text"])
        self.assertEqual(out["R"]["sub"], "900 briefs, newest 5 read")

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        self.assertNotIn("innerHTML", BRIEFS_JS)
        self.assertNotIn("setAttribute('style'", BRIEFS_JS)
        self.assertNotRegex(BRIEFS_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", BRIEFS_JS)
        self.assertNotRegex(BRIEFS_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", BRIEFS_JS)


if __name__ == "__main__":
    unittest.main()
