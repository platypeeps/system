"""Tasks: the rows and the Details reading behind the default UI's Tasks page (sd:2124).

The page is `v2/tasks.html`; it holds no rows. It reads two JSON documents,
both built here from the reads v1 already makes, so the port adds no second
query of the store:

- `/api/tasks` is `document`: every row `reads.backlog_items` returns for v1
  /backlog (unparked open items, and `done` for the week), each with the
  revision a write sends back and the statuses `workflow.allowed_statuses`
  accepts for it, so the board refuses a move for the reason the library
  would, before it posts. Each row carries its `reads.age_bucket` key and the
  document names the buckets (`ages`), so the page's age filter is the one
  Operations' histogram counts with (sd:2589). Each row also carries whether `workflow.edit_item`
  takes its fields (`_edit_capability`), so Edit, P2, recurrence and a
  matrix drop are off, with the library's reason, where the edit would fail.
- `/api/tasks/<id>` is `details`: what `sd task show <id> --json` prints
  (`item`, `notes`, `revision`, from `workflow.item_state`), split into the
  status history and the other notes as v1's item page splits them, plus the
  item's assignments (`reads.item_assignments`, with the cancel capability
  and revision `sd assignments cancel` checks), a work
  item's relink and cancel availability (`progress.work_controls`) and its
  external context (`reads.item_shadow` and `progress.tracker_freshness`,
  v1's "External context" block), or `null` when the item has none.
- `from_backlog` is where a v1 `/backlog` address goes since sd:2356 retired
  it: the same `/tasks` view with the query v1 read, so a bookmark or an old
  link keeps its filters.

Every write the page makes goes through a route `server.action_route`
already answered for v1: status, edit (priority, due, recurrence), note
resolve, work relink and cancel. Nothing here
writes.
"""

from __future__ import annotations

from datetime import datetime

from sd_db import reads, repos, workflow

__all__ = ["details", "document"]

#: The board's columns, in order: `workflow.TASK_STATUSES`. Keys 1-5 follow it.
STATUSES = workflow.TASK_STATUSES


def _assignment_states(connection, *, now: str) -> dict[int, str]:
    """Item id -> `running` or `queued`, for an item with a live assignment; running wins.

    The lanes `reads.runner_board` gives v1's runner board, so no query here.
    """
    board = reads.runner_board(connection, now=now)
    live = {row["item"]: "queued" for row in board["queued"] if row["item"] is not None}
    live.update({row["item"]: "running" for row in board["running"] if row["item"] is not None})
    return live


def _edit_capability(connection, row) -> dict:
    """Whether `workflow.edit_item` takes this row's fields, with the reason it gives when it refuses.

    The two refusals `edit_item` makes for a field edit before it reads the changes: a kind outside
    `workflow.DETAIL_KINDS`, and a work item whose repository still lets its files own status (`repo.status_source` is
    not `row`; the schema default is `file`). Without this, a file-owned work row showed Edit, P2 and the matrix drop,
    and every save failed (review, PR #46). `test_v2_tasks` checks it against `edit_item` itself.
    """
    reason = None
    if row["kind"] not in workflow.DETAIL_KINDS:
        reason = f"{row['kind']} items use their own editing workflow"
    elif row["kind"] == "work":
        # `repos.row_for`, as `register_work_item` reads it: the dashboard issues no SQL of its own (test_criterion_12).
        owner = repos.row_for(connection, row["repo"]) if row["repo"] else None
        if owner is None or owner["status_source"] != "row":
            reason = "work metadata belongs to its file owner until database cutover completes"
    return {"allowed": reason is None, "reason": reason}


def document(connection, *, now: str) -> dict:
    """The Tasks rows: one object per `reads.backlog_items` row, in its order.

    One read snapshot holds every read below (`operations_screen._one_snapshot`). Each was its own before, so an edit
    committed between the row read and its `item_state` paired the old fields with the new revision, and a write chosen
    from the old fields passed the revision check (review, PR #46).
    """
    from .operations_screen import _one_snapshot

    with _one_snapshot(connection):
        return _document(connection, now=now)


def _document(connection, *, now: str) -> dict:
    from .screens import _repo_labels

    rows = reads.backlog_items(connection, now=now)
    label = _repo_labels(row["repo"] for row in rows)
    live = _assignment_states(connection, now=now)
    out = []
    for row in rows:
        state = workflow.item_state(connection, row["id"])
        out.append({
            "id": row["id"], "title": row["title"], "kind": row["kind"], "status": row["status"],
            "priority": row["priority"], "due": row["due"],
            "repo": label(row["repo"]) if row["repo"] else None, "repo_path": row["repo"],
            "recurrence": row["recurrence"], "recurrence_anchor": state["item"]["recurrence_anchor"],
            "assignment": live.get(row["id"]),
            # The due date a completion today gives the next occurrence, for the confirm to name before it writes.
            "next_due": workflow.next_occurrence_due(state["item"]) if row["recurrence"] else None,
            "status_since": row["status_since"], "age": reads.age_bucket(row, now=now), "revision": state["revision"],
            # The matrix's urgency is `reads.is_urgent`, decided here. Without the due-date rule it is
            # `urgent_otherwise`, which the page keeps for a due edit made before the rows are read again.
            "urgent": reads.is_urgent(row, now=now),
            "urgent_otherwise": reads.is_urgent({**dict(row), "due": None}, now=now),
            # The row's own `item_state`, so each row reads its history once (sd:2380).
            "allowed": workflow.allowed_statuses(connection, row["id"], state=state),
            "edit": _edit_capability(connection, row),
        })
    # The histogram's buckets with nothing counted: their keys and the labels Operations draws on its bars.
    ages = [{"key": bucket.key, "label": bucket.label} for bucket in reads.age_histogram([], now=now)]
    return {"read": now, "statuses": list(STATUSES), "ages": ages, "rows": out}


