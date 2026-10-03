"""User task controls shared by the dashboard and CLI.

These functions write only the database. Repository files and external
trackers are optional context, never prerequisites for an ordinary task.
Validation and the writes it authorizes share a transaction, so stale forms
cannot overwrite a newer edit or leave history behind after a refusal.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sqlite3
from datetime import date
from typing import Any

from . import repos
from .database import transaction
from .errors import SdDbError
from .reads import item_by_id, item_notes
from .recurrence import ANCHORS, RecurrenceError, first_barren_start, next_after, parse as parse_rule
from .writes import (
    STATUSES, TransitionRefused, add_note, create_item, resolve_note,
    set_item_fields, transition,
)

USER_FIELDS = frozenset({"title", "body", "priority", "due", "repo", "kind"})

#: The recurrence columns migration 012 added (sd:1099). `edit_item` accepts
#: them beside `USER_FIELDS` but they stay out of that set: the dashboard's
#: details form offers exactly `USER_FIELDS`, its `ItemRepository` test holds
#: the two equal, and the form has no recurrence control yet.
RECURRENCE_FIELDS = frozenset({"recurrence", "recurrence_anchor"})

#: The item kinds a person may set by hand: the five the pack's `sd task add`
#: files (`ADD_KINDS`, `bin/sd_work.py`), for the reason that list gives. Each
#: other kind in `item.kind`'s CHECK has a producer -- `work` the work lane,
#: `report` the job that ran, `dep` the item that waits on it, `skill-review`
#: the catalogue -- or is reserved (`proposal`), or is `idea`, which
#: `writing.list_pieces` reads as a draft article only when it carries a
#: `piece`. A hand edit that made one would be a second way to make that row,
#: and a hand edit that unmade one would take a row away from its producer.
HAND_KINDS = ("task", "personal", "followup", "work-idea", "personal-idea")

#: The hand kinds filed with no repository, which the pack's `sd task add`
#: reads to settle the repository question and `reads.backlog_items(repo=NO_REPO)`
#: reads as a group: a personal to-do and the two ideas. `followup` left the
#: set on 2026-09-14 (sd:809): a followup from a code review is about one
#: repository, and with no repo a checkout's brief (`reads.brief_items`) and
#: `reads.backlog_items(repo=path)` never list it. A repository does not make
#: a followup runnable: `runner._item` refuses every kind outside
#: `runner.RUNNABLE_KINDS`, whatever repository and branch the row carries.
REPO_LESS_KINDS = frozenset({"personal", "work-idea", "personal-idea"})

#: The kinds whose user fields `edit_item` edits: title, priority, due,
#: repository and body, and `kind` beside them. A `work` row is further held to
#: its file owner until cutover, and only a `task` or `followup` body is edited
#: here. `followup` joined on 2026-09-14 (sd:809), so a followup can be moved
#: to the repository it is about. Every other kind may have its `kind` edited
#: alone, and its other fields keep their own editing workflow.
DETAIL_KINDS = ("task", "followup", "work")

#: The `item.fields` keys a producer writes onto a hand kind, each read back
#: by a reader that also keys on the row's kind. A row carrying one is a
#: produced row whatever its kind says, so its kind stays put:
#: `contribution` is written by `contributions.configure` and read by
#: `contributions._registered`, which takes only `kind='task'` rows -- a move
#: away and back would skip its one-per-pull-request and dependency-cycle
#: checks; `skill_review` is written by `skills_catalog` onto the `task` rows
#: that promote, demote or apply proposals to a skill, which the runner and
#: `delivery_candidates` read by kind.
PRODUCED_FIELDS = ("contribution", "skill_review")

#: The assignment statuses that still hold an item. `ending` is one: the
#: runner has not released the checkout yet (`runner.begin_ending`). No runner
#: constant names the set; `runner.py` spells it inline.
ACTIVE_ASSIGNMENTS = ("queued", "running", "ending")

NOTE_KINDS = ("comment", "followup", "question", "decision", "proposal")
TASK_STATUSES = ("planning", "ready", "in_progress", "blocked", "done")

#: The kinds whose status a person moves with the task controls, through the
#: same five `TASK_STATUSES`. `personal` and `followup` joined `task` on
#: 2026-09-13 (sd:768): each is a hand-filed item worked in its own right, and
#: before that the task controls offered them no status, so they could not
#: close one. `work` has its own delivery rules; `idea`, `work-idea`,
#: `personal-idea` and `report` keep their own workflows and get no status
#: choices here.
TASK_STATUS_KINDS = ("task", "personal", "followup")


class WorkflowError(SdDbError):
    """A user request could not be applied."""


class MissingItem(WorkflowError):
    """The stable item ID does not exist."""


class MissingNote(WorkflowError):
    """The note ID does not exist."""


class StaleItem(WorkflowError):
    """The request was based on a state that has since changed."""


def _identifier(value: int, name: str) -> None:
    if type(value) is not int or value < 1:
        raise WorkflowError(f"{name} must be a positive integer")


def _text(value: Any, name: str, *, blank: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise WorkflowError(f"{name} must be text without NUL characters")
    if not blank and not value.strip():
        raise WorkflowError(f"{name} must not be blank")
    return value if blank else value.strip()


def json_safe(value: object) -> dict:
    """Encode for `json.dumps` what it otherwise refuses.

    sd:874. `item.fields` and `item.body` are declared `TEXT`. TEXT affinity
    converts every numeric storage class to text before storing, so an INTEGER,
    a REAL, a CAST and a bound Python number all come back from these columns
    as `str`. BLOB is the one storage class affinity leaves alone: it comes
    back as `bytes`, and `json.JSONEncoder` has no rule for `bytes`. So a BLOB
    holding perfectly valid JSON raised `TypeError: Object of type bytes is not
    JSON serializable` out of `item_state` -- before any caller reached its own
    parse, and past every `except` the dashboard's `do_GET` names, so its
    reader got no response at all rather than an error page.

    A `CHECK` on the column would settle the same premise, but it would reject
    rows the store already holds and it is a migration this does not need. All
    the hash owes the bytes is that they are covered and told apart: an object
    is never what a `str` column value dumps to, so a BLOB and the same bytes
    stored as text do not share a revision. `default` runs only for a value
    `json.dumps` would otherwise refuse, so every row that hashes today keeps
    the revision it has and no open edit goes stale.

    Public, because the revision is not the only dump of this readback. Every
    mutation returns `item_state`, and the dashboard serialises that state
    straight back to the browser -- so a hash that survives a BLOB while the
    response does not just moves the `TypeError` from the GET to the POST.
    One encoding for both, and the API answers rather than dropping the
    connection. Its readers take `id`, `title`, `kind`, `status`, `repo` and
    `parked_at` off the item and never `fields` or `body`, so the shape here
    only has to be unambiguous, which an object among strings is.
    """
    if isinstance(value, bytes):
        return {"sd_db_bytes_hex": value.hex()}
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def item_state(connection: sqlite3.Connection, item: int) -> dict:
    """Readback plus a revision covering content and note resolutions.

    Timestamps have one-second precision. Hashing the actual rows avoids
    accepting two different edits made during the same second as one state.
    """
    _identifier(item, "item")
    # A deferred read snapshot does not reserve the writer lock. Without it a
    # concurrent writer can place new notes beside an item row from before its
    # edit, producing a revision which never described a real database state.
    connection.execute("SAVEPOINT sd_workflow_state")
    try:
        row = item_by_id(connection, item)
        if row is None:
            raise MissingItem(f"no item {item}")
        result = {"item": dict(row), "notes": [dict(note) for note in item_notes(connection, item)]}
    finally:
        connection.execute("RELEASE sd_workflow_state")
    content = json.dumps(result, sort_keys=True, separators=(",", ":"),
                         default=json_safe).encode("utf-8")
    result["revision"] = hashlib.sha256(content).hexdigest()
    return result


def _checked_state(connection: sqlite3.Connection, item: int, expected: str | None) -> dict:
    state = item_state(connection, item)
    if expected is not None:
        if not isinstance(expected, str):
            raise WorkflowError("expected_revision must be text")
        if expected != state["revision"]:
            raise StaleItem(f"item {item} changed; reload it before applying this change")
    return state


def schema_kinds(connection: sqlite3.Connection) -> tuple[str, ...]:
    """The kinds this store's `item.kind` CHECK allows, read from its schema.

    Read rather than listed, because the CHECK is the enforcement and the
    store may predate the migration that widened it: a kind the constant
    names and the schema lacks is refused here by name, not by an
    `IntegrityError` from inside the write.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'item'"
    ).fetchone()
    match = re.search(r"CHECK\s*\(\s*kind\s+IN\s*\(([^)]*)\)", row["sql"] if row else "")
    if match is None:
        raise WorkflowError("item.kind carries no CHECK this library can read")
    return tuple(re.findall(r"'([^']*)'", match.group(1)))


