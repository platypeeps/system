"""The judgment ledger: what a row holds, what it refuses, and the per-stage
comparison the report prints.

The refusals are the privacy contract in executable form. This table promises
to hold identifiers and counts and nothing that was submitted, and a promise a
test does not check is a comment.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sd_db import connect
from sd_db import judgment
from sd_db.judgment import (
    CAUSES,
    GATE_PRIMITIVE,
    JudgmentRefused,
    MAX_ANSWER,
    MAX_NAME,
    MAX_ORDERING,
    by_stage,
    document,
    json_text,
    record,
    text,
    unpaired,
)
from sd_db.migrate import initialise

AT = "2026-09-20T12:00:00+00:00"


class JudgmentCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.connection = connect(self.path)
        self.addCleanup(self.connection.close)

    def row(self, row_id):
        return self.connection.execute(
            "SELECT * FROM judgment WHERE id = ?", (row_id,)
        ).fetchone()

    def rows(self):
        return list(self.connection.execute("SELECT * FROM judgment ORDER BY id"))

    def write(self, **extra):
        fields = dict(
            caller="local-mail-intake",
            stage="JEV_MAIL_INTAKE",
            provider="typesafe",
            primitive="noul",
            outcome="ok",
            now=AT,
        )
        fields.update(extra)
        return record(self.connection, **fields)


class TheRow(JudgmentCase):
    def test_a_call_is_recorded_field_for_field(self):
        row_id = self.write(
            model="jev-1.13.0", question_id="answer", questions=1,
            answer="0.97", confidence=0.88, tokens_in=412, tokens_out=9,
            duration_ms=740, changed="yes",
        )
        row = self.row(row_id)
        self.assertEqual(
            (row["caller"], row["stage"], row["provider"], row["model"],
             row["primitive"], row["question_id"], row["questions"]),
            ("local-mail-intake", "JEV_MAIL_INTAKE", "typesafe", "jev-1.13.0",
             "noul", "answer", 1),
        )
        self.assertEqual(
            (row["outcome"], row["cause"], row["answer"], row["confidence"],
             row["tokens_in"], row["tokens_out"], row["duration_ms"],
             row["changed"]),
            ("ok", None, "0.97", 0.88, 412, 9, 740, "yes"),
        )
        self.assertEqual(row["timestamp"], AT)

    def test_changed_defaults_to_unknown(self):
        self.assertEqual(self.row(self.write())["changed"], "unknown")

    def test_the_override_columns_are_empty_and_nullable(self):
        """Room for a later human or authoritative answer, and no path to it
        yet: the columns exist so that adding one is a write and not a
        migration."""
        row = self.row(self.write())
        self.assertEqual(
            (row["override"], row["override_source"], row["override_at"]),
            (None, None, None),
        )
        self.connection.execute(
            "UPDATE judgment SET override = ?, override_source = ?, override_at = ? "
            "WHERE id = ?", ("no", "operator", AT, row["id"]),
        )
        self.assertEqual(self.row(row["id"])["override"], "no")

    def test_usd_is_null_until_there_is_a_price_list(self):
        self.assertIsNone(self.row(self.write())["usd"])

    def test_a_second_provider_records_beside_the_first(self):
        """Provider-neutral means the field is required and its value is not.
        No vendor's name is a constraint anywhere in this table."""
        self.write()
        self.write(provider="some-other-vendor", primitive="verdict")
        self.assertEqual(
            sorted(row["provider"] for row in self.rows()),
            ["some-other-vendor", "typesafe"],
        )


