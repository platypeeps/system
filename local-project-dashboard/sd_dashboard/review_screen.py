"""Review (sd:3709): the labelling queue's page, one question at a time (design docs/work/2026-10-12-review-queue).

A batch is a task filed under `review:<queue>:<batch>` by `sd-db review file`; its questions are question notes and a
label is the Decisions ruling. Every read and write here is `sd_db.review`'s, the code `sd-db review` runs, so the page
and the CLI verb share one path (operator ruling Q3); the page adds no store and no second writer.

- `document` is `/api/review`: each open batch, nightly suggestions first and then by age, with the revision read
  before the batch, so a change between the two makes the page stale (409), never newer than what it shows; and each
  open question. A blind or sealed batch sends the question text and options only: no subject and no evidence.
  A batch whose notes do not read as one is listed with its reason, never dropped.
- `rule_request` is `/api/review/rule`: `review.rule`, under the batch revision the page read.
- `withdraw_request` is `/api/review/withdraw`: `review.withdraw`, the undo.
- `export` is `/api/review/<item>/export`: `review.export` without a key, the lines `sd-db review export` writes.
  A blind or sealed batch exports only once no question is open, so the page never shows a subject while labelling.
"""

from __future__ import annotations

import re

from sd_db import reads, workflow

__all__ = ["BLIND", "NIGHTLY", "document", "export", "rule_request", "withdraw_request"]

#: The queue the nightly review job files (`sd-db review apply nightly`); its suggestions come first.
NIGHTLY = "nightly"
#: Modes whose questions show text and options only.
BLIND = ("blind", "sealed")
MAX_ID = 9223372036854775807


def _rows(connection):
    """The open batch items, oldest first: the Tasks row set (`reads.backlog_items`) under `review`'s ref, as Today counts."""
    from sd_db import review

    rows = [row for row in reads.backlog_items(connection, include_done_days=0)
            if row["status"] != "done" and row["source"] == review.REF_SOURCE
            and (row["external_id"] or "").startswith("review:")]
    return sorted(rows, key=lambda row: (row["created_at"], row["id"]))


def document(connection) -> dict:
    from sd_db import review

    batches, questions = [], []
    for row in _rows(connection):
        item = row["id"]
        revision = workflow.item_state(connection, item)["revision"]
        try:
            found = review.read(connection, item)
        except review.ReviewError as problem:
            batches.append({"item": item, "title": row["title"], "created": row["created_at"], "nightly": False,
                            "error": str(problem)})
            continue
        if found["closed"] is not None:
            continue
        header = found["header"]
        blind = header["mode"] in BLIND
        batches.append({"item": item, "title": row["title"], "created": row["created_at"], "ref": found["ref"],
                        "queue": header["queue"], "name": header["batch"], "mode": header["mode"],
                        "source": header["source"], "nightly": header["queue"] == NIGHTLY, "revision": revision,
                        "questions": len(found["questions"]), "open": found["open"], "labels": found["labels"],
                        "error": None})
        for question in found["questions"]:
            if not question["open"]:
                continue
            shown = {"item": item, "note": question["note"], "text": question["text"],
                     "options": question["options"], "asked": question["asked_at"]}
            if not blind:
                shown.update(subject=question["subject"], evidence=question["evidence"])
            questions.append(shown)
    order = {batch["item"]: number for number, batch in
             enumerate(sorted(batches, key=lambda batch: not batch["nightly"]))}
    batches.sort(key=lambda batch: order[batch["item"]])
    questions.sort(key=lambda question: (order[question["item"]], question["note"]))
    # The rail badge (sd:3446): every open question in a batch that reads. The page keeps its own badge current between
    # reads as its labels land, from the same batches' `open`.
    from .v2.pages import attention

    found = attention(["caution"] * sum(batch.get("open") or 0 for batch in batches), {"caution": "questions open"})
    return {"batches": batches, "questions": questions, "attention": found}


def _ids(payload: dict, names: tuple[str, ...]) -> bool:
    return all(type(payload[name]) is int and 1 <= payload[name] <= MAX_ID for name in names)


def _revision(payload: dict) -> bool:
    return isinstance(payload["revision"], str) and re.fullmatch(r"[a-f0-9]{64}", payload["revision"]) is not None


def rule_request(payload: dict):
    """The label POST's checks, before the server opens a writer; the write as `action_route` returns one."""
    if (set(payload) != {"item", "note", "option", "revision"} or not _ids(payload, ("item", "note"))
            or not _revision(payload) or not isinstance(payload["option"], str) or not payload["option"]):
        raise ValueError("Send the batch item, the question note, the option the page showed and the revision it read.")

    def write(connection):
        from sd_db import review

        state = review.rule(connection, payload["item"], payload["note"], payload["option"],
                            expected_revision=payload["revision"], who="dashboard")
        return {"item": payload["item"], "note": payload["note"], "revision": state["revision"],
                "ruling": state["ruling"]["id"]}

    return write


def withdraw_request(payload: dict):
    """The undo POST's checks; the write withdraws the ruling and reopens its question."""
    if set(payload) != {"item", "ruling", "revision"} or not _ids(payload, ("item", "ruling")) or not _revision(payload):
        raise ValueError("Send the batch item, the ruling note to withdraw and the revision the label returned.")

    def write(connection):
        from sd_db import review

        state = review.withdraw(connection, payload["item"], payload["ruling"],
                                expected_revision=payload["revision"], who="dashboard")
        return {"item": payload["item"], "revision": state["revision"], "withdrawal": state["withdrawal"]["id"]}

    return write


def export(connection, item: int):
    """The batch's export lines, or `(status, error)` when it is no batch or a blind one still has open questions."""
    from sd_db import review

    try:
        found = review.read(connection, item)
        if found["header"]["mode"] in BLIND and found["open"]:
            return 409, {"error": f"sd:{item} is {found['header']['mode']} and has {found['open']} open; "
                                  "its export waits until every question is ruled"}
        lines = review.export(connection, item, who="dashboard")
    except workflow.MissingItem as problem:
        return 404, {"error": str(problem)}
    except review.ReviewError as problem:
        return 400, {"error": str(problem)}
    header = lines[0]
    return {"name": f"review-{header['queue']}-{header['name']}.jsonl", "lines": lines}
