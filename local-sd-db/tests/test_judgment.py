"""The judgment ledger: what a row holds, what it refuses, and the per-stage
comparison the report prints.

The refusals are the privacy contract in executable form. This table promises
to hold identifiers and counts and nothing that was submitted, and a promise a
test does not check is a comment.
"""

import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertIsNone(self.row(self.write(tokens_in=400, tokens_out=9))["usd"])

    def test_a_second_provider_records_beside_the_first(self):
        """Provider-neutral means the field is required and its value is not.
        No vendor's name is a constraint anywhere in this table."""
        self.write()
        self.write(provider="some-other-vendor", primitive="verdict")
        self.assertEqual(
            sorted(row["provider"] for row in self.rows()),
            ["some-other-vendor", "typesafe"],
        )


PRICES = """\
bills:
  usage: { cost: usage }
providers:
  typesafe: { url: "https://api.example.test/v1/systemone", vendor: typesafe,
              bill: usage, roles: [], price: { in: 0.5, out: 2 } }
  pinned:   { url: "https://pinned.example.test/v1", model: pinned-1, vendor: pinned,
              bill: usage, roles: [], price: { in: 1, out: 1 } }
  half:     { url: "https://half.example.test/v1", vendor: half, bill: usage,
              roles: [], price: { in: 1 } }
roles:
  author: []
"""


class TheLocation(JudgmentCase):
    """Where a call came from (sd:2950). The one field a bad value does not
    refuse: it is stored as NULL and the row is written."""

    def test_a_location_is_stored(self):
        row_id = self.write(location="~/repos/system")
        self.assertEqual(self.row(row_id)["location"], "~/repos/system")

    def test_an_omitted_location_is_null(self):
        self.assertIsNone(self.row(self.write())["location"])

    def test_the_longest_location_is_kept(self):
        value = "x" * judgment.MAX_LOCATION
        self.assertEqual(self.row(self.write(location=value))["location"], value)

    def test_a_bad_location_is_null_and_the_row_is_still_written(self):
        for value in ("x" * (judgment.MAX_LOCATION + 1), "one\ntwo", "tab\there",
                      "nul\x00", "bell\x07", "del\x7f", "", 42):
            with self.subTest(value=value):
                row = self.row(self.write(location=value))
                self.assertIsNotNone(row)
                self.assertIsNone(row["location"])
                self.assertEqual(row["caller"], "local-mail-intake")

    def test_a_path_that_cannot_be_resolved_is_null_and_the_row_is_still_written(self):
        for failure in (PermissionError("denied"), RuntimeError("symlink loop"),
                        ValueError("anything else")):
            with self.subTest(failure=failure), \
                    mock.patch("sd_db.judgment.paths.key", side_effect=failure):
                row = self.row(self.write(location="/opt/unreadable"))
                self.assertIsNotNone(row)
                self.assertIsNone(row["location"])

    def test_a_resolved_key_is_held_to_the_same_rule(self):
        # Resolving follows symlinks, so a printable, short alias can resolve
        # to a key that is neither.
        for resolved in ("~/a\nb", "~/bell\x07", "~/" + "x" * judgment.MAX_LOCATION):
            with self.subTest(resolved=resolved), \
                    mock.patch("sd_db.judgment.paths.key", return_value=resolved):
                row = self.row(self.write(location="~/alias"))
                self.assertIsNotNone(row)
                self.assertIsNone(row["location"])

    def test_no_usable_home_keeps_the_location_as_given(self):
        with mock.patch("sd_db.judgment.paths.key",
                        side_effect=judgment.paths.PathRefused("HOME is unset")):
            self.assertEqual(self.row(self.write(location="/opt/outside"))["location"],
                             "/opt/outside")