class TheTwoArms(JudgmentCase):
    """One table, one stage key, two mechanisms. A fallback that cannot be
    counted against a judgment on the same stage answers nothing."""

    def test_a_row_is_the_model_arm_unless_it_says_otherwise(self):
        self.assertEqual(self.row(self.write())["arm"], "jev")

    def test_the_old_mechanism_records_under_the_same_stage(self):
        self.write(answer="0.97")
        self.write(arm="baseline", provider="local", primitive="baseline",
                   outcome="fallback", cause="switched-off", answer="2",
                   duration_ms=12)
        arms = [(row["arm"], row["stage"], row["answer"]) for row in self.rows()]
        self.assertEqual(arms, [("jev", "JEV_MAIL_INTAKE", "0.97"),
                                ("baseline", "JEV_MAIL_INTAKE", "2")])

    def test_a_pair_ties_the_two_arms_of_one_decision(self):
        self.write(pair="abc123", shadow=True, answer="1")
        self.write(pair="abc123", shadow=True, arm="baseline", provider="local",
                   primitive="baseline", answer="2")
        self.assertEqual([row["pair"] for row in self.rows()],
                         ["abc123", "abc123"])
        self.assertEqual([row["shadow"] for row in self.rows()], [1, 1])

    def test_shadow_is_off_unless_asked(self):
        self.assertEqual(self.row(self.write())["shadow"], 0)

    def test_an_arm_outside_the_two_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(arm="third")

    def test_every_decline_reason_is_its_own_value(self):
        """Six named reasons and a catch-all, because the repairs differ. The
        `no-path` case in particular has already caused a silent outage."""
        for cause in CAUSES:
            self.write(stage=f"stage-{cause}", outcome="fallback", cause=cause)
        found = {row["stage"]: row["cause"] for row in self.rows()}
        self.assertEqual(
            sorted(found.values()),
            sorted(["switched-off", "unkeyed", "no-path", "timeout", "invalid",
                    "unavailable", "budget"]),
        )
        self.assertEqual(len(set(found.values())), len(CAUSES))


class TheOrdering(JudgmentCase):
    """A re-ranking lane's delta is the two orderings. Positions, never the
    things at them."""

    def test_positions_are_kept_on_both_arms(self):
        self.write(ordering="0,1,2", arm="baseline", provider="local",
                   primitive="baseline", pair="p1")
        self.write(ordering="2,0,1", pair="p1")
        self.assertEqual([row["ordering"] for row in self.rows()],
                         ["0,1,2", "2,0,1"])

    def test_anything_that_is_not_positions_is_refused(self):
        for value in ("the quarterly numbers", "1;2", "a,b", "/Users/someone/x",
                      "1, 2"):
            with self.subTest(value=value):
                with self.assertRaises(JudgmentRefused) as caught:
                    self.write(ordering=value)
                self.assertIn("never stores submitted content",
                              str(caught.exception))
        self.assertEqual(self.rows(), [])

    def test_an_ordering_longer_than_the_cap_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(ordering=",".join("1" * (MAX_ORDERING + 1)))
        self.assertEqual(self.rows(), [])


