"""The v2 HOA page (sd:2116).

What this slice promises: `/hoa` answers under the shared policy and loads its
script before `shell.js`; `/api/hoa` reads the checkout the config folder's
`hoa.conf` names (the path names a place, so the public checkout carries only
`hoa.conf.example`): the asset map, the Mission export's last 30 days, and the
checkout's open followup items ranked overdue, due soon, then the rest. Each
source is guarded on its own, so a file that is missing or does not parse is
that part's error and the page shows it as unknown with the reason, never as
an empty list. An export whose newest row is more than two days old is stale.

`hoa.js` runs under JavaScriptCore (osascript) against the stand-in page and
shell `test_v2_tasks` defines, with the document above as its fetch answer.
The browser half -- the map's drag and zoom, the look at 375 px -- is a manual
check recorded on the pull request.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import hoa_screen, server, v2

from support import NOW, ScreenCase
from test_v2_today import OSASCRIPT, Refused
from test_v2_read import READ_SHELL
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers
from test_v2_shell_shared import cell_grammar

V2 = Path(v2.__file__).resolve().parent
HOA_JS = (V2 / "static" / "hoa.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
EXAMPLE = Path(hoa_screen.__file__).resolve().parents[1] / "hoa.conf.example"

#: NOW is 2026-09-06; the export below ends the day before, so it is current and has no row for today.
ANALOG = "date,samples,aquifer_ft_min,aquifer_ft_mean,aquifer_ft_max,tank_ft_min,tank_ft_mean,tank_ft_max,a2_flow_gpm_min,a2_flow_gpm_mean,a2_flow_gpm_max,booster_psi_min,booster_psi_mean,booster_psi_max\n" + "".join(
    f"2026-{m:02d}-{d:02d},96,150,{180 + d},240,16.0,{16 + d / 100:.2f},17.0,0,40,60,50,55,60\n"
    for m, d in [(7, x) for x in range(20, 32)] + [(8, x) for x in range(1, 32)] + [(9, x) for x in range(1, 6)])
PUMP = "date,a2_starts,a2_runtime_hours,a2_gallons,a2_flow_gpm_running,a2_gallons_metered,hours_reported\n" + "".join(
    f"2026-08-{d:02d},{d % 3},{10 + d / 10:.1f},{90000 + d},60,,24\n" for d in range(1, 32)) + "2026-09-04,0,16.9,91119,60,,24\n"
ALARMS = ("datetime,kind,event,notified,minutes\n"
          "2026-07-01 08:00:00,other,A2 VSD Alarm,N,4\n"          # older than 30 days before the newest row: not carried
          "2026-08-20 09:00:00,generator,Generator Running,N,15\n"
          "2026-09-01 10:00:00,other,A2 VSD Alarm,N,3\n"
          "2026-09-01 10:30:00,other,A2 VSD Normal,,0\n"
          "2026-09-05 06:00:00,other,Tank Low Alarm,N,7\n")
#: Two lots, one tract and one plat as squares, and three assets: synthetic coordinates, no real place.
BASE = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"kind": kind, "ain": ain, "label": label},
     "geometry": {"type": "Polygon", "coordinates": [[[x, y], [x + 0.001, y], [x + 0.001, y + 0.001], [x, y + 0.001], [x, y]]]}}
    for kind, ain, label, x, y in [("lot", "", "", 10.0, 50.0), ("lot", "", "", 10.001, 50.0),
                                   ("tract", "0001", "Tract A", 10.0, 50.001), ("plat", "", "Plat 1", 10.0, 50.0)]]}
ASSETS = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"id": ident, "type": kind, "name": name, "status": status, "accuracy": "survey",
                                       "source": "walk", "notes": "", "updated": "2026-09-01"},
     "geometry": {"type": "Point", "coordinates": [x, y]}}
    for ident, kind, name, status, x, y in [("TANK-01", "tank", "Tank", "in service", 10.0005, 50.0015),
                                            ("WELL-A2", "well", "Well A2", "in service", 10.0012, 50.0004),
                                            ("V-01", "valve", "Valve 1", "unknown", 10.0003, 50.0003)]]}


def checkout(root: Path, *, analog=ANALOG, pump=PUMP, alarms=ALARMS, base=BASE, assets=ASSETS) -> Path:
    """A checkout laid out as the design reads it."""
    mission, maps = root / "water" / "metrics" / "mission", root / "water" / "map"
    mission.mkdir(parents=True)
    maps.mkdir(parents=True)
    for name, text in (("analog-daily.csv", analog), ("pump-daily.csv", pump), ("alarm-events.csv", alarms)):
        if text is not None:
            (mission / name).write_text(text, encoding="utf-8")
    for name, data in (("base.geojson", base), ("assets.geojson", assets)):
        (maps / name).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return root


class Fixture(ScreenCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.hoa = checkout(self.root / "hoa")
        self.config = self.root / "hoa.conf"
        self.config.write_text(f"# the checkout\nrepo|{self.hoa}\n", encoding="utf-8")
        self.repo(str(self.hoa))
        self.repo("/work/other")
        self.late = self.item("Confirm the invoice was paid", kind="followup", repo=str(self.hoa), due="2026-09-01", priority=1)
        self.soon = self.item("Chase the valve schedule", kind="followup", repo=str(self.hoa), due="2026-09-10", priority=2)
        self.later = self.item("Deliver the annual report", kind="followup", repo=str(self.hoa), due="2026-10-30", priority=1)
        self.item("Already settled", kind="followup", repo=str(self.hoa), status="done", due="2026-08-01")
        self.item("A task, not a followup", kind="task", repo=str(self.hoa), due="2026-08-01")
        self.item("Another checkout's followup", kind="followup", repo="/work/other", due="2026-08-01")

    def doc(self, config=None):
        return hoa_screen.document(self.connection, now=NOW, config=self.config if config is None else config)


class TheDocument(Fixture):
    def test_without_a_config_every_part_names_what_it_did_not_read(self):
        doc = self.doc(self.root / "absent.conf")
        self.assertEqual(doc["config"], {"state": "missing", "source": "<config>/project-dashboard/absent.conf", "problems": []})
        self.assertIn("hoa.conf names no repo", doc["mission"]["error"])
        self.assertIn("hoa.conf names no repo", doc["assets"]["error"])
        self.assertIn("hoa.conf names no repo", doc["followups"]["error"])
        self.assertEqual((doc["mission"]["analog"], doc["assets"]["features"], doc["followups"]["rows"]), ([], [], []))

    def test_the_export_is_its_last_30_days_and_its_age(self):
        m = self.doc()["mission"]
        self.assertEqual(m["error"], "")
        self.assertEqual([f["rows"] for f in m["files"]], [48, 32, 5])
        self.assertEqual((len(m["analog"]), m["analog"][-1]["date"], m["pump"][-1]["date"]), (30, "2026-09-05", "2026-09-04"))
        self.assertEqual((m["latest"], m["age_days"], m["stale"]), ("2026-09-05", 1, False))
        # 30 days back from the newest alarm: the July row is gone, oldest first.
        self.assertEqual([a["event"] for a in m["alarms"]], ["Generator Running", "A2 VSD Alarm", "A2 VSD Normal", "Tank Low Alarm"])
        self.assertEqual(m["alarms_today"], {"count": None, "reason": "the export ends 2026-09-05"})

    def test_an_export_more_than_two_days_old_is_stale(self):
        self.assertEqual(hoa_screen.STALE_DAYS, 2)
        m = hoa_screen.document(self.connection, now="2026-09-08T12:00:00Z", config=self.config)["mission"]
        self.assertEqual((m["age_days"], m["stale"]), (3, True))
        m = hoa_screen.document(self.connection, now="2026-09-07T12:00:00Z", config=self.config)["mission"]
        self.assertEqual((m["age_days"], m["stale"]), (2, False))

    def test_alarms_today_count_only_real_alarms_on_a_day_the_export_reaches(self):
        alarms = ALARMS + ("2026-09-06 01:00:00,other,A2 VSD Alarm,N,2\n2026-09-06 01:10:00,other,A2 VSD Normal,,0\n"
                           "2026-09-06 02:00:00,generator,Generator Running,N,15\n")
        hoa = checkout(self.root / "today", alarms=alarms)
        self.config.write_text(f"repo|{hoa}\n", encoding="utf-8")
        self.assertEqual(self.doc()["mission"]["alarms_today"], {"count": 1, "reason": ""})

    def test_a_file_that_does_not_parse_is_its_error_and_the_rest_still_answer(self):
        hoa = checkout(self.root / "broken", pump="date,a2_starts\n2026-09-01,1\n", alarms=None, assets="{not json")
        self.config.write_text(f"repo|{hoa}\n", encoding="utf-8")
        doc = self.doc()
        files = {f["name"]: f["error"] for f in doc["mission"]["files"]}
        self.assertIn("pump-daily.csv has no a2_runtime_hours", files["pump-daily.csv"])
        self.assertEqual(files["alarm-events.csv"], "alarm-events.csv is not in the export folder")
        self.assertEqual(files["analog-daily.csv"], "")
        self.assertEqual(len(doc["mission"]["analog"]), 30)
        self.assertIn("pump-daily.csv has no", doc["mission"]["error"])
        self.assertIn("assets.geojson did not parse", doc["assets"]["error"])
        self.assertEqual(len(doc["base"]["features"]), 4)
        self.assertEqual(doc["mission"]["alarms_today"]["reason"], "alarm-events.csv is not in the export folder")

    def test_a_row_with_no_date_is_the_files_error_not_a_silent_skip(self):
        hoa = checkout(self.root / "undated", analog=ANALOG + ",96,1,2,3,4,5,6,7,8,9,10,11,12\n")
        self.config.write_text(f"repo|{hoa}\n", encoding="utf-8")
        files = {f["name"]: f["error"] for f in self.doc()["mission"]["files"]}
        self.assertEqual(files["analog-daily.csv"], "analog-daily.csv line 50: '' is not a date")

    def test_followups_are_the_checkouts_open_followup_items_ranked_by_urgency(self):
        rows = self.doc()["followups"]["rows"]
        self.assertEqual([(r["item"], r["state"], r["due_days"]) for r in rows],
                         [(self.late, "warning", -5), (self.soon, "caution", 4), (self.later, "queued", 54)])
        self.assertEqual(rows[0]["id"], f"followup:{self.late}")
        self.assertEqual(rows[0]["priority"], 1)

    def test_assets_and_outlines_carry_their_coordinates(self):
        doc = self.doc()
        self.assertEqual([(a["id"], a["type"], a["lng"], a["lat"]) for a in doc["assets"]["features"]][1], ("WELL-A2", "well", 10.0012, 50.0004))
        self.assertEqual([l["kind"] for l in doc["base"]["features"]], ["lot", "lot", "tract", "plat"])
        self.assertEqual(len(doc["base"]["features"][2]["rings"][0]), 5)

    def test_a_config_line_it_cannot_read_is_named_and_skipped(self):
        self.config.write_text(f"repo|{self.hoa}\nmission|relative/path\ncolour|blue\nrepo|{self.hoa}\n", encoding="utf-8")
        doc = self.doc()
        self.assertEqual(doc["config"]["state"], "partial")
        self.assertEqual(doc["config"]["problems"], ["line 2: mission needs an absolute path",
                                                     "line 3: expected <key>|<path>, key one of repo, mission, assets, base",
                                                     "line 4: repo is set twice"])
        self.assertEqual(doc["mission"]["error"], "")

    def test_the_example_config_parses_and_names_no_real_path(self):
        found, problems = hoa_screen.parse(EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(problems, [])
        self.assertEqual(str(found["repo"]), "/change-me/path/to/hoa-checkout")


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/hoa")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>HOA · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "hoa.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/hoa")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/hoa?range=7", headers=cookie)[0], 400)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(hoa_screen, "CONFIG", Path(tmp) / "hoa.conf"):
            status, _, body = self.request("/api/hoa", headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["config"]["state"], "missing")

    def test_the_rail_opens_hoa_at_its_page(self):
        self.assertEqual(v2.SECTIONS["HOA"], "/hoa")
        self.assertNotIn("HOA", v2.CLASSIC)


# Extras the HOA script reads from the shell that the Tasks stand-in does not carry.
HOA_SHELL = r"""
window.shell.row = () => null;
window.shell.state = s => OUT.states.push(s);
window.shell.pages = { Tasks: '/tasks' };
"""


class TheScript(Fixture):
    """hoa.js against the document `hoa_screen` builds from the fixture checkout."""

    def run_page(self, body, doc=None, status=200):
        doc = doc if doc is not None else self.doc()
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + HOA_SHELL + READ_SHELL
                  + f"\nvar DOC = {json.dumps(doc)}, STATUS = {status};\n"
                  + "ANSWER = (path, body) => path === '/api/hoa' ? [STATUS, DOC] : [404, { error: 'no answer' }];\n"
                  + HOA_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\n"
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
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.risk, c.key || null]);")
        self.assertEqual(out["R"]["reg"], [
            ["followup.draft", "followup", "safe", "d"],
            ["followup.open", "followup", "safe", "o"],
            ["asset.ask", "asset", "safe", "a"],
        ])

    def test_each_cell_is_a_reading_or_unknown_with_its_reason(self):
        out = self.run_page("R.lamps = ELS.annunciator.html; R.sub = ELS.subhead.html;")
        self.assertEqual(out["gets"], ["/api/hoa"])
        lamps = out["R"]["lamps"]
        self.assertRegex(lamps, r'data-cell="water" data-state="unknown".*?no live reader')
        self.assertRegex(lamps, r'data-cell="tank" data-open="day:2026-09-05" data-state="ok".*?<b>16.1</b> ft mean')
        self.assertRegex(lamps, r'data-cell="pumps" data-open="day:2026-09-04" data-state="ok".*?<b>16.9</b> h A2.*?0 starts · 91,119 gal · 09-04')
        self.assertRegex(lamps, r'data-cell="alarms"[^>]*data-state="unknown".*?the export ends 2026-09-05.*?2 alarms in 30 d · 1 generator test')
        self.assertRegex(lamps, rf'data-cell="followups" data-open="followup:{self.late}" data-state="warning".*?<b>1</b> overdue.*?3 open · 1 due within a week')
        self.assertRegex(lamps, r'data-cell="mission" data-open="source:mission" data-state="ok".*?<b>1 d</b> old')
        self.assertEqual(cell_grammar(self, lamps)[-1], ("button", None))
        self.assertIn("3 mapped assets, 4 lot and tract outlines; Mission export through 2026-09-05", out["R"]["sub"])
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 1, "what": "HOA annunciator cells"})
        self.assertIsNone(out["states"][-1])

    def test_the_map_draws_every_asset_and_outline_and_the_legend_hides_a_type(self):
        out = self.run_page("""R.map = ELS.map.html; R.legend = ELS.legend.html;