class TheCallContext(JudgmentCase):
    """`threshold`, `run_id`, `prompt_hash` and `load_avg` (sd:2950). Each is
    held to its shape, and a value that fails it is stored as NULL with the
    row kept, as `location` is."""

    CONTEXT = ("threshold", "run_id", "prompt_hash", "load_avg")

    def context(self, **fields):
        row = self.row(self.write(**fields))
        self.assertIsNotNone(row)
        self.assertEqual(row["caller"], "local-mail-intake")
        return {name: row[name] for name in self.CONTEXT}

    def test_every_field_is_stored(self):
        self.assertEqual(
            self.context(threshold=0.75, run_id="sd-review:2950:r4",
                         prompt_hash="0123456789abcdef", load_avg=3.5),
            {"threshold": 0.75, "run_id": "sd-review:2950:r4",
             "prompt_hash": "0123456789abcdef", "load_avg": 3.5})

    def test_an_omitted_field_is_null(self):
        self.assertEqual(self.context(), dict.fromkeys(self.CONTEXT))

    def test_the_edges_are_kept(self):
        for field, value in (("threshold", 0), ("threshold", 1), ("load_avg", 0),
                             ("run_id", "a" * MAX_NAME), ("prompt_hash", "a" * 12),
                             ("prompt_hash", "f" * 64)):
            with self.subTest(field=field, value=value):
                self.assertEqual(self.context(**{field: value})[field], value)

    def test_a_bad_value_is_null_and_the_row_is_still_written(self):
        bad = {
            "threshold": (-0.01, 1.01, math.nan, math.inf, True, "0.5"),
            "run_id": ("", "-leading", "has space", "a/b", "line\n", "a" * (MAX_NAME + 1), 7),
            "prompt_hash": ("a" * 11, "a" * 65, "ABCDEF012345", "0123456789ag", "", 123456789012),
            "load_avg": (-0.1, math.nan, math.inf, False, "1.0"),
        }
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertIsNone(self.context(**{field: value})[field])


class ThePrice(JudgmentCase):
    """A row's cost from the price `providers.yaml` registers for its
    provider (sd:2358). The price is the operator's to enter; with none
    registered the row keeps its tokens and no cost."""

    def setUp(self):
        super().setUp()
        (self.path.parent / "providers.yaml").write_text(PRICES, encoding="utf-8")

    def test_a_registered_price_fills_usd_from_the_tokens(self):
        row = self.row(self.write(tokens_in=400_000, tokens_out=10_000))
        self.assertIsNotNone(row["usd"])
        # 400k x $0.50/M + 10k x $2/M.
        self.assertAlmostEqual(row["usd"], 0.2 + 0.02)

    def test_a_cost_the_caller_reports_wins(self):
        row = self.row(self.write(tokens_in=400_000, tokens_out=10_000, usd=0.5))
        self.assertEqual(row["usd"], 0.5)

    def test_an_unregistered_provider_stays_unpriced(self):
        row = self.row(self.write(provider="nobody", tokens_in=400, tokens_out=9))
        self.assertIsNone(row["usd"])

    def test_a_row_with_no_tokens_has_no_cost(self):
        self.assertIsNone(self.row(self.write())["usd"])

    def test_a_pinned_model_prices_that_model_only(self):
        self.assertAlmostEqual(self.row(self.write(
            provider="pinned", model="pinned-1", tokens_in=1_000_000))["usd"], 1.0)
        self.assertIsNone(self.row(self.write(
            provider="pinned", model="pinned-2", tokens_in=1_000_000))["usd"])

    def test_a_side_with_tokens_and_no_price_leaves_the_row_unpriced(self):
        self.assertIsNone(self.row(self.write(
            provider="half", tokens_in=100, tokens_out=5))["usd"])
        self.assertAlmostEqual(self.row(self.write(
            provider="half", tokens_in=1_000_000, tokens_out=0))["usd"], 1.0)

    def test_an_unreadable_registry_costs_no_row(self):
        (self.path.parent / "providers.yaml").write_text("bills: [", encoding="utf-8")
        row = self.row(self.write(tokens_in=400, tokens_out=9))
        self.assertEqual((row["tokens_in"], row["usd"]), (400, None))

    def test_the_report_names_the_unpriced_rows_and_their_provider(self):
        self.write(tokens_in=400_000, tokens_out=10_000)
        self.write(provider="nobody", tokens_in=400, tokens_out=9)
        self.write(provider="nobody", tokens_in=100, tokens_out=1)
        jev = by_stage(self.connection)[0]["arms"]["jev"]
        self.assertEqual((jev["unpriced"], jev["unpriced_providers"]), (2, ["nobody"]))
        self.assertAlmostEqual(jev["usd"], 0.22)
        self.assertIn("cost $0.2200 (2 unpriced: no providers.yaml price for nobody)",
                      text(by_stage(self.connection)))


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


#: The subject a labelled stage writes, and the rule that labels it.
SUBJECT = "sd-review-tier:example.widgets:0123456789ab"
SOURCE = "outcome.example"
LATER = "2026-10-05T12:00:00+00:00"