def _fields(connection: sqlite3.Connection, changes: dict) -> dict:
    if not isinstance(changes, dict) or any(not isinstance(key, str) for key in changes):
        raise WorkflowError("changes must be an object with named fields")
    unknown = set(changes) - USER_FIELDS - RECURRENCE_FIELDS
    if unknown:
        raise WorkflowError(f"fields cannot be edited here: {', '.join(sorted(unknown))}")
    result = dict(changes)
    if "title" in result:
        result["title"] = _text(result["title"], "title")
    if "body" in result:
        result["body"] = {"text": _text(result["body"], "body", blank=True)}
    if "priority" in result:
        priority = result["priority"]
        if priority is not None and (type(priority) is not int or priority not in range(1, 5)):
            raise WorkflowError("priority must be 1, 2, 3, 4, or null")
    if "due" in result and result["due"] is not None:
        due = result["due"]
        try:
            parsed = date.fromisoformat(due)
        except (ValueError, TypeError):
            raise WorkflowError("due must be a valid YYYY-MM-DD date or null") from None
        if parsed.isoformat() != due:
            raise WorkflowError("due must be a valid YYYY-MM-DD date or null")
    if "repo" in result and result["repo"] is not None:
        repo = _text(result["repo"], "repo")
        found = repos.row_for(connection, repo)
        if found is None:
            raise WorkflowError(f"repository {repo!r} is not registered; register it or omit repo")
        result["repo"] = found["path"]
    if "kind" in result:
        kind = result["kind"]
        allowed = schema_kinds(connection)
        if not isinstance(kind, str) or kind not in allowed:
            raise WorkflowError(f"no item kind {kind!r}; the kinds are {', '.join(allowed)}")
    if result.get("recurrence") is not None:
        try:
            result["recurrence"] = parse_rule(result["recurrence"]).text()
        except RecurrenceError as error:
            raise WorkflowError(str(error)) from None
    anchor = result.get("recurrence_anchor")
    if anchor is not None and anchor not in ANCHORS:
        raise WorkflowError(f"recurrence_anchor must be {' or '.join(ANCHORS)}, or null")
    return result


