"""`POST /api/snooze`: the write both Today and Health post (sd:1896)."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from sd_db import writes

from sd_dashboard import health_screen, now_screen, snooze

from test_workflow_actions import BrowserSession

NOW = "2026-09-06T12:00:00Z"
UNTIL = "2026-09-07T08:00:00+00:00"


def later(**delta) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).isoformat(timespec="seconds")


class TheSnoozeRoute(BrowserSession):
    def held(self):
        return writes.snoozed(self.connection, now=datetime.now(UTC).isoformat())

    def test_a_snooze_holds_the_row_and_a_null_time_shows_it_again(self):
        until = later(hours=1)
        status, _, body = self.post("/api/snooze", {"page": "today", "row": "ahead:pushy:1", "until": until, "seen": "0f0f"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"key": "today:ahead:pushy:1", "until": writes.stamp(until)})
        self.assertEqual(self.held(), {"today:ahead:pushy:1": {"until": writes.stamp(until), "seen": "0f0f"}})
        status, _, body = self.post("/api/snooze", {"page": "today", "row": "ahead:pushy:1", "until": None, "seen": None})
        self.assertEqual((status, body), (200, {"key": "today:ahead:pushy:1", "until": None}))
        self.assertEqual(self.held(), {})

    def test_a_health_row_keys_on_its_own_page(self):
        self.assertEqual(self.post("/api/snooze", {"page": "health", "row": "br:system", "until": later(days=7), "seen": "0f0f"})[0], 200)
        self.assertEqual(list(self.held()), ["health:br:system"])

    def test_a_request_it_cannot_judge_writes_nothing(self):
        good = {"page": "today", "row": "ahead:pushy:1", "until": later(hours=1), "seen": "0f0f"}
        for bad in ({**good, "page": "tasks"}, {**good, "row": ""}, {**good, "row": 7}, {**good, "until": 3600},
                    {"page": "today", "row": "ahead:pushy:1", "until": later(hours=1)}, {**good, "extra": 1},
                    {**good, "until": later(hours=-1)}, {**good, "until": later(days=32)},
                    {**good, "until": "next tuesday"}, {**good, "seen": None}, {**good, "seen": ""}, {**good, "seen": 7},
                    {**good, "seen": "f" * 129}):
            with self.subTest(bad=bad):
                before = self.snapshot()
                status, _, body = self.post("/api/snooze", bad)
                self.assertEqual(status, 400, body)
                self.assertTrue(body["error"])
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.post("/api/snooze", good, Origin="https://example.invalid")[0], 403)
        self.assertEqual(self.held(), {})

def managed(**alerts) -> dict:
    return {"repo": "/checkouts/alpha", "slug": "group/alpha", "managed": True, "observed_at": NOW, "alerts": alerts}


def volume(capacity: int, avail_kb: int) -> dict:
    return {"volumes": [{"mount": "/Volumes/data", "capacity": capacity, "avail_kb": avail_kb, "size_kb": 1024 ** 3,
                         "filesystem": "apfs"}],
            "storage": [{"root": "/data", "error": "", "folders": [{"path": "/data/runs", "kb": avail_kb}]}],
            "config": "/config/disk.conf", "refused": [], "build": {"merged": [], "checked": 0, "unread": []}}


def credential(expires: str, *, now: str = NOW) -> dict:
    probe = {"id": "gh", "name": "GitHub token", "present": True, "valid": True, "expires": expires}
    return health_screen._credential_row(probe, health_screen._when(now))


def job(code: int, log_time: str) -> dict:
    row = now_screen.job_rows([{"name": "nightly", "state": "failed", "last_exit": code, "service": "gui/501/x"}], None)[0]
    return {**row, "detail": row["detail"].replace("log unknown", f"log {log_time}")}


#: Each row kind a snooze can hide, as the operator saw it and as it reads next: the same id, with a problem that
#: changed (it must show again) or with only a measure that drifts with the clock or the disk (it stays hidden).
CHANGED = {
    "dependabot alerts: a critical alert joins a low one": (
        "health", health_screen._dependency_rows([managed(dependabot={"open": 1, "severity": {"low": 1}})], now=NOW)[0][0],
        health_screen._dependency_rows([managed(dependabot={"open": 2, "severity": {"low": 1, "critical": 1}})], now=NOW)[0][0]),
    "dependabot alerts: the same count, worse severity": (
        "health", health_screen._dependency_rows([managed(dependabot={"open": 2, "severity": {"high": 1, "low": 1}})], now=NOW)[0][0],
        health_screen._dependency_rows([managed(dependabot={"open": 2, "severity": {"critical": 2}})], now=NOW)[0][0]),
    "secret scanning: another open alert": (
        "health", health_screen._security_rows([managed(secret_scanning={"open": 1})], now=NOW)[0][0],
        health_screen._security_rows([managed(secret_scanning={"open": 2})], now=NOW)[0][0]),
    "volume: caution becomes warning": (
        "health", health_screen._disk_rows(volume(82, 200 * 1024 ** 2))[0][0],
        health_screen._disk_rows(volume(95, 50 * 1024 ** 2))[0][0]),
    "credential: expiry moves inside the warning days": (
        "health", credential("2026-10-01T00:00:00Z"), credential("2026-09-10T00:00:00Z")),
    "merged branches: one more left undeleted": (
        "health",
        health_screen._branch_rows({"merged": [{"repo": "/c/alpha", "path": "/c/alpha", "deletable": [("a", "2026-09-01")],
                                                "checked_out": []}], "repos": 1, "no_default": [], "unread": []})[0],
        health_screen._branch_rows({"merged": [{"repo": "/c/alpha", "path": "/c/alpha",
                                                "deletable": [("a", "2026-09-01"), ("b", "2026-09-02")], "checked_out": []}],
                                    "repos": 1, "no_default": [], "unread": []})[0]),
    "collector: dark for another reason": (
        "today", now_screen.dark_row("prs", "shadow table unreadable"), now_screen.dark_row("prs", "database is locked")),
    "repo: unpushed commits, now with a dirty tree": (
        "today", now_screen.backbone_rows([{"name": "pushy", "ahead": 1, "dirty": 0}])[0],
        now_screen.backbone_rows([{"name": "pushy", "ahead": 1, "dirty": 3}])[0]),
}
DRIFTED = {
    "volume: another percent and fewer bytes free, still caution": (
        "health", health_screen._disk_rows(volume(82, 200 * 1024 ** 2))[0][0],
        health_screen._disk_rows(volume(84, 180 * 1024 ** 2))[0][0]),
    "storage folder: it grew": (
        "health", health_screen._disk_rows(volume(10, 200 * 1024 ** 2))[0][0],
        health_screen._disk_rows(volume(10, 210 * 1024 ** 2))[0][0]),
    "credential: a day nearer the same expiry": (
        "health", credential("2026-10-01T00:00:00Z"), credential("2026-10-01T00:00:00Z", now="2026-09-07T12:00:00Z")),
    "credentials: stale a day longer": (
        "health", health_screen._credential_rows(("2026-09-01T00:00:00Z", {"probes": []}), now=NOW)[0][-1],
        health_screen._credential_rows(("2026-09-01T00:00:00Z", {"probes": []}), now="2026-09-08T12:00:00Z")[0][-1]),
    "pull request: quiet a day longer": (
        "today", now_screen.pr_rows([{"kind": "pull", "needs_you": True, "repo": "group/alpha", "number": 7, "title": "t",
                                      "first_seen": "2026-08-01"}], NOW)[0],
        now_screen.pr_rows([{"kind": "pull", "needs_you": True, "repo": "group/alpha", "number": 7, "title": "t",
                             "first_seen": "2026-08-01"}], "2026-09-07")[0]),
    "job: the same failure, logged by a later run": (
        "today", job(1, "2026-09-06 02:00"), job(1, "2026-09-07 02:00")),
}


class TheFingerprint(unittest.TestCase):
    """A snooze holds the problem the operator saw, not the row's id (sd:1896 review): a changed problem shows again."""

    def held(self, page: str, seen: dict, now: dict) -> tuple[list[dict], list[dict]]:
        self.assertEqual(seen["id"], now["id"], "the case must keep the id: the id alone is what the snooze matched")
        shown, _ = snooze.split(page, [seen], {})
        return snooze.split(page, [now], {snooze.key(page, seen["id"]): {"until": UNTIL, "seen": shown[0].get("seen")}})

    def test_a_row_whose_problem_changed_under_the_same_id_shows_again(self):
        for case, (page, seen, now) in CHANGED.items():
            with self.subTest(case):
                shown, hidden = self.held(page, seen, now)
                self.assertEqual(([row["id"] for row in shown], hidden), ([now["id"]], []))

    def test_a_row_whose_measure_only_drifted_stays_snoozed(self):
        for case, (page, seen, now) in DRIFTED.items():
            with self.subTest(case):
                self.assertNotEqual((seen["what"], seen["detail"]), (now["what"], now["detail"]), "the case must drift")
                shown, hidden = self.held(page, seen, now)
                self.assertEqual((shown, [(row["id"], row["until"]) for row in hidden]), ([], [(now["id"], UNTIL)]))

    def test_the_unchanged_row_stays_snoozed_and_says_what_it_was_bound_to(self):
        page, seen, _ = CHANGED["dependabot alerts: a critical alert joins a low one"]
        shown, hidden = self.held(page, seen, seen)
        self.assertEqual(shown, [])
        self.assertEqual((hidden[0]["until"], len(hidden[0]["seen"])), (UNTIL, 16))
