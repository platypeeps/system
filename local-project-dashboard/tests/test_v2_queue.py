"""The v2 Queue page (sd:2585).

What this slice promises: `/api/queue` draws each managed repository's lane from `sd-ship lane list`, one row per
item in one of five states (merging, next, building, blocked, landed), with load5 and the gate slots in the header.
The only write is `/api/queue/move`: it runs `sd-ship lane move|hold|release|cancel|retry` with exactly the page's item and
place, under the session cookie, CSRF and Origin checks, and refuses a revision the queue has moved past. Retry and Approve
(`retry --manual`) show on blocked rows only when `sd-ship lane retry --help` names `--expected-head` (sd:3012); Approve
only on a manual repo. Both carry the head the row showed.

`sd-ship` and `sd` are stubs on PATH that answer from a fixture and log their arguments; no test reaches a real lane.
One test runs the pinned pack's own `sd-ship` on a queue under a temporary `SD_LANE_ROOT`, so the order the page asks
for is the order `lane list` then reports. `queue.js` runs under JavaScriptCore (osascript) against the stand-in shell
`test_v2_tasks` defines. The look at 375 px is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import queue_screen, v2
from sd_db import upsert_repo

from support import NOW, ScreenCase
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
QUEUE_JS = (V2 / "static" / "queue.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
CLOCK = queue_screen._when(NOW)

STUB = """#!/bin/sh
# A stand-in for sd-ship and sd: log the arguments, answer from the fixture.
printf '%s\\n' "$*" >> "$QUEUE_LOG"
case "$*" in
  *"lane list") if [ -f "$QUEUE_FIXTURE.$(basename "$2")" ]; then cat "$QUEUE_FIXTURE.$(basename "$2")"; else cat "$QUEUE_FIXTURE"; fi ;;
  "lane retry --help") if [ -n "$QUEUE_NO_RETRY" ]; then echo "invalid choice: 'retry'" >&2; exit 2; fi
     if [ -n "$QUEUE_OLD_RETRY" ]; then echo "usage: sd-ship lane retry [--manual] item"; else echo "usage: sd-ship lane retry [--manual] [--expected-head EXPECTED_HEAD] item"; fi ;;
  "gate status --json") printf '{"slots": 4, "holders": [{"slot": 1}], "waiters": [], "load": [1, 2, 3]}\\n' ;;
  *) if [ -n "$QUEUE_REFUSE" ]; then printf '{"ok": false, "error": "%s", "code": "%s"}\\n' "$QUEUE_REFUSE" "$QUEUE_CODE"; exit 3; fi
     printf '{"ok": true}\\n' ;;
