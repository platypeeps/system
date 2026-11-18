"""The collector's dual write: one JSONL line, and one `meter` row per
provider per window in the sd database, on the same reading.

This drives `local-agent-meter/agent-meter.py`. A missing collector fails
rather than skipping.

The collector is driven as the job drives it, a subprocess with `--out` and
`--db` under a directory the test owns. Claude reads from a `cswap` the
test builds, Codex from a session log under a CODEX_HOME the test owns
(sd:9256 dropped codexbar); `rtk` is a script on a PATH the test builds,
one that fails, because `find_bin` falls back to
the Homebrew prefix when PATH has none and the real binary's answer is not a
fixture. What matters about `rtk` here is only that its failure is an
`errors.rtk` entry beside the rows, not instead of them.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sd_db
from sd_db.migrate import initialise

SYSTEM = Path(__file__).resolve().parents[2]
SCRIPT = SYSTEM / "local-agent-meter/agent-meter.py"
#: The command pack checkout whose lib/ holds the sd_db the collector imports
#: (sd:3279), as the script names it.
PACK = Path(os.environ.get("SD_PACK_ROOT")
            or Path.home() / "repos/platypeeps/sd-workflow")

#: The shipped file's two `start` entries on their two bills, which is what
#: the sampler names when it says `--provider claude` and `--provider codex`.
REGISTRY = """\
bills:
  anthropic: { cost: subscription }
  openai:    { cost: subscription }
providers:
  claude: { start: "claude -p",  vendor: anthropic, bill: anthropic, roles: [author, reviewer], reader: claude-json }
  codex:  { start: "codex exec", vendor: openai,    bill: openai,    roles: [author, reviewer], reader: codex-json }
roles:
  author:   [claude, codex]
  reviewer: [codex, claude]
