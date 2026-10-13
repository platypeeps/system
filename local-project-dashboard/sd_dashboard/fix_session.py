"""Fix (sd:3456): one click on a task or a problem report starts a claude session for it in the operator's herdr session.

The operator ruled on 2026-10-12 (a note on sd:3456): Fix on a task starts a builder session for that task, and Fix on
a report (kind `report`) starts a debug and fix session for that report. Tasks, Reports and Today post
`/api/items/<id>/fix` with the item revision they read; `request` checks the body before the server opens a writer, and
`start` runs in the write, in three steps:

1. The item as it is now. Its revision must be the one the page read (`StaleItem`, a 409), so a second click from the
   same page, after the first one's note landed, starts nothing. It must be open, and its checkout a directory: a
   task's checkout is its repo, and a report's is its repo, else this checkout, which holds `local-cron-jobs`, since no
   report names its job's repo.
2. `local-herdr/herdr.sh launch <checkout> sd-<id> <prompt>`: a new herdr workspace with claude in it, given the
   prompt as typed text. The call is an argv, never a shell line, and the prompt is fixed text and the integer id,
   never the item's title. herdr missing or its session down answers 503 with herdr.sh's own words, which name
   `herdr.sh start`; nothing falls back to a hidden process. A later step that fails says what it left: a workspace at
   its shell prompt, or claude without its prompt. No answer within `TIMEOUT` is a 504 that says whether a session
   started is not known.
3. A comment note on the item names the session, the pane and the checkout. A note that cannot be written after a
   launch answers 503 naming the pane, so the operator knows a session runs without its record.

`LAUNCHING` holds one launch at a time, so two clicks that cross pass step 1 one after the other: the second then reads
the first one's note in the revision and is refused.
"""

from __future__ import annotations

import re
import shlex
import sqlite3
import subprocess
import threading
from pathlib import Path

from sd_db import paths, workflow
from sd_db.errors import SdDbError

__all__ = ["CHECKOUT", "HERDR", "NOTE", "TIMEOUT", "prompt", "request", "start"]

#: This repository's checkout: a report that names no repo is fixed here.
CHECKOUT = Path(__file__).resolve().parents[2]
HERDR = CHECKOUT / "local-herdr" / "herdr.sh"
#: `herdr agent start` waits up to 30 s for claude to be ready; the workspace and the prompt take a few more.
TIMEOUT = 90
LAUNCHING = threading.Lock()

BUILD = ("Build sd:{id}. Follow the command pack's builder workflow: read `sd task show {id}`, work in a worktree "
         "outside ~/repos, commit the change with a pull-request body, and enqueue it through the lane "
         "(`sd-ship lane enqueue`).")
#: The note step 3 writes; Today's Working now (sd:3466, `active_sessions`) reads the pane back from it.
NOTE = "Fix: started a {session} session (claude) in herdr workspace sd-{item}, pane {pane}, at {where}."
DEBUG = ("Use the sd-debug skill on report sd:{id}: read `sd task show {id}` and the job's log, find the root cause "
         "and fix it. Acknowledge the report (`sd reports acknowledge {id}`) only after a clean run.")


def prompt(kind: str, item: int) -> str:
    """The text the new session starts on: a debug session for a report, a builder session for anything else."""
    return (DEBUG if kind == "report" else BUILD).format(id=int(item))


def checkout(row: dict) -> Path:
    """The directory the session starts in, or `WorkflowError` naming why there is none."""
    if row["repo"]:
        directory = paths.expand(row["repo"])
    elif row["kind"] == "report":
        directory = CHECKOUT
    else:
        raise workflow.WorkflowError(f"item {row['id']} names no repository, so Fix has no checkout to start in; set its repo first")
    if not directory.is_dir():
        raise workflow.WorkflowError(f"{row['repo']} is not a directory on this machine, so Fix has no checkout to start in")
    return directory


def start(connection, item: int, *, expected_revision: str, who: str):
    """Steps 1 to 3 above: the item's state with the note, and `fix`; or `(status, {"error"})` when no session is known."""
    with LAUNCHING:
        state = workflow.item_state(connection, item)
        if state["revision"] != expected_revision:
            raise workflow.StaleItem(f"item {item} changed; reload it before applying this change")
        row = state["item"]
        if row["status"] == "done":
            raise workflow.WorkflowError(f"item {item} is done; Fix starts a session only for an open item")
        directory = checkout(row)
        session = "debug" if row["kind"] == "report" else "builder"
        try:
            done = subprocess.run([str(HERDR), "launch", str(directory), f"sd-{item}", prompt(row["kind"], item)],
                                  capture_output=True, text=True, timeout=TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            return 504, {"error": f"herdr.sh launch gave no answer in {TIMEOUT} s, so whether a session started is not "
                                  f"known. Look for workspace sd-{item} in herdr before you press Fix again."}
        if done.returncode != 0:
            said = (done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}").splitlines()[-1]
            # herdr.sh's line says what is left: nothing, a workspace at its prompt, or claude without the prompt.
            return 503, {"error": f"Fix failed: {said}"}
        found = re.match(r"launched (\S+): ", done.stdout.strip())
        pane = found[1] if found else "unknown"
        where = paths.key(str(directory))
        body = NOTE.format(session=session, item=item, pane=pane, where=where)
        try:
            state = workflow.add_item_note(connection, item, kind="comment", who=who, body=body)
        except (SdDbError, sqlite3.Error) as problem:
            return 503, {"error": f"claude started in herdr pane {pane}, but the note was not recorded: {problem}. "
                                  f"Record it with: sd task note {item} --body {shlex.quote(body)}"}
        state["fix"] = {"session": session, "pane": pane, "checkout": where}
        return state


def request(number: int, values: dict, revision: str):
    """The POST's checks, before the server opens a writer; the write as `action_route` returns one."""
    if values:
        raise ValueError("Fix takes only the item revision.")
    return lambda connection: start(connection, number, expected_revision=revision, who="dashboard")
