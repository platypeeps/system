"""Chat (sd:5294): the right pane's question, answered by one bounded, read-only `claude -p`.

Every page's Chat tab posts `/api/chat` with `{scope, page, selected, text}`; `request` checks the body before the
server opens a connection, and `answer` runs in three steps:

1. `ASKING` is taken without waiting: a question while another runs answers 409, and the running one goes on.
2. The context: `PROMPT`, the page, the scope, the selected row as the page put it and, when that row has an item, the
   item as the adversarial review reads it, read here and not taken from the page. The run's directory is
   `checkout`'s: the item's repo, else the scope's checkout, else this one.
3. `claude -p` answers on stdout. The line is `email_draft.command` with the read tools only (`TOOLS`) and its runner
   is `email_draft._model`, so the prompt goes on stdin, the token comes from `~/.config/shell/env.sh`, and the
   process group is killed past `email_draft.MODEL_TIMEOUT`. The line carries its own alarm (`GRACE` past that bound), so a run the
   dashboard stopped watching, after a restart, still ends.

The run reads files in one checkout and does nothing else: `--restricted` drops the user, project and local settings
files and keeps the file tools in the working directory, `--strict-mcp-config` loads no MCP server, and `--tools`
names only Read, Grep and Glob. It writes, sends and merges nothing; the workflow database is only read.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from sd_db import paths, workflow

from . import email_draft

__all__ = ["ANSWER_MAX", "GRACE", "PROMPT", "TEXT_MAX", "TOOLS", "answer", "checkout", "line", "request"]

#: This repository's checkout: the fleet scope's, and any scope's with no checkout of its own.
CHECKOUT = Path(__file__).resolve().parents[2]
#: The built-in tools the run has: reads only.
TOOLS = "Read,Grep,Glob"
#: The run's own alarm, past the runner's bound, so the runner's kill comes first while the dashboard waits.
GRACE = 10
TEXT_MAX = 4000
LABEL_MAX = 300
ITEM_MAX = 16000
ANSWER_MAX = 20000
FIELDS = {"scope", "page", "selected", "text"}
SELECTED = {"id", "type", "label", "item"}
ASKING = threading.Lock()

PROMPT = ("You answer one question the operator typed in the chat pane of their sd workflow dashboard. You may read "
          "files in the working directory with Read, Grep and Glob. You cannot run commands or change anything, so "
          "never say you did. Answer in plain text, briefly, and say what you do not know.")


def line() -> str:
    """The run's `sh -c` line; its alarm is `GRACE` seconds past the runner's bound as it is now."""
    return email_draft.command(TOOLS, bound=email_draft.MODEL_TIMEOUT + GRACE)


def _short(value, name: str, limit: int) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"The chat's {name} is text of at most {limit} characters.")
    return value


def request(values: dict):
    """The POST's checks, before the server opens a connection; the answer as `action_route` returns one."""
    if set(values) - FIELDS or "text" not in values:
        raise ValueError("Chat takes scope, page, selected and text.")
    text = values["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
        raise ValueError(f"Ask a question of 1 to {TEXT_MAX} characters.")
    scope = values.get("scope", "fleet")
    if not isinstance(scope, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", scope):
        raise ValueError("The chat's scope is a lower-case name.")
    page = _short(values.get("page") or "", "page", 40)
    selected = values.get("selected")
    if selected is not None:
        if not isinstance(selected, dict) or set(selected) - SELECTED:
            raise ValueError("The selected row takes id, type, label and item.")
        item = selected.get("item")
        if item is not None and (type(item) is not int or not 1 <= item <= 9223372036854775807):
            raise ValueError("The selected row's item is a positive integer.")
        selected = {**{key: _short(selected.get(key) or "", key, LABEL_MAX) for key in ("id", "type", "label")},
                    "item": item}
    return lambda connection: answer(connection, text.strip(), scope=scope, page=page, selected=selected)


def checkout(connection, scope: str, row: dict | None) -> Path:
    """The run's directory, or `OSError` naming why there is none.

    An item that names a repo gets that checkout or nothing: answering from another repo would answer about the wrong
    code. Scope `pack` is the command pack's checkout, `hoa` the registered hoa repo; any other scope is this one.
    """
    if row is not None and row.get("repo"):
        directory = paths.expand(row["repo"])
        if not directory.is_dir():
            raise FileNotFoundError(f"sd:{row['id']} names {row['repo']}, which is not a directory on this machine")
        return directory
    if scope == "pack":
        from . import pack_library

        directory = pack_library().parent
    elif scope == "hoa":
        from . import hoa_screen

        directory = hoa_screen.checkout(connection)[1] or CHECKOUT
    else:
        directory = CHECKOUT
    if not directory.is_dir():
        raise FileNotFoundError(f"the {scope} scope's checkout {directory} is not a directory on this machine")
    return directory


def _context(state: dict | None, *, scope: str, page: str, selected: dict | None, where: Path, text: str) -> str:
    """What the model reads on stdin: `PROMPT`, the page and its selection, the item when there is one, the question."""
    from .adversarial import _cut, _item_text

    lines = [PROMPT, "", "---", f"Page: {page or 'unknown'} (scope: {scope})", f"Working directory: {where}"]
    if selected:
        lines.append(f"Selected row: {selected['type'] or 'row'} {selected['label']} (id {selected['id']})")
    if state is not None:
        lines += ["", _cut(_item_text(state, heading="Selected item", extra=[]), ITEM_MAX)]
    return "\n".join(lines + ["---", "", "Question:", text, ""])


def answer(connection, text: str, *, scope: str, page: str, selected: dict | None):
    """Steps 1 to 3 above: `{answer, where}`, or `(status, {"error"})` saying why there is no answer."""
    if not ASKING.acquire(blocking=False):
        return 409, {"error": "A chat answer is still running; ask again when it is shown."}
    try:
        item = selected and selected["item"]
        state = workflow.item_state(connection, item) if item else None
        try:
            where = checkout(connection, scope, state["item"] if state else None)
        except OSError as problem:
            return 503, {"error": f"No answer: {problem}."}
        prompt = _context(state, scope=scope, page=page, selected=selected, where=where, text=text)
        try:
            said = email_draft._model(prompt, line(), cwd=where).strip()
        except TimeoutError as error:
            return 504, {"error": f"No answer: {error}."}
        except (OSError, RuntimeError) as error:
            return 503, {"error": f"No answer: {error}."}
        if not said:
            return 503, {"error": "No answer: claude -p answered nothing."}
        return {"answer": said if len(said) <= ANSWER_MAX else said[:ANSWER_MAX - 1].rstrip() + "…",
                "where": str(where)}
    finally:
        ASKING.release()
