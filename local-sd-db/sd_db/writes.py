"""Every write, as a named function. No caller issues SQL.

Requirement 2 and criterion 2 both turn on this file: a grep of the pack,
this repository and the dashboard for `sqlite3.connect` returns only this
library, and every write in the codebase goes through a function named here.
A caller that needs a write nobody wrote adds a function here, in review,
rather than a string somewhere else.

The one rule that is not a matter of taste: **a status change writes its
note in the same transaction**. An item's row holds its current status; its
notes hold the history. The item screen reads the order from them, the age
histogram reads a row's status timestamp from the latest one, and lead time
and days-blocked are sums over them. A status written without its note is a
history with a hole in it, and the hole is unrecoverable -- nothing later can
say when the change happened. So there is no function that writes `status`
except `transition`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import paths
from .database import transaction
from .errors import SdDbError

#: The item statuses, and the note kind a change writes.
STATUSES = ("planning", "ready", "in_progress", "ready_to_send", "blocked", "done")
STATUS_CHANGE = "status_change"

#: The `state` kinds. The `CHECK` in the schema is the enforcement; this is
#: the list callers name, so a typo is an error here and not a silent row.
STATE_KINDS = ("checkpoint", "verified", "restore", "watermark", "heartbeat", "check")
#: The kinds whose NULL `resolved_at` is open work; the rest are logs and
#: cursors read latest-per-key, where NULL means nothing (sd:2849).
OPEN_STATE_KINDS = ("restore", "verified", "check")


class TransitionRefused(SdDbError):
    """A status change the kind's table does not allow."""


