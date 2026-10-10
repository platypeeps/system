"""Every write is a named function, and a status change carries its note."""

import tempfile
import unittest
from pathlib import Path

from sd_db import (
    active_trials,
    add_note,
    connect,
    create_assignment,
    create_item,
    end_trial,
    record_cost,
    record_skill_use,
    record_state,
    resolve_state,
    set_item_fields,
    skill_use_since,
    snooze,
    snoozed,
    start_trial,
    transition,
    trials,
    unresolved_state,
    update_assignment,
    upsert_repo,
    upsert_shadow,
)
from sd_db.errors import SdDbError
from sd_db.migrate import initialise
from sd_db.writes import TransitionRefused, now, stamp


class WriteCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.connection = connect(self.path)
        self.addCleanup(self.connection.close)

    def notes(self, item, kind=None):
        if kind is None:
            rows = self.connection.execute(
                "SELECT * FROM note WHERE item = ? ORDER BY id", (item,)
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM note WHERE item = ? AND kind = ? ORDER BY id", (item, kind)
            )
        return list(rows)


class TheStatusRule(WriteCase):
    def test_opening_an_item_writes_the_first_status_note(self):
        item = create_item(self.connection, kind="work", title="A thing")
        history = self.notes(item, "status_change")
        self.assertEqual(len(history), 1)
        self.assertIn("planning", history[0]["body"])

    def test_a_transition_writes_the_row_and_the_note_together(self):
        item = create_item(self.connection, kind="work", title="A thing")
        previous = transition(self.connection, item, "in_progress", who="sd-ship")
        self.assertEqual(previous, "planning")
        row = self.connection.execute(
            "SELECT status FROM item WHERE id = ?", (item,)
        ).fetchone()
        self.assertEqual(row["status"], "in_progress")
        history = self.notes(item, "status_change")
        self.assertEqual(len(history), 2)
        self.assertIn("planning -> in_progress by sd-ship", history[1]["body"])

    def test_a_transition_to_the_same_status_writes_nothing(self):
        item = create_item(self.connection, kind="work", title="A thing")
        transition(self.connection, item, "planning", who="nobody")
        self.assertEqual(len(self.notes(item, "status_change")), 1)

    def test_status_cannot_be_written_any_other_way(self):
        item = create_item(self.connection, kind="work", title="A thing")
        with self.assertRaises(TransitionRefused) as raised:
            set_item_fields(self.connection, item, status="done")
        self.assertIn("transition", str(raised.exception))

    def test_a_status_change_note_cannot_be_written_on_its_own(self):
        item = create_item(self.connection, kind="work", title="A thing")
        with self.assertRaises(TransitionRefused):
            add_note(self.connection, item, "status_change", "planning -> done")

    def test_an_unknown_status_is_refused_naming_the_vocabulary(self):
        item = create_item(self.connection, kind="work", title="A thing")
        with self.assertRaises(TransitionRefused) as raised:
            transition(self.connection, item, "shipped", who="me")
        self.assertIn("ready_to_send", str(raised.exception))

    def test_a_failed_transition_leaves_no_note_behind(self):
        """The note and the row are one transaction, in both directions."""
        with self.assertRaises(TransitionRefused):
            transition(self.connection, 999, "done", who="me")
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM note").fetchone()[0], 0
        )


