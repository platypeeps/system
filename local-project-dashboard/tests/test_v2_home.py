"""The v2 Home page (sd:2117), with live Home Assistant state (sd:3427).

What this slice promises: `/home` answers under the shared policy and loads
its script before `shell.js`; `/api/home` lists the tiles the config folder's
`home-tiles.conf` names (the entity ids describe a house, so the public
checkout carries only `home-tiles.conf.example`), names every line it could
not read, and gives each tile its live state and last change from one
`GET /api/states` (sd:3427), with the URL and token local-ha-mcp uses. A
failed read (not configured, down, refused, too slow, redirected) or an entity
Home Assistant does not list is a named error on the tile, and the page still
draws. The page registers the design's Home commands with the design's ids,
labels, keys and risks, and keeps each one off: it sends nothing.

The reader tests talk to a stub server on 127.0.0.1 with synthetic entity ids
and a synthetic token; every other test runs with no Home Assistant settings
(`setUpModule`), so no test reaches a real one.

`home.js` runs under JavaScriptCore (osascript) against the stand-in page and
shell `test_v2_tasks` uses. The browser half -- the look at 375 px, the wall
display, focus -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import html
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from sd_dashboard import home_screen, server, v2

from support import NOW, ScreenCase
from test_v2_today import OSASCRIPT, Refused
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers
from test_v2_read import READ_SHELL

V2 = Path(v2.__file__).resolve().parent
HOME_JS = (V2 / "static" / "home.js").read_text(encoding="utf-8")
KIOSK_JS = (V2 / "static" / "home-kiosk.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
EXAMPLE = Path(home_screen.__file__).resolve().parents[1] / "home-tiles.conf.example"
#: A token value no page output may ever hold.
TOKEN = "tok-7f3a9c-never-shown"

#: A synthetic Home Assistant token, sent only to the stub.
HA_TOKEN = "ha-tok-5e1d-synthetic"


def setUpModule():
    """No test here reads this machine's Home Assistant settings: an empty config folder and no HA_* variables."""
    global _CONFIG, _ENV
    _CONFIG = tempfile.TemporaryDirectory()
    _ENV = mock.patch.dict("os.environ", {"SYSTEM_TOOLS_CONFIG": _CONFIG.name})
    _ENV.start()
    for name in ("HA_TOKEN", "HA_MCP_URL"):
        os.environ.pop(name, None)


def tearDownModule():
    _ENV.stop()
    _CONFIG.cleanup()


class Stub:
    """A Home Assistant stand-in on 127.0.0.1: records each request and answers as `answer(path)` says."""

    def __init__(self, answer):
        calls = self.calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append((self.path, self.headers.get("Authorization")))
                status, body, headers = answer(self.path)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    if "Content-Length" not in headers:
                        self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def entity(entity_id, state, changed="2026-09-06T11:58:00+00:00", **attributes):
    return {"entity_id": entity_id, "state": state, "last_changed": changed, "last_updated": changed,
            "attributes": {"friendly_name": entity_id, **attributes}}


BACKUP = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")

#: Synthetic states for the example's entities; binary_sensor.smoke_sensors is left out on purpose.
STATES = [
    entity("binary_sensor.problems_active", "off"),
    entity("alarm_control_panel.house", "armed_home", "2026-09-06T10:25:00+00:00"),
    entity("lock.front_door", "locked", "2026-09-06T11:40:00+00:00"),
    entity("binary_sensor.water_leak", "on", "2026-09-06T11:59:00+00:00", device_class="moisture"),
    entity("binary_sensor.wan_degraded", "unavailable"),
    # The page judges a backup's age by the browser's clock, so this one is an hour before the run, not before NOW.
    entity("sensor.backup_last_successful_automatic_backup", BACKUP, BACKUP, device_class="timestamp"),
    entity("input_boolean.guest_mode", "off"),
    entity("binary_sensor.virtual_porch_light", "on", entity_picture="/api/camera_proxy/x?token=pic-secret"),
    entity("sensor.unlisted", "12"),
]


