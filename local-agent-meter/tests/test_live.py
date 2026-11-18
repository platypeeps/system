"""The live reading: `agent-meter.py usage` (sd:9256).

Each reads a Settings list (a workflow database setting row), reads each
entry, and writes one `heartbeat` state row per entry. A failed read is a
row with status `unknown`, never `ok`; a provider with no usage API reads
`no-source`, never 0. Keys come from the environment by name, and no key
value reaches a row or the output.

The readers are driven in-process with a fake fetch, the HTTP helper and
the `http` kind against a server on 127.0.0.1, and the subcommands as the
job runs them: a subprocess on a store `initialise` made in a directory the
test owns. No test reaches the network.
"""

import datetime
import http.server
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import sd_db
from sd_db import writes
from sd_db.migrate import initialise

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import usage_sources  # noqa: E402
from usage_sources import ReadFailed  # noqa: E402

SCRIPT = HERE / "agent-meter.py"
NOW = datetime.datetime(2026, 11, 17, 20, 0, tzinfo=datetime.timezone.utc)
SECRET = "sk-test-0123456789-secret"
LOCAL = {"NO_PROXY": "*", "no_proxy": "*"}

#: Each keyed reader: its variable, an answer in the live shape (checked
#: 2026-11-17), and what the reading must hold.
ANSWERS = {
    "openrouter": ("OPENROUTER_API_KEY",
                   {"data": {"usage_monthly": 12.5, "limit": 50, "limit_remaining": 37.5}},
                   {"spend": {"amount": 12.5, "unit": "usd", "period": "month"},
                    "balance": {"amount": 37.5, "unit": "usd"}}),
    "moonshot": ("MOONSHOT_API_KEY",
                 {"code": 0, "status": True, "data": {"available_balance": 4.25,
                                                      "voucher_balance": 0, "cash_balance": 4.25}},
                 {"balance": {"amount": 4.25, "unit": "usd"}}),
    "deepinfra": ("DEEPINFRA_API_KEY",
                  {"months": [{"period": "2026.11", "total_cost": 1234, "items": []}]},
                  {"spend": {"amount": 12.34, "unit": "usd", "period": "month"}}),
    "minimax": ("MINIMAX_API_KEY",
                {"base_resp": {"status_code": 0}, "model_remains": [{
                    "model_name": "general", "start_time": 1795000000000,
                    "end_time": 1795018000000, "current_interval_remaining_percent": 80,
                    "weekly_start_time": 1794500000000, "weekly_end_time": 1795104800000,
                    "current_weekly_remaining_percent": 95}]},
                {"windows": [
                    {"name": "general", "used_percent": 20.0, "window_minutes": 300,
                     "resets_at": "2026-11-18T16:06:40Z"},
                    {"name": "general weekly", "used_percent": 5.0, "window_minutes": 10080,
                     "resets_at": "2026-11-19T16:13:20Z"}]}),
    "firecrawl": ("FIRECRAWL_API_KEY",
                  {"success": True, "data": {"remainingCredits": 250, "planCredits": 1000,
                                             "billingPeriodEnd": "2026-12-01T00:00:00Z"}},
                  {"balance": {"amount": 250.0, "unit": "credits"},
                   "windows": [{"name": "credits", "used_percent": 75.0, "window_minutes": None,
                                "resets_at": "2026-12-01T00:00:00Z"}]}),
    "heygen": ("HEYGEN_API_KEY", {"error": None, "data": {"remaining_quota": 600}},
               {"balance": {"amount": 600.0, "unit": "quota"}}),
    "runway": ("RUNWAYML_API_SECRET", {"creditBalance": 900, "tier": {}, "usage": {}},
               {"balance": {"amount": 900.0, "unit": "credits"}}),
    "openai": ("OPENAI_ADMIN_KEY",
               {"data": [{"results": [{"amount": {"value": 1.5, "currency": "usd"}}]},
                         {"results": [{"amount": {"value": 2.25, "currency": "usd"}}]}]},
               {"spend": {"amount": 3.75, "unit": "usd", "period": "month"}}),
}

#: Values no number in an answer may be taken from.
EXTREMES = ("12", True, None, float("nan"), float("inf"), float("-inf"), 10**400, [], {}, [1])


def answering(answer, calls=None):
    def fetch(url, headers=None):
        if calls is not None:
            calls.append((url, headers))
        return json.loads(json.dumps(answer)) if not callable(answer) else answer()
    return fetch


def failing(message):
    def fetch(url, headers=None):
        raise ReadFailed(message)
    return fetch