esac
"""


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


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
            {"item": 1, "title": "Cancelled", "status": "cancelled", "finished_at": "2026-09-06T07:00:00Z"},
        ]

    def calls(self):
        return [line.split() for line in self.log.read_text().splitlines()]


class TheRows(Lane, ScreenCase):
    """`queue_screen.document` from the fixture `lane list`, the gate log and `sd gate status`."""

    def doc(self):
        return queue_screen.document(self.connection, now=NOW)

    def test_each_of_the_five_states_has_its_row(self):
        rows = self.doc()["lanes"][0]["rows"]
        self.assertEqual([(row["state"], row.get("item") or row.get("builder")) for row in rows], [
            ("merging", 3), ("next", 9), ("next", 12), ("building", "queue-notes-1"), ("building", "sd1775-2"),
            ("blocked", 5), ("blocked", 4), ("landed", 7)])

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

    def test_the_claim_token_never_reaches_the_page(self):
        self.assertNotIn("claim-token-3", json.dumps(self.doc()))

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

    def test_a_new_holder_or_step_changes_the_revision_but_not_the_lane_revision(self):
        # sd:3284: Release acts on the claim the page showed; a claim taken again, or a step that moved on, is
        # another subject, so the page's revision covers both. The lane revision `lane list` prints does not.
        before = queue_screen.revision(self.entries())
        for change in ({"holder": {"host": "build-2", "pid": 9, "token": "t2"}}, {"step": "prepare"}):
            entries = self.entries()
            entries[5] = {**entries[5], **change}
            self.assertNotEqual(queue_screen.revision(entries), before, change)
            self.assertEqual(queue_screen.lane_revision(entries), queue_screen.lane_revision(self.entries()))
        entries = self.entries()
        entries[5] = {**entries[5], "holder": {**entries[5]["holder"], "token": "another-token"}}
        self.assertEqual(queue_screen.revision(entries), before)

    def test_landed_today_carries_the_pull_request_and_merge_commit(self):
        landed = self.doc()["lanes"][0]["rows"][-1]
        self.assertEqual((landed["pr"], landed["commit"]), ("161", self.merged[:12]))

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

    def reclaim(self, entry="e-3", revision=None):
        payload = {"repo": str(self.checkout), "item": 3, "action": "reclaim", "revision": revision or self.revision()}
        if entry is not None:
            payload["entry"] = entry
        return self.post("/api/queue/move", payload)

    def test_release_calls_lane_cancel_on_the_running_entry_with_the_revision(self):
        # sd:3284, D3 of sd:3174: Release on a running row is `lane cancel`, which releases its claim.
        status, _, answer = self.reclaim()
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "reclaim"), answer)
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "cancel", "3", "--expected-revision",
                                         self.lane_revision()]])

    def test_release_after_the_holder_changed_is_refused_before_any_verb(self):
        # The page showed one claim; another host took the entry since. The page's revision names the holder.
        revision = self.revision()
        entries = self.entries()
        entries[5] = {**entries[5], "holder": {"host": "build-2", "pid": 9, "token": "t2"}, "started_at": "2026-09-06T11:59:00Z"}
        self.list(entries)
        status, _, answer = self.reclaim(revision=revision)
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"]])

    def test_release_of_an_entry_that_is_not_the_running_one_is_refused_before_any_verb(self):
        for entry in ("e-other", ""):
            self.log.write_text("")
            status, _, answer = self.reclaim(entry=entry)
            self.assertIn(status, (400, 409), answer)
            self.assertEqual(self.calls()[1:], [])

    def test_release_of_an_entry_no_longer_running_is_refused_before_any_verb(self):
        # It finished and went back to pending: Release must never cancel the pending entry.
        entries = self.entries()
        entries[5] = {key: value for key, value in entries[5].items() if key not in ("holder", "step", "lease_until")}
        entries[5]["status"] = "pending"
        self.list(entries)
        status, _, answer = self.reclaim(revision=self.revision())
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"]])

    def test_release_without_the_entry_it_showed_runs_nothing(self):
        status, _, _ = self.reclaim(entry=None)
        self.assertEqual((status, self.calls()), (400, []))
        payload = {"repo": str(self.checkout), "item": 9, "action": "cancel", "revision": self.revision(), "entry": "e-3"}
        status, _, _ = self.post("/api/queue/move", payload)
        self.assertEqual((status, self.calls()), (400, []))

    def test_a_release_the_lane_refuses_is_the_answer(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:3 is running under pid 4242 on this machine; it is not stuck",
                                          "QUEUE_CODE": "holder_alive"}):
            status, _, answer = self.reclaim()
        self.assertEqual((status, answer["error"]), (400, "sd:3 is running under pid 4242 on this machine; it is not stuck"))

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

    def test_a_lane_refusal_is_the_answer(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:12 is running; the queue changes only between items"}):
            status, _, answer = self.move("up")
        self.assertEqual((status, answer["error"]), (400, "sd:12 is running; the queue changes only between items"))

    def test_the_page_and_its_document_answer(self):
        status, _, body = self.request("/queue", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn('<script src="/ui/queue.js?v=', body)
        status, _, body = self.request("/api/queue", headers={"Cookie": self.cookie})
        self.assertEqual((status, len(json.loads(body)["lanes"])), (200, 1))


def pinned_pack() -> Path:
    """The pack the gate pins (`SD_ACCEPTANCE_PACK`), else the `sd-ship` this machine runs."""
    named = os.environ.get("SD_ACCEPTANCE_PACK")
    return Path(named) if named else Path(os.path.realpath(queue_screen.command("sd-ship"))).parent.parent


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
        subprocess.run([sys.executable, "-c", SEED, str(pack / "bin"), str(self.path), "example/system",
                        json.dumps(self.seeds())], check=True, capture_output=True, text=True,
                       env={**os.environ, "HOME": str(home)})
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "sd-ship").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ship}" "$@"\n')
        (bin_dir / "sd-ship").chmod(0o755)
        patch = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "SD_LANE_ROOT": str(root),
                                             "HOME": str(home)})
        patch.start()
        self.addCleanup(patch.stop)

    def seeds(self):
        return [{"id": f"seed-{n}", "item": n, "title": f"item {n}", "status": "pending", "position": n,
                 "expected_head": "e" * 40, "repository": "example/system", "branch": f"b{n}"} for n in (1, 2, 3)]

    def order(self):
        listed = queue_screen.lane_list(str(self.checkout))
        return [entry["item"] for entry in listed["entries"] if entry["status"] == "pending"]


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
    """Release on the pinned pack's `lane cancel`: a holder on another host is released; one alive here is refused."""

    def seeds(self):
        running = {**super().seeds()[0], "status": "running", "step": "prepare", "started_at": "2026-09-06T11:50:00Z",
                   "lease_until": "2026-09-06T13:00:00Z", "holder": self.holder(), "enqueued_on": "build-2"}
        return [running, *super().seeds()[1:]]

    def release(self):
        lane = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        merging = lane["rows"][0]
        self.assertEqual((merging["state"], merging["entry"]), ("merging", "seed-1"))
        return self.post("/api/queue/move", {"repo": str(self.checkout), "item": 1, "action": "reclaim",
                                             "revision": lane["revision"], "entry": merging["entry"]}), merging

    def status(self):
        return [(entry["item"], entry["status"]) for entry in queue_screen.lane_list(str(self.checkout))["entries"]]