def _recurring(kind: str, current: dict, values: dict) -> dict:
    """Settle the rule, its anchor and `due` together, before anything is written.

    `current` is the row as it stands (empty for a new one) and `values` the
    validated changes; the result is `values` with the anchor filled in or
    cleared. A rule that could never fire is refused here rather than left
    inert: one on a row with no `due`, one on a kind `change_status` cannot
    complete, and one with no occurrence after its `due`.
    """
    values = dict(values)
    rule = values.get("recurrence", current.get("recurrence"))
    anchor = values.get("recurrence_anchor", current.get("recurrence_anchor"))
    if rule is None:
        if values.get("recurrence_anchor") is not None:
            raise WorkflowError(
                "recurrence_anchor needs a recurrence rule; set recurrence in the same write")
        if "recurrence" in values and current.get("recurrence_anchor") is not None:
            values["recurrence_anchor"] = None
        return values
    if anchor is None:
        values["recurrence_anchor"] = anchor = "schedule"
    if kind not in TASK_STATUS_KINDS:
        raise WorkflowError(
            f"{kind} items cannot recur; only {', '.join(TASK_STATUS_KINDS)} items are "
            f"completed by the task controls, so clear recurrence first")
    due = values.get("due", current.get("due"))
    if due is None:
        raise WorkflowError(
            "a recurring item needs a due date; set due, or clear recurrence in the same write")
    try:
        parsed = parse_rule(rule)
    except RecurrenceError as error:
        raise WorkflowError(str(error)) from None
    # Before the occurrence check: this refusal names its remedy, and the
    # occurrence check would otherwise answer first for some due dates.
    if anchor == "completion":
        # The completion anchor restarts the series at the completion date,
        # which supplies every part the rule omits: the day, the month, and
        # the phase INTERVAL counts from. Checking against `due` proves
        # nothing about that date, so every start in a leap cycle is tried.
        # FREQ=YEARLY;BYMONTH=2 completed on a 30th asks for February 30th.
        barren = first_barren_start(parsed)
        if barren is not None:
            raise WorkflowError(
                f"recurrence {parsed.text()} cannot be completion-anchored: completed on a "
                f"date like {barren.isoformat()} it has no next occurrence, because that date "
                f"supplies the parts the rule leaves out; use recurrence_anchor schedule, or "
                f"add the missing BYMONTH or BYMONTHDAY, with INTERVAL=1 when a BY part "
                f"is set")
    try:
        next_after(parsed, date.fromisoformat(due))
    except RecurrenceError as error:
        raise WorkflowError(str(error)) from None
    return values


