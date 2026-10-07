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
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sd_db import add_note, create_item, initialise, retention, satellite_stale
from sd_db.database import connect, default_path
from sd_db.jobs import cli

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