class TheOtherWrites(WriteCase):
    def test_a_repository_updates_only_the_fields_given(self):
        upsert_repo(self.connection, "/repos/one", remote="git@example:one", mode="full")
        upsert_repo(self.connection, "/repos/one")
        row = self.connection.execute("SELECT * FROM repo").fetchone()
        self.assertEqual(row["remote"], "git@example:one")
        self.assertEqual(row["runner_merge"], "manual")

    def test_an_exec_note_carries_the_run_and_others_may_not(self):
        item = create_item(self.connection, kind="task", title="Run it")
        add_note(self.connection, item, "exec", "ran", started="a", ended="b", exit_code=0)
        with self.assertRaises(Exception):
            add_note(self.connection, item, "comment", "no", exit_code=1)

    def test_one_tracker_re_reading_a_url_updates_its_own_row(self):
        upsert_shadow(self.connection, tracker="github", url="u", title="first")
        upsert_shadow(self.connection, tracker="github", url="u", title="second")
        rows = list(self.connection.execute("SELECT title FROM shadow"))
        self.assertEqual([row["title"] for row in rows], ["second"])

    def test_two_trackers_may_hold_the_same_url(self):
        """The key is `(tracker, url)`, so neither tracker can adopt the other's
        row. Under the url-alone key this collector's write reassigned the row's
        `tracker` column in silence: no error, no constraint violation, and the
        first tracker's identity simply gone."""
        url = "https://github.com/o/r/issues/1"
        upsert_shadow(self.connection, tracker="github", url=url, title="from github")
        upsert_shadow(self.connection, tracker="jira", url=url, title="from jira")
        rows = list(self.connection.execute(
            "SELECT tracker, title FROM shadow ORDER BY tracker"))
        self.assertEqual(
            [(row["tracker"], row["title"]) for row in rows],
            [("github", "from github"), ("jira", "from jira")],
        )

    def test_an_assignment_updates_through_its_named_function(self):
        item = create_item(self.connection, kind="work", title="A thing")
        assignment = create_assignment(self.connection, role="reviewer", status="queued", item=item)
        update_assignment(self.connection, assignment, status="blocked", phase="requested")
        row = self.connection.execute(
            "SELECT status, phase FROM assignment WHERE id = ?", (assignment,)
        ).fetchone()
        self.assertEqual((row["status"], row["phase"]), ("blocked", "requested"))

    def test_an_unknown_column_is_refused_rather_than_ignored(self):
        item = create_item(self.connection, kind="work", title="A thing")
        with self.assertRaises(SdDbError):
            set_item_fields(self.connection, item, headline="oops")

    def test_a_cost_row_records_what_kind_of_number_it_is(self):
        record_cost(self.connection, source="meter", bill=None, used_percent=42.0)
        row = self.connection.execute("SELECT source, used_percent FROM cost").fetchone()
        self.assertEqual((row["source"], row["used_percent"]), ("meter", 42.0))


class TheStateTable(WriteCase):
    def test_a_kind_with_no_home_is_refused_naming_the_kinds(self):
        with self.assertRaises(SdDbError) as raised:
            record_state(self.connection, "sighting", key="x")
        self.assertIn("checkpoint", str(raised.exception))

    def test_a_record_is_open_until_it_is_resolved(self):
        row = record_state(self.connection, "restore", key="2026-09-06")
        self.assertEqual(len(unresolved_state(self.connection, "restore")), 1)
        resolve_state(self.connection, row)
        self.assertEqual(unresolved_state(self.connection, "restore"), [])


class TheSnooze(WriteCase):
    """A row hidden until a time (sd:1896): a `snooze` state row per write,
    the latest per key the answer, and the reader's clock the expiry."""

    NOW = "2026-10-10T12:00:00Z"

    def test_a_snooze_holds_until_its_time_and_then_lapses(self):
        snooze(self.connection, "today:job:nightly:1", "2026-10-11T08:00:00-06:00", now=self.NOW)
        self.assertEqual(snoozed(self.connection, now=self.NOW), {"today:job:nightly:1": "2026-10-11T14:00:00+00:00"})
        self.assertEqual(snoozed(self.connection, now="2026-10-11T13:59:59Z"),
                         {"today:job:nightly:1": "2026-10-11T14:00:00+00:00"})
        self.assertEqual(snoozed(self.connection, now="2026-10-11T14:00:00Z"), {})
        row = self.connection.execute("SELECT kind, key, body, resolved_at FROM state").fetchone()
        self.assertEqual(tuple(row), ("snooze", "today:job:nightly:1", '{"until": "2026-10-11T14:00:00+00:00"}', None))

    def test_the_latest_write_for_a_key_wins_and_a_cleared_one_shows_the_row(self):
        snooze(self.connection, "health:br:system", "2026-10-17T12:00:00Z", now=self.NOW)
        snooze(self.connection, "health:br:system", "2026-10-10T13:00:00Z", now=self.NOW)
        snooze(self.connection, "today:ahead:system:2", "2026-10-11T12:00:00Z", now=self.NOW)
        self.assertEqual(snoozed(self.connection, now=self.NOW), {"health:br:system": "2026-10-10T13:00:00+00:00",
                                                                  "today:ahead:system:2": "2026-10-11T12:00:00+00:00"})
        snooze(self.connection, "health:br:system", None, now=self.NOW)
        self.assertEqual(snoozed(self.connection, now=self.NOW), {"today:ahead:system:2": "2026-10-11T12:00:00+00:00"})
        # Clearing a key nobody snoozed writes the same row and shows nothing new.
        snooze(self.connection, "today:dark:jobs", None, now=self.NOW)
        self.assertEqual(len(snoozed(self.connection, now=self.NOW)), 1)

    def test_a_time_in_the_past_too_far_ahead_or_without_a_zone_is_refused(self):
        for until, said in (("2026-10-10T11:59:59Z", "already past"), ("2026-10-10T12:00:00Z", "already past"),
                            ("2026-11-10T12:00:01Z", "31 days"), ("2026-10-11T08:00:00", "no timezone"),
                            ("tomorrow", "not an ISO-8601")):
            with self.subTest(until=until), self.assertRaises(SdDbError) as raised:
                snooze(self.connection, "today:job:nightly:1", until, now=self.NOW)
            self.assertIn(said, str(raised.exception))
        for key in ("", None, 7, "x" * 513):
            with self.subTest(key=key), self.assertRaises(SdDbError):
                snooze(self.connection, key, "2026-10-11T08:00:00Z", now=self.NOW)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM state").fetchone()[0], 0)

    def test_a_body_the_reader_cannot_parse_shows_the_row(self):
        """An unreadable snooze is not a snooze: the row it would hide shows."""
        for key, body in (("today:a", "not json"), ("today:b", '["2026-10-11T08:00:00Z"]'),
                          ("today:c", '{"until": "soon"}'), ("today:d", None)):
            record_state(self.connection, "snooze", key=key, body=body, timestamp=self.NOW)
        self.assertEqual(snoozed(self.connection, now=self.NOW), {})


