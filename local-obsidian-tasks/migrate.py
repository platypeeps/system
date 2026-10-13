#!/usr/bin/env python3
"""Move the vault's TaskNotes tasks into sd items (sd:3460).

The operator's ruling (sd:3460 note 15807): tracking lives in sd, and the
vault is a publication target only. `obsidian-tasks.sh migrate` runs this.
Without `--apply` it is a dry run that prints counts by shape and writes
nothing; with it, each task becomes one sd `task` with no checkout.

What maps to what:

- `status`: none and open are `planning`, in-progress is `in_progress`, done
  is `done`. An archived task (status, flag or tag) lands `done` with the
  cancelled receipt `sd task cancel` writes (operator ruling on sd:3460).
  Any other status is refused and counted.
- `priority`: high is 1, normal 2, low 3; none or missing leaves it unset.
- `scheduled`, else `due`, is the item's due date. A recurring task needs
  one; with neither, its `DTSTART` date is used.
- `recurrence`: the vault's rules start with `DTSTART:YYYYMMDD;`, which
  sd_db's parser refuses. That part is dropped and the rest goes through the
  pack's `sd_db.recurrence`, so `INTERVAL` counts from the due date: the
  `scheduled` occurrence TaskNotes already advanced to. A rule the library
  refuses is a refused task, never an item that silently stops recurring.
- `recurrence_anchor`: scheduled is `schedule`, completion `completion`.
- History: `complete_instances` and `skipped_instances` go into one note on
  the item, with the vault path, the series start and a `due` deadline that
  differs from `scheduled`. The chain of done rows sd keeps starts here; past
  occurrences are not replayed as rows, which would flood the done view.
  A done or archived recurring task imports closed and without its rule:
  its series ended.

Each task is one transaction through the pack library, as `sd task add`
writes it, so a run stopped at any point leaves every task either imported
whole or not at all. The item carries the reference `obsidian:<vault path>`,
the same `task-ref` source and external id that `sd task add --ref` writes;
a task whose reference is already in the database is skipped, whatever
became of its item since, so a second run creates nothing. The vault is only
read.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import getpass
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# The pack owns sd_db (sd:3279): `SD_PACK_ROOT` names its checkout, else the
# home clone, as for local-agent-meter.
sys.path.insert(0, str(Path(os.environ.get("SD_PACK_ROOT")
                            or Path.home() / "repos/platypeeps/sd-ai-command-pack").expanduser() / "lib"))

#: The source `sd task add --ref` files a reference under (the pack's
#: `bin/sd_work.py` and `sd_db.review` name it `REF_SOURCE`).
REF_SOURCE = "task-ref"
REF_PREFIX = "obsidian:"

#: `cancelled` is no sd status: `write` closes such a task through `cancel_work`.
STATUS = {"none": "planning", "open": "planning", "in-progress": "in_progress", "done": "done",
          "archived": "cancelled"}
CANCEL_REASON = "archived in the Obsidian vault"
PRIORITY = {"high": 1, "normal": 2, "low": 3, "none": None, "": None}
ANCHOR = {"scheduled": "schedule", "completion": "completion", "": "schedule"}


class Refused(Exception):
    """A task this importer will not turn into an item; the message is its count key."""


def frontmatter(text: str) -> dict | None:
    """The YAML block's scalars and lists, or None when the file has none.

    TaskNotes writes flat frontmatter: `key: value` scalars, inline `[a, b]`
    lists and indented `- item` lists. That is all this reads; a nested map
    reads as an empty value, which no field below uses.
    """
    m = re.match(r"(?s)^---\r?\n(.*?)\r?\n---", text)
    if not m:
        return None
    fields: dict = {}
    key = None
    for line in m.group(1).splitlines():
        item = re.match(r"^\s+-\s*(.*)$", line)
        if item and key is not None:
            if not isinstance(fields[key], list):
                fields[key] = []
            fields[key].append(_scalar(item.group(1)))
            continue
        pair = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not pair:
            continue
        key, value = pair.group(1), pair.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            fields[key] = [_scalar(v) for v in inner.split(",")] if inner else []
        else:
            fields[key] = _scalar(value)
    return fields


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _date(value) -> str | None:
    """The `YYYY-MM-DD` a TaskNotes date or datetime starts with, else None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _dates(value) -> list[str]:
    values = value if isinstance(value, list) else []
    return sorted({d for d in map(_date, values) if d})