def replace_numbers(value, extreme):
    """Every number in value replaced by extreme (bools left alone)."""
    if isinstance(value, dict):
        return {k: replace_numbers(v, extreme) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_numbers(v, extreme) for v in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return extreme
    return value


class Readers(unittest.TestCase):
    def test_each_reader_reads_its_answer(self):
        for name, (var, answer, expected) in ANSWERS.items():
            with self.subTest(provider=name):
                calls = []
                got = usage_sources.read_provider(name, {var: SECRET}, answering(answer, calls), NOW)
                self.assertEqual(got["status"], "ok", got)
                self.assertEqual(got["read_at"], "2026-11-17T20:00:00Z")
                for field, value in expected.items():
                    self.assertEqual(got[field], value)
                (url, headers), = calls
                self.assertTrue(url.startswith("https://"))
                self.assertIn(SECRET, " ".join(headers.values()))

    def test_a_missing_key_is_unknown_and_names_the_variable_only(self):
        for name, (var, answer, _) in ANSWERS.items():
            with self.subTest(provider=name):
                calls = []
                got = usage_sources.read_provider(name, {var: ""}, answering(answer, calls), NOW)
                self.assertEqual((got["status"], got["error"]), ("unknown", "%s not set" % var))
                self.assertEqual(calls, [])

    def test_a_failed_fetch_is_unknown_never_ok(self):
        for name, (var, _, _) in ANSWERS.items():
            with self.subTest(provider=name):
                got = usage_sources.read_provider(name, {var: SECRET}, failing("HTTP 401"), NOW)
                self.assertEqual((got["status"], got["error"]), ("unknown", "HTTP 401"))
                self.assertIsNone(got["read_at"])
                self.assertNotIn(SECRET, json.dumps(got))

    def test_an_answer_of_another_shape_is_unknown_never_ok(self):
        shapes = [{}, [], "x", None, 5, {"data": "x"}, {"data": None}, {"months": "x"},
                  {"base_resp": {"status_code": 1}, "model_remains": []}, {"code": 1, "data": {}}]
        for name, (var, _, _) in ANSWERS.items():
            for shape in shapes:
                with self.subTest(provider=name, shape=shape):
                    got = usage_sources.read_provider(name, {var: SECRET}, answering(shape), NOW)
                    self.assertEqual(got["status"], "unknown", got)

    def test_every_extreme_number_is_unknown_not_a_crash_or_a_reading(self):
        for name, (var, answer, _) in ANSWERS.items():
            for extreme in EXTREMES:
                with self.subTest(provider=name, extreme=extreme):
                    bent = replace_numbers(answer, extreme)
                    if name == "moonshot":
                        bent["code"] = 0
                    if name == "minimax":
                        bent["base_resp"]["status_code"] = 0
                    got = usage_sources.read_provider(name, {var: SECRET}, answering(bent), NOW)
                    if extreme == "12":
                        # A numeric string is a number some APIs send (Codex credits).
                        continue
                    self.assertEqual(got["status"], "unknown", got)
                    json.dumps(got, allow_nan=False)

    def test_no_source_providers_read_no_source_not_zero(self):
        for name in usage_sources.NO_SOURCE:
            with self.subTest(provider=name):
                got = usage_sources.read_provider(name, {}, failing("unreached"), NOW)
                self.assertEqual(got["status"], "no-source")
                self.assertEqual((got["windows"], got["balance"], got["spend"]), ([], None, None))

    def test_a_name_with_no_reader_is_unknown(self):
        got = usage_sources.read_provider("nosuch", {}, failing("unreached"), NOW)
        self.assertEqual((got["status"], got["error"]), ("unknown", "no reader for this name"))

    def test_the_starting_list_names_every_reader_once(self):
        names, problems = usage_sources.parse_providers(usage_sources.STARTING)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(names), sorted(["claude", "codex", *usage_sources.READERS,
                                                *usage_sources.NO_SOURCE]))

    def test_the_providers_grammar(self):
        names, problems = usage_sources.parse_providers(
            "# comment\nclaude\n\n codex  # trailing\nclaude\nBad Name\nx/y\n")
        self.assertEqual(names, ["claude", "codex"])
        self.assertEqual(problems, ["line 6: not a provider name", "line 7: not a provider name"])

    def test_claude_reads_every_account_and_the_active_ones_week(self):
        accounts = [{"number": 1, "active": False, "pct": 10.0, "resetsAt": "a"},
                    {"number": 2, "active": True, "pct": 67.0, "resetsAt": "2026-11-20T00:00:00Z"}]
        got = usage_sources.claude_reading(accounts, None, NOW)
        self.assertEqual(got["status"], "ok")
        self.assertEqual(got["accounts"], accounts)
        self.assertEqual(got["windows"], [{"name": "seven_day", "used_percent": 67.0,
                                           "window_minutes": 10080,
                                           "resets_at": "2026-11-20T00:00:00Z"}])
        failed = usage_sources.claude_reading(None, "exit 1", NOW)
        self.assertEqual((failed["status"], failed["error"]), ("unknown", "exit 1"))


