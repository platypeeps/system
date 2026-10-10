"""The v2 Queue page (sd:2585).

What this slice promises: `/api/queue` draws each registered repository's lane from `sd-ship lane list`, one row per
item in one of five states (merging, next, building, blocked, landed), with load5 and the gate slots in the header.
The only write is `/api/queue/move`: it runs `sd-ship lane move|hold|release|cancel|retry` with exactly the page's item and
place, under the session cookie, CSRF and Origin checks, and refuses a revision the queue has moved past. Retry and Approve
(`retry --manual`) show on blocked rows only when `sd-ship lane retry --help` answers (sd:3012); Approve only on a manual repo.

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
  *"lane list") cat "$QUEUE_FIXTURE" ;;
  "lane retry --help") if [ -n "$QUEUE_NO_RETRY" ]; then echo "invalid choice: 'retry'" >&2; exit 2; fi; echo usage ;;
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
        self.repo(str(self.checkout))
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
             "finished_at": "2026-09-06T09:00:00Z"},
            {"item": 4, "title": "Moved head", "status": "skipped", "reason": "the worktree's HEAD moved from the queued head",
             "finished_at": "2026-09-06T09:30:00Z"},
            {"item": 3, "title": "Merge the slice", "status": "running", "expected_head": "a" * 40,
             "started_at": "2026-09-06T11:50:00Z"},
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

    def test_merging_names_its_phase_and_elapsed_time(self):
        merging = self.doc()["lanes"][0]["rows"][0]
        self.assertEqual((merging["phase"], merging["elapsed"], merging["head"]), ("merge", 600, "a" * 12))

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

    def test_a_lane_that_cannot_be_listed_is_named_not_dropped(self):
        self.fixture.write_text("not json")
        doc = self.doc()
        self.assertEqual((doc["lanes"], [p["repo"] for p in doc["problems"]]), ([], [str(self.checkout)]))


class TheMove(Lane, BrowserSession):
    """`/api/queue/move` runs one lane verb, and only under the page's session, CSRF and Origin."""

    def revision(self):
        return queue_screen.revision(json.loads(self.fixture.read_text())["entries"])

    def move(self, action="up", item=12, **headers):
        payload = {"repo": str(self.checkout), "item": item, "action": action, "revision": self.revision()}
        return self.post("/api/queue/move", payload, **headers)

    def test_a_reorder_calls_exactly_lane_move_with_the_item_place_and_revision(self):
        status, _, answer = self.move("up")
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "move", "12", "up", "--expected-revision", self.revision()]])

    def test_top_hold_and_release_call_their_verbs_with_the_revision(self):
        for action, verb in (("top", ["move", "12", "top"]), ("hold", ["hold", "12"]), ("release", ["release", "12"])):
            self.log.write_text("")
            status, _, _ = self.move(action)
            self.assertEqual((status, self.calls()[-1]),
                             (200, ["-C", str(self.checkout), "lane", *verb, "--expected-revision", self.revision()]))

    def test_a_verb_that_refuses_a_stale_revision_asks_the_page_to_read_again(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "the queue changed since revision x", "QUEUE_CODE": "stale_revision"}):
            status, _, answer = self.move("hold")
        self.assertEqual((status, answer.get("reload")), (409, True), answer)

    def test_cancel_calls_exactly_lane_cancel_with_the_item(self):
        status, _, answer = self.move("cancel", item=9)
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "cancel"))
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "cancel", "9"]])

    def test_a_refused_cancel_is_the_answer(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:9 has no pending entry in this lane"}):
            status, _, answer = self.move("cancel", item=9)
        self.assertEqual((status, answer["error"]), (400, "sd:9 has no pending entry in this lane"))

    def test_retry_calls_exactly_lane_retry_with_the_item(self):
        status, _, answer = self.move("retry", item=4)
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "retry"))
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "retry", "4"]])

    def test_approve_calls_lane_retry_with_manual(self):
        status, _, answer = self.move("approve", item=5)
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "approve"))
        self.assertEqual(self.calls()[-1], ["-C", str(self.checkout), "lane", "retry", "5", "--manual"])

    def test_approve_on_an_auto_repo_runs_nothing(self):
        # runner_merge=auto needs no grant; the page offers no Approve there, and a stale page is refused too.
        upsert_repo(self.connection, str(self.checkout), runner_merge="auto")
        self.connection.commit()
        status, _, _ = self.move("approve", item=5)
        self.assertEqual((status, self.calls()), (400, []))

    def test_a_refused_retry_is_the_answer(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:4's last entry kept no body copy"}):
            status, _, answer = self.move("retry", item=4)
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


class TheRealLane(BrowserSession):
    """The pinned pack's `sd-ship lane` on a temporary lane root: the page's reorder is what `lane list` shows."""

    def setUp(self):
        super().setUp()
        self.checkout = Path(self.tmp.name) / "system"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q", "-b", "main")
        self.repo(str(self.checkout))
        root = Path(self.tmp.name) / "lanes"
        queue = root / "system" / "lane" / "queue"
        queue.mkdir(parents=True)
        entries = [{"item": n, "title": f"item {n}", "status": "pending", "expected_head": "e" * 40} for n in (1, 2, 3)]
        (queue / "queue.json").write_text(json.dumps({"entries": entries}))
        ship = pinned_pack() / "bin" / "sd-ship"
        self.assertTrue(ship.is_file(), f"no pinned sd-ship at {ship}")
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "sd-ship").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ship}" "$@"\n')
        (bin_dir / "sd-ship").chmod(0o755)
        patch = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "SD_LANE_ROOT": str(root)})
        patch.start()
        self.addCleanup(patch.stop)

    def order(self):
        listed = queue_screen.lane_list(str(self.checkout))
        return [entry["item"] for entry in listed["entries"] if entry["status"] == "pending"]

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
        self.assertEqual(re.findall(r'data-act="approve" data-item="(\d+)"', lanes), ["5", "4"])
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
        """Click a blocked row's button; a confirm answers `answer` and records what it asked."""
        return self.run_page(f"window.shell.confirm = a => {{ OUT.asked = a; return Promise.resolve({json.dumps(answer)}); }};\n"
                             f"await window.queueAct({{ dataset: {{ repo: {json.dumps(str(self.checkout))}, item: '{item}', act: '{act}' }} }}); R.lanes = ELS.lanes.html;", move)

    def test_retry_posts_at_once_and_says_the_entry_is_queued_again(self):
        out = self.click("retry", 4)
        self.assertNotIn("asked", out)
        self.assertEqual(out["posts"][0][1]["action"], "retry")
        self.assertIn("sd:4 queued again.", out["R"]["lanes"])

    def test_approve_asks_first_and_names_the_entry_and_command(self):
        out = self.click("approve", 5)
        self.assertIn("sd:5", out["asked"]["title"])
        self.assertEqual(out["asked"]["cli"], f"sd-ship -C '{self.checkout}' lane retry 5 --manual")
        self.assertEqual(out["posts"][0][1]["action"], "approve")
        self.assertIn("sd:5 approved and queued again.", out["R"]["lanes"])

    def test_approve_kept_at_the_confirm_posts_nothing(self):
        out = self.click("approve", 5, answer=False)
        self.assertEqual((out["posts"], out["gets"]), ([], ["/api/queue"]))


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "queue", "Queue", "/queue", ("/api/queue", "/api/queue/move")


if __name__ == "__main__":
    unittest.main()
