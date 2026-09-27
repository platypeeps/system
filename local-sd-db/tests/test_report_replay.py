"""Replaying a cron report is a no-op on every path (sd:757).

`reporting._beat` promised that, and two replays broke it. A recovery tick
answered `nothing` the second time, because its own first heartbeat had already
made the job read healthy. An older run replayed after newer ticks appended its
heartbeat last, so `health` read the older tick as the current one, and the next
clean tick filed a recovery that never happened.
"""
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sd_db import reporting, retention, workflow
from sd_db.database import connect
from sd_db.migrate import initialise


#: The clock every tick here reads, injected: after every fixture stamp below
#: except the ones that are deliberately in the future.
NOW = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)


class AReplayWritesNothing(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "sd.db"; initialise(self.path)
        self.db = connect(self.path); self.addCleanup(self.db.close)

    def tick(self, run, minute, *, second=0, exit_code=0, text="ok\n", ended=None):
        """One run with its own log, so a replay reads exactly what the run did."""
        log = self.root / f"{run}.log"
        if not log.exists():
            log.write_text(text)
        return reporting.ingest_log(
            self.db, job="probe", run_id=run, started="2026-09-08T00:00:00Z",
            ended=ended or f"2026-09-08T00:{minute:02d}:{second:02d}Z", exit_code=exit_code,
            log_path=log, now=NOW)

    def items(self):
        return [row["id"] for row in self.db.execute("SELECT id FROM item WHERE kind='report' ORDER BY id")]

    def beats(self):
        return [json.loads(row["body"])["run_id"] for row in self.db.execute(
            "SELECT body FROM state WHERE kind='heartbeat' AND key=? ORDER BY id",
            (reporting.HEARTBEAT_KEY + "probe",))]

    def dump(self):
        return tuple(self.db.iterdump())

    def test_replaying_a_recovery_returns_the_report_it_filed(self):
        self.tick("a", 1, exit_code=1, text="boom\n")
        first = self.tick("b", 16)
        self.assertEqual((first["recorded"], first["why"]), ("report", "the job recovered"))
        before = self.dump()
        again = self.tick("b", 16)
        self.assertEqual(again["recorded"], "report")
        self.assertEqual(again["why"], "the job recovered")
        self.assertEqual(again["item"]["id"], first["item"]["id"])
        self.assertEqual(before, self.dump())

    def test_replaying_an_older_run_leaves_current_health_alone(self):
        failed = self.tick("a", 1, exit_code=1, text="boom\n")
        self.tick("b", 16)
        self.assertEqual(self.tick("c", 31)["recorded"], "heartbeat")
        before = self.dump()
        again = self.tick("a", 1, exit_code=1, text="boom\n")
        self.assertEqual(again["recorded"], "report")
        self.assertEqual(again["item"]["id"], failed["item"]["id"])
        self.assertEqual(before, self.dump())
        self.assertIs(reporting.health(self.db, "probe"), True)
        self.assertEqual(self.beats(), ["a", "b", "c"])
        # And the job is still quiet: no recovery from a failure that is not current.
        reports = self.items()
        self.assertEqual(self.tick("d", 46)["recorded"], "heartbeat")
        self.assertEqual(self.items(), reports)

    def test_a_replay_after_compaction_still_writes_nothing(self):
        """Retention keeps one heartbeat per key, so the replayed run's own row
        may be gone; the run is still older than the tick that replaced it."""
        self.tick("a", 1)
        self.tick("b", 16)
        retention.compact_heartbeats(self.db)
        self.assertEqual(self.beats(), ["b"])
        before = self.dump()
        self.assertEqual(self.tick("a", 1)["recorded"], "nothing")
        self.assertEqual(before, self.dump())

    def test_a_replay_of_the_older_of_two_same_second_runs_writes_nothing(self):
        """The run-id guard reads every row, not only the newest.

        Two runs of one job can end in the same second, and `stamp` keeps whole
        seconds, so the `ended` guard cannot tell the older one from the newer:
        only the older run's own heartbeat row says it was already recorded.
        """
        failed = self.tick("a", 1, second=5, exit_code=1, text="boom\n")
        self.assertEqual(self.tick("c", 1, second=5)["recorded"], "report")
        before = self.dump()
        again = self.tick("a", 1, second=5, exit_code=1, text="boom\n")
        self.assertEqual((again["recorded"], again["item"]["id"]), ("report", failed["item"]["id"]))
        self.assertEqual(before, self.dump())
        self.assertEqual(self.beats(), ["a", "c"])
        self.assertIs(reporting.health(self.db, "probe"), True)

    def test_a_replay_of_a_same_second_run_after_compaction_is_not_a_recovery(self):
        """The #326 review: compaction took the older run's own row, and its `ended`
        ties the newest, so only the newest row's `same_second` can say it was seen."""
        self.tick("a", 1, second=5)
        failed = self.tick("c", 1, second=5, exit_code=1, text="boom\n")
        self.assertEqual(failed["recorded"], "report")
        self.assertEqual(retention.compact_heartbeats(self.db), 1)
        self.assertEqual(self.beats(), ["c"])
        reports = self.items()
        before = self.dump()
        again = self.tick("a", 1, second=5)
        self.assertEqual(again["recorded"], "nothing")
        self.assertEqual(again["why"], "a replay of a run whose heartbeat is already recorded, so nothing was written")
        self.assertEqual(self.items(), reports)
        self.assertEqual(before, self.dump())
        self.assertIs(reporting.health(self.db, "probe"), False)

    def test_a_replayed_same_second_failure_after_compaction_leaves_health_alone(self):
        failed = self.tick("a", 1, second=5, exit_code=1, text="boom\n")
        self.assertEqual(self.tick("c", 1, second=5)["why"], "the job recovered")
        retention.compact_heartbeats(self.db)
        before = self.dump()
        again = self.tick("a", 1, second=5, exit_code=1, text="boom\n")
        self.assertEqual((again["recorded"], again["item"]["id"]), ("report", failed["item"]["id"]))
        self.assertEqual(before, self.dump())
        self.assertIs(reporting.health(self.db, "probe"), True)

    def test_a_new_run_in_the_same_second_is_still_ordered_by_arrival(self):
        """Two first arrivals in one second: the later one is the newer tick, and it says so."""
        self.tick("a", 1, second=5)
        tied = self.tick("b", 1, second=5)
        self.assertEqual(tied["recorded"], "heartbeat")
        self.assertIn("same second", tied["why"])
        self.assertEqual(self.beats(), ["a", "b"])

    def test_the_runs_one_second_remembers_are_bounded(self):
        for number in range(reporting.REMEMBERED_RUNS + 5):
            self.tick(f"r{number:02d}", 1, second=5)
        retention.compact_heartbeats(self.db)
        body = json.loads(self.db.execute("SELECT body FROM state WHERE kind='heartbeat' AND key=?",
                                          (reporting.HEARTBEAT_KEY + "probe",)).fetchone()["body"])
        self.assertEqual(body["run_id"], "r24")
        self.assertEqual(body["same_second"], [f"r{number:02d}" for number in range(4, 24)])
        self.assertEqual(self.tick("r04", 1, second=5)["recorded"], "nothing")

    def test_an_older_clean_run_is_not_a_recovery_from_a_newer_failure(self):
        self.tick("a", 1)
        self.tick("b", 16, exit_code=1, text="boom\n")
        retention.compact_heartbeats(self.db)
        reports = self.items()
        before = self.dump()
        self.assertEqual(self.tick("a", 1)["recorded"], "nothing")
        self.assertEqual(self.items(), reports)
        self.assertEqual(before, self.dump())
        self.assertIs(reporting.health(self.db, "probe"), False)

    def test_a_late_clean_run_says_why_it_wrote_no_heartbeat(self):
        """Not "the last tick was also clean": the last tick failed."""
        self.tick("b", 16, exit_code=1, text="boom\n")
        late = self.tick("a", 1)
        self.assertEqual(late["recorded"], "nothing")
        self.assertEqual(late["why"], "an older run than the job's newest recorded tick, so no heartbeat was written")
        self.assertIs(reporting.health(self.db, "probe"), False)

    def test_a_future_stamp_does_not_freeze_health(self):
        """One heartbeat stamped past the clock would otherwise outrank every real
        tick until the clock caught up: no heartbeat written, no recovery filed."""
        self.tick("typo", 0, ended="2027-09-08T00:01:00Z")
        failed = self.tick("b", 16, exit_code=1, text="boom\n")
        self.assertEqual(failed["recorded"], "report")
        self.assertIs(reporting.health(self.db, "probe"), False)
        recovered = self.tick("c", 31)
        self.assertEqual((recovered["recorded"], recovered["why"]), ("report", "the job recovered"))
        self.assertIs(reporting.health(self.db, "probe"), True)
        self.assertEqual(self.beats(), ["typo", "b", "c"])

    def test_a_stamp_within_the_allowance_still_counts_as_newest(self):
        """Five minutes past the clock is a slewing clock, not a typo: it still wins."""
        self.tick("ahead", 0, ended="2026-09-08T01:04:59Z")
        self.assertEqual(self.tick("b", 16, exit_code=1, text="boom\n")["recorded"], "report")
        self.assertIs(reporting.health(self.db, "probe"), True)
        self.assertEqual(self.beats(), ["ahead"])
        self.tick("beyond", 0, ended="2026-09-08T01:30:00Z")
        self.assertEqual(self.beats(), ["ahead", "beyond"])
        # The second `exit 1` folds into b's open report (sd:920); its
        # heartbeat is still this tick's own, and it still makes the job unhealthy.
        self.assertEqual(self.tick("c", 31, exit_code=1, text="boom\n")["recorded"], "folded")
        self.assertIs(reporting.health(self.db, "probe"), False)

    def test_a_late_failure_is_still_filed_but_does_not_become_current_health(self):
        """A run whose ingest never landed, recovered by hand after newer ticks:
        the failure is an event and gets its report, but it is not the last tick."""
        self.tick("b", 16)
        late = self.tick("a", 1, exit_code=1, text="boom\n")
        self.assertEqual(late["recorded"], "report")
        self.assertIs(reporting.health(self.db, "probe"), True)
        self.assertEqual(self.beats(), ["b"])
        self.assertEqual(self.tick("c", 31)["recorded"], "heartbeat")

    def test_the_shapes_a_replay_already_had_are_kept(self):
        quiet = self.tick("a", 1)
        self.assertEqual(quiet["recorded"], "heartbeat")
        self.assertEqual(self.tick("a", 1)["recorded"], "nothing")
        failed = self.tick("b", 16, exit_code=1, text="boom\n")
        before = self.dump()
        again = self.tick("b", 16, exit_code=1, text="boom\n")
        self.assertEqual((again["recorded"], again["item"]["id"]), ("report", failed["item"]["id"]))
        self.assertEqual(before, self.dump())

    def test_a_replayed_clean_run_whose_log_now_declares_a_finding_files_nothing(self):
        """The #315 review: the run id is the key. A clean run was recorded as a
        heartbeat, so its replay files no report, even when the log it reads has
        grown a later `SD_REPORT_ATTENTION` line or the exit code differs."""
        self.tick("a", 1)
        (self.root / "a.log").write_text("ok\nSD_REPORT_ATTENTION: a later run's finding\n")
        before = self.dump()
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                again = self.tick("a", 1, exit_code=exit_code)
                self.assertEqual((again["recorded"], again["healthy"]), ("nothing", False))
                self.assertEqual(again["why"], "a replay of a run whose heartbeat recorded it clean;"
                                               " the run id is the key, so its changed evidence was not written")
                self.assertEqual(self.items(), [])
                self.assertEqual(before, self.dump())
        self.assertIs(reporting.health(self.db, "probe"), True)


class TheQuietPathIsCheckedAndSaysWhatHappened(unittest.TestCase):
    """The #315 review: a clean run returns before `ingest`, so it must check the
    run the way `ingest` does, and its reason must be true of this run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "sd.db"; initialise(self.path)
        self.db = connect(self.path); self.addCleanup(self.db.close)
        self.log = self.root / "probe.log"; self.log.write_text("ok\n")

    def tick(self, **changes):
        arguments = {"job": "probe", "run_id": "one", "started": "2026-09-08T00:00:00Z",
                     "ended": "2026-09-08T00:01:00Z", "exit_code": 0, "log_path": self.log, "now": NOW, **changes}
        return reporting.ingest_log(self.db, **arguments)

    def test_a_bad_first_clean_run_is_refused_before_any_heartbeat(self):
        cases = {"job with a space": {"job": "bad name"}, "job not text": {"job": 7},
                 "empty run id": {"run_id": ""}, "long run id": {"run_id": "r" * 161},
                 "run id not text": {"run_id": 12},
                 "ends before it starts": {"started": "2026-09-08T00:02:00Z"},
                 "exit code as text": {"exit_code": "0"}, "exit code as bool": {"exit_code": False}}
        for name, change in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(workflow.WorkflowError):
                    self.tick(**change)
                self.assertEqual(self.db.execute("SELECT count(*) FROM state WHERE kind='heartbeat'").fetchone()[0], 0)

    def test_a_first_clean_run_says_it_is_a_baseline(self):
        first = self.tick()
        self.assertEqual((first["recorded"], first["why"]),
                         ("heartbeat", "the job's first recorded tick, a baseline and not an event"))
        second = self.tick(run_id="two", ended="2026-09-08T00:16:00Z")
        self.assertEqual((second["recorded"], second["why"]),
                         ("heartbeat", "a clean tick of a job whose last tick was also clean"))

    def test_a_replayed_clean_run_says_it_is_a_replay(self):
        self.tick()
        again = self.tick()
        self.assertEqual((again["recorded"], again["why"]),
                         ("nothing", "a replay of a run whose heartbeat is already recorded, so nothing was written"))


class ARepeatedFailureFoldsIntoTheOpenReport(unittest.TestCase):
    """A job that fails the same way on a schedule opens one report, not one per run (sd:920).

    sd:880's watchdog opened 51 `needs attention` rows in thirteen hours, one
    every fifteen minutes, each with its own followup. The fix that stopped
    that job did not bound the next one. So a run that needs attention, when
    an open attention report for the same job and the same `attention_basis`
    already exists, folds into it: `fields.repeats` carries the count and the
    last run's identity, no second row and no second followup are written.
    The single-failure case is untouched: one bad night still opens one
    report, and the run after an acknowledge opens a fresh one.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "sd.db"; initialise(self.path)
        self.db = connect(self.path); self.addCleanup(self.db.close)

    def tick(self, run, minute, *, exit_code=1, text=None, job="probe"):
        log = self.root / f"{job}-{run}.log"
        if not log.exists():
            log.write_text(text if text is not None else f"boom at {minute}\n")
        return reporting.ingest_log(
            self.db, job=job, run_id=run, started="2026-09-08T00:00:00Z",
            ended=f"2026-09-08T00:{minute:02d}:00Z", exit_code=exit_code, log_path=log, now=NOW)

    def rows(self):
        return [dict(row) for row in self.db.execute(
            "SELECT id, title, status, external_id, fields, body FROM item WHERE kind='report' ORDER BY id")]

    def followups(self, item):
        return self.db.execute("SELECT COUNT(*) FROM note WHERE item=? AND kind='followup'", (item,)).fetchone()[0]

    def repeats(self, item):
        return json.loads(self.db.execute("SELECT fields FROM item WHERE id=?", (item,)).fetchone()["fields"]).get("repeats")

    def test_a_repeated_failure_folds_with_a_count_and_stores_no_second_body(self):
        first = self.tick("a", 1)
        self.assertEqual(first["recorded"], "report")
        item = first["item"]["id"]
        second = self.tick("b", 16)
        self.assertEqual((second["recorded"], second["item"]["id"], second["count"]), ("folded", item, 2))
        self.assertEqual(second["why"], "job exited 1")
        third = self.tick("c", 31)
        self.assertEqual((third["recorded"], third["item"]["id"], third["count"]), ("folded", item, 3))
        rows = self.rows()
        self.assertEqual([row["external_id"] for row in rows], ["probe:a"])
        self.assertEqual(rows[0]["title"], "probe: needs attention")
        self.assertEqual(json.loads(rows[0]["body"]), {"text": "boom at 1\n"})
        self.assertEqual(self.followups(item), 1)
        fields = json.loads(rows[0]["fields"])
        self.assertIs(fields["attention"], True)
        # The original run's own provenance is untouched by the folds.
        self.assertEqual((fields["report"]["run_id"], fields["report"]["ended"]), ("a", "2026-09-08T00:01:00+00:00"))
        self.assertEqual(fields["repeats"], {
            "count": 3, "first_ended": "2026-09-08T00:01:00+00:00", "last_run_id": "c",
            "last_ended": "2026-09-08T00:31:00+00:00", "run_ids": ["b", "c"]})

    def test_a_replay_of_a_folded_run_or_of_the_original_counts_nothing(self):
        item = self.tick("a", 1)["item"]["id"]
        self.tick("b", 16)
        before = tuple(self.db.iterdump())
        again = self.tick("b", 16)
        self.assertEqual((again["recorded"], again["item"]["id"], again["count"]), ("folded", item, 2))
        self.assertEqual(before, tuple(self.db.iterdump()))
        # The open report's own run, replayed after a fold changed its fields,
        # is still its own replay and not "different evidence".
        original = self.tick("a", 1)
        self.assertEqual((original["recorded"], original["item"]["id"]), ("report", item))
        self.assertEqual(before, tuple(self.db.iterdump()))

    def test_only_the_last_twenty_folded_runs_are_remembered(self):
        item = self.tick("a", 1)["item"]["id"]
        for index in range(1, 26):
            self.tick(f"r{index:02d}", 2 + index)
        repeats = self.repeats(item)
        self.assertEqual(repeats["count"], 26)
        self.assertEqual(repeats["run_ids"], [f"r{index:02d}" for index in range(6, 26)])
        self.assertEqual((repeats["last_run_id"], repeats["last_ended"]), ("r25", "2026-09-08T00:27:00+00:00"))
        # A folded run older than the twenty remembered is counted again on a
        # replay, and the docstring says so.
        self.assertEqual(self.tick("r01", 3)["count"], 27)

    def test_a_different_basis_opens_a_new_report(self):
        first = self.tick("a", 1, exit_code=1)["item"]["id"]
        second = self.tick("b", 16, exit_code=2)
        self.assertEqual(second["recorded"], "report")
        self.assertNotEqual(second["item"]["id"], first)
        self.assertEqual(len(self.rows()), 2)
        self.assertIsNone(self.repeats(first))
        self.assertIsNone(self.repeats(second["item"]["id"]))
        # And the same basis folds into the newest open one, not the oldest.
        third = self.tick("c", 31, exit_code=2)
        self.assertEqual((third["recorded"], third["item"]["id"]), ("folded", second["item"]["id"]))
        self.assertEqual(self.tick("d", 46, exit_code=1)["item"]["id"], first)

    def test_a_declared_finding_and_an_exit_code_are_two_bases(self):
        first = self.tick("a", 1, exit_code=0, text="SD_REPORT_ATTENTION: disk at 91%\n")["item"]["id"]
        self.assertEqual(self.tick("b", 16, exit_code=0, text="SD_REPORT_ATTENTION: disk at 91%\n")["item"]["id"], first)
        self.assertEqual(self.repeats(first)["count"], 2)
        other = self.tick("c", 31, exit_code=0, text="SD_REPORT_ATTENTION: disk at 95%\n")
        self.assertEqual(other["recorded"], "report")
        self.assertNotEqual(other["item"]["id"], first)

    def test_a_clean_report_never_folds_and_is_never_a_target(self):
        failed = self.tick("a", 1)["item"]["id"]
        recovered = self.tick("b", 16, exit_code=0, text="ok\n")
        self.assertEqual((recovered["recorded"], recovered["why"]), ("report", "the job recovered"))
        # The failure after a recovery folds into the still-open failure report.
        self.assertEqual(self.tick("c", 31)["item"]["id"], failed)
        again = self.tick("d", 46, exit_code=0, text="ok\n")
        self.assertEqual((again["recorded"], again["why"]), ("report", "the job recovered"))
        self.assertNotEqual(again["item"]["id"], recovered["item"]["id"])
        clean = [row for row in self.rows() if json.loads(row["fields"])["attention"] is False]
        self.assertEqual(len(clean), 2)
        self.assertTrue(all("repeats" not in json.loads(row["fields"]) for row in clean))

    def test_another_job_with_the_same_basis_is_not_a_target(self):
        first = self.tick("a", 1)["item"]["id"]
        other = self.tick("a", 1, job="other")
        self.assertEqual(other["recorded"], "report")
        self.assertNotEqual(other["item"]["id"], first)
        self.assertIsNone(self.repeats(first))

    def test_the_run_after_an_acknowledge_opens_a_fresh_report(self):
        first = self.tick("a", 1)
        self.tick("b", 16)
        reporting.acknowledge(self.db, first["item"]["id"], who="fixture", resolve_ingest_followups=True,
                              expected_revision=workflow.item_state(self.db, first["item"]["id"])["revision"])
        fresh = self.tick("c", 31)
        self.assertEqual(fresh["recorded"], "report")
        self.assertNotEqual(fresh["item"]["id"], first["item"]["id"])
        self.assertNotIn("count", fresh)
        self.assertEqual(self.followups(fresh["item"]["id"]), 1)
        self.assertIsNone(self.repeats(fresh["item"]["id"]))
        self.assertEqual(self.repeats(first["item"]["id"])["count"], 2)
        # And the fresh one is the target from here on, not the acknowledged one.
        self.assertEqual(self.tick("d", 46)["item"]["id"], fresh["item"]["id"])

    def test_a_folded_run_replayed_after_the_acknowledge_answers_with_the_done_report(self):
        # An operator recovering a folded run by hand after the report was
        # acknowledged: the run's identity lives only in the done report's
        # `repeats.run_ids`, which is not a fold target. Before sd:955 the
        # replay found no report and opened a fresh one, with a followup.
        first = self.tick("a", 1)["item"]["id"]
        self.tick("b", 16)
        reporting.acknowledge(self.db, first, who="fixture", resolve_ingest_followups=True,
                              expected_revision=workflow.item_state(self.db, first)["revision"])
        before = tuple(self.db.iterdump())
        again = self.tick("b", 16)
        self.assertEqual((again["recorded"], again["item"]["id"], again["item"]["status"]), ("folded", first, "done"))
        self.assertEqual(again["count"], 2)
        self.assertEqual([row["external_id"] for row in self.rows()], ["probe:a"])
        self.assertEqual(self.followups(first), 1)
        self.assertEqual(before, tuple(self.db.iterdump()))
        # The done report's own run replays the same way it always did.
        original = self.tick("a", 1)
        self.assertEqual((original["recorded"], original["item"]["id"]), ("report", first))
        self.assertEqual(before, tuple(self.db.iterdump()))
        # A run id that was folded under another job is not this job's replay.
        self.assertEqual(self.tick("b", 16, job="other")["recorded"], "report")

    def test_the_folded_run_lookup_reads_one_job_through_the_identity_index(self):
        """The #401 review: every report-worthy ingest asks `_folded_into`, and it
        scanned every `item` row. It now seeks the job's own `job:` range of
        `item_by_external`, so a job's lookup costs that job's reports only."""
        first = self.tick("a", 1)["item"]["id"]
        self.tick("b", 16)
        self.tick("a", 1, job="prob")
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            self.assertEqual(reporting._folded_into(self.db, "probe", "b")["id"], first)
            self.assertIsNone(reporting._folded_into(self.db, "prob", "b"))
            self.assertIsNone(reporting._folded_into(self.db, "probe", "missing"))
        finally:
            self.db.set_trace_callback(None)
        lookups = [sql for sql in statements if "repeats.run_ids" in sql]
        self.assertEqual(len(lookups), 3)
        for sql in lookups:
            plan = [row[3] for row in self.db.execute("EXPLAIN QUERY PLAN " + sql)]
            self.assertTrue(any("USING INDEX item_by_external" in step for step in plan), plan)
            self.assertFalse(any(step.startswith("SCAN item") for step in plan), plan)

    def test_a_report_with_unreadable_fields_is_never_a_target(self):
        for index, value in enumerate((None, "", "{not json")):
            with self.subTest(fields=value):
                run = f"a-{index}"
                item = self.tick(run, 1)["item"]["id"]
                self.db.execute("UPDATE item SET fields=? WHERE id=?", (value, item))
                self.db.commit()
                fresh = self.tick(run + "-next", 16)
                self.assertEqual(fresh["recorded"], "report")
                self.assertNotEqual(fresh["item"]["id"], item)
                # The readable one is acknowledged so the next subtest starts
                # from no readable open target; the unreadable one stays open,
                # which is the property: open, and still never a target.
                reporting.acknowledge(self.db, fresh["item"]["id"], who="fixture", resolve_ingest_followups=True,
                                      expected_revision=workflow.item_state(self.db, fresh["item"]["id"])["revision"])

    def test_a_record_report_neither_folds_nor_is_a_target(self):
        kwargs = dict(started="2026-09-08T00:00:00Z", ended="2026-09-08T00:01:00Z", exit_code=1,
                      text="boom\n", source_path="/fixture", attention=True)
        marked = reporting.ingest(self.db, job="probe", run_id="record-one", record="probe", **kwargs)["item"]["id"]
        plain = reporting.ingest(self.db, job="probe", run_id="plain-one", **kwargs)["item"]["id"]
        self.assertNotEqual(plain, marked)
        marked_again = reporting.ingest(self.db, job="probe", run_id="record-two", record="probe", **kwargs)["item"]["id"]
        self.assertNotIn(marked_again, (marked, plain))
        self.assertEqual(reporting.ingest(self.db, job="probe", run_id="plain-two", **kwargs)["item"]["id"], plain)
        self.assertEqual(self.repeats(plain)["count"], 2)
        self.assertIsNone(self.repeats(marked))
        self.assertIsNone(self.repeats(marked_again))

    def test_a_record_that_reuses_a_folded_run_id_is_its_own_row(self):
        # A record report is never folded, and a folded run's identity is
        # not a record's replay either: `_folded_into` is asked only for a
        # cron run, so a record that reuses a folded `job`/`run_id` is filed
        # as a new row with its own `record` and `actor` fields, and the
        # report the run folded into is untouched.
        first = self.tick("a", 1)["item"]["id"]
        self.tick("b", 16)
        repeats = self.repeats(first)
        state = reporting.ingest(self.db, job="probe", run_id="b", record="probe", actor={"who": "fixture"},
                                 started="2026-09-08T00:00:00Z", ended="2026-09-08T00:17:00Z", exit_code=1,
                                 text="by hand\n", source_path="/fixture", attention=True)
        self.assertNotEqual(state["item"]["id"], first)
        rows = {row["external_id"]: row for row in self.rows()}
        self.assertEqual(sorted(rows), ["probe:a", "probe:b"])
        fields = json.loads(rows["probe:b"]["fields"])
        self.assertEqual((fields["record"], fields["report"]["actor"]), ("probe", {"who": "fixture"}))
        self.assertEqual(self.repeats(first), repeats)
        self.assertEqual(self.followups(first), 1)


if __name__ == "__main__": unittest.main()