def _kind_change(connection: sqlite3.Connection, row: dict, new: str, repo: str | None) -> None:
    """Refuse a kind change that would take a row from its producer or reader.

    `_fields` settled that `new` is a kind the schema allows. What is left is
    whether a hand edit may make it, which kinds a row may leave, and what the
    row carries that a reader keys on. `repo` is the row's repository after
    this edit. The rule mirrors the create side, the pack's `ADD_KINDS`.
    """
    # Imported here rather than at the top: `sources` imports this module,
    # so the name is read when the rule runs and not while it is defined.
    from .sources.vault import SOURCE as VAULT

    old = row["kind"]
    if new not in HAND_KINDS:
        raise WorkflowError(
            f"{new} items are made by their own producer, not by an edit; "
            f"the kinds an edit can set are {', '.join(HAND_KINDS)}")
    if old == "idea" and row["piece"] is not None:
        raise WorkflowError(
            f"item {row['id']} is writing piece {row['piece']!r}; `writing.list_pieces` "
            f"selects kind = 'idea' AND piece IS NOT NULL, so its kind stays idea")
    if old == "idea" and row["source"] == VAULT:
        # An idea with no `piece` is a free idea *unless* the vault put it
        # there. `sources.vault.Reader.land` lands its rows with `piece` NULL
        # and calls `upsert_item(..., kind='idea')` on every run, and
        # `upsert_item` writes back any column that differs -- so the edit
        # held until the next import and then silently reverted. The row has
        # a producer; that it carries no `piece` says only which producer.
        raise WorkflowError(
            f"item {row['id']} is a vault note; the vault import rewrites its kind on every run, "
            f"so change the note in the vault rather than the row")
    if old not in HAND_KINDS and old != "idea":
        raise WorkflowError(f"{old} items are owned by their producer; their kind cannot be changed here")
    try:
        fields = json.loads(row["fields"] or "{}")
    except (TypeError, ValueError, RecursionError):
        fields = None
    if not isinstance(fields, dict):
        raise WorkflowError(f"item {row['id']} has unreadable fields; its kind cannot be changed here")
    owned = [key for key in PRODUCED_FIELDS if fields.get(key) is not None]
    if owned:
        raise WorkflowError(
            f"item {row['id']} carries {', '.join(owned)} metadata its producer reads by kind; "
            f"its kind cannot be changed here")
    active = connection.execute(
        f"SELECT id, status FROM assignment WHERE item = ? AND status IN "
        f"({', '.join('?' for _ in ACTIVE_ASSIGNMENTS)}) LIMIT 1",
        (row["id"], *ACTIVE_ASSIGNMENTS),
    ).fetchone()
    if active:
        raise WorkflowError(f"item {row['id']} has {active['status']} assignment {active['id']}")
    if new in REPO_LESS_KINDS and repo is not None:
        raise WorkflowError(
            f"a {new} item carries no repository; clear repo in the same edit to move this row to {new}")


def capture_task(
    connection: sqlite3.Connection, *, title: str, body: str = "",
    priority: int | None = None, due: str | None = None, repo: str | None = None,
    recurrence: str | None = None, recurrence_anchor: str | None = None,
    who: str,
) -> dict:
    """Capture one local task, including its initial status history.

    `recurrence` is an RRULE in `sd_db.recurrence`'s subset and needs `due`;
    `recurrence_anchor` defaults to `schedule` when a rule is given.
    """
    who = _text(who, "who")
    with transaction(connection):
        values = _fields(connection, {
            "title": title, "body": body, "priority": priority, "due": due, "repo": repo,
            "recurrence": recurrence, "recurrence_anchor": recurrence_anchor,
        })
        values = _recurring("task", {}, values)
        item = create_item(connection, kind="task", status="planning", session=who, **values)
        return item_state(connection, item)


