"""The v2 HOA page (sd:2116).

What this slice promises: `/hoa` answers under the shared policy and loads its
script before `shell.js`; `/api/hoa` reads the checkout the repo table holds
under the folder name `hoa` (sd:3985; no config file names it): the asset map, the Mission export's last 30 days, and the
checkout's open items of every kind ranked overdue, due soon, then the rest. Each
source is guarded on its own, so a file that is missing or does not parse is
that part's error and the page shows it as unknown with the reason, never as
an empty list. An export whose newest row is more than two days old is stale.

`hoa.js` runs under JavaScriptCore (osascript) against the stand-in page and
shell `test_v2_tasks` defines, with the document above as its fetch answer.
The browser half -- the map's drag and zoom, the look at 375 px -- is a manual
check recorded on the pull request.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import hoa_screen, server, v2

from sd_db import connect, upsert_repo, workflow, writes
from sd_db.migrate import initialise

from support import NOW, ScreenCase
from test_v2_today import OSASCRIPT, Refused
from test_v2_read import READ_SHELL
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers
from test_v2_shell_shared import cell_grammar

V2 = Path(v2.__file__).resolve().parent
HOA_JS = (V2 / "static" / "hoa.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

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


def parcels(root: Path, rows=None, *, base_sha=None) -> Path:
    """The hoa checkout's downloads/parcels/lots.json as tools/fetch-lot-parcels.py writes it. Synthetic names only."""
    rows = rows if rows is not None else [{"index": 0, "ain": "TEST-0001", "address": "100 Example St", "owner": "Test Owner A"},
                                          {"index": 1, "ain": "TEST-0002", "address": "102 Example St", "owner": "Test Owner B"}]
    sha = base_sha or hashlib.sha256((root / "water" / "map" / "base.geojson").read_bytes()).hexdigest()
    out = root / "downloads" / "parcels" / "lots.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"fetched_at": "2026-09-05T12:00:00+00:00", "source": "Example County GIS", "base_sha256": sha,
                               "lot_count": 2, "lots": rows, "unmatched": []}), encoding="utf-8")
    return out


def checkout(root: Path, *, analog=ANALOG, pump=PUMP, alarms=ALARMS, base=BASE, assets=ASSETS) -> Path:
    """A checkout laid out as the design reads it."""
    shutil.rmtree(root, ignore_errors=True)
    mission, maps = root / "water" / "metrics" / "mission", root / "water" / "map"
    mission.mkdir(parents=True)
    maps.mkdir(parents=True)
    for name, text in (("analog-daily.csv", analog), ("pump-daily.csv", pump), ("alarm-events.csv", alarms)):
        if text is not None:
            (mission / name).write_text(text, encoding="utf-8")
    for name, data in (("base.geojson", base), ("assets.geojson", assets)):
        (maps / name).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return root


class HoaJobs:
    """An operations backend double: `names` and `inspect`, the shape `operations.job_state` reads, and the `cron_root`
    whose `logs/` holds each job's log. A job not in `jobs` is not installed, refused as `LaunchdBackend` refuses it.
    No launchctl call is made."""

    def __init__(self, root, jobs=()):
        self.cron_root = Path(root) / "local-cron-jobs"
        (self.cron_root / "logs").mkdir(parents=True, exist_ok=True)
        self.jobs = dict(jobs)

    def names(self):
        return list(self.jobs)

    def inspect(self, name):
        if name not in self.jobs:
            raise workflow.WorkflowError("job is not supported: installed configuration is unavailable or invalid")
        state, code = self.jobs[name]
        return {"name": name, "label": "fixture." + name, "service": "fixture/" + name, "schedule": [],
                "state": state, "pid": 7 if state == "running" else None, "last_exit": code, "last_signal": None,
                "runs": 1, "lifetime": None, "boot": None, "reason": "", "_observation": f"{name} {state} {code}"}

    def log(self, name, text):
        (self.cron_root / "logs" / f"{name}.log").write_text(text)


