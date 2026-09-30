"""The read queries the two faces share.

Requirement 5 gives the dashboard five screens and requirement 6 gives the
terminal `sd today`, `sd usage` and `sd exec-log`. Criterion 4 is the reason
this module exists rather than a query inside each face: "`sd today` and the
Today screen render from the same library query; a test asserts they list the
same item ids in the same order". A query written twice is two orders, and the
second one is discovered by the operator and not by a test.

Nothing here writes. Every function takes an open connection -- the caller
decides whether it was opened `write=False` -- and returns `sqlite3.Row`s or
plain dataclasses of them, never HTML and never a formatted string: the
formatting is the face's, the rows and their *order* are the library's.

The clock is a parameter. `now` defaults to the wall clock, and every test
passes its own so that a Today built at 23:59:59 is the same Today the
assertion was written against.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import paths
from .errors import SdDbError

__all__ = [
    "AgeBucket",
    "Number",
    "ScorecardRow",
    "TRAILER",
    "age_bounds",
    "age_bucket",
    "age_histogram",
    "age_series",
    "backlog_items",
    "brief_items",
    "brief_notes",
    "capture_items",
    "cost_by_bill",
    "day_timeline",
    "item_assignments",
    "item_by_id",
    "item_notes",
    "item_shadow",
    "missing_trailers",
    "OverBudget",
    "on_branch",
    "open_followups",
    "runner_board",
    "scorecard",
    "status_changes",
    "today_items",
    "weekly_numbers",
]

#: The note kinds a brief carries. Requirement 7: "decisions, proposals,
#: comments and executions never inject; they are read on the item screen or
#: by `sd note list <item>`". The schema knows seven kinds; two of them inject.
BRIEF_KINDS = ("followup", "question")

class _NoRepo:
    """The selector for "belongs to no repository", as a value and not a word.

    An empty `repo` already means "every repository", so the unscoped rows -- a
    personal to-do, a followup, an idea that is not an article -- had no way to
    be asked for on their own. A magic string would have been the obvious
    sentinel and is the wrong one: `upsert_repo` validates nothing and
    `repo.path` carries no CHECK, so any string this module reserved could also
    be registered as a repository, and the filter would then answer two
    different questions with one value. An object compares by identity, so
    nothing a caller can store is ever equal to it.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "NO_REPO"


#: Asking `backlog_items` for the items that belong to no repository.
NO_REPO = _NoRepo()

#: The spelling a URL, a form or a CLI flag uses for `NO_REPO`, since a query
#: string carries text and not objects. A caller translates once, at the edge;
#: this is the only place the word is written down.
NO_REPO_TOKEN = "none"

#: The lanes of the runner board, in the order requirement 5 names them.
BOARD_LANES = ("queued", "running", "blocked", "done")

#: The status words a board column exists for, in the order requirement 5
#: names them. The Backlog board renders these and no other.
BOARD_COLUMNS = ("planning", "ready", "in_progress", "ready_to_send", "blocked", "done")

#: Days in a status, bucketed. The last bucket is open-ended.
AGE_EDGES = (1, 3, 7, 14, 30, 45)

#: The trailer every commit in this fleet carries. Named once because
#: `missing_trailers` counts the commits without it.
TRAILER = "Authored-with:"


def _now(now: str | None) -> str:
    return now if now is not None else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(stamp: str) -> str:
    return stamp[:10]


def _parse(stamp: str | None) -> datetime | None:
    """A stored timestamp, or None. Stored stamps are ISO 8601 in UTC."""
    if not stamp:
        return None
    text = stamp.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _days_between(earlier: str | None, later: str) -> int | None:
    start, end = _parse(earlier), _parse(later)
    if start is None or end is None:
        return None
    return max(0, (end - start).days)


# --------------------------------------------------------------------------
# Today
# --------------------------------------------------------------------------

#: Every Today query joins this: the timestamp the item entered its current
#: status, which is the newest `status_change` note or, for a row that has
#: never moved, the row's own creation. Requirement 5 wants "days-since-ready"
#: and requirement 5's age histogram wants "days in their current status";
#: both are this column against the clock.
_STATUS_SINCE = """
    COALESCE(
        (SELECT MAX(note.timestamp) FROM note
          WHERE note.item = item.id AND note.kind = 'status_change'),
        item.created_at
    )
"""


def _unparked_clause(connection: sqlite3.Connection) -> str:
    """A schema-2 store remains readable until its explicit migration."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(item)")}
    return "item.parked_at IS NULL" if "parked_at" in columns else "1"


def capture_items(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """All possible note parents, with active unparked items first.

    Stable IDs keep duplicate titles and repositories distinct. Finished and
    parked items remain selectable because adding context must not reopen them.
    """
    try:
        rows = connection.execute("""
            SELECT id, title, kind, status, repo, parked_at
              FROM item
             ORDER BY CASE WHEN status != 'done' AND parked_at IS NULL THEN 0 ELSE 1 END,
                      updated_at DESC, id DESC
        """)
    except sqlite3.OperationalError as error:
        if "no such column: parked_at" not in str(error):
            raise
        rows = connection.execute("""
            SELECT id, title, kind, status, repo, NULL AS parked_at
              FROM item
             ORDER BY CASE WHEN status != 'done' THEN 0 ELSE 1 END,
                      updated_at DESC, id DESC
        """)
    return list(rows.fetchall())


_TODAY_SQL = f"""
SELECT item.*,
       {_STATUS_SINCE} AS status_since,
       CASE WHEN item.status = 'ready_to_send' THEN 0 ELSE 1 END AS lane
  FROM item
 WHERE (item.status IN ('ready_to_send', 'in_progress')
    OR (item.due IS NOT NULL AND item.due <= :horizon
        AND item.status NOT IN ('done')))
   AND {{unparked}}
 ORDER BY lane ASC, status_since ASC, item.id ASC