class TheRealReleaseOfARemoteHolder(TheRealRelease):

    def holder(self):
        return {"host": "build-2", "pid": 4242, "token": "claim-token"}

    def test_release_puts_a_prepare_held_on_another_host_back_to_pending(self):
        (status, _, answer), merging = self.release()
        self.assertEqual((merging["host"], merging["step"]), ("build-2", "prepare"))
        self.assertEqual((status, answer.get("ok")), (200, True), answer)
        self.assertEqual(self.status(), [(1, "pending"), (2, "pending"), (3, "pending")])

    def test_a_queue_write_between_the_check_and_the_verb_refuses_the_release(self):
        listed = queue_screen.lane_list

        def then_another_writer(path):
            answer = listed(path)
            queue_screen._run([queue_screen.command("sd-ship"), "-C", path, "lane", "move", "3", "top"], 30)
            return answer
        with mock.patch.object(queue_screen, "lane_list", then_another_writer):
            (status, _, answer), _ = self.release()
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertEqual(self.status()[0], (1, "running"))


class TheRealReleaseOfALocalHolder(TheRealRelease):

    def holder(self):
        return {"host": self.host, "pid": os.getpid(), "token": "claim-token"}

    def test_release_of_a_holder_alive_on_this_machine_is_refused(self):
        (status, _, answer), merging = self.release()
        self.assertEqual(merging["host"], self.host)
        self.assertEqual(status, 400, answer)
        self.assertIn("not stuck", answer["error"])
        self.assertEqual(self.status()[0], (1, "running"))


