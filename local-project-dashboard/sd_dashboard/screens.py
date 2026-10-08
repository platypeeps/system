"""Today and Item, with all reads and writes owned by sd_db.

Every row comes from `sd_db.reads`, including the Today query shared with
`sd today`. Controls render the shared workflow's allowed transitions and
current revision; they contain no independent status rules or SQL. Tasks
took the Backlog, and its run selection, in sd:2622.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

sdpaths = None  # bound at the end of this module (sd:1439)
from sd_db import reads, workflow

from . import controls
from .charts import timeline_svg
from .listing import Column, Listing
from .markup import Markup, join, markdown, tag
from .pages import command_reference, page

__all__ = ["item", "today"]


def _cell(key: str):
    return lambda row: row[key]


def _repo_labels(paths):
    """Shortest distinct suffix; full paths remain in filters and item details."""
    parts = {path: Path(path).parts for path in set(paths) if path}
    labels = {}
    for path, components in parts.items():
        for depth in range(1, len(components) + 1):
            suffix = components[-depth:]
            if sum(other[-depth:] == suffix for other in parts.values()) == 1:
                labels[path] = path if suffix[0] == "/" else "/".join(suffix)
                break
        else:
            labels[path] = path
    return lambda path: labels.get(path, "—")


def _due(row, now):
    value = row["due"]
    if not value:
        return "—"
    try:
        date = datetime.fromisoformat(value)
    except ValueError:
        return value
    label = date.strftime("%b %-d")
    if date.year != int(now[:4]):
        label += f", {date.year}"
    return tag("time", label, datetime=value, aria_label=value)


def _status(row) -> Markup:
    return tag("span", row["status"].replace("_", " "), class_=f"status status-{row['status']}")


def readable_time(stamp):
    if not stamp:
        return Markup("—")
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        fallback = moment.strftime("%b %-d, %Y · %-I:%M %p %Z")
    except (ValueError, AttributeError):
        return tag("span", stamp)
    return tag("time", fallback, datetime=stamp, data_local_time=True)


def _history_text(body):
    return re.sub(r"\b(in_progress|ready_to_send)\b", lambda match: match[0].replace("_", " "),
                  body).replace(" -> ", " → ")


def _days(row, now: str) -> str:
    days = reads.days_since_status(row, now=now)
    return "—" if days is None else f"{days}d"


# --------------------------------------------------------------------------
# Today
# --------------------------------------------------------------------------


def _runner_board(board, now: str) -> Markup:
    holders = board.get("_holders", {})
    lanes = []
    for lane in reads.BOARD_LANES:
        rows = board.get(lane, [])
        cards = []
        for row in rows:
            waits = reads.waiting_on(row, holders)
            cards.append(
                tag(
                    "article",
                    tag("h3", tag("a", row["item_title"] or f"item {row['item']}",
                                  href=f"/item/{row['item']}")),
                    tag("p", row["item_repo"] or "(no repository)", class_="card-repo"),
                    tag("p", f"{row['role']} · {row['provider'] or 'unassigned'}",
                        class_="card-role"),
                    tag("p", f"waiting on {waits}", class_="card-wait") if waits else Markup(""),
                    class_="card",
                )
            )
        lanes.append(
            tag(
                "section",
                tag("h3", f"{lane} ({len(rows)})", class_="lane-heading"),
                join(cards) if cards else tag("p", "—", class_="lane-empty"),
                class_=f"lane lane-{lane}",
            )
        )
    return tag("div", join(lanes), class_="board board-runner")


def today(connection, *, now: str, parameters) -> str:
    """Items due or in progress, and the runner's day.

    The list is `reads.today_items` in the order the library returned it. This
    screen does not sort: criterion 4 asserts that `sd today` and this page
    list the same ids *in the same order*, and a second sort here is exactly
    how that assertion starts failing.
    """
    rows = reads.today_items(connection, now=now)
    repo_label = _repo_labels(row["repo"] for row in rows)
    query, page_number, selected = Listing.read_query(parameters)
    listing = Listing(
        name="today",
        path="/classic/today",
        query=query,
        page_number=page_number,
        selected=selected,
        rows=rows,
        row_id=lambda row: str(row["id"]),
        row_href=lambda row: f"/item/{row['id']}",
        columns=[
            Column("title", "Item", _cell("title"), css="column-item"),
            Column("status", "Status", _status, text=_cell("status"), css="column-meta column-status"),
            Column("days", "In status", lambda row: _days(row, now), css="column-meta"),
            Column("repo", "Repository", lambda row: repo_label(row["repo"]), text=_cell("repo"), css="column-meta column-repo"),
            Column("due", "Due", lambda row: _due(row, now), text=_cell("due"), css="column-meta", hide_empty=True),
            Column("kind", "Kind", _cell("kind"), hidden=True),
        ],
        empty="Nothing is due and nothing is in progress.",
        empty_next="Choose the next task from Tasks, or capture something new.",
    )

    followups = reads.open_followups(connection)
    followup_list = Listing(
        name="followups",
        path="/classic/today",
        rows=followups,
        row_href=lambda row: f"/item/{row['item']}",
        columns=[
            Column("body", "Followup", _cell("body")),
            Column("item", "Item", _cell("item_title")),
            Column("when", "Written", lambda row: row["timestamp"][:10]),
        ],
        empty="No open followups.",
    )

    board = reads.runner_board(connection, now=now)
    running = sum(len(board.get(lane, [])) for lane in ("queued", "running"))
    timeline_rows = reads.day_timeline(connection, now=now)

    board_section: list[object] = []
    if running:
        board_section = [
            tag("h2", "The runner"),
            command_reference("sd today"),
            _runner_board(board, now),
        ]
    if timeline_rows:
        board_section.extend(
            [
                tag("h2", "The day"),
                timeline_svg(
                    timeline_rows, now=now, href=lambda row: f"/item/{row['item']}"
                ),
            ]
        )

    from .contribution_screen import preview
    from .now_screen import now_panel

    return page(
        "Today",
        "today",
        tag("p", "A clear place to pick up, move forward, and finish.", class_="lead"),
        # First, before capture: Now is what the fleet is asking for, and
        # the pack put it above its own tabs for that reason. Its rows come
        # by `/api/now` after the page is up (`now_screen` says why).
        now_panel(),
        controls.capture(connection),
        preview(connection),
        tag("h2", "Your work today"),
        tag("p", "Due and in-progress items. Active assignments appear in the runner board below.", class_="hint"),
        command_reference("sd today"),
        listing.render(),
        tag("h2", "Open followups"),
        followup_list.render(),
        tag("p", tag("a", "Choose the next task from Tasks", href="/tasks", class_="button-link")),
        join(board_section),
        cli_equivalents=False,
    )


# --------------------------------------------------------------------------
# Item
# --------------------------------------------------------------------------


def artifact(row, *, document=None) -> Markup:
    """The item's artifact, read from git and rendered through the subset.

    Read at `source_commit` where the row has one, because that is the commit
    the row was landed from and the working copy may have moved since. What
    comes back is never markup: it goes through `markdown()`, which escapes
    first and adds only the tags it wrote.
    """
    repo, path = row["repo"], row["path"]
    if row["kind"] == "idea" and row["piece"]:
        if document is not None:
            document = re.sub(r"\A---\r?\n.*?\r?\n---(?:\r?\n|$)", "", document, count=1, flags=re.S)
        return (markdown(document) if document is not None else
                tag("p", "The current draft could not be read. See the review checks above.", class_="artifact-missing"))
    if row["kind"] in (*workflow.HAND_KINDS, "report", "skill-review"):
        # `followup` is here since sd:816. `workflow._fields` stores a followup's
        # body as `{"text": ...}` exactly as it stores a task's, and every
        # followup in the store carries that shape, but this decode named three
        # kinds and not four -- so a followup showed its stored JSON, braces and
        # escaped quotes and all, where its text belongs.
        #
        # The other hand kinds are here for the same reason (PR #332 review).
        # `personal`, `work-idea` and `personal-idea` are created through
        # `capture_task` and reclassified from a task by the item screen's
        # kind select, and both leave the `{"text": ...}` body in place; a
        # task that became one of them showed its braces the moment it did.
        # The set is `HAND_KINDS` rather than five names so a hand kind added
        # later decodes here without a second edit.
        try:
            body = json.loads(row["body"] or "{}")
        except ValueError:
            # sd:870, the twin of sd:775's hole. `item.body` is plain `TEXT`
            # with no `json_valid` check on it, so a row this cannot parse is a
            # row the store permits. Unguarded, `json.JSONDecodeError` -- a
            # `ValueError` -- raised through `screens.item` and past `do_GET`,
            # which catches `MissingItem`, `NotFound` and the database errors
            # and not this, so the reader got no response at all. Fail closed
            # the way sd:775 settled: render the page, say plainly why the text
            # is missing, and name the command that shows the stored bytes.
            return tag("p", "This item's stored body is not valid JSON, so its text cannot be "
                            "shown here. Read what is stored with ",
                       tag("code", f"sd store item {row['id']}"), ".", class_="artifact-missing")
        return markdown(body.get("text", "") if isinstance(body, dict) else "")
    if not repo or not path:
        return markdown(row["body"])
    commit = row["source_commit"] or "HEAD"
    try:
        done = subprocess.run(
            ["git", "-C", str(sdpaths.disk(repo)), "show", f"{commit}:{path}"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return markdown(row["body"])
    if done.returncode != 0:
        return tag(
            "p",
            f"The artifact at {path} could not be read from {commit}.",
            class_="artifact-missing",
        )
    return markdown(done.stdout)


def item(connection, item_id: int, *, now: str, parameters) -> str | None:
    row = reads.item_by_id(connection, item_id)
    if row is None:
        return None
    writing_state = None
    if row["kind"] == "idea" and row["piece"]:
        from sd_db import writing

        writing_state = writing.piece_state(connection, item_id)
    notes = reads.item_notes(connection, item_id)
    changes = reads.status_changes(connection, item_id)
    assignments = reads.item_assignments(connection, item_id)
    shadow = reads.item_shadow(connection, item_id)
    revision = workflow.item_state(connection, item_id)["revision"]
    query, page_number, selected = Listing.read_query(parameters)
    from .palette import note_summary
    execution_notes = {note["id"]: note_summary(note) for note in notes if note["kind"] == "exec"}

    def note_body(note, *, text=False):
        if note["kind"] == "exec":
            return execution_notes[note["id"]][1 if text else 0]
        return _history_text(note["body"]) if note["kind"] == "status_change" else note["body"]

    note_list = Listing(
        name="notes",
        path=f"/item/{item_id}",
        query=query,
        page_number=page_number,
        selected=selected,
        rows=notes,
        columns=[
            Column("when", "When", lambda note: readable_time(note["timestamp"])),
            Column("kind", "Kind", lambda note: note["kind"].replace("_", " ")),
            Column("body", "Note", note_body, text=lambda note: note_body(note, text=True)),
            Column("state", "State",
                   lambda note: controls.note_resolution(note, revision),
                   text=lambda note: "resolved" if note["resolved_at"] else "open"),
            Column("session", "Session", lambda note: (note["session"] or "—"), hidden=True),
        ],
        empty="No notes on this item.",
    )

    # Criterion 7's clause at `prd.md:1303-1307`: the item screen lists the
    # status changes, in order, each with old, new and time. They are notes,
    # so they are in the list above too; this is the reading of them, because
    # "the history of this item's status" is a question with its own answer.
    history = tag(
        "ol",
        join(
            tag(
                "li",
                readable_time(note["timestamp"]),
                " ",
                tag("span", _history_text(note["body"]), class_="status-change"),
                class_="history-entry",
                data_status_change=note["id"],
            )
            for note in changes
        ),
        class_="history",
    ) if changes else tag("p", "This item has never changed status.", class_="history")

    assignment_rows = tag(
        "table",
        tag(
            "thead",
            tag("tr", join(tag("th", label, scope="col") for label in
                           ("Assignment", "Role", "Provider", "Status", "Started",
                            "Ended", "Cost"))),
        ),
        tag(
            "tbody",
            join(
                tag(
                    "tr",
                    tag("td", assignment["id"]),
                    tag("td", assignment["role"]),
                    tag("td", assignment["provider"] or "—"),
                    tag("td", assignment["status"].replace("_", " ")),
                    tag("td", readable_time(assignment["started"])),
                    tag("td", readable_time(assignment["ended"])),
                    tag("td", f"${assignment['usd']:,.2f}"
                        + (" (estimated)" if assignment["estimated"] else "")),
                )
                for assignment in assignments
            ),
        ),
        class_="listing-table",
    ) if assignments else tag("p", "No assignments.", class_="empty")

    shadow_block = external_context(connection, row, shadow, now=now)
    kind_label = "writing" if writing_state else row["kind"]
    status_label = row["stage"] if writing_state else row["status"]

    from .operations_screen import assignment_cancel
    from .reports_screen import report_controls
    from .skills_screen import review_controls

    return page(
        row["title"],
        "item",
        *[
            command_reference(f"sd store item {item_id}"),
            completion_summary(row),
            controls.writing_controls(connection, row, state=writing_state),
            controls.item_controls(connection, item_id),
            controls.progress_controls(connection, item_id),
            join(assignment_cancel(connection, one["id"]) for one in assignments),
            review_controls(connection, row, revision),
            report_controls(connection, row, revision),
            tag("h2", "Status history"),
            history,
            tag("h2", "Artifact"),
            tag("article", artifact(row, document=writing_state["writing"]["document"] if writing_state else None), class_="artifact"),
            tag("h2", "Notes"),
            controls.note_capture(connection, item_id),
            note_list.render(),
            tag("h2", "Assignments"),
            assignment_rows,
            shadow_block,
        ],
        subtitle=f"#{item_id} · {kind_label} · {status_label.replace('_', ' ')} · {row['repo'] or 'no repository'}",
    )


def completion_summary(row):
    from sd_db import progress

    if row["kind"] != "work" or row["status"] != "done":
        return Markup("")
    receipt = progress.completion_record(row)
    if not receipt:
        return tag("p", "Completed; no delivery or cancellation receipt is recorded here.", class_="hint")
    if receipt["outcome"] == "cancelled":
        return tag("section", tag("h2", "Cancelled"), tag("p", receipt["reason"]),
                   readable_time(receipt["at"]), class_="control-panel")
    return tag("section", tag("h2", "Delivered"),
               tag("p", "Delivery is recorded against a verified commit."),
               readable_time(receipt["at"]), class_="control-panel")


def external_context(connection, row, shadow, *, now):
    """External state is dated context beside the local item's own status."""
    from sd_db import progress

    if shadow is None and row["source"] not in ("github", "jira"):
        return Markup("")
    url = shadow["url"] if shadow else row["external_id"]
    if not url:
        return Markup("")
    tracker = shadow["tracker"] if shadow else row["source"]
    freshness = progress.tracker_freshness(connection, tracker=tracker,
                                          now=datetime.fromisoformat(now.replace("Z", "+00:00")))
    try:
        parsed = urlsplit(url)
        safe = parsed.scheme in ("https", "http") and bool(parsed.netloc)
    except ValueError:
        safe = False
    link = (tag("a", url, href=url, rel="noopener noreferrer") if safe else tag("span", url))
    return tag("section", tag("h2", "External context"),
        tag("p", "Local progress is tracked here. External tracker state is context and does not hold this task open.", class_="hint"),
        tag("dl",
            tag("div", tag("dt", "Tracker"), tag("dd", tracker)),
            tag("div", tag("dt", "External state"), tag("dd", shadow["state"] or "Unknown" if shadow else "Not in the local snapshot")),
            tag("div", tag("dt", "Reference"), tag("dd", link)),
            tag("div", tag("dt", "Last seen"), tag("dd", readable_time(shadow["last_seen"]) if shadow else "Never")),
            tag("div", tag("dt", "Sync health"), tag("dd", freshness["state"])),
            tag("div", tag("dt", "Last successful sync"), tag("dd", readable_time(freshness["last_success_at"]) if freshness["last_success_at"] else "Never")),
            class_="shadow"),
        tag("p", freshness["reason"], class_="hint") if freshness["reason"] else Markup(""),
        class_="secondary-panel")


# A library before schema 14 (sd:1439) has no `sd_db.paths` and stores every
# repository path absolute. The services run from this checkout, so a pull can
# reach them before the library moves; against that library a key is the path.
try:
    from sd_db import paths as sdpaths
except ImportError:
    from types import SimpleNamespace as _Namespace
    sdpaths = _Namespace(
        key=lambda value: value,
        disk=lambda value: Path(value).expanduser(),
        same=lambda left, right: left is not None and right is not None and (
            Path(left).expanduser().resolve() == Path(right).expanduser().resolve()),
    )