@dataclass
class Plan:
    ref: str
    title: str
    body: str
    status: str
    priority: int | None
    due: str | None
    rule: str | None
    anchor: str | None
    note: str
    shape: str
    history: int = 0


def plan(vault: Path, path: Path) -> Plan:
    """What one task file becomes, or `Refused` naming why it becomes nothing."""
    from sd_db import recurrence

    text = path.read_text(encoding="utf-8", errors="replace")
    fm = frontmatter(text)
    if fm is None:
        raise Refused("no frontmatter")
    relative = path.relative_to(vault).as_posix()
    raw_status = str(fm.get("status") or "open").strip().lower()
    if raw_status not in STATUS:
        raise Refused(f"status {raw_status!r} has no sd status")
    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
    status = STATUS[raw_status]
    if str(fm.get("archived", "")).lower() == "true" or "archived" in tags:
        status = "cancelled"
    raw_priority = str(fm.get("priority") or "").strip().lower()
    if raw_priority not in PRIORITY:
        raise Refused(f"priority {raw_priority!r} has no sd priority")
    scheduled, deadline = _date(fm.get("scheduled")), _date(fm.get("due"))
    due = scheduled or deadline
    lines = [f"Imported from Obsidian TaskNotes: {relative}."]
    if scheduled and deadline and scheduled != deadline:
        lines.append(f"The vault's due date was {deadline}; sd's due follows scheduled, {scheduled}.")

    rule = anchor = None
    raw_rule = str(fm.get("recurrence") or "").strip()
    history = 0
    if raw_rule:
        body = raw_rule[len("RRULE:"):] if raw_rule.upper().startswith("RRULE:") else raw_rule
        parts = [p for p in body.split(";") if p.strip()]
        start = next((p.split(":", 1)[1] for p in parts if p.upper().startswith("DTSTART:")), "")
        start = _date(f"{start[:4]}-{start[4:6]}-{start[6:8]}") if re.match(r"^\d{8}", start) else None
        kept = ";".join(p for p in parts if not p.upper().startswith("DTSTART"))
        try:
            parsed = recurrence.parse(kept)
        except recurrence.RecurrenceError as error:
            raise Refused(f"rule refused: {error}") from None
        raw_anchor = str(fm.get("recurrence_anchor") or "").strip().lower()
        if raw_anchor not in ANCHOR:
            raise Refused(f"recurrence_anchor {raw_anchor!r} has no sd anchor")
        done = _dates(fm.get("complete_instances"))
        skipped = _dates(fm.get("skipped_instances"))
        history = len(done) + len(skipped)
        lines.append(f"The vault's rule: {raw_rule}; anchor {raw_anchor or 'scheduled'}.")
        if done:
            lines.append(f"Completed occurrences ({len(done)}): {', '.join(done)}.")
        if skipped:
            lines.append(f"Skipped occurrences ({len(skipped)}): {', '.join(skipped)}.")
        if status == "done":
            lines.append("Done in the vault, so its series ended and the item does not recur.")
        elif status == "cancelled":
            lines.append("Archived in the vault, so its series ended and the item does not recur.")
        else:
            due = due or start
            if due is None:
                raise Refused("recurring with no scheduled, due or DTSTART date")
            rule, anchor = parsed.text(), ANCHOR[raw_anchor]
            # The checks `sd task add --recur` makes, so a dry run counts what
            # `--apply` would refuse.
            if anchor == "completion" and recurrence.first_barren_start(parsed) is not None:
                raise Refused(f"rule refused: {rule} cannot be completion-anchored")
            try:
                recurrence.next_after(parsed, datetime.date.fromisoformat(due))
            except recurrence.RecurrenceError as error:
                raise Refused(f"rule refused: {error}") from None
    completed = _date(fm.get("completedDate"))
    if status == "done" and completed:
        lines.append(f"Completed in the vault on {completed}.")
    if status == "cancelled" and not raw_rule:
        lines.append("Archived in the vault, so the item is closed with a cancelled receipt.")

    if rule:
        freq = re.search(r"FREQ=(\w+)", rule).group(1)
        by = "".join(f"+{p}" for p in ("BYMONTH", "BYMONTHDAY", "BYDAY") if f"{p}=" in rule)
        shape = f"{status} recurring {freq}{by} {anchor}"
    else:
        shape = f"{status} one-off" + (" (series ended)" if raw_rule else "")
    title = str(fm.get("title") or path.stem).strip() or path.stem
    return Plan(ref=REF_PREFIX + relative, title=title, body=text[len(_block(text)):].strip(),
                status=status, priority=PRIORITY[raw_priority], due=due, rule=rule, anchor=anchor,
                note="\n".join(lines), shape=shape, history=history)


