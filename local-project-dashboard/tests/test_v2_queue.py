"""The v2 Queue page (sd:2585).

What this slice promises: `/api/queue` draws each managed repository's lane from `sd-ship lane list`, one row per
item in one of six states (merging, next, building, blocked, cancelled, landed), with load5 and the gate slots in the header.
The only write is `/api/queue/move`: it runs `sd-ship lane move|hold|release|cancel|retry` with exactly the page's item and
place, under the session cookie, CSRF and Origin checks, and refuses a revision the queue has moved past. Retry and Approve
(`retry --manual`) show on blocked rows only when `sd-ship lane retry --help` names `--expected-head` (sd:3012); Approve
only on a manual repo. Both carry the head the row showed. Release on a running row runs `lane cancel` with the claim the
row showed as `--expected-claim` (sd:3337), and a claim that moved on since is a 409.
Re-queue on a cancelled row runs `lane retry --expected-head` with the head the row showed (sd:3398), shows only when
`lane retry --help` names a cancelled entry (pack sd:3389), and runs nothing for an item whose newest entry is not cancelled.

`sd-ship` and `sd` are stubs on PATH that answer from a fixture and log their arguments; no test reaches a real lane.
One test runs the pinned pack's own `sd-ship` on a queue under a temporary `SD_LANE_ROOT`, so the order the page asks
for is the order `lane list` then reports. `queue.js` runs under JavaScriptCore (osascript) against the stand-in shell
`test_v2_tasks` defines. The look at 375 px is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import pack_bin, queue_screen, v2
from sd_db import upsert_repo

from support import NOW, ScreenCase
from test_v2_read import READ_SHELL
from test_v2_registry import GROUP_BLOCK, Registers
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
QUEUE_JS = (V2 / "static" / "queue.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
CLOCK = queue_screen._when(NOW)

STUB = """#!/bin/sh
# A stand-in for sd-ship and sd: log the arguments, answer from the fixture.
printf '%s\\n' "$*" >> "$QUEUE_LOG"
case "$*" in
  *"lane list") if [ -f "$QUEUE_FIXTURE.$(basename "$2")" ]; then cat "$QUEUE_FIXTURE.$(basename "$2")"; else cat "$QUEUE_FIXTURE"; fi ;;
  "lane retry --help") if [ -n "$QUEUE_NO_RETRY" ]; then echo "invalid choice: 'retry'" >&2; exit 2; fi
     if [ -n "$QUEUE_OLD_RETRY" ]; then echo "usage: sd-ship lane retry [--manual] item"; else echo "usage: sd-ship lane retry [--manual] [--expected-head EXPECTED_HEAD] item"; fi
     if [ -z "$QUEUE_OLD_REQUEUE$QUEUE_OLD_RETRY" ]; then echo "  --expected-head  ...; a cancelled entry needs it (sd:3389)"; fi ;;
  "gate status --json") printf '{"slots": 4, "holders": [{"slot": 1}], "waiters": [], "load": [1, 2, 3]}\\n' ;;
  *) if [ -n "$QUEUE_SLEEP" ]; then exec sleep "$QUEUE_SLEEP"; fi
     if [ -n "$QUEUE_CRASH" ]; then echo "Traceback: $QUEUE_CRASH" >&2; exit 1; fi
     if [ -n "$QUEUE_REFUSE" ]; then printf '{"ok": false, "error": "%s", "code": "%s"}\\n' "$QUEUE_REFUSE" "$QUEUE_CODE"; exit "${QUEUE_EXIT:-3}"; fi
     printf '{"ok": true}\\n' ;;
esac
"""


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


def numbered(entries):
    """`entries` as a pack with `enqueue_seq` lists them before any move: each one's number is its place (sd:3419)."""
    return [{**row, "enqueue_seq": seq} for seq, row in enumerate(entries, 1)]


class Lane:
    """A registered git checkout, its lane folder and a stubbed `sd-ship` on PATH that lists `entries`."""

    def setUp(self):
        super().setUp()
        work = Path(self.tmp.name)
        self.checkout = work / "system"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q", "-b", "main")
        git(self.checkout, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q", "--allow-empty",
            "-m", "Dashboard: a page (#161)")
        self.merged = git(self.checkout, "rev-parse", "HEAD")
        upsert_repo(self.connection, str(self.checkout), remote="git@example.invalid:x.git", managed=1)
        self.lane = work / "lanes" / "system" / "lane"
        (self.lane / "logs").mkdir(parents=True)
        bin_dir = work / "bin"
        bin_dir.mkdir()
        for name in ("sd-ship", "sd"):
            (bin_dir / name).write_text(STUB)
            (bin_dir / name).chmod(0o755)
        self.log, self.fixture = work / "calls.log", work / "list.json"
        self.log.write_text("")
        patch = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "QUEUE_LOG": str(self.log),
                                             "QUEUE_FIXTURE": str(self.fixture), "TZ": "UTC"})
        patch.start()
        self.addCleanup(time.tzset)
        self.addCleanup(patch.stop)
        time.tzset()
        self.list(self.entries())
        prepared = self.lane / "logs" / "prepare-3-20260906T115500.log"
        prepared.write_text("prepare output")
        os.utime(prepared, (CLOCK - 300, CLOCK - 300))
        for name, text, age in (("gate-queue-notes-1.log", "", 120), ("gate-sd1775-2.log", json.dumps(
                {"status": "success", "summary": "sd-check pass (check pass)", "head": "d" * 40}), 600),
                ("gate-stale.log", "", 7 * 3600)):
            (self.lane / name).write_text(text)
            os.utime(self.lane / name, (CLOCK - age, CLOCK - age))

    def list(self, entries):
        self.fixture.write_text(json.dumps({"ok": True, "queue": str(self.lane / "queue" / "queue.json"), "entries": entries}))

    def entries(self):
        """One entry per state, plus history the page must leave out."""
        return [
            {"item": 9, "title": "Old failure, since retried", "status": "failed", "step": "prepare", "reason": "gate failed",
             "finished_at": "2026-09-06T08:00:00Z"},
            {"item": 7, "title": "Land the hub docs", "status": "merged", "merge_commit": self.merged,
             "finished_at": "2026-09-06T10:00:00Z"},
            {"item": 6, "title": "Landed yesterday", "status": "merged", "merge_commit": self.merged,
             "finished_at": "2026-09-05T10:00:00Z"},
            {"item": 5, "title": "Prepared only", "status": "prepared", "reason": "queued without --manual; merge by hand",
             "expected_head": "5" * 40, "finished_at": "2026-09-06T09:00:00Z"},
            {"item": 4, "title": "Moved head", "status": "skipped", "reason": "the worktree's HEAD moved from the queued head",
             "expected_head": "4" * 40, "finished_at": "2026-09-06T09:30:00Z"},
            {"item": 3, "title": "Merge the slice", "status": "running", "expected_head": "a" * 40, "id": "e-3",
             "started_at": "2026-09-06T11:50:00Z", "step": "merge", "lease_until": "2026-09-06T13:20:00Z",
             "holder": {"host": "hub-1", "pid": 4242, "token": "claim-token-3"}, "enqueued_on": "hub-1",
             "log_host": "hub-1"},
            {"item": 9, "title": "Old failure, since retried", "status": "pending", "expected_head": "b" * 40,
             "speculation": {"status": "success", "summary": "sd-check pass"}},
            {"item": 12, "title": "Queue page", "status": "pending", "expected_head": "c" * 40, "held": True},
            {"item": 1, "title": "Cancelled", "status": "cancelled", "expected_head": "1" * 40, "branch": "b1",
             "finished_at": "2026-09-06T07:00:00Z"},
        ]

    def calls(self):
        return [line.split() for line in self.log.read_text().splitlines()]