class Fixture(ScreenCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # Never this machine's levels: a test that wants one writes this file.
        self.levels = self.root / "hoa-thresholds.conf"
        patch = mock.patch.object(hoa_screen, "THRESHOLDS", self.levels)
        patch.start()
        self.addCleanup(patch.stop)
        # Never this machine's launchd: a test that wants a refresh job installed names it here.
        self.jobs = HoaJobs(self.root)
        self.hoa = checkout(self.root / "hoa")
        self.lot_data = parcels(self.hoa)
        self.repo(str(self.hoa))
        self.repo("/work/other")
        self.late = self.item("Confirm the invoice was paid", kind="followup", repo=str(self.hoa), due="2026-09-01", priority=1)
        self.soon = self.item("Chase the valve schedule", kind="followup", repo=str(self.hoa), due="2026-09-10", priority=2)
        self.later = self.item("Deliver the annual report", kind="followup", repo=str(self.hoa), due="2026-10-30", priority=1)
        self.item("Already settled", kind="followup", repo=str(self.hoa), status="done", due="2026-08-01")
        self.task = self.item("Paint the pump house", kind="task", repo=str(self.hoa), due="2026-09-20", priority=2)
        self.blocked = self.item("Wait on the county permit", kind="task", repo=str(self.hoa), status="blocked")
        self.item("A report is no task", kind="report", repo=str(self.hoa), due="2026-08-01")
        self.item("Another checkout's followup", kind="followup", repo="/work/other", due="2026-08-01")

    def doc(self):
        return hoa_screen.document(self.connection, now=NOW, jobs=self.jobs)

    def bare(self, *paths):
        """A document from a database that registers only `paths`."""
        database = self.root / "bare.db"
        initialise(database)
        connection = connect(database)
        self.addCleanup(connection.close)
        for path in paths:
            upsert_repo(connection, path, remote="git@example.invalid:x.git")
        return hoa_screen.document(connection, now=NOW, jobs=self.jobs)


class TheDocument(Fixture):
    def test_without_a_registered_hoa_checkout_every_part_names_what_it_did_not_read(self):
        doc = self.bare("/work/other")
        self.assertEqual(doc["config"], {"state": "missing", "source": "the repo table", "problems": []})
        self.assertIn("no repo named hoa", doc["mission"]["error"])
        self.assertIn("no repo named hoa", doc["assets"]["error"])
        self.assertIn("no repo named hoa", doc["items"]["error"])
        self.assertEqual((doc["mission"]["analog"], doc["assets"]["features"], doc["items"]["rows"]), ([], [], []))

    def test_the_export_is_its_last_30_days_and_its_age(self):
        m = self.doc()["mission"]
        self.assertEqual(m["error"], "")
        self.assertEqual([f["rows"] for f in m["files"]], [48, 32, 5])
        self.assertEqual((len(m["analog"]), m["analog"][-1]["date"], m["pump"][-1]["date"]), (30, "2026-09-05", "2026-09-04"))
        self.assertEqual((m["latest"], m["age_days"], m["stale"]), ("2026-09-05", 1, False))
        # 30 days back from the newest alarm: the July row is gone, oldest first.
        self.assertEqual([a["event"] for a in m["alarms"]], ["Generator Running", "A2 VSD Alarm", "A2 VSD Normal", "Tank Low Alarm"])
        self.assertEqual(m["alarms_today"], {"count": None, "reason": "the export ends 2026-09-05", "acked": 0})

    def test_an_export_more_than_two_days_old_is_stale(self):
        self.assertEqual(hoa_screen.STALE_DAYS, 2)
        m = hoa_screen.document(self.connection, now="2026-09-08T12:00:00Z")["mission"]
        self.assertEqual((m["age_days"], m["stale"]), (3, True))
        m = hoa_screen.document(self.connection, now="2026-09-07T12:00:00Z")["mission"]
        self.assertEqual((m["age_days"], m["stale"]), (2, False))

    def test_alarms_today_count_only_real_alarms_on_a_day_the_export_reaches(self):
        alarms = ALARMS + ("2026-09-06 01:00:00,other,A2 VSD Alarm,N,2\n2026-09-06 01:10:00,other,A2 VSD Normal,,0\n"
                           "2026-09-06 02:00:00,generator,Generator Running,N,15\n")
        hoa = checkout(self.hoa, alarms=alarms)
        self.assertEqual(self.doc()["mission"]["alarms_today"], {"count": 1, "reason": "", "acked": 0})

    def test_a_file_that_does_not_parse_is_its_error_and_the_rest_still_answer(self):
        hoa = checkout(self.hoa, pump="date,a2_starts\n2026-09-01,1\n", alarms=None, assets="{not json")
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
        hoa = checkout(self.hoa, analog=ANALOG + ",96,1,2,3,4,5,6,7,8,9,10,11,12\n")
        files = {f["name"]: f["error"] for f in self.doc()["mission"]["files"]}
        self.assertEqual(files["analog-daily.csv"], "analog-daily.csv line 50: '' is not a date")

    def test_items_are_the_checkouts_open_items_of_every_kind_ranked_by_urgency(self):
        # sd:4959: one table of every open hoa item; a report is its family's, so it is not here.
        rows = self.doc()["items"]["rows"]
        self.assertEqual([(r["item"], r["kind"], r["state"], r["due_days"]) for r in rows],
                         [(self.late, "followup", "warning", -5), (self.soon, "followup", "caution", 4),
                          (self.later, "followup", "queued", 54), (self.task, "task", "queued", 14),
                          (self.blocked, "task", "queued", None)])
        self.assertEqual(rows[0]["id"], f"item:{self.late}")
        self.assertEqual((rows[0]["priority"], rows[-1]["status"]), (1, "blocked"))

    # sd:4964: each lot carries its address and owner of record from the checkout's downloads/parcels/lots.json, joined by
    # the lot's index among base.geojson's lots, and only against the base file the lot data was made from.
    def test_each_lot_carries_its_address_and_owner_from_the_lot_data(self):
        doc = self.doc()
        self.assertEqual(doc["lots"], {"error": "", "source": str(self.hoa / "downloads" / "parcels" / "lots.json"), "fetched_at": "2026-09-05T12:00:00+00:00"})
        lots = [f for f in doc["base"]["features"] if f["kind"] == "lot"]
        self.assertEqual([(l["index"], l["address"], l["owner"], l["parcel"]) for l in lots],
                         [(0, "100 Example St", "Test Owner A", "TEST-0001"), (1, "102 Example St", "Test Owner B", "TEST-0002")])
        self.assertNotIn("owner", next(f for f in doc["base"]["features"] if f["kind"] == "tract"))

    def test_lot_data_not_fetched_is_unknown_and_names_the_script(self):
        self.lot_data.unlink()
        doc = self.doc()
        self.assertEqual(doc["lots"]["error"], "lot data not fetched: run python3 tools/fetch-lot-parcels.py in the hoa checkout")
        self.assertEqual([l.get("owner") for l in doc["base"]["features"] if l["kind"] == "lot"], [None, None])

    def test_lot_data_made_from_another_base_file_is_not_joined(self):
        parcels(self.hoa, base_sha="0" * 64)
        doc = self.doc()
        self.assertEqual(doc["lots"]["error"], "lot data is for another base.geojson: run python3 tools/fetch-lot-parcels.py in the hoa checkout")
        self.assertEqual([l.get("owner") for l in doc["base"]["features"] if l["kind"] == "lot"], [None, None])

    def test_lot_data_that_does_not_parse_is_named(self):
        out = parcels(self.hoa)
        out.write_text("{not json", encoding="utf-8")
        self.assertRegex(self.doc()["lots"]["error"], r"^lots.json did not parse: JSONDecodeError")
        parcels(self.hoa, rows=[{"index": "zero", "address": 1}])
        self.assertRegex(self.doc()["lots"]["error"], r"^lots.json did not parse: ")

    def test_assets_and_outlines_carry_their_coordinates(self):
        doc = self.doc()
        self.assertEqual([(a["id"], a["type"], a["lng"], a["lat"]) for a in doc["assets"]["features"]][1], ("WELL-A2", "well", 10.0012, 50.0004))
        self.assertEqual([l["kind"] for l in doc["base"]["features"]], ["lot", "lot", "tract", "plat"])
        self.assertEqual(len(doc["base"]["features"][2]["rings"][0]), 5)

    def test_the_checkout_is_the_registered_repo_whose_folder_is_named_hoa(self):
        doc = self.doc()
        self.assertEqual(doc["config"], {"state": "read", "source": "the repo table", "problems": []})
        self.assertEqual(doc["mission"]["dir"], str(self.hoa / "water" / "metrics" / "mission"))

    def test_two_registered_hoa_folders_are_named_and_neither_is_read(self):
        other = self.root / "elsewhere" / "hoa"
        self.repo(str(other))
        doc = self.doc()
        self.assertEqual(doc["config"]["state"], "error")
        self.assertEqual(doc["config"]["problems"], [f"two repos are named hoa: {other}, {self.hoa}"])
        self.assertIn("two repos are named hoa", doc["mission"]["error"])
        self.assertEqual(doc["items"]["rows"], [])

    def test_a_folder_that_only_contains_hoa_in_its_name_is_not_the_checkout(self):
        doc = self.bare("/work/hoa-notes", "/work/not-hoa", "/work/hoa/water")
        self.assertEqual(doc["config"]["state"], "missing")

    def test_a_tilde_path_reads_from_the_home_folder(self):
        home = self.root / "home"
        checkout(home / "repos" / "hoa")
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            doc = self.bare("~/repos/hoa")
        self.assertEqual(doc["config"]["state"], "read")
        self.assertEqual(doc["mission"]["error"], "")


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/hoa")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>HOA · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "commands.js", "hoa.js", "places.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/hoa")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/hoa?range=7", headers=cookie)[0], 400)
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

    def run_page(self, body, doc=None, status=200, pre=""):
        doc = doc if doc is not None else self.doc()
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + HOA_SHELL + READ_SHELL
                  + f"\nvar DOC = {json.dumps(doc)}, STATUS = {status};\n"
                  + "ANSWER = (path, body) => path === '/api/hoa' ? [STATUS, DOC] : [404, { error: 'no answer' }];\n"
                  + pre + "\n" + COMMANDS_JS + HOA_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\n"
                  + "(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_every_does_line_keeps_the_shared_word_limit(self):
        # Retry and Show log are spread from commands.js, which test_v2_commands' scan cannot read, so HOA checks its
        # registered commands here, a per-row text once for each source.
        from test_v2_commands import WORDS
        out = self.run_page("""R.d = REG.filter(c => c.does).flatMap(c => typeof c.does === 'function'
  ? ['vantaca', 'mission'].map(source => [c.id, c.does({ source })]) : [[c.id, c.does]]);""")
        long = [f"{command}: {does}" for command, does in out["R"]["d"] if len(does.split()) > WORDS]
        self.assertEqual(long, [], f"a does line over {WORDS} words")

    def test_every_command_is_registered_with_the_design_id_risk_and_key(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.risk, c.key || null]);")
        self.assertEqual(out["R"]["reg"], [
            ["item.draft", "item", "safe", "d"],
            ["item.open", "item", "safe", "o"],
            ["asset.ask", "asset", "safe", "a"],
            ["alarm.ack", "alarm", "undo", "a"],
            ["alarm.unack", "alarm", "undo", "a"],
            ["jobs.retry", "job", "safe", "t"],
            ["jobs.log", "job", "safe", "l"],
            ["sweep.run", "sweep", "safe", "w"],
        ])

    def test_every_lamp_says_what_it_measures_and_its_source_on_hover_and_focus(self):
        # sd:5287: the shell's tip (data-tip) on each lamp: what it measures, then its source.
        out = self.run_page("R.lamps = ELS.annunciator.html;")
        tips = dict(re.findall(r'data-cell="(\w+)"[^>]*data-tip="([^"]+)"', out["R"]["lamps"]))
        self.assertEqual(sorted(tips), ["alarms", "aquifer", "mission", "pumps", "tank", "tasks", "water"])
        self.assertIn("Source: alarm-events.csv", tips["alarms"])
        self.assertIn("CLI: python3 tools/source-ages.py", tips["mission"])
        self.assertRegex(out["R"]["lamps"], r'id="refresh"[^>]*data-tip="When this page read /api/hoa')

    def test_each_cell_is_a_reading_or_unknown_with_its_reason(self):
        out = self.run_page("R.lamps = ELS.annunciator.html; R.sub = ELS.subhead.html;")
        self.assertEqual(out["gets"], ["/api/hoa"])
        lamps = out["R"]["lamps"]
        self.assertRegex(lamps, r'data-cell="water" data-state="unknown".*?no live reader')
        self.assertRegex(lamps, r'data-cell="tank" data-open="day:2026-09-05" data-state="ok".*?<b>16.1</b> ft mean')
        self.assertRegex(lamps, r'data-cell="pumps" data-open="day:2026-09-04" data-state="ok".*?<b>16.9</b> h A2.*?0 starts · 91,119 gal · 09-04')
        self.assertRegex(lamps, r'data-cell="alarms"[^>]*data-state="unknown".*?the export ends 2026-09-05.*?2 alarms in 30 d · 1 generator test')
        self.assertRegex(lamps, rf'data-cell="tasks" data-open="item:{self.late}" data-state="warning".*?Tasks.*?<b>1</b> overdue.*?5 open · 1 due within a week')
        self.assertRegex(lamps, r'data-cell="mission" data-open="source:mission" data-state="ok".*?<b>1 d</b> old')
        self.assertEqual(cell_grammar(self, lamps)[-1], ("button", None))
        self.assertIn("3 mapped assets, 4 lot and tract outlines; Mission export through 2026-09-05", out["R"]["sub"])
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 1, "what": "HOA annunciator cells"})
        self.assertIsNone(out["states"][-1])

    def test_the_badge_rule_lights_what_the_lamps_light(self):
        # sd:3446: the rule is the server's, and every page's rail shows it; it must count the cells hoa.js lights.
        def lit(doc):
            lamps = self.run_page("R.lamps = ELS.annunciator.html;", doc)["R"]["lamps"]
            states = re.findall(r'data-cell="\w+"[^>]*data-state="(\w+)"', lamps)
            worst = "warning" if "warning" in states else "caution" if "caution" in states else "ok"
            return worst, states.count(worst) if worst != "ok" else 0

        cases = []
        for alarms, stale, rows in ((None, False, None), (2, True, None), (0, True, []), (1, False, [])):
            doc = self.doc()
            doc["mission"]["alarms_today"]["count"], doc["mission"]["stale"] = alarms, stale
            if rows is not None:
                doc["items"]["rows"] = [dict(row, state="caution") for row in doc["items"]["rows"]]
            doc["attention"] = hoa_screen._attention(doc["mission"], doc["items"])
            cases.append((lit(doc), (doc["attention"]["state"], doc["attention"]["n"])))
        for drawn, counted in cases:
            self.assertEqual(drawn, counted)
        self.assertEqual([counted for _, counted in cases], [("warning", 1), ("warning", 2), ("caution", 2), ("warning", 1)])

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

    def test_the_tasks_are_ranked_rows_with_their_due_state(self):
        out = self.run_page("R.ob = ELS['ob-body'].html; R.al = ELS['al-body'].html; R.det = ELS.details.html;")
        ob = out["R"]["ob"]
        self.assertEqual(re.findall(r'<tr data-id="item:(\d+)"', ob), [str(i) for i in (self.late, self.soon, self.later, self.task, self.blocked)])
        self.assertIn("overdue 5 days", ob)
        self.assertIn("in 4 days", ob)
        self.assertRegex(ob, r'g g-warning" aria-label="warning">■')
        # The first row is selected on the first read and Details shows it.
        self.assertIn("Confirm the invoice was paid", out["R"]["det"])
        self.assertIn("2026-09-01 · overdue 5 days", out["R"]["det"])
        self.assertEqual(re.findall(r'<button type="button">([^<]+)</button>', out["R"]["al"]),
                         ["Tank Low Alarm", "A2 VSD Normal", "A2 VSD Alarm", "Generator Running"])

    # sd:4959: the HOA Tasks view is the one table of every open hoa item: the id is its own first column, every column
    # with data sorts through shell.list, a third click on a header returns to the urgency rank, and Kind narrows the rows.
    def sort(self, key):
        return f"ELS['ob-head'].listeners.click[0]({{ target: {{ closest: s => s === 'button[data-sort]' ? {{ dataset: {{ sort: '{key}' }} }} : null }} }}); "

    def test_the_id_is_the_first_column_and_every_data_column_sorts(self):
        out = self.run_page("R.head = ELS['ob-head'].html;")
        heads = re.findall(r"<th\b.*?</th>", out["R"]["head"], re.S)
        self.assertIn('data-sort="item"', heads[0])
        self.assertIn(">ID<", heads[0])
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["head"]), ["item", "rank", "title", "kind", "due", "status"])
        self.assertEqual([h for h in heads if "data-sort" not in h], [heads[-1]])
        self.assertIn("Actions", heads[-1])

    def test_the_id_sorts_by_number_and_a_third_click_returns_to_the_rank(self):
        doc = self.doc()
        nine, ten = (dict(doc["items"]["rows"][0], id=f"item:{n}", item=n) for n in (9, 10))
        doc["items"]["rows"] = [ten, nine]
        ids = "re = () => [...ELS['ob-body'].html.matchAll(/data-id=\"item:(\\d+)\"/g)].map(m => +m[1]); "
        out = self.run_page(ids + "R.rank = re(); " + self.sort("item") + "R.up = re(); R.cell = ELS['ob-body'].html; "
                            + self.sort("item") + "R.down = re(); " + self.sort("item") + "R.back = re(); R.head = ELS['ob-head'].html;", doc)
        self.assertEqual((out["R"]["rank"], out["R"]["up"], out["R"]["down"], out["R"]["back"]), ([10, 9], [9, 10], [10, 9], [10, 9]))
        self.assertIn('<td class="id num">sd:9</td>', out["R"]["cell"])
        self.assertNotIn("aria-sort", out["R"]["head"])

    def test_a_followups_row_action_drafts_and_any_other_kinds_opens_the_item(self):
        out = self.run_page(f"""const p = (id, o) => !!REG.find(c => c.id === id).primary(OBJ.get('item:' + o));
R.p = [p('item.draft', {self.late}), p('item.open', {self.late}), p('item.draft', {self.task}), p('item.open', {self.task})];""")
        self.assertEqual(out["R"]["p"], [True, False, False, True])

    def test_one_followup_one_task_and_one_blocked_task_show_and_kind_narrows(self):
        kind = "ELS['ob-kinds'].listeners.click[0]({ target: { closest: s => s === 'button[data-kind]' ? { dataset: { kind: 'K' } } : null } }); "
        rows = "re = () => [...ELS['ob-body'].html.matchAll(/data-id=\"item:(\\d+)\"/g)].map(m => +m[1]); "
        out = self.run_page(rows + "R.all = re(); R.chips = ELS['ob-kinds'].html; " + kind.replace("K", "task") + "R.task = re(); "
                            + kind.replace("K", "followup") + "R.followup = re(); " + kind.replace("K", "all") + "R.again = re(); R.tally = ELS['tasks-tally'].html;")
        everything = [self.late, self.soon, self.later, self.task, self.blocked]
        self.assertEqual(out["R"]["all"], everything)
        self.assertEqual(out["R"]["task"], [self.task, self.blocked])
        self.assertEqual(out["R"]["followup"], [self.late, self.soon, self.later])
        self.assertEqual(out["R"]["again"], everything)
        self.assertEqual(re.findall(r'data-kind="(\w+)" aria-pressed="(\w+)"', out["R"]["chips"]),
                         [("all", "true"), ("followup", "false"), ("task", "false")])
        self.assertIn("5 open items", out["R"]["tally"])

    def test_a_lot_shows_its_address_as_its_label_and_its_owner_in_details(self):
        tap = "ELS.map.listeners.click[0]({ target: { closest: s => s === '[data-lot]' ? { dataset: { lot: '1' } } : null } }); "
        out = self.run_page("R.map = ELS.map.html; ELS['zoom-in'].listeners.click[0](); ELS['zoom-in'].listeners.click[0](); R.zoomed = ELS.map.html; "
                            + tap + "R.det = ELS.details.html;")
        self.assertIn('<path class="lot k-lot" data-lot="0"', out["R"]["map"])
        self.assertIn("<title>100 Example St</title>", out["R"]["map"])
        self.assertNotIn('class="lot-label"', out["R"]["map"])
        self.assertRegex(out["R"]["zoomed"], r'<text class="lot-label"[^>]*>102 Example St</text>')
        det = out["R"]["det"]
        self.assertIn("<h2>102 Example St</h2>", det)
        self.assertIn("<dt>Owner of record</dt><dd>Test Owner B</dd>", det)
        self.assertIn("<dt>AIN</dt><dd>TEST-0002</dd>", det)
        self.assertIn("2026-09-05", det)

    def test_lot_data_not_fetched_is_a_partial_read_and_the_lot_says_why(self):
        self.lot_data.unlink()
        tap = "ELS.map.listeners.click[0]({ target: { closest: s => s === '[data-lot]' ? { dataset: { lot: '0' } } : null } }); "
        out = self.run_page(tap + "R.det = ELS.details.html;")
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("Lots: lot data not fetched: run python3 tools/fetch-lot-parcels.py", out["states"][-1]["text"])
        self.assertIn("<b>▨ Not read.</b> lot data not fetched: run python3 tools/fetch-lot-parcels.py", out["R"]["det"])
        self.assertIn("<h2>Lot 1</h2>", out["R"]["det"])

    def test_a_missing_config_is_the_empty_state_and_no_cell_is_lit(self):
        out = self.run_page("R.lamps = ELS.annunciator.html; R.map = ELS.map.html;", self.bare("/work/other"))
        self.assertEqual(out["states"][-1]["kind"], "empty")
        self.assertIn("Register the HOA checkout", out["states"][-1]["text"])
        self.assertNotRegex(out["R"]["lamps"], r'data-state="(ok|caution|warning)"')
        self.assertIn("Not read.", out["R"]["map"])
        self.assertEqual(out["attention"][-1], {"state": "ok", "n": 0, "what": "HOA water system"})

    def test_a_broken_file_is_a_partial_read_and_its_cells_are_unknown(self):
        hoa = checkout(self.hoa, pump="date,a2_starts\n2026-09-01,1\n")
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
        out = self.run_page(f"""var o = OBJ.get('item:{self.late}'); var c = REG.find(x => x.id === 'item.open');
R.cli = c.cli(o); R.run = c.run(o); R.href = location.href;""")
        self.assertEqual(out["R"]["cli"], f"sd task show {self.late}")
        self.assertEqual(out["R"]["href"], f"/tasks?row={self.late}")

    def test_the_page_keeps_the_shells_keys_and_reads_through_the_shared_reader(self):
        self.assertIn("S.read(spec)", HOA_JS)
        self.assertNotIn("innerHTML", HOA_JS)
        self.assertNotRegex(HOA_JS, r"\.style\.")
        self.assertNotRegex(HOA_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", HOA_JS)


class TheTrendStatistics(Fixture):
    """sd:4969: each trend's window min, median and max, and its problem level (dashboard.hoa_thresholds, sd:5109)."""

    run_page = TheScript.run_page

    def trends(self, doc=None, body=""):
        return self.run_page(body + "R.trends = ELS['trend-list'].html;", doc)["R"]["trends"]

    @staticmethod
    def figure(trends, key):
        return re.search(rf'<figure class="spark" data-series="{key}">(.*?)</figure>', trends, re.S).group(1)

    @staticmethod
    def stats(fig):
        return re.findall(r"(min|median|max) <b>([^<]+)</b>", fig)

    def test_an_odd_window_reads_its_middle_day_and_its_lowest_low_and_highest_high(self):
        trends = self.trends()
        # The 7d window of pump-daily.csv: 12.6 to 13.1 for 08-26..08-31, then 16.9 on 09-04.
        self.assertEqual(self.stats(self.figure(trends, "runtime")), [("min", "12.6"), ("median", "12.9"), ("max", "16.9")])
        # Aquifer means 181..185, 210, 211: the median is 184; min and max are the daily min and max columns.
        self.assertEqual(self.stats(self.figure(trends, "aquifer")), [("min", "150.0"), ("median", "184.0"), ("max", "240.0")])
        runtime = self.figure(trends, "runtime")
        self.assertIn('data-ext="min"', runtime)
        self.assertIn('data-ext="max"', runtime)

    def test_an_even_window_with_a_gap_reads_the_mean_of_its_middle_two_and_skips_the_gap(self):
        doc = self.doc()
        doc["mission"]["pump"] = [dict(doc["mission"]["pump"][0], date=f"2026-09-0{d}", a2_runtime_hours=v)
                                  for d, v in ((1, "1"), (2, "4"), (3, ""), (4, "2"), (5, "10"))]
        fig = self.figure(self.trends(doc), "runtime")
        # Four days with a reading: 1, 2, 4, 10. The blank day is not a zero.
        self.assertEqual(self.stats(fig), [("min", "1.0"), ("median", "3.0"), ("max", "10.0")])

    def test_a_level_draws_its_line_and_puts_each_day_beyond_it_in_the_warning_state(self):
        self.levels.write_text("# sample\nruntime|above|13\ntank|below|15.5\n", encoding="utf-8")
        trends = self.trends()
        runtime, tank, aquifer = (self.figure(trends, key) for key in ("runtime", "tank", "aquifer"))
        self.assertIn('class="limit"', runtime)
        self.assertEqual(len(re.findall(r'class="bar[^"]* over"', runtime)), 2)
        self.assertIn("problem level above 13 h · 2 days beyond", runtime)
        self.assertIn("g-warning", runtime)
        self.assertIn('class="limit"', tank)
        self.assertNotIn('class="over"', tank)
        self.assertIn("problem level below 15.5 ft · no day beyond", tank)
        self.assertNotIn('class="limit"', aquifer)
        self.assertIn("no problem level set", aquifer)

    def test_a_line_series_marks_a_day_whose_low_is_under_its_level(self):
        self.levels.write_text("tank|below|16.5\n", encoding="utf-8")
        tank = self.figure(self.trends(), "tank")
        # Every tank day's low is 16.0: all seven are beyond.
        self.assertEqual(len(re.findall(r'class="over"', tank)), 7)
        self.assertIn("7 days beyond", tank)

    def test_the_levels_file_names_each_line_it_does_not_read(self):
        self.levels.write_text("tank|below|change-me\npump|above|3\naquifer|level|100\naquifer|above|250\naquifer|below|1\n"
                               "runtime|above|inf\n", encoding="utf-8")
        got = self.doc()["thresholds"]
        self.assertEqual(got["levels"], {"aquifer": {"dir": "above", "value": 250.0}})
        self.assertEqual([p.split(":")[0] for p in got["problems"]], ["line 1", "line 2", "line 3", "line 5", "line 6"])
        self.assertIn("'change-me' is not a number", got["problems"][0])
        self.assertIn("aquifer has a level already", got["problems"][3])
        self.assertEqual(got["source"], "<config>/project-dashboard/hoa-thresholds.conf")

    def test_no_levels_file_is_no_level_and_no_error(self):
        self.assertEqual(self.doc()["thresholds"], {"source": "<config>/project-dashboard/hoa-thresholds.conf", "levels": {},
                                                    "problems": [], "error": ""})

    def test_an_unreadable_levels_file_is_unknown_on_every_trend(self):
        self.levels.mkdir()
        doc = self.doc()
        self.assertEqual(doc["thresholds"]["error"], "IsADirectoryError")
        self.assertIn("problem level not read", self.figure(self.trends(doc), "tank"))

    def test_a_levels_row_wins_over_the_file_and_a_cleared_one_hides_it(self):
        """sd:5109: the dashboard.hoa_thresholds row holds the levels; the old file answers only before any row."""
        self.levels.write_text("tank|below|12\n", encoding="utf-8")
        writes.set_setting(self.connection, "dashboard.hoa_thresholds", "aquifer|above|250\n")
        got = self.doc()["thresholds"]
        self.assertEqual((got["levels"], got["source"]), ({"aquifer": {"dir": "above", "value": 250.0}},
                                                          "workflow database setting dashboard.hoa_thresholds"))
        writes.set_setting(self.connection, "dashboard.hoa_thresholds", None)
        self.assertEqual(self.doc()["thresholds"]["levels"], {})

    def test_a_levels_row_that_does_not_read_is_unknown_on_every_trend(self):
        self.levels.write_text("tank|below|12\n", encoding="utf-8")

        def refuse(connection, keys):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(writes, "read_settings", refuse):
            doc = self.doc()
        self.assertEqual(doc["thresholds"]["levels"], {})
        self.assertIn("database is locked", doc["thresholds"]["error"])
        self.assertIn("problem level not read", self.figure(self.trends(doc), "tank"))

    def test_the_levels_name_only_series_the_page_draws(self):
        levels = {}
        self.assertEqual(hoa_screen.parse_thresholds("tank|below|12\nruntime|above|9\naquifer|below|100\n", levels), [])
        self.assertEqual(list(levels), ["tank", "runtime", "aquifer"])
        self.assertEqual(re.findall(r"key: '(\w+)'", HOA_JS.split("const SERIES = [")[1].split("];")[0]), list(hoa_screen.SERIES))


#: The fixture's newest alarm event, as the server keys it.
TANK_LOW = "hoa-alarm/2026-09-05 06:00:00 Tank Low Alarm"


class TheAcknowledgement(Fixture):
    """sd:4967: an acknowledged alarm event is a hidden view row keyed by its datetime and text; Undo writes it back."""

    def ack(self, key, acked=True):
        result = hoa_screen.acknowledge({"key": key, "acked": acked})(self.connection)
        self.connection.commit()
        return result

    def alarms(self, doc=None):
        return {row["key"]: row["acked"] for row in (doc or self.doc())["mission"]["alarms"]}

    def test_each_event_is_keyed_by_its_datetime_and_text_and_none_is_acknowledged(self):
        doc = self.doc()
        self.assertEqual(self.alarms(doc), {
            "hoa-alarm/2026-08-20 09:00:00 Generator Running": False, "hoa-alarm/2026-09-01 10:00:00 A2 VSD Alarm": False,
            "hoa-alarm/2026-09-01 10:30:00 A2 VSD Normal": False, TANK_LOW: False})
        self.assertEqual(doc["mission"]["acks"], {"available": True, "reason": ""})

    def test_acknowledge_hides_the_event_and_undo_restores_it(self):
        self.assertEqual(self.ack(TANK_LOW)["hidden"], True)
        self.assertEqual([k for k, acked in self.alarms().items() if acked], [TANK_LOW])
        self.ack(TANK_LOW, False)
        self.assertEqual([k for k, acked in self.alarms().items() if acked], [])

    def test_the_key_holds_across_reads_and_a_newer_export(self):
        self.ack(TANK_LOW)
        first = self.alarms()
        self.assertEqual(self.alarms(), first)
        checkout(self.hoa, alarms=ALARMS + "2026-09-05 23:00:00,other,A2 VSD Alarm,N,1\n")
        later = self.alarms()
        self.assertTrue(later[TANK_LOW])
        self.assertEqual({k: v for k, v in later.items() if k in first}, first)

    def test_a_key_the_export_no_longer_holds_is_refused_by_name_and_nothing_is_written(self):
        before = writes.views(self.connection)
        with self.assertRaises(workflow.StaleItem) as caught:
            self.ack("hoa-alarm/2026-09-05 06:00:00 Tank High Alarm")
        self.assertIn("holds no alarm event 2026-09-05 06:00:00 Tank High Alarm", str(caught.exception))
        self.assertIn("Nothing was acknowledged", str(caught.exception))
        self.assertEqual(writes.views(self.connection), before)

    def test_a_request_that_is_not_one_key_and_a_flag_is_refused_before_a_write(self):
        for payload in ({"key": TANK_LOW}, {"key": TANK_LOW, "acked": "yes"}, {"key": "designs/x.html", "acked": True},
                        {"key": TANK_LOW, "acked": True, "extra": 1}):
            with self.assertRaises(ValueError):
                hoa_screen.acknowledge(payload)

    def test_an_acknowledged_alarm_today_leaves_the_lamp_and_the_badge(self):
        checkout(self.hoa, alarms=ALARMS + "2026-09-06 07:00:00,other,A2 VSD Alarm,N,2\n")
        doc = self.doc()
        self.assertEqual(doc["mission"]["alarms_today"]["count"], 1)
        self.assertEqual(doc["attention"]["state"], "warning")
        self.ack("hoa-alarm/2026-09-06 07:00:00 A2 VSD Alarm")
        doc = self.doc()
        self.assertEqual((doc["mission"]["alarms_today"]["count"], doc["mission"]["alarms_today"]["acked"]), (0, 1))
        # The overdue followup still lights the badge; the alarm no longer does.
        self.assertEqual(doc["attention"], hoa_screen._attention(doc["mission"], doc["items"]))
        self.assertEqual(doc["attention"]["n"], 1)

    def test_a_store_that_does_not_read_acknowledges_nothing_and_says_why(self):
        self.ack(TANK_LOW)
        with mock.patch.object(writes, "views", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.doc()
        self.assertFalse(any(self.alarms(doc).values()))
        self.assertEqual(doc["mission"]["acks"], {"available": False, "reason": "the view store was not read: database is locked"})


class TheAcknowledgeCommand(Fixture):
    """hoa.js: the Alarms view shows Open, Acknowledged or All events; Acknowledge posts the key and Undo posts it back."""

    run_page = TheScript.run_page

    def acked_doc(self):
        doc = self.doc()
        for row in doc["mission"]["alarms"]:
            row["acked"] = row["key"] == TANK_LOW
        return doc

    def test_the_alarm_views_show_open_acknowledged_or_all_events(self):
        # sd:5286: Show acknowledged is three views on the left of the Alarms tab's row, not a checkbox chip.
        out = self.run_page("""const press = v => ELS['al-show'].listeners.click[0]({ target: { closest: s => s === '[data-alview]' ? { dataset: { alview: v } } : null } });
R.open = ELS['al-body'].html; R.seg = ELS['al-show'].html;
press('acked'); R.acked = ELS['al-body'].html; R.segAcked = ELS['al-show'].html;
press('all'); R.all = ELS['al-body'].html;""", self.acked_doc())
        rows = lambda html: re.findall(r'<button type="button">([^<]+)</button>', html)  # noqa: E731
        self.assertEqual(rows(out["R"]["open"]), ["A2 VSD Normal", "A2 VSD Alarm", "Generator Running"])
        self.assertEqual(rows(out["R"]["acked"]), ["Tank Low Alarm"])
        self.assertEqual(rows(out["R"]["all"]), ["Tank Low Alarm", "A2 VSD Normal", "A2 VSD Alarm", "Generator Running"])
        self.assertIn('<span class="tag">acknowledged</span>', out["R"]["all"])
        self.assertRegex(out["R"]["seg"], r'data-alview="open" aria-pressed="true">Open <span class="n">3</span>.*'
                                          r'data-alview="acked" aria-pressed="false">Acknowledged <span class="n">1</span>.*'
                                          r'data-alview="all" aria-pressed="false">All <span class="n">4</span>')
        self.assertIn('data-alview="acked" aria-pressed="true"', out["R"]["segAcked"])
        self.assertNotIn("Show acknowledged", out["R"]["seg"])

    def test_the_alarm_ledger_is_its_own_view_after_tasks(self):
        page = (V2 / "hoa.html").read_text(encoding="utf-8")
        overview = re.search(r'<div id="view-overview".*?\n  </div>\n', page, re.S).group(0)
        self.assertNotIn('id="al-table"', overview)
        views = re.findall(r'data-place-view="([a-z]+)"', page)
        self.assertEqual(views[:3], ["overview", "tasks", "alarms"])
        self.assertRegex(page, r'<table class="ledger" id="al-table" aria-labelledby="al-h" data-toolbar="al-toolbar">')

    def test_every_event_acknowledged_says_so_instead_of_an_empty_table(self):
        doc = self.doc()
        for row in doc["mission"]["alarms"]:
            row["acked"] = True
        out = self.run_page("R.al = ELS['al-body'].html;", doc)
        self.assertIn("Every alarm event in the last 30 days is acknowledged", out["R"]["al"])

    def test_acknowledge_posts_the_key_and_undo_posts_it_back(self):
        out = self.run_page(f"""ANSWER = (path, body) => path === '/api/hoa' ? [200, DOC] : [200, {{ key: body.key, hidden: body.acked }}];
const o = OBJ.get('alarm:2026-09-05 06:00:00 Tank Low Alarm');
R.when = cmd('alarm.ack').when(o); R.off = cmd('alarm.unack').when(o);
C.run(cmd('alarm.ack'), o); await flush();
OUT.toasts[OUT.toasts.length - 1].undo(); await flush();""")
        self.assertEqual((out["R"]["when"], out["R"]["off"]), (True, "not acknowledged"))
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/hoa/alarm", {"key": TANK_LOW, "acked": True}],
                                                         ["/api/hoa/alarm", {"key": TANK_LOW, "acked": False}]])
        self.assertIn("Acknowledged: 09-05 06:00 Tank Low Alarm. The Acknowledged view lists it.", out["toasts"][0][0])
        self.assertTrue(out["toasts"][0][1])
        # Each landed write reads HOA again.
        self.assertEqual(out["gets"], ["/api/hoa"] * 3)

    def test_a_refused_event_is_named_in_the_toast(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/hoa' ? [200, DOC]
  : [409, { error: 'alarm-events.csv holds no alarm event 2026-09-05 06:00:00 Tank Low Alarm. Nothing was acknowledged. Read HOA again.', reload: true }];
C.run(cmd('alarm.ack'), OBJ.get('alarm:2026-09-05 06:00:00 Tank Low Alarm')); await flush();""")
        self.assertTrue(any("holds no alarm event 2026-09-05 06:00:00 Tank Low Alarm" in t[0] for t in out["toasts"]), out["toasts"])

    def test_picked_rows_acknowledge_together_through_the_bulk_bar(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/hoa' ? [200, DOC] : [200, { key: body.key, hidden: body.acked }];
R.bulk = cmd('alarm.ack').bulk;
C.runBulk(cmd('alarm.ack'), ['alarm:2026-09-05 06:00:00 Tank Low Alarm', 'alarm:2026-09-01 10:00:00 A2 VSD Alarm'].map(id => OBJ.get(id)));
await flush();""")
        self.assertIs(out["R"]["bulk"], True)
        self.assertEqual(sorted(p[1]["key"] for p in out["posts"]), ["hoa-alarm/2026-09-01 10:00:00 A2 VSD Alarm", TANK_LOW])

    def test_an_unread_store_turns_both_commands_off_with_its_reason(self):
        doc = self.doc()
        doc["mission"]["acks"] = {"available": False, "reason": "the view store was not read: database is locked"}
        out = self.run_page("R.when = cmd('alarm.ack').when(OBJ.get('alarm:2026-09-05 06:00:00 Tank Low Alarm'));", doc)
        self.assertEqual(out["R"]["when"], "Acknowledge is off: the view store was not read: database is locked")


