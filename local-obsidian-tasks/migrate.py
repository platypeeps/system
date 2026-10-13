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
  sd_db's parser refuses, and sd keeps no series start: `INTERVAL` and the
  implied day count from the due date. So that part is dropped and the rest
  goes through the pack's `sd_db.recurrence`, but only when the due date is
  an occurrence of the DTSTART series and sd's chain from it lands on the
  same dates; a task postponed off its series is refused. A rule the library
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


#: The keys `plan` reads. Each must decode exactly, or the task is refused:
#: an item written from a misread value keeps its reference, and a rerun
#: would never import the task again. Every other key is left unread.
SCALAR_KEYS = ("title", "status", "priority", "scheduled", "due", "recurrence",
               "recurrence_anchor", "completedDate", "archived")
LIST_KEYS = ("complete_instances", "skipped_instances", "tags")
READ_KEYS = SCALAR_KEYS + LIST_KEYS


def frontmatter(text: str) -> dict | None:
    """The read keys of the YAML block, or None when the file has none.

    TaskNotes writes flat frontmatter: `key: value` scalars, inline `[a, b]`
    lists and indented `- item` lists. That is all this decodes. A read key
    whose value uses any other YAML syntax raises `Refused` naming the key:
    escapes in quotes, block or folded scalars, anchors, aliases, tags, flow
    maps, comments, continuation lines and nested maps.
    """
    m = re.match(r"(?s)^---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text)
    if not m:
        return None
    fields: dict = {}
    block: set = set()  # read keys whose value is an indented `- item` list
    key = None
    for number, line in enumerate(m.group(1).splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            if key not in READ_KEYS:
                continue
            item = re.match(r"^\s+-(?:\s+(.*))?$", line)
            if item is None or (key not in block and fields[key] != ""):
                raise Refused(f"{key}: an indented line the importer does not decode")
            if key not in block:
                block.add(key)
                fields[key] = []
            value = (item.group(1) or "").strip()
            if re.match(r"^[^'\"]*:(\s|$)", value):
                raise Refused(f"{key}: a map inside a list")
            fields[key].append(_scalar(key, value))
            continue
        pair = re.match(r"^([A-Za-z_][\w-]*):(?:[ \t]+(.*))?$", line)
        if not pair:
            raise Refused(f"frontmatter line {number}: not a plain key the importer decodes")
        key, value = pair.group(1), (pair.group(2) or "").strip()
        if key not in READ_KEYS:
            fields[key] = value
            continue
        if key in fields:
            raise Refused(f"{key}: given twice")
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            fields[key] = [_scalar(key, v) for v in inner.split(",")] if inner else []
        else:
            fields[key] = _scalar(key, value)
    return fields


def _scalar(key: str, value: str) -> str:
    """One plain or simply quoted value, exactly, or `Refused` naming the key."""
    value = value.strip()
    if not value:
        return ""
    if value[0] in "'\"":
        inner = value[1:-1]
        if len(value) < 2 or value[-1] != value[0] or value[0] in inner or (value[0] == '"' and "\\" in inner):
            raise Refused(f"{key}: a quoted value with an escape or no closing quote")
        return inner
    if value[0] in "|>&*!{}[]%@`,?":
        raise Refused(f"{key}: YAML syntax the importer does not decode "
                      "(a block or folded scalar, anchor, alias, tag or flow value)")
    if " #" in value or ": " in value or value.endswith(":") or "\t#" in value:
        raise Refused(f"{key}: a comment or map syntax in a plain value")
    return value


#: A TaskNotes date or datetime, read whole: `YYYY-MM-DD`, then an optional time and zone.
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")


def _date(key: str, value) -> str | None:
    """The `YYYY-MM-DD` of a TaskNotes date or datetime, None when empty, else `Refused`."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not DATE.match(value):
        raise Refused(f"{key}: not a date")
    try:
        return datetime.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        raise Refused(f"{key}: not a date") from None


def _dates(key: str, value) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise Refused(f"{key}: not a list")
    return sorted({_date(key, v) for v in value if v != ""} - {None})


#: How many occurrences `_drifts` compares past the due date.
DRIFT_STEPS = 200


def _drifts(recurrence, rule, start: str, due: str) -> bool:
    """Whether sd's chain from `due` leaves the vault's series from `DTSTART`.

    The pinned pack keeps no `DTSTART`: each completion searches from the due
    date it closed (`workflow._series_start`). The vault's series survives
    only when `due` is one of its occurrences and every later step lands on
    the same date. A search that runs out of occurrences counts as drift.
    """
    first, ours = datetime.date.fromisoformat(start), datetime.date.fromisoformat(due)
    try:
        if ours < first or (ours != first and recurrence.next_after(
                rule, first, ours - datetime.timedelta(days=1)) != ours):
            return True
        theirs = ours
        for _ in range(DRIFT_STEPS):
            ours, theirs = recurrence.next_after(rule, ours), recurrence.next_after(rule, first, theirs)
            if ours != theirs:
                return True
    except recurrence.RecurrenceError:
        return True
    return False


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

    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("not UTF-8") from None
    fm = frontmatter(text)
    if fm is None:
        raise Refused("no frontmatter")
    for key in SCALAR_KEYS:
        if isinstance(fm.get(key), list):
            raise Refused(f"{key}: a list where one value belongs")
    relative = path.relative_to(vault).as_posix()
    raw_status = str(fm.get("status") or "open").strip().lower()
    if raw_status not in STATUS:
        raise Refused(f"status {raw_status!r} has no sd status")
    tags = fm.get("tags") or []
    tags = tags if isinstance(tags, list) else [tags]
    archived = str(fm.get("archived") or "false").lower()
    if archived not in ("true", "false"):
        raise Refused("archived: neither true nor false")
    status = STATUS[raw_status]
    if archived == "true" or "archived" in tags:
        status = "cancelled"
    raw_priority = str(fm.get("priority") or "").strip().lower()
    if raw_priority not in PRIORITY:
        raise Refused(f"priority {raw_priority!r} has no sd priority")
    scheduled, deadline = _date("scheduled", fm.get("scheduled")), _date("due", fm.get("due"))
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
        starts = [p for p in parts if p.strip().upper().startswith("DTSTART")]
        start = None
        if starts:
            found = re.match(r"^DTSTART:(\d{4})(\d{2})(\d{2})(?:T\d{6}Z?)?$", starts[0].strip(), re.I)
            if len(starts) > 1 or found is None:
                raise Refused("recurrence: a DTSTART the importer does not decode")
            start = _date("recurrence", "-".join(found.groups()))
        kept = ";".join(p for p in parts if not p.strip().upper().startswith("DTSTART"))
        try:
            parsed = recurrence.parse(kept)
        except recurrence.RecurrenceError as error:
            raise Refused(f"rule refused: {error}") from None
        raw_anchor = str(fm.get("recurrence_anchor") or "").strip().lower()
        if raw_anchor not in ANCHOR:
            raise Refused(f"recurrence_anchor {raw_anchor!r} has no sd anchor")
        done = _dates("complete_instances", fm.get("complete_instances"))
        skipped = _dates("skipped_instances", fm.get("skipped_instances"))
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
            if anchor == "schedule" and start and _drifts(recurrence, parsed, start, due):
                raise Refused(f"recurrence: due date {due} is off the series DTSTART {start} starts, "
                              "and sd keeps no DTSTART, so it would restart the series there")
    completed = _date("completedDate", fm.get("completedDate"))
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
    m = re.match(r"(?s)^---\r?\n.*?\r?\n---[ \t]*(?:\r?\n|$)", text)
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
    refused_tasks: list = []
    failed: collections.Counter = collections.Counter()
    present = history = 0
    who = getpass.getuser()
    try:
        for path in files:
            try:
                task = plan(vault, path)
            except Refused as why:
                refused[str(why)] += 1
                refused_tasks.append((path.relative_to(vault).as_posix(), str(why)))
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

    # Counts, and the path of each refused task; a title never reaches this output.
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
    # The one place a file name is printed: the operator fixes each of these
    # in the vault, and a rerun imports it, since a refusal writes no reference.
    for relative, why in refused_tasks:
        print(f"  refused {relative}: {why}")
    if args.apply:
        print(f"failed: {sum(failed.values())}")
        for why, n in sorted(failed.items()):
            print(f"  {why}: {n}")
        return 1 if refused or failed else 0
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
