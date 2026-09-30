"""The v2 Home page (sd:2117).

What this slice promises: `/home` answers under the shared policy and loads
its script before `shell.js`; `/api/home` lists the tiles the config folder's
`home-tiles.conf` names (the entity ids describe a house, so the public
checkout carries only `home-tiles.conf.example`), names every line it could
not read, and says no Home Assistant state is read. The page shows every tile
as unknown with that reason, registers the design's Home commands with the
design's ids, labels, keys and risks, and keeps each one off with the reason:
it sends nothing.

`home.js` runs under JavaScriptCore (osascript) against the stand-in page and
shell `test_v2_tasks` uses. The browser half -- the look at 375 px, the wall
display, focus -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import home_screen, server, v2

from support import NOW, ScreenCase
from test_v2_today import OSASCRIPT, Refused
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
HOME_JS = (V2 / "static" / "home.js").read_text(encoding="utf-8")
KIOSK_JS = (V2 / "static" / "home-kiosk.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
EXAMPLE = Path(home_screen.__file__).resolve().parents[1] / "home-tiles.conf.example"
#: A token value no page output may ever hold.
TOKEN = "tok-7f3a9c-never-shown"

#: The design's Home commands (design source products/system/designs/v2/home.html): id, object type, label, key, risk.
COMMANDS = [
    ["alarm.arm_home", "alarm", "Arm home", "h", "confirm"],
    ["alarm.arm_away", "alarm", "Arm away", "w", "confirm"],
    ["alarm.disarm", "alarm", "Disarm", "d", "confirm"],
    ["lock.lock", "lock", "Lock", "l", "undo"],
    ["lock.unlock", "lock", "Unlock", "u", "confirm"],
    ["toggle.on", "toggle", "Turn on", "o", "undo"],
    ["toggle.off", "toggle", "Turn off", "f", "undo"],
    ["sensor.read", "sensor", "Read state", "s", "safe"],
]


class TheDocument(ScreenCase):
    """`home_screen.document` reads the tile list and reads no state."""

    def setUp(self):
        super().setUp()
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def conf(self, text):
        path = self.dir / "home-tiles.conf"
        path.write_text(text, encoding="utf-8")
        return path

    def test_no_config_is_named_and_lists_nothing(self):
        doc = home_screen.document(now=NOW, config=self.dir / "home-tiles.conf")
        self.assertEqual(doc["config"], {"state": "missing", "source": "<config>/project-dashboard/home-tiles.conf", "problems": []})
        self.assertEqual((doc["headline"], doc["groups"], doc["read"]), (None, [], NOW))
        self.assertEqual(doc["reader"], {"available": False, "reason": home_screen.READER_REASON})

    def test_the_example_reads_as_a_headline_and_groups_in_file_order(self):
        doc = home_screen.document(now=NOW, config=EXAMPLE)
        self.assertEqual((doc["config"]["state"], doc["config"]["problems"]), ("read", []))
        self.assertEqual(doc["headline"], {"id": "binary_sensor.problems_active", "name": "Problems active", "domain": "binary_sensor", "type": None})
        self.assertEqual([g["name"] for g in doc["groups"]], ["Security", "Water & safety", "Network & backup", "Toggles"])
        self.assertEqual([t["id"] for t in doc["groups"][0]["tiles"]], ["alarm_control_panel.house", "lock.front_door"])
        # The optional fifth field types a tile the domain would not: a virtual switch is a toggle.
        self.assertEqual([(t["id"], t["type"]) for t in doc["groups"][3]["tiles"]],
                         [("input_boolean.guest_mode", None), ("binary_sensor.virtual_porch_light", "toggle")])
        self.assertFalse(doc["reader"]["available"])

    def test_a_line_it_cannot_read_is_named_and_the_rest_still_count(self):
        doc = home_screen.document(now=NOW, config=self.conf("\n".join([
            "# a comment",
            "tile|Security|lock.front_door|Front door",
            "tile|Security|Not An Id|Bad",
            "tile|Security|lock.back_door",
            "bogus|lock.side_door|Side",
            "tile|Security|lock.front_door|Again",
            "headline|binary_sensor.problems_active|Problems",
            "headline|binary_sensor.other|Second",
            "tile||lock.gate|Gate",
            "tile|Security|lock.shed|Shed|door",
            "tile|Security|switch.fan|Fan|toggle",
        ])))
        self.assertEqual(doc["config"]["state"], "partial")
        self.assertEqual([p.split(":", 1)[0] for p in doc["config"]["problems"]],
                         ["line 3", "line 4", "line 5", "line 6", "line 8", "line 9", "line 10"])
        self.assertIn("type 'door' is not one of alarm, lock, toggle, sensor", doc["config"]["problems"][6])
        self.assertIn("is listed twice", doc["config"]["problems"][3])
        self.assertIn("a second headline", doc["config"]["problems"][4])
        self.assertEqual(doc["groups"], [{"name": "Security", "tiles": [
            {"id": "lock.front_door", "name": "Front door", "domain": "lock", "type": None},
            {"id": "switch.fan", "name": "Fan", "domain": "switch", "type": "toggle"}]}])
        self.assertEqual(doc["headline"]["id"], "binary_sensor.problems_active")


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/home")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Home · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"]+)"', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "home-kiosk.js", "icons.js", "sections.js", "home.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page(self):
        self.assertEqual(v2.SECTIONS.get("Home"), "/home")
        self.assertNotIn("Home", v2.CLASSIC)

    def test_the_data_route_needs_a_session_takes_no_query_and_reads_the_config(self):
        self.assertEqual(self.request("/api/home")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/home?kiosk=1", headers=cookie)[0], 400)
        with mock.patch.object(home_screen, "CONFIG", EXAMPLE):
            status, _, body = self.request("/api/home", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["headline"]["id"], "binary_sensor.problems_active")
        self.assertFalse(doc["reader"]["available"])

    def test_the_document_never_carries_the_token(self):
        cookie = {"Cookie": self.cookie}
        with mock.patch.object(home_screen, "CONFIG", EXAMPLE), mock.patch.dict("os.environ", {"HA_TOKEN": TOKEN, "HA_URL": "https://ha.example.test"}):
            status, _, body = self.request("/api/home", headers=cookie)
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN, body)
        self.assertNotIn("ha.example.test", body)


# The stand-in additions home.js and home-kiosk.js need beyond test_v2_tasks: a root element with attributes, a clock, and the shell's row, url and
# closePane.
ROOT = r"""
var ROOT_ATTRS = {};
document.documentElement = { setAttribute(k, v) { ROOT_ATTRS[k] = v; }, removeAttribute(k) { delete ROOT_ATTRS[k]; },
  hasAttribute(k) { return k in ROOT_ATTRS; } };
