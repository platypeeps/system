"""Today's Working now (sd:3466): each open item's live sessions, when each started and how long it has run.

Five readers, each one the tree already had, joined per work item; this module adds only the join:

  lane       the `sd-ship lane` entries: the latest `state` row (kind `checkpoint`) of each key `lane:v1:<repo>:<id>`,
             as the pack's `sd_lane.stored` reads one repository's. A `running` entry is a session from its `started_at`,
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

A source that could not be read is a reason in `sources`, never an empty list. A launch note whose pane herdr could not
be asked about is a row with `verified` false; one whose pane herdr answered without is a session that ended, and no row.
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
#: The lane's key prefix, the pack's `sd_lane.KEY_PREFIX`.
LANE_PREFIX = "lane:v1:"
#: An item number in a branch name: digits after the start, `sd`, `-`, `_` or `/`, and before the end or a separator.
BRANCH_ITEM = re.compile(r"(?:^|[-_/]|sd)(\d+)(?=$|[-_/])")
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
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


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
    found = re.fullmatch(r"(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)", text.strip())
    if not found:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in found.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _real(path) -> str:
    return os.path.realpath(os.path.expanduser(path)) if isinstance(path, str) and path else ""


def _inside(path: str, joined: dict[str, int]) -> str | None:
    """The joined worktree `path` is in, or None."""
    real = _real(path)
    return next((tree for tree in joined if real == tree or real.startswith(tree + os.sep)), None)


# -- the readers ------------------------------------------------------------------------------------------------------


def _lane(connection) -> list[dict]:
    rows = connection.execute(
        "SELECT body FROM state WHERE id IN (SELECT MAX(id) FROM state WHERE kind = 'checkpoint' "
        "AND substr(key, 1, ?) = ? GROUP BY key)", (len(LANE_PREFIX), LANE_PREFIX)).fetchall()
    entries = [json.loads(row[0]) for row in rows]
    return [entry for entry in entries if isinstance(entry, dict) and type(entry.get("item")) is int]


def _launches(connection) -> dict[str, dict]:
    """The newest launch note per herdr pane, on items that are not done."""
    prefix = fix_session.NOTE.split("{", 1)[0]
    rows = connection.execute(
        "SELECT note.item, note.timestamp, note.body FROM note JOIN item ON item.id = note.item "
        "WHERE note.kind = 'comment' AND substr(note.body, 1, ?) = ? AND item.status != 'done' ORDER BY note.id",
        (len(prefix), prefix)).fetchall()
    out = {}
    for item, stamp, body in rows:
        found = LAUNCH.match(body)
        if found and found["pane"] != "unknown":
            out[found["pane"]] = {"item": item, "at": _when(stamp), "session": found["session"]}
    return out


def _wrap():
    """`local-herdr/herdr_wrap.py`, loaded by path as `fleet` loads `collectors.py`: its session rule is the one Fix uses."""
    spec = importlib.util.spec_from_file_location("sd_dashboard_herdr_wrap", fix_session.CHECKOUT / "local-herdr" / "herdr_wrap.py")
    if spec is None or spec.loader is None:
        raise ValueError("local-herdr/herdr_wrap.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _herdr(args: list[str], environment: dict) -> dict:
    said = "herdr " + " ".join(args)
    try:
        done = subprocess.run(["herdr", *args], env=environment, capture_output=True, text=True,
                              timeout=HERDR_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise ValueError(f"{said} gave no answer in {HERDR_SECONDS} s") from None
    except OSError as error:
        raise ValueError(f"{said} did not run: {error}") from None
    for stream in (done.stdout, done.stderr):
        try:
            answer = json.loads(stream)
        except ValueError:
            continue
        if isinstance(answer, dict):
            if answer.get("error"):
                raise ValueError(f"{said}: {answer['error']}")
            return answer
    raise ValueError(f"{said}: exit {done.returncode}, no JSON answer")


def herdr_agents() -> list[dict]:
    """Live agents in the herdr session Fix launches into; [] when herdr is not installed or that session is not running."""
    if shutil.which("herdr") is None:
        return []
    wrap = _wrap()
    session = wrap.session_name()
    environment = wrap.herdr_env(session)
    listed = _herdr(["session", "list", "--json"], environment)
    entry = next((row for row in listed.get("sessions") or [] if isinstance(row, dict) and row.get("name") == session), None)
    if not entry or not entry.get("running"):
        return []
    if entry.get("socket_path"):
        environment["HERDR_SOCKET_PATH"] = entry["socket_path"]
    agents = (_herdr(["agent", "list"], environment).get("result") or {}).get("agents")
    if not isinstance(agents, list):
        raise ValueError("herdr agent list answered no agent list")  # noqa: TRY004 - external document validation
    return [agent for agent in agents if isinstance(agent, dict)]


def _fleet(sessions: dict) -> tuple[list[dict], list[dict], str]:
    trees, procs = sessions.get("worktrees"), sessions.get("processes")
    if not isinstance(trees, list) or not isinstance(procs, list):
        raise ValueError("fleet collector returned an incomplete sessions document")  # noqa: TRY004 - external document validation
    trees = [tree for tree in trees if isinstance(tree, dict) and tree.get("live") is True
             and isinstance(tree.get("path"), str) and isinstance(tree.get("branch"), str)]
    procs = [proc for proc in procs if isinstance(proc, dict)
             and all(isinstance(proc.get(field), str) for field in ("pid", "elapsed", "command"))]
    error = sessions.get("processes_error")
    return trees, procs, f"the process table: {error}" if isinstance(error, str) and error else ""


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
            if word == "--item" and follows.isdigit():
                item = int(follows)
            elif word == "-C":
                directory = follows
        elif word.startswith("--item=") and word[7:].isdigit():
            item = int(word[7:])
        elif not word.startswith("-"):
            words.append(word)
    return item, directory, " ".join([Path(argv[0]).name, *words[:1]])


def _sessions(entries, launches, trees, procs, agents, now: datetime) -> list[dict]:
    """Every session the sources tie to an item, done items included; `agents` None is herdr unread."""
    found: list[dict] = []

    def add(item, kind, session, started, via, verified=True):
        found.append({"item": item, "kind": kind, "session": session, "at": started, "via": via, "verified": verified})

    by_worktree = {}
    for entry in sorted(entries, key=lambda row: str(row.get("enqueued_at") or "")):
        if _real(entry.get("worktree")):
            by_worktree[_real(entry["worktree"])] = entry
    joined: dict[str, int] = {}
    for tree in trees:
        real = _real(tree["path"])
        entry = by_worktree.get(real)
        number = BRANCH_ITEM.search(tree["branch"])
        if entry:
            item, via = entry["item"], f"lane entry {entry.get('id')}"
        elif number:
            item, via = int(number[1]), f"branch {tree['branch']}"
        else:
            continue
        joined[real] = item
        add(item, "worktree", f"Worktree {Path(tree['path']).name}", _when(tree.get("created")), via)
    for entry in entries:
        if entry.get("status") == "running":
            holder = entry.get("holder") if isinstance(entry.get("holder"), dict) else {}
            add(entry["item"], "lane", f"Lane {entry.get('step') or 'run'} on {holder.get('host') or 'an unnamed host'}",
                _when(entry.get("started_at")), f"lane entry {entry.get('id')}")
        elif entry.get("status") == "pending":
            add(entry["item"], "lane", "Lane queue (pending)", _when(entry.get("enqueued_at")), f"lane entry {entry.get('id')}")
    for proc in procs:
        argv = proc["command"].split()
        item, directory, name = _command(argv)
        tree = _inside(directory, joined) if directory else None
        if item is not None:
            via = f"--item {item}"
        elif tree:
            item, via = joined[tree], f"-C {paths.key(directory)}"
        else:
            continue
        seconds = _etime(proc["elapsed"])
        add(item, "process", f"{name} (pid {proc['pid']})", now - timedelta(seconds=seconds) if seconds is not None else None, via)
    if agents is None:
        for pane, launch in launches.items():
            add(launch["item"], "agent", f"herdr pane {pane}, {launch['session']} session", launch["at"],
                f"Fix note {_iso(launch['at'])}; herdr not read", verified=False)
        return found
    for agent in agents:
        pane, launch = agent.get("pane_id"), launches.get(agent.get("pane_id"))
        session = f"herdr pane {pane}, {agent.get('agent') or 'agent'} {agent.get('agent_status') or ''}".rstrip()
        trees_in = [tree for tree in (_inside(agent.get(key), joined) for key in ("foreground_cwd", "cwd") if agent.get(key)) if tree]
        if launch:
            add(launch["item"], "agent", session, launch["at"], f"Fix note {_iso(launch['at'])}")
        elif trees_in:
            add(joined[trees_in[0]], "agent", session, None, f"cwd in worktree {Path(trees_in[0]).name}")
    return found


def document(connection, *, now: str, sessions: dict, sessions_error: str = "", herdr=None) -> dict:
    """`rows`, one per session, longest-running item first; `sources`, each source's reason when it was not read.

    `sessions` is the fleet's sessions document Now already read, and `sessions_error` the reason it could not be.
    `herdr` returns the live agents, `herdr_agents` by default, and is the seam a test fills. `error` says why no
    item could be read, so no row could be kept.
    """
    sources = {"fleet": sessions_error, "lane": "", "launch": "", "herdr": ""}
    trees, procs = [], []
    if not sessions_error:
        try:
            trees, procs, sources["fleet"] = _fleet(sessions)
        except ValueError as failure:
            sources["fleet"] = str(failure)
    try:
        entries = _lane(connection)
    except (SdDbError, sqlite3.Error, ValueError) as failure:
        entries, sources["lane"] = [], str(failure) or "the lane entries could not be read"
    try:
        launches = _launches(connection)
    except (SdDbError, sqlite3.Error) as failure:
        launches, sources["launch"] = {}, str(failure) or "the Fix notes could not be read"
    clock = _when(now) or datetime.now(timezone.utc)
    # Every item a row can name is in this first pass or a launch note: herdr only ties agents to those.
    probe = _sessions(entries, launches, trees, procs, [], clock)
    try:
        items = {number: reads.item_by_id(connection, number)
                 for number in {row["item"] for row in probe} | {launch["item"] for launch in launches.values()}}
    except (SdDbError, sqlite3.Error) as failure:
        return {"rows": [], "sources": sources, "error": str(failure) or "the items could not be read"}
    open_items = {number for number, item in items.items() if item is not None and item["status"] != "done"}
    # herdr is asked only when an agent could be tied to an open item: a launch note, or a worktree that names one.
    agents = []
    if launches or any(row["kind"] == "worktree" and row["item"] in open_items for row in probe):
        try:
            agents = (herdr or herdr_agents)()
        except (ValueError, OSError) as failure:
            agents, sources["herdr"] = None, str(failure) or "herdr could not be read"
    found = _sessions(entries, launches, trees, procs, agents, clock)
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