"""


def today_items(
    connection: sqlite3.Connection, *, now: str | None = None, horizon_days: int = 0
) -> list[sqlite3.Row]:
    """Unparked items due or in progress, finished-unsent first.

    This is criterion 4's query. The Today screen renders it and `sd today`
    prints it; neither may sort it again. The order is three keys deep so it
    is total: the `ready_to_send` lane first, oldest in its status first
    within a lane, and the row id as the tie-break, because two rows that
    entered a status in the same second must still come out in one order.
    """
    stamp = _now(now)
    horizon = (_parse(stamp) + timedelta(days=horizon_days)).strftime("%Y-%m-%d")
    sql = _TODAY_SQL.format(unparked=_unparked_clause(connection))
    return list(connection.execute(sql, {"horizon": horizon}).fetchall())


def days_since_status(row: sqlite3.Row, *, now: str | None = None) -> int | None:
    """Days the row has been in its current status. `status_since` must be on it."""
    keys = row.keys() if hasattr(row, "keys") else ()
    if "status_since" not in keys:
        return None
    return _days_between(row["status_since"], _now(now))


def open_followups(connection: sqlite3.Connection, *, limit: int = 200) -> list[sqlite3.Row]:
    """Unresolved followups on unparked items, oldest first."""
    return list(
        connection.execute(
            f"""
            SELECT note.*, item.title AS item_title, item.repo AS item_repo
              FROM note JOIN item ON item.id = note.item
             WHERE note.kind = 'followup' AND note.resolved_at IS NULL
               AND {_unparked_clause(connection)}
             ORDER BY note.timestamp ASC, note.id ASC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def on_branch(stored: str | None, branch: str | None) -> bool:
    """Whether an item's stored `branch` is the checked-out `branch`.

    The one predicate, so `brief_items`'s SQL below and `brief.note_brief`'s
    scope cannot disagree about which items are on the branch. See
    `brief_items` for why a remote-tracking name is the same branch.
    """
    return bool(branch) and stored in (branch, f"origin/{branch}")


def brief_items(
    connection: sqlite3.Connection, repo: str, *, branch: str | None = None
) -> list[sqlite3.Row]:
    """The items a session's brief covers, in `repo`.

    Requirement 7 (`prd.md:1056-1060`): the item whose branch is checked
    out, or every not-`done` item in the repository when no branch matches.
    A `done` item never matches, even by branch -- criterion 16 seeds one
    with open notes and asserts they are absent -- and neither does a parked
    one, for the reason `open_followups` and Today leave parked work out:
    paused work is not the next session's action list.

    `branch=None` is "no branch is checked out" (a detached HEAD), which is
    the repository-wide case and not an error. Two live items on one branch
    both match; the brief is theirs together.

    **`branch` is a local name and `item.branch` may be a remote-tracking
    one.** `brief.checked_out_branch` reads `git symbolic-ref --short`, which
    answers `feat/x`, while `sources.docs_work` records the ref it read the
    prd from and that is `origin/feat/x`. Compared exactly, those never met:
    an item on the checked-out branch fell through to the repository-wide
    list, and a session working one branch was briefed on every open item in
    the repository. So `origin/<branch>` matches too. Only that direction --
    a caller passing `origin/feat/x` is asking for a branch of that name and
    gets it, because nothing here reads a remote-tracking name as local.
    """
    # Every stored form of the repository, so a disk path and its key brief
    # the same rows (sd:1439).
    probe = paths.keys(repo)
    live = (f"item.repo IN ({paths.placeholders(probe)}) AND item.status != 'done' "
            f"AND {_unparked_clause(connection)}")
    order = "ORDER BY item.updated_at DESC, item.id DESC"
    if branch:
        rows = connection.execute(
            f"SELECT item.* FROM item WHERE {live} AND item.branch IN (?, ?) {order}",
            (*probe, branch, f"origin/{branch}"),
        ).fetchall()
        if rows:
            return list(rows)
    return list(connection.execute(f"SELECT item.* FROM item WHERE {live} {order}", probe).fetchall())


def brief_notes(connection: sqlite3.Connection, items: list[int]) -> list[sqlite3.Row]:
    """The open `followup` and `question` notes on `items`, newest first.

    Newest first is requirement 7's order and the opposite of
    `open_followups`, which is Today's oldest-first list: a brief is read
    from the top by a session that has eight kilobytes, so what was named
    last -- nearest to where the last session stopped -- comes first. The
    id is the tie-break so two notes written in one second keep one order.
    Open is `resolved_at IS NULL`; a resolved note stays in the history and
    never returns here.
    """
    if not items:
        return []
    marks = ", ".join("?" for _ in items)
    kinds = ", ".join("?" for _ in BRIEF_KINDS)
    return list(
        connection.execute(
            f"""
            SELECT note.*, item.title AS item_title, item.branch AS item_branch
              FROM note JOIN item ON item.id = note.item
             WHERE note.item IN ({marks}) AND note.kind IN ({kinds})
               AND note.resolved_at IS NULL
             ORDER BY note.timestamp DESC, note.id DESC
            """,
            (*items, *BRIEF_KINDS),
        ).fetchall()
    )


