"""Draft email (sd:3463): one click on a task saves a Gmail draft about it, written by claude. It never sends.

The operator ruled on 2026-10-13 (a note on sd:3463): Claude drafts the text from the item's title and notes, and the
action saves it as a Gmail draft through the workspace tool. Tasks posts `/api/items/<id>/draft-email` with the item
revision it read; `request` checks the body before the server opens a writer, and `start` runs in four steps:

1. The item as it is now. Its revision must be the one the page read (`StaleItem`, a 409), so a second click from the
   same page, after the first one's note landed, drafts nothing. Its kind must be one of `KINDS`, and the Gmail account
   must be set: `EMAIL_FROM`, the account `local-notify` sends from and Notes reads (`notes_screen.workspace`).
2. `claude -p` writes the draft: the fixed `PROMPT` and the item's title, description and notes, all on stdin, never in
   argv or a shell line. It runs with no tools and no MCP servers, sources `~/.config/shell/env.sh` on the same line for
   its token, and is bounded by `MODEL_TIMEOUT`: past it the whole process group is killed. The reply must be a
   `Subject:` line, a blank line and a body; anything else saves nothing.
3. workspace-mcp's `draft_gmail_message` saves it, with no recipient, through `local-mail-intake`'s client. No other
   Gmail tool is called; `test_email_draft` fails a send. A call refused by the server saved nothing; a call with no
   answer may have saved a draft, and the note says so.
4. A comment note on the item names the subject, the draft id and a Gmail Drafts link, never the body. A failure in
   step 2 or 3 writes a note too, saying whether a draft exists. A note that cannot be written after a saved draft
   answers 503 naming the draft, so the operator knows it exists without its record.

`DRAFTING` holds one draft at a time, so two clicks that cross pass step 1 one after the other: the second then reads
the first one's note in the revision and is refused.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import threading
from urllib.parse import quote

from sd_db import workflow
from sd_db.errors import SdDbError
from sd_db.progress import CANCELLABLE_TASK_KINDS

from . import notes_screen
from .claude_env import SHELL_ENV

__all__ = ["KINDS", "MODEL_TIMEOUT", "GMAIL_TIMEOUT", "TOOL", "command", "prompt", "reply", "request", "start"]

#: The kinds a task's email fits: `sd task` items, as Close's `progress.task_guard` admits them; Tasks' `CLOSES` too.
KINDS = CANCELLABLE_TASK_KINDS
MODEL_TIMEOUT = 120
GMAIL_TIMEOUT = 30
#: The one Gmail tool this action calls: a draft, never a send.
TOOL = "draft_gmail_message"
#: The item text the model sees, newest notes kept when it is longer.
TEXT_MAX = 12000
DRAFTING = threading.Lock()

PROMPT = ("Write a short plain-text email about the work item below, in the first person, from the person who owns "
          "it. Name no recipient and invent no fact, name, address or date the item does not state. Reply with "
          "exactly this and nothing else: one line `Subject: <subject>`, one blank line, then the body.")
#: After a kill, how long the run's pipes are read before they are closed unread.
REAP_TIMEOUT = 5


def command(tools: str, *, bound: int | None = None) -> str:
    """The `sh -c` line of one `claude -p` whose only built-in tools are `tools` (`'""'` for none).

    launchd gives the dashboard 256 open files, and claude aborts below that need (local-cron-jobs raises it too);
    the LaunchAgent's PATH lacks ~/.local/bin, where the native installer puts claude. With `bound`, the run carries
    its own alarm, which survives the exec: it ends after `bound` seconds even when no dashboard is waiting for it.
    `SHELL_ENV` (claude_env) sources ~/.config/shell/env.sh and maps its CUSTOM_ token (sd:5791).
    """
    alarm = f"perl -e 'alarm shift; exec @ARGV' {int(bound)} " if bound else ""
    return (SHELL_ENV + 'ulimit -S -n 65536 2>/dev/null; '
            f'exec {alarm}"${{CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}}" -p --model sonnet '
            f'--restricted --strict-mcp-config --tools {tools} --no-session-persistence')


CLAUDE = command('""')
NOTE = 'Draft email: saved Gmail draft "{subject}" (draft {draft}); it is not sent. Open it in Drafts: {link}'
FAILED = "Draft email failed: {why}"


def _text(body) -> str:
    """An item body is `{"text": ...}` JSON; an older one is plain text."""
    try:
        value = json.loads(body or "")
    except ValueError:
        return body or ""
    return value.get("text") or "" if isinstance(value, dict) else body or ""


def prompt(state: dict) -> str:
    """What the model reads on stdin: `PROMPT`, then the item. Status changes are left out; they say nothing to write."""
    row = state["item"]
    notes = [f"- {note['kind']} {note['timestamp']}: {note['body']}" for note in state["notes"]
             if note["kind"] != "status_change" and note["body"]]
    head = f"Item sd:{row['id']}\nTitle: {row['title']}\nStatus: {row['status']}\nDescription: {_text(row['body']) or '(none)'}\n"
    while notes and len(head) + sum(len(line) + 1 for line in notes) > TEXT_MAX:
        notes.pop(0)
    return f"{PROMPT}\n\n---\n{head}Notes, oldest first:\n" + ("\n".join(notes) or "(none)") + "\n"


def reply(text: str) -> tuple[str, str]:
    """`(subject, body)` from the model's reply, or `ValueError` saying what was wrong with it."""
    lines = text.strip().splitlines()
    found = re.fullmatch(r"Subject:\s*(.+)", lines[0].strip()) if lines else None
    if not found or len(found[1]) > 200:
        raise ValueError("the reply did not start with a Subject line")
    body = "\n".join(lines[1:]).strip()
    if not body:
        raise ValueError("the reply had no body")
    return found[1].strip(), body