class TheLabel(JudgmentCase):
    """A later, authoritative answer, written through the one door the ledger
    owns. The refusals are the same privacy contract as a row's: a number,
    an identifier, and nothing that was submitted."""

    def judged(self, **extra):
        fields = dict(stage="JEV_SD_REVIEW", primitive="choice",
                      question_id=SUBJECT, answer="2", confidence=0.93)
        fields.update(extra)
        return self.write(**fields)

    def test_a_label_is_written_with_its_source_and_its_moment(self):
        row_id = self.judged()
        self.assertTrue(judgment.label(self.connection, row_id, "3", SOURCE,
                                       now=LATER))
        row = self.row(row_id)
        self.assertEqual(
            (row["override"], row["override_source"], row["override_at"]),
            ("3", SOURCE, LATER))

    def test_the_same_label_again_changes_nothing(self):
        row_id = self.judged()
        judgment.label(self.connection, row_id, "2", SOURCE, now=LATER)
        self.assertFalse(judgment.label(self.connection, row_id, "2", SOURCE,
                                        now="2026-11-01T00:00:00+00:00"))
        self.assertEqual(self.row(row_id)["override_at"], LATER)

    def test_a_different_label_is_refused_without_replace(self):
        row_id = self.judged()
        judgment.label(self.connection, row_id, "2", SOURCE, now=LATER)
        with self.assertRaises(JudgmentRefused) as caught:
            judgment.label(self.connection, row_id, "3", SOURCE, now=LATER)
        self.assertIn("already labelled", str(caught.exception))
        self.assertEqual(self.row(row_id)["override"], "2")

    def test_replace_writes_the_different_label(self):
        row_id = self.judged()
        judgment.label(self.connection, row_id, "2", SOURCE, now=LATER)
        self.assertTrue(judgment.label(self.connection, row_id, "3", "operator",
                                       replace=True, now=LATER))
        row = self.row(row_id)
        self.assertEqual((row["override"], row["override_source"]),
                         ("3", "operator"))

    def test_a_label_that_is_not_a_number_is_refused(self):
        row_id = self.judged()
        for value in ("deep", "/srv/example.test/private.txt", "", None):
            with self.subTest(value=value):
                with self.assertRaises(JudgmentRefused):
                    judgment.label(self.connection, row_id, value, SOURCE)
        self.assertIsNone(self.row(row_id)["override"])

    def test_a_source_that_is_not_an_identifier_is_refused(self):
        row_id = self.judged()
        for value in ("a later fix", "/tmp/rule", "", None):
            with self.subTest(value=value):
                with self.assertRaises(JudgmentRefused):
                    judgment.label(self.connection, row_id, "3", value)
        self.assertIsNone(self.row(row_id)["override_source"])

    def test_a_row_that_does_not_exist_is_refused(self):
        with self.assertRaises(JudgmentRefused) as caught:
            judgment.label(self.connection, 999, "3", SOURCE)
        self.assertIn("no judgment row 999", str(caught.exception))

    def test_a_row_with_no_answer_cannot_be_labelled(self):
        """Copilot b655780dbf07: a failed call or an ask batch recorded no
        answer, so a label on it could only ever count as wrong."""
        row_id = self.judged(answer=None, outcome="timeout")
        with self.assertRaises(JudgmentRefused) as caught:
            judgment.label(self.connection, row_id, "2", SOURCE)
        self.assertIn("no answer", str(caught.exception))
        self.assertIsNone(self.row(row_id)["override"])

    def test_labels_compare_exactly_and_not_as_floats(self):
        """Copilot 8a3aaacf325b: 2**53 and 2**53 + 1 are one float, so a float
        comparison called a different label the same and refused nothing."""
        row_id = self.judged(answer="9007199254740992")
        judgment.label(self.connection, row_id, "9007199254740992", SOURCE)
        self.assertTrue(judgment.label(self.connection, row_id, "9007199254740993",
                                       SOURCE, replace=True))
        self.assertFalse(judgment.label(self.connection, row_id, "9007199254740993.0",
                                        SOURCE))

    def test_a_gate_event_cannot_be_labelled(self):
        """A gate row is not a decision, so there is nothing to be right about."""
        row_id = self.write(arm="baseline", provider="local-fallback",
                            primitive=GATE_PRIMITIVE, outcome="fallback",
                            cause="unkeyed")
        with self.assertRaises(JudgmentRefused):
            judgment.label(self.connection, row_id, "1", SOURCE)


