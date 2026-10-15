"""The digest reports, per Jev stage, how often Jev differed from the fallback (sd:3583).

The numbers come from the judgment ledger through the pack's
`sd-db judgments compare`, a read: the digest asks Jev nothing. The ledger
here is a fresh database under a temporary HOME, written with the pack's own
`sd_db.judgment.record`, so the read is the real one against the pinned pack
(`SD_PACK_ROOT`, which the gate sets). A `jev` on `PATH` records any call, and
the Jev pins keep a stray one away from the operator's ledger, corpus and
collector anyway.
"""

import datetime
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "weekly-digest.sh"
PACK = pathlib.Path(os.environ.get("SD_PACK_ROOT")
                    or pathlib.Path.home() / "repos/platypeeps/sd-ai-command-pack")
HEADING = "jev against the fallback, last 7 days (per stage):"
UNKNOWN = "jev against the fallback: unknown (the judgment ledger read failed)"


def _stub(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class DivergenceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="weekly-digest-jev."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        _stub(self.bin / "brew", "exit 0\n")
        _stub(self.bin / "mas", "exit 0\n")
        _stub(self.bin / "hostname", 'echo "fixturehost"\n')
        self.asked = self.tmp / "jev-calls"
        _stub(self.bin / "jev", f'printf "%s\\n" "$*" >> "{self.asked}"\nexit 3\n')
        msetup = _stub(self.bin / "machine-setup.sh", 'echo "drift : 0"\n')
        logs = self.tmp / "logs"
        logs.mkdir()
        (logs / "failures.log").write_text("")
        python = os.path.dirname(sys.executable)
        self.env = {
            "HOME": str(self.home), "PATH": f"{self.bin}:{python}:/usr/bin:/bin",
            "WEEKLY_DIGEST_CRON_LOGS": str(logs), "WEEKLY_DIGEST_MSETUP": str(msetup),
            "SD_PACK_ROOT": str(PACK),
            "JEV_METER": "0", "JEV_CORPUS_DIR": str(self.tmp / "corpus"),
            "JEV_TRACES_URL": "0",
        }

    def check(self):
        run = subprocess.run(["sh", str(SCRIPT), "check"], capture_output=True, text=True,
                             env=self.env, timeout=120, check=False)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse(self.asked.exists(),
                         self.asked.read_text() if self.asked.exists() else "")
        return run

    def block(self, out):
        """The digest's Jev lines, heading included, up to the next blank line."""
        lines = out.splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith("jev against")), None)
        if start is None:
            return []
        end = next((i for i in range(start, len(lines)) if not lines[i]), len(lines))
        return lines[start:end]


