"""Tasks: the rows and the Details reading behind the default UI's Tasks page (sd:2124).

The page is `v2/tasks.html`; it holds no rows. It reads two JSON documents,
both built here from the reads v1 already makes, so the port adds no second
query of the store:

- `/api/tasks` is `document`: every row `reads.backlog_items` returns for v1
  /backlog (unparked open items, and `done` for the week), each with the
  revision a write sends back and the statuses `workflow.allowed_statuses`
  accepts for it, so the board refuses a move for the reason the library
  would, before it posts. Each row also carries whether `workflow.edit_item`
  takes its fields (`_edit_capability`), so Edit, P2, recurrence and a
  matrix drop are off, with the library's reason, where the edit would fail.
- `/api/tasks/<id>` is `details`: what `sd task show <id> --json` prints
  (`item`, `notes`, `revision`, from `workflow.item_state`), split into the
  status history and the other notes as v1's item page splits them, plus the
  item's assignments (`reads.item_assignments`, with the cancel capability
  and queue revision `sd assignments get` and `sd runner` use) and its
  external context (`reads.item_shadow` and `progress.tracker_freshness`,
  v1's "External context" block), or `null` when the item has none.

Every write the page makes goes through a route `server.action_route`
already answered for v1: status, edit (priority, due, recurrence), note
resolve, runner requeue and cancel. Nothing here writes.
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
            "status_since": row["status_since"], "revision": state["revision"],
            # The matrix's urgency is `reads.is_urgent`, decided here. Without the due-date rule it is
            # `urgent_otherwise`, which the page keeps for a due edit made before the rows are read again.
            "urgent": reads.is_urgent(row, now=now),
            "urgent_otherwise": reads.is_urgent({**dict(row), "due": None}, now=now),
            # The row's own `item_state`, so each row reads its history once (sd:2380).
            "allowed": workflow.allowed_statuses(connection, row["id"], state=state),
            "edit": _edit_capability(connection, row),
        })
    return {"read": now, "statuses": list(STATUSES), "rows": out}


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


def _runner_capabilities(queue: dict) -> dict:
    """What `sd runner requeue` and `sd runner cancel` accept for this assignment, with the reason when they refuse.

    The same rules `runner_screen.assignment_controls` uses to offer v1's buttons, from `runner.requeue` and
    `runner_controls.control`.
    """
    held, status = queue["run"], queue["status"]
    if queue["role"] == "exec":
        requeue = "finite execution authorizations are single-use"
    elif status not in ("blocked", "cancelled"):
        requeue = f"the assignment is {status}, not blocked or cancelled"
    elif held and not held.get("released_at"):
        requeue = "its runner lease is not released yet"
    else:
        requeue = None
    if held and held.get("cancel_requested"):
        cancel = "a stop is already requested; the runner still owns cleanup"
    elif status == "queued" or (status == "running" and held):
        cancel = None
    elif status == "running":
        cancel = "a legacy running assignment has no owned runner attempt to stop"
    else:
        cancel = f"the assignment is {status}, not queued or running"
    return {"requeue": {"allowed": requeue is None, "reason": requeue},
            "cancel": {"allowed": cancel is None, "reason": cancel}}


def details(connection, item: int, *, now: str) -> dict:
    """One item's Details sections. Raises `workflow.MissingItem` for an id with no item."""
    from sd_db import operations, runner, runner_controls

    state = workflow.item_state(connection, item)
    row = reads.item_by_id(connection, item)
    assignments = []
    for assignment in reads.item_assignments(connection, item):
        cancel = operations.assignment_state(connection, assignment["id"])["capabilities"]["cancel"]
        queue = runner.queue_state(connection, assignment["id"])
        assignments.append({
            "id": assignment["id"], "role": assignment["role"], "provider": assignment["provider"],
            "status": assignment["status"], "started": assignment["started"], "ended": assignment["ended"],
            "usd": assignment["usd"], "estimated": bool(assignment["estimated"]),
            "revision": queue["revision"], "cancel": cancel, "runner": _runner_capabilities(queue),
        })
    ready = runner_controls.readiness(connection, item)
    return {
        "read": now, "item": state["item"], "revision": state["revision"],
        "history": [_note(note) for note in state["notes"] if note["kind"] == "status_change"],
        "notes": [_note(note) for note in state["notes"] if note["kind"] != "status_change"],
        "assignments": assignments, "allowed": workflow.allowed_statuses(connection, item, state=state),
        "external": _external(connection, row, now=now),
        # `sd run` readiness, as `/api/run` checks it (`runner_controls.readiness`): read here, for one item, rather
        # than for every row, because it reads the item's assignments and leases.
        "run": {"allowed": ready["allowed"], "reason": ready["reason"]},
    }
