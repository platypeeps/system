"""Today's Working now (sd:3466): each open item's live sessions, when each started and how long it has run.

Five readers, each one the tree already had, joined per work item; this module adds only the join:

  lane       the `sd-ship lane` entries: the latest `state` row (kind `checkpoint`) of each key `lane:v1:<repo>:<id>`,
             read through the pack's `reads.lane_entries`. A `running` entry is a session from its `started_at`,
             a `pending` one from its `enqueued_at`. Every entry, in any status, also ties its `worktree` to its `item`.
  launch     the comment notes Fix writes (sd:3456, `fix_session.NOTE`) on items that are not done. The newest note per
             herdr pane ties that pane to the note's item, from the note's time. A note whose pane was not recorded
             (`unknown`) ties nothing.
  herdr      `herdr agent list` in the session Fix launches into (`herdr_wrap.session_name`): each live agent's pane and cwd.
  worktrees  `fleet.collect("sessions")`, the read Now already makes: each live worktree, its path, branch and `created`.
  processes  the same read's `sd` and `sd-*` commands, with the elapsed time `ps` gave.

The join, in order. A worktree names its item through the newest lane entry for its path, else through an item number
in its branch name (`sd3456-fix-action`, `sessions-view-3466`). A process names its item through `--item N`, else
through `-C <dir>` inside a worktree that named one. A herdr agent names its item through its pane's launch note, else
through a cwd inside a worktree that named one. Each row's `via` says which tie made it, so a reader can check it. A row
is kept only when its item exists and is not done: a worktree left after its merge is cleanup, not work.

A source that could not be read, or answered in the wrong shape, is a reason in `sources`, never an empty list. A
launch note whose pane herdr could not be asked about is a row with `verified` false; one whose pane herdr answered
without is a session that ended, and no row.
herdr not installed, or its session not running, is a definite answer: no agent is live. herdr is asked only when a
launch note or a joined worktree could tie an agent to an item.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sd_db import paths, reads
from sd_db.errors import SdDbError

from . import fix_session

__all__ = ["document", "elapsed", "herdr_agents"]

#: One herdr control call's bound; a herdr that does not answer within it is an unknown, not an empty list.
HERDR_SECONDS = 5
#: An item number in a branch name: digits after the start, `sd`, `-`, `_` or `/`, and before the end or a separator.
BRANCH_ITEM = re.compile(r"(?:^|[-_/]|sd)(\d+)(?=$|[-_/])", re.ASCII)
#: Fix's note, read back: the pattern is `fix_session.NOTE` with its four fields as groups.
LAUNCH = re.compile("^" + re.escape(fix_session.NOTE)
                    .replace(r"\{session\}", r"(?P<session>\S+)").replace(r"\{item\}", r"(?P<item>\d+)")
                    .replace(r"\{pane\}", r"(?P<pane>[^,\s]+)").replace(r"\{where\}", r"(?P<where>.+)") + "$")
#: The order of a row's kinds when two start at the same time, or neither has a start.
KINDS = ("worktree", "lane", "process", "agent")


def _when(stamp) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        value = datetime.fromisoformat(stamp)
        # In UTC here, so a time at the edge of the calendar (`0001-01-01T00:00+05:00`) is no time, not a later raise.
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _iso(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""


def elapsed(seconds: int) -> str:
    """How long, in the two largest units: `<1m`, `14m`, `2h 5m`, `3d 4h`."""
    minutes = max(seconds, 0) // 60
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h {minutes % 60}m"
    return f"{hours // 24}d {hours % 24}h"


def _etime(text: str) -> int | None:
    """`ps`'s `etime`, `[[dd-]hh:]mm:ss`, in seconds; None when it is not one."""
    # Bounded: five digits of days is 273 years, and `now` minus a longer time leaves the calendar.
    found = re.fullmatch(r"(?:(?:(\d{1,5})-)?(\d{1,2}):)?(\d{1,2}):(\d{1,2})", text.strip(), re.ASCII)
    if not found:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in found.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _real(path) -> str:
    return os.path.realpath(os.path.expanduser(path)) if isinstance(path, str) and path else ""


class Unread(ValueError):
    """A source that could not be read, or answered in the wrong shape; its text is the reason `sources` shows."""


#: What a source's read may raise: each marks that source unread, and the rest of `/api/now` still answers.
UNREAD = (ValueError, OverflowError, TypeError, KeyError, IndexError, AttributeError, OSError, sqlite3.Error, SdDbError)


def _reason(failure: Exception) -> str:
    """`Unread`'s own text; any other failure is unexpected, so its class leads."""
    if isinstance(failure, Unread):
        return str(failure) or "it could not be read"
    return f"{type(failure).__name__}: {failure}"


