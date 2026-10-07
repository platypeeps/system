"""The tracker collector, in the library, writing `shadow` and its watermark.

When this module was written, the pack's `dashboard` package held the only
collector that refreshed the tracker rows. A one-time import of
`index.sqlite` would therefore have left
`shadow` frozen on the day of the switch: correct at midnight, wrong by
lunch, and nothing in the system would have said so. So the collector moved
here first, and PR 6's retire of `index.sqlite` is gated on a sync *after* the
import having brought in a new issue.

**This module is the collector, not the command.** `sd shadow sync` is a verb
and verbs live in the pack -- that is the settled answer, and an earlier
draft of this item built the verb here, which was the one place this item
decided where a verb lives and decided it against itself. What lands here is
the function the verb calls and the `watermark` rows it resumes from.

**Windowed, with an hour of overlap, and the watermark moves only on
success.** Both rules come from the collector this replaces and both exist
because a windowed collector that gets them wrong loses issues silently.
GitHub accepts second-precision `updated:` bounds over a mutable search
index, so an issue updated at the instant of a collect can be
missing from it and, without overlap, from every later one too. And a
partial collect that advanced its watermark would step the window past
exactly the issues it failed to read.

**Two trackers, one per call.** The 2026-09-06 decision that Jira would
retire with no successor was reversed by the 2026-09-12 decision on sd:361:
Jira tickets are shadowed the way GitHub issues are. The second module is
`shadow_jira`, which collects Jira tickets into the same `Collected` shape
over its own JQL window. `sync`'s `tracker` chooses the collector: `github`
runs the four buckets here with their contribution details and protection
sweep, `jira` runs `shadow_jira` with none of that, and any other name is a
`ValueError`. `sync` stays one tracker per call; the caller -- the pack's
`sd shadow sync` -- iterates `TRACKERS` and reports each tracker on its own.
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import refuse_hub_only, transaction
from .errors import SdDbError
from .writes import record_state, resolve_state, upsert_shadow

TRACKER = "github"
#: Every tracker `sync` can collect, in the order the caller reports them.
#: Enumerated rather than discovered: a module that exists is not a tracker
#: until it is named here and `sync` dispatches to it.
TRACKERS = (TRACKER, "jira")
GH_TIMEOUT_SECONDS = 60
PAGE_SIZE = 100

#: Ten pages per search range. Larger result sets need narrower ranges.
MAX_PAGES = 10
SEARCH_CAP = 1000
#: One collect's whole budget: the search walk, the contribution details that
#: follow it, and the branch-protection sweep all spend from it. 200/300 was
#: chosen when only the search spent it; once #301 ran details on the same
#: budget, a nightly run had ~90 requests left after the search (45-77 a
#: night) and the protection reserve (then 2 per registered repository), which at
#: 8 per pull cleared ~11 details against 40-90 new rows a day -- the queue
#: reached 213 on 2026-09-12 and could only grow. 1000 clears ~110 a night
#: and is a fifth of GitHub's hourly REST allowance; at the ~2.8 requests/s
#: the nightly measured, spending it needs ~360 s, hence the 600.
MAX_REQUESTS = 1000
MAX_SECONDS = 600

#: An hour, deliberately re-read. Cheaper than the alternative: the upsert
#: makes a repeat sighting free, and a missed issue is never seen again.
OVERLAP = timedelta(hours=1)

#: A first collect has no watermark, and an unbounded query would drag years
#: of closed work in to render a "needs you" list.
FIRST_RUN_WINDOW = timedelta(days=90)

#: `(why, search qualifier)`. `involves:@me` would collapse these into one
#: call and lose *why* an issue reached you, which is the whole value:
#: "three people requested your review" and "you opened three issues" are the
#: same count and completely different mornings.
BUCKETS = (
    ("assigned", "assignee:@me"),
    ("mentioned", "mentions:@me"),
    ("review-requested", "review-requested:@me"),
    ("author", "author:@me"),
)

QUERY = """
query($q: String!, $n: Int!, $after: String) {
  search(query: $q, type: ISSUE, first: $n, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      __typename
      ... on Issue {
        number title url state updatedAt
        author { login }
        repository { nameWithOwner }
      }
      ... on PullRequest {
        number title url state updatedAt
        author { login }
        repository { nameWithOwner }
      }
    }
  }
}
"""

NO_GH = "gh is not installed; install it (`brew install gh`) to collect issues"
NO_AUTH = "gh is installed but not authenticated; run `gh auth login`"

#: The `state` kind the cursor lives in. Named in the schema's own CHECK, so
#: a typo here is a refusal from `record_state` and never a silent row.
WATERMARK = "watermark"


def iso(moment: datetime) -> str:
    """UTC, second resolution, the spelling GitHub search accepts."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(text or "")
        return value.astimezone(timezone.utc) if value.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def window_start(watermark: str | None, now: datetime) -> datetime:
    """Where this collect starts reading.

    A watermark that cannot be parsed is treated as no watermark at all: a
    corrupt value must widen the window, never narrow it. Narrowing on
    garbage is how a windowed collector silently stops collecting.
    """
    previous = parse_iso(watermark)
    if previous is None:
        return now - FIRST_RUN_WINDOW
    return previous - OVERLAP