class FromTheLedger(DivergenceCase):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(PACK / "lib"))
        self.addCleanup(sys.path.remove, str(PACK / "lib"))
        from sd_db.database import default_path
        from sd_db.migrate import initialise
        self.store = default_path(self.home)
        self.store.parent.mkdir(parents=True)
        initialise(self.store)

    def write(self, rows):
        from sd_db import judgment
        from sd_db.database import connect
        connection = connect(self.store)
        try:
            for row in rows:
                judgment.record(connection, **row)
            connection.commit()
        finally:
            connection.close()

    def decision(self, stage, pair, said, old, when):
        """A shadow noul and, given `old`, the caller's own answer under its pair."""
        rows = [{"caller": "example-caller", "stage": stage, "provider": "typesafe",
                 "primitive": "noul", "outcome": "ok", "arm": "jev", "pair": pair,
                 "shadow": True, "answer": said, "now": when}]
        if old is not None:
            rows.append(self.baseline(stage, pair, old, when))
        return rows

    def baseline(self, stage, pair, old, when):
        """The caller's own answer for a pair; `None` is one that is no number."""
        return {"caller": "example-caller", "stage": stage, "provider": "local",
                "primitive": "baseline", "outcome": "ok", "arm": "baseline",
                "pair": pair, "answer": old, "now": when}

    def test_each_stage_reports_its_calls_and_how_often_jev_differed(self):
        now = datetime.datetime.now(datetime.UTC)
        week = (now - datetime.timedelta(days=1)).isoformat()
        month = (now - datetime.timedelta(days=30)).isoformat()
        rows = []
        # Four decisions this week, one of them a disagreement.
        for n, (said, old) in enumerate((("0.9", "1"), ("0.8", "1"), ("0.1", "0"), ("0.9", "0"))):
            rows += self.decision("JEV_EXAMPLE_A", f"a{n}", said, old, week)
        # A month old: outside the window, so its disagreement is not counted.
        rows += self.decision("JEV_EXAMPLE_A", "old", "0.9", "0", month)
        # Two calls with no answer of the caller's own beside them.
        rows += self.decision("JEV_EXAMPLE_B", None, "0.7", None, week)
        rows += self.decision("JEV_EXAMPLE_B", None, "0.2", None, week)
        self.write(rows)
        run = self.check()
        self.assertEqual(self.block(run.stdout), [
            HEADING,
            "  JEV_EXAMPLE_A                4 call(s), differed from the fallback in 1 of 4 (25%)",
            "  JEV_EXAMPLE_B                2 call(s), no fallback answer recorded",
        ], run.stdout)

    def test_an_old_answer_that_is_no_number_is_counted_not_compared(self):
        week = datetime.datetime.now(datetime.UTC).isoformat()
        rows = self.decision("JEV_EXAMPLE_C", "c0", "0.9", "1", week)
        rows += self.decision("JEV_EXAMPLE_C", "c1", "0.9", None, week)
        rows.append(self.baseline("JEV_EXAMPLE_C", "c1", None, week))
        rows += self.decision("JEV_EXAMPLE_D", "d0", "0.9", None, week)
        rows.append(self.baseline("JEV_EXAMPLE_D", "d0", None, week))
        self.write(rows)
        run = self.check()
        self.assertEqual(self.block(run.stdout), [
            HEADING,
            ("  JEV_EXAMPLE_C                2 call(s), differed from the fallback in 0 of 1 (0%),"
             " 1 not comparable"),
            "  JEV_EXAMPLE_D                1 call(s), no comparable fallback answer in 1 pair(s)",
        ], run.stdout)

    def test_a_week_without_jev_calls_says_so(self):
        run = self.check()
        self.assertEqual(self.block(run.stdout), [HEADING, "  no Jev calls recorded"],
                         run.stdout)


class WithoutTheLedger(DivergenceCase):
    def test_a_failed_read_is_unknown_and_not_zero(self):
        """A read that failed counts nothing; it must not read as a quiet week."""
        for body in ("exit 1\n", 'echo "not json"\n', 'echo "{}"\n'):
            with self.subTest(body=body):
                pack = self.tmp / f"pack-{len(body)}"
                _stub(pack / "bin" / "sd-db", body)
                self.env["SD_PACK_ROOT"] = str(pack)
                run = self.check()
                self.assertEqual(self.block(run.stdout), [UNKNOWN], run.stdout)

    def test_the_read_is_a_bounded_step_for_this_week(self):
        """The bound names the read in the job log, and the window is this week."""
        pack = self.tmp / "pack"
        argv = self.tmp / "sd-db-argv"
        _stub(pack / "bin" / "sd-db", f'printf "%s\\n" "$*" > "{argv}"\necho \'{{"stages": []}}\'\n')
        self.env["SD_PACK_ROOT"] = str(pack)
        run = self.check()
        cutoff = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        self.assertEqual(argv.read_text(), f"judgments compare --since {cutoff} --json\n")
        self.assertRegex(run.stderr, r"\[step [\d:]+\] \S+/bin/sd-db judgments compare .*\(bound 120s\)")

    def test_no_pack_leaves_the_block_out(self):
        """Like the drift line: a machine without the tool has nothing to read."""
        self.env["SD_PACK_ROOT"] = str(self.tmp / "no-pack")
        run = self.check()
        self.assertEqual(self.block(run.stdout), [], run.stdout)


if __name__ == "__main__":
    unittest.main()