def register_work_item(
    connection: sqlite3.Connection, *, repo: str, path: str, title: str,
    created_at: str, branch: str | None = None, source_commit: str | None = None,
    who: str,
) -> dict:
    """Register a `docs/work` folder that exists on disk as the row that owns it.

    Retirement handed status to the database and, with it, the making of the
    row: `import docs-work` reads the file source, every repository has now
    retired that source, and the importer refuses on the first retired
    repository it meets. A folder created after the cutover therefore had
    nothing to register it and no readable status at all -- which is what
    `sd-status` reports as `status-unreadable`.

    This is that missing step and it is deliberately narrow. It creates one
    row for one folder out of what the folder and git already say, and decides
    nothing: the title and date come from the frontmatter, the commit from the
    checkout, the status is always `planning`, because an item nobody has
    started is what a new folder is.

    `branch` is the branch the work is done on -- the one meaning every reader
    of the column has (`runner.py:_item`, `configure_item`, `sd_plan.py`) --
    and it is None unless the caller can name a local head. It is never the
    remote default: `origin/main` passes the runner's shape check and names no
    branch, which is how 65 rows came to read as runnable and fail only inside
    the clone (sd:462). NULL is the honest state, and `configure_item` is what
    fills it.

    Registering twice is not an error. The caller that wants to know writes
    the `created` key; the unique index on `(source, external_id)` is what
    makes the second call safe rather than this check.
    """
    who = _text(who, "who")
    repo = _text(repo, "repo")
    path = _text(path, "path")
    title = _text(title, "title")
    created_at = _text(created_at, "created_at")

    owner = repos.row_for(connection, repo)
    if owner is None:
        raise WorkflowError(f"repository {repo!r} is not registered; `sd-db.sh repo add` first")
    # The key the row holds, so `external_id` is built from it and a second
    # machine builds the same one (sd:1439).
    repo = owner["path"]
    if owner["status_source"] != "row":
        raise WorkflowError(
            f"{repo} still lets its files own status; a row here would be a second answer"
        )

    parts = tuple(pathlib.PurePosixPath(path).parts)
    if path.startswith("/") or ".." in parts:
        raise WorkflowError("path must be relative to the repository, without `..`")
    # Exactly four components, because that is what the readers enumerate:
    # `sources.docs_work.files` keys on `len(parts) == 4`. Registering
    # `docs/work/a/nested/prd.md` under `< 4` filed a row the import and the
    # verify could never see again, so the item existed and no source ever
    # matched it.
    if len(parts) != 4 or parts[:2] != ("docs", "work") or parts[-1] != "prd.md":
        raise WorkflowError("path must be docs/work/<item>/prd.md, the file the readers key on")

    try:
        parsed = date.fromisoformat(created_at)
    except (ValueError, TypeError):
        raise WorkflowError("created_at must be a valid YYYY-MM-DD date") from None
    if parsed.isoformat() != created_at:
        raise WorkflowError("created_at must be a valid YYYY-MM-DD date")

    external_id = f"{repo}::{path}"
    with transaction(connection):
        existing = connection.execute(
            "SELECT id FROM item WHERE source = 'docs/work' AND external_id = ?",
            (external_id,),
        ).fetchone()
        if existing is not None:
            return {**item_state(connection, existing["id"]), "created": False}
        item = create_item(
            connection, kind="work", status="planning", title=title, repo=repo,
            branch=branch, path=path, source="docs/work", external_id=external_id,
            source_commit=source_commit, created_at=created_at, session=who,
        )
        return {**item_state(connection, item), "created": True}