class Server:
    """A server on 127.0.0.1 answering each path from `routes`: (code, body)."""

    def __init__(self, routes):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                code, body = outer.routes.get(self.path, (404, b"{}"))
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

            def log_message(self, *args):
                pass

        self.routes = routes
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Fetch(unittest.TestCase):
    def setUp(self):
        os.environ.update(LOCAL)
        self.server = Server({"/ok": (200, {"a": 1}), "/broken": (500, b"oops"),
                              "/garbage": (200, b"<html>")})
        self.addCleanup(self.server.close)

    def test_a_failed_read_is_read_failed_with_no_body(self):
        self.assertEqual(usage_sources.fetch_json(self.server.url + "/ok"), {"a": 1})
        for path, message in (("/broken", "HTTP 500"), ("/garbage", "unparseable answer"),
                              ("/missing", "HTTP 404")):
            with self.subTest(path=path):
                with self.assertRaises(ReadFailed) as caught:
                    usage_sources.fetch_json(self.server.url + path, {"Authorization": SECRET})
                self.assertEqual(str(caught.exception), message)
        with self.assertRaises(ReadFailed) as caught:
            usage_sources.fetch_json("http://127.0.0.1:%d/" % closed_port())
        self.assertNotIn(SECRET, str(caught.exception))


class Codex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.day = self.home / "sessions/2026/11/17"
        self.day.mkdir(parents=True)

    def rollout(self, name, lines, mtime):
        path = self.day / ("rollout-%s.jsonl" % name)
        path.write_text("".join(json.dumps(line) + "\n" if not isinstance(line, str) else line + "\n"
                                for line in lines), encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    @staticmethod
    def event(used=61.0, stamp="2026-11-17T19:00:00.000Z", **limits):
        rate = {"primary": {"used_percent": used, "window_minutes": 10080,
                            "resets_at": 1794992575},
                "secondary": None,
                "credits": {"has_credits": True, "unlimited": False, "balance": "62500.0"}}
        rate.update(limits)
        return {"timestamp": stamp, "type": "event_msg",
                "payload": {"type": "token_count", "info": {"cwd": "/x"}, "rate_limits": rate}}

    def test_the_newest_rollout_with_limits_is_the_reading(self):
        self.rollout("old", [self.event(used=10.0, stamp="2026-11-16T00:00:00Z")], 1000)
        self.rollout("new", [self.event(used=40.0, stamp="2026-11-17T18:00:00Z"),
                             self.event(used=61.0), "{not json"], 2000)
        got = usage_sources.codex_reading(self.home)
        self.assertEqual(got["status"], "ok")
        self.assertEqual(got["read_at"], "2026-11-17T19:00:00.000Z")
        self.assertEqual(got["windows"], [{"name": "primary", "used_percent": 61.0,
                                           "window_minutes": 10080,
                                           "resets_at": "2026-11-18T09:02:55Z"}])
        self.assertEqual(got["balance"], {"amount": 62500.0, "unit": "credits"})

    def test_a_newest_rollout_without_limits_falls_back_to_the_one_before(self):
        self.rollout("old", [self.event(used=10.0, stamp="2026-11-16T00:00:00Z")], 1000)
        self.rollout("new", [{"timestamp": "2026-11-17T19:00:00Z", "type": "session_meta",
                              "payload": {"type": "session_meta"}}], 2000)
        got = usage_sources.codex_reading(self.home)
        self.assertEqual((got["status"], got["read_at"]), ("ok", "2026-11-16T00:00:00Z"))

    def test_no_limits_anywhere_is_unknown(self):
        got = usage_sources.codex_reading(self.home)
        self.assertEqual(got["status"], "unknown")
        self.rollout("bad", [self.event(primary="x"), self.event(used=float("nan"))], 1000)
        got = usage_sources.codex_reading(self.home)
        self.assertEqual(got["status"], "unknown")

    def test_every_extreme_value_is_skipped_not_a_reading(self):
        for field in ("used_percent", "window_minutes", "resets_at"):
            for extreme in EXTREMES + (0, -5, 1e308):
                with self.subTest(field=field, extreme=extreme):
                    if field == "used_percent" and extreme in (0, -5, "12", 1e308):
                        continue  # a number; the meter row refuses one out of range
                    if field == "resets_at" and extreme in (0, -5, "12"):
                        continue
                    event = self.event()
                    event["payload"]["rate_limits"]["primary"][field] = extreme
                    self.rollout("x", [event], 1000)
                    got = usage_sources.codex_reading(self.home)
                    self.assertEqual(got["status"], "unknown", got)
                    json.dumps(got, allow_nan=False)


CSWAP_LIST = {"accounts": [{"number": 1, "email": "one@example.test", "active": True,
                            "usageStatus": "ok", "usage": {"sevenDay": {
                                "pct": 40.0, "resetsAt": "2026-11-20T00:00:00Z",
                                "expectedPct": 50.0}, "scoped": []},
                            "usageFetchedAt": "2026-11-17T19:59:00Z"}]}
CSWAP = "#!/bin/sh\ncat \"$CSWAP_FIXTURE\"\n"


class Live(unittest.TestCase):
    """The subcommands as the job runs them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "sd.db"
        initialise(self.db)
        self.cswap = root / "cswap"
        self.cswap.write_text(CSWAP, encoding="utf-8")
        self.cswap.chmod(self.cswap.stat().st_mode | stat.S_IXUSR)
        (root / "cswap.json").write_text(json.dumps(CSWAP_LIST), encoding="utf-8")
        day = root / "codex/sessions/2026/11/17"
        day.mkdir(parents=True)
        (day / "rollout-x.jsonl").write_text(json.dumps(Codex.event()) + "\n", encoding="utf-8")
        keys = {var for var, _, _ in ANSWERS.values()}
        self.env = {k: v for k, v in os.environ.items() if k not in keys}
        self.env.update(LOCAL, CSWAP_BIN=str(self.cswap), CSWAP_FIXTURE=str(root / "cswap.json"),
                        CODEX_HOME=str(root / "codex"))

    def run_live(self, command, *args, db=None):
        return subprocess.run([sys.executable, "-I", str(SCRIPT), command,
                               "--db", str(db or self.db), *args],
                              capture_output=True, text=True, env=self.env, timeout=120)

    def connection(self):
        connection = sd_db.connect(self.db, write=True)
        self.addCleanup(connection.close)
        return connection

    def rows(self, prefix):
        """{key: body} of the newest heartbeat row per key under prefix."""
        found = {}
        for key, body in self.connection().execute(
                "SELECT key, body FROM state WHERE kind = 'heartbeat' AND key LIKE ? ORDER BY id",
                (prefix + "%",)):
            found[key] = json.loads(body)
        return found

    def setting(self, key, value):
        writes.set_setting(self.connection(), key, value)

    def test_usage_on_a_store_that_never_held_the_list_starts_it_and_reads_each_line(self):
        done = self.run_live("usage")
        self.assertEqual(done.returncode, 0, done.stderr)
        held = writes.read_settings(self.connection(), ["dashboard.usage_providers"])
        self.assertEqual(held, {"dashboard.usage_providers": usage_sources.STARTING})
        rows = self.rows("usage:")
        names, _ = usage_sources.parse_providers(usage_sources.STARTING)
        self.assertEqual(sorted(rows), sorted("usage:" + n for n in names))
        self.assertEqual(rows["usage:claude"]["status"], "ok")
        self.assertEqual(rows["usage:claude"]["windows"][0]["used_percent"], 40.0)
        self.assertEqual(rows["usage:codex"]["status"], "ok")
        self.assertEqual(rows["usage:openrouter"],
                         usage_sources.reading("openrouter", "unknown", "api",
                                               error="OPENROUTER_API_KEY not set"))
        self.assertEqual(rows["usage:gemini"]["status"], "no-source")
        self.assertNotIn("example.test", json.dumps(rows))
        self.assertIn("rows", done.stdout)

    def test_usage_reads_the_held_list_only_and_names_a_refused_line(self):
        self.setting("dashboard.usage_providers", "codex\nnot a name\ngroq\n")
        done = self.run_live("usage")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(self.rows("usage:")), ["usage:codex", "usage:groq"])
        self.assertIn("SD_REPORT_ATTENTION: dashboard.usage_providers line 2: not a provider name",
                      done.stdout)

    def test_a_cleared_list_reads_nothing_and_writes_nothing(self):
        self.setting("dashboard.usage_providers", None)
        done = self.run_live("usage")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("cleared", done.stdout)
        self.assertEqual(self.rows("usage:"), {})
        self.assertEqual(writes.read_settings(self.connection(), ["dashboard.usage_providers"]),
                         {"dashboard.usage_providers": None})

    def test_a_store_that_does_not_open_exits_1(self):
        for command in ("usage",):
            with self.subTest(command=command):
                done = self.run_live(command, db=Path(self.tmp.name) / "missing" / "sd.db")
                self.assertEqual(done.returncode, 1)
                self.assertIn("did not open", done.stderr)
                self.assertNotIn("Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()
