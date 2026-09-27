"""The collector's dual write: one JSONL line, and one `meter` row per
provider per window in the sd database, on the same reading.

This drives `local-agent-meter/agent-meter.py` from the library's suite
rather than from a suite of its own, for the reason `test_controls.py` walks
`local-sd-runner` from here: the CI workflow's unwired-suite guard fails
every leg on a folder that grows `tests/test_*.py` without a `run_suite`
line naming it, and that line is the workflow's to add. Both folders are in
the same checkout wherever this suite runs, and a missing collector fails
rather than skipping. When `local-agent-meter` gets its own `run_suite`
line, this file moves next to the script.

The collector is driven as the job drives it, a subprocess with `--out` and
`--db` under a directory the test owns. `codexbar` is a script on a PATH the
test builds, printing what the real one prints for `claude` and `codex`;
`rtk` is a script there too, one that fails, because `find_bin` falls back to
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

#: One `codexbar usage --format json --provider X` answer each, in the shape
#: the live ledger holds: a list of one entry, `usage.primary` and
#: `usage.secondary` each carrying `windowMinutes` and `usedPercent`.
READINGS = {
    "claude": [{"provider": "claude", "source": "claude", "usage": {
        "primary": {"usedPercent": 61, "windowMinutes": 300,
                    "resetsAt": "2026-09-16T21:10:00Z"},
        "secondary": {"usedPercent": 37, "windowMinutes": 10080,
                      "resetsAt": "2026-09-22T18:00:00Z"},
        "tertiary": None}}],
    "codex": [{"provider": "codex", "source": "oauth", "usage": {
        "primary": {"usedPercent": 12.5, "windowMinutes": 300},
        "secondary": {"usedPercent": 23, "windowMinutes": 10080}}}],
}

CODEXBAR = """\
#!/bin/sh
# A codexbar that answers `usage --format json --provider <name>` from a file.
while [ $# -gt 0 ]; do
  case "$1" in --provider) shift; provider="$1" ;; esac
  shift
done
[ -f "$CODEXBAR_FIXTURES/$provider.json" ] || { echo "no fixture for $provider" >&2; exit 1; }
cat "$CODEXBAR_FIXTURES/$provider.json"
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
        for name, reading in READINGS.items():
            (self.fixtures / f"{name}.json").write_text(json.dumps(reading), encoding="utf-8")
        self.executable(self.bin / "codexbar", CODEXBAR)
        self.executable(self.bin / "rtk", RTK)
        self.out = root / "ledger" / "meter.jsonl"
        self.out.parent.mkdir()
        # A store as `sd-db.sh init` makes one, with the registry beside it as
        # the library reads it (`sd_db.registry.beside`). Seeding is the
        # library's, on the first sample through the writable connection.
        self.state = root / "state"
        self.state.mkdir()
        self.db = self.state / "sd.db"
        (self.state / "providers.yaml").write_text(REGISTRY, encoding="utf-8")
        initialise(self.db)

    @staticmethod
    def executable(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def run_meter(self, *args, pythonpath=None):
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(self.bin), env.get("PATH", "/usr/bin:/bin")])
        env["CODEXBAR_FIXTURES"] = str(self.fixtures)
        if pythonpath is not None:
            env["PYTHONPATH"] = pythonpath
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--out", str(self.out), "--timeout", "10", *args],
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

    def test_a_reading_is_four_meter_rows_on_their_bills_and_one_jsonl_line(self):
        done = self.run_meter("--db", str(self.db))
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 4})
        self.assertNotIn("sd_db", record["errors"])
        self.assertIn("rtk", record["errors"])
        stamp = record["ts"].replace("Z", "+00:00")
        self.assertEqual(
            self.meter_rows(),
            [("claude", "anthropic", 300, 61.0, stamp, "meter") + (None,) * 6,
             ("claude", "anthropic", 10080, 37.0, stamp, "meter") + (None,) * 6,
             ("codex", "openai", 300, 12.5, stamp, "meter") + (None,) * 6,
             ("codex", "openai", 10080, 23.0, stamp, "meter") + (None,) * 6],
        )
        self.assertIn("sd_db 4 rows", done.stdout)

    def test_a_missing_database_is_an_error_entry_and_the_jsonl_line_still_lands(self):
        gone = self.state / "elsewhere" / "sd.db"
        done = self.run_meter("--db", str(gone))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 0})
        self.assertIn("FileNotFoundError", record["errors"]["sd_db"])
        self.assertIn(str(gone), record["errors"]["sd_db"])
        self.assertEqual(record["codexbar"]["claude"]["usage"]["primary"]["usedPercent"], 61)
        self.assertEqual(self.meter_rows(), [])
        self.assertIn("sd_db ERR", done.stdout)

    def test_an_interpreter_without_sd_db_is_an_error_entry_and_the_jsonl_line_still_lands(self):
        """`/opt/homebrew/bin/python3` cannot import `sd_db`; the job runs
        the pack's interpreter, and any other python3 must still collect."""
        shadow = Path(self.tmp.name) / "shadow"
        shadow.mkdir()
        (shadow / "sd_db.py").write_text(
            "raise ImportError('no sd_db under this interpreter')\n", encoding="utf-8")
        done = self.run_meter("--db", str(self.db), pythonpath=str(shadow))
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 0})
        self.assertEqual(record["errors"]["sd_db"],
                         "ImportError: no sd_db under this interpreter")
        self.assertEqual(self.meter_rows(), [])

    def test_no_db_writes_the_jsonl_line_and_no_row(self):
        done = self.run_meter("--no-db")
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 0, "skipped": "--no-db"})
        self.assertNotIn("sd_db", record["errors"])
        self.assertEqual(self.meter_rows(), [])

    def test_one_refused_window_costs_that_window_and_the_other_three_land(self):
        """A percentage codexbar reports outside 0..100 is refused by the
        library; the refusal is one `errors` entry and the run goes on."""
        reading = json.loads((self.fixtures / "codex.json").read_text())
        reading[0]["usage"]["primary"]["usedPercent"] = 140
        (self.fixtures / "codex.json").write_text(json.dumps(reading), encoding="utf-8")
        done = self.run_meter("--db", str(self.db))
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 3})
        self.assertIn("140", record["errors"]["sd_db.codex.primary"])
        self.assertNotIn("sd_db", record["errors"])
        self.assertEqual([row[:3] for row in self.meter_rows()],
                         [("claude", "anthropic", 300), ("claude", "anthropic", 10080),
                          ("codex", "openai", 10080)])
        # The cron log says so too, not only the JSONL line.
        self.assertIn("sd_db 3 rows, 1 refused", done.stdout)

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
        (self.fixtures / "codex.json").unlink()
        done = self.run_meter("--db", str(self.db))
        self.assertEqual(done.returncode, 0, done.stderr)
        (record,) = self.lines()
        self.assertEqual(record["sd_db"], {"rows": 2})
        self.assertIn("codexbar.codex", record["errors"])
        self.assertEqual([row[0] for row in self.meter_rows()], ["claude", "claude"])


if __name__ == "__main__":
    unittest.main()