def edit_item(
    connection: sqlite3.Connection, item: int, changes: dict, *,
    who: str, expected_revision: str | None = None,
) -> dict:
    """Edit user fields without exposing status or internal source metadata.

    `kind` is the one field a row outside `DETAIL_KINDS` may have edited,
    alone or with `repo` cleared, since a repository-less kind needs that in
    the same edit: a kind change is how such a row is reclassified, and its
    other fields keep their own editing workflow. A kind change is recorded
    as a note naming both kinds and who made it, because it rewrites what the
    row means.
    """
    who = _text(who, "who")
    with transaction(connection):
        state = _checked_state(connection, item, expected_revision)
        row = state["item"]
        reclassify = (isinstance(changes, dict) and "kind" in changes
                      and set(changes) <= {"kind", "repo"} and changes.get("repo") is None)
        if row["kind"] not in DETAIL_KINDS and not reclassify:
            raise WorkflowError(f"{row['kind']} items use their own editing workflow")
        if row["kind"] == "work":
            owner = connection.execute("SELECT status_source FROM repo WHERE path = ?", (row["repo"],)).fetchone()
            if owner is None or owner["status_source"] != "row":
                raise WorkflowError("work metadata belongs to its file owner until database cutover completes")
        values = _fields(connection, changes)
        if "body" in values and row["kind"] not in ("task", "followup"):
            raise WorkflowError("body text can only be edited here for task and followup items")
        if "repo" in values and row["kind"] == "work" and values["repo"] != row["repo"]:
            raise WorkflowError("a work item's repository is part of its source identity and cannot be reassigned here")
        # Equal submissions should not manufacture activity or history.
        changed = {
            key: value for key, value in values.items()
            if row[key] != (json.dumps(value, sort_keys=True) if key == "body" else value)
        }
        if row["kind"] not in DETAIL_KINDS and changed and "kind" not in changed:
            # A repository clear rides only on a real reclassification.
            raise WorkflowError(f"{row['kind']} items use their own editing workflow")
        if "kind" in changed:
            _kind_change(connection, row, changed["kind"], values.get("repo", row["repo"]))
        changed = _recurring(changed.get("kind", row["kind"]), row, changed)
        if changed:
            set_item_fields(connection, item, **changed)
            lines = []
            if "kind" in changed:
                lines.append(f"Changed kind {row['kind']} -> {changed['kind']} by {who}")
            fields = sorted(set(changed) - {"kind"})
            if fields:
                lines.append(f"Updated {', '.join(fields)} by {who}")
            add_note(connection, item, "comment", "\n".join(lines), session=who)
        return item_state(connection, item)


def allowed_statuses(connection: sqlite3.Connection, item: int, *, state: dict | None = None) -> list[str]:
    """The status choices a write through `change_status` accepts.

    `state` is the item's `item_state` when the caller holds it already, read
    in the same snapshot, so a caller listing many items reads each history
    once (sd:2380); it must be this item's.

    A `TASK_STATUS_KINDS` item gets `TASK_STATUSES`; database-owned work that
    is not done gets every status but `done`; every other kind, and any item
    with a queued or running assignment, gets none.

    The dashboard item screen offers exactly these choices, but only on the
    panels it renders: `task` and `TASK_STATUS_KINDS` items always, a `work`
    item only while `progress.work_controls` gives no reason, and no other
    kind. A `personal` item gets the status control without the details form
    (sd:772), because `edit_item` refuses its details. `edit_item` accepts a
    followup's since sd:809, and since sd:816 the screen renders the matching
    form instead of a hint naming the CLI's `sd task edit`.
    """
    if state is not None and state["item"]["id"] != item:
        raise WorkflowError(f"the state given is item {state['item']['id']}'s, not item {item}'s")
    row = (state if state is not None else item_state(connection, item))["item"]
    active = connection.execute(
        "SELECT 1 FROM assignment WHERE item = ? AND status IN ('queued', 'running') LIMIT 1",
        (item,),
    ).fetchone()
    if active:
        return []
    if row["kind"] in TASK_STATUS_KINDS:
        return list(TASK_STATUSES)
    if row["kind"] == "work":
        if row["status"] == "done":
            return []
        owner = connection.execute("SELECT status_source FROM repo WHERE path = ?", (row["repo"],)).fetchone()
        if owner is None or owner["status_source"] != "row":
            return []
        return [status for status in STATUSES if status != "done"]
    return []