"""

#: One Codex `token_count` event in the shape its rollout files hold:
#: the rate limits beside `info`, which carries what the ledger must not.
#: Its windows reset in 2100, so no run of these tests sees them reset.
CODEX_EVENT = {"timestamp": "2026-11-03T14:00:00.000Z", "type": "event_msg", "payload": {
    "type": "token_count",
    "info": {"cwd": "/Users/someone/example.test", "last_message": "who@example.test"},
    "rate_limits": {
        "limit_id": "codex", "plan_type": "pro",
        "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": 4102444800},
        "secondary": {"used_percent": 23, "window_minutes": 10080, "resets_at": 4103049600},
        "credits": {"has_credits": False, "unlimited": False, "balance": None}}}}

#: A codexbar that leaves a mark: no meter path may call it (sd:9256).
CODEXBAR = """\
#!/bin/sh
touch "$CODEXBAR_MARK"
exit 1
"""

#: One `cswap list --json` answer in the live shape: per account its
#: number, email, organization and the seven-day window with its scoped
#: (per-model) windows. The email, organization name and UUID are what
#: the ledger must never hold.
CSWAP_LIST = {
    "schemaVersion": 1,
    "activeAccountNumber": 2,
    "accounts": [
        {"number": 1, "email": "first@example.test",
         "organizationName": "Example Org One",
         "organizationUuid": "00000000-0000-4000-8000-000000000001",
         "isOrganization": True, "active": False, "usageStatus": "ok",
         "usage": {
             "sevenDay": {"pct": 27.0, "resetsAt": "2026-11-03T18:00:00.222257+00:00",
                          "countdown": "3h 49m", "expectedPct": 97.7,
                          "aheadOfPace": False, "willLastToReset": True},
             "scoped": [{"pct": 5.0, "resetsAt": "2026-11-03T18:00:00+00:00",
                         "expectedPct": 97.7, "name": "Fable"}]},
         "usageFetchedAt": "2026-11-03T14:07:07Z", "usageAgeSeconds": 212.7},
        {"number": 2, "email": "second@example.test",
         "organizationName": "Example Org Two",
         "organizationUuid": "00000000-0000-4000-8000-000000000002",
         "isOrganization": True, "active": True, "usageStatus": "ok",
         "usage": {
             "sevenDay": {"pct": 67.0, "resetsAt": "2100-01-04T03:00:00+00:00",
                          "expectedPct": 92.3},
             "scoped": []},
         "usageFetchedAt": "2026-11-03T14:05:15Z"},
    ],
    "unclaimedCredentials": ["20260807T151943-000000000000-000000"],
}

#: Every value from the fixture that must stay out of the ledger.
PRIVATE = ("example.test", "Example Org", "00000000-0000-4000-8000",
           "20260807T151943")

CSWAP = """\
#!/bin/sh
# A cswap that answers `list --json` from a file.
[ "$1 $2" = "list --json" ] || { echo "unexpected: $*" >&2; exit 2; }
cat "$CSWAP_FIXTURE"
"""

RTK = """\
#!/bin/sh
echo "rtk: stubbed out in the agent-meter suite" >&2
exit 3
"""


class MeterRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.fixtures = root / "fixtures"
        self.fixtures.mkdir()
        self.codex_home = root / "codex"
        self.rollout = self.codex_home / "sessions/2026/11/03/rollout-2026-11-03T14-00-00-x.jsonl"
        self.rollout.parent.mkdir(parents=True)
        self.write_codex(CODEX_EVENT)
        self.executable(self.bin / "codexbar", CODEXBAR)
        self.executable(self.bin / "rtk", RTK)
        self.cswap = root / "cswap-bin" / "cswap"
        self.cswap.parent.mkdir()
        self.executable(self.cswap, CSWAP)
        self.cswap_fixture = self.fixtures / "cswap.json"
        self.cswap_fixture.write_text(json.dumps(CSWAP_LIST), encoding="utf-8")
        self.out = root / "ledger" / "meter.jsonl"
        self.out.parent.mkdir()
        # A store as `sd-db init` makes one, with the registry beside it as
        # the library reads it (`sd_db.registry.beside`). Seeding is the
        # library's, on the first sample through the writable connection.
        self.state = root / "state"
        self.state.mkdir()
        self.db = self.state / "sd.db"
        (self.state / "providers.yaml").write_text(REGISTRY, encoding="utf-8")
        initialise(self.db)

    def write_codex(self, event):
        self.rollout.write_text(json.dumps(event) + "\n" + json.dumps(
            {"timestamp": "2026-11-03T14:00:01.000Z", "type": "response_item",
             "payload": {"type": "message"}}) + "\n", encoding="utf-8")

    @staticmethod
    def executable(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def run_meter(self, *args, pythonpath=None, home=None, pack="", isolated=False):
        """`pack` is SD_PACK_ROOT; "" leaves the caller's, None removes it.
        `isolated` runs `python -I`, which drops PYTHONPATH and the cwd, so
        only the pack's lib/ and the interpreter's own packages can answer."""
        env = dict(os.environ)
        if home is not None:
            env["HOME"] = str(home)
        if pack is None:
            env.pop("SD_PACK_ROOT", None)
        elif pack != "":
            env["SD_PACK_ROOT"] = str(pack)
        env["PATH"] = os.pathsep.join([str(self.bin), env.get("PATH", "/usr/bin:/bin")])
        env["CODEXBAR_MARK"] = str(self.fixtures / "codexbar-called")
        env["CODEX_HOME"] = str(self.codex_home)
        # Never the real cswap under ~/.local/bin: its answer holds emails.
        env["CSWAP_BIN"] = str(self.cswap)
        env["CSWAP_FIXTURE"] = str(self.cswap_fixture)
        if pythonpath is not None:
            env["PYTHONPATH"] = pythonpath
        return subprocess.run(
            [sys.executable, *(["-I"] if isolated else []), str(SCRIPT),
             "--out", str(self.out), "--timeout", "10", *args],
            capture_output=True, text=True, env=env, timeout=60,
        )

    def lines(self):
        text = self.out.read_text(encoding="utf-8") if self.out.exists() else ""
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def meter_rows(self):
        connection = sd_db.connect(self.db, write=False)
        try:
            return [tuple(row) for row in connection.execute(
                "SELECT provider, bill, window_minutes, used_percent, timestamp, source, "
                "usd, tokens_in, tokens_out, assignment, pass, call_id "
                "FROM cost ORDER BY id"
            ).fetchall()]
        finally:
            connection.close()

    def test_a_reading_is_three_meter_rows_on_their_bills_and_one_jsonl_line(self):
        """Claude's row is the active cswap account's seven days; Codex's
        two are its session log's windows."""
        done = self.run_meter("--db", str(self.db))
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 3})
        self.assertNotIn("sd_db", record["errors"])
        self.assertIn("rtk", record["errors"])
        # Each row carries its reading's time, not the run's (sd:9256
        # review): cswap's fetch of the active account, Codex's event.
        fetched, event = "2026-11-03T14:05:15+00:00", "2026-11-03T14:00:00+00:00"
        self.assertEqual(
            self.meter_rows(),
            [("claude", "anthropic", 10080, 67.0, fetched, "meter") + (None,) * 6,
             ("codex", "openai", 300, 12.5, event, "meter") + (None,) * 6,
             ("codex", "openai", 10080, 23.0, event, "meter") + (None,) * 6],
        )
        self.assertIn("sd_db 3 rows", done.stdout)
        self.assertEqual(record["usage"]["codex"]["read_at"], "2026-11-03T14:00:00.000Z")
        self.assertEqual(record["usage"]["codex"]["windows"][0]["resets_at"], "2100-01-01T00:00:00Z")

    def test_a_window_that_reset_after_its_reading_gets_no_row(self):
        # sd:9256 review: an idle Codex's old share was stamped as current.
        event = json.loads(json.dumps(CODEX_EVENT))
        event["payload"]["rate_limits"]["primary"]["resets_at"] = 1793700000
        self.write_codex(event)
        done = self.run_meter("--db", str(self.db))
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 2, "stale": 1})
        self.assertEqual([row[:3] for row in self.meter_rows()],
                         [("claude", "anthropic", 10080), ("codex", "openai", 10080)])

    def test_deep_replies_cost_their_readings_not_the_ledger_line(self):
        # sd:9256 review: json.loads raised RecursionError on a reply
        # nested past its depth (200000 on 3.15), and the 4-hourly run
        # wrote no line at all.
        deep = "[" * 200000 + "]" * 200000
        self.cswap_fixture.write_text(deep, encoding="utf-8")
        self.rollout.write_text('{"type": "token_count", "rate_limits": %s}\n' % deep,
                                encoding="utf-8")
        done = self.run_meter("--db", str(self.db))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        (record,) = self.lines()
        self.assertEqual((record["usage"]["claude"]["status"], record["usage"]["codex"]["status"]),
                         ("unknown", "unknown"))
        self.assertEqual(record["errors"]["cswap"], "unparseable output")
        self.assertEqual(record["sd_db"], {"rows": 0})

    def test_a_cached_claude_week_that_reset_gets_no_row(self):
        # sd:9256 review: cswap's cached reading looked fresh.
        data = json.loads(json.dumps(CSWAP_LIST))
        data["accounts"][1]["usage"]["sevenDay"]["resetsAt"] = "2026-11-04T03:00:00+00:00"
        self.cswap_fixture.write_text(json.dumps(data), encoding="utf-8")
        done = self.run_meter("--db", str(self.db))
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 2, "stale": 1})
        self.assertEqual([row[0] for row in self.meter_rows()], ["codex", "codex"])

    def test_no_meter_path_calls_codexbar(self):
        """sd:9256: done when no meter path calls codexbar."""
        for args in (["--db", str(self.db)], ["--no-db"]):
            with self.subTest(args=args):
                done = self.run_meter(*args)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertFalse((self.fixtures / "codexbar-called").exists())
                self.assertNotIn("codexbar", self.out.read_text(encoding="utf-8"))

    def test_the_scheduled_run_without_db_writes_to_the_store_under_home(self):
        """The job passes no `--db`, so `sd_db.connect(None)` picks
        `$HOME/.local/share/sd/sd.db` and its registry beside it. HOME here
        is a directory the test owns; PYTHONPATH names the library this
        suite imported, since a user site under the real HOME is not read."""
        home = Path(self.tmp.name) / "home"
        store = home / ".local/share/sd"
        store.mkdir(parents=True)
        (store / "providers.yaml").write_text(REGISTRY, encoding="utf-8")
        initialise(store / "sd.db")
        library = str(Path(sd_db.__file__).resolve().parents[1])
        done = self.run_meter(pythonpath=library, home=home)
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertNotIn("sd_db", record["errors"])
        self.assertEqual(record["sd_db"], {"rows": 3})
        self.db = store / "sd.db"
        self.assertEqual([row[:3] for row in self.meter_rows()],
                         [("claude", "anthropic", 10080),
                          ("codex", "openai", 300), ("codex", "openai", 10080)])

    def test_a_missing_database_is_an_error_entry_and_the_jsonl_line_still_lands(self):
        gone = self.state / "elsewhere" / "sd.db"
        done = self.run_meter("--db", str(gone))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 0})
        self.assertIn("FileNotFoundError", record["errors"]["sd_db"])
        self.assertIn(str(gone), record["errors"]["sd_db"])
        self.assertEqual(record["usage"]["codex"]["windows"][0]["used_percent"], 12.5)
        self.assertEqual(self.meter_rows(), [])
        self.assertIn("sd_db ERR", done.stdout)

    def test_an_interpreter_without_sd_db_is_an_error_entry_and_the_jsonl_line_still_lands(self):
        """`/opt/homebrew/bin/python3` cannot import `sd_db`; the job runs
        the pack's interpreter, and any other python3 must still collect."""
        shadow = Path(self.tmp.name) / "shadow"
        shadow.mkdir()
        (shadow / "sd_db.py").write_text(
            "raise ImportError('no sd_db under this interpreter')\n", encoding="utf-8")
        # A pack with no lib/: the shadow on PYTHONPATH is all that answers.
        done = self.run_meter("--db", str(self.db), pythonpath=str(shadow),
                              pack=Path(self.tmp.name) / "no-pack")
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 0})
        self.assertEqual(record["errors"]["sd_db"],
                         "ImportError: no sd_db under this interpreter")
        self.assertEqual(self.meter_rows(), [])

    def stub_pack(self, root):
        """A pack whose lib/sd_db refuses to import, saying which pack it is."""
        lib = Path(root) / "lib/sd_db"
        lib.mkdir(parents=True)
        (lib / "__init__.py").write_text(
            "raise ImportError('the stub pack lib answered')\n", encoding="utf-8")
        return Path(root)

    def test_the_named_packs_lib_is_the_sd_db_the_collector_imports(self):
        """sd:3279: SD_PACK_ROOT's lib/ goes first, ahead of the copy that
        PYTHONPATH (and any site-packages) offers."""
        pack = self.stub_pack(Path(self.tmp.name) / "stub-pack")
        library = str(Path(sd_db.__file__).resolve().parents[1])
        done = self.run_meter("--db", str(self.db), pythonpath=library, pack=pack)
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["errors"]["sd_db"],
                         "ImportError: the stub pack lib answered")
        self.assertEqual(self.meter_rows(), [])

    def test_with_no_sd_pack_root_the_home_clone_is_the_pack(self):
        home = Path(self.tmp.name) / "home"
        self.stub_pack(home / "repos/platypeeps/sd-workflow")
        library = str(Path(sd_db.__file__).resolve().parents[1])
        done = self.run_meter("--db", str(self.db), pythonpath=library,
                              home=home, pack=None)
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["errors"]["sd_db"],
                         "ImportError: the stub pack lib answered")

    def test_an_isolated_interpreter_writes_the_rows_from_the_packs_lib(self):
        """The job's launch: no PYTHONPATH reaches it, so only the pack's
        lib/ can supply the rows' library."""
        pack = Path(self.tmp.name) / "real-pack"
        pack.mkdir()
        (pack / "lib").symlink_to(PACK / "lib")
        done = self.run_meter("--db", str(self.db), pack=pack, isolated=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertNotIn("sd_db", record["errors"])
        self.assertEqual(record["sd_db"], {"rows": 3})
        self.assertEqual(len(self.meter_rows()), 3)

    def test_no_db_writes_the_jsonl_line_and_no_row(self):
        done = self.run_meter("--no-db")
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 0, "skipped": "--no-db"})
        self.assertNotIn("sd_db", record["errors"])
        self.assertEqual(self.meter_rows(), [])

    def test_an_exported_empty_ledger_name_beats_the_config_file(self):
        """sd:3964: st_source_env keeps an exported empty AGENT_METER_FILE, so
        the reading lands in the default ledger, not the one the file names."""
        config = Path(self.tmp.name) / "config"
        (config / "agent-meter").mkdir(parents=True)
        (config / "agent-meter" / ".env").write_text(f"AGENT_METER_FILE={self.out}\n", encoding="utf-8")
        env = dict(os.environ, SYSTEM_TOOLS_CONFIG=str(config), AGENT_METER_FILE="",
                   PATH=os.pathsep.join([str(self.bin), os.environ.get("PATH", "/usr/bin:/bin")]),
                   CODEX_HOME=str(self.codex_home), CSWAP_BIN=str(self.cswap),
                   CSWAP_FIXTURE=str(self.cswap_fixture))
        done = subprocess.run([sys.executable, str(SCRIPT), "--no-db", "--timeout", "10"],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.lines(), [])
        self.assertTrue((config / "agent-meter" / "meter.jsonl").is_file())

    def test_one_refused_window_costs_that_window_and_the_other_two_land(self):
        """A percentage a source reports outside 0..100 is refused by the
        library; the refusal is one `errors` entry and the run goes on."""
        event = json.loads(json.dumps(CODEX_EVENT))
        event["payload"]["rate_limits"]["primary"]["used_percent"] = 140
        self.write_codex(event)
        done = self.run_meter("--db", str(self.db))
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 2})
        self.assertIn("140", record["errors"]["sd_db.codex.primary"])
        self.assertNotIn("sd_db", record["errors"])
        self.assertEqual([row[:3] for row in self.meter_rows()],
                         [("claude", "anthropic", 10080), ("codex", "openai", 10080)])
        # The cron log says so too, not only the JSONL line.
        self.assertIn("sd_db 2 rows, 1 refused", done.stdout)

    def test_a_store_without_its_registry_is_one_top_level_error_and_no_row(self):
        """Only a `MeterRefused` is a per-window entry. A store with no
        `providers.yaml` beside it fails every sample for the same reason,
        which is not the window's: that is one `errors.sd_db`, the
        transaction rolls back, no row lands, and the line still does."""
        (self.state / "providers.yaml").unlink()
        done = self.run_meter("--db", str(self.db))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 0})
        self.assertIn("providers.yaml", record["errors"]["sd_db"])
        self.assertEqual([key for key in record["errors"] if key.startswith("sd_db.")], [])
        self.assertEqual(self.meter_rows(), [])
        self.assertIn("sd_db ERR", done.stdout)

    def test_a_provider_that_did_not_read_gets_no_row(self):
        self.rollout.unlink()
        done = self.run_meter("--db", str(self.db))
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 1})
        self.assertEqual(record["usage"]["codex"]["status"], "unknown")
        self.assertIn("usage.codex", record["errors"])
        self.assertEqual([row[0] for row in self.meter_rows()], ["claude"])

    # sd:7322: each cswap account, keyed by number, on every reading.

    def test_a_reading_records_each_cswap_account_by_number(self):
        done = self.run_meter("--no-db")
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertNotIn("cswap", record["errors"])
        self.assertEqual(record["accounts"], [
            {"number": 1, "active": False, "status": "ok", "pct": 27.0,
             "resetsAt": "2026-11-03T18:00:00.222257+00:00", "expectedPct": 97.7,
             "scoped": {"Fable": 5.0}, "fetchedAt": "2026-11-03T14:07:07Z"},
            {"number": 2, "active": True, "status": "ok", "pct": 67.0,
             "resetsAt": "2100-01-04T03:00:00+00:00", "expectedPct": 92.3,
             "scoped": {}, "fetchedAt": "2026-11-03T14:05:15Z"},
        ])
        self.assertIn("cswap 2 accounts", done.stdout)

    def test_no_email_organization_or_uuid_reaches_the_ledger(self):
        done = self.run_meter("--db", str(self.db))
        self.assertEqual(done.returncode, 0, done.stderr)
        text = self.out.read_text(encoding="utf-8")
        self.assertIn('"accounts"', text)
        for value in PRIVATE:
            self.assertNotIn(value, text)
            self.assertNotIn(value, done.stdout + done.stderr)

    def test_a_failing_cswap_is_an_error_entry_without_its_output(self):
        """A failed list is unknown, not zero accounts; and its output,
        which may be the human list with emails, stays out of the ledger."""
        self.executable(self.cswap, "#!/bin/sh\n"
                        "echo '1 first@example.test Example Org One' >&2\nexit 1\n")
        done = self.run_meter("--no-db")
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertIsNone(record["accounts"])
        self.assertEqual(record["errors"]["cswap"], "exit 1")
        self.assertEqual(record["usage"]["claude"]["status"], "unknown")
        self.assertEqual(record["usage"]["codex"]["status"], "ok")
        self.assertIn("cswap ERR", done.stdout)
        for value in PRIVATE:
            self.assertNotIn(value, self.out.read_text(encoding="utf-8"))

    def test_unparseable_cswap_output_is_an_error_entry_without_the_text(self):
        self.cswap_fixture.write_text("1 first@example.test Example Org One\n",
                                      encoding="utf-8")
        done = self.run_meter("--no-db")
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertIsNone(record["accounts"])
        self.assertEqual(record["errors"]["cswap"], "unparseable output")
        self.assertNotIn("example.test", self.out.read_text(encoding="utf-8"))

    def test_a_missing_cswap_is_an_error_entry_and_exit_0(self):
        self.cswap.unlink()
        done = self.run_meter("--no-db")
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertIsNone(record["accounts"])
        self.assertIn("cswap binary not found", record["errors"]["cswap"])

    def test_an_account_without_a_number_fails_the_read_not_the_run(self):
        broken = json.loads(json.dumps(CSWAP_LIST))
        del broken["accounts"][1]["number"]
        self.cswap_fixture.write_text(json.dumps(broken), encoding="utf-8")
        done = self.run_meter("--no-db")
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertIsNone(record["accounts"])
        self.assertIn("unexpected cswap shape", record["errors"]["cswap"])
        self.assertNotIn("example.test", self.out.read_text(encoding="utf-8"))

    # sd:9256: a Codex event carries the session's text beside its limits.

    def test_only_codex_rate_limit_fields_reach_the_ledger(self):
        done = self.run_meter("--no-db")
        self.assertEqual(done.returncode, 0, done.stderr)
        text = self.out.read_text(encoding="utf-8")
        for value in ("example.test", "/Users/someone", "plan_type"):
            self.assertNotIn(value, text)
        (record,) = self.lines()
        self.assertEqual([w["window_minutes"] for w in record["usage"]["codex"]["windows"]],
                         [300, 10080])

    # sd:7332: rtk's total_saved is read as a number or not at all.

    def run_with_rtk(self, answer):
        reply = self.fixtures / "rtk.json"
        reply.write_text(answer, encoding="utf-8")
        self.executable(self.bin / "rtk", "#!/bin/sh\ncat '%s'\n" % reply)
        return self.run_meter("--no-db")

    def test_a_huge_int_or_a_string_in_total_saved_is_unset_not_a_crash(self):
        for saved in ("1" + "0" * 400, '"lots"', "NaN", "Infinity", "true", "null", "[1]"):
            with self.subTest(total_saved=saved):
                self.out.unlink(missing_ok=True)
                done = self.run_with_rtk(
                    '{"summary": {"total_saved": %s, "avg_savings_pct": 12.5}}' % saved)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn("Traceback", done.stderr)
                (record,) = self.lines()
                self.assertNotIn("rtk", record["errors"])
                self.assertIn("rtk saved ? (12.5%)", done.stdout)

    def test_a_bad_avg_savings_pct_is_unset_too(self):
        for pct in ("1" + "0" * 400, '"most"', "NaN"):
            with self.subTest(avg_savings_pct=pct):
                self.out.unlink(missing_ok=True)
                done = self.run_with_rtk(
                    '{"summary": {"total_saved": 2500000, "avg_savings_pct": %s}}' % pct)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("rtk saved 2.5M (?%)", done.stdout)

    def test_an_rtk_answer_that_is_not_an_object_still_writes_the_line(self):
        for answer in ("[1, 2]", '{"summary": "none"}', "{}"):
            with self.subTest(answer=answer):
                self.out.unlink(missing_ok=True)
                done = self.run_with_rtk(answer)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn("Traceback", done.stderr)
                self.assertEqual(len(self.lines()), 1)

    def test_a_good_total_saved_prints_as_before(self):
        done = self.run_with_rtk('{"summary": {"total_saved": 2500000, "avg_savings_pct": 12.5}}')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("rtk saved 2.5M (12.5%)", done.stdout)

    # sd:7344: the ledger keeps the two rtk fields the brief reads.

    def test_only_the_rtk_fields_the_brief_reads_reach_the_ledger(self):
        done = self.run_with_rtk(json.dumps({
            "project": "/Users/someone/example.test/project",
            "user": "who@example.test",
            "summary": {"total_saved": 2500000, "avg_savings_pct": 12.5,
                        "total_commands": 40, "cwd": "/home/example.test",
                        "email": "who@example.test"},
            "daily": [{"date": "2026-11-03", "project": "/srv/example.test"}]}))
        self.assertEqual(done.returncode, 0, done.stderr)
        text = self.out.read_text(encoding="utf-8")
        self.assertNotIn("example.test", text)
        self.assertNotIn("/Users/", text)
        (record,) = self.lines()
        self.assertEqual(record["rtk"], {"summary": {"total_saved": 2500000,
                                                     "avg_savings_pct": 12.5}})
        self.assertIn("rtk saved 2.5M (12.5%)", done.stdout)

    def test_a_non_number_rtk_field_is_not_copied_into_the_ledger(self):
        done = self.run_with_rtk('{"summary": {"total_saved": "/home/example.test",'
                                 ' "avg_savings_pct": 12.5}}')
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["rtk"], {"summary": {"avg_savings_pct": 12.5}})
        self.assertNotIn("example.test", self.out.read_text(encoding="utf-8"))

    # The `accounts` answer: used at reset per past window, pace now.

    def run_accounts(self, *args):
        env = dict(os.environ, TZ="UTC")
        return subprocess.run(
            [sys.executable, str(SCRIPT), "accounts", "--out", str(self.out), *args],
            capture_output=True, text=True, env=env, timeout=60)

    def write_ledger(self, readings):
        """readings: (ts, [(number, active, pct, resetsAt, expectedPct)])"""
        with self.out.open("w", encoding="utf-8") as f:
            for ts, accounts in readings:
                f.write(json.dumps({"ts": ts, "accounts": None if accounts is None else [
                    {"number": n, "active": act, "status": "ok", "pct": pct,
                     "resetsAt": reset, "expectedPct": exp, "scoped": {}, "fetchedAt": None}
                    for n, act, pct, reset, exp in accounts]}) + "\n")
            f.write("{not json\n")

    def test_accounts_reports_used_at_reset_and_the_current_pace(self):
        past = "2026-10-28T03:00:00.895156+00:00"
        jitter = "2026-10-28T03:00:00.000001+00:00"
        future = "2099-01-01T03:00:00+00:00"
        self.write_ledger([
            ("2026-10-27T16:00:00Z", [(1, True, 70.0, past, 90.0), (2, False, 10.0, jitter, 90.0)]),
            ("2026-10-27T20:00:00Z", [(1, True, 81.0, jitter, 95.0), (2, False, 12.0, past, 95.0)]),
            ("2026-10-28T00:00:00Z", None),
            ("2026-10-28T04:00:00Z", [(1, False, 3.0, future, 1.0), (2, True, 0.0, future, 1.0)]),
            ("2026-10-28T08:00:00Z", [(1, False, 4.0, future, 5.0), (2, True, 9.0, future, 5.0)]),
        ])
        done = self.run_accounts()
        self.assertEqual(done.returncode, 0, done.stderr)
        out = done.stdout
        self.assertIn("lower bound", out)
        self.assertIn("account 1\n", out)
        self.assertIn("account 2 (active)\n", out)
        # The last reading before the reset, read 7 h before it.
        self.assertIn("reset 2026-10-28 03:00 UTC  used >= 81% (last read 7.0 h before)", out)
        self.assertIn("reset 2026-10-28 03:00 UTC  used >= 12% (last read 7.0 h before)", out)
        self.assertIn("now  resets 2099-01-01 03:00 UTC  4% used, 5% expected "
                      "(read 2026-10-28 08:00 UTC)", out)
        self.assertIn("9% used, 5% expected", out)
        # One past window per account, not one per jittered resetsAt.
        self.assertEqual(out.count("reset 2026-10-28"), 2)
        self.assertLess(out.index("account 1"), out.index("account 2"))

    def test_a_reading_after_its_own_reset_says_after_not_a_negative_gap(self):
        self.write_ledger([("2026-10-28T05:00:00Z",
                            [(1, False, 40.0, "2026-10-28T03:00:00Z", 99.0)])])
        done = self.run_accounts()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("used >= 40% (last read 2.0 h after)", done.stdout)
        self.assertNotIn("-2.0", done.stdout)

    # Malformed values, collector and report (review of 5295f10e).

    #: Values no pct, expectedPct or number may hold. NaN and Infinity
    #: are here because `json.loads` accepts them.
    EXTREME = ("27", True, False, float("nan"), float("inf"), float("-inf"),
               [], {}, [27], 10**400, -10**400)

    def test_a_malformed_cswap_value_is_an_error_entry_not_a_reading(self):
        cases = [("accounts", v) for v in (5, "x", {}, None, [5], ["x"])]
        cases += [(field, v) for field in ("number", "pct", "expectedPct", "scoped")
                  for v in self.EXTREME]
        cases += [("number", 1.0), ("number", None), ("number", 0), ("number", -1),
                  ("number", 2**31), ("resetsAt", 5)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                reply = json.loads(json.dumps(CSWAP_LIST))
                if field == "accounts":
                    reply["accounts"] = value
                elif field == "number":
                    reply["accounts"][0]["number"] = value
                elif field == "scoped":
                    reply["accounts"][0]["usage"]["scoped"][0]["pct"] = value
                else:
                    reply["accounts"][0]["usage"]["sevenDay"][field] = value
                self.cswap_fixture.write_text(json.dumps(reply), encoding="utf-8")
                self.out.unlink(missing_ok=True)
                done = self.run_meter("--no-db")
                self.assertEqual(done.returncode, 0, done.stderr)
                (record,) = self.lines()
                self.assertIsNone(record["accounts"])
                self.assertIn("unexpected cswap shape", record["errors"]["cswap"])
                self.assertNotIn("example.test", self.out.read_text(encoding="utf-8"))

    def test_one_malformed_ledger_entry_cannot_break_the_report(self):
        good = {"number": 2, "active": True, "status": "ok", "pct": 9.0,
                "resetsAt": "2099-01-01T03:00:00+00:00", "expectedPct": 5.0,
                "scoped": {}, "fetchedAt": None}
        bad_records = [5, "x", None, [], {"ts": 5, "accounts": [good | {"number": 1}]},
                       {"ts": "2026-10-28T08:00:00Z", "accounts": 5},
                       {"ts": "2026-10-28T08:00:00Z", "accounts": "xy"},
                       {"ts": "2026-10-28T08:00:00Z", "accounts": [5, "x", None, []]}]
        for field in ("number", "pct", "expectedPct", "resetsAt", "fetchedAt"):
            for value in self.EXTREME + ("9999-12-31T23:59:59+00:00",
                                         "0001-01-01T00:00:00+00:00", 1e308):
                bad_records.append({"ts": "2026-10-28T08:00:00Z",
                                    "accounts": [good | {"number": 1, field: value}]})
        for bad in bad_records:
            with self.subTest(bad=bad):
                with self.out.open("w", encoding="utf-8") as f:
                    f.write(json.dumps(bad) + "\n")
                    f.write(json.dumps({"ts": "2026-10-28T08:00:00Z",
                                        "accounts": [good]}) + "\n")
                done = self.run_accounts()
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn("Traceback", done.stderr)
                self.assertIn("account 2 (active)", done.stdout)
                self.assertIn("9% used, 5% expected", done.stdout)
                self.assertNotIn("nan", done.stdout)
                self.assertNotIn("inf%", done.stdout)

    def test_a_ledger_line_that_is_not_utf8_cannot_break_the_report(self):
        # sd:9256 round 6: the ledger was read as strict UTF-8.
        good = {"number": 2, "active": True, "status": "ok", "pct": 9.0,
                "resetsAt": "2099-01-01T03:00:00+00:00", "expectedPct": 5.0,
                "scoped": {}, "fetchedAt": None}
        with self.out.open("wb") as f:
            f.write(b'{"ts": "\xff"}\n')
            f.write(json.dumps({"ts": "2026-10-28T08:00:00Z", "accounts": [good]}).encode()
                    + b"\n")
        done = self.run_accounts()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("account 2 (active)", done.stdout)

    def test_accounts_on_a_ledger_without_account_readings_says_so(self):
        self.write_ledger([("2026-10-27T16:00:00Z", None)])
        done = self.run_accounts()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no account readings", done.stdout)

    def test_accounts_on_a_missing_ledger_exits_1(self):
        done = self.run_accounts()
        self.assertEqual(done.returncode, 1)
        self.assertIn(str(self.out), done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_help_names_the_subcommands(self):
        done = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("accounts", done.stdout)
        self.assertIn("used at reset", done.stdout)
        self.assertIn("usage providers", done.stdout)
        self.assertIn("status services", done.stdout)


if __name__ == "__main__":
    unittest.main()