class TheUnlabelled(JudgmentCase):
    def test_only_unlabelled_rows_of_the_stage_under_the_prefix_oldest_first(self):
        later = self.write(stage="JEV_SD_REVIEW", question_id=SUBJECT,
                           answer="1", now="2026-09-22T00:00:00+00:00")
        earlier = self.write(stage="JEV_SD_REVIEW", question_id=SUBJECT[:-1] + "c",
                             answer="2", now="2026-09-21T00:00:00+00:00")
        done = self.write(stage="JEV_SD_REVIEW", question_id=SUBJECT, answer="3")
        judgment.label(self.connection, done, "3", SOURCE)
        self.write(stage="JEV_SD_REVIEW", question_id="sd-review-tier", answer="1")
        self.write(stage="JEV_NOTIFY", question_id=SUBJECT, answer="1")
        self.write(stage="JEV_SD_REVIEW", arm="baseline", provider="local",
                   primitive=GATE_PRIMITIVE, outcome="fallback", cause="unkeyed")
        found = judgment.unlabelled(self.connection, "JEV_SD_REVIEW",
                                    prefix="sd-review-tier:")
        self.assertEqual([row["id"] for row in found], [earlier, later])
        self.assertEqual(found[0]["answer"], "2")
        self.assertEqual(found[0]["question_id"], SUBJECT[:-1] + "c")

    def test_a_row_with_no_answer_is_not_listed(self):
        """Copilot 2095adee1168: `label` refuses an answerless row, so listing
        one would hand a labeller a row it can never write."""
        self.write(stage="JEV_SD_REVIEW", question_id=SUBJECT, answer=None,
                   outcome="timeout")
        kept = self.write(stage="JEV_SD_REVIEW", question_id=SUBJECT, answer="2")
        self.assertEqual([row["id"] for row in
                          judgment.unlabelled(self.connection, "JEV_SD_REVIEW")], [kept])

    def test_no_prefix_means_every_unlabelled_decision_of_the_stage(self):
        self.write(stage="JEV_SD_REVIEW", question_id=SUBJECT, answer="1")
        self.write(stage="JEV_SD_REVIEW", answer="1")
        self.assertEqual(len(judgment.unlabelled(self.connection, "JEV_SD_REVIEW")), 2)


class TheCorrectnessReport(JudgmentCase):
    """Labelled and right per arm, and the same by reported confidence, so a
    floor can be read off the report instead of guessed."""

    def setUp(self):
        super().setUp()
        rows = (("2", 0.95, "2"), ("2", 0.93, "3"), ("4", 0.62, "4"),
                ("1", None, "1"), ("3", 0.97, None))
        for answer, confidence, override in rows:
            row_id = self.write(stage="JEV_SD_REVIEW", primitive="choice",
                                question_id=SUBJECT, answer=answer,
                                confidence=confidence)
            if override is not None:
                judgment.label(self.connection, row_id, override, SOURCE)
        # A label that equals the answer in another spelling is still right.
        row_id = self.write(stage="JEV_SD_REVIEW", arm="baseline",
                            provider="local", primitive="baseline", answer="2")
        judgment.label(self.connection, row_id, "2.0", SOURCE)
        self.entry = {entry["stage"]: entry
                      for entry in by_stage(self.connection)}["JEV_SD_REVIEW"]

    def test_a_wrong_label_past_float_precision_is_not_right(self):
        """Copilot 8a3aaacf325b: the report compared REAL casts, so a label one
        past 2**53 read as right."""
        row_id = self.write(stage="precise", answer="9007199254740992")
        judgment.label(self.connection, row_id, "9007199254740993", SOURCE)
        entry = {e["stage"]: e for e in by_stage(self.connection)}["precise"]
        self.assertEqual((entry["arms"]["jev"]["labelled"],
                          entry["arms"]["jev"]["right"]), (1, 0))
        self.assertEqual([(b["arm"], b["labelled"], b["right"]) for b in entry["bands"]],
                         [("jev", 1, 0)])

    def test_labelled_and_right_are_counted_per_arm(self):
        jev = self.entry["arms"]["jev"]
        self.assertEqual((jev["labelled"], jev["right"]), (4, 3))
        baseline = self.entry["arms"]["baseline"]
        self.assertEqual((baseline["labelled"], baseline["right"]), (1, 1))

    def test_the_bands_are_reported_confidence_in_tenths_per_arm(self):
        """Copilot 3b5b44ffd195: a baseline row has no confidence, so pooling
        it with the model's `none` band mixed two arms in one number."""
        bands = [(band["arm"], band["band"], band["labelled"], band["right"])
                 for band in self.entry["bands"]]
        self.assertEqual(bands, [("baseline", "none", 1, 1), ("jev", "0.6", 1, 1),
                                 ("jev", "0.9", 2, 1), ("jev", "none", 1, 1)])

    def test_a_confidence_of_one_is_in_the_top_band(self):
        row_id = self.write(stage="JEV_SD_REVIEW", answer="1", confidence=1.0)
        judgment.label(self.connection, row_id, "1", SOURCE)
        entry = {e["stage"]: e for e in by_stage(self.connection)}["JEV_SD_REVIEW"]
        self.assertEqual({(b["arm"], b["band"]): b["labelled"]
                          for b in entry["bands"]}[("jev", "0.9")], 3)

    def test_the_text_prints_correctness_and_the_rule_s_limit(self):
        limits = {SOURCE: "labels see missed problems, not wasted depth"}
        with mock.patch.dict(judgment.LABEL_LIMITS, limits):
            printed = text(by_stage(self.connection))
        # Copilot 240a25dd2ee3: per arm, as the PR says, not one stage total.
        self.assertIn("correctness jev: 4 labelled, 3 right (75%)", printed)
        self.assertIn("correctness baseline: 1 labelled, 1 right (100%)", printed)
        self.assertIn("by confidence jev: 0.6 1/1, 0.9 1/2, none 1/1", printed)
        self.assertIn("by confidence baseline: none 1/1", printed)
        self.assertIn("outcome.example: labels see missed problems, "
                      "not wasted depth", printed)

    def test_a_stage_with_no_labels_says_so(self):
        self.write(stage="plain", answer="1")
        printed = text(by_stage(self.connection))
        self.assertIn("correctness: no labelled rows", printed)

    def test_the_json_carries_the_counts(self):
        parsed = json.loads(json_text(by_stage(self.connection)))
        entry = {e["stage"]: e for e in parsed["stages"]}["JEV_SD_REVIEW"]
        self.assertEqual(entry["arms"]["jev"]["right"], 3)
        self.assertEqual(entry["sources"], [SOURCE])