def _expect(holds: bool, problem: str) -> None:
    """A source's answer of the wrong shape is that source unread, as a failed read is: never fewer rows."""
    if not holds:
        raise Unread(problem)


def _is_text(value) -> bool:
    """Text with no NUL: a NUL is no path, and `os.path.realpath` raises on one."""
    return isinstance(value, str) and "\x00" not in value


def _text(row: dict, *fields: str) -> bool:
    """Each of `fields` is absent, null or text: the optional fields a source may leave out."""
    return all(row.get(field) is None or _is_text(row.get(field)) for field in fields)


def _item_id(value) -> int | None:
    """An item id: 1 to 9 ASCII digits, as text or an int; anything else is None.

    `str.isdigit` also takes `²`, which `int` refuses, and `١`, which it reads; `int` refuses 4301 digits; SQLite
    overflows past 2**63. Every text or number that becomes an item id here goes through this one check.
    """
    if type(value) is int:
        return value if 0 <= value < 10**9 else None
    if isinstance(value, str) and 1 <= len(value) <= 9 and value.isascii() and value.isdigit():
        return int(value)
    return None


def _inside(real: str, joined: dict[str, int]) -> str | None:
    """The joined worktree the resolved path `real` is in, or None."""
    if not real:
        return None
    return next((tree for tree in joined if real == tree or real.startswith(tree + os.sep)), None)


# -- the readers ------------------------------------------------------------------------------------------------------


def _lane(connection) -> list[dict]:
    entries = []
    for key, _, body in reads.lane_entries(connection):
        try:
            entry = json.loads(body)
        except (TypeError, ValueError):
            entry = None
        _expect(isinstance(entry, dict) and type(entry.get("item")) is int and _item_id(entry["item"]) is not None
                and _text(entry, "worktree", "status", "step", "started_at", "enqueued_at")
                and isinstance(entry.get("holder"), (dict, type(None))), f"lane entry {key} is not a lane entry")
        holder = entry.get("holder") or {}
        entries.append({"id": entry.get("id"), "item": entry["item"], "status": entry.get("status"),
                        "real": _real(entry.get("worktree")), "enqueued": str(entry.get("enqueued_at") or ""),
                        "session": f"Lane {entry.get('step') or 'run'} on {holder.get('host') or 'an unnamed host'}",
                        "started": _when(entry.get("started_at")), "queued": _when(entry.get("enqueued_at"))})
    return entries


def _launches(connection) -> dict[str, dict]:
    """The newest launch note per herdr pane, on items that are not done."""
    prefix = fix_session.NOTE.split("{", 1)[0]
    out = {}
    for _, item, stamp, body in reads.launch_notes(connection, prefix):
        _expect(_item_id(item) is not None and _is_text(body), f"a Fix note on item {item} is not a note")
        found = LAUNCH.match(body)
        if found and found["pane"] != "unknown":
            out[found["pane"]] = {"item": item, "at": _when(stamp), "session": found["session"]}
    return out


