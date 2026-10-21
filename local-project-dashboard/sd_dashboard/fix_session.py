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

**Fix on a Today Now row** (sd:5652). Today posts `/api/now/fix` with a Now row's id for the rows no other command
addresses: a repo ahead, dirty or pinned, the abandoned worktrees, and a dark repos, sessions or jobs collector (a
failed job's Fix is its Run now, a pull request's its Open on GitHub, the pull request collector's its sync, all in the
page). `start_row` reads that source again and launches only while the read still raises the row: a row it no longer
raises is `StaleItem` (409), and a read that failed says whether the row stands is not known (503); a dark collector is
the one row a failed read confirms. A repo row starts in the checkout that read names; the rest start in this checkout.
The launch is step 2 above, on `row_prompt`'s fixed text for the row's kind; no fleet value enters the prompt. No note
is written: a Now row has no item, and herdr's workspace `fix-<kind>[-<name>]` is the record.
"""

from __future__ import annotations

import re
import shlex
import sqlite3
import subprocess
import threading
from pathlib import Path

from sd_db import operations, paths, workflow
from sd_db.errors import SdDbError

__all__ = ["CHECKOUT", "HERDR", "NOTE", "TIMEOUT", "prompt", "request", "row_prompt", "row_request", "start", "start_row"]

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


#: The Now rows Fix starts a session for, by kind (sd:5652). The session starts in the row's checkout, so no fleet value
#: enters the text; `{checkout}` is this checkout and `{source}` one of `DARK`.
ROW = {
    "ahead": ("Today's Now says this checkout has commits its remote does not have. Read `git status` and the unpushed "
              "commits, then land them as this repository's CLAUDE.md says: a branch and a pull request through the lane, "
              "never a push to the default branch. If they should not land, say why and stop."),
    "dirty": ("Today's Now says this checkout has uncommitted files. Read `git status` and `git diff`, find the work item "
              "each change belongs to, and land it as this repository's CLAUDE.md says. Discard nothing: ask about a "
              "change you cannot place."),
    "pinned": ("Today's Now says this checkout is pinned behind its origin, so merged commits are not live here. Run "
               "`{checkout}/local-repo-sync/repo-sync.sh refresh`, check that the pin moved, and report what the refresh said."),
    "worktree": ("Today's Now counts worktrees registered in a checkout's .git/worktrees whose directory is gone. Find each "
                 "in the checkouts local-project-dashboard/sd_dashboard/fleet.py reads (`git worktree list --porcelain` "
                 "marks it prunable), run `git worktree prune -v` in each, and report what was pruned. Change nothing else."),
    "dark": ("Today's Now says its {source} collector could not be read. Find why: read "
             "local-project-dashboard/sd_dashboard/now_screen.py and the collector it calls, reproduce the failure, fix "
             "the cause, and land the fix through the lane."),
}
#: The dark collectors a session is for; the pull request collector's Fix is its sync, in the page.
DARK = ("repos", "sessions", "jobs")
ROW_ID = re.compile(r"(?:(ahead|dirty|pinned):(.+):[1-9][0-9]{0,8}|worktrees:[1-9][0-9]{0,8}|dark:(%s))" % "|".join(DARK))
#: What a source's read may raise, as `now_screen.document` guards each one.
UNREAD = (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error)


def row_prompt(kind: str, *, source: str = "") -> str:
    """The text a Now row's session starts on: fixed per kind."""
    return ROW[kind].format(checkout=CHECKOUT, source=source)


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
        if row["kind"] == "report" and row["source"] == "brief":
            raise workflow.WorkflowError(f"item {item} is a routine brief; Fix starts no debug session for one (sd:3459)")
        directory = checkout(row)
        session = "debug" if row["kind"] == "report" else "builder"
        pane = _launch(directory, f"sd-{item}", prompt(row["kind"], item))
        if isinstance(pane, tuple):
            return pane
        where = paths.key(str(directory))
        body = NOTE.format(session=session, item=item, pane=pane, where=where)
        try:
            state = workflow.add_item_note(connection, item, kind="comment", who=who, body=body)
        except (SdDbError, sqlite3.Error) as problem:
            return 503, {"error": f"claude started in herdr pane {pane}, but the note was not recorded: {problem}. "
                                  f"Record it with: sd task note {item} {shlex.quote(body)}"}
        state["fix"] = {"session": session, "pane": pane, "checkout": where}
        return state


