"""The v2 Health page (sd:2115).

What this slice promises: `/fleet-health` answers under the shared policy and
loads its script before `shell.js` (`/health` stays the service's own check);
`/api/health` is every area of the design, in its order, each saying whether a
reader covers it. Worktrees come from the fleet child's registrations, grouped
per checkout, and Attribution from `reads.missing_trailers`. An area with no
reader, or whose reader failed, is unknown on the page and names what it does
not read, never a clean lamp.

`health.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` defines, with the document above as its fetch
answer. The browser half -- focus, the look at 375 px -- is a manual check
recorded on the pull request.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from sd_dashboard import health_screen, server, v2

from support import NOW, ScreenCase
from test_v2_today import OSASCRIPT, Refused
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
HEALTH_JS = (V2 / "static" / "health.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

TREES = [
    {"repo": "group/alpha", "name": "a", "path": "/work/wt-a", "branch": "feat/a", "live": False, "state": "abandoned"},
    {"repo": "group/alpha", "name": "b", "path": "/work/wt-b", "branch": "fix/b", "live": False, "state": "abandoned"},
    {"repo": "beta", "name": "c", "path": "", "branch": "?", "live": False, "state": "unknown"},
    {"repo": "beta", "name": "d", "path": "/work/wt-d", "branch": "main", "live": True, "state": "live"},
]


def fleet_of(trees, root="/checkouts"):
    def read(area):
        assert area == "sessions", area
        return {"root": root, "rootExists": True, "worktrees": trees, "processes": [], "processes_error": "",
                "processes_truncated": False, "abandoned": 0, "counts": {}}
    return read


def trailers_of(count):
    return lambda connection, *, now: count


class TheDocument(ScreenCase):
    """`health_screen.document` from the fleet child and the trailer count."""

    def doc(self, trees=TREES, count=3, **kwargs):
        return health_screen.document(self.connection, now=NOW, fleet=kwargs.get("fleet") or fleet_of(trees),
                                      trailers=kwargs.get("trailers") or trailers_of(count))

    def test_every_design_area_is_there_in_order_and_says_whether_a_reader_covers_it(self):
        areas = self.doc()["areas"]
        self.assertEqual([(a["id"], a["read"]) for a in areas],
                         [("disk", False), ("cred", False), ("attr", True), ("wt", True),
                          ("br", False), ("dep", False), ("sec", False)])
        for area in areas:
            self.assertTrue(area["missing"], f"{area['id']} names nothing it does not read")
            if not area["read"]:
                self.assertEqual((area["rows"], area["error"], area["source"]), ([], "", None))

    def test_registrations_whose_directory_is_gone_are_one_row_per_checkout(self):
        wt = self.doc()["areas"][3]
        rows = {row["id"]: row for row in wt["rows"]}
        self.assertEqual(set(rows), {"gone:group/alpha", "unread:beta"})
        gone = rows["gone:group/alpha"]
        self.assertEqual((gone["state"], gone["type"], gone["repo_path"]), ("caution", "worktree registrations", "/checkouts/group/alpha"))
        self.assertEqual(gone["what"], "group/alpha: 2 worktrees registered, directory gone")
        self.assertEqual(gone["facts"]["Registered"], "2")
        self.assertEqual(gone["list"], ["/work/wt-a (feat/a)", "/work/wt-b (fix/b)"])
        unread = rows["unread:beta"]
        self.assertEqual((unread["state"], unread["type"], unread["list"]), ("unknown", "unread registrations", ["(unnamed) (?)"]))

    def test_a_fleet_with_every_registration_present_is_one_ok_row(self):
        (row,) = self.doc(trees=[TREES[3]])["areas"][3]["rows"]
        self.assertEqual((row["id"], row["state"], row["type"]), ("wt:ok", "ok", "check"))
        self.assertIn("checked 1 registration in 1 checkout under /checkouts", row["detail"])

    def test_the_trailer_count_is_a_caution_row_or_an_ok_check(self):
        (row,) = self.doc(count=3)["areas"][2]["rows"]
        self.assertEqual((row["id"], row["state"], row["type"], row["facts"]["Missing"]), ("attr:week", "caution", "attribution gap", "3"))
        self.assertEqual(row["what"], "3 commits of the last 7 days lack Authored-with")
        (row,) = self.doc(count=0)["areas"][2]["rows"]
        self.assertEqual((row["id"], row["state"], row["type"]), ("attr:ok", "ok", "check"))

    def test_a_reader_that_fails_is_its_area_error_and_the_other_still_answers(self):
        def broken(area):
            raise ValueError("fleet collection was stopped at its budget: sessions")
        areas = self.doc(fleet=broken)["areas"]
        self.assertEqual((areas[3]["error"], areas[3]["rows"]), ("fleet collection was stopped at its budget: sessions", []))
        self.assertEqual(areas[2]["rows"][0]["id"], "attr:week")
        areas = self.doc(fleet=lambda area: {"root": "/checkouts", "worktrees": [{"repo": "x"}]})["areas"]
        self.assertEqual(areas[3]["error"], "fleet collector returned an incomplete sessions document")

    def test_the_default_trailer_reader_counts_a_registered_repo(self):
        repo = Path(self.tmp.name) / "repo"
        env = {**os.environ, "GIT_AUTHOR_DATE": "2026-09-05T10:00:00Z", "GIT_COMMITTER_DATE": "2026-09-05T10:00:00Z",
               "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.test",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.test"}
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
        for message in ("no trailer", "with trailer\n\nAuthored-with: human"):
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", message], check=True, env=env)
        self.repo(str(repo))
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of([]))
        self.assertEqual(doc["areas"][2]["rows"][0]["facts"]["Missing"], "1")


class ThePage(BrowserSession):
    fleet_backend = staticmethod(fleet_of(TREES))

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/fleet-health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Health · system</title>", body)
        self.assertRegex(body, r'<meta name="sd-csrf" content="[a-f0-9]{64}"></head>')
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"]+)"', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "health.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)
        # /health is the service's own check, which the runtime reads; the page does not take it over.
        status, _, body = self.request("/health")
        self.assertEqual(json.loads(body)["service"], "sd-dashboard")

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/health")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/health?area=wt", headers=cookie)[0], 400)
        status, _, body = self.request("/api/health", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertRegex(doc["read"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual([row["id"] for row in doc["areas"][3]["rows"]], ["gone:group/alpha", "unread:beta"])

    def test_the_rail_opens_health_at_its_page(self):
        self.assertEqual(v2.SECTIONS["Health"], "/fleet-health")
        self.assertNotIn("Health", v2.CLASSIC)
        self.assertIn("/operations?area=sessions", v2.SCREENS.values())


# Extras the Health script reads from the shell that the Tasks stand-in does not carry.
HEALTH_SHELL = r"""
window.shell.shq = s => "'" + String(s).replace(/'/g, "'\\''") + "'";
window.shell.row = () => null;
window.shell.url = q => { OUT.url = String(q && q.toString ? q.toString() : q); };
window.shell.state = s => OUT.states.push(s);
"""


class TheScript(ScreenCase):
    """health.js against the document `health_screen` builds from the fixtures above."""

    def run_page(self, body, doc=None, status=200):
        doc = doc if doc is not None else health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES),
                                                                 trailers=trailers_of(3))
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + HEALTH_SHELL
                  + f"\nvar DOC = {json.dumps(doc)}, STATUS = {status};\n"
                  + "URLSearchParams.prototype.toString = function () { return ''; };\n"
                  + "ANSWER = (path, body) => path === '/api/health' ? [STATUS, DOC] : [404, { error: 'no answer' }];\n"
                  + HEALTH_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\n"
                  + "(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_every_command_is_registered_with_the_design_id_risk_and_key(self):
        out = self.run_page("""R.reg = REG.map(c => [c.id, c.on, c.risk, c.key || null, typeof c.executes === 'boolean' ? c.executes : null, !!c.bulk]);""")
        self.assertEqual(out["R"]["reg"], [
            ["check.recheck", "check", "safe", "e", False, False],
            ["attribution gap.attribute", "attribution gap", "safe", "a", False, False],
            ["worktree registrations.prune", "worktree registrations", "confirm", "p", False, True],
            ["worktree registrations.snooze", "worktree registrations", "undo", "z", None, True],
            ["unread registrations.snooze", "unread registrations", "undo", "z", None, True],
            ["attribution gap.snooze", "attribution gap", "undo", "z", None, True],
        ])

    def test_an_area_with_no_reader_is_an_unknown_lamp_that_names_what_it_does_not_read(self):
        out = self.run_page("R.lamps = ELS.annunciator.html; R.areas = ELS.areas.html; R.sub = ELS.subhead.html;")
        self.assertEqual(out["gets"], ["/api/health"])
        lamps = out["R"]["lamps"]
        for area in ("disk", "cred", "br", "dep", "sec"):
            self.assertRegex(lamps, rf'data-area="{area}" data-state="unknown"[^>]*>.*?no reader', area)
        self.assertRegex(lamps, r'data-area="wt" data-state="caution"[^>]*>.*?<b>2</b> dir gone')
        self.assertRegex(lamps, r'data-area="attr" data-state="caution"[^>]*>.*?<b>3</b> missing')
        areas = out["R"]["areas"]
        self.assertIn("<b>No reader yet.</b> The dashboard has no collector for this area, so nothing here is known: volume use (df -k)", areas)
        self.assertIn("<b>Not read here:</b> merged worktrees still on disk", areas)
        self.assertIn('data-id="gone:group/alpha"', areas)
        self.assertIn("2 caution rows want you · 5 areas with no reader yet", out["R"]["sub"])
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 2, "what": "findings to watch"})
        self.assertIsNone(out["states"][-1])

    def test_a_failed_reader_is_a_partial_read_and_its_lamp_is_unknown(self):
        def broken(area):
            raise ValueError("fleet collection was stopped at its budget: sessions")
        doc = health_screen.document(self.connection, now=NOW, fleet=broken, trailers=trailers_of(0))
        out = self.run_page("R.lamps = ELS.annunciator.html; R.areas = ELS.areas.html;", doc)
        self.assertRegex(out["R"]["lamps"], r'data-area="wt" data-state="unknown"[^>]*>.*?not read')
        self.assertIn("<b>Not read:</b> fleet collection was stopped at its budget: sessions", out["R"]["areas"])
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertEqual(out["attention"][-1], {"state": "ok", "n": 0, "what": "findings to watch"})

    def test_a_document_that_does_not_arrive_leaves_no_row_and_says_so(self):
        out = self.run_page("""R.before = ELS.areas.html;
DOC = { error: 'Open a dashboard page before reading Health.' }; STATUS = 403;
ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? { id: 'refresh', dataset: {} } : null } }); await flush();
R.areas = ELS.areas.html; R.lamps = ELS.annunciator.html;""")
        self.assertEqual(out["gets"], ["/api/health", "/api/health"])
        self.assertIn('data-id="gone:group/alpha"', out["R"]["before"])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("Open a dashboard page before reading Health.", out["states"][-1]["text"])
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "rows"})
        self.assertNotIn("data-id=", out["R"]["areas"], "rows from the last read stayed on screen")
        self.assertIn("not read", out["R"]["lamps"])

    def test_a_lamp_shows_its_area_only_and_a_state_chip_narrows_the_rows(self):
        click = "ELS.{el}.listeners.click[0]({{ target: {{ closest: s => s === '{sel}' ? {{ id: '', dataset: {data} }} : null }} }});"
        out = self.run_page(click.format(el="annunciator", sel="button.cell", data="{ area: 'disk' }")
                            + "R.disk = ELS.areas.html; R.note = ELS.filtered.html;"
                            + click.format(el="annunciator", sel="button.cell", data="{ area: 'disk' }")
                            + click.format(el="filters", sel="[data-f]", data="{ f: 'state', v: 'unknown' }")
                            + "R.unknown = ELS.areas.html;")
        disk, unknown = out["R"]["disk"], out["R"]["unknown"]
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', disk), ["disk"])
        self.assertIn("No row matches Disk only.", out["R"]["note"])
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', unknown), ["wt"])
        self.assertEqual(re.findall(r'data-id="([^"]+)"', unknown), ["unread:beta"])

    def test_prune_confirms_first_is_copy_only_and_posts_nothing(self):
        out = self.run_page("""var o = C.get('gone:group/alpha'); R.cli = cmd('worktree registrations.prune').cli(o);
