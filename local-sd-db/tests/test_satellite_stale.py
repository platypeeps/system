"""sd:2918: a satellite claims an item; the hub alarms when the claim goes quiet.

`docs/work/2026-10-07-satellite-staleness-alarm/`. A claim is a `heartbeat`
row keyed `satellite-claim:<item>`; progress is the newest note, the item's
`updated_at`, and the claimed branch's head on `origin`. One stale episode
sends one alert, recorded as a `watermark` row keyed `satellite-stale:<item>`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sd_db import add_note, create_item, initialise, retention, satellite_stale
from sd_db.database import connect, default_path
from sd_db.writes import upsert_repo
from sd_db.jobs import cli, satellite_cli

from tests.support import commit, git, push, repository
from tests.test_hub import Satellite
from tests.test_wire import Served

T0 = datetime.fromisoformat("2026-10-07T12:00:00+00:00")


def at(hours: float) -> str:
    """An instant `hours` after T0, in the shape `writes.now()` writes."""
    return (T0 + timedelta(hours=hours)).isoformat(timespec="seconds")


def no_branch(_repo, _branch):
    return None


class Database(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "sd.db"
        initialise(self.path)
        self.connection = connect(self.path)
        self.addCleanup(self.connection.close)

    def item(self, status: str = "in_progress", hours: float = 0, **columns) -> int:
        item = create_item(self.connection, kind="task", title="a satellite's task", status=status,
                           created_at=at(hours), **columns)
        self.connection.execute("UPDATE item SET updated_at = ? WHERE id = ?", (at(hours), item))
        # The opening note keeps the real clock; age it with the item.
        self.connection.execute("UPDATE note SET timestamp = ? WHERE item = ?", (at(hours), item))
        self.connection.commit()
        return item

    def note(self, item: int, hours: float) -> None:
        add_note(self.connection, item, "comment", "progress")
        self.connection.execute("UPDATE note SET timestamp = ? WHERE id = (SELECT MAX(id) FROM note)",
                                (at(hours),))
        self.connection.commit()

    def assess(self, hours: float, branch_time=no_branch):
        return satellite_stale.assess(self.connection, at=T0 + timedelta(hours=hours),
                                      hours=3, branch_time=branch_time)

    def state_of(self, verdicts, item: int) -> str:
        return next(v.state for v in verdicts if v.claim.item == item)


class TheClaim(Database):
    def test_a_claim_is_an_open_heartbeat_row_naming_host_and_branch(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", branch="sd-1-x", at=at(0))
        [claim] = satellite_stale.open_claims(self.connection)
        self.assertEqual((claim.item, claim.host, claim.branch, claim.since), (item, "laptop", "sd-1-x", at(0)))
        row = self.connection.execute("SELECT kind, key FROM state WHERE id = ?", (claim.row,)).fetchone()
        self.assertEqual(tuple(row), ("heartbeat", f"satellite-claim:{item}"))

    def test_a_second_claim_replaces_the_first(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="one", at=at(0))
        satellite_stale.claim(self.connection, item, host="two", at=at(1))
        self.assertEqual([c.host for c in satellite_stale.open_claims(self.connection)], ["two"])

    def test_unclaim_resolves_the_claim_and_its_episode(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", at=at(0))
        sent = []
        satellite_stale.alert(self.connection, self.assess(4), send=sent.append)
        self.assertTrue(satellite_stale.unclaim(self.connection, item))
        self.assertEqual(satellite_stale.open_claims(self.connection), [])
        open_rows = self.connection.execute(
            "SELECT COUNT(*) FROM state WHERE key LIKE 'satellite-%' AND resolved_at IS NULL").fetchone()[0]
        self.assertEqual(open_rows, 0)
        self.assertFalse(satellite_stale.unclaim(self.connection, item))

    def test_the_nightly_heartbeat_prune_keeps_the_open_claim(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="one", at=at(0))
        satellite_stale.claim(self.connection, item, host="two", at=at(1))
        retention.compact_heartbeats(self.connection)
        self.assertEqual([c.host for c in satellite_stale.open_claims(self.connection)], ["two"])

    def test_the_prune_keeps_an_open_claim_written_by_a_clock_behind(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="one", at=at(1))
        satellite_stale.claim(self.connection, item, host="two", at=at(0))
        retention.compact_heartbeats(self.connection)
        self.assertEqual([c.host for c in satellite_stale.open_claims(self.connection)], ["two"])

    def test_a_replacement_without_a_branch_keeps_the_stored_branch(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", branch="sd-1-x", at=at(0))
        satellite_stale.claim(self.connection, item, host="laptop", quiet_until=at(6), at=at(1))
        [held] = satellite_stale.open_claims(self.connection)
        self.assertEqual((held.branch, held.quiet_until), ("sd-1-x", at(6)))
        satellite_stale.claim(self.connection, item, host="laptop", branch="sd-1-y", at=at(2))
        self.assertEqual(satellite_stale.open_claims(self.connection)[0].branch, "sd-1-y")

    def test_a_second_writer_waits_for_the_whole_replacement(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="one", at=at(0))
        other = connect(self.path, busy_timeout=0)
        self.addCleanup(other.close)
        real = satellite_stale.record_state
        raced = []

        def insert_then_race(*args, **kwargs):
            if not raced:
                raced.append("tried")
                try:
                    satellite_stale.claim(other, item, host="three", at=at(1))
                except sqlite3.OperationalError as error:
                    raced.append(str(error))
            return real(*args, **kwargs)

        with mock.patch.object(satellite_stale, "record_state", side_effect=insert_then_race):
            satellite_stale.claim(self.connection, item, host="two", at=at(1))
        self.assertEqual(len(raced), 2, "the second writer got in while the first held its claim open")
        self.assertIn("locked", raced[1])
        self.assertEqual([c.host for c in satellite_stale.open_claims(self.connection)], ["two"])

    def test_a_failed_replacement_keeps_the_old_claim(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="one", at=at(0))
        with mock.patch.object(satellite_stale, "record_state", side_effect=sqlite3.OperationalError("disk")):
            with self.assertRaises(sqlite3.OperationalError):
                satellite_stale.claim(self.connection, item, host="two", at=at(1))
        self.assertEqual([c.host for c in satellite_stale.open_claims(self.connection)], ["one"])

    def test_one_malformed_claim_does_not_stop_the_others(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", at=at(0))
        for key, body in (("satellite-claim:x", "{}"), ("satellite-claim:8", "[]"), ("satellite-claim:9", "null"),
                          ("satellite-claim:10", '{"since": 123}'), ("satellite-claim:11", "not json")):
            self.connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('heartbeat', ?, ?, ?)",
                                    (key, at(0), body))
        self.connection.commit()
        self.assertEqual([c.item for c in satellite_stale.open_claims(self.connection)], [item])
        self.assertEqual(self.state_of(self.assess(4), item), "stale")

    def test_a_claim_on_no_item_is_refused(self):
        with self.assertRaises(satellite_stale.ClaimRefused):
            satellite_stale.claim(self.connection, 999, host="laptop")


class TheProgress(Database):
    def test_a_note_two_hours_old_is_fresh(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", at=at(0))
        self.note(item, 3)
        self.assertEqual(self.state_of(self.assess(5), item), "fresh")

    def test_every_signal_four_hours_old_is_stale(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", at=at(0))
        self.note(item, 0)
        [verdict] = self.assess(4)
        self.assertEqual((verdict.state, verdict.progress, verdict.source), ("stale", at(0), "note"))

    def test_updated_at_counts(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", at=at(0))
        self.connection.execute("UPDATE item SET updated_at = ? WHERE id = ?", (at(2), item))
        self.assertEqual(self.state_of(self.assess(4), item), "fresh")

    def test_a_pushed_commit_keeps_a_claim_fresh_when_notes_are_old(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", branch="sd-1-x", at=at(0))
        self.note(item, 0)
        asked = []

        def branch_time(repo, branch):
            asked.append(branch)
            return at(4)

        [verdict] = self.assess(5, branch_time)
        self.assertEqual((verdict.state, verdict.source, asked), ("fresh", "branch", ["sd-1-x"]))

    def test_the_item_branch_stands_in_for_an_unnamed_claim_branch(self):
        item = self.item(branch="sd-2-y")
        satellite_stale.claim(self.connection, item, host="laptop", at=at(0))
        asked = []
        self.assess(5, lambda repo, branch: asked.append(branch))
        self.assertEqual(asked, ["sd-2-y"])

    def test_blocked_ready_to_send_and_done_are_skipped(self):
        for status in ("blocked", "ready_to_send", "done"):
            item = self.item(status=status)
            satellite_stale.claim(self.connection, item, host="laptop", at=at(0))
            self.assertEqual(self.state_of(self.assess(9), item), "skipped", status)

    def test_quiet_until_holds_until_its_time(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", quiet_until=at(6), at=at(0))
        self.assertEqual(self.state_of(self.assess(5), item), "quiet")
        self.assertEqual(self.state_of(self.assess(7), item), "stale")


class TheEpisode(Database):
    def test_one_stale_episode_sends_one_alert(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", at=at(0))
        sent = []
        first = satellite_stale.alert(self.connection, self.assess(4), send=sent.append)
        second = satellite_stale.alert(self.connection, self.assess(5), send=sent.append)
        self.assertEqual((len(first), len(second), len(sent)), (1, 0, 1))
        self.assertIn(f"sd:{item}", sent[0].title)
        self.assertIn("unclaim", sent[0].body)

    def test_progress_then_a_new_stall_sends_again(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", at=at(0))
        sent = []
        satellite_stale.alert(self.connection, self.assess(4), send=sent.append)
        self.note(item, 5)
        satellite_stale.alert(self.connection, self.assess(6), send=sent.append)
        satellite_stale.alert(self.connection, self.assess(9), send=sent.append)
        self.assertEqual(len(sent), 2)

    def test_quiet_and_skipped_claims_send_nothing(self):
        quiet = self.item()
        satellite_stale.claim(self.connection, quiet, host="laptop", quiet_until=at(9), at=at(0))
        blocked = self.item(status="blocked")
        satellite_stale.claim(self.connection, blocked, host="laptop", at=at(0))
        sent = []
        satellite_stale.alert(self.connection, self.assess(5), send=sent.append)
        self.assertEqual(sent, [])

    def test_a_failed_send_writes_no_watermark_so_the_next_run_retries(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", at=at(0))

        def fail(_alert):
            raise satellite_stale.SendFailed("ntfy down")

        with self.assertRaises(satellite_stale.SendFailed):
            satellite_stale.alert(self.connection, self.assess(4), send=fail)
        sent = []
        satellite_stale.alert(self.connection, self.assess(4), send=sent.append)
        self.assertEqual(len(sent), 1)

    def test_a_dropped_signal_does_not_alert_again_on_older_progress(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", branch="sd-1-x", at=at(0))
        sent = []

        def pushed(_repo, _branch):
            return at(4)

        satellite_stale.alert(self.connection, self.assess(8, branch_time=pushed), send=sent.append)
        satellite_stale.alert(self.connection, self.assess(9), send=sent.append)
        satellite_stale.alert(self.connection, self.assess(10, branch_time=pushed), send=sent.append)
        self.assertEqual(len(sent), 1, "a failed fetch exposed older progress and alerted again")

    def test_a_failed_watermark_replacement_keeps_the_old_episode(self):
        item = self.item()
        satellite_stale.claim(self.connection, item, host="laptop", at=at(0))
        satellite_stale.alert(self.connection, self.assess(4), send=lambda _alert: None)
        self.note(item, 5)
        with mock.patch.object(satellite_stale, "record_state", side_effect=sqlite3.OperationalError("disk")):
            with self.assertRaises(sqlite3.OperationalError):
                satellite_stale.alert(self.connection, self.assess(9), send=lambda _alert: None)
        open_rows = self.connection.execute(
            "SELECT body FROM state WHERE key = ? AND resolved_at IS NULL", (f"satellite-stale:{item}",)).fetchall()
        self.assertEqual([row[0] for row in open_rows], [at(0)])


class TheWindow(unittest.TestCase):
    def test_the_default_window_is_seven_to_twenty_two(self):
        self.assertEqual(satellite_stale.parse_window(None), (7, 22))

    def test_inside_and_outside(self):
        window = (7, 22)
        self.assertTrue(satellite_stale.in_window(datetime(2026, 10, 7, 7, 0), window))
        self.assertTrue(satellite_stale.in_window(datetime(2026, 10, 7, 21, 59), window))
        self.assertFalse(satellite_stale.in_window(datetime(2026, 10, 7, 22, 0), window))
        self.assertFalse(satellite_stale.in_window(datetime(2026, 10, 7, 3, 0), window))

    def test_a_malformed_window_is_refused(self):
        for text in ("7", "22-7", "a-b", "7-25"):
            with self.assertRaises(satellite_stale.ClaimRefused, msg=text):
                satellite_stale.parse_window(text)


class TheRunBudget(unittest.TestCase):
    """One run's fetches, reads and sends fit inside the cron job's limit
    (sd:2918 round 5): slow or failed I/O must not spend the time an alert
    needs."""

    def test_fetches_stop_at_the_budget_and_read_stored_refs(self):
        calls = []

        def hung(checkout, branch, *, bound, fetch=True):
            calls.append((branch, fetch))
            if fetch:
                time.sleep(min(bound, 0.3))
                return None
            return at(3)

        reader = satellite_stale.BranchReader(budget=0.5, fetch=hung)
        started = time.monotonic()
        with mock.patch.object(satellite_stale.paths, "disk", side_effect=Path):
            stamps = [reader("repo", f"sd-{n}-x") for n in range(10)]
        self.assertLess(time.monotonic() - started, 1.5, "fetches ran past the budget")
        self.assertEqual(stamps, [at(3)] * 10, "a branch not fetched reads the stored ref")
        self.assertLessEqual(sum(1 for _, fetched in calls if fetched), 3)
        self.assertEqual(reader.unfetched, {("repo", f"sd-{n}-x") for n in range(10)})

    def test_a_fetched_branch_is_not_named(self):
        reader = satellite_stale.BranchReader(budget=5, fetch=lambda *_a, **_k: at(3))
        with mock.patch.object(satellite_stale.paths, "disk", side_effect=Path):
            self.assertEqual(reader("repo", "sd-1-x"), at(3))
        self.assertEqual(reader.unfetched, set())

    def test_the_alert_names_a_branch_read_without_a_fetch(self):
        claim = satellite_stale.Claim(1, 7, "laptop", "sd-7-x", at(0), None)
        verdict = satellite_stale.Verdict(claim, "t", "in_progress", "repo", "sd-7-x", "stale", at(0), "claim",
                                          4.0, False, unfetched=True)
        self.assertIn("not fetched", satellite_stale.describe(verdict).body)

    def test_a_hung_notifier_is_cut_at_its_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "notify.sh"
            script.write_text("#!/bin/sh\nsleep 30\n")
            send = satellite_cli._sender(str(script), deadline=time.monotonic() + 1)
            started = time.monotonic()
            with self.assertRaises(satellite_stale.SendFailed):
                send(satellite_stale.Alert(7, "title", "body"))
            self.assertLess(time.monotonic() - started, 5)

    def test_no_send_starts_after_the_run_deadline(self):
        send = satellite_cli._sender("/nonexistent/notify.sh", deadline=time.monotonic() - 1)
        with self.assertRaises(satellite_stale.SendFailed) as raised:
            send(satellite_stale.Alert(7, "title", "body"))
        self.assertIn("deadline", str(raised.exception))

    def test_the_budgets_fit_inside_the_cron_limit(self):
        job = (Path(__file__).resolve().parents[2] / "local-cron-jobs" / "examples" / "satellite-stale.job").read_text()
        limit = int(next(line for line in job.splitlines() if line.startswith("JOB_TIMEOUT=")).split('"')[1][:-1]) * 60
        self.assertLess(satellite_stale.RUN_BUDGET, limit)
        self.assertLess(satellite_stale.FETCH_BUDGET, satellite_stale.READ_BUDGET)
        self.assertLess(satellite_stale.READ_BUDGET, satellite_stale.RUN_BUDGET)
        self.assertLess(satellite_stale.STATUS_BUDGET, 30, "the health check's status bound")


class TheBranchTime(unittest.TestCase):
    def test_reads_the_pushed_head_committer_date_from_a_local_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = repository(root / "work", bare=root / "origin.git")
            git(work, "checkout", "-qb", "sd-1-x")
            (work / "f").write_text("x")
            with mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": "2026-10-07T14:00:00+00:00"}):
                commit(work)
            push(work, "sd-1-x")
            hub = root / "hub"
            git(root, "clone", "-q", str(root / "origin.git"), str(hub))
            stamp = satellite_stale.fetch_branch_time(hub, "sd-1-x")
            self.assertEqual(datetime.fromisoformat(stamp), datetime.fromisoformat("2026-10-07T14:00:00+00:00"))
            git(work, "commit", "-q", "--allow-empty", "-m", "later")
            push(work, "sd-1-x")
            self.assertEqual(satellite_stale.fetch_branch_time(hub, "sd-1-x", fetch=False), stamp,
                             "without a fetch, the ref the last fetch left")
            self.assertIsNone(satellite_stale.fetch_branch_time(hub, "no-such-branch"))
            self.assertIsNone(satellite_stale.fetch_branch_time(root / "missing", "sd-1-x"))


class Cli(Satellite):
    """The verbs through `sd_db.jobs.cli`, with `HOME` under the test."""

    def run_cli(self, *argv: str, env: dict | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        variables = {"HOME": str(self.home), "XDG_DATA_HOME": str(self.home / ".local" / "share"), **(env or {})}
        with mock.patch.dict(os.environ, variables), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def hub_database(self) -> Path:
        initialise(home=self.home)
        return default_path(self.home)

    def seed(self, status: str = "in_progress", stale: bool = True) -> int:
        if not default_path(self.home).exists():
            self.hub_database()
        connection = connect(default_path(self.home))
        try:
            when = at(0) if stale else datetime.now().astimezone().isoformat(timespec="seconds")
            item = create_item(connection, kind="task", title="t", status=status, created_at=when)
            connection.execute("UPDATE item SET updated_at = ? WHERE id = ?", (when, item))
            connection.execute("UPDATE note SET timestamp = ? WHERE item = ?", (when, item))
            connection.commit()
            satellite_stale.claim(connection, item, host="laptop", at=at(0))
        finally:
            connection.close()
        return item


class TheClaimVerbs(Cli):
    def test_claim_on_the_hub_is_refused(self):
        self.hub_database()
        code, _, err = self.run_cli("claim", "1")
        self.assertEqual(code, 1)
        self.assertIn("claim runs on a satellite", err)

    def test_claim_and_unclaim_from_a_satellite_cross_the_wire(self):
        served = Served(self.root)
        self.addCleanup(served.stop)
        raw = connect(served.database)
        try:
            item = create_item(raw, kind="task", title="t", status="in_progress")
        finally:
            raw.close()
        self.name_hub(served.port, served.token_file)
        code, out, err = self.run_cli("claim", str(item), "--branch", "sd-1-x", "--host", "laptop")
        self.assertEqual(code, 0, err)
        self.assertIn(f"claimed sd:{item}", out)
        hub = sqlite3.connect(served.database)
        try:
            body = hub.execute("SELECT body FROM state WHERE key = ? AND resolved_at IS NULL",
                               (f"satellite-claim:{item}",)).fetchone()[0]
        finally:
            hub.close()
        self.assertEqual((json.loads(body)["host"], json.loads(body)["branch"]), ("laptop", "sd-1-x"))
        code, out, _ = self.run_cli("unclaim", str(item))
        self.assertEqual((code, f"unclaimed sd:{item}" in out), (0, True))

    def test_claim_needs_an_item_number(self):
        self.hub_database()
        code, _, err = self.run_cli("claim", "x")
        self.assertEqual(code, 1)
        self.assertIn("item number", err)


class TheStatusVerb(Cli):
    def test_no_database_is_nothing_to_check(self):
        code, out, _ = self.run_cli("satellite-stale", "status")
        self.assertEqual(code, 3, out)

    def test_no_open_claim_is_nothing_to_check(self):
        self.hub_database()
        self.assertEqual(self.run_cli("satellite-stale", "status")[0], 3)

    def test_a_stale_claim_is_broken(self):
        item = self.seed()
        code, out, _ = self.run_cli("satellite-stale", "status")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("satellite-stale: 1 stale"), out)
        self.assertIn(f"sd:{item}", out)

    def test_a_fresh_claim_is_healthy(self):
        self.seed(stale=False)
        self.assertEqual(self.run_cli("satellite-stale", "status")[0], 0)

    def test_on_a_satellite_status_is_nothing_to_check(self):
        self.name_hub(1)
        code, out, _ = self.run_cli("satellite-stale", "status")
        self.assertEqual(code, 3)
        self.assertIn("runs on the hub", out)


class TheNotifyVerb(Cli):
    def notifier(self) -> tuple[Path, Path]:
        record = self.root / "sent.log"
        script = self.root / "notify.sh"
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{record}"\nexit "${{NOTIFY_EXIT:-0}}"\n')
        return script, record

    def test_notify_sends_once_per_episode_inside_the_window(self):
        item = self.seed()
        script, record = self.notifier()
        env = {"SD_NOTIFY": str(script), "SD_SATELLITE_STALE_WINDOW": "0-24"}
        self.assertEqual(self.run_cli("satellite-stale", "--notify", env=env)[0], 0)
        self.assertEqual(self.run_cli("satellite-stale", "--notify", env=env)[0], 0)
        lines = record.read_text().splitlines()
        self.assertEqual(lines.count("-t"), 1)
        self.assertIn(f"Satellite stalled: sd:{item}", lines)
        self.assertIn("ntfy,email", lines)

    def test_notify_outside_the_window_sends_nothing(self):
        self.seed()
        script, record = self.notifier()
        hour = datetime.now().astimezone().hour
        window = f"{(hour + 1) % 24}-{(hour + 1) % 24 + 1}" if hour < 22 else "1-2"
        code, out, _ = self.run_cli("satellite-stale", "--notify",
                                    env={"SD_NOTIFY": str(script), "SD_SATELLITE_STALE_WINDOW": window})
        self.assertEqual(code, 0)
        self.assertIn("outside the alert window", out)
        self.assertFalse(record.exists())

    def test_a_failed_delivery_exits_one(self):
        self.seed()
        script, _ = self.notifier()
        code, _, err = self.run_cli("satellite-stale", "--notify", env={
            "SD_NOTIFY": str(script), "SD_SATELLITE_STALE_WINDOW": "0-24", "NOTIFY_EXIT": "1"})
        self.assertEqual(code, 1)
        self.assertIn("not delivered", err)

    def test_notify_without_a_notifier_is_refused(self):
        self.seed()
        env = {"SD_SATELLITE_STALE_WINDOW": "0-24"}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SD_NOTIFY", None)
            code, _, err = self.run_cli("satellite-stale", "--notify", env=env)
        self.assertEqual(code, 1)
        self.assertIn("SD_NOTIFY", err)

    def test_the_report_and_notify_refuse_on_a_satellite(self):
        self.name_hub(1)
        for argv in (("satellite-stale",), ("satellite-stale", "--notify")):
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("runs on the sd hub only", err)

    def test_the_report_lists_each_claim(self):
        item = self.seed()
        code, out, _ = self.run_cli("satellite-stale")
        self.assertEqual(code, 0)
        self.assertRegex(out, rf"^stale\s+sd:{item}\s+laptop")


class TheRunDeadline(Cli):
    """Every git call in one run sits inside one budget, so no number of
    claims delays the first send (sd:2918 round 7). Budgets are scaled down."""

    def fifty_claims(self) -> list[int]:
        self.hub_database()
        connection = connect(default_path(self.home))
        items = []
        try:
            upsert_repo(connection, "~/repos/example", status_source="row")
            for n in range(50):
                item = create_item(connection, kind="task", title=f"t{n}", status="in_progress",
                                   created_at=at(0), repo="~/repos/example")
                connection.execute("UPDATE item SET updated_at = ? WHERE id = ?", (at(0), item))
                connection.execute("UPDATE note SET timestamp = ? WHERE item = ?", (at(0), item))
                connection.commit()
                satellite_stale.claim(connection, item, host="laptop", branch=f"sd-{item}-x", at=at(0))
                items.append(item)
        finally:
            connection.close()
        return items

    @staticmethod
    def hung(_checkout, _branch, *, bound, fetch=True):
        time.sleep(bound)
        return None

    def test_fifty_hung_claims_send_before_the_budget_and_are_all_named(self):
        items = self.fifty_claims()
        hung = self.hung
        sent = []
        real = satellite_cli._sender

        def timed(notifier, *, deadline):
            send = real(notifier, deadline=deadline)

            def record(alert):
                sent.append(time.monotonic())
                send(alert)

            return record

        script = self.root / "notify.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        budgets = {"FETCH_BUDGET": 0.4, "READ_BUDGET": 0.8, "FETCH_BOUND": 0.2, "READ_BOUND": 0.1, "RUN_BUDGET": 60}
        started = time.monotonic()
        with mock.patch.multiple(satellite_stale, fetch_branch_time=hung, **budgets), \
                mock.patch.object(satellite_cli, "_sender", timed):
            code, out, err = self.run_cli("satellite-stale", "--notify", env={
                "SD_NOTIFY": str(script), "SD_SATELLITE_STALE_WINDOW": "0-24"})
        self.assertEqual(code, 0, err)
        self.assertTrue(sent, out)
        self.assertLess(sent[0] - started, 0.8 + 1.0, "the first send waited on branch reads past the budget")
        self.assertEqual(len(sent), 50)
        for item in items:
            self.assertRegex(out, rf"sd:{item}\s")
        self.assertIn("(not read)", out)

    def test_status_reads_end_inside_the_health_check_bound(self):
        self.fifty_claims()
        started = time.monotonic()
        with mock.patch.multiple(satellite_stale, fetch_branch_time=self.hung, STATUS_BUDGET=0.5, READ_BOUND=0.1):
            code, out, err = self.run_cli("satellite-stale", "status", env={})
        self.assertEqual(code, 1, out + err)
        self.assertLess(time.monotonic() - started, 0.5 + 1.0, "status read past its budget")

    def test_a_fetch_and_its_read_share_one_bound(self):
        calls = []

        def slow(command, *, timeout, **_):
            calls.append(command[3])
            time.sleep(timeout)
            return subprocess.CompletedProcess(command, 0, "", "")

        started = time.monotonic()
        with mock.patch.object(satellite_stale.subprocess, "run", slow):
            satellite_stale.fetch_branch_time(self.root, "sd-1-x", bound=0.3)
        self.assertEqual(calls, ["fetch"], "the read ran after the fetch spent the bound")
        self.assertLess(time.monotonic() - started, 0.45)


class TheThreshold(Cli):
    def test_the_hours_variable_moves_the_threshold(self):
        self.seed()
        self.assertEqual(self.run_cli("satellite-stale", "status",
                                      env={"SD_SATELLITE_STALE_HOURS": "100000"})[0], 0)

    def test_a_malformed_threshold_is_refused(self):
        self.seed()
        for text in ("soon", "nan", "inf", "-1"):
            code, _, err = self.run_cli("satellite-stale", "status", env={"SD_SATELLITE_STALE_HOURS": text})
            self.assertEqual(code, 1, text)
            self.assertIn("SD_SATELLITE_STALE_HOURS", err)


if __name__ == "__main__":
    unittest.main()