# --------------------------------------------------------------- the cursor


def read_watermark(connection: sqlite3.Connection, tracker: str = TRACKER) -> str | None:
    """The newest cursor written for this tracker, or None on a first run.

    Only a **resolved** row counts. `resolved_at` on a watermark means the
    cursor was written completely; a row without it is a write that did not
    finish, and resuming from half a cursor is the one way this table could
    make the collector skip a window.
    """
    row = connection.execute(
        "SELECT body FROM state WHERE kind = ? AND key = ? AND resolved_at IS NOT NULL "
        "ORDER BY timestamp DESC, id DESC LIMIT 1",
        (WATERMARK, tracker),
    ).fetchone()
    if row is None:
        return None
    try:
        return (json.loads(row["body"]) or {}).get("collected_at")
    except (TypeError, ValueError):
        return None


def write_watermark(
    connection: sqlite3.Connection,
    tracker: str,
    collected_at: str,
    start: str,
) -> int:
    """Record a *successful* collect, and mark it complete.

    Called only after a collect that returned without error. A failed or
    partial collect deliberately leaves the cursor where it was, so the next
    run re-queries the window that was missed instead of stepping over it.
    """
    row = record_state(
        connection,
        WATERMARK,
        key=tracker,
        body={"collected_at": collected_at, "window_start": start},
    )
    resolve_state(connection, row)
    return row


# ----------------------------------------------------------------- the fetch


