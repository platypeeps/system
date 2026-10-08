"""Metrics: the readings behind the default UI's Metrics page (sd:2119).

The page is `v2/metrics.html`; it holds no data. It reads one JSON document,
`/api/metrics`, built here by `document` from the reads the classic Usage and
Progress areas already make:

- `usage`: the month as `sd usage --json` prints it (`usage.read`), so the
  page, the classic screen and the CLI cannot disagree about a dollar
  (requirement 6). Dollars are the month to date, not a week: a week of spend
  would be a second computation of the same ledger.
- `trend`: the last 7-day-window meter reading of each day for the last week
  (`reads.meter_days`).
- `numbers`: the week's four numbers (`reads.weekly_numbers`).
- `scorecard`: the providers this month (`reads.scorecard`).
- `skills`: skill uses per day and skill for four weeks (`reads.skill_use_days`).
- `age`: open items by days in their current status (`reads.age_histogram`).
- `observed`: observed workflow activity (`reporting.metrics`).

Each part is guarded on its own: one that raises is that part's `error`, and
the others still answer. `unread` names the design's panels no reader covers,
with the reason, so the page shows them as unknown and never as empty.
Nothing here writes or reaches the network.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from sd_db import reads, reporting, usage
from sd_db.errors import SdDbError

__all__ = ["SKILL_WEEKS", "TREND_DAYS", "UNREAD", "WINDOW", "document"]

#: The vendor window the trend draws: seven days, in minutes.
WINDOW = 10080

#: Days of window readings the trend carries, ending today.
TREND_DAYS = 7

#: Monday-start weeks of skill use, this week included.
SKILL_WEEKS = 4

#: The design's panels no reader covers, and why; the page draws each as unknown with its reason.
UNREAD = (
    {"id": "model", "name": "By model", "reason": "the cost ledger records bill and provider, not model; judgment.model covers Jev calls only"},
    {"id": "ci", "name": "CI health", "reason": "GitHub Actions is off (local CI since 2026-09-27) and sd.db stores no sd/local-gate run; reading runs needs a network call"},
    {"id": "flaky", "name": "Flaky tests", "reason": "no test-result store exists, so no test is known to have failed and then passed"},
)

FAILURES = (SdDbError, sqlite3.Error, OSError, ValueError)


def _stamp(now: str) -> datetime:
    return datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc)


def _guard(read):
    try:
        return read()
    except FAILURES as error:
        return {"error": f"{error.__class__.__name__}: {error}"}


def _trend(connection, today):
    since = today - timedelta(days=TREND_DAYS - 1)
    days = [(since + timedelta(days=n)).isoformat() for n in range(TREND_DAYS)]
    series = {}
    for row in reads.meter_days(connection, window_minutes=WINDOW, since=since.isoformat()):
        series.setdefault(row["provider"], {"provider": row["provider"], "bill": row["bill"], "points": []})["points"].append(
            [row["day"], row["used_percent"], row["timestamp"]])
    return {"error": "", "window": WINDOW, "days": days, "series": list(series.values())}


def _numbers(connection, now):
    return {"error": "", "rows": [{"key": n.key, "label": n.label, "value": n.value, "unit": n.unit, "inputs": [list(i) for i in n.inputs]}
                                  for n in reads.weekly_numbers(connection, now=now)]}


def _scorecard(connection, now):
    return {"error": "", "rows": [{"provider": r.provider, "enabled": r.enabled, "reason": r.reason or "", "author_rank": r.author_rank,
                                   "reviewer_rank": r.reviewer_rank, "passes": r.passes, "blocking": r.blocking, "usd": r.usd,
                                   "usd_per_pass": r.usd_per_pass, "fallthrough": r.fallthrough} for r in reads.scorecard(connection, now=now)]}


def _skills(connection, today):
    monday = today - timedelta(days=today.weekday())
    starts = [monday - timedelta(weeks=n) for n in range(SKILL_WEEKS)]
    weeks = {start.isoformat(): 0 for start in starts}
    top: dict[str, int] = {}
    rows = reads.skill_use_days(connection, since=starts[-1].isoformat())
    for row in rows:
        day = datetime.strptime(row["day"], "%Y-%m-%d").date()
        start = (day - timedelta(days=day.weekday())).isoformat()
        if start in weeks:
            weeks[start] += row["uses"]
        if start == monday.isoformat():
            top[row["skill"]] = top.get(row["skill"], 0) + row["uses"]
    return {"error": "", "since": starts[-1].isoformat(), "first": rows[0]["day"] if rows else "",
            "weeks": [{"start": start, "uses": uses} for start, uses in weeks.items()],
            "top": sorted(([skill, uses] for skill, uses in top.items()), key=lambda pair: (-pair[1], pair[0]))[:10]}


def _age(connection, now):
    rows = [row for row in reads.backlog_items(connection, now=now) if row["status"] != "done"]
    return {"error": "", "buckets": [{"label": b.label, "lower": b.lower, "upper": b.upper, "ready_to_send": b.counts["ready_to_send"],
                                      "other": b.counts["other"]} for b in reads.age_histogram(rows, now=now)]}


def document(connection, *, now: str) -> dict:
    """The Metrics page's document: every reading, each guarded on its own, and the panels no reader covers."""
    today = _stamp(now).date()
    return {
        "read": now,
        "usage": _guard(lambda: {"error": "", **usage.read(connection, month=None, now=now).document()}),
        "trend": _guard(lambda: _trend(connection, today)),
        "numbers": _guard(lambda: _numbers(connection, now)),
        "scorecard": _guard(lambda: _scorecard(connection, now)),
        "skills": _guard(lambda: _skills(connection, today)),
        "age": _guard(lambda: _age(connection, now)),
        "observed": _guard(lambda: {"error": "", **reporting.metrics(connection, now=now)}),
        "unread": list(UNREAD),
    }