def now() -> str:
    """One clock, in UTC, in one format. Rows are compared as text."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def stamp(value: str) -> str:
    """One caller-supplied instant, in the one shape `now()` writes.

    Rows are compared as text, so two spellings of the same instant are two
    different instants to every reader in this file. `now()` writes
    `+00:00`; `2026-09-06T19:11:39Z` is the same moment and sorts *after* it,
    because `Z` is 0x5A and `+` is 0x2B. Mixed shapes therefore order
    correctly everywhere except at a tie on the same second -- which is
    exactly where `skill_use_since` and `active_trials` are asked their
    question, and exactly the boundary the `timestamp` parameter was added to
    make answerable.

    So the shape is enforced at the write rather than expected of the caller.
    A naive stamp is refused rather than assumed to be UTC: a local stamp
    read as UTC is wrong by the offset, silently, and no caller has one.
    """
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise SdDbError(
            f"{value!r} is not an ISO-8601 timestamp; rows are compared as "
            f"text and must be written in one shape"
        ) from None
    if moment.tzinfo is None:
        raise SdDbError(
            f"{value!r} carries no timezone. Pass an aware UTC stamp -- a "
            f"naive one read as UTC is wrong by the offset and says nothing "
            f"about it"
        )
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


# ---------------------------------------------------------------- repo


def upsert_repo(
    connection: sqlite3.Connection,
    path: str,
    *,
    remote: str | None = None,
    mode: str | None = None,
    runner_merge: str | None = None,
    managed: int | None = None,
    ci: str | None = None,
    satellite_gate: str | None = None,
    status_source: str | None = None,
    pieces_source: str | None = None,
) -> str:
    """Record a repository, or update the fields given.

    `None` means "leave it": a caller that knows the remote and not the mode
    must not blank the mode by passing the row it did not read.

    `path` is a key (sd:1439). An absolute path under `$HOME` is refused
    rather than converted: every caller in the library converts with
    `paths.key` first, so one arriving here bypassed the conversion, and a
    silent fix would hide the writer that did it.
    """
    if path.startswith("/") and paths.key(path) != path:
        raise paths.PathRefused(
            f"{path} is an absolute path under $HOME; a repository is stored as "
            f"its key {paths.key(path)!r}. Convert with `sd_db.paths.key` before "
            f"calling upsert_repo"
        )
    stamp = now()
    with transaction(connection):
        connection.execute(
            "INSERT INTO repo (path, remote, mode, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(path) DO NOTHING",
            (path, remote, mode, stamp, stamp),
        )
        updates = {
            "remote": remote,
            "mode": mode,
            "runner_merge": runner_merge,
            "managed": managed,
            "ci": ci,
            "satellite_gate": satellite_gate,
            "status_source": status_source,
            "pieces_source": pieces_source,
        }
        given = {key: value for key, value in updates.items() if value is not None}
        if given:
            assignments = ", ".join(f"{key} = ?" for key in given)
            connection.execute(
                f"UPDATE repo SET {assignments}, updated_at = ? WHERE path = ?",
                (*given.values(), stamp, path),
            )
    return path


# ---------------------------------------------------------------- item


def create_item(
    connection: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    status: str = "planning",
    repo: str | None = None,
    branch: str | None = None,
    path: str | None = None,
    stage: str | None = None,
    priority: int | None = None,
    due: str | None = None,
    source: str | None = None,
    external_id: str | None = None,
    source_commit: str | None = None,
    fields: dict | None = None,
    body: dict | None = None,
    session: str | None = None,
    created_at: str | None = None,
    recurrence: str | None = None,
    recurrence_anchor: str | None = None,
) -> int:
    """Open an item, and write the note that says it was opened.

    The opening note is a `status_change` from nothing, so the history starts
    at the first status rather than at the second.

    `created_at` is an argument because a migration knows something the clock
    does not: an item whose `prd.md` was written in July is not new today.
    Thirty-eight of the sixty-four `docs/work` items predate the forty-five
    day idle threshold, and seeding the clock from the import would offer all
    thirty-eight to the first sweep as freshly touched work. The note keeps
    the real time, so the history still says when the row was written.
    """
    if status not in STATUSES:
        raise TransitionRefused(f"no status {status!r}; the statuses are {', '.join(STATUSES)}")
    repo = paths.key(repo)
    stamp = now()
    opened = created_at or stamp
    with transaction(connection):
        values = {
            "kind": kind, "repo": repo, "branch": branch, "path": path,
            "title": title, "status": status, "stage": stage,
            "priority": priority, "due": due, "source": source,
            "external_id": external_id, "source_commit": source_commit,
            "fields": _json(fields), "body": _json(body),
            "created_at": opened, "updated_at": stamp,
        }
        # Named only when set, so a row without a rule is the same INSERT it
        # was before migration 012 added the columns.
        if recurrence is not None or recurrence_anchor is not None:
            values.update(recurrence=recurrence, recurrence_anchor=recurrence_anchor)
        cursor = connection.execute(
            f"INSERT INTO item ({', '.join(values)}) "
            f"VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )
        item = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO note (item, timestamp, kind, body, session) VALUES (?, ?, ?, ?, ?)",
            (item, stamp, STATUS_CHANGE, f"opened as {status}", session),
        )
    return item


def transition(
    connection: sqlite3.Connection,
    item: int,
    target: str,
    *,
    who: str,
    reason: str | None = None,
) -> str:
    """The one function that writes `item.status`.

    The note goes in the same transaction. `who` is recorded because the
    kind's table -- which arrives with the kinds that have one -- decides by
    asker as much as by target, and a change with no asker cannot be judged
    later.
    """
    with transaction(connection):
        row = connection.execute("SELECT kind, status FROM item WHERE id = ?", (item,)).fetchone()
        if row is not None and row["kind"] == "work" and row["status"] != target and target == "done":
            raise TransitionRefused(
                "work completion requires deliver_work or cancel_work and their verified evidence"
            )
        return _transition(connection, item, target, who=who, reason=reason)


def _transition(
    connection: sqlite3.Connection, item: int, target: str, *,
    who: str, reason: str | None = None,
) -> str:
    """Trusted status primitive used by source imports and completion policy.

    User-facing callers use transition or the workflow APIs. Imports retain
    historical source truth; completion policy verifies current evidence.
    """
    if target not in STATUSES:
        raise TransitionRefused(f"no status {target!r}; the statuses are {', '.join(STATUSES)}")
    stamp = now()
    with transaction(connection):
        row = connection.execute(
            "SELECT status, kind FROM item WHERE id = ?", (item,)
        ).fetchone()
        if row is None:
            raise TransitionRefused(f"no item {item}")
        current = row["status"]
        if current == target:
            return current
        connection.execute(
            "UPDATE item SET status = ?, updated_at = ? WHERE id = ?",
            (target, stamp, item),
        )
        note = f"{current} -> {target} by {who}"
        if reason:
            note += f": {reason}"
        connection.execute(
            "INSERT INTO note (item, timestamp, kind, body, session) VALUES (?, ?, ?, ?, ?)",
            (item, stamp, STATUS_CHANGE, note, who),
        )
    return current


def set_item_fields(
    connection: sqlite3.Connection,
    item: int,
    **columns: Any,
) -> None:
    """Update an item's columns. `status` is refused here; use `transition`."""
    if "status" in columns:
        raise TransitionRefused(
            "status is written by `transition`, which writes its note in the "
            "same transaction; this function would leave the history with a hole"
        )
    allowed = {
        "kind", "repo", "branch", "path", "title", "stage", "priority", "due",
        "source", "external_id", "shipped_at", "fields", "body", "source_commit",
        "created_at",
        "piece", "parked_at", "gate_generation", "ready_digest",
        "recurrence", "recurrence_anchor",
    }
    unknown = set(columns) - allowed
    if unknown:
        raise SdDbError(f"item has no column(s) {sorted(unknown)}")
    if not columns:
        return
    if columns.get("repo") is not None:
        columns["repo"] = paths.key(columns["repo"])
    for key in ("fields", "body"):
        if key in columns and not isinstance(columns[key], (str, type(None))):
            columns[key] = _json(columns[key])
    assignments = ", ".join(f"{key} = ?" for key in columns)
    with transaction(connection):
        connection.execute(
            f"UPDATE item SET {assignments}, updated_at = ? WHERE id = ?",
            (*columns.values(), now(), item),
        )



