"""The comparison arms (sd:2366): a `kev` and a `haiku` row beside each Jev
call, what they may hold, and the per-arm report `judgments compare` prints.

The arms only record. Their rows share the Jev row's pair id, so agreement and
accuracy are computed here, at read time, from rows that hold numbers only.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sd_db import connect
from sd_db.judgment import (
    ARMS,
    JudgmentRefused,
    by_stage,
    compare,
    compare_json,
    compare_text,
    label,
    record,
    unlabelled,
)
from sd_db.migrate import initialise, migrate
from sd_db.schema import SCHEMA_DIR

AT = "2026-10-01T12:00:00+00:00"


class CompareCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.connection = connect(self.path)
        self.addCleanup(self.connection.close)

    def write(self, **extra):
        fields = dict(caller="local-notify", stage="JEV_NOTIFY",
                      provider="typesafe", primitive="noul", outcome="ok",
                      now=AT)
        fields.update(extra)
        return record(self.connection, **fields)

    def row(self, row_id):
        return self.connection.execute(
            "SELECT * FROM judgment WHERE id = ?", (row_id,)).fetchone()


class TheArms(CompareCase):
    def test_the_two_comparison_arms_are_accepted(self):
        self.assertEqual(ARMS, ("jev", "baseline", "kev", "haiku"))
        kev = self.write(arm="kev", provider="local", model="jaredpalmer/kev-4b@v1.0",
                         pair="p1", usd=0.0)
        haiku = self.write(arm="haiku", provider="openrouter",
                           model="anthropic/claude-haiku-4.5", pair="p1")
        self.assertEqual(self.row(kev)["arm"], "kev")
        self.assertEqual(self.row(haiku)["provider"], "openrouter")

    def test_the_database_refuses_an_arm_the_library_does_not_name(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.connection:
                self.connection.execute(
                    "INSERT INTO judgment (timestamp, caller, stage, arm, provider, "
                    "primitive, outcome) VALUES (?, 'c', 's', 'gpt', 'p', 'noul', 'ok')",
                    (AT,))

    def test_server_latency_and_probabilities_are_stored(self):
        row = self.row(self.write(primitive="choice", answer="1", server_ms=495,
                                  probabilities="0.47,0.28,0.25"))
        self.assertEqual(row["server_ms"], 495)
        self.assertEqual(row["probabilities"], "0.47,0.28,0.25")

    def test_a_distribution_over_every_option_kev_supports_is_stored(self):
        # Kev's API takes up to 255 options; 0.0000 for each is 1784 characters.
        value = ",".join(["0.0000"] * 254 + ["1.0000"])
        row = self.row(self.write(primitive="choice", answer="255", probabilities=value))
        self.assertEqual(row["probabilities"], value)

    def test_a_distribution_over_more_options_than_kev_supports_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(probabilities=",".join(["0"] * 256))

    def test_the_labelling_queue_holds_no_comparison_arm_row(self):
        jev_row = self.write(arm="jev", pair="p1", answer="0.9", question_id="q1")
        base = self.write(arm="baseline", provider="local", answer="1", question_id="q2")
        self.write(arm="kev", provider="local", pair="p1", answer="0.8", question_id="q1")
        self.write(arm="haiku", provider="anthropic", pair="p1", answer="0.7",
                   question_id="q1")
        found = unlabelled(self.connection, "JEV_NOTIFY")
        self.assertEqual(sorted(row["id"] for row in found), sorted([jev_row, base]))

    def test_a_label_on_a_comparison_arm_row_is_refused(self):
        self.write(arm="jev", pair="p1", answer="0.9")
        for arm, provider in (("kev", "local"), ("haiku", "anthropic")):
            row = self.write(arm=arm, provider=provider, pair="p1", answer="0.8")
            with self.subTest(arm=arm), self.assertRaises(JudgmentRefused):
                label(self.connection, row, "1", "test-rule")
            self.assertIsNone(self.row(row)["override"])

    def test_probabilities_that_are_not_numbers_are_refused(self):
        for value in ("returns,shipping", "0.5;0.5", "0.5, 0.5", "/home/x", ""):
            with self.subTest(value=value), self.assertRaises(JudgmentRefused):
                self.write(probabilities=value)

    def test_server_latency_must_be_a_count(self):
        with self.assertRaises(JudgmentRefused):
            self.write(server_ms=-1)

    def test_the_old_report_reads_only_jev_and_baseline(self):
        self.write(pair="p1", answer="0.9")
        self.write(arm="baseline", provider="local", primitive="baseline", pair="p1")
        self.write(arm="kev", provider="local", pair="p1", answer="0.8")
        self.write(arm="haiku", provider="anthropic", pair="p1", answer="0.7")
        stages = by_stage(self.connection)
        self.assertEqual(sorted(stages[0]["arms"]), ["baseline", "jev"])
        # One decision with both of the old arms is one paired sample, still.
        self.assertEqual(stages[0]["paired"], 1)


class TheMigration(unittest.TestCase):
    def test_rows_written_at_16_survive_017_with_their_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sd.db"
            initialise(path)
            raw = sqlite3.connect(path, isolation_level=None)
            text = (SCHEMA_DIR / "017_judgment_compare_arms.sql").read_text()
            raw.executescript("\n".join(line[4:] for line in text.splitlines()
                                        if line.startswith("--   ")))
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 16)
            raw.execute(
                "INSERT INTO judgment (id, timestamp, caller, stage, arm, provider, "
                "primitive, outcome, answer) "
                "VALUES (7, ?, 'c', 's', 'jev', 'typesafe', 'noul', 'ok', '0.4')", (AT,))
            raw.close()
            migrate(path)
            connection = connect(path)
            try:
                row = connection.execute("SELECT * FROM judgment WHERE id = 7").fetchone()
                self.assertEqual(row["answer"], "0.4")
                self.assertIsNone(row["server_ms"])
                record(connection, caller="c", stage="s", provider="local",
                       primitive="noul", outcome="ok", arm="kev", now=AT)
            finally:
                connection.close()


class TheComparison(CompareCase):
    """Three noul decisions and one choice, each asked of all three arms."""

    def decision(self, pair, jev, kev, haiku, primitive="noul", probabilities=None,
                 ms=(100, 700, 1500)):
        probabilities = probabilities or (None, None, None)
        ids = []
        for arm, provider, answer, p, duration in (
                ("jev", "typesafe", jev, probabilities[0], ms[0]),
                ("kev", "local", kev, probabilities[1], ms[1]),
                ("haiku", "anthropic", haiku, probabilities[2], ms[2])):
            ids.append(self.write(arm=arm, provider=provider, pair=pair,
                                  primitive=primitive, answer=answer,
                                  probabilities=p, duration_ms=duration,
                                  tokens_in=10, tokens_out=2,
                                  usd=0.0 if arm == "kev" else (0.001 if arm == "haiku" else None),
                                  server_ms=50 if arm == "kev" else None))
        return ids

    def arm(self, report, name, provider=None):
        for entry in report[0]["arms"]:
            if entry["arm"] == name and (provider is None or entry["provider"] == provider):
                return entry
        self.fail(f"no {name} arm in {report}")

    def test_agreement_is_measured_against_the_jev_row_of_the_same_pair(self):
        self.decision("a", "0.9", "0.8", "0.3")   # kev agrees, haiku does not
        self.decision("b", "0.2", "0.1", "0.4")   # both agree
        self.decision("c", "0.6", "0.4", "0.7")   # haiku agrees
        report = compare(self.connection)
        kev, haiku = self.arm(report, "kev"), self.arm(report, "haiku")
        self.assertEqual((kev["paired"], kev["agree"]), (3, 2))
        self.assertEqual((haiku["paired"], haiku["agree"]), (3, 2))
        self.assertAlmostEqual(kev["mean_abs_dp"], (0.1 + 0.1 + 0.2) / 3)
        self.assertAlmostEqual(haiku["mean_abs_dp"], (0.6 + 0.2 + 0.1) / 3)
        self.assertIsNone(self.arm(report, "jev")["agree"])

    def test_latency_percentiles_tokens_and_cost(self):
        self.decision("a", "0.9", "0.8", "0.3", ms=(100, 600, 1000))
        self.decision("b", "0.9", "0.8", "0.3", ms=(200, 700, 2000))
        self.decision("c", "0.9", "0.8", "0.3", ms=(300, 800, 9000))
        report = compare(self.connection)
        haiku = self.arm(report, "haiku")
        self.assertEqual(haiku["calls"], 3)
        self.assertEqual(haiku["p50_ms"], 2000)
        self.assertEqual(haiku["p95_ms"], 9000)
        self.assertEqual((haiku["tokens_in"], haiku["tokens_out"]), (30, 6))
        self.assertAlmostEqual(haiku["usd"], 0.003)
        kev = self.arm(report, "kev")
        self.assertEqual(kev["usd"], 0.0)
        self.assertEqual(kev["server_p50_ms"], 50)

    def test_a_choice_agrees_on_the_position_that_won(self):
        self.decision("a", "2", "2", "1", primitive="choice")
        report = compare(self.connection)
        self.assertEqual(self.arm(report, "kev")["agree"], 1)
        self.assertEqual(self.arm(report, "haiku")["agree"], 0)
        self.assertIsNone(self.arm(report, "kev")["mean_abs_dp"])

    def test_only_the_jev_row_carries_the_label_of_a_pair(self):
        # A label that reached an arm row some other way is not the pair's truth.
        _, kev, _ = self.decision("a", "0.9", "0.8", "0.3")
        with self.connection:
            self.connection.execute(
                "UPDATE judgment SET override = '0', override_source = 'test-rule' "
                "WHERE id = ?", (kev,))
        report = compare(self.connection)
        for name in ("kev", "haiku"):
            self.assertEqual(self.arm(report, name)["labelled"], 0)

    def test_a_score_agrees_and_is_right_on_the_recorded_score_not_the_mode(self):
        # Kev records 2.0, its expected level; its most likely level is 0.
        jev, _, _ = self.decision(
            "a", "2", "2", "0", primitive="score",
            probabilities=(None, "0.4,0.0,0.35,0.25", None))
        label(self.connection, jev, "2", "test-rule")
        report = compare(self.connection)
        kev = self.arm(report, "kev")
        self.assertEqual((kev["paired"], kev["agree"]), (1, 1))
        self.assertEqual((kev["labelled"], kev["right"]), (1, 1))
        # The distribution still scores Brier: 0.4^2 + 0.65^2 + 0.25^2.
        self.assertAlmostEqual(kev["brier"], 0.16 + 0.4225 + 0.0625)

    def test_accuracy_and_brier_read_the_label_on_the_jev_row(self):
        jev, _, _ = self.decision("a", "0.9", "0.8", "0.3")
        label(self.connection, jev, "1", "test-rule")
        jev, _, _ = self.decision(
            "b", "1", "1", "2", primitive="choice",
            probabilities=("0.6,0.4", "0.9,0.1", "0.2,0.8"))
        label(self.connection, jev, "1", "test-rule")
        report = compare(self.connection)
        kev, haiku = self.arm(report, "kev"), self.arm(report, "haiku")
        self.assertEqual((kev["labelled"], kev["right"]), (2, 2))
        self.assertEqual((haiku["labelled"], haiku["right"]), (2, 0))
        # Noul: (0.8 - 1)^2 = 0.04; choice: (0.9-1)^2 + 0.1^2 = 0.02.
        self.assertAlmostEqual(kev["brier"], (0.04 + 0.02) / 2)
        # Noul: 0.7^2 = 0.49; choice: 0.8^2 + 0.8^2 = 1.28.
        self.assertAlmostEqual(haiku["brier"], (0.49 + 1.28) / 2)

    def test_declines_are_counted_by_cause_and_excluded_from_agreement(self):
        self.decision("a", "0.9", "0.8", "0.3")
        self.write(arm="haiku", provider="anthropic", pair="b", outcome="unavailable",
                   cause="unkeyed")
        report = compare(self.connection)
        haiku = self.arm(report, "haiku")
        self.assertEqual(haiku["calls"], 2)
        self.assertEqual(haiku["ok"], 1)
        self.assertEqual(haiku["declines"], {"unkeyed": 1})
        self.assertEqual(haiku["paired"], 1)

    def test_each_haiku_transport_is_its_own_line(self):
        self.decision("a", "0.9", "0.8", "0.3")
        self.write(arm="haiku", provider="openrouter", pair="a", answer="0.95")
        report = compare(self.connection)
        self.assertEqual(self.arm(report, "haiku", "openrouter")["agree"], 1)
        self.assertEqual(self.arm(report, "haiku", "anthropic")["agree"], 0)

    def test_a_stage_filter_and_the_two_serialisations(self):
        self.decision("a", "0.9", "0.8", "0.3")
        self.write(stage="JEV_OTHER", pair="z", answer="0.5")
        self.assertEqual([s["stage"] for s in compare(self.connection)],
                         ["JEV_NOTIFY", "JEV_OTHER"])
        only = compare(self.connection, stage="JEV_OTHER")
        self.assertEqual([s["stage"] for s in only], ["JEV_OTHER"])
        report = compare(self.connection, stage="JEV_NOTIFY")
        printed = compare_text(report)
        self.assertIn("JEV_NOTIFY", printed)
        self.assertIn("kev", printed)
        self.assertIn("agree", printed)
        parsed = json.loads(compare_json(report))
        self.assertEqual(parsed["stages"][0]["stage"], "JEV_NOTIFY")

    def test_an_empty_ledger_says_so(self):
        self.assertEqual(compare(self.connection), [])
        self.assertIn("no calls", compare_text([]))


class TheCompareVerb(CompareCase):
    """`sd-db.sh judgments compare`, through the same CLI entry the shell uses."""

    def cli(self, *argv):
        import contextlib
        import io
        import os
        from unittest import mock

        from sd_db.jobs import cli

        home = Path(self.tmp.name) / "home"
        target = home / ".local/share/sd/sd.db"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(self.path)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_text_json_and_a_stage_filter(self):
        self.write(pair="a", answer="0.9")
        self.write(arm="kev", provider="local", pair="a", answer="0.8")
        self.write(stage="JEV_OTHER", pair="b", answer="0.1")
        code, out, _ = self.cli("judgments", "compare")
        self.assertEqual(code, 0)
        self.assertIn("JEV_NOTIFY", out)
        self.assertIn("agree with jev 1/1", out)
        code, out, _ = self.cli("judgments", "compare", "--stage", "JEV_OTHER", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([s["stage"] for s in json.loads(out)["stages"]], ["JEV_OTHER"])

    def test_an_unknown_flag_is_refused(self):
        code, _, err = self.cli("judgments", "compare", "--bogus")
        self.assertEqual(code, 1)
        self.assertIn("compare", err)


if __name__ == "__main__":
    unittest.main()