class TheRows(Lane, ScreenCase):
    """`queue_screen.document` from the fixture `lane list`, the gate log and `sd gate status`."""

    def doc(self):
        return queue_screen.document(self.connection, now=NOW)

    def test_each_of_the_six_states_has_its_row(self):
        rows = self.doc()["lanes"][0]["rows"]
        self.assertEqual([(row["state"], row.get("item") or row.get("builder")) for row in rows], [
            ("merging", 3), ("next", 9), ("next", 12), ("building", "queue-notes-1"), ("building", "sd1775-2"),
            ("blocked", 5), ("blocked", 4), ("cancelled", 1), ("landed", 7)])

    def worktrees(self):
        """The fixture with the worktree `lane enqueue` records on the next and blocked rows (sd:5064)."""
        self.list([{**e, "worktree": f"/work/sd-{e['item']}"} if e["status"] in ("pending", "prepared") else e
                   for e in self.entries()])

    def test_a_lane_row_carries_the_worktree_its_entry_recorded(self):
        self.worktrees()
        rows = self.doc()["lanes"][0]["rows"]
        self.assertEqual([(row["state"], row.get("worktree")) for row in rows], [
            ("merging", None), ("next", "/work/sd-9"), ("next", "/work/sd-12"), ("building", None), ("building", None),
            ("blocked", "/work/sd-5"), ("blocked", None), ("cancelled", None), ("landed", None)])

    def test_the_badge_counts_the_blocked_rows(self):
        # sd:3446: the read's rule, which every page's rail shows from the page's last read.
        self.assertEqual(self.doc()["attention"], {"state": "caution", "n": 2, "what": "blocked"})

    def test_merging_names_its_step_host_claim_age_and_lease(self):
        merging = self.doc()["lanes"][0]["rows"][0]
        self.assertEqual((merging["step"], merging["host"], merging["claim_age"], merging["lease_until"], merging["head"]),
                         ("merge", "hub-1", 600, "2026-09-06T13:20:00Z", "a" * 12))

    def test_the_step_comes_from_the_entry_not_a_prepare_log(self):
        # sd:3284: the entry's `step` replaces the probe for a prepare log newer than the claim. The fixture's
        # prepare log for sd:3 is newer than its claim, which the probe read as `merge`.
        entries = self.entries()
        entries[5] = {**entries[5], "step": "prepare"}
        self.list(entries)
        self.assertEqual(self.doc()["lanes"][0]["rows"][0]["step"], "prepare")

    def test_a_holder_on_another_host_is_named(self):
        # Pack sd:3283 runs an entry on any host; the hub's page shows the satellite that holds it.
        entries = self.entries()
        entries[5] = {**entries[5], "holder": {"host": "build-2", "pid": 77, "token": "t"}, "log_host": "build-2",
                      "step": "prepare"}
        self.list(entries)
        merging = self.doc()["lanes"][0]["rows"][0]
        self.assertEqual((merging["host"], merging["step"]), ("build-2", "prepare"))

    def test_a_running_entry_without_holder_or_step_reads_unknown_not_guessed(self):
        # An entry an earlier pack wrote: no holder, no step, no lease. The page shows each as not recorded.
        entries = self.entries()
        entries[5] = {key: value for key, value in entries[5].items() if key not in ("holder", "step", "lease_until", "log_host")}
        self.list(entries)
        merging = self.doc()["lanes"][0]["rows"][0]
        self.assertEqual((merging["host"], merging["step"], merging["lease_until"], merging["claim_age"]), (None, None, None, 600))

    def test_the_claim_token_reaches_the_page_only_as_the_running_rows_claim(self):
        # sd:3337: Release sends back the claim the row showed, `<holder token>:<step>`, for `--expected-claim`.
        doc = self.doc()
        self.assertEqual(doc["lanes"][0]["rows"][0]["claim"], "claim-token-3:merge")
        self.assertEqual(json.dumps(doc).count("claim-token-3"), 1)

    def test_a_running_entry_without_a_token_or_step_has_no_claim(self):
        # An entry an earlier pack wrote names no claim, so there is nothing a Release could bind to.
        for drop in ("holder", "step"):
            entries = self.entries()
            entries[5] = {key: value for key, value in entries[5].items() if key != drop}
            self.list(entries)
            self.assertIsNone(self.doc()["lanes"][0]["rows"][0]["claim"], drop)

    def test_every_managed_repository_has_a_lane_even_without_a_lane_folder(self):
        # sd:3284: the queue lives in the hub database, so a lane hosted elsewhere has no folder on this machine.
        other = Path(self.tmp.name) / "satellite-only"
        other.mkdir()
        upsert_repo(self.connection, str(other), remote="git@example.invalid:y.git", managed=1)
        self.connection.execute("UPDATE repo SET lane_host = 'build-2' WHERE path = ?", (str(other),))
        unmanaged = Path(self.tmp.name) / "not-managed"
        unmanaged.mkdir()
        upsert_repo(self.connection, str(unmanaged), remote="git@example.invalid:z.git", managed=0)
        self.connection.commit()
        Path(f"{self.fixture}.satellite-only").write_text(json.dumps({"ok": True, "queue": str(
            Path(self.tmp.name) / "lanes" / "satellite-only" / "lane" / "queue" / "queue.json"), "entries": [
            {"item": 21, "title": "Run elsewhere", "status": "pending", "expected_head": "2" * 40}]}))
        doc = self.doc()
        self.assertEqual([(lane["repo"], lane["host"]) for lane in doc["lanes"]], [("satellite-only", "build-2"), ("system", "hub")])
        self.assertEqual([row["item"] for row in doc["lanes"][0]["rows"]], [21])
        self.assertEqual(doc["managed"], 2)
        self.assertNotIn(["-C", str(unmanaged), "lane", "list"], self.calls())

    def test_a_managed_repository_with_no_checkout_here_is_named_not_dropped(self):
        gone = Path(self.tmp.name) / "gone"
        upsert_repo(self.connection, str(gone), remote="git@example.invalid:g.git", managed=1)
        self.connection.commit()
        doc = self.doc()
        self.assertEqual(([lane["repo"] for lane in doc["lanes"]], [p["repo"] for p in doc["problems"]]),
                         (["system"], [str(gone)]))

    def test_next_rows_carry_order_gate_head_and_hold(self):
        first, second = self.doc()["lanes"][0]["rows"][1:3]
        self.assertEqual((first["position"], first["gate"], first["gate_summary"], first["head"], first["held"]),
                         (1, "success", "sd-check pass", "b" * 12, False))
        self.assertEqual((second["position"], second["gate"], second["held"]), (2, "not gated", True))

    def test_building_reads_the_builder_gate_logs(self):
        running, done = self.doc()["lanes"][0]["rows"][3:5]
        self.assertEqual((running["gate"], running["summary"]), ("running", None))
        self.assertEqual((done["gate"], done["summary"], done["head"]), ("pass", "sd-check pass (check pass)", "d" * 12))

    def test_blocked_names_who_acts_and_why(self):
        prepared, skipped = self.doc()["lanes"][0]["rows"][5:7]
        self.assertEqual((prepared["who"], prepared["reason"]), ("operator", "queued without --manual; merge by hand"))
        self.assertEqual((skipped["who"], skipped["status"]), ("builder", "skipped"))
        # sd:3012 review: Retry and Approve name the entry they act on by its full head.
        self.assertEqual((prepared["head"], prepared["expected_head"]), ("5" * 12, "5" * 40))

    def test_a_new_blocked_head_changes_the_revision(self):
        # sd:3012 review: a later run that ends blocked at another head leaves the pending order alone, so the
        # revision must read each blocked entry's head too, or Approve would grant a head the page never showed.
        before = self.doc()["lanes"][0]["revision"]
        self.list(self.entries() + [{"item": 5, "title": "Prepared only", "status": "prepared", "reason": "again",
                                     "expected_head": "f" * 40, "finished_at": "2026-09-06T11:00:00Z"}])
        after = self.doc()["lanes"][0]["revision"]
        self.assertNotEqual(before, after)
        self.assertEqual(queue_screen.lane_revision(self.entries()),
                         queue_screen.lane_revision(json.loads(self.fixture.read_text())["entries"]))

    # sd:3398: a cancelled entry is a row, so Re-queue can name the head the operator cancelled.
    def test_a_cancelled_entry_carries_its_head_and_when_it_was_cancelled(self):
        cancelled = next(row for row in self.doc()["lanes"][0]["rows"] if row["state"] == "cancelled")
        self.assertEqual((cancelled["item"], cancelled["head"], cancelled["expected_head"], cancelled["finished"]),
                         (1, "1" * 12, "1" * 40, "2026-09-06T07:00:00Z"))

    def test_a_cancelled_entry_that_is_not_its_items_newest_or_is_old_has_no_row(self):
        later = {"item": 1, "title": "Cancelled", "status": "pending", "expected_head": "1" * 40}
        old = {"item": 16, "title": "Cancelled last week", "status": "cancelled", "expected_head": "6" * 40,
               "finished_at": "2026-08-30T07:00:00Z"}
        self.list(self.entries() + [later, old])
        rows = self.doc()["lanes"][0]["rows"]
        self.assertEqual([row["item"] for row in rows if row["state"] == "cancelled"], [])

    def test_a_cancelled_head_is_part_of_the_page_revision(self):
        # Re-queue carries the head the row showed; a later entry cancelled at another head must change the revision.
        before = self.doc()["lanes"][0]["revision"]
        self.list(self.entries() + [{"item": 1, "title": "Cancelled", "status": "cancelled", "expected_head": "f" * 40,
                                     "finished_at": "2026-09-06T08:00:00Z"}])
        self.assertNotEqual(self.doc()["lanes"][0]["revision"], before)

    # sd:3419: `lane move` can list an item's newer entry above its older ones, so its place is not its age.
    def test_after_a_move_an_items_newest_entry_is_its_highest_enqueue_seq_not_its_last_listed(self):
        newer = [{"item": 5, "title": "Prepared only", "status": "prepared", "reason": "again", "expected_head": "f" * 40,
                  "finished_at": "2026-09-06T11:00:00Z", "enqueue_seq": 98},
                 {"item": 1, "title": "Cancelled", "status": "pending", "expected_head": "1" * 40, "enqueue_seq": 99}]
        self.list(newer + numbered(self.entries()))
        rows = self.doc()["lanes"][0]["rows"]
        self.assertEqual([(row["item"], row["expected_head"]) for row in rows if row["state"] == "blocked"],
                         [(5, "f" * 40), (4, "4" * 40)])
        self.assertEqual([row["item"] for row in rows if row["state"] == "cancelled"], [])

    def test_after_a_move_the_page_revision_reads_the_head_of_the_highest_enqueue_seq(self):
        newer = {"item": 5, "title": "Prepared only", "status": "prepared", "reason": "again", "expected_head": "f" * 40,
                 "finished_at": "2026-09-06T11:00:00Z", "enqueue_seq": 99}
        self.list([newer] + numbered(self.entries()))
        before = self.doc()["lanes"][0]["revision"]
        self.list([{**newer, "expected_head": "e" * 40}] + numbered(self.entries()))
        self.assertNotEqual(self.doc()["lanes"][0]["revision"], before)

    def test_without_enqueue_seq_on_every_entry_the_last_listed_entry_is_the_newest(self):
        # The fallback for a pack that writes no `enqueue_seq`: it lists an item's entries oldest first.
        older, newer = {"item": 1, "status": "failed"}, {"item": 1, "status": "cancelled"}
        self.assertIs(queue_screen.newest([older, newer])[1], newer)
        self.assertIs(queue_screen.newest([{**older, "enqueue_seq": 7}, newer])[1], newer)
        moved = [{**newer, "enqueue_seq": 7}, {**older, "enqueue_seq": 2}]
        self.assertIs(queue_screen.newest(moved)[1], moved[0])

    def test_a_pack_whose_retry_takes_a_cancelled_entry_offers_requeue(self):
        doc = self.doc()
        self.assertEqual((doc["requeue"], doc["requeue_off"]), (True, None))

    def test_a_pack_whose_retry_takes_no_cancelled_entry_leaves_requeue_off_with_its_reason(self):
        # Before pack sd:3389, `lane retry --help` does not name a cancelled entry.
        with mock.patch.dict(os.environ, {"QUEUE_OLD_REQUEUE": "1"}):
            doc = self.doc()
        self.assertEqual((doc["retry"], doc["requeue"]), (True, False))
        self.assertIn("sd:3389", doc["requeue_off"])

    def test_a_failed_help_probe_leaves_requeue_off_as_unknown_not_as_absent(self):
        with mock.patch.dict(os.environ, {"QUEUE_NO_RETRY": "1"}):
            doc = self.doc()
        self.assertIs(doc["requeue"], False)
        self.assertIn("exited 2", doc["requeue_off"])
        self.assertIn("invalid choice", doc["requeue_off"])

    def test_landed_today_carries_the_pull_request_and_merge_commit(self):
        landed = self.doc()["lanes"][0]["rows"][-1]
        self.assertEqual((landed["pr"], landed["commit"]), ("161", self.merged[:12]))

    def test_a_merge_commits_pull_request_is_read_from_git_once(self):
        # A commit's subject never changes, so the next read asks git nothing for it.
        run = subprocess.run
        with mock.patch.object(queue_screen.subprocess, "run", side_effect=run) as runs:
            first, second = self.doc(), self.doc()
        logs = [call for call in runs.call_args_list if call.args[0][:1] == ["git"] and "log" in call.args[0]]
        self.assertEqual(len(logs), 1)
        self.assertEqual(first["lanes"][0]["rows"][-1]["pr"], second["lanes"][0]["rows"][-1]["pr"])

    def test_a_subject_git_could_not_read_is_asked_again(self):
        missing = str(Path(self.tmp.name) / "no-checkout")
        run = subprocess.run
        with mock.patch.object(queue_screen.subprocess, "run", side_effect=run) as runs:
            self.assertEqual((queue_screen._pr(missing, self.merged), queue_screen._pr(missing, self.merged)), (None, None))
        self.assertEqual(len(runs.call_args_list), 2)

    def test_the_header_reads_load_and_gate_slots(self):
        doc = self.doc()
        self.assertEqual(doc["gates"], {"running": 1, "cap": 4, "waiting": 0})
        self.assertEqual(set(doc["load"]), {"load1", "load5", "load15", "trend"})
        self.assertIn("next item boundary", doc["edits"])

    def test_a_failed_entry_shows_its_status_step_and_the_first_line_of_its_reason(self):
        # sd:3012: older failed history drops off after `BLOCKED_DAYS`; a reason shows its first line only.
        self.list(self.entries() + [
            {"item": 14, "title": "Failed this morning", "status": "failed", "step": "merge",
             "reason": "merge refused: required check pending\n  sd/local-gate: queued", "finished_at": "2026-09-06T06:00:00Z"},
            {"item": 15, "title": "Failed four days ago", "status": "failed", "step": "prepare", "reason": "gate failed",
             "finished_at": "2026-09-02T11:00:00Z"}])
        blocked = [row for row in self.doc()["lanes"][0]["rows"] if row["state"] == "blocked"]
        self.assertEqual([row["item"] for row in blocked], [5, 4, 14])
        failed = blocked[2]
        self.assertEqual((failed["status"], failed["step"], failed["reason"]),
                         ("failed", "merge", "merge refused: required check pending"))

    def test_a_pack_with_lane_retry_offers_it_and_names_the_repos_merge_setting(self):
        doc = self.doc()
        self.assertEqual((doc["retry"], doc["lanes"][0]["runner_merge"]), (True, "manual"))
        self.assertIn(["lane", "retry", "--help"], self.calls())

    def test_a_pack_without_lane_retry_hides_it(self):
        # Before the pack carries sd:3254 its argparse refuses `retry`; the page offers neither button.
        with mock.patch.dict(os.environ, {"QUEUE_NO_RETRY": "1"}):
            self.assertIs(self.doc()["retry"], False)

    def test_a_pack_whose_retry_takes_no_expected_head_hides_it(self):
        # Before pack sd:3268, retry cannot check the head the page showed; the page offers neither button.
        with mock.patch.dict(os.environ, {"QUEUE_OLD_RETRY": "1"}):
            self.assertIs(self.doc()["retry"], False)

    def test_a_lane_that_cannot_be_listed_is_named_not_dropped(self):
        self.fixture.write_text("not json")
        doc = self.doc()
        self.assertEqual((doc["lanes"], [p["repo"] for p in doc["problems"]]), ([], [str(self.checkout)]))