class TheScript(Lane, ScreenCase):
    """queue.js against the document `queue_screen` builds from the fixture."""

    def run_page(self, body, move=(200, {"ok": True, "edits": "Edits take effect at the next item boundary."})):
        doc = queue_screen.document(self.connection, now=NOW)
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + READ_SHELL
                  + f"\nvar DOC = {json.dumps(doc)}, MOVE = {json.dumps(list(move))};\n"
                  + "ANSWER = (path, body) => path === '/api/queue' ? [200, DOC] : path === '/api/queue/move' ? MOVE : [404, { error: 'no answer' }];\n"
                  + QUEUE_JS + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
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
            ("blocked", "caution", "▲", "BLOCKED"), ("blocked", "caution", "▲", "BLOCKED"), ("landed", "ok", "●", "LANDED")])
        self.assertIn("<b>operator acts</b>", lanes)
        self.assertIn("PR #161", lanes)

    def test_the_merging_row_names_step_host_claim_age_and_lease(self):
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        merging = next(row for row in lanes.split("<li ")[1:] if 'data-state="merging"' in row)
        self.assertIn("<b>merge</b> on <b>hub-1</b> · claimed 10m 0s ago · lease to 13:20 · head " + "a" * 12, merging)

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
        out = self.run_page("R.lanes = ELS.lanes.html; R.subhead = ELS.subhead.html;")
        lanes = out["R"]["lanes"]
        self.assertIn('<span class="host">lane on hub</span>', lanes)
        self.assertNotIn('aria-labelledby="lane-quiet"', lanes)
        self.assertIn('<p class="idle">Idle: <code>quiet</code> (lane on build-2)</p>', lanes)
        self.assertIn("2 lanes of 2 managed repositories", out["R"]["subhead"])

    def test_a_running_row_with_a_holder_offers_release(self):
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        self.assertEqual(re.findall(r'data-act="reclaim" data-item="(\d+)" data-entry="([\w-]+)"', lanes), [("3", "e-3")])
        offered = [row for row in lanes.split("<li ")[1:] if 'data-act="reclaim"' in row]
        self.assertEqual([re.search(r'data-state="(\w+)"', row)[1] for row in offered], ["merging"])

    def test_a_running_row_without_a_holder_offers_no_release(self):
        # An entry an earlier pack wrote names no claim, so there is nothing the page can show it releases.
        entries = self.entries()
        entries[5] = {key: value for key, value in entries[5].items() if key != "holder"}
        self.list(entries)
        lanes = self.run_page("R.lanes = ELS.lanes.html;")["R"]["lanes"]
        self.assertNotIn('data-act="reclaim"', lanes)

    def release(self, answer, step="merge", move=(200, {"ok": True, "action": "reclaim", "item": 3, "edits": "Edits take effect at the next item boundary."})):
        entries = self.entries()
        entries[5] = {**entries[5], "step": step}
        self.list(entries)
        return self.run_page(f"window.shell.confirm = a => {{ OUT.asked = a; return Promise.resolve({json.dumps(answer)}); }};\n"
                             f"await window.queueAct({{ dataset: {{ repo: {json.dumps(str(self.checkout))}, item: '3', act: 'reclaim', entry: 'e-3' }} }}); R.lanes = ELS.lanes.html;", move)

    def test_release_asks_first_naming_the_holder_step_claim_age_and_command(self):
        out = self.release(True)
        asked = out["asked"]
        self.assertIn("sd:3", asked["title"])
        self.assertIn("hub-1", asked["title"])
        self.assertIn("hub-1 claimed it 10m 0s ago, at merge.", asked["body"])
        self.assertIn("A merge it already started may still land", asked["body"])
        self.assertEqual(asked["cli"], f"sd-ship -C '{self.checkout}' lane cancel 3")
        revision = queue_screen.document(self.connection, now=NOW)["lanes"][0]["revision"]
        self.assertEqual(out["posts"][0][1], {"repo": str(self.checkout), "item": 3, "action": "reclaim",
                                              "revision": revision, "entry": "e-3"})
        self.assertIn("sd:3 released from its holder.", out["R"]["lanes"])

    def test_release_at_prepare_says_the_entry_goes_back_to_pending(self):
        asked = self.release(True, step="prepare")["asked"]
        self.assertIn("goes back to pending at its place", asked["body"])

    def test_release_kept_at_the_confirm_posts_nothing(self):
        out = self.release(False)
        self.assertEqual((out["posts"], out["gets"]), ([], ["/api/queue"]))

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

    def test_cancel_kept_at_the_confirm_posts_nothing(self):
        out = self.cancel(False)
        self.assertEqual((out["posts"], out["gets"]), ([], ["/api/queue"]))

    def test_a_refused_cancel_says_why_in_its_lane(self):
        out = self.cancel(True, move=(400, {"error": "sd:9 has no pending entry in this lane"}))
        self.assertIn('<p class="fail" role="alert">sd:9 not cancelled: sd:9 has no pending entry in this lane The queue is unchanged.</p>',
                      out["R"]["lanes"])


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


if __name__ == "__main__":
    unittest.main()