#: Timers recorded, not run: a running sweep reads HOA again every 15 s.
TIMERS = "var TIMERS = []; setTimeout = (f, ms) => { TIMERS.push(ms); return TIMERS.length; };"


class TheSweepRow(Fixture):
    """sd:5095: the sweep is one more Data sources row; Run sweep asks first, posts /api/hoa/sweep and reads HOA again."""

    run_page = TheScript.run_page

    def sweep_doc(self, **sweep):
        doc = self.doc()
        doc["sweep"] = {"running": False, "error": "", "command": "python3 tools/sweep.py run", "repo": str(self.hoa),
                        "start_log": "/state/start.log", "last": {"state": "failed", "exit": 1, "started": "2026-09-06T07:00:00+00:00",
                                                                 "reason": "RESULT: failed, step 3: Gmail unreachable"}} | sweep
        return doc

    def test_the_row_shows_the_last_run_and_run_sweep_posts_it(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/hoa' ? [200, DOC] : [202, { started: '2026-09-06T08:00:00+00:00' }];
R.row = ELS['src-body'].html; R.when = cmd('sweep.run').when(OBJ.get('sweep')); R.ask = cmd('sweep.run').askFirst;
C.run(cmd('sweep.run'), OBJ.get('sweep')); await flush(); R.timers = TIMERS;""", self.sweep_doc(), pre=TIMERS)
        self.assertIn('data-id="sweep"', out["R"]["row"])
        self.assertIn("last sweep failed, exit 1: RESULT: failed, step 3: Gmail unreachable", out["R"]["row"])
        self.assertIn('class="g g-warning"', out["R"]["row"].split('data-id="sweep"')[1])
        self.assertEqual((out["R"]["when"], out["R"]["ask"]), (True, True))
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/hoa/sweep", {}]])
        self.assertIn("Sweep started 08:00 UTC", out["toasts"][-1][0])
        # A started run reads HOA again until it ends, so the row follows the run.
        self.assertEqual(out["R"]["timers"], [15000])

    def test_run_sweep_is_off_while_a_run_holds_the_lock_and_a_409_is_its_toast(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/hoa' ? [200, DOC] : [409, { error: 'A sweep is already running (started x); one runs at a time.' }];
R.when = cmd('sweep.run').when(OBJ.get('sweep'));""", self.sweep_doc(running=True), pre=TIMERS)
        self.assertEqual(out["R"]["when"], "a sweep is running now")