def item_by_external(
    connection: sqlite3.Connection, source: str, external_id: str
) -> sqlite3.Row | None:
    """The row a source already landed, or None. A read, kept beside its write."""
    return connection.execute(
        "SELECT * FROM item WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()


def upsert_item(
    connection: sqlite3.Connection,
    *,
    source: str,
    external_id: str,
    kind: str,
    title: str,
    status: str,
    who: str,
    created_at: str | None = None,
    **columns: Any,
) -> tuple[int, str]:
    """Land a row a migration read from a source, once. Returns `(id, what)`.

    `what` is `inserted`, `updated` or `unchanged`, which is what makes the
    second run of a migration assertable: criterion 5 asks each one to report
    the same counts with zero new rows, and a function that returned only an
    id could not tell those apart.

    Identity is `(source, external_id)`, the pair the schema's unique index
    already enforces. A migration that keyed on the title would land a second
    row the day somebody renamed a heading.

    **The status still goes through `transition`.** An import that wrote
    `status` directly would be the one hole in the rule this file exists to
    keep, and it would open on the largest write the system ever makes.
    A source whose status changed since the last import therefore lands a
    `status_change` note like any other change, with `who` naming the
    migration -- which is how criterion 23's "a status change made through the
    old command after the import is read back by the next import" is visible
    at all.
    """
    if columns.get("repo") is not None:
        columns["repo"] = paths.key(columns["repo"])
    found = item_by_external(connection, source, external_id)
    if found is None:
        # An idea promoted to a writing piece took the piece's identity and
        # kept its own in `promoted_from`; the piece owns that row now (sd:1994).
        # Only a piece row counts: an imported note can carry any frontmatter.
        promoted = connection.execute(
            "SELECT id FROM item WHERE source = 'writing-piece' AND piece IS NOT NULL "
            "AND CASE WHEN json_valid(fields) THEN "
            "json_extract(fields, '$.promoted_from.source') = ? "
            "AND json_extract(fields, '$.promoted_from.external_id') = ? END",
            (source, external_id),
        ).fetchone()
        if promoted is not None:
            return int(promoted[0]), "unchanged"
        item = create_item(
            connection,
            kind=kind,
            title=title,
            status=status,
            source=source,
            external_id=external_id,
            created_at=created_at,
            session=who,
            **columns,
        )
        return item, "inserted"

    item = int(found["id"])
    changed = {}
    for key, value in {"kind": kind, "title": title, **columns}.items():
        current = found[key] if key in found.keys() else None
        wanted = _json(value) if key in ("fields", "body") and not isinstance(
            value, (str, type(None))
        ) else value
        if current != wanted:
            changed[key] = value
    if changed:
        set_item_fields(connection, item, **changed)
    moved = found["status"] != status
    if moved:
        _transition(connection, item, status, who=who, reason="read from the source")
    if not changed and not moved:
        return item, "unchanged"
    return item, "updated"


# ---------------------------------------------------------------- note


def add_note(
    connection: sqlite3.Connection,
    item: int,
    kind: str,
    body: str,
    *,
    session: str | None = None,
    started: str | None = None,
    ended: str | None = None,
    exit_code: int | None = None,
    output_path: str | None = None,
) -> int:
    """Any note but a status change, which only `transition` writes."""
    if kind == STATUS_CHANGE:
        raise TransitionRefused(
            "a status_change note is written by `transition`, with the status, "
            "in one transaction"
        )
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO note (item, timestamp, kind, body, session, started, "
            "ended, exit_code, output_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item, now(), kind, body, session, started, ended, exit_code, output_path),
        )
    return int(cursor.lastrowid)


def resolve_note(connection: sqlite3.Connection, note: int) -> None:
    with transaction(connection):
        connection.execute(
            "UPDATE note SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL",
            (now(), note),
        )


# ---------------------------------------------------------------- state


def record_state(
    connection: sqlite3.Connection,
    kind: str,
    *,
    key: str | None = None,
    body: Any = None,
    timestamp: str | None = None,
) -> int:
    """One operational record: a checkpoint, a restore, a watermark, a tick.

    These are not items. They live in `state` because a record kind written
    into a table named for something else is how a schema grows by
    convention, which requirement 1 forbids.
    """
    if kind not in STATE_KINDS:
        raise SdDbError(
            f"no state kind {kind!r}; the kinds are {', '.join(STATE_KINDS)}. "
            f"A new kind is added to the schema and the document first."
        )
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO state (kind, key, timestamp, body) VALUES (?, ?, ?, ?)",
            (kind, key, timestamp or now(), _json(body) if not isinstance(body, str) else body),
        )
    return int(cursor.lastrowid)


