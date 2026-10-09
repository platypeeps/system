"""Decisions: the questions that wait on the operator, answered with one button per option (sd:3012).

A decision is a `question` note whose body names its choices, one line each:

    Which store holds quick notes?
    Option: A pack store kind
    Option: Workflow database rows

`sd task note N --kind question --body "$(printf 'Q\\nOption: A\\nOption: B')"` writes one. A line that starts
`Option:` is a choice and every other line is the question. A repeated choice counts once, and a note with fewer
than two choices is a plain question, not a decision. An old prose "Decision needed" comment stays a comment: it
lists nothing here and gets no buttons, because prose options are not parsed.

- `document` is `/api/decisions`: every unresolved decision on an open item Tasks lists (`reads.backlog_items`,
  `reads.brief_notes`), the item's priority first, then the oldest, each with the revision `workflow.item_state` gives.
- `answer` is `/api/decisions/answer`, posted with the item, the note, the option and that revision: the CLI's two
  verbs in one transaction under the revision.
  `workflow.add_item_note` writes the ruling, a `decision` note `Ruling on note #<note>: <option>`, and
  `workflow.resolve_item_note` resolves the question. A stale revision writes nothing (`StaleItem`, a 409).
  `sd task show <item>` prints the ruling and the resolved question.

Every read and write is an existing library call, so the installed sd_db serves it without a new install.
"""

from __future__ import annotations

import re

from sd_db import reads, workflow

__all__ = ["answer", "document", "parse", "request"]

OPTION = re.compile(r"^[ \t]*Option:[ \t]*(.*?)[ \t]*$")


def parse(body: str) -> tuple[str, list[str]]:
    """The question text and the choices, in the order written."""
    question, options = [], []
    for line in body.splitlines():
        found = OPTION.match(line)
        if found is None:
            question.append(line)
        elif found[1] and found[1] not in options:
            options.append(found[1])
    return "\n".join(question).strip(), options


def document(connection, *, now: str | None = None) -> dict:
    open_items = {row["id"]: row for row in reads.backlog_items(connection, include_done_days=0, now=now)
                  if row["status"] != "done"}
    found, revisions = [], {}
    for row in reads.brief_notes(connection, list(open_items)):
        question, options = parse(row["body"])
        if row["kind"] != "question" or len(options) < 2:
            continue
        if row["item"] not in revisions:
            revisions[row["item"]] = workflow.item_state(connection, row["item"])["revision"]
        item = open_items[row["item"]]
        found.append({"note": row["id"], "item": row["item"], "title": item["title"], "repo": item["repo"],
                      "priority": item["priority"], "asked": row["timestamp"], "question": question,
                      "options": options, "revision": revisions[row["item"]]})
    found.sort(key=lambda d: (d["priority"] is None, d["priority"] or 0, d["asked"], d["note"]))
    return {"decisions": found}


def answer(connection, item: int, note: int, option: str, *, expected_revision: str, who: str) -> dict:
    """Record `option` as the ruling on decision `note` of `item` and resolve it; the item's state, with the ruling."""
    with workflow.transaction(connection):
        state = workflow.item_state(connection, item)
        # The revision first: a page that read before another answer landed rereads (409) rather than reading a refusal.
        if state["revision"] != expected_revision:
            raise workflow.StaleItem(f"item {item} changed; reload it before applying this change")
        row = next((found for found in state["notes"] if found["id"] == note), None)
        if row is None:
            raise workflow.MissingNote(f"item {item} has no note {note}")
        options = parse(row["body"])[1]
        if row["kind"] != "question" or len(options) < 2:
            raise workflow.WorkflowError(f"note {note} is not a decision: it needs kind question and two Option: lines")
        if row["resolved_at"] is not None:
            raise workflow.WorkflowError(f"note {note} was answered at {row['resolved_at']}")
        if option not in options:
            raise workflow.WorkflowError(f"note {note} offers no option {option!r}")
        ruled = workflow.add_item_note(connection, item, kind="decision", who=who,
                                       body=f"Ruling on note #{note}: {option}", expected_revision=expected_revision)
        state = workflow.resolve_item_note(connection, note, who=who, expected_revision=ruled["revision"])
        state["ruling"] = ruled["note"]
        return state


def request(payload: dict):
    """The POST's checks, before the server opens a writer; the write as `action_route` returns one."""
    ids = ("item", "note")
    if (set(payload) != {*ids, "revision", "option"}
            or any(type(payload[key]) is not int or not 1 <= payload[key] <= 9223372036854775807 for key in ids)
            or not isinstance(payload["revision"], str) or not re.fullmatch(r"[a-f0-9]{64}", payload["revision"])
            or not isinstance(payload["option"], str) or not payload["option"].strip()):
        raise ValueError("Send the decision's item and note, the option the page showed and the item revision it read.")
    return lambda connection: answer(connection, payload["item"], payload["note"], payload["option"],
                                     expected_revision=payload["revision"], who="dashboard")