def _block(text: str) -> str:
    m = re.match(r"(?s)^---\r?\n.*?\r?\n---[^\n]*\n?", text)
    return m.group(0) if m else ""


def write(connection, task: Plan, who: str) -> int:
    """Create one item for `task` in one transaction, as `sd task add` would."""
    from sd_db import progress, workflow, writes

    with workflow.transaction(connection):
        state = workflow.capture_task(
            connection, title=task.title, body=task.body, priority=task.priority, due=task.due,
            repo=None, recurrence=task.rule, recurrence_anchor=task.anchor, who=who)
        item = state["item"]["id"]
        writes.set_item_fields(connection, item, source=REF_SOURCE, external_id=task.ref)
        if task.status == "cancelled":
            progress.cancel_work(connection, item, reason=CANCEL_REASON, who=who, guard=progress.task_guard)
        elif task.status != "planning":
            workflow.change_status(connection, item, task.status, who=who)
        workflow.add_item_note(connection, item, body=task.note, who=who)
        return item


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-tasks.sh migrate", description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="create the items; without it, a dry run")
    args = parser.parse_args(argv)
    vault = Path(os.environ.get("OBSIDIAN_VAULT") or "~/Documents/Obsidian Vault").expanduser()
    tasks_dir = vault / (os.environ.get("OBSIDIAN_TASKS_SUBDIR") or "TaskNotes/Tasks")
    if not tasks_dir.is_dir():
        print(f"obsidian-tasks.sh migrate: no task folder at {tasks_dir}", file=sys.stderr)
        return 1
    try:
        import sd_db
        connection = sd_db.connect(write=args.apply)
    except Exception as error:
        # An unread database is an unknown, not "nothing imported yet".
        print(f"obsidian-tasks.sh migrate: cannot open the sd database ({type(error).__name__}: {error}); "
              "nothing was counted or written", file=sys.stderr)
        return 1

    from sd_db import writes

    files = sorted(tasks_dir.glob("*.md"))
    shapes: collections.Counter = collections.Counter()
    refused: collections.Counter = collections.Counter()
    failed: collections.Counter = collections.Counter()
    present = history = 0
    who = getpass.getuser()
    try:
        for path in files:
            try:
                task = plan(vault, path)
            except Refused as why:
                refused[str(why)] += 1
                continue
            if writes.item_by_external(connection, REF_SOURCE, task.ref) is not None:
                present += 1
                continue
            shapes[task.shape] += 1
            history += task.history
            if args.apply:
                try:
                    write(connection, task, who)
                except Exception as error:
                    shapes[task.shape] -= 1
                    failed[f"{type(error).__name__}: {error}"] += 1
    finally:
        connection.close()

    # Counts only: a task's title or file name never reaches this output.
    verb = "created" if args.apply else "would create"
    print(f"obsidian-tasks.sh migrate: {'apply' if args.apply else 'dry run, nothing written; --apply writes'}")
    print(f"task files: {len(files)}")
    print(f"already imported: {present}")
    print(f"{verb}: {sum(shapes.values())}")
    for shape, n in sorted(shapes.items()):
        if n:
            print(f"  {shape}: {n}")
    print(f"  history entries kept in notes: {history}")
    print(f"refused: {sum(refused.values())}")
    for why, n in sorted(refused.items()):
        print(f"  {why}: {n}")
    if args.apply:
        print(f"failed: {sum(failed.values())}")
        for why, n in sorted(failed.items()):
            print(f"  {why}: {n}")
        return 1 if refused or failed else 0
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