R.why = cmd('worktree registrations.prune').consequence(o); R.snooze = cmd('worktree registrations.snooze').when(o);
shellRun(cmd('worktree registrations.prune'), o); await flush();""")
        self.assertEqual(out["R"]["cli"], "git -C '/checkouts/group/alpha' worktree prune -v")
        self.assertEqual(out["R"]["why"], "Removes 2 worktree registrations whose directories are gone. No directory is touched.")
        self.assertEqual(out["R"]["snooze"], "no CLI verb: sd has no snooze")
        self.assertEqual(out["confirms"], ["worktree registrations.prune"])
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"][-1][0], "Not run here: copy the line from Details and run it in a terminal · "
                                               "group/alpha: 2 worktrees registered, directory gone")

    def test_details_show_the_facts_and_the_registrations_behind_a_row(self):
        out = self.run_page("document.dispatchEvent(new CustomEvent('shell:open', { detail: 'gone:group/alpha' })); R.d = ELS.details.html;")
        details = out["R"]["d"]
        self.assertIn("<h2>group/alpha: 2 worktrees registered, directory gone</h2>", details)
        self.assertIn("<dt>Repo</dt><dd>/checkouts/group/alpha</dd>", details)
        self.assertIn("<li>/work/wt-a (feat/a)</li>", details)
        self.assertIn("<dt>Source</dt><dd>the fleet&#39;s .git/worktrees registrations</dd>", details)

    def test_the_first_read_opens_the_first_row_in_details(self):
        out = self.run_page("R.d = document.getElementById('details').html || '';")
        self.assertIn("<h2>3 commits of the last 7 days lack Authored-with</h2>", out["R"]["d"],
                      "Details still say the fleet is being read after the rows arrived")

    def test_the_script_adds_no_sink_and_no_inline_style(self):
        self.assertNotIn("innerHTML", HEALTH_JS)
        self.assertNotRegex(HEALTH_JS, r"style=\\?\"")
        self.assertEqual(re.findall(r"window\.markup\b", re.sub(r"const \{ [\w, ]+ \} = window\.markup;", "", HEALTH_JS)), [])
        # The shell owns j / k and Esc through PAGE_LIST; a page-level handler is drift.
        self.assertNotRegex(HEALTH_JS, r"e\.key === '[jk]'")


if __name__ == "__main__":
    unittest.main()