def resolve_state(connection: sqlite3.Connection, row: int) -> None:
    """Mark a state record reconciled -- what `sd restore resume` does."""
    with transaction(connection):
        connection.execute(
            "UPDATE state SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL",
            (now(), row),
        )


def unresolved_state(connection: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    """Open records of one kind, oldest first. A read, kept beside its write."""
    return list(
        connection.execute(
            "SELECT * FROM state WHERE kind = ? AND resolved_at IS NULL "
            "ORDER BY timestamp",
            (kind,),
        )
    )


# ---------------------------------------------------------------- shadow


def upsert_shadow(
    connection: sqlite3.Connection,
    *,
    tracker: str,
    url: str,
    repo: str | None = None,
    number: int | None = None,
    kind: str | None = None,
    title: str | None = None,
    state: str | None = None,
    author: str | None = None,
) -> None:
    """A tracker's row, keyed by `(tracker, url)`. Seen once, then kept current.

    The conflict target is both columns, so this updates only the row this
    tracker already owns. Until sd:603 it was `url` alone and the update set
    `tracker = excluded.tracker`, which meant the last collector to see a url
    took the row from whoever held it -- silently, because reassigning an
    owner violates no constraint. A second tracker writing the same url now
    gets its own row instead.
    """
    stamp = now()
    with transaction(connection):
        connection.execute(
            "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, "
            "author, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tracker, url) DO UPDATE SET "
            "repo = excluded.repo, number = excluded.number, kind = excluded.kind, "
            "title = excluded.title, state = excluded.state, "
            "author = excluded.author, last_seen = excluded.last_seen",
            (tracker, repo, url, number, kind, title, state, author, stamp, stamp),
        )


# ----------------------------------------------------------- assignment


def create_assignment(
    connection: sqlite3.Connection,
    *,
    role: str,
    status: str,
    item: int | None = None,
    provider: str | None = None,
    after: int | None = None,
    parent: int | None = None,
    lane: str = "serial",
    budget_minutes: int | None = None,
    budget_usd: float | None = None,
) -> int:
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO assignment (item, role, provider, status, started, after, "
            "parent, lane, budget_minutes, budget_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item, role, provider, status, now(), after, parent, lane,
             budget_minutes, budget_usd),
        )
    return int(cursor.lastrowid)