class TheMove(Lane, BrowserSession):
    """`/api/queue/move` runs one lane verb, and only under the page's session, CSRF and Origin."""

    def revision(self):
        return queue_screen.revision(json.loads(self.fixture.read_text())["entries"])

    def lane_revision(self):
        return queue_screen.lane_revision(json.loads(self.fixture.read_text())["entries"])

    def move(self, action="up", item=12, head=None, **headers):
        payload = {"repo": str(self.checkout), "item": item, "action": action, "revision": self.revision()}
        if head is not None:
            payload["head"] = head
        return self.post("/api/queue/move", payload, **headers)

    def test_a_reorder_calls_exactly_lane_move_with_the_item_place_and_revision(self):
        status, _, answer = self.move("up")
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "move", "12", "up", "--expected-revision", self.lane_revision()]])

    def test_top_hold_and_release_call_their_verbs_with_the_revision(self):
        for action, verb in (("top", ["move", "12", "top"]), ("hold", ["hold", "12"]), ("release", ["release", "12"])):
            self.log.write_text("")
            status, _, _ = self.move(action)
            self.assertEqual((status, self.calls()[-1]),
                             (200, ["-C", str(self.checkout), "lane", *verb, "--expected-revision", self.lane_revision()]))

    def test_a_verb_that_refuses_a_stale_revision_asks_the_page_to_read_again(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "the queue changed since revision x", "QUEUE_CODE": "stale_revision"}):
            status, _, answer = self.move("hold")
        self.assertEqual((status, answer.get("reload")), (409, True), answer)

    def test_cancel_calls_exactly_lane_cancel_with_the_item_and_revision(self):
        # sd:3284: `lane cancel` also releases a running entry (pack sd:3282), so a Cancel the page showed on a
        # pending row must not land after the runner claimed it: the verb checks the revision under its lock.
        status, _, answer = self.move("cancel", item=9)
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "cancel"))
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "cancel", "9", "--expected-revision",
                                         self.lane_revision()]])

    def test_a_refused_cancel_is_the_answer(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:9 has no pending entry in this lane"}):
            status, _, answer = self.move("cancel", item=9)
        self.assertEqual((status, answer["error"]), (400, "sd:9 has no pending entry in this lane"))

    def test_retry_calls_exactly_lane_retry_with_the_item_and_its_head(self):
        status, _, answer = self.move("retry", item=4, head="4" * 40)
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "retry"))
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "retry", "4", "--expected-head", "4" * 40]])

    def test_approve_calls_lane_retry_with_its_head_and_manual(self):
        status, _, answer = self.move("approve", item=5, head="5" * 40)
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "approve"))
        self.assertEqual(self.calls()[-1],
                         ["-C", str(self.checkout), "lane", "retry", "5", "--expected-head", "5" * 40, "--manual"])

    def test_retry_or_approve_without_a_full_head_runs_nothing(self):
        for action, head in (("retry", None), ("approve", None), ("approve", "5" * 12), ("retry", "HEAD")):
            status, _, _ = self.move(action, item=5, head=head)
            self.assertEqual((action, head, status, self.calls()), (action, head, 400, []))
        status, _, _ = self.move("up", head="c" * 40)
        self.assertEqual((status, self.calls()), (400, []))

    def test_approve_after_a_later_run_blocked_at_another_head_is_refused_before_any_verb(self):
        # sd:3012 review: the page showed sd:5 at one head; a later run ended blocked at another, pending unchanged.
        revision = self.revision()
        self.list(self.entries() + [{"item": 5, "title": "Prepared only", "status": "prepared", "reason": "again",
                                     "expected_head": "f" * 40, "finished_at": "2026-09-06T11:00:00Z"}])
        status, _, answer = self.post("/api/queue/move", {"repo": str(self.checkout), "item": 5, "action": "approve",
                                                          "revision": revision, "head": "5" * 40})
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"]])

    def test_a_retry_the_lane_refuses_for_another_head_asks_the_page_to_read_again(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:5's last entry is at another head", "QUEUE_CODE": "stale_head"}):
            status, _, answer = self.move("approve", item=5, head="5" * 40)
        self.assertEqual((status, answer.get("reload")), (409, True), answer)

    def test_approve_on_an_auto_repo_runs_nothing(self):
        # runner_merge=auto needs no grant; the page offers no Approve there, and a stale page is refused too.
        upsert_repo(self.connection, str(self.checkout), runner_merge="auto")
        self.connection.commit()
        status, _, _ = self.move("approve", item=5, head="5" * 40)
        self.assertEqual((status, self.calls()), (400, []))

    def test_a_refused_retry_is_the_answer(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:4's last entry kept no body copy"}):
            status, _, answer = self.move("retry", item=4, head="4" * 40)
        self.assertEqual((status, answer["error"]), (400, "sd:4's last entry kept no body copy"))

    # sd:3339: only a refusal `sd-ship lane` reports wrote nothing; any other failure may come after the write landed.
    def test_a_hub_fault_an_unknown_publish_or_an_unknown_code_is_an_unknown_outcome(self):
        for code in ("hub_unavailable", "publish_unknown", "a_code_this_page_does_not_know"):
            with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "The hub database did not answer.", "QUEUE_CODE": code}):
                status, _, answer = self.move("cancel", item=9)
            self.assertEqual((code, status, answer.get("outcome")), (code, 502, "unknown"), answer)
            self.assertEqual(answer["error"], "The hub database did not answer.")

    def test_a_verb_that_runs_past_its_bound_is_an_unknown_outcome(self):
        with mock.patch.object(queue_screen, "WRITE_SECONDS", 1), mock.patch.dict(os.environ, {"QUEUE_SLEEP": "10"}):
            status, _, answer = self.move("hold")
        self.assertEqual((status, answer.get("outcome")), (502, "unknown"), answer)
        self.assertNotIn("did not run", answer["error"])

    def test_an_exit_without_a_refusal_is_an_unknown_outcome(self):
        for env in ({"QUEUE_CRASH": "KeyError: 'item'"}, {"QUEUE_REFUSE": "sd:12 is running", "QUEUE_EXIT": "1"}):
            with mock.patch.dict(os.environ, env):
                status, _, answer = self.move("up")
            self.assertEqual((env, status, answer.get("outcome")), (env, 502, "unknown"), answer)

    def test_a_refusal_that_wrote_nothing_stays_a_refusal(self):
        for code in ("", "holder_alive", "head_gone", "branch_diverged", "default_branch"):
            with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "refused before the write", "QUEUE_CODE": code}):
                status, _, answer = self.move("retry", item=4, head="4" * 40)
            self.assertEqual((code, status, answer), (code, 400, {"error": "refused before the write"}))

    # sd:3398: Re-queue runs `lane retry` on a cancelled entry with the head the row showed (pack sd:3389).
    def test_requeue_calls_exactly_lane_retry_with_the_item_and_its_head(self):
        status, _, answer = self.move("requeue", item=1, head="1" * 40)
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "requeue"), answer)
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "retry", "1", "--expected-head", "1" * 40]])

    def test_requeue_without_a_full_head_runs_nothing(self):
        for head in (None, "1" * 12, "HEAD"):
            status, _, _ = self.move("requeue", item=1, head=head)
            self.assertEqual((head, status, self.calls()), (head, 400, []))

    def test_requeue_of_an_entry_that_is_not_cancelled_runs_no_verb(self):
        # The operator's ruling: the page re-queues a cancelled entry and enqueues nothing else.
        for item, head in ((4, "4" * 40), (12, "c" * 40), (77, "7" * 40)):
            self.log.write_text("")
            status, _, answer = self.move("requeue", item=item, head=head)
            self.assertEqual((item, status, self.calls()), (item, 400, [["-C", str(self.checkout), "lane", "list"]]))
            self.assertIn("cancelled", answer["error"])

    def test_requeue_after_the_item_was_cancelled_again_at_another_head_is_refused_before_any_verb(self):
        revision = self.revision()
        self.list(self.entries() + [{"item": 1, "title": "Cancelled", "status": "cancelled", "expected_head": "f" * 40,
                                     "finished_at": "2026-09-06T08:00:00Z"}])
        status, _, answer = self.post("/api/queue/move", {"repo": str(self.checkout), "item": 1, "action": "requeue",
                                                          "revision": revision, "head": "1" * 40})
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"]])

    def test_requeue_after_a_move_reads_the_highest_enqueue_seq_as_the_newest_entry(self):
        # sd:3419: a newer cancel listed above an older failure is the newest entry, so Re-queue takes it.
        cancelled = {"item": 9, "title": "Old failure, since retried", "status": "cancelled", "expected_head": "f" * 40,
                     "finished_at": "2026-09-06T11:00:00Z", "enqueue_seq": 99}
        self.list([cancelled] + numbered([row for row in self.entries() if row["status"] != "pending" or row["item"] != 9]))
        status, _, answer = self.move("requeue", item=9, head="f" * 40)
        self.assertEqual((status, answer.get("ok")), (200, True), answer)
        self.assertEqual(self.calls()[-1], ["-C", str(self.checkout), "lane", "retry", "9", "--expected-head", "f" * 40])

    def test_requeue_after_a_move_put_a_newer_entry_above_the_cancel_runs_no_verb(self):
        self.list([{"item": 1, "title": "Cancelled", "status": "pending", "expected_head": "1" * 40, "enqueue_seq": 99}]
                  + numbered(self.entries()))
        status, _, answer = self.move("requeue", item=1, head="1" * 40)
        self.assertEqual((status, self.calls()), (400, [["-C", str(self.checkout), "lane", "list"]]), answer)
        self.assertIn("newest entry in this lane is pending", answer["error"])

    def test_a_requeue_the_lane_refuses_answers_with_the_packs_reason(self):
        # Each refusal of pack sd:3389 writes nothing: a stale head or a newer entry asks the page to read again.
        for code, want in (("stale_head", 409), ("superseded", 409), ("head_moved", 400), ("already_merged", 400)):
            with mock.patch.dict(os.environ, {"QUEUE_REFUSE": f"refused {code}; nothing was queued", "QUEUE_CODE": code}):
                status, _, answer = self.move("requeue", item=1, head="1" * 40)
            self.assertEqual((code, status), (code, want), answer)
            if code != "stale_head":
                self.assertEqual(answer["error"], f"refused {code}; nothing was queued")

    def test_a_requeue_whose_publish_is_unknown_is_an_unknown_outcome(self):
        # `publish_unknown` also follows a push that may have landed on origin; the page cannot tell which.
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "origin did not answer; nothing was queued",
                                          "QUEUE_CODE": "publish_unknown"}):
            status, _, answer = self.move("requeue", item=1, head="1" * 40)
        self.assertEqual((status, answer), (502, {"error": "origin did not answer; nothing was queued", "outcome": "unknown"}))

    def test_a_post_without_the_csrf_token_runs_nothing(self):
        status, _, _ = self.move(**{"X-SD-CSRF": "0" * 64})
        self.assertEqual((status, self.calls()), (403, []))

    def test_a_post_from_another_origin_runs_nothing(self):
        status, _, _ = self.move(Origin="https://example.test")
        self.assertEqual((status, self.calls()), (403, []))

    def test_a_stale_revision_is_refused_before_any_verb(self):
        revision = queue_screen.revision(json.loads(self.fixture.read_text())["entries"])
        self.list(self.entries()[::-1])  # the queue moved after the page read it
        status, _, answer = self.post("/api/queue/move", {"repo": str(self.checkout), "item": 12, "action": "up", "revision": revision})
        self.assertEqual((status, answer.get("reload")), (409, True))
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"]])

    def test_an_unregistered_repository_or_unknown_action_runs_nothing(self):
        status, _, _ = self.post("/api/queue/move", {"repo": self.tmp.name, "item": 12, "action": "up", "revision": "x"})
        self.assertEqual((status, self.calls()), (400, []))
        status, _, _ = self.move("delete")
        self.assertEqual((status, self.calls()), (400, []))

    def reclaim(self, claim="claim-token-3:merge", revision=None):
        payload = {"repo": str(self.checkout), "item": 3, "action": "reclaim", "revision": revision or self.revision()}
        if claim is not None:
            payload["claim"] = claim
        return self.post("/api/queue/move", payload)

    def test_release_calls_lane_cancel_with_the_claim_the_row_showed(self):
        # sd:3337: `--expected-claim` binds the release to the claim the page read, in the verb's transaction
        # (pack sd:3330). The pending order is not what Release acts on, so no `--expected-revision`.
        status, _, answer = self.reclaim()
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "reclaim"), answer)
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "cancel", "3", "--expected-claim",
                                         "claim-token-3:merge"]])

    def test_a_claim_the_lane_refuses_as_stale_asks_the_page_to_read_again(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:3 is not running under claim claim-token-3:merge",
                                          "QUEUE_CODE": "stale_claim"}):
            status, _, answer = self.reclaim()
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertIn("moved on", answer["error"])

    def test_a_release_the_lane_refuses_is_the_answer(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:3 is running under pid 4242 on this machine; it is not stuck",
                                          "QUEUE_CODE": "holder_alive"}):
            status, _, answer = self.reclaim()
        self.assertEqual((status, answer["error"]), (400, "sd:3 is running under pid 4242 on this machine; it is not stuck"))

    def test_a_release_the_hub_did_not_answer_is_an_unknown_outcome(self):
        # sd:3339: `lane cancel` may have released the claim before the hub fault; a 400 would say nothing changed.
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "The hub database did not answer.", "QUEUE_CODE": "hub_unavailable"}):
            status, _, answer = self.reclaim()
        self.assertEqual((status, answer), (502, {"error": "The hub database did not answer.", "outcome": "unknown"}))

    def test_release_without_a_whole_claim_runs_nothing(self):
        for claim in (None, "", "claim-token-3", ":merge", "claim-token-3:", 7, "x" * 300 + ":merge"):
            status, _, _ = self.reclaim(claim=claim)
            self.assertEqual((claim, status, self.calls()), (claim, 400, []))
        payload = {"repo": str(self.checkout), "item": 9, "action": "cancel", "revision": self.revision(),
                   "claim": "claim-token-3:merge"}
        status, _, _ = self.post("/api/queue/move", payload)
        self.assertEqual((status, self.calls()), (400, []))

    def test_a_lane_refusal_is_the_answer(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:12 is running; the queue changes only between items"}):
            status, _, answer = self.move("up")
        self.assertEqual((status, answer["error"]), (400, "sd:12 is running; the queue changes only between items"))

    def test_the_page_and_its_document_answer(self):
        status, _, body = self.request("/queue", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn('<script src="/ui/queue.js?v=', body)
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertLess(scripts.index("commands.js"), scripts.index("queue.js"))
        status, _, body = self.request("/api/queue", headers={"Cookie": self.cookie})
        self.assertEqual((status, len(json.loads(body)["lanes"])), (200, 1))


def pinned_pack() -> Path:
    """The pack whose `sd_db` this process loaded: the gate's pin, or `SD_PACK_ROOT` in a hand run (sd:5140).

    Not the `sd-ship` on PATH: on the hub that is the serving copy, and a fixture database one library writes, the
    other may refuse.
    """
    return pack_bin().parent


#: Three pending entries, written by the pinned pack's own `sd_lane.store` into the fixture database.
SEED = """\
import json, sys
sys.path.insert(0, sys.argv[1])
import sd_lane
from sd_db.database import connect, transaction
connection = connect(sys.argv[2], write=True)
with transaction(connection):
    for entry in json.loads(sys.argv[4]):
        sd_lane.store(connection, sys.argv[3], entry)
connection.close()
"""


#: The host name the pinned pack's lane writes as a holder's `host`.
HOST = """\
import sys
sys.path.insert(0, sys.argv[1])
import sd_lane
print(sd_lane.this_host())
"""


class RealLane(BrowserSession):
    """The pinned pack's `sd-ship lane` on a temporary lane root, seeded with `seeds()`.

    The pack keeps the queue in the hub database (pack sd:3282), so the entries go there through its own writer:
    a `<lane>/queue/queue.json` is only an earlier version's file, which `lane list` imports by publishing each
    pending head to `origin`. The checkout's origin names a GitHub repository, as every lane's must.
    """

    def setUp(self):
        super().setUp()
        self.checkout = Path(self.tmp.name) / "system"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q", "-b", "main")
        origin = "https://github.com/example/system.git"
        git(self.checkout, "remote", "add", "origin", origin)
        upsert_repo(self.connection, str(self.checkout), remote=origin, managed=1)
        self.connection.commit()
        # `sd-ship` opens `$HOME/.local/share/sd/sd.db`; the fixture database stands there.
        home = Path(self.tmp.name) / "home"
        (home / ".local/share/sd").mkdir(parents=True)
        (home / ".local/share/sd/sd.db").symlink_to(self.path)
        root = Path(self.tmp.name) / "lanes"
        (root / "system" / "lane" / "queue").mkdir(parents=True)
        pack = pinned_pack()
        ship = pack / "bin" / "sd-ship"
        self.assertTrue(ship.is_file(), f"no pinned sd-ship at {ship}")
        self.host = subprocess.run([sys.executable, "-c", HOST, str(pack / "bin")], check=True, capture_output=True,
                                   text=True, env={**os.environ, "HOME": str(home)}).stdout.strip()
        self.pack, self.home = pack, home
        self.store(self.seeds())
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "sd-ship").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ship}" "$@"\n')
        (bin_dir / "sd-ship").chmod(0o755)
        patch = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "SD_LANE_ROOT": str(root),
                                             "HOME": str(home)})
        patch.start()
        self.addCleanup(patch.stop)

    def store(self, entries):
        """Write `entries` through the pinned pack's own `sd_lane.store`, as a runner's write would land."""
        subprocess.run([sys.executable, "-c", SEED, str(self.pack / "bin"), str(self.path), "example/system",
                        json.dumps(entries)], check=True, capture_output=True, text=True,
                       env={**os.environ, "HOME": str(self.home)})

    def seeds(self):
        return [{"id": f"seed-{n}", "item": n, "title": f"item {n}", "status": "pending", "position": n,
                 "expected_head": "e" * 40, "repository": "example/system", "branch": f"b{n}"} for n in (1, 2, 3)]

    def entries(self):
        return queue_screen.lane_list(str(self.checkout))["entries"]

    def order(self):
        return [entry["item"] for entry in self.entries() if entry["status"] == "pending"]