class WhatItRefuses(JudgmentCase):
    def test_an_answer_longer_than_the_cap_is_refused_not_truncated(self):
        with self.assertRaises(JudgmentRefused) as caught:
            self.write(answer="x" * (MAX_ANSWER + 1))
        self.assertIn("never stores submitted content", str(caught.exception))
        self.assertEqual(self.rows(), [])

    def test_an_answer_with_a_newline_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(answer="one\ntwo")
        self.assertEqual(self.rows(), [])

    def test_a_short_path_is_refused_because_a_cap_is_not_a_guarantee(self):
        """The first review of this table: length keeps a paragraph out and a
        path straight in. An answer is a number, so there is nothing a caller
        can put here that is content."""
        with self.assertRaises(JudgmentRefused) as caught:
            self.write(answer="/Users/someone/private.txt")
        self.assertIn("must be a number", str(caught.exception))
        self.assertEqual(self.rows(), [])

    def test_a_criterion_key_is_refused_even_when_it_looks_harmless(self):
        """`phone` is a label somebody wrote, and the next one is a subject
        line. The caller has its own criteria, so it records which one won."""
        with self.assertRaises(JudgmentRefused):
            self.write(answer="phone")
        self.assertEqual(self.rows(), [])

    def test_the_three_numeric_shapes_an_answer_may_take_are_accepted(self):
        for value in ("0.97", "3", "-1", "0", "10.5"):
            with self.subTest(value=value):
                self.assertEqual(self.row(self.write(answer=value))["answer"],
                                 value)

    def test_a_name_longer_than_the_cap_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(stage="s" * (MAX_NAME + 1))
        self.assertEqual(self.rows(), [])

    def test_a_name_with_a_newline_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(caller="local-notify\nSubject: the quarterly numbers")
        self.assertEqual(self.rows(), [])

    def test_an_outcome_outside_the_five_is_refused(self):
        with self.assertRaises(JudgmentRefused) as caught:
            self.write(outcome="broke")
        self.assertIn("ok, timeout, fallback, unavailable, invalid",
                      str(caught.exception))

    def test_a_cause_outside_the_three_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(outcome="fallback", cause="ok")

    def test_changed_outside_the_three_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(changed="probably")

    def test_a_confidence_outside_zero_to_one_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(confidence=1.4)

    def test_a_negative_token_count_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(tokens_in=-1)

    def test_a_missing_caller_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(caller="")


#: Three stages. `drive` has both arms and a paired sample; `plan` has both
#: arms and no pair, which is the case the report has to name rather than let
#: a reader assume a delta exists; `solo` has only the model's arm.
SEED = (
    dict(stage="drive", outcome="ok", tokens_in=100, tokens_out=4,
         duration_ms=200, changed="yes", pair="d1", shadow=True),
    dict(stage="drive", arm="baseline", provider="local", primitive="baseline",
         outcome="ok", duration_ms=8, changed="no", pair="d1", shadow=True),
    dict(stage="drive", outcome="fallback", cause="timeout", duration_ms=30000),
    dict(stage="plan", outcome="ok", tokens_in=900, tokens_out=20,
         duration_ms=1500, changed="yes"),
    dict(stage="plan", arm="baseline", provider="local", primitive="decline",
         outcome="fallback", cause="no-path", duration_ms=3),
    dict(stage="solo", outcome="invalid", duration_ms=800),
)