ELS.legend.listeners.click[0]({ target: { closest: s => s === 'button[data-type]' ? { dataset: { type: 'valve' } } : null } });
R.after = ELS.map.html;""")
        self.assertEqual(re.findall(r'data-asset="([^"]+)"', out["R"]["map"]), ["TANK-01", "WELL-A2", "V-01"])
        self.assertEqual(len(re.findall(r'<path class="lot k-', out["R"]["map"])), 4)
        self.assertIn('data-tract="0001"', out["R"]["map"])
        self.assertIn('class="asset t-valve st-unknown"', out["R"]["map"])
        self.assertIn("Valves <b>1</b>", out["R"]["legend"])
        self.assertEqual(re.findall(r'data-asset="([^"]+)"', out["R"]["after"]), ["TANK-01", "WELL-A2"])

    def test_the_followups_are_ranked_rows_with_their_due_state(self):
        out = self.run_page("R.ob = ELS['ob-body'].html; R.al = ELS['al-body'].html; R.det = ELS.details.html;")
        ob = out["R"]["ob"]
        self.assertEqual(re.findall(r'<tr data-id="followup:(\d+)"', ob), [str(self.late), str(self.soon), str(self.later)])
        self.assertIn("overdue 5 days", ob)
        self.assertIn("in 4 days", ob)
        self.assertRegex(ob, r'g g-warning" aria-label="warning">■')
        # The first row is selected on the first read and Details shows it.
        self.assertIn("Confirm the invoice was paid", out["R"]["det"])
        self.assertIn("2026-09-01 · overdue 5 days", out["R"]["det"])
        self.assertEqual(re.findall(r'<button type="button">([^<]+)</button>', out["R"]["al"]),
                         ["Tank Low Alarm", "A2 VSD Normal", "A2 VSD Alarm", "Generator Running"])

    def test_a_missing_config_is_the_empty_state_and_no_cell_is_lit(self):
        out = self.run_page("R.lamps = ELS.annunciator.html; R.map = ELS.map.html;", self.doc(self.root / "absent.conf"))
        self.assertEqual(out["states"][-1]["kind"], "empty")
        self.assertIn("Copy project-dashboard/hoa.conf.example to <config>/project-dashboard/absent.conf", out["states"][-1]["text"])
        self.assertNotRegex(out["R"]["lamps"], r'data-state="(ok|caution|warning)"')
        self.assertIn("Not read.", out["R"]["map"])
        self.assertEqual(out["attention"][-1], {"state": "ok", "n": 0, "what": "HOA water system"})

    def test_a_broken_file_is_a_partial_read_and_its_cells_are_unknown(self):
        hoa = checkout(self.root / "broken", pump="date,a2_starts\n2026-09-01,1\n")
        self.config.write_text(f"repo|{hoa}\n", encoding="utf-8")
        out = self.run_page("R.lamps = ELS.annunciator.html; R.trends = ELS['trend-list'].html;")
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("Mission export: pump-daily.csv has no", out["states"][-1]["text"])
        self.assertRegex(out["R"]["lamps"], r'data-cell="pumps" data-state="unknown".*?pump-daily.csv has no')
        self.assertRegex(out["R"]["lamps"], r'data-cell="tank"[^>]*data-state="ok"')
        self.assertIn("<b>▨ Not read.</b> pump-daily.csv has no", out["R"]["trends"])

    def test_a_document_that_does_not_arrive_leaves_no_row_and_says_so(self):
        out = self.run_page("""R.before = ELS['ob-body'].html;