def change_status(
    connection: sqlite3.Connection, item: int, target: str, *, who: str,
    reason: str | None = None, expected_revision: str | None = None,
) -> dict:
    """Move a task, personal, followup or active work item without bypassing delivery authority."""
    who = _text(who, "who")
    if reason is not None:
        reason = _text(reason, "reason")
    if not isinstance(target, str) or target not in STATUSES:
        raise TransitionRefused(f"no status {target!r}; the statuses are {', '.join(STATUSES)}")
    with transaction(connection):
        state = _checked_state(connection, item, expected_revision)
        row = state["item"]
        # Before the same-status no-op below, on purpose: criterion 7's clause
        # 7.19 refuses *every* status write while the runner owns the row,
        # and a write of the current status is a write. The only callers are
        # the dashboard's status route and the pack's `sd task status`; the
        # runner moves its item through `writes.transition` and never here.
        active = connection.execute(
            "SELECT id, status FROM assignment WHERE item = ? AND status IN ('queued', 'running') LIMIT 1",
            (item,),
        ).fetchone()
        if active:
            # The refusal names the row and the way out. A queued row has no
            # process, so its cancel is the library's own, `sd runner cancel`
            # (the pack's `sd assignments cancel` is the same cancel). A
            # running row with an owned `runner_run` is stopped from the
            # runner's control entry, the same verb. A running row with no
            # run -- a row written by hand, or from before the runner
            # recorded attempts -- has no supported cancel: `request_cancel`
            # and `runner_controls.control` both refuse it and the item
            # screen renders no control, so the refusal says so rather than
            # naming a verb that cannot clear it.
            #
            # Owned is `released_at IS NULL`, which is what every active-run
            # query filters on (`runner_controls.control`, `runner_exec`,
            # `runner.queue_state`). `runner_run` is durable attempt history
            # and a released attempt stays in it, so the bare existence test
            # sent an assignment whose only attempt was released to a control
            # entry that refuses it -- the third arm's case, answered with
            # the second arm's sentence.
            if active["status"] == "queued":
                way_out = f"cancel it with `sd runner cancel {active['id']}`"
            elif connection.execute("SELECT 1 FROM runner_run WHERE assignment = ? AND released_at IS NULL LIMIT 1",
                                    (active["id"],)).fetchone():
                way_out = f"stop it from the runner's control entry, `sd runner cancel {active['id']}`"
            else:
                way_out = ("it is a running assignment without a runner run, and there is no "
                           "supported cancel for it yet (sd:234)")
            raise TransitionRefused(f"item {item} has queued or running assignment {active['id']}; {way_out}")
        if row["status"] == target:
            return state
        if row["kind"] == "work":
            if row["status"] == "done":
                raise TransitionRefused("completed work is terminal; its delivery or cancellation history cannot be reopened here")
            owner = connection.execute("SELECT status_source FROM repo WHERE path = ?", (row["repo"],)).fetchone()
            if owner is None or owner["status_source"] != "row":
                raise TransitionRefused("work status belongs to its current source owner until database cutover completes")
        if row["kind"] == "work" and target == "done":
            raise TransitionRefused("work completion requires verified delivery or cancellation evidence")
        if target not in allowed_statuses(connection, item):
            raise TransitionRefused(f"{row['kind']} item {item} cannot move to {target} through task controls")
        transition(connection, item, target, who=who, reason=reason)
        if target != "done" or row.get("recurrence") is None:
            return item_state(connection, item)
        spawned, ended = _next_occurrence(connection, row, who=who)
        return {**item_state(connection, item), "next_occurrence": spawned,
                "next_occurrence_reason": ended}


def _today() -> date:
    """The completion date: the machine's calendar day, as `due` is written."""
    return date.today()


def _series_start(row: dict, rule, anchor: str, today: date) -> date:
    """The date the next occurrence is searched from: the completion day, or the item's `due` for a schedule anchor."""
    if anchor == "completion":
        return today
    if row["due"] is None:
        raise RecurrenceError(f"recurrence {rule.text()} is schedule-anchored and the item has no due date")
    return date.fromisoformat(row["due"])


def next_occurrence_due(row: dict, *, today: date | None = None) -> str | None:
    """The due date that completing `row` today gives its next occurrence, or None.

    It makes the computation `_next_occurrence` makes when the completion
    lands, and writes nothing, so a page can name the date before it asks.
    None means no rule, or no next occurrence: the completion would end the
    series.
    """
    anchor = row["recurrence_anchor"] or "schedule"
    if row["recurrence"] is None or anchor not in ANCHORS:
        return None
    try:
        rule = parse_rule(row["recurrence"])
        return next_after(rule, _series_start(row, rule, anchor, today or _today())).isoformat()
    except (RecurrenceError, ValueError):
        return None