if __name__ == "__main__":
    unittest.main()


class OneShapeOnDisk(WriteCase):
    """Two spellings of one instant are two instants to a text comparison.

    `now()` writes `+00:00`. `...Z` is the same moment and sorts *after* it,
    because `Z` is 0x5A and `+` is 0x2B. Every stamp a caller hands in is
    normalized at the write, so this is a property of the rows rather than a
    rule callers are asked to remember.
    """

    def test_the_two_spellings_really_do_order_differently(self):
        """The premise. Without it every test below passes for no reason."""
        plus = "2026-09-06T19:11:39+00:00"
        zulu = "2026-09-06T19:11:39Z"
        self.assertNotEqual(plus, zulu)
        self.assertGreater(zulu, plus)
        self.assertEqual(stamp(zulu), plus)

    def test_now_writes_the_shape_stamp_normalizes_to(self):
        """One clock read, three assertions. Two reads is a race, not a test.

        `stamp()` is what every write puts a caller's value through, and
        `now()` is what it puts its own through. If those two disagreed on
        shape the normalization would be normalizing to something the default
        does not use, and every comparison in this file would still be
        between two shapes.
        """
        moment = now()
        self.assertTrue(moment.endswith("+00:00"))
        self.assertEqual(stamp(moment), moment)
        self.assertEqual(stamp(moment.replace("+00:00", "Z")), moment)

    def test_an_offset_stamp_is_converted_rather_than_truncated(self):
        self.assertEqual(stamp("2026-09-06T14:11:39-05:00"), "2026-09-06T19:11:39+00:00")

    def test_a_trial_expiry_lands_normalized(self):
        start_trial(self.connection, "sd-grill", "2099-01-01T00:00:00Z")
        self.assertEqual(trials(self.connection)[0]["expires"], "2099-01-01T00:00:00+00:00")

    def test_a_use_timestamp_lands_normalized(self):
        record_skill_use(self.connection, "sd-grill", timestamp="2026-01-01T00:00:00Z")
        row = self.connection.execute("SELECT timestamp FROM skill_use").fetchone()
        self.assertEqual(row["timestamp"], "2026-01-01T00:00:00+00:00")

    def test_the_boundary_holds_across_the_two_spellings(self):
        """The bug this closes, stated as the question the installer asks.

        A trial started at one instant and a use recorded at that same
        instant, spelled the other way. `>=` must count it. Before the
        normalization it did not, because `+00:00` < `Z` put the use before
        its own trial.
        """
        start_trial(self.connection, "sd-grill", "2099-01-01T00:00:00Z")
        started = trials(self.connection)[0]["started"]
        zulu = started.replace("+00:00", "Z")
        self.assertNotEqual(zulu, started)
        record_skill_use(self.connection, "sd-grill", timestamp=zulu)
        self.assertEqual(skill_use_since(self.connection, "sd-grill", started), 1)

    def test_active_trials_compares_a_passed_moment_in_the_same_shape(self):
        start_trial(self.connection, "sd-grill", "2026-10-06T00:00:00+00:00")
        # The same instant as the expiry, spelled the other way. Neither
        # spelling may make an expired trial look active.
        self.assertEqual(active_trials(self.connection, "2026-10-06T00:00:00Z"), [])
        self.assertEqual(active_trials(self.connection, "2026-10-05T23:59:59Z")[0]["skill"], "sd-grill")

    def test_a_naive_stamp_is_refused_and_says_why(self):
        with self.assertRaises(SdDbError) as caught:
            start_trial(self.connection, "sd-grill", "2099-01-01T00:00:00")
        self.assertIn("carries no timezone", str(caught.exception))
        self.assertEqual(trials(self.connection), [])

    def test_something_that_is_not_a_timestamp_is_refused(self):
        with self.assertRaises(SdDbError) as caught:
            record_skill_use(self.connection, "sd-grill", timestamp="thursday")
        self.assertIn("is not an ISO-8601 timestamp", str(caught.exception))


