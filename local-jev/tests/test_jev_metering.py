"""What a call writes down, and what it may never write down.

The stub from `test_jev` answers, and a database of this suite's own receives
the rows, so nothing here touches the operator's store. Three promises are
checked as promises and not as comments:

* the token counts and the confidence come out of the response, and are not
  defaulted to numbers that happen to match;
* no part of what was submitted reaches the ledger, in any column or anywhere
  in the file;
* a missing or unwritable database changes neither the answer nor the exit
  code.
"""

import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

import jev
import jev_meter

from .test_jev import Stub, StubServer

# `sd_db` is installed into the environment that runs this suite in CI, the
# same install the library's own tests use. A checkout with no install reads
# the package from the folder beside this one, because a suite that skipped
# here would be a green that measured nothing -- and skips fail this repo's CI.
try:  # pragma: no cover - one branch per machine
    import sd_db  # noqa: F401
except ImportError:  # pragma: no cover - one branch per machine
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "local-sd-db"))

from sd_db.database import connect  # noqa: E402
from sd_db.judgment import by_stage  # noqa: E402
from sd_db.migrate import initialise  # noqa: E402

#: A string that appears in the question and in the state and nowhere else, so
#: a single grep of the whole database file settles whether content leaked.
SENTINEL = "zqx-quarterly-severance-terms-9f31"


class MeteringCase(StubServer):
    def setUp(self):
        super().setUp()
        self.store = Path(tempfile.mkdtemp()) / "sd.db"
        initialise(self.store)

    def env(self, **extra):
        settings = {"JEV_METER": "1", "JEV_METER_DB": str(self.store)}
        settings.update(extra)
        return super().env(**settings)

    def rows(self):
        connection = connect(self.store, write=False)
        try:
            return list(connection.execute(
                "SELECT * FROM judgment ORDER BY id"))
        finally:
            connection.close()

    def only(self):
        found = self.rows()
        self.assertEqual(len(found), 1, f"expected one row, got {len(found)}")
        return found[0]