def update_assignment(connection: sqlite3.Connection, assignment: int, **columns: Any) -> None:
    """Update assignment details; entering running is an atomic queue claim.

    A caller may dispatch only after this succeeds. A cancelled assignment is
    terminal, so a worker holding an old queue snapshot cannot resurrect it.
    """
    allowed = {"provider", "status", "started", "ended", "cost", "result",
               "after", "parent", "lane", "budget_minutes", "budget_usd", "phase"}
    unknown = set(columns) - allowed
    if unknown:
        raise SdDbError(f"assignment has no column(s) {sorted(unknown)}")
    if not columns:
        return
    assignments = ", ".join(f"{key} = ?" for key in columns)
    with transaction(connection):
        current = connection.execute("SELECT status FROM assignment WHERE id = ?", (assignment,)).fetchone()
        if current is None:
            raise SdDbError(f"no assignment {assignment}")
        if current["status"] == "cancelled" and columns.get("status", "cancelled") != "cancelled":
            raise SdDbError(f"assignment {assignment} is cancelled and cannot be reopened")
        if columns.get("status") == "running" and current["status"] != "queued":
            raise SdDbError(f"assignment {assignment} is not queued; cannot claim it")
        connection.execute(
            f"UPDATE assignment SET {assignments} WHERE id = ?",
            (*columns.values(), assignment),
        )


# ---------------------------------------------------------------- cost


def record_cost(
    connection: sqlite3.Connection,
    *,
    source: str,
    provider: str | None = None,
    bill: str | None = None,
    call_id: str | None = None,
    role: str | None = None,
    repo: str | None = None,
    assignment: int | None = None,
    pass_: str | None = None,
    owner_pid: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    usd: float | None = None,
    window_minutes: int | None = None,
    used_percent: float | None = None,
) -> int:
    """One cost row. `source` says what kind of number it is."""
    repo = paths.key(repo)
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO cost (call_id, timestamp, provider, bill, role, repo, "
            "assignment, pass, owner_pid, tokens_in, tokens_out, usd, "
            "window_minutes, used_percent, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (call_id, now(), provider, bill, role, repo, assignment, pass_,
             owner_pid, tokens_in, tokens_out, usd, window_minutes,
             used_percent, source),
        )
    return int(cursor.lastrowid)


# ----------------------------------------------------- skills and trials


def record_skill_use(
    connection: sqlite3.Connection,
    skill: str,
    *,
    surface: str | None = None,
    mode: str | None = None,
    cwd: str | None = None,
    timestamp: str | None = None,
) -> int:
    """One use of one skill. `timestamp` defaults to now, and often must not.

    A `PreToolUse` hook records a use as it happens and takes the default.
    The nightly parse of `~/.codex/sessions` does not: it reads sessions that
    ran while it was asleep, and stamping them with the parse time would file
    a week of Codex use under the morning the parse first ran. The parameter
    exists for the reader that knows when the thing happened.
    """
    cwd = paths.key(cwd)
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO skill_use (timestamp, skill, surface, mode, cwd) "
            "VALUES (?, ?, ?, ?, ?)",
            (now() if timestamp is None else stamp(timestamp), skill, surface, mode, cwd),
        )
    return int(cursor.lastrowid)


