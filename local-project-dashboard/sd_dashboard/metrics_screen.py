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
- `controls`: the provider registry as `provider_controls.snapshot` reads it, with its `revision`, and per bill the
  one cap write the route takes (`_writes`), the read the classic Usage area's forms make (sd:3387).

`usage` and `numbers` take seconds (`SLOW`): the served dashboard keeps them in a snapshot, and each then carries
`observed`, when it was read, and `stale`, why its newest read failed (sd:4955). A stale month lights no lamp.

Each part is guarded on its own: one that raises is that part's `error`, and
the others still answer. `unread` names the design's panels no reader covers,
with the reason, so the page shows them as unknown and never as empty.
Nothing here writes or reaches the network. The page's Cap and Configure post `controls.revision` to
`/api/bills/<bill>/cap` and `/api/providers/configure` (`server.action_route`), whose library refuses it once the
registry, a bill's cap or the order policy changed since this read.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from sd_db import provider_controls, reads, registry, reporting, usage
from sd_db.errors import SdDbError

from .operations_screen import BILL_NAME

__all__ = ["SKILL_WEEKS", "SLOW", "TREND_DAYS", "UNREAD", "WINDOW", "document", "slow"]

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

#: The parts that take seconds, which the served dashboard keeps in a `snapshot.Kept` (sd:4955).
SLOW = ("usage", "numbers")


def _stamp(now: str) -> datetime:
    return datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc)


def _guard(read):
    try:
        return read()
    except FAILURES as error:
        return {"error": f"{error.__class__.__name__}: {error}"}


def _attention(spend: dict, trend: dict) -> dict:
    """The rail badge (sd:3446): the lamps metrics.js can light, each week window at 75 % or more and the estimated
    spend rows. An unread usage is unknown: no lamp could be read."""
    from .v2.pages import attention

    if spend["error"] or spend.get("stale"):
        return {"state": "unknown", "n": 0, "what": "readings not read"}

    def level(used):
        number = isinstance(used, (int, float)) and not isinstance(used, bool)
        return "warning" if number and used >= 100 else "caution" if number and used >= 75 else ""

    window = trend.get("window")
    cells = [level(meter["used_percent"]) for meter in spend["meter"] if meter["window_minutes"] == window]
    return attention([*cells, "caution" if spend["bound"] else ""], {"caution": "readings past a threshold"})


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


def _writes(name, spawned, held):
    """The cap write a bill takes, and why not when it takes none: `set`, `clear` or `""`.

    A bill a `start` entry is billed to takes no cap (`set_cap` refuses it with the registry's sentence), except the
    clear of a legacy row cap the merged view hides, the repair `set_cap(..., None)` makes. A name outside `BILL_NAME`
    has no route. The rule is `operations_screen._bill_caps`', so both screens offer the same writes until slice 10.
    """
    if not re.fullmatch(BILL_NAME, name):
        return "", "its name is outside what the cap route accepts"
    if name in spawned and held.get(name) is not None:
        return "clear", f"the row holds a cap of ${held[name]:,.2f} that nothing enforces; clearing it is the one write this bill takes"
    if name in spawned:
        return "", "a 'start' entry is billed to it, so no cap can be set"
    return "set", ""


def _controls(connection):
    current = provider_controls.snapshot(connection)
    spawned = {entry.bill for entry in registry.read(connection=connection).providers.values() if entry.kind == "start"}
    held = {row["name"]: row["cap_usd_month"] for row in reads.cost_by_bill(connection)}
    bills = []
    for bill in current["bills"]:
        write, why = _writes(bill["name"], spawned, held)
        bills.append({**bill, "write": write, "why": why})
    return {"error": "", "revision": current["revision"], "orders": current["orders"], "warnings": current["warnings"], "bills": bills,
            "providers": [{key: entry[key] for key in ("name", "vendor", "enabled", "reason", "roles")} for entry in current["providers"]]}


def slow(connection, name: str, *, now: str) -> dict:
    """One part of `SLOW`, read now; it raises what its read raised."""
    if name == "usage":
        return {"error": "", **usage.read(connection, month=None, now=now).document()}
    if name == "numbers":
        return _numbers(connection, now)
    raise ValueError(f"{name!r} is not a slow Metrics part")


def document(connection, *, now: str, kept=None) -> dict:
    """The Metrics page's document: every reading, each guarded on its own, and the panels no reader covers.

    `kept(name)` answers a `SLOW` part from the server's snapshot, with `observed` and `stale`; without it they are
    read now.
    """
    today = _stamp(now).date()
    read_slow = kept or (lambda name: slow(connection, name, now=now))
    spend, trend = _guard(lambda: read_slow("usage")), _guard(lambda: _trend(connection, today))
    return {
        "read": now,
        "attention": _attention(spend, trend),
        "usage": spend,
        "trend": trend,
        "numbers": _guard(lambda: read_slow("numbers")),
        "scorecard": _guard(lambda: _scorecard(connection, now)),
        "skills": _guard(lambda: _skills(connection, today)),
        "age": _guard(lambda: _age(connection, now)),
        "observed": _guard(lambda: {"error": "", **reporting.metrics(connection, now=now)}),
        "controls": _guard(lambda: _controls(connection)),
        "unread": list(UNREAD),
    }
