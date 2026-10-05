"""What the row records of a session: its notes and, for a `start` entry, its cost.

Criterion 1's opening sentence: the row records provider, start, end, cost
and the worktree path, and at least one `note` from the session is written.
Provider, start, end and the path are the claim's and `update_run`'s; this
module reads the two things only the session can say and hands them to the
library, which is the one writer.

**Notes.** A session records followups, decisions, proposals and questions
as it produces them by appending one JSON line to `.git/sd-notes.jsonl` in
its clone: `{"kind": "followup", "body": "..."}`. The runner files each as a
`note` row on the item with the run as its session. The file lives under
`.git/` beside the provider log, so it is retained and archived with the
clone and never committed.

**Cost.** On a `url` entry every call is one item B's library made and
reserved, so the row's cost is the sum of those rows and the runner writes
none. On a `start` entry -- `claude -p`, `codex exec`, a fixture script --
the library is in no call's path, so the runner reads the total the session
reports at its own exit and writes one `run` row through B's
`record_session_cost`. The read is the entry's `reader`: `claude-json` is the
result envelope `claude --output-format json` prints last, `usage` with
`input_tokens` and `output_tokens` and `total_cost_usd`; `codex-json` is the
same shape when the session prints one. A session that reported nothing
readable still gets its row, with the numbers null and the row's `detail`
saying so, because a `start` session that cost something and shows nothing
is worse than one that shows a blank.
"""

from __future__ import annotations

import json
from pathlib import Path

from sd_db import runner as store
from sd_db.database import transaction

NOTES_FILE = ".git/sd-notes.jsonl"
#: Beside the database, outside the clone the session can write (sd:2503).
HELD_DIR = "runner-session-held"
NOTES_LIMIT = 256 * 1024
NOTE_BODY_LIMIT = 8000
NOTE_COUNT_LIMIT = 200
LOG_TAIL = 1024 * 1024
TOKEN_LIMIT = 10**12


def read_notes(clone: Path) -> list[dict]:
    """The session's note lines, parsed and bounded; a malformed line refuses the file.

    Refusing rather than skipping: a session that wrote a line the runner
    drops has lost a note it named, which is what requirement 7 exists to
    prevent. The refusal names the line.

    The file is the session's own and is read as one: a link here is
    refused, because `is_file()` follows it and a session could make the
    runner file an operator's file as `note` rows (sd:1221). The bytes are
    decoded strictly for the same reason the lines are: `errors="replace"`
    turned malformed bytes into U+FFFD and let the note through altered.
    """
    path = Path(clone) / NOTES_FILE
    if path.is_symlink():
        raise store.RunnerRefused(f"{NOTES_FILE} is a link; the runner records only the session's own file")
    if not path.is_file():
        return []
    if path.stat().st_size > NOTES_LIMIT:
        raise store.RunnerRefused(f"{NOTES_FILE} exceeds {NOTES_LIMIT} bytes")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise store.RunnerRefused(f"{NOTES_FILE} is not UTF-8: {error}") from error
    notes = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as error:
            raise store.RunnerRefused(f"{NOTES_FILE} line {number} is not JSON: {error}") from error
        if not isinstance(value, dict) or value.get("kind") not in store.SESSION_NOTE_KINDS or not isinstance(value.get("body"), str) or not value["body"].strip():
            raise store.RunnerRefused(f"{NOTES_FILE} line {number} must be {{\"kind\": one of {', '.join(store.SESSION_NOTE_KINDS)}, \"body\": text}}")
        notes.append({"kind": value["kind"], "body": value["body"].strip()[:NOTE_BODY_LIMIT]})
        if len(notes) > NOTE_COUNT_LIMIT:
            raise store.RunnerRefused(f"{NOTES_FILE} holds more than {NOTE_COUNT_LIMIT} notes")
    return notes


def _int(value) -> int | None:
    return value if type(value) is int and 0 <= value <= TOKEN_LIMIT else None


def read_usage(log: Path, reader: str | None) -> dict:
    """The total a `start` session reported at its exit, from its retained log.

    The log is the provider's stdout and stderr together, so the envelope is
    searched for from the end: the last line that is a JSON object of
    `type` `result`. Nothing found is `{"found": False}` and no numbers.
    """
    empty = {"found": False, "tokens_in": None, "tokens_out": None, "usd": None}
    if reader not in ("claude-json", "codex-json") or not Path(log).is_file():
        return empty
    with Path(log).open("rb") as handle:
        handle.seek(max(0, Path(log).stat().st_size - LOG_TAIL))
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict) or value.get("type") != "result":
            continue
        usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
        usd = value.get("total_cost_usd")
        return {"found": True, "tokens_in": _int(usage.get("input_tokens")), "tokens_out": _int(usage.get("output_tokens")),
                "usd": float(usd) if type(usd) in (int, float) and 0 <= usd <= 10**9 else None}
    return empty


def _cost(connection, ident: str, provider: dict, usage: dict) -> int:
    return store.record_session_cost(connection, ident, provider=provider["provider"], bill=provider.get("bill"),
                                     tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"], usd=usage["usd"])


def _both(connection, ident: str, notes: list[dict], provider: dict, usage: dict | None) -> dict:
    """The notes and the cost in one commit, so neither survives the other."""
    with transaction(connection):
        result = {"notes": store.record_session_notes(connection, ident, notes) if notes else [], "cost": None}
        if usage is not None:
            result["cost"] = _cost(connection, ident, provider, usage)
        return result


def record(connection, write, request: dict, provider: dict, clone: Path) -> dict:
    """File the session's notes and, for a `start` entry, its one cost row.

    `write` is the runner's retrying database writer. `provider` is what
    `provider_command` resolved: `provider`, `vendor`, and for a registry
    entry its `bill`, `reader` and whether it is a `start` line. The fixture
    path, which resolves no registry entry, records notes and no cost.

    Both rows are one write. Two commits left a runner that died between
    them with the notes filed and no cost, and recovery records the ending
    alone (sd:1221). An unreadable notes file still files the cost first:
    the session spent what it spent, and the refusal that follows is what
    blocks the row.
    """
    ident = request["run"]["id"]
    usage = read_usage(Path(clone) / ".git/sd-provider.log", provider.get("reader")) if provider.get("start") else None
    try:
        notes = read_notes(clone)
    except store.RunnerRefused:
        if usage is not None:
            write(connection, _cost, ident, provider, usage)
        raise
    result = write(connection, _both, ident, notes, provider, usage)
    if usage is not None:
        result["usage"] = usage
    return result


def held(database: Path, ident: str) -> Path:
    """Where a session a recovery hold kept from the store waits for its ending (sd:2503)."""
    return Path(database).parent / HELD_DIR / f"{ident}.json"


def hold(database: Path, ident: str, provider: dict) -> None:
    """Remember the session's provider; its notes and usage stay in the clone."""
    path = held(database, ident)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: provider.get(key) for key in ("provider", "bill", "reader", "start")}))
