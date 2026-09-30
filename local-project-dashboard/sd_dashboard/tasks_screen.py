"""Tasks: the rows and the Details reading behind the default UI's Tasks page (sd:2124).

The page is `v2/tasks.html`; it holds no rows. It reads two JSON documents,
both built here from the reads v1 already makes, so the port adds no second
query of the store:

- `/api/tasks` is `document`: every row `reads.backlog_items` returns for v1
  /backlog (unparked open items, and `done` for the week), each with the
  revision a write sends back and the statuses `workflow.allowed_statuses`
  accepts for it, so the board refuses a move for the reason the library
  would, before it posts.
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

from sd_db import reads, workflow

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


def document(connection, *, now: str) -> dict:
    """The Tasks rows: one object per `reads.backlog_items` row, in its order."""
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
            "recurrence": row["recurrence"], "assignment": live.get(row["id"]),
            "status_since": row["status_since"], "revision": state["revision"],
            "allowed": workflow.allowed_statuses(connection, row["id"]),
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


def details(connection, item: int, *, now: str) -> dict:
    """One item's Details sections. Raises `workflow.MissingItem` for an id with no item."""
    from sd_db import operations, runner

    state = workflow.item_state(connection, item)
    row = reads.item_by_id(connection, item)
    assignments = []
    for assignment in reads.item_assignments(connection, item):
        cancel = operations.assignment_state(connection, assignment["id"])["capabilities"]["cancel"]
        assignments.append({
            "id": assignment["id"], "role": assignment["role"], "provider": assignment["provider"],
            "status": assignment["status"], "started": assignment["started"], "ended": assignment["ended"],
            "usd": assignment["usd"], "estimated": bool(assignment["estimated"]),
            "revision": runner.queue_state(connection, assignment["id"])["revision"], "cancel": cancel,
        })
    return {
        "read": now, "item": state["item"], "revision": state["revision"],
        "history": [_note(note) for note in state["notes"] if note["kind"] == "status_change"],
        "notes": [_note(note) for note in state["notes"] if note["kind"] != "status_change"],
        "assignments": assignments, "allowed": workflow.allowed_statuses(connection, item),
        "external": _external(connection, row, now=now),
    }
