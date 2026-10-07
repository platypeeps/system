"""A satellite's claim on an item, and the hub's alarm when the claim goes quiet (sd:2918).

`docs/work/2026-10-07-satellite-staleness-alarm/design.md` is the record.

A claim is a `state` row of kind `heartbeat`, keyed `satellite-claim:<item>`,
whose body names the host, the branch, when it started and an optional
`quiet_until`. The kind exists and its unique index covers only the key
`runner`, so a claim needs no migration. A resolved row is a released claim.

Progress is the newest of three instants: any note on the item, the item's
`updated_at`, and the committer date of the claimed branch's head on
`origin`. A claim is stale when that instant is older than the threshold.

One stale episode sends one alert. After a send, a `watermark` row keyed
`satellite-stale:<item>` holds the progress instant it alerted on; the next
alert waits until progress moves past it and stalls again.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import paths
from .database import transaction
from .errors import SdDbError
from .writes import now, record_state

CLAIM_PREFIX = "satellite-claim:"
EPISODE_PREFIX = "satellite-stale:"
DEFAULT_HOURS = 3.0
DEFAULT_WINDOW = (7, 22)
FETCH_BOUND = 60
#: Statuses that wait on someone other than the satellite, or are over.
NOT_CHECKED = ("done", "blocked", "ready_to_send")


class ClaimRefused(SdDbError):
    """A claim, a threshold or a window this module will not take."""


class SendFailed(SdDbError):
    """An alert the notifier did not deliver; its episode stays open."""


@dataclass(frozen=True)
class Claim:
    row: int
    item: int
    host: str
    branch: str | None
    since: str
    quiet_until: str | None


@dataclass(frozen=True)
class Verdict:
    claim: Claim
    title: str
    status: str | None
    repo: str | None
    branch: str | None
    #: `fresh`, `stale`, `quiet` or `skipped`.
    state: str
    progress: str | None
    #: Which signal gave `progress`: `note`, `updated`, `branch` or `claim`.
    source: str
    age_hours: float | None
    #: True when an alert already went out for this `progress`.
    alerted: bool


@dataclass(frozen=True)
class Alert:
    item: int
    title: str
    body: str


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.astimezone()


def _claim(row: sqlite3.Row) -> Claim | None:
    """The claim a row holds, or `None` for a row `claim` did not write,
    so one malformed row cannot stop the check for every other item."""
    try:
        body = json.loads(row["body"] or "{}")
        item = int(row["key"][len(CLAIM_PREFIX):])
        if not isinstance(body, dict):
            return None
        held = Claim(row=int(row["id"]), item=item, host=str(body.get("host") or "?"),
                     branch=body.get("branch") or None, since=body.get("since") or row["timestamp"],
                     quiet_until=body.get("quiet_until"))
        for value in (held.since, held.quiet_until):
            if value is not None:
                _instant(value)
        if held.branch is not None and not isinstance(held.branch, str):
            return None
    except (TypeError, ValueError):
        return None
    return held


def _open_rows(connection: sqlite3.Connection, kind: str, key: str) -> list[int]:
    return [int(row[0]) for row in connection.execute(
        "SELECT id FROM state WHERE kind = ? AND key = ? AND resolved_at IS NULL", (kind, key))]


def _resolve(connection: sqlite3.Connection, rows: list[int], when: str) -> None:
    for row in rows:
        connection.execute("UPDATE state SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL", (when, row))


def claim(connection: sqlite3.Connection, item: int, *, host: str, branch: str | None = None,
          quiet_until: str | None = None, at: str | None = None) -> int:
    """Open a claim on `item`, replacing any open one. Returns the row id."""
    if connection.execute("SELECT 1 FROM item WHERE id = ?", (item,)).fetchone() is None:
        raise ClaimRefused(f"no item sd:{item}")
    if quiet_until is not None:
        _instant(quiet_until)
    when = at or now()
    key = f"{CLAIM_PREFIX}{item}"
    # One transaction: a failed insert keeps the old claim, and BEGIN
    # IMMEDIATE orders two replacements so only one claim stays open.
    # A replacement that names no branch keeps the stored one, so
    # `claim ITEM --quiet-until T` does not drop the branch signal.
    with transaction(connection):
        held = _open_rows(connection, "heartbeat", key)
        if branch is None and held:
            stored = _claim(connection.execute("SELECT id, key, timestamp, body FROM state WHERE id = ?",
                                               (held[-1],)).fetchone())
            branch = stored.branch if stored else None
        _resolve(connection, held, when)
        body = {"host": host, "branch": branch, "since": when, "quiet_until": quiet_until}
        return record_state(connection, "heartbeat", key=key, body=body, timestamp=when)


def unclaim(connection: sqlite3.Connection, item: int, *, at: str | None = None) -> bool:
    """Release `item`'s claim and close its episode. False when none was open."""
    when = at or now()
    with transaction(connection):
        claims = _open_rows(connection, "heartbeat", f"{CLAIM_PREFIX}{item}")
        _resolve(connection, claims, when)
        _resolve(connection, _open_rows(connection, "watermark", f"{EPISODE_PREFIX}{item}"), when)
    return bool(claims)