def cost_by_bill(
    connection: sqlite3.Connection, *, now: str | None = None, caps: Mapping[str, float | None] | None = None
) -> list[sqlite3.Row]:
    """The cost tile: one line per bill, spend this month against its cap.

    `meter` rows are excluded -- requirement 6 says they feed the plan-usage
    line on Usage and nothing else. `reserved` and `sending` are money not yet
    known to be spent and are reported beside the spend, not inside it.

    `caps` is the cap per bill as the registry merges it, `usage.caps`, which
    a caller with a registry beside its database passes: a bill it names takes
    that cap in place of its row's, so a legacy row cap on a bill a `start`
    entry is billed to -- the one `registry.merge` leaves out of the merged
    view and reports in `warnings` -- is not printed as the cap the way
    `provider_controls.snapshot` does not print it (sd:234 slice 12h). A bill
    it does not name, and every bill when it is None, keeps the row's. The
    overlay is the statement's own, `json_each` of the mapping joined on the
    bill's name, so the rows stay `sqlite3.Row`s, the module's contract, and
    the cap is read in the same snapshot as the spend. This module reads the
    database and nothing else, so the registry is the caller's to consult.
    """
    month = _now(now)[:7]
    return list(
        connection.execute(
            """
            SELECT bill.name AS name,
                   bill.cost_basis AS cost_basis,
                   CASE WHEN merged.key IS NULL THEN bill.cap_usd_month
                        ELSE merged.value END AS cap_usd_month,
                   COALESCE(SUM(CASE WHEN cost.source IN ('run', 'bound')
                                     THEN cost.usd END), 0.0) AS spent,
                   COALESCE(SUM(CASE WHEN cost.source IN ('reserved', 'sending')
                                     THEN cost.usd END), 0.0) AS reserved
              FROM bill
              LEFT JOIN json_each(?) AS merged
                ON merged.key = bill.name
              LEFT JOIN cost
                ON cost.bill = bill.name
               AND substr(cost.timestamp, 1, 7) = ?
             GROUP BY bill.name
             ORDER BY bill.name ASC
            """,
            (json.dumps(dict(caps) if caps else {}), month),
        ).fetchall()
    )


@dataclass(frozen=True)
class Number:
    """One of the four weekly numbers, with the inputs it was computed from.

    Criterion 15 asks that the inputs are "visible on hover or in a detail
    view". The dashboard renders the detail view and never the hover, so the
    inputs travel with the number rather than being recomputed by the face.
    """

    key: str
    label: str
    value: float | None
    unit: str
    inputs: tuple[tuple[str, str], ...]