def _launch(directory: Path, label: str, text: str):
    """Step 2: `herdr.sh launch`, answered as the pane, or `(status, {"error"})` when no session is known."""
    try:
        done = subprocess.run([str(HERDR), "launch", str(directory), label, text],
                              capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return 504, {"error": f"herdr.sh launch gave no answer in {TIMEOUT} s, so whether a session started is not "
                              f"known. Look for workspace {label} in herdr before you press Fix again."}
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}").splitlines()[-1]
        # herdr.sh's line says what is left: nothing, a workspace at its prompt, or claude without the prompt.
        return 503, {"error": f"Fix failed: {said}"}
    found = re.match(r"launched (\S+): ", done.stdout.strip())
    return found[1] if found else "unknown"


def _row(connection, row: str, fleet, jobs):
    """The row's checkout, workspace label and prompt, from a read of its source now; see the module's last part."""
    from . import now_screen  # now_screen reads active_sessions, which reads this module's NOTE

    found = ROW_ID.fullmatch(row)
    kind, name, source = found[1] or ("worktree" if row.startswith("worktrees:") else "dark"), found[2], found[3]
    if kind == "dark":
        try:
            if source == "jobs":
                now_screen._jobs(connection, jobs or operations.LaunchdBackend())
            else:
                (now_screen._repos if source == "repos" else now_screen._sessions)(fleet)
        except UNREAD:
            return CHECKOUT, f"fix-dark-{source}", row_prompt("dark", source=source)
        raise workflow.StaleItem(f"the {source} collector reads now, so {row} is not a Now row; read Now again")
    area = "sessions" if kind == "worktree" else "repos"
    try:
        document = fleet(area)
        rows = (now_screen._sessions if kind == "worktree" else now_screen._repos)(lambda _: document)
    except UNREAD as problem:
        return 503, {"error": f"the {area} were not read, so whether {row} is still a Now row is not known: {problem}"}
    if not any(raised["id"] == row for raised in rows):
        raise workflow.StaleItem(f"{row} is not a Now row any more; read Now again")
    if kind == "worktree":
        return CHECKOUT, "fix-worktrees", row_prompt(kind)
    # A repo row's id is its name and count, and two checkouts in two groups can share a name.
    named = [repo for repo in document["repos"] if any(raised["id"] == row for raised in now_screen.backbone_rows([repo]))]
    if len(named) != 1:
        raise workflow.WorkflowError(f"{len(named)} checkouts are named {name}, so Fix cannot tell which one {row} is")
    directory = Path(str(named[0].get("path") or ""))
    if not directory.is_absolute() or not directory.is_dir():
        raise workflow.WorkflowError(f"{named[0].get('path')} is not a directory on this machine, so Fix has no checkout to start in")
    return directory, f"fix-{kind}-{name}", row_prompt(kind)


def start_row(connection, row: str, *, fleet=None, jobs=None):
    """A session on a Now row that its source raises now: `{"fix"}`, or `(status, {"error"})` when none is known."""
    from . import fleet as fleet_module

    with LAUNCHING:
        found = _row(connection, row, fleet or fleet_module.collect, jobs)
        if isinstance(found[1], dict):
            return found
        directory, label, text = found
        pane = _launch(directory, label, text)
        if isinstance(pane, tuple):
            return pane
        return {"fix": {"pane": pane, "checkout": paths.key(str(directory)), "workspace": label}}


def row_request(values: dict, *, fleet=None, jobs=None):
    """`/api/now/fix`'s checks, before the server opens a writer: one Now row id that no other route fixes."""
    if set(values) != {"row"} or not isinstance(values["row"], str) or not ROW_ID.fullmatch(values["row"]):
        raise ValueError("Fix takes one Now row: a repo ahead, dirty or pinned, the abandoned worktrees, or a dark "
                         "repos, sessions or jobs collector.")
    return lambda connection: start_row(connection, values["row"], fleet=fleet, jobs=jobs)


def request(number: int, values: dict, revision: str):
    """The POST's checks, before the server opens a writer; the write as `action_route` returns one."""
    if values:
        raise ValueError("Fix takes only the item revision.")
    return lambda connection: start(connection, number, expected_revision=revision, who="dashboard")