class TheAcknowledgeRoute(BrowserSession):
    def test_the_route_writes_a_listed_event_and_answers_409_naming_one_it_does_not_list(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hoa = checkout(Path(tmp.name) / "hoa")
        self.repo(str(hoa))
        status, _, body = self.post("/api/hoa/alarm", {"key": "hoa-alarm/2026-09-05 06:00:00 Tank High Alarm", "acked": True})
        self.assertEqual(status, 409)
        self.assertTrue(body["reload"])
        self.assertIn("holds no alarm event 2026-09-05 06:00:00 Tank High Alarm", body["error"])
        status, _, body = self.post("/api/hoa/alarm", {"key": TANK_LOW, "acked": True})
        self.assertEqual((status, body["key"], body["hidden"]), (200, TANK_LOW, True))
        status, _, body = self.request("/api/hoa", headers={"Cookie": self.cookie})
        self.assertEqual([r["key"] for r in json.loads(body)["mission"]["alarms"] if r["acked"]], [TANK_LOW])


def source_row(source, label, latest, age, allowed, state, ends=None, refresh=None, error=None):
    """One row as hoa's tools/source-ages.py --json prints it."""
    return {"source": source, "label": label, "latest": latest, "age_days": age, "allowed_days": allowed, "state": state,
            "ends": ends, "refresh": refresh, "todo": f"refresh {source}", "error": error}


#: NOW is 2026-09-06: the Mission export is current, Vantaca stale; CORE and TMT have no refresh command.
SOURCES = [source_row("mission", "Mission export", "2026-09-05", 1, 14, "ok", refresh="sh tools/mission/mission.sh export"),
           source_row("core", "CORE bills", "2026-08-01", 36, 40, "ok"),
           source_row("vantaca", "Vantaca invoices", "2026-08-20", 17, 14, "stale", ends="2026-10-31",
                      refresh="python3 tools/vantaca-pull.py"),
           source_row("tmt", "TMT readings", "2026-07-01", 67, 85, "ok", ends="2026-11-30")]


class TheSourceAges(Fixture):
    """sd:4965: the Overview's age lamps, from the checkout's tools/source-ages.py --json, run bounded."""

    run_page = TheScript.run_page

    def script(self, rows=SOURCES, *, code=0, stderr="", body=None):
        """A stand-in tools/source-ages.py: prints `rows` as the real one does, says `stderr`, exits `code`."""
        tools = self.hoa / "tools"
        tools.mkdir(exist_ok=True)
        text = body or (f"import json, sys\nprint(json.dumps({{'repo': '.', 'today': '2026-09-06', 'sources': {rows!r}}}))\n"
                        f"sys.stderr.write({stderr!r})\nsys.exit({code})\n")
        (tools / "source-ages.py").write_text(text, encoding="utf-8")

    def test_the_lamps_are_every_row_of_the_script_in_its_order(self):
        self.script()
        got = self.doc()["sources"]
        self.assertEqual(got["error"], "")
        self.assertEqual(got["command"], "python3 tools/source-ages.py --json")
        self.assertEqual([(r["source"], r["state"], r["age_days"]) for r in got["rows"]],
                         [("mission", "ok", 1), ("core", "ok", 36), ("vantaca", "stale", 17), ("tmt", "ok", 67)])
        self.assertEqual(set(got["rows"][1]), set(hoa_screen.SOURCE_KEYS))

    def test_a_source_that_did_not_read_is_unknown_and_the_others_still_answer(self):
        rows = [source_row("mission", "Mission export", None, None, 14, "unknown", error="cannot read analog-daily.csv"), SOURCES[2]]
        self.script(rows, code=1)
        got = self.doc()["sources"]
        self.assertEqual(got["error"], "")
        self.assertEqual([(r["source"], r["state"], r["error"]) for r in got["rows"]],
                         [("mission", "unknown", "cannot read analog-daily.csv"), ("vantaca", "stale", None)])
        self.assertEqual([r["job"]["name"] for r in got["rows"]], ["hoa-mission-export", "hoa-vantaca-pull"])

    def test_a_checkout_without_the_script_names_it(self):
        self.assertEqual(self.doc()["sources"]["error"], "tools/source-ages.py is not in the checkout")

    def test_any_other_exit_is_the_parts_error_with_the_first_stderr_line(self):
        self.script(code=3, stderr="no export folder\nmore\n")
        self.assertEqual(self.doc()["sources"], {"error": "tools/source-ages.py exited 3: no export folder", "rows": [],
                                                 "command": "python3 tools/source-ages.py --json", "repo": str(self.hoa)})

    def test_output_that_is_not_a_source_list_is_the_parts_error(self):
        self.script(body="print('not json')\n")
        self.assertIn("tools/source-ages.py printed no source list", self.doc()["sources"]["error"])
        self.script(body="print('{\"sources\": [1]}')\n")
        self.assertIn("a source is not an object", self.doc()["sources"]["error"])

    def test_a_script_past_its_ceiling_is_stopped_and_named(self):
        self.script(body="import time\ntime.sleep(5)\n")
        with mock.patch.object(hoa_screen, "SOURCE_SECONDS", 0.5):
            self.assertEqual(self.doc()["sources"]["error"], "tools/source-ages.py ran past its 0.5 seconds")

    def test_no_checkout_reads_no_script(self):
        self.assertEqual(self.bare("/work/other")["sources"]["error"], "no checkout: no repo named hoa is registered")

    def test_the_page_draws_a_lamp_per_source_and_hides_a_retired_one(self):
        self.script()
        out = self.run_page("R.src = ELS['src-body'].html; document.dispatchEvent(new CustomEvent('shell:open', { detail: 'source:vantaca' })); R.det = ELS.details.html;")
        src = out["R"]["src"]
        self.assertEqual(re.findall(r'<tr data-id="source:(\w+)"', src), ["mission", "core", "vantaca", "tmt"])
        self.assertRegex(src, re.compile(r'data-id="source:mission".*?g g-ok" aria-label="ok">●.*?newest 2026-09-05 · 1 d old', re.S))
        self.assertRegex(src, re.compile(r'data-id="source:vantaca".*?g g-caution" aria-label="caution">▲.*?newest 2026-08-20 · 17 d old', re.S))
        self.assertIn("refresh vantaca", out["R"]["det"])
        retired = [SOURCES[0], dict(SOURCES[2], state="retired", refresh=None)]  # what the script prints after Oct 30
        self.script(retired)
        self.assertEqual(re.findall(r'<tr data-id="source:(\w+)"', self.run_page("R.src = ELS['src-body'].html;")["R"]["src"]), ["mission"])

    def test_vantaca_hides_after_its_end_date_whatever_the_script_says(self):
        self.script([SOURCES[0], dict(SOURCES[2], ends="2026-09-05")])
        out = self.run_page("R.src = ELS['src-body'].html; R.obj = !!OBJ.get('source:vantaca');")
        self.assertEqual(re.findall(r'<tr data-id="source:(\w+)"', out["R"]["src"]), ["mission"])
        self.assertFalse(out["R"]["obj"])
        # On its end date it still shows: the day is compared as YYYY-MM-DD in the browser's zone.
        self.script([SOURCES[0], dict(SOURCES[2], ends="2026-09-06")])
        out = self.run_page("R.src = ELS['src-body'].html;")
        self.assertEqual(re.findall(r'<tr data-id="source:(\w+)"', out["R"]["src"]), ["mission", "vantaca"])

    def test_retry_starts_the_sources_manual_job_through_the_jobs_route(self):
        self.script()
        self.jobs.jobs.update({"hoa-mission-export": ("failed", 3), "hoa-vantaca-pull": ("idle", 0)})
        doc = self.doc()
        revision = doc["sources"]["rows"][0]["job"]["revision"]
        out = self.run_page("""ANSWER = (path, body) => path === '/api/hoa' ? [200, DOC]
  : [200, { request: { status: 'accepted', message: 'launchctl accepted the request' }, job: {} }];
const c = cmd('jobs.retry'), m = OBJ.get('source:mission'), v = OBJ.get('source:vantaca');
R.types = ['mission', 'core', 'vantaca', 'tmt'].map(k => OBJ.get(`source:${k}`).type);
R.cli = [c.cli(m), c.cli(v)]; R.sends = c.sends(m); R.primary = [c.primary(m), c.primary(v)]; R.does = c.does(v);
R.when = c.when(m); R.log = cmd('jobs.log').cli(m);
C.run(c, m); await flush(); R.timers = TIMERS;""", doc,
                            pre="var TIMERS = []; setTimeout = (f, ms) => { TIMERS.push(ms); return TIMERS.length; };")
        # CORE and TMT have no refresh job: they are source rows, and Retry is not theirs.
        self.assertEqual(out["R"]["types"], ["job", "source", "job", "source"])
        self.assertEqual(out["R"]["cli"], ["sd jobs retry hoa-mission-export", "sd jobs retry hoa-vantaca-pull"])
        self.assertEqual(out["R"]["sends"], "launchctl kickstart fixture/hoa-mission-export")
        self.assertEqual(out["R"]["primary"], [True, False])
        self.assertIn("Vantaca", out["R"]["does"])
        self.assertIs(out["R"]["when"], True)
        self.assertEqual(out["R"]["log"], "local-cron-jobs/cron-jobs.sh logs hoa-mission-export")
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/jobs/hoa-mission-export/retry", {"revision": revision}]])
        self.assertIn("Mission export refresh started · hoa-mission-export", out["toasts"][0][0])
        # The landed start reads HOA again, and the lamps with it,
        self.assertEqual(out["gets"], ["/api/hoa", "/api/hoa"])
        # and once more after SRC_POLL, in case launchd had not marked the job running yet.
        self.assertEqual(out["R"]["timers"], [15000])

    def test_a_refused_start_is_named(self):
        self.script()
        self.jobs.jobs["hoa-mission-export"] = ("failed", 3)
        out = self.run_page("""ANSWER = (path, body) => path === '/api/hoa' ? [200, DOC]
  : [200, { request: { status: 'failed', message: 'job changed before dispatch; no action was sent' }, job: {} }];
C.run(cmd('jobs.retry'), OBJ.get('source:mission')); await flush();""",
                            pre="var TIMERS = []; setTimeout = (f, ms) => { TIMERS.push(ms); return TIMERS.length; };")
        self.assertIn("hoa-mission-export not started: job changed before dispatch; no action was sent", out["toasts"][0][0])

    def test_an_idle_job_may_start_and_a_running_one_is_off_with_the_inventorys_reason(self):
        # An idle job may run now (sd:4982): a manual refresh job is idle until its button starts it.
        self.script()
        self.jobs.jobs.update({"hoa-mission-export": ("idle", 0), "hoa-vantaca-pull": ("running", None)})
        out = self.run_page("""const c = cmd('jobs.retry');
R.when = ['mission', 'vantaca'].map(k => c.when(OBJ.get(`source:${k}`)));""",
                            pre="var TIMERS = []; setTimeout = (f, ms) => { TIMERS.push(ms); return TIMERS.length; };")
        self.assertEqual(out["R"]["when"], [True, "hoa-vantaca-pull is running now"])

    def test_a_job_that_is_not_installed_turns_retry_off_and_says_how_to_install_it(self):
        self.script()
        doc = self.doc()
        self.assertEqual(doc["sources"]["rows"][0]["job"], {
            "name": "hoa-mission-export",
            "error": "hoa-mission-export is not an installed job (job is not supported: installed configuration is "
                     "unavailable or invalid); local-cron-jobs/cron-jobs.sh install hoa-mission-export installs it"})
        self.assertNotIn("job", doc["sources"]["rows"][1])
        out = self.run_page("R.when = cmd('jobs.retry').when(OBJ.get('source:mission')); R.src = ELS['src-body'].html;", doc)
        self.assertEqual(out["R"]["when"], doc["sources"]["rows"][0]["job"]["error"])
        self.assertRegex(out["R"]["src"], re.compile(r'data-id="source:mission".*?class="detail hide-sm">refresh job not installed<', re.S))

    def test_each_row_shows_its_jobs_last_run_and_a_failure_names_its_last_line(self):
        self.script()
        self.jobs.jobs.update({"hoa-mission-export": ("failed", 3), "hoa-vantaca-pull": ("running", None)})
        self.jobs.log("hoa-mission-export", "[hoa-mission-export] 2026-09-06T08:00:00-0600 starting (cwd: /x)\n"
                      "mission export: Chrome is not running; open it and sign in to 123scada.com\n\n"
                      "[hoa-mission-export] 2026-09-06T08:00:01-0600 FAILED rc=3\n")
        doc = self.doc()
        self.assertEqual(doc["sources"]["rows"][0]["job"]["said"],
                         "mission export: Chrome is not running; open it and sign in to 123scada.com")
        out = self.run_page("R.src = ELS['src-body'].html; R.timers = TIMERS;", doc,
                            pre="var TIMERS = []; setTimeout = (f, ms) => { TIMERS.push(ms); return TIMERS.length; };")
        src = out["R"]["src"]
        self.assertRegex(src, re.compile(r'data-id="source:mission".*?>last refresh failed, exit 3: mission export: Chrome is not running', re.S))
        self.assertRegex(src, re.compile(r'data-id="source:vantaca".*?>refresh running<', re.S))
        # A running job reads HOA again until it ends, so the lamps follow the run.
        self.assertEqual(out["R"]["timers"], [15000])

    def test_a_script_that_did_not_answer_is_unknown_with_its_reason(self):
        out = self.run_page("R.src = ELS['src-body'].html;")
        self.assertIn("<b>▨ Not read.</b> tools/source-ages.py is not in the checkout", out["R"]["src"])


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "hoa", "HOA", "/hoa", ("/api/hoa", "/api/hoa/alarm", "/api/hoa/sweep",
                                              "/api/hoa/sweep/status", "/api/hoa/briefs",
                                              "/api/hoa/reports")


if __name__ == "__main__":
    unittest.main()