def _wrap():
    """`local-herdr/herdr_wrap.py`, loaded by path as `fleet` loads `collectors.py`: its session rule is the one Fix uses."""
    spec = importlib.util.spec_from_file_location("sd_dashboard_herdr_wrap", fix_session.CHECKOUT / "local-herdr" / "herdr_wrap.py")
    if spec is None or spec.loader is None:
        raise Unread("local-herdr/herdr_wrap.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _herdr(args: list[str], environment: dict) -> dict:
    said = "herdr " + " ".join(args)
    try:
        done = subprocess.run(["herdr", *args], env=environment, capture_output=True, text=True,
                              timeout=HERDR_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise Unread(f"{said} gave no answer in {HERDR_SECONDS} s") from None
    except OSError as error:
        raise Unread(f"{said} did not run: {error}") from None
    for stream in (done.stdout, done.stderr):
        try:
            answer = json.loads(stream)
        except ValueError:
            continue
        if isinstance(answer, dict):
            if answer.get("error"):
                raise Unread(f"{said}: {answer['error']}")
            return answer
    raise Unread(f"{said}: exit {done.returncode}, no JSON answer")


def herdr_agents() -> list[dict]:
    """Live agents in the herdr session Fix launches into; [] when herdr is not installed or that session is not running."""
    if shutil.which("herdr") is None:
        return []
    wrap = _wrap()
    session = wrap.session_name()
    environment = wrap.herdr_env(session)
    listed = _herdr(["session", "list", "--json"], environment).get("sessions")
    _expect(isinstance(listed, list) and all(isinstance(row, dict) for row in listed),
            "herdr session list --json answered no session list")
    entry = next((row for row in listed if row.get("name") == session), None)
    if entry is None:
        return []
    _expect(isinstance(entry.get("running"), bool) and _text(entry, "socket_path"),
            f"herdr session list --json answered session {session} of the wrong shape")
    if not entry["running"]:
        return []
    if entry.get("socket_path"):
        environment["HERDR_SOCKET_PATH"] = entry["socket_path"]
    result = _herdr(["agent", "list"], environment).get("result")
    agents = result.get("agents") if isinstance(result, dict) else None
    _expect(isinstance(agents, list), "herdr agent list answered no agent list")
    _expect(all(isinstance(agent, dict) and _is_text(agent.get("pane_id"))
                and _text(agent, "cwd", "foreground_cwd", "agent", "agent_status") for agent in agents),
            "herdr agent list answered an agent of the wrong shape")
    return agents


def _fleet(sessions: dict, now: datetime) -> tuple[list[dict], list[dict], str]:
    """Live worktrees and `sd-*` processes, each parsed for the join, and the process table's reason.

    A process the table cannot parse is that reason, and the worktrees still join.
    """
    _expect(isinstance(sessions, dict), "fleet collector returned no sessions document")
    trees, procs = sessions.get("worktrees"), sessions.get("processes")
    _expect(isinstance(trees, list) and isinstance(procs, list), "fleet collector returned an incomplete sessions document")
    # `created` is absent from a fleet child older than sd:3466; that is no start, not a wrong shape.
    _expect(all(isinstance(tree, dict) and isinstance(tree.get("live"), bool) and _is_text(tree.get("path"))
                and _is_text(tree.get("branch")) and _text(tree, "created") for tree in trees),
            "fleet collector returned a worktree of the wrong shape")
    parsed = []
    for tree in trees:
        if tree["live"]:
            number = BRANCH_ITEM.search(tree["branch"])
            parsed.append({"real": _real(tree["path"]), "name": Path(tree["path"]).name, "branch": tree["branch"],
                           "item": _item_id(number[1]) if number else None, "at": _when(tree.get("created"))})
    error = sessions.get("processes_error")
    if not isinstance(error, (str, type(None))):
        error = "it answered a reason that is not text"
    elif not all(isinstance(proc, dict) and all(_is_text(proc.get(field)) for field in ("pid", "elapsed", "command"))
                 and proc["command"].split() for proc in procs):
        error = "it answered a process of the wrong shape"
    rows = []
    if not error:
        try:
            for proc in procs:
                item, directory, name = _command(proc["command"].split())
                seconds = _etime(proc["elapsed"])
                rows.append({"item": item, "real": _real(directory), "key": paths.key(directory) if directory else "",
                             "session": f"{name} (pid {proc['pid']})",
                             "at": now - timedelta(seconds=seconds) if seconds is not None else None})
        except UNREAD as failure:
            rows, error = [], _reason(failure)
    return parsed, rows, f"the process table: {error}" if error else ""


def _agents(agents: list[dict]) -> list[dict]:
    """herdr's agents, each parsed for the join: its pane, a label and the resolved cwds."""
    return [{"pane": agent.get("pane_id"),
             "session": f"herdr pane {agent.get('pane_id')}, {agent.get('agent') or 'agent'} {agent.get('agent_status') or ''}".rstrip(),
             "reals": [_real(agent.get(key)) for key in ("foreground_cwd", "cwd") if agent.get(key)]} for agent in agents]


# -- the join ---------------------------------------------------------------------------------------------------------


def _command(argv: list[str]) -> tuple[int | None, str | None, str]:
    """A command line's `--item N`, its `-C <dir>`, and a short name: the command and its first word that is no option."""
    item = directory = None
    words, skip = [], False
    for index, word in enumerate(argv[1:], start=1):
        follows = argv[index + 1] if index + 1 < len(argv) else ""
        if skip:
            skip = False
        elif word in ("--item", "-C"):
            skip = True
            if word == "--item":
                item = _item_id(follows)
            else:
                directory = follows
        elif word.startswith("--item="):
            item = _item_id(word[7:])
        elif not word.startswith("-"):
            words.append(word)
    return item, directory, " ".join([Path(argv[0]).name, *words[:1]])


def _sessions(entries, launches, trees, procs, agents, now: datetime) -> list[dict]:
    """Every session the parsed sources tie to an item, done items included; `agents` None is herdr unread.

    Each source was parsed at its own boundary in `document`, so this only looks values up.
    """
    found: list[dict] = []

    def add(item, kind, session, started, via, verified=True):
        found.append({"item": item, "kind": kind, "session": session, "at": started, "via": via, "verified": verified})

    by_worktree = {entry["real"]: entry for entry in sorted(entries, key=lambda row: row["enqueued"]) if entry["real"]}
    joined: dict[str, int] = {}
    for tree in trees:
        entry = by_worktree.get(tree["real"])
        if entry:
            item, via = entry["item"], f"lane entry {entry['id']}"
        elif tree["item"] is not None:
            item, via = tree["item"], f"branch {tree['branch']}"
        else:
            continue
        joined[tree["real"]] = item
        add(item, "worktree", f"Worktree {tree['name']}", tree["at"], via)
    for entry in entries:
        if entry["status"] == "running":
            add(entry["item"], "lane", entry["session"], entry["started"], f"lane entry {entry['id']}")
        elif entry["status"] == "pending":
            add(entry["item"], "lane", "Lane queue (pending)", entry["queued"], f"lane entry {entry['id']}")
    for proc in procs:
        tree = _inside(proc["real"], joined)
        if proc["item"] is not None:
            add(proc["item"], "process", proc["session"], proc["at"], f"--item {proc['item']}")
        elif tree:
            add(joined[tree], "process", proc["session"], proc["at"], f"-C {proc['key']}")
    if agents is None:
        for pane, launch in launches.items():
            add(launch["item"], "agent", f"herdr pane {pane}, {launch['session']} session", launch["at"],
                f"Fix note {_iso(launch['at'])}; herdr not read", verified=False)
        return found
    for agent in agents:
        launch = launches.get(agent["pane"])
        trees_in = [tree for tree in (_inside(real, joined) for real in agent["reals"]) if tree]
        if launch:
            add(launch["item"], "agent", agent["session"], launch["at"], f"Fix note {_iso(launch['at'])}")
        elif trees_in:
            add(joined[trees_in[0]], "agent", agent["session"], None, f"cwd in worktree {Path(trees_in[0]).name}")
    return found


def document(connection, *, now: str, sessions: dict, sessions_error: str = "", herdr=None) -> dict:
    """`rows`, one per session, longest-running item first; `sources`, each source's reason when it was not read.

    `sessions` is the fleet's sessions document Now already read, and `sessions_error` the reason it could not be.
    `herdr` returns the live agents, `herdr_agents` by default, and is the seam a test fills. `error` says why no
    item could be read, so no row could be kept.

    Each source is read and parsed inside its own guard: whatever it raises (`UNREAD`) marks that source unread with
    the reason, and the others still answer. The join after them only looks values up; a failure there is `error`.
    """
    sources = {"fleet": sessions_error, "lane": "", "launch": "", "herdr": ""}
    clock = _when(now) or datetime.now(timezone.utc)

    def read(source, reader, empty):
        try:
            return reader()
        except UNREAD as failure:
            sources[source] = _reason(failure)
            return empty

    trees, procs = [], []
    fleet = None if sessions_error else read("fleet", lambda: _fleet(sessions, clock), None)
    if fleet:
        trees, procs, sources["fleet"] = fleet
    entries = read("lane", lambda: _lane(connection), [])
    launches = read("launch", lambda: _launches(connection), {})
    try:
        # Every item a row can name is in this first pass or a launch note: herdr only ties agents to those.
        probe = _sessions(entries, launches, trees, procs, [], clock)
    except UNREAD as failure:
        return {"rows": [], "sources": sources, "error": f"the join failed: {_reason(failure)}"}
    try:
        items = {number: reads.item_by_id(connection, number)
                 for number in {row["item"] for row in probe} | {launch["item"] for launch in launches.values()}}
    except UNREAD as failure:
        return {"rows": [], "sources": sources, "error": f"the items could not be read: {_reason(failure)}"}
    open_items = {number for number, item in items.items() if item is not None and item["status"] != "done"}
    # herdr is asked only when an agent could be tied to an open item: a launch note, or a worktree that names one.
    agents = []
    if launches or any(row["kind"] == "worktree" and row["item"] in open_items for row in probe):
        agents = read("herdr", lambda: _agents((herdr or herdr_agents)()), None)
    try:
        found = _sessions(entries, launches, trees, procs, agents, clock)
    except UNREAD as failure:
        return {"rows": [], "sources": sources, "error": f"the join failed: {_reason(failure)}"}
    rows = []
    for row in found:
        if row["item"] not in open_items:
            continue
        item = items[row["item"]]
        seconds = int((clock - row["at"]).total_seconds()) if row["at"] else None
        rows.append({"item": row["item"], "title": item["title"], "status": item["status"], "kind": row["kind"],
                     "session": row["session"], "started": _iso(row["at"]), "elapsed": elapsed(seconds) if seconds is not None else "",
                     "seconds": seconds, "via": row["via"], "verified": row["verified"]})
    first = {}
    for row in rows:
        first[row["item"]] = max(first.get(row["item"], -1), row["seconds"] if row["seconds"] is not None else -1)
    rows.sort(key=lambda row: (-first[row["item"]], row["item"],
                               -(row["seconds"] if row["seconds"] is not None else -1), KINDS.index(row["kind"])))
    return {"rows": rows, "sources": sources, "error": ""}