def _week(now: str) -> tuple[str, str]:
    """The seven days ending at `now`, as two dates."""
    end = _parse(now)
    return (end - timedelta(days=7)).strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _git_log(path: Path, since: str, until: str) -> list[str]:
    """Commit subjects in a repository's week, or nothing when it cannot be read."""
    try:
        done = subprocess.run(
            ["git", "-C", str(path), "log", "--no-merges",
             f"--since={since}", f"--until={until}T23:59:59", "--format=%s"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [line for line in done.stdout.splitlines() if line.strip()]


def weekly_numbers(
    connection: sqlite3.Connection, *, now: str | None = None, repo_paths: list[str] | None = None
) -> list[Number]:
    """The four numbers of requirement 6, over items shipped in the week.

    Shipped is `shipped_at` in the week and nothing else: a declined idea, a
    closed report and a finished task reach `done` and are never shipped, so
    they are not in the denominator.
    """
    stamp = _now(now)
    since, until = _week(stamp)
    shipped = list(
        connection.execute(
            "SELECT id, title, shipped_at FROM item "
            " WHERE shipped_at IS NOT NULL AND shipped_at >= ? AND shipped_at <= ? "
            " ORDER BY shipped_at ASC, id ASC",
            (since, until + "T23:59:59Z"),
        ).fetchall()
    )
    ids = [row["id"] for row in shipped]
    count = len(ids)

    usd = None
    if ids:
        marks = ",".join("?" * len(ids))
        usd = connection.execute(
            f"""
            SELECT SUM(cost.usd)
              FROM cost JOIN assignment ON assignment.id = cost.assignment
             WHERE cost.source IN ('run', 'bound')
               AND assignment.item IN ({marks})
            """,
            ids,
        ).fetchone()[0]

    hours: list[float] = []
    for row in shipped:
        started = connection.execute(
            "SELECT MIN(started) FROM assignment WHERE item = ? AND started IS NOT NULL",
            (row["id"],),
        ).fetchone()[0]
        first, delivered = _parse(started), _parse(row["shipped_at"])
        if first and delivered and delivered >= first:
            hours.append((delivered - first).total_seconds() / 3600.0)
    hours.sort()
    median = None
    if hours:
        middle = len(hours) // 2
        median = hours[middle] if len(hours) % 2 else (hours[middle - 1] + hours[middle]) / 2

    if repo_paths is None:
        repo_paths = [
            row["path"]
            for row in connection.execute("SELECT path FROM repo ORDER BY path").fetchall()
        ]
    subjects: list[tuple[str, str]] = []
    for path in repo_paths:
        for subject in _git_log(paths.disk(path), since, until):
            subjects.append((path, subject))
    reverts = [pair for pair in subjects if pair[1].startswith("Revert")]

    return [
        Number(
            key="cost_per_shipped",
            label="cost per shipped item",
            value=(usd / count) if count and usd is not None else None,
            unit="usd",
            inputs=(
                ("shipped items", str(count)),
                ("run and bound cost", f"{usd:.2f}" if usd is not None else "not recorded"),
                ("meter rows", "excluded"),
                ("week", f"{since} to {until}"),
            ),
        ),
        Number(
            key="commits_per_shipped",
            label="framework commits per shipped item",
            value=(len(subjects) / count) if count else None,
            unit="commits",
            inputs=(
                ("commits", str(len(subjects))),
                ("repositories", str(len(repo_paths))),
                ("shipped items", str(count)),
            ),
        ),
        Number(
            key="assignment_to_merge",
            label="assignment to merge",
            value=median,
            unit="hours",
            inputs=(
                ("items with an assignment", str(len(hours))),
                ("statistic", "median"),
            ),
        ),
        Number(
            key="reverts",
            label="reverts",
            value=float(len(reverts)),
            unit="commits",
            inputs=tuple((path, subject) for path, subject in reverts[:20])
            or (("commits whose subject starts with Revert", "none"),),
        ),
    ]


class OverBudget(SdDbError):
    """A read that ran past the budget its caller set and was stopped, not waited on."""


def missing_trailers(
    connection: sqlite3.Connection, *, now: str | None = None, repo_paths: list[str] | None = None,
    within: float | None = None,
) -> int:
    """The missing-trailer count Today shows: week commits with no `Authored-with:`.

    **The record separator is not a newline, because `%(trailers)` is not one
    line.** The first version of this function asked for `%H%x00%(trailers)`
    and split the output on `\\n`, so only a commit's *first* trailer line
    carried the NUL that identified the record; a commit whose
    `Authored-with:` was its second trailer had that line skipped by the
    "no NUL here" guard and was counted as missing. Two commits, one of them
    correctly trailered, came back as two missing.

    So `-z` separates the records, which puts the NUL *between* commits where
    a newline cannot reach it, and `%x1f` separates the two fields inside a
    record -- a unit separator, because it cannot occur in a hash and, unlike
    the NUL, does not collide with what `-z` is already using.

    `within` is an overall time budget in seconds, for a caller that must not
    wait on a slow fleet (the Health page). The walk stops when it is spent
    and raises `OverBudget`: a partial count is not the count. Without it
    each repository still has its own 20 s timeout, as before.
    """
    stamp = _now(now)
    stop = None if within is None else time.monotonic() + within
    refusal = f"the trailer count ran past its budget of {within:g} seconds" if within is not None else ""
    since, until = _week(stamp)
    if repo_paths is None:
        repo_paths = [
            row["path"]
            for row in connection.execute("SELECT path FROM repo ORDER BY path").fetchall()
        ]
    missing = 0
    for path in repo_paths:
        timeout = 20.0
        if stop is not None:
            timeout = min(timeout, stop - time.monotonic())
            if timeout <= 0:
                raise OverBudget(refusal)
        try:
            done = subprocess.run(
                ["git", "-C", str(paths.disk(path)), "log", "--no-merges", "-z",
                 f"--since={since}", f"--until={until}T23:59:59",
                 "--format=%H%x1f%(trailers)"],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            if stop is not None and time.monotonic() >= stop:
                raise OverBudget(refusal) from None
            continue
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode != 0:
            continue
        for record in done.stdout.split("\x00"):
            if not record.strip():
                continue
            _, separator, trailers = record.partition("\x1f")
            if not separator:
                continue
            if TRAILER not in trailers:
                missing += 1
    return missing


def runner_board(
    connection: sqlite3.Connection, *, now: str | None = None
) -> dict[str, list[sqlite3.Row]]:
    """One lane per assignment status, and `done` for the day alone.

    Every card carries what it waits on: the `after` row, the repository
    another row holds, or the operator's merge. Those are columns on the row,
    joined here so the face never issues a second query per card.
    """
    stamp = _now(now)
    rows = connection.execute(
        """
        SELECT assignment.*,
               item.title AS item_title,
               item.repo AS item_repo,
               waited.status AS after_status,
               waited.role AS after_role
          FROM assignment
          LEFT JOIN item ON item.id = assignment.item
          LEFT JOIN assignment AS waited ON waited.id = assignment.after
         ORDER BY assignment.id ASC
        """
    ).fetchall()
    board: dict[str, list[sqlite3.Row]] = {lane: [] for lane in BOARD_LANES}
    holders = {
        row["item_repo"]: row["id"]
        for row in rows
        if row["status"] == "running" and row["item_repo"]
    }
    for row in rows:
        status = row["status"]
        if status == "done" and _day(row["ended"] or "") != _day(stamp):
            continue
        if status not in board:
            continue
        board[status].append(row)
    board["_holders"] = holders  # type: ignore[assignment]
    return board


def waiting_on(row: sqlite3.Row, holders: dict[str, int]) -> str | None:
    """What a queued row waits on, in the words requirement 5 uses."""
    if row["status"] != "queued":
        return None
    if row["after"] is not None:
        return f"assignment {row['after']} ({row['after_status'] or 'unknown'})"
    repo = row["item_repo"]
    if repo and repo in holders and holders[repo] != row["id"]:
        return f"the repository, held by assignment {holders[repo]}"
    if (row["role"] or "") == "merge":
        return "the operator's merge"
    return None


def day_timeline(
    connection: sqlite3.Connection, *, now: str | None = None
) -> list[sqlite3.Row]:
    """The day's assignments, one row per bar, ordered by repository then start.

    A row still running has no `ended`; the face draws it to now. The lane is
    the repository, which is why the order is the repository first.
    """
    day = _day(_now(now))
    return list(
        connection.execute(
            """
            SELECT assignment.*,
                   item.title AS item_title,
                   COALESCE(item.repo, '(no repository)') AS lane
              FROM assignment
              LEFT JOIN item ON item.id = assignment.item
             WHERE substr(COALESCE(assignment.started, assignment.ended, ''), 1, 10) = ?
                OR (assignment.started IS NOT NULL AND assignment.ended IS NULL)
             ORDER BY lane ASC, assignment.started ASC, assignment.id ASC
            """,
            (day,),
        ).fetchall()
    )


@dataclass(frozen=True)
class ScorecardRow:
    """One provider's month, from `cost` rows and the review notes."""

    provider: str
    enabled: bool
    reason: str | None
    author_rank: int | None
    reviewer_rank: int | None
    passes: int
    blocking: int
    usd: float
    fallthrough: int

    @property
    def usd_per_pass(self) -> float | None:
        return (self.usd / self.passes) if self.passes else None


def scorecard(connection: sqlite3.Connection, *, now: str | None = None) -> list[ScorecardRow]:
    """The provider scorecard for the month, beside the rank it decides.

    `passes` is the count of distinct review passes the provider's cost rows
    name; `blocking` counts the `proposal` notes those passes' assignments
    left; `fallthrough` counts the calls that skipped the entry, which are
    `cost` rows carrying the provider with no assignment and a zero amount.
    """
    month = _now(now)[:7]
    rows = connection.execute(
        """
        SELECT provider.name AS name,
               provider.enabled AS enabled,
               provider.reason AS reason,
               provider.author_rank AS author_rank,
               provider.reviewer_rank AS reviewer_rank,
               COUNT(DISTINCT CASE WHEN cost.pass IS NOT NULL
                                   THEN cost.assignment || ':' || cost.pass END) AS passes,
               COALESCE(SUM(CASE WHEN cost.source IN ('run', 'bound')
                                 THEN cost.usd END), 0.0) AS usd,
               COALESCE(SUM(CASE WHEN cost.source = 'run' AND cost.assignment IS NULL
                                 THEN 1 END), 0) AS fallthrough
          FROM provider
          LEFT JOIN cost
            ON cost.provider = provider.name
           AND substr(cost.timestamp, 1, 7) = ?
         GROUP BY provider.name
         ORDER BY provider.name ASC
        """,
        (month,),
    ).fetchall()
    out = []
    for row in rows:
        blocking = connection.execute(
            """
            SELECT COUNT(*) FROM note
             WHERE note.kind = 'proposal'
               AND substr(note.timestamp, 1, 7) = ?
               AND note.item IN (
                   SELECT assignment.item FROM assignment
                    WHERE assignment.provider = ?)
            """,
            (month, row["name"]),
        ).fetchone()[0]
        out.append(
            ScorecardRow(
                provider=row["name"],
                enabled=bool(row["enabled"]),
                reason=row["reason"],
                author_rank=row["author_rank"],
                reviewer_rank=row["reviewer_rank"],
                passes=int(row["passes"] or 0),
                blocking=int(blocking or 0),
                usd=float(row["usd"] or 0.0),
                fallthrough=int(row["fallthrough"] or 0),
            )
        )
    return out


# --------------------------------------------------------------------------
# Backlog
# --------------------------------------------------------------------------

#: A clean run report is not backlog. `sd:739` gave a quiet cron tick the
#: `state` heartbeat it should always have had, but the reports already written
#: stay, and every one of them is a row in the list, the board and the matrix
#: for fourteen days -- seven `planning` before `retention` settles it, seven
#: more inside `include_done_days`. Measured on the live store the day this was
#: written: 310 of 739 rows, 42% of the shared set, and 84% of the `0-0d` bar
#: in the age histogram.
#:
#: The exclusion is by `attention` and NOT by kind, which matters. An attention
#: report is a durable item -- it opens a followup, it waits for a person, and
#: `is_urgent` below promotes it into the matrix's urgent quadrant by name. A
#: blanket `kind = 'report'` clause would delete the rows that function was
#: written to find and leave it dead in the same file. 282 of the 310 are
#: clean; the 28 that ask for something stay.
#:
#: It belongs inside the shared query rather than in a view. The docstring
#: below is the reason: three views of one row set, and a view that filtered
#: again would make `Run sequential` mean different things in different places.
#: Narrowing the one set is compatible with that; a second filter is not.
#:
#: `json_valid` is load-bearing and not defensive dressing. `json_extract`
#: RAISES `sqlite3.OperationalError: malformed JSON` on a bad document -- it
#: does not return NULL, which is what this clause was first written to assume.
#: Measured by the test below: without the guard, one report row with unparsable
#: `fields` takes down the whole backlog query, and with it the list, the board
#: and the matrix. `is_urgent` further down has always caught `ValueError` for
#: the same reason; this is that care moved into the SQL. NULL `fields` and
#: the empty string fall out the same way -- `json_valid(NULL)` is NULL and
#: `json_valid('')` is 0, neither true -- so the row is not listed.
#:
#: Not listed is NOT the same as clean, and this clause is not the one that
#: decides. The one meaning for a `fields` that cannot say the report is
#: clean is `reporting.UNREADABLE_FIELDS` (sd:873): such a report is for a
#: person to look at. Retention never settles it and names it, with
#: attention, in the prune report; the bulk clean declines it; the page
#: withholds the one-click acknowledge. The backlog lists a report on its own
#: word, `attention` true, and this row has no word, so it is not listed on
#: its own -- the prune report that names it is the attention report that
#: carries it into this backlog and the urgent quadrant. `is_urgent` below
#: answers False for the same three inputs, so the three views agree.
#:
#: Precisely, so the tests below are not read as proving more than they do:
#: the guard is load-bearing for MALFORMED JSON only. A NULL `fields` would be
#: quiet without it, because `json_type(NULL, ...)` returns NULL rather than
#: raising. `test_a_null_fields_report_is_quiet` therefore fixes the answer for
#: a real row shape -- `item.fields` is nullable -- and is not evidence for
#: `json_valid`. The malformed test is the one that proves the guard.
#:
#: `json_type` and not `json_extract`, because the two disagree about numbers.
#: `json_extract` returns 1 for the JSON boolean `true` AND for the JSON number
#: `1`, while `is_urgent` below asks `fields.get("attention") is True` and
#: accepts only the boolean. A row carrying `{"attention": 1}` was therefore in
#: the backlog and not in the matrix's urgent quadrant -- measured, before this
#: was tightened -- which is the shared-row contract broken by the clause
#: written to protect it. `json_type` answers `'true'` for the boolean and
#: `'integer'` for the number, so it is the one that matches.
#:
#: `ingest` cannot produce a numeric one: `source:local-sd-db/sd_db/reporting.py::ingest`
#: refuses anything where `type(attention) is not bool`. So this only ever sees a hand-edited or
#: legacy row, and for those quiet is the safe direction -- a report that does
#: not say it needs attention in the type the system uses is not evidence that
#: it does. That is the same reasoning as the malformed case above, and now the
#: same answer.
_NOT_A_QUIET_REPORT = (
    "(item.kind != 'report'"
    " OR (json_valid(item.fields)"
    "     AND json_type(item.fields, '$.attention') = 'true'))"
)

_BACKLOG_SQL = f"""
SELECT item.*, {_STATUS_SINCE} AS status_since
  FROM item
 WHERE ({{clauses}})
   AND {_NOT_A_QUIET_REPORT}
   AND {{unparked}}
 ORDER BY item.priority IS NULL, item.priority ASC,
          item.due IS NULL, item.due ASC, item.id ASC
"""


def backlog_items(
    connection: sqlite3.Connection,
    *,
    kind: str | None = None,
    repo: str | _NoRepo | None = None,
    status: str | None = None,
    include_done_days: int = 7,
    now: str | None = None,
) -> list[sqlite3.Row]:
    """Unparked open items, and `done` for the week, in one shared order.

    The list, the board and the matrix are three views of *one row set*
    (requirement 5), so there is one query. A view that filtered again would
    make `Run sequential` mean something different in a column than in the
    list, which is what the one list component exists to prevent.

    `repo` selects one repository, `NO_REPO` selects the items that belong to
    none, and `None` selects every item either way.

    A run report carrying no `attention` is never in the set -- see
    `_NOT_A_QUIET_REPORT`. Passing `kind="report"` therefore returns the
    attention reports and only those, which is the honest answer to "what
    reports need me" and not a filter the caller has to remember to apply.
    """
    stamp = _now(now)
    cutoff = (_parse(stamp) - timedelta(days=include_done_days)).strftime("%Y-%m-%d")
    clauses = ["item.status != 'done'",
               "(item.status = 'done' AND "
               f"{_STATUS_SINCE} >= :cutoff)"]
    params: dict[str, object] = {"cutoff": cutoff}
    sql = _BACKLOG_SQL.format(clauses=" OR ".join(clauses), unparked=_unparked_clause(connection))
    rows = list(connection.execute(sql, params).fetchall())
    if kind:
        rows = [row for row in rows if row["kind"] == kind]
    if repo is NO_REPO:
        rows = [row for row in rows if not row["repo"]]
    elif repo:
        # A `?repo=` bookmark holding the absolute path still matches the
        # `~/` row, and the key matches itself (sd:1439).
        wanted = set(paths.keys(repo))
        rows = [row for row in rows if (row["repo"] or "") in wanted]
    if status:
        rows = [row for row in rows if row["status"] == status]
    return rows


def is_urgent(row: sqlite3.Row, *, now: str | None = None) -> bool:
    """The matrix's derived urgency, exactly as requirement 5 defines it.

    `due` within seven days, `ready_to_send` older than three days, or a
    `report` row carrying `attention`. Importance is the row's `priority` and
    is not derived, which is the point of the quadrant: important work with no
    due date has a place instead of sinking under what is due.
    """
    stamp = _now(now)
    due = _parse(row["due"])
    if due is not None and (due - _parse(stamp)).days <= 7:
        return True
    if row["status"] == "ready_to_send":
        days = _days_between(row["status_since"], stamp) if "status_since" in row.keys() else None
        if days is not None and days > 3:
            return True
    if row["kind"] == "report":
        import json

        try:
            fields = json.loads(row["fields"] or "{}")
        except (ValueError, TypeError):
            fields = {}
        return isinstance(fields, dict) and fields.get("attention") is True
    return False


@dataclass(frozen=True)
class AgeBucket:
    label: str
    lower: int
    upper: int | None
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def key(self) -> str:
        """What a URL carries to mean this bucket.

        The lower bound, which is unique across the bounds and stays readable
        in a link. **Not the label**: `3-6d` is prose, and the first version of
        the histogram put exactly that into the list's *text filter*, where it
        matched the string nothing renders -- `In status` renders `5d` -- so
        every bar filtered to an empty page. A bucket is a facet, not a search
        term, and this is the facet's value.
        """
        return str(self.lower)


def age_bounds() -> list[tuple[int, int | None]]:
    """The bucket bounds, low to high. The last one is open-ended."""
    bounds: list[tuple[int, int | None]] = []
    previous = 0
    for edge in AGE_EDGES:
        bounds.append((previous, edge))
        previous = edge
    bounds.append((previous, None))
    return bounds


def age_bucket(row: sqlite3.Row, *, now: str | None = None) -> str:
    """Which bucket one row falls in, as the key a URL carries.

    The one function the bar and the filter both compute from. Two of them --
    one drawing the histogram, one narrowing the list -- is how a bar comes to
    disagree with the rows under it, which is the failure this replaced.
    """
    stamp = _now(now)
    keys = row.keys() if hasattr(row, "keys") else ()
    days = _days_between(row["status_since"] if "status_since" in keys else None, stamp)
    if days is None:
        days = 0
    for lower, upper in age_bounds():
        if days >= lower and (upper is None or days < upper):
            return str(lower)
    return str(age_bounds()[-1][0])


def age_series(row: sqlite3.Row) -> str:
    """Which series a row belongs to. `ready_to_send` is its own (requirement 5)."""
    return "ready_to_send" if row["status"] == "ready_to_send" else "other"


def age_histogram(
    rows: list[sqlite3.Row], *, now: str | None = None
) -> list[AgeBucket]:
    """Open items by days in their current status, `ready_to_send` its own series.

    Takes the rows rather than the connection: the histogram sits above the
    three views and must count the *same* row set they render, filter and all.
    A second query here is how a bar stops agreeing with the list under it.
    """
    stamp = _now(now)
    bounds = age_bounds()
    index_of = {str(lower): index for index, (lower, _) in enumerate(bounds)}
    tally = [{"ready_to_send": 0, "other": 0} for _ in bounds]
    for row in rows:
        tally[index_of[age_bucket(row, now=stamp)]][age_series(row)] += 1
    buckets: list[AgeBucket] = []
    for (lower, upper), counts in zip(bounds, tally):
        label = f"{lower}-{upper - 1}d" if upper is not None else f"{lower}d+"
        buckets.append(AgeBucket(label=label, lower=lower, upper=upper, counts=counts))
    return buckets


# --------------------------------------------------------------------------
# Item
# --------------------------------------------------------------------------


def item_by_id(connection: sqlite3.Connection, item: int) -> sqlite3.Row | None:
    return connection.execute(
        f"SELECT item.*, {_STATUS_SINCE} AS status_since FROM item WHERE item.id = ?",
        (item,),
    ).fetchone()


def item_notes(connection: sqlite3.Connection, item: int) -> list[sqlite3.Row]:
    """Every note on the item, in order. Order is the history; there is no other."""
    return list(
        connection.execute(
            "SELECT * FROM note WHERE item = ? ORDER BY timestamp ASC, id ASC",
            (item,),
        ).fetchall()
    )


def status_changes(connection: sqlite3.Connection, item: int) -> list[sqlite3.Row]:
    """The `status_change` notes on the item, oldest first.

    Criterion 7's clause at `prd.md:1303-1307`: three status changes write
    three notes in order, each with old, new and time, and the item screen
    lists them. `writes.transition` is the only writer; the body it composes
    is `"<old> -> <new> by <who>"` and the row's `timestamp` is the time.
    """
    return list(
        connection.execute(
            "SELECT * FROM note WHERE item = ? AND kind = 'status_change' "
            " ORDER BY timestamp ASC, id ASC",
            (item,),
        ).fetchall()
    )


def item_assignments(connection: sqlite3.Connection, item: int) -> list[sqlite3.Row]:
    """The item's assignments with their cost, which is a sum and never a row."""
    return list(
        connection.execute(
            """
            SELECT assignment.*,
                   COALESCE((SELECT SUM(cost.usd) FROM cost
                              WHERE cost.assignment = assignment.id
                                AND cost.source IN ('run', 'bound')), 0.0) AS usd,
                   EXISTS(SELECT 1 FROM cost
                           WHERE cost.assignment = assignment.id
                             AND cost.source = 'bound') AS estimated
              FROM assignment
             WHERE assignment.item = ?
             ORDER BY assignment.id ASC
            """,
            (item,),
        ).fetchall()
    )


def item_shadow(
    connection: sqlite3.Connection, item: int, *, tracker: str | None = None
) -> sqlite3.Row | None:
    """The item's shadow, if it has one. The join is the item's `external_id`.

    `shadow` is keyed by `(tracker, url)` (sd:603), so a url identifies a row
    only once you say whose. Pass `tracker` when the caller knows it. When it
    does not, a url held by two trackers is ambiguous and this refuses rather
    than returning whichever row the index reached first -- an arbitrary
    answer here is the same silent reassignment the composite key exists to
    prevent, moved from the write side to the read side. `item.source` names
    where the item was read from, not a tracker, so it cannot stand in.
    """
    row = connection.execute(
        "SELECT source, external_id FROM item WHERE id = ?", (item,)
    ).fetchone()
    if row is None or not row["external_id"]:
        return None
    if tracker is not None:
        return connection.execute(
            "SELECT * FROM shadow WHERE tracker = ? AND url = ?",
            (tracker, row["external_id"]),
        ).fetchone()
    found = list(connection.execute(
        "SELECT * FROM shadow WHERE url = ? ORDER BY tracker, id", (row["external_id"],)
    ))
    if len(found) > 1:
        raise SdDbError(
            f"item {item} has a shadow url held by {len(found)} trackers "
            f"({', '.join(sorted({str(shadow['tracker']) for shadow in found}))}); "
            f"name one with tracker="
        )
    return found[0] if found else None


# ------------------------------------------------------------------ usage


# Exported here and not in the list at the top: an insertion above line 740
# moves the citation that line makes, and `tests/test_citations.py` keys it.
__all__ += ["BillUsage", "Usage", "month_of", "usage_month"]


@dataclass(frozen=True)
class BillUsage:
    """One bill's four numbers for the month, and the points its burn is drawn from.

    `spent` is the month's `run` and `bound` rows; `estimated` is the share of
    it that is `bound`, money the provider may have billed for a response
    that was lost; `held` is every `reserved` and `sending` row still open,
    whichever month it was made in, as `exposure` counts it; `cap` is
    `cap_usd_month`, None on a bill without one. `room` is what the cap has
    left after spend and holds, None without a cap. `burn` is the cumulative
    spend by day of the month, one point per day that has a row.
    """

    name: str
    cost_basis: str
    cap: float | None
    spent: float
    estimated: float
    held: float
    room: float | None
    burn: tuple[tuple[int, float], ...]


@dataclass(frozen=True)
class Usage:
    """The month as `sd usage` prints it and the Usage screen shows it.

    `bills` and `roles` are the groupings clause 15 names; `bound` is every
    `bound` row of the month, listed so what was billed is never counted as
    nothing; `meter` is the latest `meter` row per provider and window in
    the month, the two gauges of a `plan` bill. `days` is the month's length
    and `today` the day of the month `now` falls on, None when `now` is in
    another month, which is what the projection needs.
    """

    month: str
    days: int
    today: int | None
    bills: tuple[BillUsage, ...]
    roles: tuple[dict, ...]
    bound: tuple[dict, ...]
    meter: tuple[dict, ...]
    spent: float
    held: float

    def document(self) -> dict:
        """The plain shape both faces serialise; the JSON is the same bytes."""
        return {
            "month": self.month, "days": self.days, "today": self.today,
            "bills": [asdict(bill) for bill in self.bills],
            "roles": list(self.roles), "bound": list(self.bound),
            "meter": list(self.meter), "spent": self.spent, "held": self.held,
        }


def month_of(month: str | None = None, *, now: str | None = None) -> str:
    """`YYYY-MM`, or the month `now` is in. The one check for a month a
    caller passes: `usage.report` runs it before its sweep, so a refused
    month sweeps nothing, and `usage_month` runs it before its reads.

    `9999-12` parses and is refused anyway. `_usage_reads` measures a month's
    length by stepping into the next one, and December 9999 has no next one
    in `datetime`: `sd-db.sh usage --month 9999-12` raised an uncaught
    `OverflowError` out of the verb and out of the API the Usage screen
    reads. Refusing it here is the one place a caller's month is checked, so
    every reader gets the refusal rather than the traceback.
    """
    if month is None:
        return _now(now)[:7]
    try:
        if len(month) != 7:
            raise ValueError(month)
        first = datetime.strptime(month, "%Y-%m")
        # The step `_usage_reads` takes, taken here where it can be refused.
        (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    except (TypeError, ValueError, OverflowError):
        raise SdDbError(f"a month is YYYY-MM, not {month!r}") from None
    return month


def usage_month(
    connection: sqlite3.Connection,
    *,
    month: str | None = None,
    now: str | None = None,
    caps: Mapping[str, float | None] | None = None,
) -> Usage:
    """Requirement 6's month, from the one per-bill read Today's tile uses.

    The per-bill `spent` and `cap` are `cost_by_bill`'s own rows, asked for
    the month rather than for now and under the same `caps` (`usage.caps`,
    the registry's merged view; `cost_by_bill` says what it overlays), so
    the tile, the screen and `sd usage` cannot disagree; the item screen's
    `item_assignments` sums the same two sources per assignment. `held` is every open `reserved` and `sending`
    row whichever month it was made in, as `ledger.exposure` counts it, so
    `room` is the room a reservation would see; the tile's `reserved` is
    the month's own open rows, which differs only while a hold straddles a
    month's end. A month is a stored timestamp's first seven characters,
    so a row at 23:59 on the last day and one at 00:00 the next are in
    different months.
    """
    moment = _now(now)
    month = month_of(month, now=now)
    # The connection is `isolation_level=None`, so without this the SELECTs
    # below are each their own snapshot and a row committed between two of
    # them is in `spent` and not on the burn line. One deferred read
    # transaction holds the snapshot; a caller's own transaction already does.
    if connection.in_transaction:
        return _usage_reads(connection, month=month, moment=moment, caps=caps)
    connection.execute("BEGIN")
    try:
        return _usage_reads(connection, month=month, moment=moment, caps=caps)
    finally:
        connection.execute("COMMIT")


def _usage_reads(
    connection: sqlite3.Connection, *, month: str, moment: str, caps: Mapping[str, float | None] | None
) -> Usage:
    first = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    days = ((first.replace(day=28) + timedelta(days=4)).replace(day=1) - first).days
    today = int(moment[8:10]) if moment[:7] == month else None
    estimated = dict(connection.execute(
        "SELECT bill, COALESCE(SUM(usd), 0.0) FROM cost WHERE source = 'bound' "
        "AND substr(timestamp, 1, 7) = ? GROUP BY bill", (month,)).fetchall())
    held = dict(connection.execute(
        "SELECT bill, COALESCE(SUM(usd), 0.0) FROM cost "
        "WHERE source IN ('reserved', 'sending') GROUP BY bill").fetchall())
    burn: dict[str, list[tuple[int, float]]] = {}
    for row in connection.execute(
        # A start-session `run` row may carry `usd` NULL: the session reported
        # no total. It is nothing on the line, as `cost_by_bill` counts it.
        "SELECT bill, substr(timestamp, 9, 2) AS day, COALESCE(SUM(usd), 0.0) AS usd FROM cost "
        "WHERE source IN ('run', 'bound') AND substr(timestamp, 1, 7) = ? "
        "GROUP BY bill, day ORDER BY bill, day", (month,)):
        points = burn.setdefault(row["bill"], [])
        points.append((int(row["day"]), round((points[-1][1] if points else 0.0) + float(row["usd"]), 6)))
    bills = []
    for row in cost_by_bill(connection, now=f"{month}-01T00:00:00Z", caps=caps):
        cap = row["cap_usd_month"]
        bills.append(BillUsage(
            name=row["name"], cost_basis=row["cost_basis"], cap=cap,
            spent=float(row["spent"]), estimated=float(estimated.get(row["name"], 0.0)),
            held=float(held.get(row["name"], 0.0)),
            room=None if cap is None else round(float(cap) - float(row["spent"]) - float(held.get(row["name"], 0.0)), 6),
            burn=tuple(burn.get(row["name"], ())),
        ))
    roles = connection.execute(
        "SELECT bill, provider, role, COUNT(*) AS calls, "
        "COALESCE(SUM(CASE WHEN source IN ('run', 'bound') THEN usd END), 0.0) AS spent, "
        "COALESCE(SUM(CASE WHEN source IN ('reserved', 'sending') THEN usd END), 0.0) AS held, "
        "COALESCE(SUM(tokens_in), 0) AS tokens_in, COALESCE(SUM(tokens_out), 0) AS tokens_out "
        "FROM cost WHERE source != 'meter' AND (source IN ('reserved', 'sending') "
        "OR substr(timestamp, 1, 7) = ?) GROUP BY bill, provider, role "
        "ORDER BY bill, provider, role", (month,)).fetchall()
    bound = connection.execute(
        "SELECT id, call_id, timestamp, provider, bill, role, repo, assignment, pass, usd "
        "FROM cost WHERE source = 'bound' AND substr(timestamp, 1, 7) = ? ORDER BY timestamp, id",
        (month,)).fetchall()
    # The latest sample per provider and window, by timestamp then id, so
    # two samples at one moment resolve to the later row and not to
    # whichever bare column SQLite carries out of a MAX() group.
    meter = connection.execute(
        "SELECT provider, bill, window_minutes, used_percent, timestamp FROM cost AS latest "
        "WHERE source = 'meter' AND substr(timestamp, 1, 7) = ? AND id = ("
        "SELECT id FROM cost WHERE source = 'meter' AND provider IS latest.provider "
        "AND window_minutes IS latest.window_minutes AND substr(timestamp, 1, 7) = ? "
        "ORDER BY timestamp DESC, id DESC LIMIT 1) ORDER BY provider, window_minutes",
        (month, month)).fetchall()
    return Usage(
        month=month, days=days, today=today, bills=tuple(bills),
        roles=tuple(dict(row) for row in roles), bound=tuple(dict(row) for row in bound),
        meter=tuple(dict(row) for row in meter),
        spent=round(sum(bill.spent for bill in bills), 6), held=round(sum(bill.held for bill in bills), 6),
    )