function setInterval() { return 0; }
"""
SHELL_MORE = r"""
window.shell.row = () => null; window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
"""


class TheScript(ScreenCase):
    """home.js against the document `home_screen` builds from the example config."""

    def setUp(self):
        super().setUp()
        self.doc = home_screen.document(now=NOW, config=EXAMPLE)

    def run_page(self, body, answer=None, search=""):
        answer = answer or f"() => [200, {json.dumps(self.doc)}]"
        script = (STAND_IN + ROOT + f"location.search = {json.dumps(search)};\n" + KIOSK_JS + "\n" + MARKUP_JS + "\nconst mk = window.markup.html;\n"
                  + SHELL + SHELL_MORE + f"\nANSWER = {answer};\n" + HOME_JS
                  + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.root = ROOT_ATTRS; OUT.attention = window.PAGE_ATTENTION;"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]);")
        self.assertEqual(out["R"]["reg"], COMMANDS)

    def test_every_write_is_off_with_the_reader_reason_and_sends_nothing(self):
        out = self.run_page("""const target = c => C.get(c.on === 'alarm' ? 'alarm_control_panel.house' : c.on === 'lock' ? 'lock.front_door'
  : c.on === 'toggle' ? 'input_boolean.guest_mode' : 'binary_sensor.water_leak');
R.when = REG.map(c => (c.when || (() => true))(target(c)));
REG.forEach(c => shellRun(c, target(c))); await flush();""")
        writes = [c for c in COMMANDS if c[0] != "sensor.read"]
        self.assertEqual(out["R"]["when"], [home_screen.READER_REASON] * len(writes) + [True])
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["gets"], ["/api/home"])
        self.assertEqual(out["confirms"], [])
        self.assertEqual([t[0] for t in out["toasts"]], [f"off: {home_screen.READER_REASON}"] * len(writes)
                         + ["Copy the line; it reads HA_TOKEN and HA_URL from your shell"])

    def test_read_state_is_a_copy_only_line_that_names_the_token_and_never_holds_one(self):
        # A document that carries a token by mistake: nothing the page shows or copies may hold its value.
        doc = json.loads(json.dumps(self.doc))
        doc["reader"]["token"] = TOKEN
        doc["groups"][2]["tiles"][0]["token"] = TOKEN
        out = self.run_page("""const read = REG.find(c => c.id === 'sensor.read');