def _model(text: str, line: str | None = None, *, cwd=None) -> str:
    """The reply of one bounded `claude -p` run of `line` (`CLAUDE` by default) in `cwd`; `TimeoutError` or
    `RuntimeError` saying why there is none."""
    child = subprocess.Popen(["sh", "-c", line or CLAUDE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True, cwd=cwd)
    try:
        out, err = child.communicate(text, timeout=MODEL_TIMEOUT)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        try:
            child.communicate(timeout=REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            # A descendant that left the group still holds the pipes; the run is dead, so stop reading them.
            child.stdout.close()
            child.stderr.close()
            child.wait()
        raise TimeoutError(f"claude -p gave no answer in {MODEL_TIMEOUT} s") from None
    if child.returncode != 0:
        said = (err.strip() or out.strip() or "no output").splitlines()[-1]
        raise RuntimeError(f"claude -p exited {child.returncode}: {said}")
    return out


def _save(url: str, account: str, subject: str, body: str) -> str:
    """The draft id; `ConnectionError` when nothing was saved, `LookupError` when whether a draft exists is not known."""
    mi = notes_screen._mail_intake()
    client = mi.Mcp(url, timeout=GMAIL_TIMEOUT)
    try:
        try:
            client.connect()
        except Exception as error:  # noqa: BLE001 - no draft call was sent, whatever broke
            raise ConnectionError(f"workspace-mcp at {url}: {error}") from None
        try:
            said = client.call(TOOL, {"user_google_email": account, "subject": subject, "body": body})
        except mi.McpError as error:
            if str(error).startswith(f"{TOOL}: ") and "unparseable" not in str(error):
                raise ConnectionError(f"workspace-mcp refused the draft: {error}") from None
            raise LookupError(str(error)) from None
        except Exception as error:  # noqa: BLE001 - the call left; its answer did not come back
            raise LookupError(f"{error.__class__.__name__}: {error}") from None
    finally:
        notes_screen.close_session(url, client)
    found = re.search(r"Draft ID:\s*(\S+)", said)
    if not found:
        raise LookupError(f"workspace-mcp answered without a draft id: {said[:200]}")
    return found[1]


def _failed(connection, item: int, who: str, status: int, why: str):
    """`(status, {"error"})` with the failure recorded on the item, or named as unrecorded when the note fails."""
    body = FAILED.format(why=why)
    try:
        workflow.add_item_note(connection, item, kind="comment", who=who, body=body)
    except (SdDbError, sqlite3.Error) as problem:
        return status, {"error": f"{body} The failure was not recorded on the item: {problem}."}
    return status, {"error": body}


def start(connection, item: int, *, expected_revision: str, who: str):
    """Steps 1 to 4 above: the item's state with the note, and `draft`; or `(status, {"error"})` naming what is left."""
    with DRAFTING:
        state = workflow.item_state(connection, item)
        if state["revision"] != expected_revision:
            raise workflow.StaleItem(f"item {item} changed; reload it before applying this change")
        row = state["item"]
        if row["kind"] not in KINDS:
            raise workflow.WorkflowError(f"item {item} is a {row['kind']}; Draft email writes about a task or followup")
        url, account, why = notes_screen.workspace()
        if not account:
            return 503, {"error": f"Draft email needs the Gmail account. {why}"}
        try:
            subject, body = reply(_model(prompt(state)))
        except TimeoutError as error:
            return _failed(connection, item, who, 504, f"{error}. No Gmail draft was saved.")
        except (OSError, RuntimeError, ValueError) as error:
            return _failed(connection, item, who, 503, f"{error}. No Gmail draft was saved.")
        try:
            draft = _save(url, account, subject, body)
        except ConnectionError as error:
            return _failed(connection, item, who, 503, f"{error}. No Gmail draft was saved.")
        except LookupError as error:
            return _failed(connection, item, who, 504, f"workspace-mcp gave no clear answer ({error}), so whether a "
                                                       f'Gmail draft "{subject}" was saved is not known. Look in Gmail '
                                                       "Drafts before you press Draft email again.")
        link = f"https://mail.google.com/mail/?authuser={quote(account)}#drafts"
        note = NOTE.format(subject=subject, draft=draft, link=link)
        try:
            state = workflow.add_item_note(connection, item, kind="comment", who=who, body=note)
        except (SdDbError, sqlite3.Error) as problem:
            return 503, {"error": f'Gmail draft "{subject}" (draft {draft}) was saved, but the note was not recorded: '
                                  f"{problem}. Record it with: sd task note {item} {shlex.quote(note)}"}
        state["draft"] = {"subject": subject, "id": draft, "link": link}
        return state


def request(number: int, values: dict, revision: str):
    """The POST's checks, before the server opens a writer; the write as `action_route` returns one."""
    if values:
        raise ValueError("Draft email takes only the item revision.")
    return lambda connection: start(connection, number, expected_revision=revision, who="dashboard")