class TheReport(JudgmentCase):
    def setUp(self):
        super().setUp()
        for entry in SEED:
            self.write(**entry)
        self.stages = {entry["stage"]: entry for entry in by_stage(self.connection)}

    def test_both_arms_are_compared_under_one_stage(self):
        drive = self.stages["drive"]
        self.assertEqual(
            (drive["arms"]["jev"]["calls"], drive["arms"]["jev"]["ok"],
             drive["arms"]["jev"]["fallbacks"]),
            (2, 1, 1),
        )
        self.assertEqual(drive["arms"]["jev"]["max_ms"], 30000)
        self.assertEqual(drive["arms"]["baseline"]["calls"], 1)
        self.assertEqual(drive["arms"]["baseline"]["max_ms"], 8)
        self.assertEqual((drive["arms"]["jev"]["tokens_in"],
                          drive["arms"]["jev"]["tokens_out"]), (100, 4))

    def test_a_stage_with_a_paired_sample_says_how_many(self):
        self.assertEqual(self.stages["drive"]["paired"], 1)
        self.assertEqual(self.stages["drive"]["arms"]["jev"]["shadow"], 1)

    def test_a_stage_with_no_paired_sample_is_named(self):
        """Both arms have run and never on the same decision, so no delta
        exists. Saying nothing would let a reader assume one does."""
        self.assertEqual(self.stages["plan"]["paired"], 0)
        self.assertEqual(sorted(unpaired(by_stage(self.connection))),
                         ["plan", "solo"])

    def test_the_decline_reasons_are_counted_by_name(self):
        self.assertEqual(self.stages["drive"]["declines"], {"timeout": 1})
        self.assertEqual(self.stages["plan"]["declines"], {"no-path": 1})

    def test_overrides_are_counted_per_stage(self):
        self.connection.execute(
            "UPDATE judgment SET override = 'no' WHERE id = "
            "(SELECT MIN(id) FROM judgment WHERE stage = 'plan')")
        stages = {entry["stage"]: entry for entry in by_stage(self.connection)}
        self.assertEqual(stages["plan"]["arms"]["jev"]["overrides"], 1)
        self.assertEqual(stages["drive"]["arms"]["jev"]["overrides"], 0)

    def test_the_bounds_are_text_against_the_one_timestamp_shape(self):
        self.write(stage="late", outcome="ok", now="2026-10-01T00:00:00+00:00")
        names = [entry["stage"] for entry in by_stage(self.connection, since="2026-10")]
        self.assertEqual(names, ["late"])
        names = [entry["stage"] for entry in by_stage(self.connection, until="2026-10")]
        self.assertEqual(names, ["drive", "plan", "solo"])

    def test_the_text_compares_the_arms_and_names_the_unpaired_stages(self):
        printed = text(by_stage(self.connection))
        self.assertIn("drive: 1 paired sample(s)", printed)
        self.assertIn("plan: 0 paired sample(s) — no delta yet", printed)
        self.assertIn("jev     : 2 run(s), 1 ok, 1 fallback (50%)", printed)
        self.assertIn("baseline: 1 run(s), 1 ok, 0 fallback (0%)", printed)
        self.assertIn("declines: 1 timeout", printed)
        self.assertIn("no paired sample yet, so no delta: plan, solo", printed)

    def test_a_stage_with_only_one_arm_says_the_other_has_no_runs(self):
        self.assertIn("baseline: no runs", text(by_stage(self.connection)))

    def test_an_empty_ledger_says_so_rather_than_printing_a_header(self):
        self.connection.execute("DELETE FROM judgment")
        self.assertEqual(text(by_stage(self.connection)),
                         "judgments: no calls recorded\n")

    def test_the_json_is_the_same_read(self):
        parsed = json.loads(json_text(by_stage(self.connection)))
        self.assertEqual([entry["stage"] for entry in parsed["stages"]],
                         ["drive", "plan", "solo"])
        self.assertEqual(parsed["unpaired"], ["plan", "solo"])
        self.assertEqual(parsed, document(by_stage(self.connection)))



class TheGateIsNotADecision(JudgmentCase):
    """One decline, one decision, and never two of either.

    A caller asks `jev enabled --record`, is declined, and then records what
    its own mechanism did. That is two rows for one decision, and counting
    both made a single decline read as two calls and two declines with
    nothing tying them together. The gate row says the old path is about to
    run; the completed row says what it did.
    """

    def gate(self, **extra):
        fields = dict(arm="baseline", provider="local-fallback",
                      primitive=GATE_PRIMITIVE, outcome="fallback",
                      cause="unkeyed")
        fields.update(extra)
        return self.write(**fields)

    def completed(self, **extra):
        fields = dict(arm="baseline", provider="local-fallback",
                      primitive="baseline", outcome="fallback",
                      cause="unkeyed")
        fields.update(extra)
        return self.write(**fields)

    def stage(self):
        return {entry["stage"]: entry
                for entry in by_stage(self.connection)}["JEV_MAIL_INTAKE"]

    def test_one_decline_and_its_decision_count_as_one_of_each(self):
        self.gate()
        self.completed()
        entry = self.stage()
        self.assertEqual(entry["arms"]["baseline"]["calls"], 1)
        self.assertEqual(entry["declines"], {"unkeyed": 1})
        self.assertEqual(entry["gates"], {"unkeyed": 1})

    def test_a_stage_whose_callers_only_decline_is_still_reported(self):
        """The gate row is the only fact a decline-only stage ever had.
        Leaving it out of the decision counts must not leave the stage out."""
        self.gate()
        entry = self.stage()
        self.assertEqual(entry["gates"], {"unkeyed": 1})
        self.assertEqual(entry["arms"], {})
        self.assertIn("never reached a decision: 1 unkeyed",
                      text(by_stage(self.connection)))

    def test_a_gate_event_is_not_a_paired_sample(self):
        """A pair needs both arms of one decision, and a gate is neither."""
        self.write(pair="p1")
        self.gate(pair="p1")
        self.assertEqual(self.stage()["paired"], 0)
        self.completed(pair="p1")
        self.assertEqual(self.stage()["paired"], 1)