DOC = { error: 'Open a dashboard page before reading HOA.' }; STATUS = 403;
ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? { id: 'refresh', dataset: {} } : null } }); await flush();
R.ob = ELS['ob-body'].html; R.lamps = ELS.annunciator.html;""")
        self.assertEqual(out["gets"], ["/api/hoa", "/api/hoa"])
        self.assertIn("data-id=", out["R"]["before"])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertNotIn("data-id=", out["R"]["ob"])
        self.assertNotIn("data-cell=", out["R"]["lamps"])
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "HOA not read"})

    def test_the_open_command_names_the_item_and_opens_it_in_tasks(self):
        out = self.run_page(f"""var o = OBJ.get('followup:{self.late}'); var c = REG.find(x => x.id === 'followup.open');
R.cli = c.cli(o); R.run = c.run(o); R.href = location.href;""")
        self.assertEqual(out["R"]["cli"], f"sd task show {self.late}")
        self.assertEqual(out["R"]["href"], f"/tasks?row={self.late}")

    def test_the_page_keeps_the_shells_keys_and_reads_through_the_shared_reader(self):
        self.assertIn("S.read(spec)", HOA_JS)
        self.assertNotIn("innerHTML", HOA_JS)
        self.assertNotRegex(HOA_JS, r"\.style\.")
        self.assertNotRegex(HOA_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", HOA_JS)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "hoa", "HOA", "/hoa", ("/api/hoa",)


if __name__ == "__main__":
    unittest.main()