#: The design's Home commands (design source products/system/designs/pages/home.html): id, object type, label, key, risk.
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
    """`home_screen.document` reads the tile list; with no Home Assistant settings it names that and calls nothing."""

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
        self.assertEqual(doc["reader"], {"available": False, "reason": home_screen.NO_TILES})

    def test_the_example_reads_as_a_headline_and_groups_in_file_order(self):
        doc = home_screen.document(now=NOW, config=EXAMPLE)
        self.assertEqual((doc["config"]["state"], doc["config"]["problems"]), ("read", []))
        self.assertEqual(doc["headline"], {"id": "binary_sensor.problems_active", "name": "Problems active", "domain": "binary_sensor", "type": None,
                                           "state": None, "error": "HA not configured"})
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
        unread = {"state": None, "error": "HA not configured"}
        self.assertEqual(doc["groups"], [{"name": "Security", "tiles": [
            {"id": "lock.front_door", "name": "Front door", "domain": "lock", "type": None, **unread},
            {"id": "switch.fan", "name": "Fan", "domain": "switch", "type": "toggle", **unread}]}])
        self.assertEqual(doc["headline"]["id"], "binary_sensor.problems_active")


class TheLiveRead(unittest.TestCase):
    """sd:3427: one GET /api/states, with local-ha-mcp's URL and token; every failure is a named error and the tiles still list."""

    def setUp(self):
        self.config = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def stub(self, answer=None):
        stub = Stub(answer or (lambda path: (200, STATES, {})))
        self.addCleanup(stub.close)
        return stub

    def env(self, url, token=HA_TOKEN, **more):
        return {"SYSTEM_TOOLS_CONFIG": str(self.config), "HA_TOKEN": token, "HA_MCP_URL": f"{url}/mcp_server/sse", **more}

    def doc(self, environ, timeout=2.0):
        return home_screen.document(now=NOW, config=EXAMPLE, environ=environ, timeout=timeout)

    def tile(self, doc, entity_id):
        tiles = [doc["headline"]] + [t for g in doc["groups"] for t in g["tiles"]]
        return next(t for t in tiles if t["id"] == entity_id)

    def test_each_tile_carries_its_state_and_last_change_from_one_read(self):
        stub = self.stub()
        doc = self.doc(self.env(stub.url))
        self.assertEqual(stub.calls, [("/api/states", f"Bearer {HA_TOKEN}")])
        self.assertEqual(doc["reader"]["available"], True)
        self.assertEqual(self.tile(doc, "lock.front_door")["state"],
                         {"value": "locked", "changed": "2026-09-06T11:40:00+00:00", "device_class": None, "unit": None})
        self.assertEqual(self.tile(doc, "binary_sensor.water_leak")["state"]["device_class"], "moisture")
        self.assertIsNone(self.tile(doc, "lock.front_door")["error"])
        self.assertEqual(self.tile(doc, "binary_sensor.problems_active")["state"]["value"], "off")
        # An entity Home Assistant does not list is named on its tile; the others still read.
        self.assertEqual((self.tile(doc, "binary_sensor.smoke_sensors")["state"], self.tile(doc, "binary_sensor.smoke_sensors")["error"]),
                         (None, home_screen.NOT_LISTED))
        text = json.dumps(doc)
        for secret in (HA_TOKEN, "pic-secret", "127.0.0.1", "sensor.unlisted"):
            self.assertNotIn(secret, text)

    def test_the_settings_are_local_ha_mcps_and_the_environment_wins(self):
        stub = self.stub()
        (self.config / "ha-mcp").mkdir()
        (self.config / "ha-mcp" / ".env").write_text(f"HA_TOKEN=from-file\nHA_MCP_URL={stub.url}/mcp_server/sse\n", encoding="utf-8")
        self.doc({"SYSTEM_TOOLS_CONFIG": str(self.config)})
        self.doc({"SYSTEM_TOOLS_CONFIG": str(self.config), "HA_TOKEN": "from-env"})
        self.assertEqual(stub.calls, [("/api/states", "Bearer from-file"), ("/api/states", "Bearer from-env")])

    def test_unset_settings_are_named_and_nothing_is_called(self):
        (self.config / "ha-mcp").mkdir()
        (self.config / "ha-mcp" / ".env").write_text("HA_TOKEN=change-me\nHA_MCP_URL=http://change-me:8123/mcp_server/sse\n", encoding="utf-8")
        doc = self.doc({"SYSTEM_TOOLS_CONFIG": str(self.config)})
        self.assertFalse(doc["reader"]["available"])
        self.assertIn("HA_TOKEN", doc["reader"]["reason"])
        self.assertIn("HA_MCP_URL", doc["reader"]["reason"])
        self.assertIn("<config>/ha-mcp/.env", doc["reader"]["reason"])
        self.assertEqual(self.tile(doc, "lock.front_door")["error"], "HA not configured")
        self.assertEqual(len(doc["groups"]), 4, "a reader failure dropped the tiles")

    def test_a_refused_token_is_named_on_every_tile(self):
        stub = self.stub(lambda path: (401, {"message": "Unauthorized"}, {}))
        doc = self.doc(self.env(stub.url))
        self.assertFalse(doc["reader"]["available"])
        self.assertIn("HTTP 401", doc["reader"]["reason"])
        self.assertEqual({t["error"] for g in doc["groups"] for t in g["tiles"]}, {"HA refused the token"})
        self.assertTrue(all(t["state"] is None for g in doc["groups"] for t in g["tiles"]))

    def test_home_assistant_down_is_named(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        doc = self.doc(self.env(f"http://127.0.0.1:{port}"))
        self.assertFalse(doc["reader"]["available"])
        self.assertEqual(self.tile(doc, "lock.front_door")["error"], "HA did not answer")
        self.assertNotIn(str(port), json.dumps(doc))

    def test_a_slow_answer_is_cut_at_the_timeout(self):
        stub = self.stub(lambda path: (time.sleep(2), (200, STATES, {}))[1])
        started = time.monotonic()
        doc = self.doc(self.env(stub.url), timeout=0.3)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(doc["reader"]["available"])
        self.assertIn("0.3 s", doc["reader"]["reason"])
        self.assertEqual(self.tile(doc, "lock.front_door")["error"], "HA timed out")

    def test_a_redirect_is_not_followed_so_the_token_goes_nowhere_else(self):
        stub = self.stub(lambda path: (302, b"", {"Location": "/elsewhere"}) if path == "/api/states" else (200, STATES, {}))
        doc = self.doc(self.env(stub.url))
        self.assertEqual([path for path, _ in stub.calls], ["/api/states"])
        self.assertFalse(doc["reader"]["available"])
        self.assertIn("HTTP 302", doc["reader"]["reason"])

    def test_an_answer_that_is_not_a_state_list_is_named(self):
        for body in (b"<html>", {"entity_id": "lock.front_door"}, [{"entity_id": 3}]):
            with self.subTest(body=body):
                stub = self.stub(lambda path, body=body: (200, body, {}))
                doc = self.doc(self.env(stub.url))
                self.assertFalse(doc["reader"]["available"])
                self.assertEqual(self.tile(doc, "lock.front_door")["error"], "HA answer not read")

    def test_a_cut_answer_or_an_unusable_url_is_named(self):
        stub = self.stub(lambda path: (200, b'[{"entity_id": "lock.front_door"', {"Content-Length": "4096"}))
        doc = self.doc(self.env(stub.url))
        self.assertFalse(doc["reader"]["available"])
        self.assertEqual(self.tile(doc, "lock.front_door")["error"], "HA answer not read")
        doc = self.doc(self.env("http://127.0.0.1:notaport"))
        self.assertEqual((doc["reader"]["available"], self.tile(doc, "lock.front_door")["error"]), (False, "HA not configured"))

    def test_no_tile_list_makes_no_call(self):
        stub = self.stub()
        doc = home_screen.document(now=NOW, config=self.config / "home-tiles.conf", environ=self.env(stub.url))
        self.assertEqual((stub.calls, doc["groups"]), ([], []))

    def test_writes_stay_off_with_their_own_reason_when_the_read_works(self):
        stub = self.stub()
        doc = self.doc(self.env(stub.url))
        self.assertEqual(doc["writes"], {"available": False, "reason": home_screen.WRITES_REASON})


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/home")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Home · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "home-kiosk.js", "icons.js", "sections.js", "read.js", "home.js", "shell.js"])
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
var INTERVALS = [];
function setInterval(f, ms) { INTERVALS.push([f, ms]); return 0; }
"""
SHELL_MORE = r"""
window.shell.row = () => new URLSearchParams(location.search).get('row'); window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
OUT.panes = []; window.shell.openPane = t => OUT.panes.push(t);
"""
# The reader as the page gets it, with each spec the page hands it recorded.
READ_SPY = r"""
{ const real = window.shell.read; OUT.reads = []; window.shell.read = spec => { OUT.reads.push([spec.source, spec.what]); return real(spec); }; }
"""


class TheScript(ScreenCase):
    """home.js against the document `home_screen` builds from the example config."""

    def setUp(self):
        super().setUp()
        self.doc = home_screen.document(now=NOW, config=EXAMPLE)

    def run_page(self, body, answer=None, search=""):
        answer = answer or f"() => [200, {json.dumps(self.doc)}]"
        script = (STAND_IN + ROOT + f"location.search = {json.dumps(search)};\n" + KIOSK_JS + "\n" + MARKUP_JS + "\nconst mk = window.markup.html;\n"
                  + SHELL + SHELL_MORE + READ_SHELL + READ_SPY + f"\nANSWER = {answer};\n" + HOME_JS
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

    def live(self, answer=None):
        """The document `home_screen` builds against a stub Home Assistant."""
        stub = Stub(answer or (lambda path: (200, STATES, {})))
        self.addCleanup(stub.close)
        return home_screen.document(now=NOW, config=EXAMPLE, environ={
            "SYSTEM_TOOLS_CONFIG": os.environ["SYSTEM_TOOLS_CONFIG"], "HA_TOKEN": HA_TOKEN, "HA_MCP_URL": f"{stub.url}/mcp_server/sse"})

    def test_every_write_is_off_with_the_write_reason_and_sends_nothing(self):
        out = self.run_page("""const target = c => C.get(c.on === 'alarm' ? 'alarm_control_panel.house' : c.on === 'lock' ? 'lock.front_door'
  : c.on === 'toggle' ? 'input_boolean.guest_mode' : 'binary_sensor.water_leak');
R.when = REG.map(c => (c.when || (() => true))(target(c)));
REG.forEach(c => shellRun(c, target(c))); await flush();""", answer=f"() => [200, {json.dumps(self.live())}]")
        writes = [c for c in COMMANDS if c[0] != "sensor.read"]
        self.assertEqual(out["R"]["when"], [home_screen.WRITES_REASON] * len(writes) + [True])
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["gets"], ["/api/home"])
        self.assertEqual(out["confirms"], [])
        self.assertEqual([t[0] for t in out["toasts"]], [f"off: {home_screen.WRITES_REASON}"] * len(writes)
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

    def test_unconfigured_tiles_are_unknown_with_the_reason_and_the_page_claims_nothing(self):
        out = self.run_page("R.groups = ELS.groups.html; R.head = ELS.headline.html; R.sub = ELS.subhead.html;")
        groups = out["R"]["groups"]
        for name in ("House alarm", "Front door", "Water leak", "Guest mode", "Water &amp; safety"):
            self.assertIn(name, groups)
        self.assertEqual(len(re.findall(r'data-state="unknown"', groups)), 8)
        self.assertNotRegex(groups, r'data-state="(ok|caution|warning)"')
        self.assertEqual(groups.count("HA not configured"), 8 * 2, "each tile names the error in its words and its label")
        self.assertIn("Problems active", out["R"]["head"])
        self.assertIn("8 tiles", out["R"]["sub"])
        self.assertIn("no HA state read", out["R"]["sub"])
        self.assertEqual(out["states"][0]["kind"], "loading")
        last = out["states"][-1] or {}
        self.assertEqual(last.get("kind"), "partial", "the page did not say no state was read")
        self.assertIn(self.doc["reader"]["reason"], last["text"])
        self.assertEqual(out["attention"], {"state": "unknown", "n": 0, "what": "HA not read"})

    def test_live_tiles_show_their_state_lamp_and_age(self):
        out = self.run_page("R.groups = ELS.groups.html; R.head = ELS.headline.html; R.sub = ELS.subhead.html;",
                            answer=f"() => [200, {json.dumps(self.live())}]")
        tiles = dict(re.findall(r'<div class="tile" data-id="([^"]+)"(.*?)</div></li>', out["R"]["groups"], re.S))
        lamp = {i: re.search(r'data-state="(\w+)"', t).group(1) for i, t in tiles.items()}
        self.assertEqual(lamp, {
            "alarm_control_panel.house": "ok", "lock.front_door": "ok", "binary_sensor.water_leak": "warning",
            "binary_sensor.smoke_sensors": "unknown", "binary_sensor.wan_degraded": "unknown",
            "sensor.backup_last_successful_automatic_backup": "ok", "input_boolean.guest_mode": "ok",
            "binary_sensor.virtual_porch_light": "ok"})
        for entity_id, words in (("alarm_control_panel.house", "armed home"), ("lock.front_door", "locked"),
                                 ("binary_sensor.water_leak", "detected"), ("binary_sensor.wan_degraded", "unavailable · no reading"),
                                 ("binary_sensor.smoke_sensors", home_screen.NOT_LISTED), ("input_boolean.guest_mode", "off")):
            self.assertIn(f"</span>{words}</span>", tiles[entity_id], entity_id)
        # The age: each read tile carries its last change for the shell's relative-time renderer; an unread one carries none.
        self.assertIn('<time class="rel" datetime="2026-09-06T11:40:00+00:00"', tiles["lock.front_door"])
        self.assertNotIn('<time class="rel" datetime="2', tiles["binary_sensor.smoke_sensors"])
        self.assertIn('<span>nothing reported</span></span><span class="st"><span aria-hidden="true">●</span> none</span>', out["R"]["head"])
        self.assertIn("· HA read", out["R"]["sub"])
        last = out["states"][-1] or {}
        self.assertEqual(last.get("kind"), "partial", "the page did not name the tile Home Assistant does not list")
        self.assertIn("binary_sensor.smoke_sensors", last["text"])
        self.assertIn(home_screen.NOT_LISTED, last["text"])

    def test_a_failed_home_assistant_read_names_its_error_on_every_tile(self):
        doc = self.live(lambda path: (401, {"message": "Unauthorized"}, {}))
        out = self.run_page("R.groups = ELS.groups.html; open('lock.front_door'); R.det = ELS.details.html;",
                            answer=f"() => [200, {json.dumps(doc)}]")
        self.assertEqual(len(re.findall(r'data-state="unknown"', out["R"]["groups"])), 8)
        self.assertIn("HA refused the token", out["R"]["groups"])
        last = out["states"][-1] or {}
        self.assertEqual(last.get("kind"), "partial")
        self.assertIn("HTTP 401", last["text"])
        self.assertIn(f"Not read: {html.escape(doc['reader']['reason'], quote=False)}.", out["R"]["det"])

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

    def test_no_tile_shows_ok_without_a_state_even_when_the_reader_answers(self):
        # A tile claims only a state it was given.
        doc = json.loads(json.dumps(self.doc))
        doc["reader"] = {"available": True, "reason": ""}
        for tile in [doc["headline"]] + [t for g in doc["groups"] for t in g["tiles"]]:
            tile.update(state=None, error=None)
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
        self.assertIn(f"Not read: {html.escape(self.doc['reader']['reason'], quote=False)}.", det)
        self.assertEqual(out["R"]["after"], det)

    def test_details_show_the_live_state_and_its_last_change(self):
        out = self.run_page("open('lock.front_door'); R.det = ELS.details.html; open('binary_sensor.smoke_sensors'); R.missing = ELS.details.html;",
                            answer=f"() => [200, {json.dumps(self.live())}]")
        self.assertIn("locked", out["R"]["det"])
        self.assertIn('<time class="rel" datetime="2026-09-06T11:40:00+00:00"', out["R"]["det"])
        self.assertNotIn("Not read", out["R"]["det"])
        self.assertIn(f"Not read: {home_screen.NOT_LISTED}.", out["R"]["missing"])

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



class TheReader(TheScript):
    """sd:2486: Home reads /api/home through shell.read (read.js, sd:2418): who reads, the failed read and ?row=. sd:3522:
    it reads again on Read again and each minute while visible, and gives the shell's staleness band its reading time."""

    def test_the_page_reads_through_the_shared_reader(self):
        out = self.run_page("")
        self.assertEqual(out["reads"], [["/api/home", "the tiles"]])
        self.assertEqual(out["gets"], ["/api/home"])
        self.assertNotIn("async function getJSON", HOME_JS)

    def test_a_failed_read_says_so_and_shows_no_tile(self):
        out = self.run_page("R.groups = ELS.groups.html; R.det = ELS.details.html; R.sub = ELS.subhead.html;", answer="() => [500, { error: 'boom' }]")
        self.assertNotIn("· HA read", out["R"]["sub"] or "", "a failed read claimed Home Assistant was read")
        self.assertEqual(out["states"][-1], {"kind": "error", "text": "The tiles were not read: boom. Reload retries it.", "source": "/api/home"})
        self.assertEqual(out["R"]["groups"], "")
        self.assertIn("Select a tile", out["R"]["det"])

    def test_the_tiles_are_read_again_each_minute_and_on_read_again(self):
        # REGRESSION sd:3522: Home read /api/home once, so a wall display kept its first state until a reload.
        out = self.run_page("""R.observed = [document.body.dataset.observed, document.body.dataset.interval];
(document.getElementById('refresh').listeners.click || []).forEach(f => f()); await flush();
const every = INTERVALS.filter(i => i[1] === 60 * 1000).map(i => i[0]);
every.forEach(f => f()); await flush();
document.hidden = true; every.forEach(f => f()); await flush();""")
        self.assertEqual(out["R"]["observed"], [self.doc["read"], "1"])
        self.assertEqual(out["gets"], ["/api/home"] * 3, "the first read, Read again and one minute, and none while hidden")

    def test_the_minute_read_shows_no_loading_state(self):
        # REGRESSION sd:3522: each minute read drew the loading state above the tiles, so the wall display jumped by its height.
        out = self.run_page("""R.first = OUT.states.length;
INTERVALS.filter(i => i[1] === 60 * 1000).forEach(i => i[0]()); await flush();""")
        self.assertEqual(out["gets"], ["/api/home"] * 2)
        self.assertEqual([s for s in out["states"][out["R"]["first"]:] if s and s["kind"] == "loading"], [])

    def test_a_failed_read_leaves_no_reading_time_for_the_staleness_band(self):
        out = self.run_page("R.observed = document.body.dataset.observed;", answer="() => [500, { error: 'boom' }]")
        self.assertIsNone(out["R"].get("observed"))

    def test_the_linked_tile_is_selected_and_opened_and_an_unlisted_one_is_not(self):
        """A guard: ?row= selected its tile before the reader; it still does, and only for a listed tile outside the wall display."""
        out = self.run_page("R.det = ELS.details.html;", search="?row=lock.front_door")
        self.assertIn("<code>lock.front_door</code>", out["R"]["det"])
        self.assertEqual(out["panes"], ["tab-details"])
        out = self.run_page("R.det = ELS.details.html;", search="?row=lock.back_door")
        self.assertIn("Select a tile", out["R"]["det"])
        self.assertEqual(out["panes"], [])
        out = self.run_page("R.det = ELS.details.html;", search="?kiosk=1&row=lock.front_door")
        self.assertNotIn("lock.front_door", out["R"]["det"])


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "home", "Home", "/home", ("/api/home",)


if __name__ == "__main__":
    unittest.main()