class ThePinnedPack(unittest.TestCase):
    """The real-lane tests write, run and read with one pack: the one whose `sd_db` this process loaded (sd:5140)."""

    def test_an_sd_ship_on_path_from_another_pack_is_not_the_pinned_pack(self):
        # On the hub, PATH's `sd-ship` is the serving copy and `SD_PACK_ROOT` the checkout. When they differ, the
        # fixture written by one pack's library and read by the other's failed 5 tests and erred 7 (SchemaTooNew).
        with tempfile.TemporaryDirectory() as other:
            ship = Path(other) / "bin" / "sd-ship"
            ship.parent.mkdir()
            ship.write_text("#!/bin/sh\nexit 1\n")
            ship.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{ship.parent}:{os.environ['PATH']}"}):
                self.assertEqual(pinned_pack(), pack_bin().parent)


class TheRealLane(RealLane):
    """The page's reorder is what the pinned pack's `lane list` shows."""

    def test_a_reorder_from_the_page_is_the_order_lane_list_shows(self):
        lane = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        status, _, answer = self.post("/api/queue/move", {"repo": str(self.checkout), "item": 3, "action": "top",
                                                          "revision": lane["revision"]})
        self.assertEqual((status, answer.get("ok")), (200, True), answer)
        self.assertEqual(self.order(), [3, 1, 2])
        status, _, _ = self.post("/api/queue/move", {"repo": str(self.checkout), "item": 1, "action": "down",
                                                     "revision": lane["revision"]})
        self.assertEqual((status, self.order()), (409, [3, 1, 2]))

    def test_a_write_between_the_check_and_the_verb_is_refused_and_moves_nothing(self):
        # The verb checks the revision again under the queue's lock, so a writer that lands after the page's
        # check makes it refuse; the queue keeps the other writer's order.
        lane = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        listed = queue_screen.lane_list

        def then_another_writer(path):
            answer = listed(path)
            queue_screen._run([queue_screen.command("sd-ship"), "-C", path, "lane", "move", "2", "top"], 30)
            return answer
        with mock.patch.object(queue_screen, "lane_list", then_another_writer):
            status, _, answer = self.post("/api/queue/move", {"repo": str(self.checkout), "item": 3, "action": "top",
                                                              "revision": lane["revision"]})
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertEqual(self.order(), [2, 1, 3])