def start_trial(connection: sqlite3.Connection, skill: str, expires: str) -> None:
    with transaction(connection):
        connection.execute(
            "INSERT INTO trial (skill, started, expires) VALUES (?, ?, ?) "
            "ON CONFLICT(skill) DO UPDATE SET started = excluded.started, "
            "expires = excluded.expires",
            (skill, now(), stamp(expires)),
        )


def end_trial(connection: sqlite3.Connection, skill: str) -> bool:
    """Remove a trial row. True when there was one, so a caller can say so.

    The installer removes an expired trial that earned no use, and the
    sentence it prints is the reason the boolean is returned rather than
    discarded: "removed, no use in thirty days" is a different report from
    "nothing to remove", and a caller that cannot tell them apart prints the
    first one when the second is true.
    """
    with transaction(connection):
        cursor = connection.execute("DELETE FROM trial WHERE skill = ?", (skill,))
    return cursor.rowcount > 0


def trials(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every trial row, in skill order. The installer's second input.

    Unfiltered on purpose. The installer needs the expired ones too -- an
    expired trial with no use is what it removes -- so a reader that returned
    only the active ones would make the removal invisible to the one caller
    that has to perform it. `active_trials` is the filtered view, and it is
    built from this one rather than from a second query.
    """
    return list(connection.execute("SELECT * FROM trial ORDER BY skill"))


def active_trials(
    connection: sqlite3.Connection, moment: str | None = None
) -> list[sqlite3.Row]:
    """The trials that have not expired, at `moment` or now.

    Criterion 24's "the union of the paths plus active trials" is exactly
    this list. The comparison is a string comparison over ISO-8601 UTC
    stamps, which orders correctly because every stamp reaching a row went
    through `stamp()` and comes back out in one shape. It used to say the
    caller was *expected* to use that shape, which is not a property a
    reader can rely on; `start_trial` normalizes now, and so does the
    `moment` a caller passes here.
    """
    when = now() if moment is None else stamp(moment)
    return [row for row in trials(connection) if row["expires"] > when]


def skill_use_since(
    connection: sqlite3.Connection, skill: str, since: str
) -> int:
    """How many times a skill was used at or after `since`.

    Criterion 25 removes an expired trial "with no `skill_use` rows", and the
    window that matters is the trial's own: a row from before the trial
    started is not evidence the trial earned anything. So the caller passes
    the trial's `started`, never a bare zero, and the count answers a
    question about the trial rather than about the skill's whole history.

    `since` is normalized for the same reason the writes are: a `...Z` stamp
    and a `...+00:00` stamp of the same second compare differently, and this
    comparison is the one that lives on that boundary.
    """
    row = connection.execute(
        "SELECT count(*) AS uses FROM skill_use WHERE skill = ? AND timestamp >= ?",
        (skill, stamp(since)),
    ).fetchone()
    return int(row["uses"])


# --------------------------------------------------- providers and bills


def set_provider_state(
    connection: sqlite3.Connection,
    name: str,
    *,
    enabled: bool | None = None,
    reason: str | None = None,
    author_rank: int | None = None,
    reviewer_rank: int | None = None,
) -> None:
    """What the dashboard changes about a provider. Identity stays in the file."""
    columns: dict[str, Any] = {}
    if enabled is not None:
        columns["enabled"] = int(enabled)
    if reason is not None:
        columns["reason"] = reason
    if author_rank is not None:
        columns["author_rank"] = author_rank
    if reviewer_rank is not None:
        columns["reviewer_rank"] = reviewer_rank
    if not columns:
        return
    assignments = ", ".join(f"{key} = ?" for key in columns)
    with transaction(connection):
        connection.execute(
            f"UPDATE provider SET {assignments} WHERE name = ?",
            (*columns.values(), name),
        )


def set_bill_cap(connection: sqlite3.Connection, name: str, cap_usd_month: float | None) -> None:
    with transaction(connection):
        connection.execute(
            "UPDATE bill SET cap_usd_month = ? WHERE name = ?", (cap_usd_month, name)
        )
