"""Briefs: the document behind the default UI's Briefs page (sd:2112).

The page is `v2/briefs.html`; it holds no rows. It reads one JSON document,
`/api/briefs`, built here by `document`.

**Which briefs.** The design lists brief mail. The dashboard has no mail
reader; the brief reader it has is `collectors.collect_briefs`, the notes the
scheduled routines file in the vault's `System/AI Generated/Briefs` folder,
which Research > Resources > Briefs shows as a table. This page reads the same
notes through the same child: `sd_tile.py briefs-rows`, run under
`collectors.Budget` as `reports_screen.collect` runs a Resources view, so a
vault that macOS holds behind a prompt is a refusal with its reason and never
a wait. The tile caps the rows (`BRIEF_ROWS`) and their bytes
(`BRIEF_JSON_BYTES`); `reader.total` and `reader.shown` say when it did.

A row is one note: its file stem is the subject, the kind its file name gives
is the source, and its modification time is when it was sent. A time is given
only when it falls on the note's own day; a note changed later keeps its day
and has no time (`at` is null).

**What is not read.** Unread state, follow-up flags, the watchdog's failures
and each job's cadence come from mail and the weekly digest in the design. No
reader supplies them: `watchdog.available` is false and `watchdog.reason`
says so, and the page shows them unknown. Nothing here writes.
"""

from __future__ import annotations

import datetime
import json
import re
import sys

from . import reports_screen

__all__ = ["ROWS", "SECONDS", "WATCHDOG_REASON", "collect", "document", "rows"]

#: The child's time ceiling, a Resources view's (`reports_screen.VIEW_SECONDS`).
SECONDS = reports_screen.VIEW_SECONDS["briefs"]
#: The most rows the page takes, whatever the child sends; the tile's own cap is the same.
ROWS = 200
#: The longest lead the page shows, in characters (`collectors.BRIEF_LEAD`).
LEAD = 200
#: Why no brief has a failure, a cadence or an unread state.
WATCHDOG_REASON = "no watchdog reader: the dashboard does not read the failures the weekly digest lists"
SOURCE = "<vault>/System/AI Generated/Briefs"

DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def collect() -> dict:
    """The `briefs-rows` tab, read from its child within `SECONDS` and the shared 64 KB."""
    module = reports_screen._collectors()
    budget = module.Budget(SECONDS)
    try:
        process = budget.run([sys.executable, "-I", str(reports_screen.TILE), "briefs-rows"], label="briefs-rows")
    except module.OverBudget as error:
        raise ValueError(f"the brief reader was stopped at its budget: {error}") from None
    if process.returncode:
        raise ValueError(process.stderr.strip() or f"the brief reader exited {process.returncode} without a reason")
    got = json.loads(process.stdout)
    if not isinstance(got, dict) or not isinstance(got.get("briefs"), list):
        raise ValueError("the brief reader returned incomplete output")  # noqa: TRY004 - external document validation
    return got


def _row(raw) -> dict | None:
    """One page row from one tile row, or None when the row is not one."""
    if not isinstance(raw, dict):
        return None
    stem, rel, kind, day, at = (raw.get(k) for k in ("stem", "rel", "kind", "day", "at"))
    words, lead, link = raw.get("words"), raw.get("lead"), raw.get("obsidian")
    if not all(isinstance(v, str) and v for v in (stem, rel, kind)) or not isinstance(at, str) or not AT.fullmatch(at):
        return None
    if not isinstance(day, str) or (day and not DAY.fullmatch(day)):
        return None
    if not isinstance(words, int) or isinstance(words, bool) or words < 0 or not isinstance(lead, str):
        return None
    try:  # The shape is not a date: 2026-02-30 matches DAY and is no day.
        datetime.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ")
        if day:
            datetime.date.fromisoformat(day)
    except ValueError:
        return None
    day = day or at[:10]
    return {"id": stem, "subj": stem, "src": kind, "day": day, "at": at if at[:10] == day else None,
            "words": words, "lead": lead[:LEAD], "rel": rel,
            "open": link if isinstance(link, str) and link.startswith("obsidian://") else None}


def rows(got: dict) -> tuple[list[dict], int]:
    """The page rows, newest first and at most `ROWS`, and how many tile rows were not rows."""
    out, bad, seen = [], 0, set()
    for raw in got.get("briefs", []):
        row = _row(raw)
        if row is None or row["id"] in seen:
            bad += 1
            continue
        seen.add(row["id"])
        out.append(row)
    out.sort(key=lambda r: (r["day"], r["at"] or "", r["id"]), reverse=True)
    return out[:ROWS], bad


def document(*, now: str, reader=None) -> dict:
    """The Briefs page's document: the vault's brief notes, and what is not read."""
    reader = collect if reader is None else reader
    watchdog = {"available": False, "reason": WATCHDOG_REASON}
    try:
        got = reader()
    except (ValueError, OSError) as error:
        return {"read": now, "reader": {"state": "error", "reason": str(error), "source": SOURCE, "total": 0, "shown": 0,
                                        "skipped": 0, "capped": False}, "briefs": [], "watchdog": watchdog}
    briefs, bad = rows(got)
    total = got.get("total") if isinstance(got.get("total"), int) else len(briefs)
    # Capped only when rows were cut: the tile stopped short of its total, or more briefs passed than the page takes.
    # A rejected row is skipped, not capped.
    raw = len(got.get("briefs", []))
    capped = total > raw or raw - bad > ROWS
    return {"read": now,
            "reader": {"state": "partial" if bad else "read", "reason": f"{bad} rows were not briefs" if bad else "",
                       "source": SOURCE, "total": max(total, len(briefs)), "shown": len(briefs), "skipped": bad,
                       "capped": capped},
            "briefs": briefs, "watchdog": watchdog}