def open_claims(connection: sqlite3.Connection) -> list[Claim]:
    rows = connection.execute(
        "SELECT id, key, timestamp, body FROM state WHERE kind = 'heartbeat' AND key LIKE ? "
        "AND resolved_at IS NULL ORDER BY timestamp, id", (f"{CLAIM_PREFIX}%",))
    return [held for held in map(_claim, rows) if held is not None]


def _alerted_on(connection: sqlite3.Connection, item: int) -> str | None:
    row = connection.execute(
        "SELECT body FROM state WHERE kind = 'watermark' AND key = ? AND resolved_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (f"{EPISODE_PREFIX}{item}",)).fetchone()
    return None if row is None else row[0]


def _newest(signals: list[tuple[str, str | None]]) -> tuple[str | None, str]:
    found = [(_instant(value), value, source) for source, value in signals if value]
    if not found:
        return None, "claim"
    _, value, source = max(found, key=lambda entry: entry[0])
    return value, source


def assess(connection: sqlite3.Connection, *, at: datetime, hours: float,
           branch_time: Callable[[str | None, str], str | None]) -> list[Verdict]:
    """One verdict per open claim. `branch_time(repo, branch)` reads `origin`."""
    verdicts = []
    for held in open_claims(connection):
        item = connection.execute(
            "SELECT title, status, repo, branch, updated_at FROM item WHERE id = ?", (held.item,)).fetchone()
        if item is None or item["status"] in NOT_CHECKED:
            verdicts.append(Verdict(held, item["title"] if item else "(no item)", item["status"] if item else None,
                                    None, held.branch, "skipped", None, "claim", None, False))
            continue
        branch = held.branch or item["branch"]
        noted = connection.execute("SELECT MAX(timestamp) FROM note WHERE item = ?", (held.item,)).fetchone()[0]
        signals = [("note", noted), ("updated", item["updated_at"]), ("claim", held.since)]
        if branch:
            signals.append(("branch", branch_time(item["repo"], branch)))
        progress, source = _newest(signals)
        age = (at - _instant(progress)).total_seconds() / 3600 if progress else None
        if held.quiet_until and at < _instant(held.quiet_until):
            state = "quiet"
        elif age is not None and age >= hours:
            state = "stale"
        else:
            state = "fresh"
        verdicts.append(Verdict(held, item["title"], item["status"], item["repo"], branch, state, progress,
                                source, age, _alerted_on(connection, held.item) == progress))
    return verdicts


def describe(verdict: Verdict) -> Alert:
    item = verdict.claim.item
    age = f"{verdict.age_hours:.1f} h" if verdict.age_hours is not None else "unknown"
    body = "\n".join([
        f"sd:{item} {verdict.title}",
        f"host {verdict.claim.host}, branch {verdict.branch or '(none)'}, status {verdict.status}",
        f"newest progress: {verdict.progress} ({verdict.source}), {age} ago",
        f"silence until a time: sd-db.sh claim {item} --quiet-until <ISO time> (on the satellite)",
        f"release the claim: sd-db.sh unclaim {item}",
    ])
    return Alert(item, f"Satellite stalled: sd:{item}", body)


def alert(connection: sqlite3.Connection, verdicts: list[Verdict], *,
          send: Callable[[Alert], None], at: str | None = None) -> list[Verdict]:
    """Send one alert per new stale episode. A `send` that raises stops the run
    before that episode's watermark, so the next run retries it."""
    sent = []
    for verdict in verdicts:
        if verdict.state != "stale" or verdict.alerted:
            continue
        send(describe(verdict))
        key = f"{EPISODE_PREFIX}{verdict.claim.item}"
        when = at or now()
        with transaction(connection):
            _resolve(connection, _open_rows(connection, "watermark", key), when)
            record_state(connection, "watermark", key=key, body=verdict.progress, timestamp=when)
        sent.append(verdict)
    return sent


def parse_hours(text: str | None) -> float:
    if text in (None, ""):
        return DEFAULT_HOURS
    try:
        hours = float(text)
    except ValueError:
        hours = -1.0
    if not (math.isfinite(hours) and hours > 0):
        raise ClaimRefused(f"SD_SATELLITE_STALE_HOURS={text!r} is not a positive number of hours")
    return hours


def parse_window(text: str | None) -> tuple[int, int]:
    """`START-END` in local hours, start inclusive, end exclusive."""
    if text in (None, ""):
        return DEFAULT_WINDOW
    try:
        start, end = (int(part) for part in text.split("-"))
    except ValueError:
        start, end = -1, -1
    if not 0 <= start < end <= 24:
        raise ClaimRefused(f"SD_SATELLITE_STALE_WINDOW={text!r} is not START-END local hours, 0 <= START < END <= 24")
    return start, end


def in_window(moment: datetime, window: tuple[int, int]) -> bool:
    local = moment.astimezone() if moment.tzinfo else moment
    return window[0] <= local.hour < window[1]


def fetch_branch_time(checkout: Path, branch: str, *, bound: int = FETCH_BOUND, fetch: bool = True) -> str | None:
    """The committer date of `origin/<branch>`, after a bounded fetch, or `None`.

    A failed or expired fetch drops the signal; it never fails the run.
    `fetch=False` reads the ref the last fetch left, for `status`, which the
    health check bounds more tightly than one fetch may take.
    """
    if not Path(checkout).is_dir():
        return None
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    ref = f"refs/remotes/origin/{branch}"
    try:
        if fetch:
            fetched = subprocess.run(
                ["git", "-C", str(checkout), "fetch", "-q", "origin", f"+refs/heads/{branch}:{ref}"],
                capture_output=True, text=True, timeout=bound, env=environment)
            if fetched.returncode != 0:
                return None
        shown = subprocess.run(["git", "-C", str(checkout), "log", "-1", "--format=%cI", ref],
                               capture_output=True, text=True, timeout=bound, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        return None
    stamp = shown.stdout.strip()
    return stamp if shown.returncode == 0 and stamp else None


def branch_time_on_disk(repo: str | None, branch: str) -> str | None:
    """`fetch_branch_time` for a stored repository key."""
    if not repo:
        return None
    return fetch_branch_time(paths.disk(repo), branch)


def branch_time_last_fetched(repo: str | None, branch: str) -> str | None:
    """The same, reading what the last fetch left, with no network."""
    if not repo:
        return None
    return fetch_branch_time(paths.disk(repo), branch, fetch=False)