class TheCounts(MeteringCase):
    """Fixture responses prove the extraction. The counts and the confidence
    were in every response from the first call and were dropped on the floor,
    so the failure this guards against is precisely `None`."""

    def test_the_token_counts_come_out_of_the_response(self):
        Stub.input_tokens = 412
        Stub.output_tokens = 9
        self.run_main(["noul", "is it?", "--caller", "local-adversarial-gate",
                       "--stage", "JEV_ADVERSARIAL_GATE"])
        row = self.only()
        self.assertEqual((row["tokens_in"], row["tokens_out"]), (412, 9))

    def test_a_registered_price_makes_the_row_cost_money(self):
        """The ledger prices the row from `providers.yaml` beside the database
        (sd:2358); TypeSafe's real price is the operator's to enter."""
        (self.store.parent / "providers.yaml").write_text(
            "bills:\n  usage: { cost: usage }\n"
            "providers:\n  typesafe: { url: \"https://api.example.test/v1/systemone\",\n"
            "    vendor: typesafe, bill: usage, roles: [], price: { in: 2, out: 10 } }\n"
            "roles:\n  author: []\n", encoding="utf-8")
        Stub.input_tokens = 500_000
        Stub.output_tokens = 1_000
        self.run_main(["noul", "is it?"])
        row = self.only()
        self.assertIsNotNone(row["usd"])
        self.assertAlmostEqual(row["usd"], 1.0 + 0.01)

    def test_the_confidence_comes_out_of_the_response(self):
        Stub.confidence = 0.62
        self.run_main(["score", "how urgent?", "--levels", "low,high"])
        self.assertAlmostEqual(self.only()["confidence"], 0.62)

    def test_a_choice_records_which_criterion_won_and_not_its_name(self):
        """The position, counting from 1, into the criteria the caller passed.

        A criterion key is text the caller wrote, so it can be a path or a
        subject; the caller already holds the list, so a position is the same
        fact without the string.
        """
        Stub.confidence = 0.91
        Stub.choice_value = "phone"
        self.run_main(["choice", "which route?", "--criteria", "desk,phone"])
        row = self.only()
        self.assertEqual(row["answer"], "2")
        self.assertAlmostEqual(row["confidence"], 0.91)

    def test_a_choice_the_model_invented_is_a_position_into_nothing(self):
        """A key that is not one of the caller's criteria is not a position,
        and is dropped rather than stored under a number it does not have."""
        Stub.choice_value = "somewhere-else"
        self.run_main(["choice", "which route?", "--criteria", "desk,phone"])
        self.assertIsNone(self.only()["answer"])

    def test_a_noul_records_the_probability_and_no_confidence(self):
        """A noul is the probability. There is no second number beside it, and
        inventing one would put a value in the report that nothing measured."""
        Stub.noul_value = 0.83
        self.run_main(["noul", "is it?"])
        row = self.only()
        self.assertEqual(row["answer"], "0.83")
        self.assertIsNone(row["confidence"])

    def test_the_model_and_the_duration_are_recorded(self):
        self.run_main(["noul", "is it?"])
        row = self.only()
        self.assertEqual(row["model"], "jev-stub")
        self.assertIsNotNone(row["duration_ms"])
        self.assertGreaterEqual(row["duration_ms"], 0)

    def test_a_response_with_no_usage_records_no_counts_rather_than_zero(self):
        Stub.input_tokens = "many"
        self.run_main(["noul", "is it?"])
        row = self.only()
        self.assertIsNone(row["tokens_in"])
        self.assertEqual(row["tokens_out"], 1)

    def test_a_batch_records_its_size_and_no_single_judgment(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('{"a": {"type": "noul", "instructions": "x"}, '
                     '"b": {"type": "noul", "instructions": "y"}}')
        self.addCleanup(os.unlink, fh.name)
        self.run_main(["ask", "--questions", fh.name, "--state", fh.name])
        row = self.only()
        self.assertEqual((row["primitive"], row["questions"]), ("ask", 2))
        self.assertIsNone(row["answer"])
        self.assertIsNone(row["question_id"])


class NoContent(MeteringCase):
    """The hard promise: identifiers and counts, and nothing that was
    submitted. Checked against the columns and against the bytes of the file,
    because a column-by-column check cannot see an index or a freelist page."""

    def test_the_question_and_the_state_reach_no_column(self):
        self.run_main(["noul", f"Does this mention {SENTINEL}?"],
                      stdin=f"the state also says {SENTINEL}")
        row = self.only()
        for column in row.keys():
            with self.subTest(column=column):
                self.assertNotIn(SENTINEL, str(row[column]))

    def test_the_question_and_the_state_reach_no_byte_of_the_file(self):
        self.run_main(["noul", f"Does this mention {SENTINEL}?"],
                      stdin=f"the state also says {SENTINEL}")
        found = self.store.read_bytes()
        self.assertNotIn(SENTINEL.encode(), found)
        # The stub did see it, so the test is proving a filter and not an
        # absence: a run that never sent the sentinel would pass vacuously.
        self.assertIn(SENTINEL, str(Stub.seen))

    def test_a_criterion_key_never_reaches_the_file_even_as_the_answer(self):
        """The first review of this table found the hole: a key short enough
        passed the length cap, so `/Users/someone/private.txt` stored cleanly.
        Now the answer is a position and a key cannot arrive at all."""
        self.run_main(["choice", "which route?",
                       "--criteria", f"{SENTINEL}=x,phone=elsewhere"])
        self.assertEqual(self.only()["answer"], "1")
        self.assertNotIn(SENTINEL.encode(), self.store.read_bytes())
        # The stub did see it, so this proves a filter rather than an absence.
        self.assertIn(SENTINEL, str(Stub.seen))

    def test_a_caller_that_names_itself_with_content_is_filed_under_unknown(self):
        """REGRESSION (the #489 Copilot review). The ledger refuses a name
        that is content, and a refusal loses the stage, the arm and the
        timing over the one field. `jev` drops the name first, so the
        measurement survives and the content still reaches no byte."""
        self.run_main(["noul", "is it?", "--stage", SENTINEL * 4])
        self.assertEqual(self.only()["stage"], "unknown")
        self.assertNotIn(SENTINEL.encode(), self.store.read_bytes())

    def test_a_question_id_that_is_a_subject_line_is_dropped(self):
        """`--id` is caller-controlled and a length cap passes a short
        subject. The grammar is the guarantee: the row keeps everything
        else."""
        self.run_main(["noul", "is it?", "--id", f"the {SENTINEL} numbers",
                       "--caller", "local-notify", "--stage", "JEV_NOTIFY"])
        row = self.only()
        self.assertIsNone(row["question_id"])
        self.assertEqual((row["caller"], row["stage"], row["outcome"]),
                         ("local-notify", "JEV_NOTIFY", "ok"))
        self.assertNotIn(SENTINEL.encode(), self.store.read_bytes())

    def test_a_stage_that_is_a_path_reaches_no_byte_of_the_file(self):
        self.run_main(["noul", "is it?", "--stage", f"/Users/someone/{SENTINEL}.txt"])
        self.assertEqual(self.only()["stage"], "unknown")
        self.assertNotIn(SENTINEL.encode(), self.store.read_bytes())

    def test_an_identifier_shaped_id_is_kept(self):
        """The filter has to pass what the callers actually record, or it is
        a filter that drops every measurement."""
        self.run_main(["noul", "is it?", "--id", "notify-route"])
        self.assertEqual(self.only()["question_id"], "notify-route")


class TheSubject(MeteringCase):
    """`--subject` names the judged thing for the ledger and never leaves the
    machine (sd:2107). `--id` is a key of the request body, so a repository
    name in it would reach the endpoint; the subject goes to the row alone."""

    SUBJECT = "sd-review-tier:example.widgets:0123456789ab"

    def test_the_subject_is_the_row_s_question_id_and_not_in_the_payload(self):
        Stub.choice_value = "deep"
        self.run_main(["choice", "how deep?", "--criteria", "cheap,deep",
                       "--id", "sd-review-tier", "--subject", self.SUBJECT,
                       "--stage", "JEV_SD_REVIEW"])
        self.assertEqual(self.only()["question_id"], self.SUBJECT)
        sent = json.dumps(Stub.seen[-1]["payload"])
        self.assertNotIn("example.widgets", sent)
        self.assertIn("sd-review-tier", Stub.seen[-1]["payload"]["questions"])

    def test_without_a_subject_the_id_is_the_question_id(self):
        self.run_main(["noul", "is it?", "--id", "notify-route"])
        self.assertEqual(self.only()["question_id"], "notify-route")

    def test_a_subject_that_is_not_an_identifier_is_dropped(self):
        self.run_main(["noul", "is it?", "--id", "notify-route",
                       "--subject", f"/srv/example.test/{SENTINEL}"])
        self.assertIsNone(self.only()["question_id"])
        self.assertNotIn(SENTINEL.encode(), self.store.read_bytes())

    def test_a_fallback_row_carries_the_subject(self):
        """The declined arm is the one a labeller most needs to compare."""
        self.run_main(["noul", "is it?", "--subject", self.SUBJECT,
                       "--fallback", "0.5"], TYPESAFE_API_KEY="")
        self.assertEqual(self.only()["question_id"], self.SUBJECT)

    def test_a_shadow_pair_carries_the_subject_on_both_arms(self):
        self.run_main(["noul", "is it?", "--subject", self.SUBJECT,
                       "--shadow", "1"])
        self.assertEqual([row["question_id"] for row in self.rows()],
                         [self.SUBJECT, self.SUBJECT])

    def test_record_takes_a_subject(self):
        self.run_main(["record", "--caller", "sd-review", "--stage",
                       "JEV_SD_REVIEW", "--answer", "2", "--subject",
                       self.SUBJECT])
        self.assertEqual(self.only()["question_id"], self.SUBJECT)


class WhenTheStoreIsGone(MeteringCase):
    """A missing or unwritable database degrades to no recording, never to a
    failed or slowed judgment."""

    def baseline(self):
        return self.run_verbose(["noul", "is it?"], JEV_METER="0")

    def test_a_missing_database_changes_neither_the_answer_nor_the_exit_code(self):
        missing = Path(tempfile.mkdtemp()) / "not-here" / "sd.db"
        self.assertEqual(self.run_verbose(["noul", "is it?"],
                                          JEV_METER_DB=str(missing)),
                         self.baseline())

    def test_an_unwritable_database_changes_neither_the_answer_nor_the_code(self):
        self.store.chmod(stat.S_IRUSR)
        self.addCleanup(self.store.chmod, stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(self.run_verbose(["noul", "is it?"]), self.baseline())

    def test_a_database_that_is_a_directory_is_no_recording_and_no_failure(self):
        folder = Path(tempfile.mkdtemp())
        self.assertEqual(self.run_verbose(["noul", "is it?"],
                                          JEV_METER_DB=str(folder)),
                         self.baseline())

    def test_the_recorder_says_what_it_did_and_never_raises(self):
        folder = Path(tempfile.mkdtemp())
        # A directory is not a database. SQLite reports the failed open as
        # `unable to open database file`, an OperationalError, so the word is
        # the busy one; either way nothing is recorded and nothing raises.
        self.assertIn(
            jev_meter.record({"caller": "c"}, {"JEV_METER_DB": str(folder)}),
            (jev_meter.NO_STORE, jev_meter.CONTENDED),
        )
        self.assertEqual(jev_meter.record({}, {"JEV_METER": "off"}),
                         jev_meter.SWITCHED_OFF)

    def test_a_refused_row_is_not_a_failed_call(self):
        """A stage over the cap no longer reaches the ledger -- `named` drops
        it and the row is filed under `unknown` -- so the refusal is driven
        through `--provider`, which is deliberately free metadata and is the
        one name this tool does not filter."""
        code, out = self.run_main(
            ["record", "--caller", "c", "--stage", "s",
             "--provider", "p" * 200, "--outcome", "ok"])
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.rows(), [])

    def test_a_judgment_still_answers_when_its_own_row_is_refused(self):
        Stub.noul_value = 0.5
        code, out = self.run_main(["noul", "is it?", "--stage", "s" * 200])
        self.assertEqual((code, out), (0, "0.5\n"))
        self.assertEqual(self.only()["stage"], "unknown")


class TheOutcomes(MeteringCase):
    def test_an_answered_call_is_ok(self):
        self.run_main(["noul", "is it?"])
        row = self.only()
        self.assertEqual((row["outcome"], row["cause"]), ("ok", None))

    def test_a_switched_off_machine_with_a_fallback_is_a_fallback(self):
        self.write_switch("off")
        code, out = self.run_main(["noul", "is it?", "--fallback", "yes"])
        self.assertEqual((code, out), (0, "yes\n"))
        row = self.only()
        self.assertEqual((row["outcome"], row["cause"]),
                         ("fallback", "switched-off"))

    def test_a_declined_judgment_writes_an_event_rather_than_silence(self):
        self.write_switch("off")
        self.run_main(["noul", "is it?", "--caller", "local-notify",
                       "--stage", "JEV_NOTIFY"])
        row = self.only()
        self.assertEqual(
            (row["caller"], row["stage"], row["outcome"], row["cause"]),
            ("local-notify", "JEV_NOTIFY", "unavailable", "switched-off"),
        )

    def test_an_unkeyed_machine_says_unkeyed_and_not_switched_off(self):
        self.run_main(["noul", "is it?", "--fallback", "yes"],
                      TYPESAFE_API_KEY="")
        self.assertEqual(self.only()["cause"], "unkeyed")

    def test_a_placeholder_key_is_unkeyed_too(self):
        self.run_main(["noul", "is it?", "--fallback", "yes"],
                      TYPESAFE_API_KEY="change-me")
        self.assertEqual(self.only()["cause"], "unkeyed")

    def test_a_response_with_no_answer_is_invalid_and_not_unavailable(self):
        Stub.drop_answers = True
        self.run_main(["noul", "is it?", "--fallback", "yes"])
        row = self.only()
        self.assertEqual((row["outcome"], row["cause"]), ("fallback", "invalid"))

    def test_a_body_that_is_not_json_is_invalid_and_not_unavailable(self):
        """REGRESSION (the #489 Copilot review). An endpoint that answered
        with something that is not JSON answered, so recording it as an
        outage loses the one distinction this column exists to keep."""

        class Answered:
            def read(self):
                return b"<html>maintenance</html>"

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def garbage(_request, timeout=None):
            return Answered()

        # The state defaults to stdin. Supplied here, or the call blocks on
        # whatever stdin the runner inherited (an open pipe never reaches EOF).
        with tempfile.TemporaryFile("w+") as out, \
                unittest.mock.patch.object(sys, "stdin", io.StringIO("a sentence")):
            code = jev.main(["noul", "is it?", "--fallback", "yes"], out=out,
                            env=self.env(JEV_RETRIES="0"), opener=garbage,
                            sleep=lambda _s: None)
        self.assertEqual(code, 0)
        row = self.only()
        self.assertEqual((row["outcome"], row["cause"]), ("fallback", "invalid"))

    def test_an_endpoint_that_refuses_is_unavailable(self):
        Stub.status = 500
        self.run_main(["noul", "is it?", "--fallback", "yes"], JEV_RETRIES="0")
        self.assertEqual(self.only()["cause"], "unavailable")

    def test_a_timeout_is_its_own_class(self):
        """Driven through an injected opener, because a stub that really
        hangs would put the timeout in the suite's own runtime."""

        def slow(_request, timeout=None):
            raise urllib.error.URLError(TimeoutError("timed out"))

        # The state defaults to stdin. Supplied here, or the call blocks on
        # whatever stdin the runner inherited (an open pipe never reaches EOF).
        with tempfile.TemporaryFile("w+") as out, \
                unittest.mock.patch.object(sys, "stdin", io.StringIO("a sentence")):
            code = jev.main(["noul", "is it?", "--fallback", "yes"], out=out,
                            env=self.env(JEV_RETRIES="0"), opener=slow,
                            sleep=lambda _s: None)
        self.assertEqual(code, 0)
        row = self.only()
        self.assertEqual((row["outcome"], row["cause"]), ("fallback", "timeout"))

    def test_every_decline_reason_this_tool_can_see_is_its_own_value(self):
        seen = set()
        for extra, expected in (({"TYPESAFE_API_KEY": ""}, "unkeyed"),
                                ({"JEV_ENABLED": "0"}, "switched-off"),
                                ({"JEV_TIMEOUT": "soon"}, "unavailable")):
            with self.subTest(expected=expected):
                self.run_main(["noul", "is it?", "--fallback", "y"], **extra)
                seen.add(self.rows()[-1]["cause"])
        self.assertEqual(seen, {"unkeyed", "switched-off", "unavailable"})


class TheControlArm(MeteringCase):
    """The old mechanism is the control arm and is measured with the same
    fields, under the same stage key, in the same table."""

    def test_enabled_records_its_decline_when_asked(self):
        self.write_switch("off")
        code, _out = self.run_main(["enabled", "JEV_NOTIFY", "--record",
                                    "--caller", "local-notify"])
        self.assertEqual(code, 3)
        row = self.only()
        self.assertEqual(
            (row["arm"], row["caller"], row["stage"], row["outcome"],
             row["cause"], row["primitive"]),
            ("baseline", "local-notify", "JEV_NOTIFY", "fallback",
             "switched-off", "gate"),
        )

    def test_enabled_records_nothing_unless_asked(self):
        """The verb promises to cost nothing and every caller asks it on every
        run, including runs where it then does nothing at all."""
        self.write_switch("off")
        self.assertEqual(self.run_main(["enabled", "JEV_NOTIFY"])[0], 3)
        self.assertEqual(self.rows(), [])

    def test_enabled_records_nothing_when_the_answer_is_yes(self):
        self.assertEqual(self.run_main(["enabled", "JEV_NOTIFY", "--record"])[0], 0)
        self.assertEqual(self.rows(), [])

    def test_record_writes_what_the_old_path_did(self):
        code, out = self.run_main([
            "record", "--caller", "local-weekly-digest",
            "--stage", "JEV_WEEKLY_DIGEST", "--decline", "no-path",
            "--answer", "2", "--positions", "0,1,2",
            "--duration-ms", "17",
        ])
        self.assertEqual((code, out), (0, ""))
        row = self.only()
        self.assertEqual(
            (row["arm"], row["provider"], row["primitive"], row["outcome"],
             row["cause"], row["answer"], row["ordering"], row["duration_ms"]),
            ("baseline", "local", "baseline", "fallback", "no-path",
             "2", "0,1,2", 17),
        )

    def test_record_refuses_an_ordering_that_is_not_positions(self):
        self.run_main(["record", "--caller", "c", "--stage", "s",
                       "--positions", SENTINEL])
        self.assertEqual(self.rows(), [])

    def test_a_fallback_run_is_countable_against_a_judgment_on_one_stage(self):
        self.run_main(["noul", "is it?", "--caller", "local-adversarial-gate",
                       "--stage", "JEV_ADVERSARIAL_GATE"])
        self.run_main(["record", "--caller", "local-adversarial-gate",
                       "--stage", "JEV_ADVERSARIAL_GATE", "--decline", "timeout",
                       "--answer", "unknown"])
        arms = [(row["stage"], row["arm"]) for row in self.rows()]
        self.assertEqual(arms, [("JEV_ADVERSARIAL_GATE", "jev"),
                                ("JEV_ADVERSARIAL_GATE", "baseline")])

    def test_every_decline_word_the_vocabulary_holds_is_accepted(self):
        for word in jev.DECLINES:
            with self.subTest(word=word):
                self.run_main(["record", "--caller", "c", "--stage", "s",
                               "--decline", word])
        self.assertEqual(sorted({row["cause"] for row in self.rows()}),
                         sorted(jev.DECLINES))


class ShadowMode(MeteringCase):
    """Both arms on the same input, the old answer used, a paired sample
    produced, and nothing about the stage's behaviour changed."""

    def test_it_returns_the_old_answer_and_records_both(self):
        Stub.noul_value = 0.97
        code, out = self.run_main(["noul", "is it?", "--gate", "0.5",
                                   "--shadow", "no", "--shadow-ms", "4",
                                   "--caller", "local-notify",
                                   "--stage", "JEV_NOTIFY"])
        self.assertEqual((code, out), (0, "no\n"))
        judged, baseline = self.rows()
        self.assertEqual((judged["arm"], judged["answer"], judged["shadow"]),
                         ("jev", "0.97", 1))
        # `no` is the word `--gate` prints, so it is stored as its side of
        # 0.5; other text of the caller's own is not stored (below).
        self.assertEqual(
            (baseline["arm"], baseline["answer"], baseline["shadow"],
             baseline["duration_ms"], baseline["provider"]),
            ("baseline", "0", 1, 4, "local"),
        )
        self.assertEqual(judged["changed"], "yes")
        self.assertEqual(judged["pair"], baseline["pair"])
        self.assertIsNotNone(judged["pair"])

    def test_the_pair_carries_the_delta_between_the_two_answers(self):
        Stub.noul_value = 0.97
        self.run_main(["noul", "is it?", "--gate", "0.5", "--shadow", "no"])
        self.assertEqual(self.rows()[0]["changed"], "yes")
        self.run_main(["noul", "is it?", "--gate", "0.5", "--shadow", "yes"])
        self.assertEqual(self.rows()[2]["changed"], "no")

    def test_a_failing_call_still_returns_the_old_answer_and_exits_zero(self):
        Stub.status = 500
        code, out = self.run_main(["noul", "is it?", "--shadow", "keep"],
                                  JEV_RETRIES="0")
        self.assertEqual((code, out), (0, "keep\n"))
        judged, baseline = self.rows()
        self.assertEqual(judged["cause"], "unavailable")
        self.assertIsNone(baseline["answer"])
        self.assertEqual(baseline["arm"], "baseline")

    def test_shadow_is_off_unless_asked(self):
        self.run_main(["noul", "is it?"])
        self.assertEqual(self.only()["shadow"], 0)

    def test_shadow_refuses_to_share_a_call_with_json(self):
        code, out = self.run_main(["noul", "is it?", "--shadow", "no", "--json"])
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(self.rows(), [])

    def test_a_shadow_choice_records_the_old_answer_as_a_position(self):
        """The baseline row writer is shared with live mode, so shadow mode
        gains the same shape: which criterion won, never its name."""
        self.run_main(["choice", "which route?", "--criteria", "desk,phone",
                       "--shadow", "phone"])
        judged, baseline = self.rows()
        self.assertEqual((judged["answer"], baseline["answer"]), ("1", "2"))


class LivePaired(MeteringCase):
    """sd:2357. `--baseline` hands over the caller's own answer on a live
    call: the judgment is printed and used as before, and the pair is
    written. Shadow mode measures a judgment nobody uses; this measures the
    one the caller does."""

    def test_it_prints_the_judgment_and_records_both_arms(self):
        Stub.noul_value = 0.97
        code, out = self.run_main(["noul", "is it?", "--gate", "0.5",
                                   "--baseline", "no", "--baseline-ms", "6",
                                   "--caller", "local-notify",
                                   "--stage", "JEV_NOTIFY"])
        self.assertEqual((code, out), (0, "yes\n"))
        judged, baseline = self.rows()
        self.assertEqual((judged["arm"], judged["shadow"], judged["answer"],
                          judged["changed"]), ("jev", 0, "0.97", "yes"))
        self.assertEqual(
            (baseline["arm"], baseline["shadow"], baseline["primitive"],
             baseline["provider"], baseline["outcome"], baseline["duration_ms"],
             baseline["stage"], baseline["changed"]),
            ("baseline", 0, "baseline", jev.BASELINE_PROVIDER, "ok", 6,
             "JEV_NOTIFY", "no"),
        )
        self.assertIsNotNone(judged["pair"])
        self.assertEqual(judged["pair"], baseline["pair"])

    def test_changed_compares_with_the_baseline_and_not_the_fallback(self):
        """sd-review passes a sentinel token as `--fallback` that no tier can
        be, so a fallback comparison said `yes` on every row. The baseline is
        the caller's real answer and is what the judgment is compared with."""
        Stub.noul_value = 0.97
        self.run_main(["noul", "is it?", "--gate", "0.5",
                       "--fallback", "SENTINEL-NOT-A-TIER", "--baseline", "yes"])
        self.assertEqual(self.rows()[0]["changed"], "no")

    def test_a_baseline_under_a_gate_other_than_a_half_is_no_number(self):
        """`judgments compare` reads every noul at 0.5. Under `--gate 0.8` a
        0.7 prints `no`; the caller's `no` stored as 0 would then read as a
        disagreement the caller never saw. The row is kept without a number,
        so the pair is counted and not compared, and `changed` still says
        what the printed words did."""
        Stub.noul_value = 0.7
        for flag, printed in (("--baseline", "no\n"), ("--shadow", "no\n")):
            with self.subTest(flag=flag):
                code, out = self.run_main(["noul", "is it?", "--gate", "0.8",
                                           flag, "no"])
                self.assertEqual((code, out), (0, printed))
                judged, baseline = self.rows()[-2:]
                self.assertEqual((judged["answer"], judged["changed"]),
                                 ("0.7", "no"))
                self.assertEqual(baseline["arm"], "baseline")
                self.assertIsNone(baseline["answer"])
        # A number of the caller's own has the same problem: 0.6 is `no` at
        # 0.8 and would be read as yes.
        self.run_main(["noul", "is it?", "--gate", "0.8", "--baseline", "0.6"])
        self.assertIsNone(self.rows()[-1]["answer"])

    def test_a_baseline_under_unsure_below_is_no_position(self):
        """A choice printed `unsure` is not the chosen key the report reads,
        so a gated choice is counted and not compared, like a gated noul."""
        Stub.choice_value, Stub.confidence = "desk", 0.1
        # Live prints the gated judgment; shadow prints the caller's own key.
        for flag, printed in (("--baseline", "unsure\n"), ("--shadow", "desk\n")):
            with self.subTest(flag=flag):
                code, out = self.run_main(["choice", "where?", "--criteria", "desk,phone",
                                           "--unsure-below", "0.5", flag, "desk"])
                self.assertEqual((code, out), (0, printed))
                judged, baseline = self.rows()[-2:]
                self.assertEqual(judged["changed"], "yes")
                self.assertEqual(baseline["arm"], "baseline")
                self.assertIsNone(baseline["answer"])

    def test_a_numeric_baseline_is_compared_as_a_number(self):
        """`score` prints `2.0`; a caller's own scale says `2`. They agree."""
        Stub.score_value = 2.0
        self.run_main(["score", "how urgent?", "--levels", "low,mid,high",
                       "--baseline", "2"])
        judged, baseline = self.rows()
        self.assertEqual((judged["changed"], baseline["answer"]), ("no", "2"))

    def test_a_choice_key_that_looks_like_a_number_is_compared_as_text(self):
        """Criteria keys are text the caller wrote: `2` and `2.0` are two
        routes, so a judgment of one against a baseline of the other changed
        what the caller did. Only `score` and `noul` compare as numbers."""
        Stub.choice_value = "2"
        self.run_main(["choice", "which?", "--criteria", "2,2.0",
                       "--baseline", "2.0"])
        judged, baseline = self.rows()
        self.assertEqual((judged["answer"], baseline["answer"], judged["changed"]),
                         ("1", "2", "yes"))

    def test_a_choice_baseline_is_stored_as_its_position(self):
        Stub.choice_value = "desk"
        code, out = self.run_main(["choice", "which route?",
                                   "--criteria", "desk,phone",
                                   "--baseline", "phone"])
        self.assertEqual((code, out), (0, "desk\n"))
        judged, baseline = self.rows()
        self.assertEqual((judged["answer"], baseline["answer"]), ("1", "2"))
        self.assertEqual(judged["changed"], "yes")

    def test_a_choice_baseline_is_positioned_when_jev_never_ran(self):
        """Switched off, the criteria were never parsed by the verb; the
        baseline is still a position into them, not a dropped answer."""
        self.write_switch("off")
        self.run_main(["choice", "which route?", "--criteria", "desk,phone",
                       "--fallback", "phone", "--baseline", "phone"])
        judged, baseline = self.rows()
        self.assertEqual((judged["outcome"], baseline["answer"]), ("fallback", "2"))

    def test_a_declined_choice_never_reads_stdin_for_its_baseline(self):
        """Criteria on stdin were never read when the call was declined, and
        reading them after the answer is printed can block a caller whose
        stdin is still open. The baseline answer is dropped instead."""
        self.write_switch("off")
        with unittest.mock.patch.object(jev, "read_source",
                                        return_value='{"a": null}') as read:
            code, out = self.run_main(["choice", "which?", "--criteria", "@-",
                                       "--fallback", "a", "--baseline", "a"])
        self.assertEqual((code, out), (0, "a\n"))
        self.assertEqual(read.call_count, 0)
        judged, baseline = self.rows()
        self.assertIsNone(baseline["answer"])

    def test_a_baseline_that_is_not_a_number_keeps_the_row(self):
        Stub.choice_value = "desk"
        self.run_main(["choice", "which route?", "--criteria", "desk,phone",
                       "--baseline", "somewhere-else"])
        judged, baseline = self.rows()
        self.assertEqual(baseline["arm"], "baseline")
        self.assertIsNone(baseline["answer"])
        self.assertEqual(judged["changed"], "yes")

    def test_the_text_of_a_baseline_reaches_no_byte_of_the_file(self):
        self.run_main(["noul", "is it?", "--gate", "0.5", "--baseline", SENTINEL])
        self.assertEqual(len(self.rows()), 2)
        self.assertNotIn(SENTINEL.encode(), self.store.read_bytes())

    def test_a_batch_takes_a_baseline_too(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('{"a": {"type": "noul", "instructions": "x"}}')
        self.addCleanup(os.unlink, fh.name)
        self.run_main(["ask", "--questions", fh.name, "--state", fh.name,
                       "--baseline", "1"])
        judged, baseline = self.rows()
        self.assertEqual((judged["arm"], baseline["arm"]), ("jev", "baseline"))
        self.assertEqual(judged["pair"], baseline["pair"])

    def test_the_stage_report_counts_it_as_a_paired_sample(self):
        self.run_main(["noul", "is it?", "--baseline", "0.4",
                       "--stage", "JEV_NOTIFY"])
        connection = connect(self.store, write=False)
        try:
            stage, = by_stage(connection)
        finally:
            connection.close()
        self.assertEqual((stage["stage"], stage["paired"]), ("JEV_NOTIFY", 1))
        self.assertEqual(stage["arms"]["jev"]["shadow"], 0)

    def test_a_failed_call_still_writes_both_arms(self):
        Stub.status = 500
        self.run_main(["noul", "is it?", "--baseline", "0.4"], JEV_RETRIES="0")
        judged, baseline = self.rows()
        self.assertEqual((judged["cause"], judged["changed"]),
                         ("unavailable", "unknown"))
        self.assertEqual(baseline["answer"], "0.4")

    def test_baseline_is_off_unless_asked(self):
        self.run_main(["noul", "is it?"])
        self.assertIsNone(self.only()["pair"])

    def test_baseline_and_shadow_together_are_refused(self):
        """Two answers to which one is printed. Refused before anything is
        sent, written or printed, the way `--shadow --json` is."""
        code, out, err = self.run_verbose(["noul", "is it?", "--shadow", "no",
                                           "--baseline", "no"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--baseline", err)
        self.assertEqual(self.rows(), [])
        self.assertEqual(Stub.seen, [])


class TheBaselineChangesNothingACallerSees(MeteringCase):
    """The contract: `--baseline` adds two rows and nothing else. Every
    path a caller can take prints the same bytes, on both streams, and exits
    with the same code, with the flag and without it."""

    def same(self, argv, **extra):
        without = self.run_verbose(argv, **extra)
        with_it = self.run_verbose(argv + ["--baseline", "0.4",
                                           "--baseline-ms", "3"], **extra)
        self.assertEqual(with_it, without, argv)

    def test_an_answered_call(self):
        self.same(["noul", "is it?"])
        self.same(["noul", "is it?", "--gate", "0.5"])
        self.same(["choice", "which?", "--criteria", "a,b"])
        self.same(["choice", "which?", "--criteria", "a,b", "--unsure-below", "2"])
        self.same(["score", "how?", "--levels", "low,high", "--json"])

    def test_a_failing_call_with_and_without_a_fallback(self):
        Stub.status = 500
        self.same(["noul", "is it?"], JEV_RETRIES="0")
        self.same(["noul", "is it?", "--fallback", "keep"], JEV_RETRIES="0")

    def test_a_switched_off_machine_with_and_without_a_fallback(self):
        self.write_switch("off")
        self.same(["noul", "is it?"])
        self.same(["noul", "is it?", "--fallback", "keep"])

    def test_an_unkeyed_machine(self):
        self.same(["noul", "is it?", "--fallback", "keep"], TYPESAFE_API_KEY="")

    def test_a_meter_that_is_off_or_has_no_store(self):
        self.same(["noul", "is it?"], JEV_METER="0")
        missing = Path(tempfile.mkdtemp()) / "not-here" / "sd.db"
        self.same(["noul", "is it?"], JEV_METER_DB=str(missing))


class TheShadowSwitch(MeteringCase):
    """sd:2761: with `jev shadow on`, every caller runs its old mechanism and
    the ledger still gets the judgment, paired with the caller's own answer."""

    def switch_on(self):
        Path(self.switch).with_name("shadow").write_text("on\n")

    def test_a_fallback_is_printed_and_both_answers_are_paired(self):
        self.switch_on()
        code, out = self.run_main(["score", "how urgent?", "--levels", "a,b,c",
                                   "--fallback", "1", "--stage", "JEV_NOTIFY"])
        self.assertEqual((code, out), (0, "1\n"))
        self.assertTrue(Stub.seen, "the switch must still ask Jev")
        judged, baseline = self.rows()
        self.assertEqual((judged["arm"], judged["answer"], judged["shadow"]),
                         ("jev", "2.0", 1))
        # The fallback is a marker, not the caller's old answer: the row is
        # paired and carries no number, so `judgments compare` counts the
        # pair and does not compare it.
        self.assertEqual((baseline["arm"], baseline["answer"], baseline["shadow"]),
                         ("baseline", None, 1))
        self.assertIsNotNone(judged["pair"])
        self.assertEqual(judged["pair"], baseline["pair"])
        # The caller does not know the switch is on, so the row cannot say
        # whether the judgment would have changed what it did.
        self.assertEqual(judged["changed"], "unknown")

    def test_a_switched_fallback_is_never_an_old_answer(self):
        """A fallback that happens to read as a number, a yes or no, or one
        of the criteria is still a marker (`tests/test_jev_contract.py`), and
        only `--baseline` is the pair's old answer."""
        self.switch_on()
        for argv in (["noul", "q", "--gate", "0.5", "--fallback", "yes"],
                     ["choice", "which?", "--criteria", "desk,phone",
                      "--fallback", "desk"]):
            with self.subTest(verb=argv[0]):
                self.assertEqual(self.run_main(argv)[0], 0)
                judged, baseline = self.rows()[-2:]
                self.assertEqual((judged["changed"], baseline["answer"]),
                                 ("unknown", None))

    def test_off_a_fallback_call_is_unchanged(self):
        code, out = self.run_main(["score", "how urgent?", "--levels", "a,b,c",
                                   "--fallback", "1"])
        self.assertEqual((code, out), (0, "2.0\n"))
        row = self.only()
        self.assertEqual((row["shadow"], row["pair"]), (0, None))

    def test_the_variable_beats_the_file_both_ways(self):
        self.switch_on()
        self.assertEqual(self.run_main(["noul", "q", "--fallback", "keep"],
                                       JEV_SHADOW="0")[1], "0.97\n")
        Path(self.switch).with_name("shadow").unlink()
        self.assertEqual(self.run_main(["noul", "q", "--fallback", "keep"],
                                       JEV_SHADOW="1")[1], "keep\n")

    def test_a_call_with_no_fallback_is_answered_as_if_jev_were_down(self):
        self.switch_on()
        code, out, err = self.run_verbose(["noul", "q", "--gate", "0.5"])
        self.assertEqual((code, out), (3, ""))
        self.assertIn("shadow on", err)
        self.assertTrue(Stub.seen)
        row = self.only()
        self.assertEqual((row["answer"], row["shadow"], row["outcome"]),
                         ("0.97", 1, "ok"))
        self.assertIsNotNone(row["pair"])

    def test_a_fallback_with_json_still_prints_the_fallback(self):
        self.switch_on()
        self.assertEqual(self.run_main(["noul", "q", "--json", "--fallback", "keep"]),
                         (0, "keep\n"))

    def test_an_explicit_shadow_is_unchanged(self):
        self.switch_on()
        code, out = self.run_main(["noul", "q", "--gate", "0.5", "--shadow", "yes",
                                   "--fallback", "keep"])
        self.assertEqual((code, out), (0, "yes\n"))
        self.assertEqual(self.rows()[0]["changed"], "no")

    def test_a_baseline_is_the_old_answer_of_the_pair(self):
        """sd:2357 under the switch: the fallback is printed, and `--baseline`
        is what the judgment is compared with and the pair's old answer."""
        self.switch_on()
        code, out = self.run_main(["noul", "q", "--gate", "0.5", "--fallback", "keep",
                                   "--baseline", "no", "--stage", "JEV_NOTIFY"])
        self.assertEqual((code, out), (0, "keep\n"))
        judged, baseline = self.rows()
        self.assertEqual((judged["changed"], baseline["answer"], baseline["shadow"]),
                         ("yes", "0", 1))
        self.assertEqual(judged["pair"], baseline["pair"])

    def test_a_baseline_with_no_fallback_exits_3_and_is_recorded(self):
        self.switch_on()
        code, out = self.run_main(["noul", "q", "--gate", "0.5", "--baseline", "yes"])
        self.assertEqual((code, out), (3, ""))
        judged, baseline = self.rows()
        self.assertEqual((judged["changed"], baseline["answer"]), ("no", "1"))

    def test_no_shadow_notice_when_nothing_is_sent(self):
        self.switch_on()
        self.write_switch("off")
        code, out, err = self.run_verbose(["noul", "q", "--fallback", "keep"])
        self.assertEqual((code, out), (0, "keep\n"))
        self.assertNotIn("shadow on", err)
        self.assertEqual(Stub.seen, [])


class WhoAsked(MeteringCase):
    def test_the_flags_name_the_caller_and_the_stage(self):
        self.run_main(["noul", "is it?", "--caller", "local-sd-plan",
                       "--stage", "JEV_SD_PLAN"])
        row = self.only()
        self.assertEqual((row["caller"], row["stage"]),
                         ("local-sd-plan", "JEV_SD_PLAN"))

    def test_the_environment_names_them_for_a_caller_that_is_a_shell(self):
        self.run_main(["noul", "is it?"], JEV_CALLER="local-notify",
                      JEV_STAGE="JEV_NOTIFY")
        row = self.only()
        self.assertEqual((row["caller"], row["stage"]),
                         ("local-notify", "JEV_NOTIFY"))

    def test_a_caller_that_names_nothing_is_recorded_as_unknown(self):
        self.run_main(["noul", "is it?"])
        row = self.only()
        self.assertEqual((row["caller"], row["stage"]), ("unknown", "unknown"))
        self.assertEqual(row["provider"], "typesafe")

    def test_switching_the_meter_off_records_nothing(self):
        self.assertEqual(self.run_main(["noul", "is it?"], JEV_METER="0")[1],
                         "0.97\n")
        self.assertEqual(self.rows(), [])


class DidItChangeAnything(MeteringCase):
    def test_a_fallback_the_judgment_agrees_with_changed_nothing(self):
        Stub.noul_value = 0.97
        self.run_main(["noul", "is it?", "--gate", "0.5", "--fallback", "yes"])
        self.assertEqual(self.only()["changed"], "no")

    def test_a_fallback_the_judgment_disagrees_with_changed_something(self):
        Stub.noul_value = 0.97
        self.run_main(["noul", "is it?", "--gate", "0.5", "--fallback", "no"])
        self.assertEqual(self.only()["changed"], "yes")

    def test_a_caller_that_hands_over_nothing_is_recorded_as_unknown(self):
        self.run_main(["noul", "is it?"])
        self.assertEqual(self.only()["changed"], "unknown")

    def test_a_caller_that_knows_may_say_so(self):
        self.run_main(["noul", "is it?", "--changed", "yes"])
        self.assertEqual(self.only()["changed"], "yes")

    def test_a_taken_fallback_changed_nothing_because_nothing_was_used(self):
        self.write_switch("off")
        self.run_main(["noul", "is it?", "--fallback", "yes"])
        self.assertEqual(self.only()["changed"], "no")


class OneDecisionIsOneDecision(MeteringCase):
    """`enabled --record` then `record` is one decision, not two.

    Both rows are the control arm on the same stage, and both used to count
    as a call and as a decline with nothing tying them together, so a single
    decline read as two. The gate row is a gate event now, which the decision
    aggregates leave out and count on their own.
    """

    def stage(self):
        connection = connect(self.store)
        self.addCleanup(connection.close)
        return {entry["stage"]: entry
                for entry in by_stage(connection)}["JEV_NOTIFY"]

    def test_a_decline_and_the_decision_behind_it_count_once_each(self):
        self.write_switch("off")
        self.run_main(["enabled", "JEV_NOTIFY", "--record",
                       "--caller", "local-notify"])
        self.run_main(["record", "--caller", "local-notify",
                       "--stage", "JEV_NOTIFY", "--decline", "switched-off"])
        self.assertEqual(len(self.rows()), 2)
        entry = self.stage()
        self.assertEqual(entry["arms"]["baseline"]["calls"], 1)
        self.assertEqual(entry["declines"], {"switched-off": 1})
        self.assertEqual(entry["gates"], {"switched-off": 1})

    def test_a_caller_that_only_declines_still_has_a_number(self):
        """The gate row is the only fact those stages ever had, and leaving it
        out of the decision counts must not leave the stage out."""
        self.write_switch("off")
        self.run_main(["enabled", "JEV_NOTIFY", "--record",
                       "--caller", "local-notify"])
        entry = self.stage()
        self.assertEqual(entry["gates"], {"switched-off": 1})
        self.assertEqual(entry["arms"], {})


class TheCauseIsWrittenOnce(MeteringCase):
    """A failed call and the old path that followed it are one decline.

    `OneDecisionIsOneDecision` above covers the gate: no call was made, so
    the gate row carries the cause and `GATE_PRIMITIVE` keeps it out of the
    decision counts. This is the other half, and it was found in review of
    the caller wiring. When a call *is* made and fails, `flush` writes the
    cause on the judgment's own row. The caller then records that its old
    mechanism ran -- and `DECLINES` groups by stage and cause across both
    arms, with no deduplication, so a cause named twice is a decline counted
    twice.

    The fix is on the writer and not on the aggregate: the caller's row says
    the control arm completed, which nothing else records, and says nothing
    about why, which something else already did. Changing `DECLINES` instead
    would change what every row already in the ledger means.
    """

    def ask_and_fail(self):
        """One `ask` that is made, fails at the endpoint, and is recorded.

        A real call and not a synthesised row: what is being checked is that
        `flush` puts the cause on this row, so a test that wrote the row
        itself would assert its own fixture.
        """
        Stub.status = 500
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as state:
            state.write(json.dumps({"subject": "a thread"}))
        self.addCleanup(os.unlink, state.name)
        self.run_main(
            ["ask", "--questions", "-", "--state", state.name,
             "--state-format", "json", "--fallback", "{}",
             "--caller", "local-sd-plan", "--stage", "JEV_SD_PLAN"],
            stdin=json.dumps({"q1": {"type": "noul", "instructions": "is it?"}}),
            JEV_RETRIES="0",
        )

    def stage(self):
        connection = connect(self.store)
        self.addCleanup(connection.close)
        return {entry["stage"]: entry
                for entry in by_stage(connection)}["JEV_SD_PLAN"]

    def test_a_failed_ask_and_the_old_path_behind_it_are_one_decline(self):
        self.ask_and_fail()
        cause = self.rows()[0]["cause"]
        self.assertIsNotNone(cause, "the failed ask recorded no cause at all, "
                                    "so this measures nothing")
        # What every wired caller writes on the branch its own mechanism took.
        self.run_main(["record", "--caller", "local-sd-plan",
                       "--stage", "JEV_SD_PLAN", "--arm", "baseline",
                       "--outcome", "ok"])
        entry = self.stage()
        self.assertEqual(entry["declines"], {cause: 1})
        # And the row still says the control arm ran, which is its whole job.
        self.assertEqual(entry["arms"]["baseline"]["calls"], 1)
        self.assertEqual(entry["arms"]["baseline"]["ok"], 1)

    def test_repeating_the_cause_on_that_row_is_what_made_it_two(self):
        """The defect, pinned, so the rule banning `--decline` has a number.

        `tests/test_jev_contract.py` refuses a `--decline` from any caller.
        This is why: the same decision, recorded with one, counts twice --
        and it is `DECLINES` doing the counting, so no caller can see it.
        """
        self.ask_and_fail()
        cause = self.rows()[0]["cause"]
        self.run_main(["record", "--caller", "local-sd-plan",
                       "--stage", "JEV_SD_PLAN", "--arm", "baseline",
                       "--outcome", "ok", "--decline", cause])
        self.assertEqual(self.stage()["declines"], {cause: 2})


class TheMeterWaitsOnlyBriefly(MeteringCase):
    """Metering runs after the answer is printed, but a subprocess caller
    waits for this process to exit, so a lock it waited on would be time
    added to a decision that already finished. The wait is bounded, and short
    enough that the ordinary contention of a busy machine still lands."""

    EVENT = {"caller": "c", "stage": "s", "provider": "p",
             "primitive": "noul", "outcome": "ok"}

    def hold(self):
        holder = connect(self.store)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        return holder

    def test_a_locked_ledger_drops_the_measurement_after_the_bound(self):
        import time

        holder = self.hold()
        started = time.monotonic()
        said = jev_meter.record(dict(self.EVENT), {"JEV_METER_DB": str(self.store),
                                                   "JEV_METER_BUSY_MS": "100"})
        waited = time.monotonic() - started
        holder.execute("ROLLBACK")
        self.assertEqual(said, jev_meter.CONTENDED)
        # The ceiling is what the bound replaced, sd_db's own five seconds,
        # less headroom. A tight wall-clock ceiling failed the gate at load 23
        # (1.23 s against 1.0 s for a 250 ms bound); forty times the bound
        # still tells a bounded wait from the library's.
        self.assertLess(waited, 4.0, f"waited {waited:.2f}s on a locked ledger")

    def test_a_lock_released_inside_the_bound_still_writes_the_row(self):
        """The case the bound exists for: a zero wait dropped this row."""
        import time

        locked = threading.Event()

        def hold_briefly():
            # SQLite connections stay in the thread that opened them.
            holder = connect(self.store)
            try:
                holder.execute("BEGIN IMMEDIATE")
                locked.set()
                time.sleep(0.05)
                holder.execute("ROLLBACK")
            finally:
                holder.close()

        thread = threading.Thread(target=hold_briefly)
        thread.start()
        self.assertTrue(locked.wait(5))
        # A bound far above the 50 ms hold, so load cannot stretch the hold
        # past it; that the default bound is not zero is asserted below.
        said = jev_meter.record(dict(self.EVENT), {"JEV_METER_DB": str(self.store),
                                                   "JEV_METER_BUSY_MS": "3000"})
        thread.join()
        self.assertEqual(said, jev_meter.WRITTEN)

    def test_the_default_bound_waits_long_enough_to_land_a_contended_row(self):
        """A zero default dropped rows under ordinary contention (sd:2087)."""
        self.assertGreaterEqual(jev_meter.busy_ms({}), 100)

    def test_the_bound_can_be_set_and_a_typo_keeps_the_default(self):
        self.assertEqual(jev_meter.busy_ms({}), jev_meter.BUSY_MS)
        self.assertEqual(jev_meter.busy_ms({"JEV_METER_BUSY_MS": "0"}), 0)
        self.assertEqual(jev_meter.busy_ms({"JEV_METER_BUSY_MS": "40"}), 40)
        self.assertEqual(jev_meter.busy_ms({"JEV_METER_BUSY_MS": "soon"}),
                         jev_meter.BUSY_MS)

    def test_a_locked_ledger_changes_neither_the_answer_nor_the_exit_code(self):
        holder = connect(self.store)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        try:
            self.assertEqual(self.run_verbose(["noul", "is it?"]),
                             self.run_verbose(["noul", "is it?"],
                                              JEV_METER="0"))
        finally:
            holder.execute("ROLLBACK")