class TrialsAndUse(WriteCase):
    """The two reads the installer makes, and the one removal it performs.

    Criterion 24 renders "the union of the paths plus active trials" and
    criterion 25 removes an expired trial that earned no use. Both are reads
    the library did not have when the trials were first written, which is why
    they arrive here rather than beside `start_trial`.
    """

    def test_an_active_trial_is_the_one_that_has_not_expired(self):
        start_trial(self.connection, "sd-grill", "2099-01-01T00:00:00Z")
        start_trial(self.connection, "sd-paper", "2001-01-01T00:00:00Z")
        self.assertEqual(
            [row["skill"] for row in active_trials(self.connection)], ["sd-grill"]
        )
        # Both are still rows. The installer needs the expired one to remove it,
        # so the unfiltered read has to keep answering with it.
        self.assertEqual(
            [row["skill"] for row in trials(self.connection)], ["sd-grill", "sd-paper"]
        )

    def test_the_moment_is_the_callers_to_choose(self):
        start_trial(self.connection, "sd-grill", "2026-10-06T00:00:00Z")
        self.assertEqual(len(active_trials(self.connection, "2026-09-06T00:00:00Z")), 1)
        self.assertEqual(len(active_trials(self.connection, "2026-11-06T00:00:00Z")), 0)

    def test_a_trial_starting_again_replaces_its_expiry_and_adds_no_row(self):
        start_trial(self.connection, "sd-grill", "2026-10-06T00:00:00Z")
        start_trial(self.connection, "sd-grill", "2026-12-06T00:00:00Z")
        rows = trials(self.connection)
        self.assertEqual(len(rows), 1)
        # Normalized on the way in, so the row is the shape `now()` writes.
        self.assertEqual(rows[0]["expires"], "2026-12-06T00:00:00+00:00")

    def test_use_is_counted_from_the_trials_own_start_and_not_before_it(self):
        # Stamped rather than clocked: `now()` has one-second resolution, and a
        # use written in the same second as the trial began cannot be told from
        # one written after it. The parse of `~/.codex/sessions` needs the same
        # parameter for a real reason, so the test uses the real API.
        record_skill_use(self.connection, "sd-grill", timestamp="2026-01-01T00:00:00Z")
        start_trial(self.connection, "sd-grill", "2099-01-01T00:00:00Z")
        row = trials(self.connection)[0]
        # The use above happened before the trial began. Counted from the
        # trial's own start it is not evidence the trial earned anything.
        self.assertEqual(skill_use_since(self.connection, "sd-grill", row["started"]), 0)
        record_skill_use(self.connection, "sd-grill")
        self.assertEqual(skill_use_since(self.connection, "sd-grill", row["started"]), 1)

    def test_use_by_another_skill_is_not_this_skills_use(self):
        start_trial(self.connection, "sd-grill", "2099-01-01T00:00:00Z")
        record_skill_use(self.connection, "sd-paper")
        row = trials(self.connection)[0]
        self.assertEqual(skill_use_since(self.connection, "sd-grill", row["started"]), 0)

    def test_ending_a_trial_says_whether_there_was_one(self):
        start_trial(self.connection, "sd-grill", "2099-01-01T00:00:00Z")
        self.assertTrue(end_trial(self.connection, "sd-grill"))
        self.assertEqual(trials(self.connection), [])
        # The second call has nothing to remove, and says so rather than
        # letting a caller print "removed" twice.
        self.assertFalse(end_trial(self.connection, "sd-grill"))