def _note(note) -> dict:
    return {"id": note["id"], "kind": note["kind"], "at": note["timestamp"], "body": note["body"],
            "session": note["session"], "resolved": note["resolved_at"]}


def _external(connection, row, *, now: str) -> dict | None:
    """v1's External context (`screens.external_context`) as data: None when the item has no tracker reference."""
    from sd_db import progress

    shadow = reads.item_shadow(connection, row["id"])
    if shadow is None and row["source"] not in ("github", "jira"):
        return None
    url = shadow["url"] if shadow else row["external_id"]
    if not url:
        return None
    tracker = shadow["tracker"] if shadow else row["source"]
    freshness = progress.tracker_freshness(connection, tracker=tracker,
                                          now=datetime.fromisoformat(now.replace("Z", "+00:00")))
    return {"tracker": tracker, "url": url, "state": shadow["state"] if shadow else None,
            "last_seen": shadow["last_seen"] if shadow else None,
            "freshness": {"state": freshness["state"], "last_success_at": freshness["last_success_at"],
                          "reason": freshness["reason"]}}


def details(connection, item: int, *, now: str) -> dict:
    """One item's Details sections. Raises `workflow.MissingItem` for an id with no item."""
    from sd_db import operations, progress

    state = workflow.item_state(connection, item)
    row = reads.item_by_id(connection, item)
    assignments = []
    for assignment in reads.item_assignments(connection, item):
        held = operations.assignment_state(connection, assignment["id"])
        assignments.append({
            "id": assignment["id"], "role": assignment["role"], "provider": assignment["provider"],
            "status": assignment["status"], "started": assignment["started"], "ended": assignment["ended"],
            "usd": assignment["usd"], "estimated": bool(assignment["estimated"]),
            "revision": held["revision"], "cancel": held["capabilities"]["cancel"],
        })
    return {
        "read": now, "item": state["item"], "revision": state["revision"],
        "history": [_note(note) for note in state["notes"] if note["kind"] == "status_change"],
        "notes": [_note(note) for note in state["notes"] if note["kind"] != "status_change"],
        "assignments": assignments, "allowed": workflow.allowed_statuses(connection, item, state=state),
        "external": _external(connection, row, now=now),
        # `sd work relink` and `sd work cancel` availability, as the mutation checks it under its lock
        # (`progress.work_controls`), for a work item; None for any other kind (sd:2200).
        "work": progress.work_controls(connection, item) if state["item"]["kind"] == "work" else None,
    }


#: The views v1 /backlog had; Tasks has the same three. v1 opened on the list, Tasks on the board.
BACKLOG_VIEWS = ("list", "board", "matrix")


def from_backlog(connection, parameters, *, now: str) -> str:
    """The `/tasks` address for a v1 `/backlog` query (sd:2356).

    Tasks reads the names v1 read (sd:2589, sd:2590): view, kind, status, age, active, q, page and skill. Its repo filter
    is the row's label, not the path v1 took, so a path becomes the label `_document` gives it. v1's run picks
    (`sel`) do not carry over: a run from a link is not a run the operator chose.
    """
    from urllib.parse import quote, urlencode

    from .screens import _repo_labels

    def one(key: str) -> str:
        return (parameters.get(key) or [""])[0]

    view = one("view")
    query = {"view": view if view in BACKLOG_VIEWS else "list"}
    for key in ("kind", "status", "age"):
        if one(key):
            query[key] = one(key)
    repo = one("repo")
    if repo == reads.NO_REPO_TOKEN:
        query["repo"] = "no repo"
    elif repo:
        label = _repo_labels(row["repo"] for row in reads.backlog_items(connection, now=now))(repo)
        query["repo"] = repo if label == "—" else label
    if one("active") == "1":
        query["active"] = "1"
    if one("q"):
        query["q"] = one("q")[:200]
    if one("page").isdigit() and int(one("page")) > 1:
        query["page"] = str(int(one("page")))
    if one("skill"):
        query["skill"] = one("skill")
    # %20, not +, for a space: every URL decoder reads %20 as a space; decodeURIComponent keeps a + as a plus.
    return "/tasks?" + urlencode(query, quote_via=quote)
