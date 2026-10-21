"""Decisions: the questions that wait on the operator, answered with one button per option (sd:3012) or in words (sd:5064).

A decision is a `question` note whose body names its choices, one line each:

    Which store holds quick notes?
    Option: A pack store kind
    Option: Workflow database rows

`sd task note N "$(printf 'Q\\nOption: A\\nOption: B')" --kind question` writes one. A line that starts
`Option:` is a choice and every other line is the question. A repeated choice counts once. A question with fewer
than two choices is a free-text question (sd:5064): it lists here too, and the page answers it with a text box in
place of buttons. An old prose "Decision needed" comment stays a comment: it lists nothing here, because only a
`question` note asks.

- `document` is `/api/decisions`: every unresolved decision on an open item Tasks lists (`reads.backlog_items`,
  `reads.brief_notes`), the item's priority first, then the oldest, each with the revision `workflow.item_state` gives,
  read for every item in one `reads.revisions`.
  `recommended` is the index of the option the note recommends, or null (sd:5306): one option that ends
  "(Recommended)" or starts "(Recommended)" or "Recommended:", else a line "Recommendation: <text>" that names an
  option. A conditional "(Recommended only if …)" and two marked options recommend nothing.
  A question on a review batch (sd:3709, a task under a `review:` ref) is Review's: it is counted in `review`, and
  Today draws one line that links there in place of its rows.
- `answer` is `/api/decisions/answer`, posted with the item, the note, the option and that revision: the CLI's two
  verbs in one transaction under the revision, as `sd task note N "<ruling>" --kind decision --answers <note>` writes.
  `workflow.add_item_note` writes the ruling, a `decision` note `Ruling on note #<note>: <option>`, and
  `workflow.resolve_item_note` resolves the question. A question with two choices or more takes only one of them;
  a free-text question takes any words. A stale revision writes nothing (`StaleItem`, a 409).
  `sd task show <item>` prints the ruling and the resolved question.

Every read and write is an existing library call, so the installed sd_db serves it without a new install.
"""

from __future__ import annotations

import re

from sd_db import reads, workflow

from .tasks_screen import is_scheduled

__all__ = ["answer", "document", "parse", "recommended", "request"]

OPTION = re.compile(r"^[ \t]*Option:[ \t]*(.*?)[ \t]*$")
MARKED = re.compile(r"\(recommended\)$|^(?:\(recommended\)|recommended:)", re.IGNORECASE)
ADVICE = re.compile(r"^[ \t]*Recommendation:[ \t]*(.*?)[ \t.]*$", re.IGNORECASE)
#: A review batch's ref, as `sd_db.review` files it (`REF_SOURCE`, `review:<queue>:<batch>`); named here so Today reads
#: without that module, and a test holds the two equal.
BATCH_SOURCE, BATCH_PREFIX = "task-ref", "review:"
#: The longest free-text answer the page posts: a ruling is a line or a short paragraph, not a document.
ANSWER_MAX = 4000


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


def recommended(body: str, options: list[str]) -> int | None:
    """The index of the recommended option, or None; the posted text stays the option as written."""
    if len(options) < 2:
        return None
    marked = [i for i, option in enumerate(options) if MARKED.search(option)]
    if marked:
        return marked[0] if len(marked) == 1 else None
    for line in body.splitlines():
        found = ADVICE.match(line)
        if found:
            said = found[1].casefold() + " "
            # The longest option the line starts with, whole words, so "Rows" does not take "Rows and a cache".
            named = [i for i, option in enumerate(map(str.casefold, options))
                     if said.startswith(option) and not said[len(option)].isalnum()]
            return max(named, key=lambda i: len(options[i])) if named else None
    return None


def document(connection, *, now: str | None = None) -> dict:
    open_items = {row["id"]: row for row in reads.backlog_items(connection, include_done_days=0, now=now)
                  if row["status"] != "done" and not is_scheduled(row)}
    found, batched = [], 0
    for row in reads.brief_notes(connection, list(open_items)):
        question, options = parse(row["body"])
        if row["kind"] != "question":
            continue
        item = open_items[row["item"]]
        if item["source"] == BATCH_SOURCE and (item["external_id"] or "").startswith(BATCH_PREFIX):
            batched += 1
            continue
        found.append({"note": row["id"], "item": row["item"], "title": item["title"], "repo": item["repo"],
                      "priority": item["priority"], "asked": row["timestamp"], "question": question,
                      "options": options, "recommended": recommended(row["body"], options)})
    # One batched revision read for every item asking, not an `item_state` per item (sd:3531).
    revisions = reads.revisions(connection, [decision["item"] for decision in found])
    for decision in found:
        decision["revision"] = revisions[decision["item"]]
    found.sort(key=lambda d: (d["priority"] is None, d["priority"] or 0, d["asked"], d["note"]))
    return {"decisions": found, "review": batched}


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
        if row["kind"] != "question":
            raise workflow.WorkflowError(f"note {note} is not a question: only a question note takes a ruling")
        if row["resolved_at"] is not None:
            raise workflow.WorkflowError(f"note {note} was answered at {row['resolved_at']}")
        # Two choices or more: one of them. Fewer: a free-text question, answered in words (sd:5064).
        if len(options) >= 2 and option not in options:
            raise workflow.WorkflowError(f"note {note} offers no option {option!r}")
        option = option.strip()
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
            or not isinstance(payload["option"], str) or not payload["option"].strip() or len(payload["option"]) > ANSWER_MAX):
        raise ValueError("Send the decision's item and note, the option the page showed (or an answer of at most "
                         f"{ANSWER_MAX} characters) and the item revision it read.")
    return lambda connection: answer(connection, payload["item"], payload["note"], payload["option"],
                                     expected_revision=payload["revision"], who="dashboard")