class TheRealRelease(RealLane):
    """Release on the pinned pack's `lane cancel --expected-claim` (sd:3337): the claim the row showed is released, and
    a claim that moved on since the read is refused with nothing changed, whatever the page's revision says."""

    step = "prepare"

    def holder(self):
        return {"host": "build-2", "pid": 4242, "token": "claim-token"}

    def running(self, **change):
        return {**super().seeds()[0], "status": "running", "step": self.step, "started_at": "2026-09-06T11:50:00Z",
                "lease_until": "2026-09-06T13:00:00Z", "holder": self.holder(), "enqueued_on": "build-2", **change}

    def seeds(self):
        return [self.running(), *super().seeds()[1:]]

    def read(self):
        """What the page read: its lane and the running row with the claim Release sends."""
        lane = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        merging = lane["rows"][0]
        self.assertEqual((merging["state"], merging["item"]), ("merging", 1))
        return lane, merging

    def release(self, lane, merging):
        return self.post("/api/queue/move", {"repo": str(self.checkout), "item": 1, "action": "reclaim",
                                             "revision": lane["revision"], "claim": merging["claim"]})

    def refused_unchanged(self, lane, merging):
        """The release of a claim that moved on is a 409, the lane is as the runner left it and the revision held."""
        before = self.entries()
        self.assertEqual(queue_screen.revision(before), lane["revision"])  # the page's revision cannot tell
        status, _, answer = self.release(lane, merging)
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertEqual(self.entries(), before)

    def test_release_puts_a_stuck_prepare_back_to_pending(self):
        lane, merging = self.read()
        self.assertEqual(merging["claim"], "claim-token:prepare")
        status, _, answer = self.release(lane, merging)
        self.assertEqual((status, answer.get("ok")), (200, True), answer)
        self.assertEqual([(entry["item"], entry["status"]) for entry in self.entries()],
                         [(1, "pending"), (2, "pending"), (3, "pending")])

    def test_release_fails_a_stuck_merge(self):
        self.store([self.running(step="merge")])
        lane, merging = self.read()
        status, _, answer = self.release(lane, merging)
        self.assertEqual((status, answer.get("ok")), (200, True), answer)
        self.assertEqual(self.entries()[0]["status"], "failed")

    def test_a_prepare_that_reached_merge_since_the_read_is_refused(self):
        # The old Release's race (sd:3284 review): a prepare read, then merge, must not be failed.
        lane, merging = self.read()
        self.store([self.running(step="merge", lease_until="2026-09-06T14:00:00Z")])
        self.refused_unchanged(lane, merging)
        self.assertEqual((self.entries()[0]["status"], self.entries()[0]["step"]), ("running", "merge"))

    def test_a_reclaim_at_the_same_revision_is_refused(self):
        # The entry was reclaimed and claimed again at the same step: the pending order never moved, a new token holds it.
        lane, merging = self.read()
        self.store([self.running(holder={"host": "build-3", "pid": 77, "token": "another-claim"})])
        self.refused_unchanged(lane, merging)
        self.assertEqual(self.entries()[0]["holder"]["token"], "another-claim")

    def test_a_finished_entry_is_refused(self):
        lane, merging = self.read()
        done = {key: value for key, value in self.running().items() if key not in ("holder", "step", "lease_until")}
        self.store([{**done, "status": "merged", "finished_at": "2026-09-06T12:05:00Z", "merge_commit": "f" * 40}])
        self.refused_unchanged(lane, merging)
        self.assertEqual(self.entries()[0]["status"], "merged")

    def test_release_of_a_holder_alive_on_this_machine_is_refused(self):
        self.store([self.running(holder={"host": self.host, "pid": os.getpid(), "token": "claim-token"})])
        lane, merging = self.read()
        before = self.entries()
        status, _, answer = self.release(lane, merging)
        self.assertEqual(status, 400, answer)
        self.assertIn("not stuck", answer["error"])
        self.assertEqual(self.entries(), before)