def _next_occurrence(connection: sqlite3.Connection, row: dict, *, who: str) -> tuple[int | None, str | None]:
    """Create the row a completed recurring item owes, and move the rule to it.

    Called inside `change_status`'s transaction, after the completion. The
    new row copies what the obligation is -- kind, title, body, priority,
    repository -- and not what one instance of it carried: `branch`, `path`
    and the producer-owned `fields`. Clearing the rule on the completed row
    is what makes a reopen and a second completion spawn nothing.

    `schedule` walks the series that starts at the completed row's `due` and
    takes the first occurrence after it. `completion` restarts the series at
    the completion date and takes the first occurrence after that, so it
    drifts by design: "every three years after the last time we did it". An
    early completion therefore can produce a row due on the date of the one
    it closed.

    **The recurrence never blocks the completion.** When there is no next
    occurrence -- none within `HORIZON_YEARS`, a schedule-anchored row with
    no `due`, or a rule or anchor written around `_recurring` -- the
    completion still lands, no row is created, the rule is cleared as usual,
    and a note on the item says the recurrence ended and why. The write-time
    sweep in `_recurring` catches the common cases early; this is the
    guarantee, because a sweep over one leap cycle cannot see 2100, which is
    not a leap year. Returns the new row's id, or None and the reason.
    """
    anchor = row["recurrence_anchor"] or "schedule"
    start = None
    try:
        if anchor not in ANCHORS:
            raise RecurrenceError(f"recurrence_anchor {anchor!r} is not one of {', '.join(ANCHORS)}")
        rule = parse_rule(row["recurrence"])
        start = _series_start(row, rule, anchor, _today())
        due = next_after(rule, start).isoformat()
    except (RecurrenceError, ValueError) as error:
        searched = f", searched from {start.isoformat()}" if start is not None else ""
        reason = f"{error} ({anchor} anchor{searched})"
        set_item_fields(connection, row["id"], recurrence=None, recurrence_anchor=None)
        add_note(connection, row["id"], "comment",
                 f"Recurrence ended: {reason}. No next occurrence was created; completed by {who}",
                 session=who)
        return None, reason
    spawned = create_item(
        connection, kind=row["kind"], title=row["title"], status="planning",
        repo=row["repo"], priority=row["priority"], due=due,
        recurrence=rule.text(), recurrence_anchor=anchor, session=who,
    )
    if row["body"] is not None:
        set_item_fields(connection, spawned, body=row["body"])
    set_item_fields(connection, row["id"], recurrence=None, recurrence_anchor=None)
    add_note(connection, row["id"], "comment",
             f"Next occurrence: item {spawned}, due {due} ({rule.text()}, {anchor}-anchored) by {who}",
             session=who)
    add_note(connection, spawned, "comment",
             f"Recurs from item {row['id']}, completed by {who}", session=who)
    return spawned, None


def add_item_note(
    connection: sqlite3.Connection, item: int, *, body: str, kind: str = "comment",
    who: str, expected_revision: str | None = None,
) -> dict:
    """Add a human note; status and execution history have their own writers."""
    who = _text(who, "who")
    body = _text(body, "body")
    if kind not in NOTE_KINDS:
        raise WorkflowError(f"note kind must be one of {', '.join(NOTE_KINDS)}")
    with transaction(connection):
        _checked_state(connection, item, expected_revision)
        note = add_note(connection, item, kind, body, session=who)
        state = item_state(connection, item)
        state["note"] = next(row for row in state["notes"] if row["id"] == note)
        return state


def resolve_item_note(
    connection: sqlite3.Connection, note: int, *, who: str,
    expected_revision: str | None = None,
) -> dict:
    """Resolve a user note, retaining its text and ID for later readback."""
    _identifier(note, "note")
    _text(who, "who")
    with transaction(connection):
        row = connection.execute("SELECT * FROM note WHERE id = ?", (note,)).fetchone()
        if row is None:
            raise MissingNote(f"no note {note}")
        _checked_state(connection, row["item"], expected_revision)
        if row["kind"] not in NOTE_KINDS:
            raise WorkflowError("status and execution history cannot be resolved as user notes")
        resolve_note(connection, note)
        state = item_state(connection, row["item"])
        state["note"] = next(found for found in state["notes"] if found["id"] == note)
        return state