class TheLabelVerbs(JudgmentCase):
    """`sd-db.sh judgments label` and `judgments unlabelled`: the command
    surface a labeller uses instead of opening the database itself."""

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

    def test_label_then_unlabelled_then_the_report(self):
        row_id = self.write(stage="JEV_SD_REVIEW", question_id=SUBJECT,
                            answer="2", confidence=0.9)
        code, out, _ = self.cli("judgments", "unlabelled", "--stage",
                                "JEV_SD_REVIEW", "--prefix", "sd-review-tier:",
                                "--json")
        self.assertEqual(code, 0)
        self.assertEqual([row["id"] for row in json.loads(out)], [row_id])
        code, out, _ = self.cli("judgments", "label", "--row", str(row_id),
                                "--override", "3", "--source", SOURCE)
        self.assertEqual(code, 0, out)
        self.assertIn(f"labelled row {row_id}", out)
        code, out, _ = self.cli("judgments", "unlabelled", "--stage",
                                "JEV_SD_REVIEW", "--json")
        self.assertEqual(json.loads(out), [])
        code, out, _ = self.cli("judgments")
        self.assertIn("correctness jev: 1 labelled, 0 right (0%)", out)

    def test_a_changed_label_is_refused_without_replace(self):
        row_id = self.write(stage="JEV_SD_REVIEW", answer="2")
        self.cli("judgments", "label", "--row", str(row_id), "--override", "2",
                 "--source", SOURCE)
        code, _, err = self.cli("judgments", "label", "--row", str(row_id),
                                "--override", "3", "--source", SOURCE)
        self.assertEqual(code, 1)
        self.assertIn("already labelled", err)
        code, out, _ = self.cli("judgments", "label", "--row", str(row_id),
                                "--override", "3", "--source", SOURCE,
                                "--replace")
        self.assertEqual(code, 0)
        self.assertEqual(self.row(row_id)["override"], "3")

    def test_the_same_label_again_says_unchanged(self):
        row_id = self.write(stage="JEV_SD_REVIEW", answer="2")
        self.cli("judgments", "label", "--row", str(row_id), "--override", "2",
                 "--source", SOURCE)
        code, out, _ = self.cli("judgments", "label", "--row", str(row_id),
                                "--override", "2", "--source", SOURCE)
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)

    def test_a_label_without_its_three_fields_is_a_usage_error(self):
        code, _, err = self.cli("judgments", "label", "--row", "1")
        self.assertEqual(code, 1)
        self.assertIn("--row N --override NUMBER --source NAME", err)


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