R.read = [read.executes, read.cli(C.get('binary_sensor.water_leak'))];
R.lines = []; OBJ.forEach(o => REG.filter(c => c.on === o.type).forEach(c => R.lines.push(c.cli(o))));
open('binary_sensor.wan_degraded'); R.det = ELS.details.html; R.groups = ELS.groups.html;""", answer=f"() => [200, {json.dumps(doc)}]")
        self.assertEqual(out["R"]["read"], [False, 'curl -H "Authorization: Bearer $HA_TOKEN" "$HA_URL/api/states/binary_sensor.water_leak"'])
        self.assertTrue(out["R"]["lines"])
        for line in out["R"]["lines"]:
            self.assertIn("$HA_TOKEN", line)
            self.assertIn("$HA_URL", line)
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_the_tiles_are_unknown_with_the_reason_and_the_page_claims_nothing(self):
        out = self.run_page("R.groups = ELS.groups.html; R.head = ELS.headline.html; R.sub = ELS.subhead.html;")
        groups = out["R"]["groups"]
        for name in ("House alarm", "Front door", "Water leak", "Guest mode", "Water &amp; safety"):
            self.assertIn(name, groups)
        self.assertEqual(len(re.findall(r'data-state="unknown"', groups)), 8)
        self.assertNotRegex(groups, r'data-state="(ok|caution|warning)"')
        self.assertIn("no HA reader", groups)
        self.assertIn("Problems active", out["R"]["head"])
        self.assertIn("8 tiles", out["R"]["sub"])
        self.assertEqual(out["states"][0]["kind"], "loading")
        last = out["states"][-1] or {}
        self.assertEqual(last.get("kind"), "partial", "the page did not say no state was read")
        self.assertIn(home_screen.READER_REASON, last["text"])
        self.assertEqual(out["attention"], {"state": "unknown", "n": 0, "what": "HA not read"})

    def test_a_missing_config_and_a_failed_read_each_say_so(self):
        missing = home_screen.document(now=NOW, config=Path(self.enterContext(tempfile.TemporaryDirectory())) / "home-tiles.conf")
        out = self.run_page("R.groups = ELS.groups.html;", answer=f"() => [200, {json.dumps(missing)}]")
        last = out["states"][-1]
        self.assertEqual((last["kind"], last["title"], last["source"]), ("empty", "No tile list", "<config>/project-dashboard/home-tiles.conf"))
        self.assertIn("project-dashboard/home-tiles.conf.example", last["text"])
        self.assertEqual(out["R"]["groups"], "", "a missing tile list drew a grid")
        out = self.run_page("", answer="() => [500, { error: 'boom' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])

    def test_no_tile_shows_ok_even_when_a_reader_answers(self):
        # Until a reader ports the design's state grammar, no tile may claim a state it was not given.
        doc = json.loads(json.dumps(self.doc))
        doc["reader"] = {"available": True, "reason": ""}
        out = self.run_page("R.groups = ELS.groups.html; R.head = ELS.headline.html;", answer=f"() => [200, {json.dumps(doc)}]")
        self.assertNotRegex(out["R"]["groups"] + out["R"]["head"], r'data-state="(ok|caution|warning)"|●')
        self.assertEqual(len(re.findall(r'data-state="unknown"', out["R"]["groups"])), 8)

    def test_a_configured_type_gives_the_tile_its_commands(self):
        out = self.run_page("R.t = [C.get('binary_sensor.virtual_porch_light').type, C.get('binary_sensor.water_leak').type, C.get('input_boolean.guest_mode').type];")
        self.assertEqual(out["R"]["t"], ["toggle", "sensor", "toggle"])

    def test_details_name_the_entity_and_why_it_has_no_state(self):
        out = self.run_page("open('lock.front_door'); R.det = ELS.details.html; open('no.such'); R.after = ELS.details.html;")
        det = out["R"]["det"]
        self.assertIn("<code>lock.front_door</code>", det)
        self.assertIn("Security", det)
        self.assertIn(f"Not read: {home_screen.READER_REASON}.", det)
        self.assertEqual(out["R"]["after"], det)

    def test_the_wall_display_flag_comes_from_the_address(self):
        self.assertIn("data-kiosk", self.run_page("", search="?kiosk=1")["root"])
        self.assertNotIn("data-kiosk", self.run_page("")["root"])

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        self.assertNotIn("innerHTML", HOME_JS)
        self.assertNotIn("setAttribute('style'", HOME_JS)
        self.assertNotRegex(HOME_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", HOME_JS)
        # A page-level j/k handler is drift (the shell owns them through PAGE_LIST).
        self.assertNotRegex(HOME_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", HOME_JS)


if __name__ == "__main__":
    unittest.main()