class TheInProcessRead(RealLane):
    """The page reads each lane in-process through the pinned pack's own `sd_lane` (sd:TBD): no `lane list` runs per
    repository, and what it reads is what `lane list` prints -- the queue path, the revision and every entry."""

    def seeds(self):
        pending = super().seeds()
        return [*pending,
                {**pending[0], "id": "seed-run", "item": 4, "status": "running", "step": "merge", "position": 0,
                 "started_at": "2026-09-06T11:50:00Z", "lease_until": "2026-09-06T13:00:00Z", "body": "the body",
                 "holder": {"host": "build-2", "pid": 77, "token": "claim-token"}},
                {**pending[0], "id": "seed-merged", "item": 5, "status": "merged", "position": 5,
                 "merge_commit": "f" * 40, "finished_at": "2026-09-06T10:00:00Z"},
                {**pending[0], "id": "seed-failed", "item": 6, "status": "failed", "step": "prepare", "position": 6,
                 "reason": "gate failed", "finished_at": "2026-09-06T09:00:00Z"},
                {**pending[0], "id": "seed-again", "item": 6, "status": "cancelled", "position": -1,
                 "finished_at": "2026-09-06T09:30:00Z"}]

    def listed(self):
        """What the pinned pack's `sd-ship lane list` prints for the checkout, its exit status aside."""
        answer = queue_screen.lane_list(str(self.checkout))
        self.assertEqual(answer.pop("exit"), 0, answer)
        return answer

    def test_the_in_process_read_is_what_lane_list_prints(self):
        self.store([{**self.seeds()[0], "held": True}])
        read = queue_screen.in_process(self.connection, str(self.checkout), "https://github.com/example/system.git")
        self.assertEqual(read, self.listed())
        self.assertEqual([entry["item"] for entry in read["entries"]], [6, 4, 1, 2, 3, 5, 6])

    def test_the_page_runs_no_lane_list(self):
        with mock.patch.object(queue_screen, "lane_list", side_effect=AssertionError("lane list ran")):
            doc = queue_screen.document(self.connection, now=NOW)
        lane = doc["lanes"][0]
        self.assertEqual(doc["problems"], [])
        self.assertEqual([(row["state"], row.get("item")) for row in lane["rows"]],
                         [("merging", 4), ("next", 1), ("next", 2), ("next", 3), ("cancelled", 6), ("landed", 5)])
        self.assertEqual(lane["revision"], queue_screen.revision(self.entries()))
        self.assertEqual(Path(lane["lane"]), Path(self.listed()["queue"]).parent.parent)

    def test_a_lane_hosted_on_a_satellite_reads_the_same_entries(self):
        # Pack sd:3003: the queue sits in the hub database whichever machine runs the lane.
        hub = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        self.connection.execute("UPDATE repo SET lane_host = 'build-2' WHERE path = ?", (str(self.checkout),))
        self.connection.commit()
        with mock.patch.object(queue_screen, "lane_list", side_effect=AssertionError("lane list ran")):
            satellite = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        self.assertEqual(satellite["host"], "build-2")
        self.assertEqual(satellite, {**hub, "host": "build-2"})

    def test_a_queue_file_an_earlier_version_left_is_read_by_lane_list(self):
        # `lane list` imports an earlier version's queue file into the database once; the page lets it.
        queue = Path(os.environ["SD_LANE_ROOT"]) / "system" / "lane" / "queue" / "queue.json"
        queue.write_text(json.dumps({"entries": []}))
        with mock.patch.object(queue_screen, "lane_list", wraps=queue_screen.lane_list) as listing:
            lane = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        listing.assert_called_once_with(str(self.checkout))
        self.assertEqual(len(lane["rows"]), 6)

    def test_without_the_packs_bin_lane_list_answers(self):
        with mock.patch.object(queue_screen, "pack_module", side_effect=ImportError("no bin")), \
                mock.patch.object(queue_screen, "lane_list", wraps=queue_screen.lane_list) as listing:
            lane = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        listing.assert_called_once_with(str(self.checkout))
        self.assertEqual(len(lane["rows"]), 6)

    def test_a_pack_without_item_rounds_answers_through_lane_list(self):
        # An earlier pack (before sd:3483) has no `item_rounds`; its own `lane list` prints what it has.
        lane_module = queue_screen.pack_module("sd_lane")
        with mock.patch.object(lane_module, "item_rounds", None), \
                mock.patch.object(queue_screen, "lane_list", return_value={"ok": False, "error": "listed"}) as listing:
            doc = queue_screen.document(self.connection, now=NOW)
        listing.assert_called_once_with(str(self.checkout))
        self.assertEqual(doc["problems"], [{"repo": str(self.checkout), "error": "listed"}])

    def test_a_failed_read_is_a_named_problem_not_an_empty_lane(self):
        lane_module = queue_screen.pack_module("sd_lane")
        with mock.patch.object(lane_module, "stored", side_effect=sqlite3.OperationalError("database is locked")):
            doc = queue_screen.document(self.connection, now=NOW)
        self.assertEqual(doc["lanes"], [])
        self.assertEqual([problem["repo"] for problem in doc["problems"]], [str(self.checkout)])
        self.assertIn("database is locked", doc["problems"][0]["error"])