class TheLedgerCanBeWrittenWithoutWaiting(JudgmentCase):
    """`connect(busy_timeout=0)` fails at once instead of waiting.

    Metering runs after the caller's answer is printed, but a subprocess
    caller still waits for the process to exit, so five seconds of lock
    contention is five seconds added to a decision that already finished.
    """

    def test_a_zero_timeout_refuses_a_locked_table_at_once(self):
        import sqlite3
        import time

        holder = connect(self.path)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        fast = connect(self.path, busy_timeout=0)
        self.addCleanup(fast.close)
        started = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            record(fast, caller="c", stage="s", provider="p",
                   primitive="noul", outcome="ok", now=AT)
        waited = time.monotonic() - started
        self.assertLess(waited, 1.0, f"waited {waited:.2f}s for a locked table")
        holder.execute("ROLLBACK")

    def test_the_default_still_waits_for_a_busy_database(self):
        """The library's own callers are waited on by a person; they keep the
        five seconds. Only the caller that asks gets zero."""
        self.assertEqual(
            connect(self.path).execute("PRAGMA busy_timeout").fetchone()[0],
            5000)


class AnIdentifierIsNotContent(JudgmentCase):
    """REGRESSION (the #489 Copilot review). The shape is the guarantee.

    A cap and a newline check pass `--stage /Users/someone/private.txt` and a
    short subject line with it, because both are strings and both are short.
    The table's promise is that no path and no submitted content reaches it,
    so the names the caller chooses are held to an identifier grammar.
    """

    def test_a_path_in_a_stage_is_refused(self):
        with self.assertRaises(JudgmentRefused) as caught:
            self.write(stage="/Users/someone/private.txt")
        self.assertIn("must be an identifier", str(caught.exception))

    def test_a_subject_line_in_a_question_id_is_refused(self):
        with self.assertRaises(JudgmentRefused) as caught:
            self.write(question_id="the quarterly numbers")
        self.assertIn("must be an identifier", str(caught.exception))

    def test_a_caller_that_is_a_path_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(caller="./tools/notify")

    def test_a_pair_that_is_a_path_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(pair="/tmp/pair")

    def test_the_names_this_repository_actually_records_are_accepted(self):
        for stage in ("JEV_MAIL_INTAKE", "JEV_NOTIFY", "sd-review", "plan.2"):
            with self.subTest(stage=stage):
                self.write(stage=stage, caller="local-mail-intake",
                           question_id="notify-route", pair="d1")

    def test_a_model_name_keeps_its_vendor_prefix(self):
        """Provider and model are exempt: the names come from a vendor."""
        self.write(provider="anthropic", model="anthropic/claude-opus-5")


class AReaderOnAnOlderSchema(unittest.TestCase):
    """REGRESSION (the #489 Copilot review). `_open_for_read` allows a
    database that has not run migration 011, and a reader may not migrate
    one. The empty list is that database's honest answer, and it is what the
    read-only open promises."""

    def test_a_database_without_the_table_reads_as_no_judgments(self):
        connection = sqlite3.connect(":memory:")
        try:
            self.assertFalse(judgment.present(connection))
            self.assertEqual(by_stage(connection), [])
            self.assertEqual(text(by_stage(connection)),
                             "judgments: no calls recorded\n")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