def _run(argv: list[str], runner=None, *, timeout=GH_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Fixed-argv subprocess, or the injected runner the tests supply."""
    if runner is not None:
        return runner(argv)
    try:
        done = subprocess.run(  # nosec B603 - fixed argv, shell=False
            argv, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out after {timeout:g}s"
    except OSError as error:
        return 127, "", f"cannot run {argv[0]}: {error}"
    return done.returncode, done.stdout, done.stderr


def available(runner=None, *, timeout=GH_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Whether this run can read GitHub, and if not, why not.

    Reports the *name* of what is missing and never the credential itself;
    `gh auth status` is consulted for its exit code alone.
    """
    if runner is None and shutil.which("gh") is None:
        return False, NO_GH
    code, _, _ = _run(["gh", "auth", "status"], runner, timeout=timeout)
    if code != 0:
        return False, NO_AUTH
    return True, ""


def _page(query: str, cursor: str | None, runner=None, *, timeout=GH_TIMEOUT_SECONDS) -> tuple[dict, str]:
    """One page of one search. Returns `(search block, error sentence)`."""
    argv = ["gh", "api", "graphql", "-f", f"query={QUERY}", "-f", f"q={query}",
            "-F", f"n={PAGE_SIZE}"]
    # Omitted rather than passed empty on the first page: `-F after=` sends an
    # empty string, and GraphQL reads that as a cursor rather than as absent.
    if cursor:
        argv += ["-f", f"after={cursor}"]
    code, out, err = _run(argv, runner, timeout=timeout)
    if code != 0:
        detail = (err or out).strip().splitlines()
        return {}, detail[0] if detail else f"gh api graphql exited {code}"
    try:
        payload = json.loads(out or "null") or {}
    except json.JSONDecodeError as error:
        return {}, f"gh api graphql did not return JSON: {error}"
    if not isinstance(payload, dict):
        return {}, "gh api graphql did not return an object"
    errors = payload.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else {}
        message = first.get("message") if isinstance(first, dict) else ""
        return {}, f"gh api graphql returned errors: {message or errors}"
    data = payload.get("data")
    block = data.get("search") if isinstance(data, dict) else None
    # An absent `search` is malformed; an empty one is a legitimate result
    # with no matches. Collapsing both would turn the first into a silent
    # empty page, which is a silent drop in a different coat.
    if not isinstance(block, dict):
        return {}, "gh api graphql returned no search block"
    return block, ""


@dataclass
class _SearchBudget:
    """One collect's bounded GraphQL walk, shared by every bucket and split."""

    remaining: int
    deadline: float
    requests: int = 0

    def page(self, query, cursor, runner):
        left = self.deadline - time.monotonic()
        if self.remaining <= 0:
            return {}, "collection request limit exhausted"
        if left <= 0:
            return {}, "collection time limit exhausted"
        self.remaining -= 1
        self.requests += 1
        return _page(query, cursor, runner, timeout=min(GH_TIMEOUT_SECONDS, left))


def search(query: str, runner=None, *, budget=None, observed=None) -> tuple[list[dict], bool, str]:
    """One search, followed across pages. Returns `(nodes, truncated, error)`.

    `truncated` means rows exist that this walk did not return. Two things
    cut a walk short and only one is this module's: the page ceiling is, and
    GitHub's 1,000-result search cap is not. The cap arrives disguised as
    `hasNextPage: false`, which is why the finish is checked against
    `issueCount` rather than believed.
    """
    nodes: dict[str, dict] = {}
    cursor: str | None = None
    cursors: set[str] = set()
    count = None
    for _ in range(MAX_PAGES):
        block, error = (budget.page(query, cursor, runner) if budget else
                        _page(query, cursor, runner))
        if error:
            return list(nodes.values()), False, error
        total = block.get("issueCount")
        page = block.get("nodes")
        info = block.get("pageInfo")
        if (type(total) is not int or total < 0 or not isinstance(page, list)
                or not isinstance(info, dict) or type(info.get("hasNextPage")) is not bool):
            return list(nodes.values()), False, "malformed search pagination"
        if count is not None and total != count:
            return list(nodes.values()), False, "search count changed during pagination"
        count = total
        if observed is not None:
            observed["count"] = count
        for node in page:
            if not isinstance(node, dict) or not isinstance(node.get("url"), str) or not node["url"].strip():
                return list(nodes.values()), False, "search page has an unidentifiable result"
            nodes[node["url"]] = node
        if len(nodes) > count:
            return list(nodes.values()), False, "search returned more identities than its count"
        # Do not fetch ten pages that cannot cover the advertised result set.
        if count > SEARCH_CAP:
            return list(nodes.values()), True, ""
        if not info["hasNextPage"]:
            return list(nodes.values()), len(nodes) != count, ""
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            return list(nodes.values()), False, "missing or repeated pagination cursor"
        cursors.add(cursor)
    return list(nodes.values()), True, ""


def _search_window(qualifier, start, end, runner, budget):
    """Split capped inclusive ranges, sharing endpoints to avoid precision gaps."""
    pending = [(start, end)]
    found: dict[str, dict] = {}
    coverage = []
    errors = []
    splits = []
    leaves = []
    while pending:
        lower, upper = pending.pop()
        query = f"{qualifier} updated:{iso(lower)}..{iso(upper)}"
        observed = {}
        nodes, cut, error = search(query, runner, budget=budget, observed=observed)
        for node in nodes:
            updated = parse_iso(node.get("updatedAt"))
            if updated is None or not lower <= updated <= upper:
                error = "search result timestamp is invalid or outside the requested interval"
                cut = False
                continue
            previous = found.get(node["url"])
            if previous is None or updated >= parse_iso(previous["updatedAt"]):
                found[node["url"]] = node
        if cut and upper - lower > timedelta(seconds=1):
            splits.append((lower, upper, observed["count"]))
            midpoint = lower + timedelta(seconds=int((upper - lower).total_seconds()) // 2)
            pending.extend([(midpoint, upper), (lower, midpoint)])
            continue
        if cut:
            error = f"search remains incomplete within second interval {iso(lower)}..{iso(upper)}"
        complete = not cut and not error
        coverage.append({"start": iso(lower), "end": iso(upper),
                         "count": len(nodes), "complete": complete})
        leaves.append((lower, upper, {node["url"] for node in nodes}))
        if error:
            errors.append(error)
    if not errors:
        for lower, upper, expected in splits:
            identities = set().union(*(urls for first, last, urls in leaves
                                      if lower <= first and last <= upper))
            if len(identities) != expected:
                errors.append("search count changed while splitting ranges")
                for window in coverage:
                    if lower <= parse_iso(window["start"]) and parse_iso(window["end"]) <= upper:
                        window["complete"] = False
    return list(found.values()), coverage, "; ".join(dict.fromkeys(errors))


def _utc_second(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    try:
        return value.astimezone(timezone.utc).replace(microsecond=0)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} must fit in the UTC datetime range") from error


def _validate_limits(max_requests, max_seconds):
    if type(max_requests) is not int or max_requests <= 0:
        raise ValueError("max_requests must be a positive integer")
    if (type(max_seconds) not in (int, float) or not math.isfinite(max_seconds)
            or max_seconds <= 0):
        raise ValueError("max_seconds must be a positive finite number")


def normalize(node: dict, why: str) -> dict | None:
    """One search result as a `shadow` row, or None when it is unusable.

    A node with no URL cannot be keyed, unioned or opened, so it is dropped
    rather than stored under an empty string where it would collide with the
    next one.
    """
    url = (node.get("url") or "").strip()
    if not url:
        return None
    return {
        "tracker": TRACKER,
        "url": url,
        "repo": (node.get("repository") or {}).get("nameWithOwner") or "",
        "number": node.get("number"),
        "kind": "pull" if node.get("__typename") == "PullRequest" else "issue",
        "title": node.get("title") or "",
        "state": (node.get("state") or "").lower(),
        "author": (node.get("author") or {}).get("login") or "",
        "updated_at": node.get("updatedAt") or "",
        "why": [why],
    }


@dataclass
class Collected:
    """One collect, whether or not it succeeded. `ok` gates the watermark."""

    ok: bool
    reason: str
    issues: list[dict] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    window_start: str = ""
    window_end: str = ""
    requests: int = 0
    coverage: list[dict] = field(default_factory=list)


def collect(watermark: str | None, now: datetime, runner=None, *, since=None,
            max_requests=MAX_REQUESTS, max_seconds=MAX_SECONDS, budget=None) -> Collected:
    """Every bucket, unioned by URL.

    A bucket that errors makes the whole collect unsuccessful, and the caller
    must not advance the watermark. Returning the rows that did arrive is
    still right -- they are real. What must not happen is treating a partial
    collect as a complete one and stepping the window past the part that
    failed.
    """
    _validate_limits(max_requests, max_seconds)
    end = _utc_second(now, "now")
    start = _utc_second(since if since is not None else window_start(watermark, end), "since")
    if start > end:
        raise ValueError("since must not be later than now")
    budget = budget or _SearchBudget(max_requests, time.monotonic() + max_seconds)
    left = budget.deadline - time.monotonic()
    if left <= 0:
        return Collected(ok=False, reason="collection time limit exhausted", window_start=iso(start), window_end=iso(end))
    ok, reason = available(runner, timeout=min(GH_TIMEOUT_SECONDS, left))
    if not ok:
        return Collected(ok=False, reason=reason, window_start=iso(start), window_end=iso(end))
    merged: dict[str, dict] = {}
    truncated: list[str] = []
    errors: list[str] = []
    coverage = []
    for why, qualifier in BUCKETS:
        nodes, windows, error = _search_window(qualifier, start, end, runner, budget)
        coverage.extend({"bucket": why, **window} for window in windows)
        if error:
            errors.append(f"{why}: {error}")
        if any(not window["complete"] for window in windows):
            truncated.append(why)
        for node in nodes:
            row = normalize(node, why)
            if row is None:
                continue
            found = merged.get(row["url"])
            if found is None:
                merged[row["url"]] = row
            elif why not in found["why"]:
                found["why"].append(why)
    rows = sorted(merged.values(), key=lambda row: row["updated_at"], reverse=True)
    for row in rows:
        row["why"].sort()
    return Collected(
        ok=not errors and not truncated,
        reason="; ".join(errors),
        issues=rows,
        truncated=truncated,
        window_start=iso(start),
        window_end=iso(end),
        requests=budget.requests,
        coverage=coverage,
    )


def store(connection: sqlite3.Connection, issues: list[dict]) -> int:
    """Write collected issues as `shadow` rows, keyed by `(tracker, url)`.

    `normalize` returns ten keys and this writes eight. The two it does not
    write -- `why` and `updated_at` -- are not an oversight; the `shadow`
    table has no column for either, and the comment above that table says
    why and where `why` goes instead. It goes there *before* this runs:
    `sync` passes the same `result.issues` to `contribution_sync.refresh`
    first. Returns the count.
    """
    for issue in issues:
        upsert_shadow(
            connection,
            tracker=issue.get("tracker") or TRACKER,
            url=issue["url"],
            repo=issue.get("repo") or None,
            number=issue.get("number"),
            kind=issue.get("kind") or None,
            title=issue.get("title") or None,
            state=issue.get("state") or None,
            author=issue.get("author") or None,
        )
    return len(issues)


def _observe_protection(connection: sqlite3.Connection, client, observed_at: str) -> dict:
    """Run the protection collector and record its own heartbeat.

    Guarded twice over, because this is a side observation of the tracker's
    sync and not the sync: a failure in the collector becomes `unknown` rows
    (inside `protection.sync`) or, if the collector itself raises, an errors
    list in the heartbeat -- and never an exception out of `sync()`, a failed
    `result.ok`, or a held watermark.
    """
    from . import protection

    try:
        summary = protection.sync(connection, client=client, observed_at=observed_at)
    except Exception as error:  # noqa: BLE001 - a side observation never fails the tracker
        summary = {"attempted": 0, "protected": 0, "unprotected": 0, "unknown": 0,
                   "requests": 0, "errors": [f"{type(error).__name__}: {error}"]}
    try:
        record_state(connection, "heartbeat", key=protection.HEARTBEAT_KEY,
                     timestamp=datetime.fromtimestamp(time.time(), timezone.utc).isoformat(timespec="seconds"),
                     body={"ok": not summary["errors"], "observed_at": observed_at, **summary})
    except Exception:  # noqa: BLE001 - same rule: the heartbeat is reporting, not the work
        pass
    return summary


@dataclass
class Synced:
    """What one sync did, in the shape the pack's verb prints."""

    ok: bool
    reason: str
    written: int
    truncated: list[str]
    window_start: str
    watermark_moved: bool
    window_end: str = ""
    requests: int = 0
    coverage: list[dict] = field(default_factory=list)
    #: Contribution details the budget did not reach, and the ones it reached
    #: but could not complete (`key: reason`). Neither is a failure of the
    #: collect: the first is a backlog for the next run, the second is held
    #: on that contribution's own heartbeat.
    queued: int = 0
    incomplete: list[str] = field(default_factory=list)
    #: False only when the tracker's configuration is absent, so the caller
    #: can print "not collected" rather than a failure without parsing
    #: `reason`. Last, so positional construction is unaffected.
    configured: bool = True

    def report(self) -> list[str]:
        lines = [
            f"shadow sync: {self.written} row(s) from {self.window_start}, "
            f"watermark {'moved' if self.watermark_moved else 'held'}"
        ]
        if self.truncated:
            # Reported rather than swallowed: a collect that hit its page
            # ceiling collected less than it saw, and a count that omits the
            # remainder reads as "that is all there is".
            lines.append(f"shadow sync: truncated buckets: {', '.join(self.truncated)}")
        if not self.ok:
            lines.append(f"shadow sync: {self.reason}")
        if self.incomplete:
            lines.append(f"shadow sync: contribution observation incomplete: {'; '.join(self.incomplete)}")
        if self.queued:
            lines.append(f"shadow sync: contribution detail backlog: {self.queued} queued")
        return lines


class SyncBusy(SdDbError):
    """Another `sync` holds this database's shadow-sync lock."""


@contextmanager
def sync_lock(connection: sqlite3.Connection):
    """Hold this database's shadow-sync lock, or raise `SyncBusy` at once.

    Every `sync` runs inside it, so the nightly job, a terminal and the
    dashboard never collect at once (sd:2207 review): `store` writes rows
    before the cursor recheck, and an older run finishing second would leave
    its older observations over a newer run's. The lock is an flock on a file
    beside the database, so it is the hub's alone; a satellite refuses with
    `HubOnly`, as the control gate does. An in-memory database has no file
    another process could reach, and takes no lock.
    """
    from .runner import RunnerRefused
    from .runner_journal import lock

    refuse_hub_only(connection, "shadow sync")
    filename = next((row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not filename:
        yield
        return
    path = Path(filename).resolve().parent / "operation-locks" / "shadow-sync.lock"
    entered = False
    try:
        with lock(path, blocking=False, noun="shadow sync"):
            entered = True
            yield
    except RunnerRefused as error:
        if entered:
            raise
        if not isinstance(error.__cause__, BlockingIOError):
            raise SdDbError(str(error)) from error
        raise SyncBusy("another shadow sync is already running against this database; wait for it to finish") from error


def sync(connection: sqlite3.Connection, **options) -> Synced:
    """`_sync` under `sync_lock`: one collect at a time per database, whoever calls it."""
    with sync_lock(connection):
        return _sync(connection, **options)


def _sync(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    runner=None,
    tracker: str = TRACKER,
    since: datetime | None = None,
    max_requests: int = MAX_REQUESTS,
    max_seconds: float = MAX_SECONDS,
    contribution_client=None,
    notifier=None,
) -> Synced:
    """Collect, write the rows, and advance the cursor only on success.

    This is what `sd shadow sync` calls. The order is the whole design: rows
    are written whether or not the collect succeeded, because a partial
    answer is still true; the cursor moves only on the collect's own success.

    Success is the search's: complete coverage of every bucket, and the
    identities it found staged durably. The contribution details that follow
    on the same budget are one observation per contribution, and one that
    cannot be completed -- a dependency with no configured release tag, a
    pull whose API read fails -- is held on that contribution's heartbeat and
    named in `incomplete`, not turned into a failed collect. Until 2026-09-12
    it was, and one item with a missing tag made every night's strict run
    unsuccessful and the tracker's health `degraded`, while the cursor had
    in fact moved. Only the detail collector's own failures (no authenticated
    operator, a queue checkpoint that moved) still fail the run.

    `tracker` chooses the collector, from `TRACKERS`. `jira` is collect,
    store, watermark on success, heartbeat, and nothing else: `runner` is its
    HTTP transport seam rather than a `gh` runner, `max_requests` and
    `max_seconds` are validated and otherwise unused because Jira's ceiling
    is `shadow_jira.MAX_PAGES`, and `since` is refused because the recovery
    window is GitHub's -- a relative-minutes JQL window cannot express it.
    """
    if tracker not in TRACKERS:
        raise ValueError(f"tracker must be one of {TRACKERS}, not {tracker!r}")
    moment = _utc_second(datetime.now(timezone.utc) if now is None else now, "now")
    from . import contribution_sync, protection
    from .contribution_github import Client

    _validate_limits(max_requests, max_seconds)
    if tracker == "jira":
        return _sync_jira(connection, moment, runner, since)
    planned = contribution_sync.plan(connection)
    budget = _SearchBudget(max_requests, time.monotonic() + max_seconds)
    # Existing work must still refresh when a large search consumes its slice.
    reserve = min(80, max_requests // 2) if planned["pending"] else 0
    search_budget = _SearchBudget(max_requests - reserve,
                                 budget.deadline - (min(120, max_seconds / 2) if reserve else 0))
    previous = read_watermark(connection, tracker)
    result = collect(previous, moment, runner, since=since,
                     max_requests=max_requests, max_seconds=max_seconds, budget=search_budget)
    budget.remaining -= search_budget.requests
    budget.requests = search_budget.requests
    search_complete = result.ok
    client = contribution_client or Client(budget, runner=runner)
    client.budget = budget
    # Details run until the budget runs out, stopping short of what the
    # protection collector below needs to reach every registered repository:
    # its requests and its time. The time is the limit that ran out first
    # (sd:1663): with the requests reserved alone, a detail backlog spent
    # the deadline and the sweep filed every row on zero requests.
    detail = contribution_sync.refresh(connection, planned, result.issues,
                                       client=client, observed_at=iso(moment),
                                       search_complete=search_complete, reserve=protection.reserve(connection),
                                       reserve_seconds=protection.reserve_seconds(connection, max_seconds))
    if detail["errors"]:
        result.ok = False
        result.reason = "; ".join(filter(None, [result.reason, *detail["errors"]]))
    # The fleet's branch protection rides on the same budget, after the work
    # the tracker exists for. It is a side observation: it cannot fail this
    # sync, hold the watermark, or touch `result` beyond the requests it spent.
    _observe_protection(connection, client, iso(moment))
    result.requests = budget.requests
    # Collection holds no write lock. Recheck the cursor under the transaction
    # because another collector can complete while this one waits for GitHub.
    with transaction(connection):
        written = store(connection, result.issues)
        previous_time = parse_iso(read_watermark(connection, tracker))
        # A complete explicit recovery can still leave an uncovered prefix.
        moved = search_complete and detail["staged"] and (previous_time is None or (
            moment > previous_time and parse_iso(result.window_start) <= previous_time
        ))
        if moved:
            write_watermark(connection, tracker, iso(moment), result.window_start)
        record_state(
            connection, "heartbeat", key=f"tracker-sync:{tracker}",
            # Historical recovery bounds must not hide a later failed attempt.
            timestamp=datetime.fromtimestamp(time.time(), timezone.utc).isoformat(timespec="seconds"),
            body={"ok": result.ok, "reason": result.reason, "truncated": result.truncated,
                  "window_start": result.window_start, "window_end": result.window_end,
                  "requests": result.requests, "coverage": result.coverage,
                  "queued": detail["queued"], "incomplete": detail["incomplete"]},
        )
    contribution_sync.dispatch_pending(connection, notifier=notifier, keys=detail["completed_keys"],
                                       deadline=budget.deadline)
    return Synced(
        ok=result.ok,
        reason=result.reason,
        written=written,
        truncated=result.truncated,
        window_start=result.window_start,
        watermark_moved=moved,
        window_end=result.window_end,
        requests=result.requests,
        coverage=result.coverage,
        queued=detail["queued"],
        incomplete=detail["incomplete"],
    )


def _sync_jira(connection: sqlite3.Connection, moment: datetime, transport, since) -> Synced:
    """Jira's sync: the GitHub shape with every GitHub-only stage left out.

    An unconfigured Jira is not a failure of the run. It writes the heartbeat
    with the missing variables' names, so a reader on the same machine can say
    why there is no row, and returns `configured=False` before any request.
    """
    from . import shadow_jira

    if since is not None:
        raise ValueError("since is GitHub's recovery window; the jira tracker takes none")
    configured = not shadow_jira.missing(shadow_jira.settings())
    previous = read_watermark(connection, shadow_jira.TRACKER)
    # `collect` checks the configuration first: unconfigured, it answers with
    # no request, no issues and `ok` False, so nothing below stores or moves.
    result = shadow_jira.collect(previous, moment, transport)
    # Collection holds no write lock; recheck the cursor under the transaction.
    with transaction(connection):
        written = store(connection, result.issues)
        previous_time = parse_iso(read_watermark(connection, shadow_jira.TRACKER))
        moved = result.ok and (previous_time is None or moment > previous_time)
        if moved:
            write_watermark(connection, shadow_jira.TRACKER, iso(moment), result.window_start)
        record_state(
            connection, "heartbeat", key=f"tracker-sync:{shadow_jira.TRACKER}",
            timestamp=datetime.fromtimestamp(time.time(), timezone.utc).isoformat(timespec="seconds"),
            body={"ok": result.ok, "reason": result.reason, "truncated": result.truncated,
                  "window_start": result.window_start, "window_end": result.window_end,
                  "requests": result.requests, "coverage": result.coverage,
                  "queued": 0, "incomplete": []},
        )
    return Synced(
        ok=result.ok,
        reason=result.reason,
        written=written,
        truncated=result.truncated,
        window_start=result.window_start,
        watermark_moved=moved,
        window_end=result.window_end,
        requests=result.requests,
        coverage=result.coverage,
        configured=configured,
    )