class TheScript(Lane, ScreenCase):
    """queue.js against the document `queue_screen` builds from the fixture."""

    def run_page(self, body, move=(200, {"ok": True, "edits": "Edits take effect at the next item boundary."}), search="?show=all"):
        """`search` is the page's address; most tests read every row, so it defaults to Show: all."""
        doc = queue_screen.document(self.connection, now=NOW)
        script = (STAND_IN + f"location.search = {json.dumps(search)};\n" + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + READ_SHELL
                  + f"\nvar DOC = {json.dumps(doc)}, MOVE = {json.dumps(list(move))};\n"
                  + "ANSWER = (path, body) => path === '/api/queue' ? [200, DOC] : path === '/api/queue/move' ? MOVE : (window.ELSE || (() => [404, { error: 'no answer' }]))(path, body);\n"
                  + COMMANDS_JS + QUEUE_JS + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = []; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_each_state_renders_its_glyph_and_label(self):
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        found = re.findall(r'data-state="(\w+)".*?<span class="g g-(\w+)" aria-hidden="true">(.)</span><span class="st">([A-Z]+)', lanes)
        self.assertEqual([tuple(f) for f in found], [
            ("merging", "live", "●", "MERGING"), ("next", "queued", "◌", "NEXT"), ("next", "caution", "▲", "HELD"),
            ("building", "live", "●", "BUILDING"), ("building", "ok", "●", "BUILDING"),
            ("blocked", "caution", "▲", "BLOCKED"), ("blocked", "caution", "▲", "BLOCKED"),
            ("cancelled", "queued", "◌", "CANCELLED"), ("landed", "ok", "●", "LANDED")])
        self.assertIn("<b>operator acts</b>", lanes)
        self.assertIn("PR #161", lanes)

    def test_adversarial_review_on_a_lane_row_posts_its_worktree_and_item(self):
        """sd:5064: a lane row with a recorded worktree asks for {kind: change, target, item}; Details follow the run."""
        self.list([{**e, "worktree": f"/work/sd-{e['item']}"} if e["item"] == 12 else e for e in self.entries()])
        out = self.run_page(f"""window.ELSE = (p, b) => p === '/api/adversarial' ? [202, {{ id: 'r1', state: 'queued', item: 12, filed: null }}]
  : p === '/api/adversarial/r1' ? [200, {{ id: 'r1', state: 'done', item: 12, filed: 'written', brief_item: 77 }}] : [404, {{ error: 'no' }}];
const c = REG.find(x => x.id === 'entry.adversarial-review'); R.on = c.on;
R.off = c.when(C.get('entry:{self.checkout}:5')); R.objs = [...OBJ.keys()];
shellRun(c, C.get('entry:{self.checkout}:12')); await flush(); await flush();
R.det = ELS.adv.html;""")
        self.assertEqual(out["posts"], [["/api/adversarial", {"kind": "change", "target": "/work/sd-12", "item": 12}, 64]])
        self.assertEqual(out["R"]["off"], "the lane recorded no worktree for this entry")
        # Building rows are builder gate logs, not entries: no object, so no command.
        self.assertEqual(len(out["R"]["objs"]), 7)
        self.assertIn('<a href="/item/77">brief sd:77</a>', out["R"]["det"])

    # Tuning: Show draws the active rows by default: merging, next (held too), building and blocked; All adds the ended
    # ones, cancelled and landed. The badge and each lane's counts read every row, whatever Show draws.
    SHOW_ALL = "ELS.show.listeners.click[0]({ target: { closest: s => s === 'button[data-show]' ? { dataset: { show: 'all' } } : null } });"

    def test_show_defaults_to_the_active_rows_and_all_adds_the_ended_ones(self):
        out = self.run_page("window.shell.url = q => OUT.urls.push(q); OUT.urls = []; R.lanes = ELS.lanes.html; R.show = ELS.show.html;"
                            f" {self.SHOW_ALL} R.all = ELS.lanes.html; R.show2 = ELS.show.html;", search="")
        states = lambda lanes: re.findall(r'<li class="qrow" data-state="(\w+)"', lanes)
        self.assertEqual(states(out["R"]["lanes"]), ["merging", "next", "next", "building", "building", "blocked", "blocked"])
        self.assertEqual(states(out["R"]["all"]), ["merging", "next", "next", "building", "building", "blocked", "blocked", "cancelled", "landed"])
        chips = r'data-show="(\w+)" aria-pressed="(\w+)">\w+ <span class="n">(\d+)</span>'
        self.assertEqual(re.findall(chips, out["R"]["show"]), [("active", "true", "7"), ("all", "false", "9")])
        self.assertEqual(re.findall(chips, out["R"]["show2"]), [("active", "false", "7"), ("all", "true", "9")])
        self.assertEqual(out["urls"], [{"show": "all"}])

    def test_show_all_comes_from_the_address(self):
        lanes = self.run_page("R.lanes = ELS.lanes.html;", search="?show=all")["R"]["lanes"]
        self.assertEqual(len(re.findall(r'<li class="qrow"', lanes)), 9)

    def test_the_badge_and_the_lane_counts_read_every_row_whatever_show_draws(self):
        out = self.run_page("R.lanes = ELS.lanes.html;", search="")
        self.assertIn("1 merging · 2 next · 2 blocked · 1 landed today", out["R"]["lanes"])
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 2, "what": "blocked"})

    def test_a_lane_with_only_ended_rows_joins_the_idle_line_under_active(self):
        self.list([entry for entry in self.entries() if entry["status"] in ("merged", "cancelled")])
        for log in self.lane.glob("gate-*.log"):
            log.unlink()
        out = self.run_page("R.lanes = ELS.lanes.html; R.idle = ELS.idle.html;", search="")
        self.assertNotIn('<li class="qrow"', out["R"]["lanes"])
        # sd:5640: idle lanes have their own view; Lanes says where they went.
        self.assertIn("No lane has a row to show. 1 idle lane is under Idle.", out["R"]["lanes"])
        # sd:5650: the Idle table says why each lane is idle: here only ended rows.
        self.assertIn('<td><code>system</code></td><td>', out["R"]["idle"])
        self.assertIn(' (hub)</td>', out["R"]["idle"])
        self.assertRegex(out["R"]["idle"], r"No active entry · \d+ landed, \d+ cancelled today")

    def test_the_merging_row_names_step_host_claim_age_and_lease(self):
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        merging = next(row for row in lanes.split("<li ")[1:] if 'data-state="merging"' in row)
        self.assertIn("<b>merge</b> on <b>hub-1</b> · claimed 10m 0s ago · lease to 13:20 · head " + "a" * 12, merging)

    def test_a_running_row_with_a_claim_offers_release(self):
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        self.assertEqual(re.findall(r'data-act="reclaim" data-item="(\d+)" data-claim="([\w:-]+)"', lanes),
                         [("3", "claim-token-3:merge")])
        offered = [row for row in lanes.split("<li ")[1:] if 'data-act="reclaim"' in row]
        self.assertEqual([re.search(r'data-state="(\w+)"', row)[1] for row in offered], ["merging"])

    def test_a_running_row_without_a_holder_offers_no_release(self):
        entries = self.entries()
        entries[5] = {key: value for key, value in entries[5].items() if key != "holder"}
        self.list(entries)
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        self.assertNotIn('data-act="reclaim"', lanes)

    def release(self, answer, move=(200, {"ok": True, "action": "reclaim", "item": 3, "edits": "Edits take effect at the next item boundary."})):
        """Click Release on sd:3; the confirm answers `answer` and records what it asked."""
        claim = queue_screen.document(self.connection, now=NOW)["lanes"][0]["rows"][0]["claim"]
        return self.run_page(f"window.shell.confirm = a => {{ OUT.asked = a; return Promise.resolve({json.dumps(answer)}); }};\n"
                             f"await window.queueAct({{ dataset: {{ repo: {json.dumps(str(self.checkout))}, item: '3', act: 'reclaim', claim: {json.dumps(claim)} }} }}); R.lanes = ELS.lanes.html;", move)

    def test_release_asks_first_naming_the_holder_step_outcome_and_command(self):
        out = self.release(True)
        asked = out["asked"]
        self.assertIn("sd:3", asked["title"])
        self.assertIn("hub-1", asked["title"])
        self.assertIn("hub-1 claimed it 10m 0s ago, at merge.", asked["body"])
        self.assertIn("A merge it already started may still land", asked["body"])
        self.assertEqual(asked["cli"], f"sd-ship -C '{self.checkout}' lane cancel 3 --expected-claim 'claim-token-3:merge'")
        revision = queue_screen.document(self.connection, now=NOW)["lanes"][0]["revision"]
        self.assertEqual(out["posts"][0][1], {"repo": str(self.checkout), "item": 3, "action": "reclaim",
                                              "revision": revision, "claim": "claim-token-3:merge"})
        self.assertIn("sd:3 released from its holder.", out["R"]["lanes"])

    def test_release_in_prepare_says_the_entry_goes_back_to_pending(self):
        entries = self.entries()
        entries[5] = {**entries[5], "step": "prepare"}
        self.list(entries)
        out = self.release(True)
        self.assertIn("goes back to pending at its place", out["asked"]["body"])
        self.assertNotIn("may still land", out["asked"]["body"])
        self.assertEqual(out["posts"][0][1]["claim"], "claim-token-3:prepare")

    def test_release_kept_at_the_confirm_posts_nothing(self):
        out = self.release(False)
        self.assertEqual((out["posts"], out["gets"]), ([], ["/api/queue"]))

    def test_a_release_of_a_claim_that_moved_on_says_so_in_its_lane(self):
        out = self.release(True, move=(409, {"error": "sd:3 moved on since the page read it.", "reload": True}))
        self.assertIn('<p class="fail" role="alert">sd:3 not released from its holder: sd:3 moved on since the page read it.</p>',
                      out["R"]["lanes"])

    def test_a_release_with_an_unknown_outcome_says_so_and_reads_the_lane_again(self):
        out = self.release(True, move=(502, {"error": "The hub database did not answer.", "outcome": "unknown"}))
        self.assertIn('<p class="fail" role="alert">Not known whether sd:3 was released from its holder: The hub database did '
                      'not answer. The lane below was read again; check sd:3 there before you act again.</p>', out["R"]["lanes"])
        self.assertEqual(out["gets"], ["/api/queue", "/api/queue"])

    def test_a_running_entry_past_its_lease_lights_caution(self):
        entries = self.entries()
        entries[5] = {**entries[5], "lease_until": "2026-09-06T11:55:00Z", "step": None, "holder": None}
        self.list(entries)
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        merging = next(row for row in lanes.split("<li ")[1:] if 'data-state="merging"' in row)
        self.assertIn('<span class="g g-caution" aria-hidden="true">▲</span>', merging)
        self.assertIn("<b>step not recorded</b> on <b>host not recorded</b> · claimed 10m 0s ago · lease passed 11:55", merging)

    def test_each_lane_names_its_host_and_idle_lanes_share_one_line(self):
        other = Path(self.tmp.name) / "quiet"
        other.mkdir()
        upsert_repo(self.connection, str(other), remote="git@example.invalid:q.git", managed=1)
        self.connection.execute("UPDATE repo SET lane_host = 'build-2' WHERE path = ?", (str(other),))
        self.connection.commit()
        Path(f"{self.fixture}.quiet").write_text(json.dumps({"ok": True, "queue": str(
            Path(self.tmp.name) / "lanes" / "quiet" / "lane" / "queue" / "queue.json"), "entries": []}))
        out = self.run_page("R.lanes = ELS.lanes.html; R.subhead = ELS.subhead.html; R.idle = ELS.idle.html;")
        lanes = out["R"]["lanes"]
        self.assertIn('<span class="host">lane on hub</span>', lanes)
        self.assertNotIn('aria-labelledby="lane-quiet"', lanes)
        # sd:5650: idle lanes sit in the Idle table, one row each: repository, machine, state and merge.
        self.assertNotIn("<code>quiet</code>", lanes)
        self.assertIn('<tr><td><code>quiet</code></td><td>build-2</td><td>Empty: nothing queued, gating or landed today</td>'
                      '<td>Approve to merge</td></tr>', out["R"]["idle"])
        # sd:5524: the Lanes tab carries the busy lane count, Idle the rest; the line under the title counts the managed repositories.
        self.assertIn("2 managed repositories · read", out["R"]["subhead"])
        self.assertNotIn("lanes of", out["R"]["subhead"])
        self.assertEqual(out["counts"], {"lanes": 1, "idle": 1})

    def test_only_next_rows_carry_controls_and_the_ends_are_off(self):
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        self.assertEqual(lanes.count('data-act="up"'), 2)
        self.assertRegex(lanes, r'data-act="up" data-item="9"[^>]*disabled')
        self.assertRegex(lanes, r'data-act="down" data-item="12"[^>]*disabled')
        self.assertIn('data-act="release" data-item="12"', lanes)

    def test_a_click_posts_the_lane_revision_and_reads_again(self):
        revision = queue_screen.document(self.connection, now=NOW)["lanes"][0]["revision"]
        out = self.run_page(f"await window.queueAct({{ dataset: {{ repo: {json.dumps(str(self.checkout))}, item: '9', act: 'down' }} }}); R.lanes = ELS.lanes.html;")
        self.assertEqual(out["posts"], [["/api/queue/move", {"repo": str(self.checkout), "item": 9, "action": "down", "revision": revision}, 64]])
        self.assertEqual(out["gets"], ["/api/queue", "/api/queue"])
        self.assertIn("sd:9 moved down. Edits take effect", out["R"]["lanes"])

    def test_a_refused_move_says_why_in_its_lane(self):
        out = self.run_page(f"await window.queueAct({{ dataset: {{ repo: {json.dumps(str(self.checkout))}, item: '9', act: 'up' }} }}); R.lanes = ELS.lanes.html;",
                            move=(409, {"error": "The queue changed since the page read it.", "reload": True}))
        self.assertIn('<p class="fail" role="alert">sd:9 not moved up: The queue changed since the page read it.</p>', out["R"]["lanes"])

    def cancel(self, answer, move=(200, {"ok": True, "action": "cancel", "item": 9, "edits": "Edits take effect at the next item boundary."})):
        """Click Cancel on sd:9; the confirm answers `answer` and records what it asked."""
        return self.run_page(f"window.shell.confirm = a => {{ OUT.asked = a; return Promise.resolve({json.dumps(answer)}); }};\n"
                             f"await window.queueAct({{ dataset: {{ repo: {json.dumps(str(self.checkout))}, item: '9', act: 'cancel' }} }}); R.lanes = ELS.lanes.html;", move)

    def test_each_next_row_offers_cancel(self):
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        self.assertEqual(re.findall(r'data-act="cancel" data-item="(\d+)"', lanes), ["9", "12"])

    def test_cancel_asks_first_and_names_the_entry_and_command(self):
        out = self.cancel(True)
        asked = out["asked"]
        self.assertIn("sd:9", asked["title"])
        self.assertIn("Old failure, since retried", asked["title"])
        self.assertEqual(asked["cli"], f"sd-ship -C '{self.checkout}' lane cancel 9")
        self.assertEqual(out["posts"][0][1]["action"], "cancel")
        self.assertIn("sd:9 cancelled.", out["R"]["lanes"])

    def test_cancel_names_requeue_as_the_way_back(self):
        self.assertIn("Re-queue", self.cancel(True)["asked"]["body"])
        with mock.patch.dict(os.environ, {"QUEUE_OLD_REQUEUE": "1"}):
            body = self.cancel(True)["asked"]["body"]
        self.assertIn("enqueue it again from its worktree", body)
        self.assertNotIn("Re-queue", body)

    def test_cancel_kept_at_the_confirm_posts_nothing(self):
        out = self.cancel(False)
        self.assertEqual((out["posts"], out["gets"]), ([], ["/api/queue"]))

    def test_a_refused_cancel_says_why_in_its_lane(self):
        out = self.cancel(True, move=(400, {"error": "sd:9 has no pending entry in this lane"}))
        self.assertIn('<p class="fail" role="alert">sd:9 not cancelled: sd:9 has no pending entry in this lane The queue is unchanged.</p>',
                      out["R"]["lanes"])

    # sd:3339: a hub fault or timeout may come after the write landed, so the page says the outcome is not known.
    def test_an_unknown_outcome_never_says_the_queue_is_unchanged_and_reads_the_lane_again(self):
        out = self.cancel(True, move=(502, {"error": "The hub database did not answer.", "outcome": "unknown"}))
        self.assertNotIn("unchanged", out["R"]["lanes"])
        self.assertIn('<p class="fail" role="alert">Not known whether sd:9 was cancelled: The hub database did not answer. '
                      'The lane below was read again; check sd:9 there before you act again.</p>', out["R"]["lanes"])
        self.assertEqual(out["gets"], ["/api/queue", "/api/queue"])

    def test_a_post_that_got_no_answer_is_an_unknown_outcome(self):
        out = self.run_page("var answered = fetch; fetch = (p, o) => o && o.method === 'POST' ? Promise.reject(new TypeError('Load failed')) : answered(p, o);\n"
                            f"await window.queueAct({{ dataset: {{ repo: {json.dumps(str(self.checkout))}, item: '12', act: 'hold' }} }}); R.lanes = ELS.lanes.html;")
        self.assertNotIn("unchanged", out["R"]["lanes"])
        self.assertIn("Not known whether sd:12 was held: Load failed", out["R"]["lanes"])
        self.assertEqual(out["gets"], ["/api/queue", "/api/queue"])


    def test_blocked_rows_offer_retry_and_approve_on_a_manual_repo(self):
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        self.assertEqual(re.findall(r'data-act="retry" data-item="(\d+)"', lanes), ["5", "4"])
        self.assertEqual(re.findall(r'data-act="approve" data-item="(\d+)" data-head="(\w+)"', lanes),
                         [("5", "5" * 40), ("4", "4" * 40)])
        self.assertEqual(re.findall(r'data-act="retry" data-item="\d+" data-head="(\w+)"', lanes), ["5" * 40, "4" * 40])
        offered = [row for row in lanes.split("<li ")[1:] if 'data-act="retry"' in row]
        self.assertEqual([re.search(r'data-state="(\w+)"', row)[1] for row in offered], ["blocked", "blocked"])

    def test_an_auto_repo_gets_retry_without_approve(self):
        upsert_repo(self.connection, str(self.checkout), runner_merge="auto")
        self.connection.commit()
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        self.assertEqual((lanes.count('data-act="retry"'), lanes.count('data-act="approve"')), (2, 0))

    def test_a_pack_without_lane_retry_shows_neither_button(self):
        with mock.patch.dict(os.environ, {"QUEUE_NO_RETRY": "1"}):
            lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        self.assertEqual((lanes.count('data-act="retry"'), lanes.count('data-act="approve"')), (0, 0))

    # sd:3398: a cancelled row offers Re-queue when the pack's retry takes a cancelled entry.
    def test_a_cancelled_row_offers_requeue_with_its_head(self):
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        self.assertEqual(re.findall(r'data-act="requeue" data-item="(\d+)" data-head="(\w+)"', lanes), [("1", "1" * 40)])
        row = next(row for row in lanes.split("<li ")[1:] if 'data-state="cancelled"' in row)
        self.assertIn('data-act="requeue"', row)
        self.assertIn("cancelled at 07:00 · head " + "1" * 12, row)

    def test_a_pack_without_requeue_shows_the_row_and_says_why_requeue_is_off(self):
        with mock.patch.dict(os.environ, {"QUEUE_OLD_REQUEUE": "1"}):
            lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        self.assertNotIn('data-act="requeue"', lanes)
        row = next(row for row in lanes.split("<li ")[1:] if 'data-state="cancelled"' in row)
        self.assertIn("Re-queue off:", row)
        self.assertIn("sd:3389", row)

    def test_requeue_posts_at_once_with_its_head_and_revision_and_reads_again(self):
        revision = queue_screen.document(self.connection, now=NOW)["lanes"][0]["revision"]
        out = self.click("requeue", 1)
        self.assertNotIn("asked", out)
        self.assertEqual(out["posts"][0][1], {"repo": str(self.checkout), "item": 1, "action": "requeue",
                                              "revision": revision, "head": "1" * 40})
        self.assertEqual(out["gets"], ["/api/queue", "/api/queue"])
        self.assertIn("sd:1 re-queued.", out["R"]["lanes"])

    def test_a_refused_requeue_shows_the_packs_reason(self):
        out = self.click("requeue", 1, move=(400, {"error": "origin/b1 moved; nothing was queued."}))
        self.assertIn('<p class="fail" role="alert">sd:1 not re-queued: origin/b1 moved; nothing was queued. The queue is unchanged.</p>',
                      out["R"]["lanes"])
        out = self.click("requeue", 1, move=(409, {"error": "sd:1 has a newer entry.", "reload": True}))
        self.assertIn('<p class="fail" role="alert">sd:1 not re-queued: sd:1 has a newer entry.</p>', out["R"]["lanes"])

    def test_a_requeue_with_an_unknown_outcome_says_so_and_reads_again(self):
        out = self.click("requeue", 1, move=(502, {"error": "origin did not answer.", "outcome": "unknown"}))
        self.assertIn("Not known whether sd:1 was re-queued: origin did not answer.", out["R"]["lanes"])
        self.assertEqual(out["gets"], ["/api/queue", "/api/queue"])

    def click(self, act, item, answer=True, move=(200, {"ok": True, "edits": "Edits take effect at the next item boundary."})):
        """Click a blocked row's button, which carries its entry's head; a confirm answers `answer` and records what it asked."""
        return self.run_page(f"window.shell.confirm = a => {{ OUT.asked = a; return Promise.resolve({json.dumps(answer)}); }};\n"
                             f"await window.queueAct({{ dataset: {{ repo: {json.dumps(str(self.checkout))}, item: '{item}', act: '{act}', head: '{str(item) * 40}' }} }}); R.lanes = ELS.lanes.html;", move)

    def test_retry_posts_at_once_with_its_head_and_says_the_entry_is_queued_again(self):
        out = self.click("retry", 4)
        self.assertNotIn("asked", out)
        self.assertEqual((out["posts"][0][1]["action"], out["posts"][0][1]["head"]), ("retry", "4" * 40))
        self.assertIn("sd:4 queued again.", out["R"]["lanes"])

    def test_approve_asks_first_and_names_the_entry_its_head_and_command(self):
        out = self.click("approve", 5)
        self.assertIn("sd:5", out["asked"]["title"])
        self.assertIn("5" * 12, out["asked"]["body"])
        self.assertEqual(out["asked"]["cli"], f"sd-ship -C '{self.checkout}' lane retry 5 --expected-head {'5' * 40} --manual")
        self.assertEqual((out["posts"][0][1]["action"], out["posts"][0][1]["head"]), ("approve", "5" * 40))
        self.assertIn("sd:5 approved and queued again.", out["R"]["lanes"])

    def test_approve_kept_at_the_confirm_posts_nothing(self):
        out = self.click("approve", 5, answer=False)
        self.assertEqual((out["posts"], out["gets"]), ([], ["/api/queue"]))


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "queue", "Queue", "/queue", ("/api/queue", "/api/queue/move")

    def test_queue_ends_now_and_has_left_fleet(self):
        now = re.search(r"\['Now',\s*\[(.*?)\]\],", GROUP_BLOCK).group(1)
        fleet = re.search(r"\['Fleet',\s*\[(.*?)\]\],", GROUP_BLOCK).group(1)
        self.assertTrue(now.endswith("['Briefs', 'inbox', 'i'], ['Queue', 'git-merge', 'u']"), now)
        self.assertNotIn("'Queue'", fleet)


if __name__ == "__main__":
    unittest.main()
