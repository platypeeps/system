"""Management > Sessions: each Claude session on this Mac, what it works on, and whether it may exit (sd:5106).

No ledger records a session; each fact is read from what Claude Code, git and `ps` already keep:

  agents      `claude agents --json`: each live session's pid, cwd, start, id, name and `status` (`busy`, `idle` or
              `waiting`, with `waitingFor` when a permission prompt or a question holds it). `waiting` is "waits on
              the operator".
  transcripts `<claude home>/projects/*/<session id>.jsonl` and its `subagents/agent-*.jsonl` with `.meta.json`. The
              newest write of either is the age of the last action. A subagent whose last turn record is not a
              finished answer is running; one untouched for `STALL` seconds is stalled, not running. A session whose
              first prompt is Fix's builder prompt (`fix_session.BUILD`) is a builder itself.
  processes   one `ps`: an `sd-ship ... lane watch` that descends from the session's pid is its lane watch.
  git         the session's cwd and each live worktree (`fleet.collect("sessions")`) whose branch names an item its
              builders work on: uncommitted files and commits no remote-tracking ref holds.

The current task is the item the cwd's branch names, the item Fix's prompt names, and each item a builder's
description names (`sd:N`); each carries its `via`. Safe to exit is `yes` only when the session is idle, nothing
runs under it, and no checkout above holds unpushed work; a fact that could not be read makes it `unknown` with
the reason, never `yes`. Sessions on other machines are not listed: `claude agents` lists this Mac's.

`builders` counts the builders running on this Mac, for Tasks' Start next (sd:5107): running builder subagents and
busy or waiting Fix sessions. A source that could not be read makes it None with the reason.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sd_db.errors import SdDbError

from . import fix_session
from .active_sessions import BRANCH_ITEM, Unread, _item_id, _reason, elapsed

__all__ = ["builders", "document", "list_agents", "list_processes", "read_work"]

#: One `claude agents --json`, `ps` or git call's bound; no answer within it is unknown, not empty.
CLAUDE_SECONDS = 10
GIT_SECONDS = 10
#: A running subagent writes its transcript at each step; a test run of 10 minutes writes nothing, so the bound is wider.
STALL = 30 * 60
#: How much of a transcript's end is read for its last turn record, and the most read when one line is longer.
TAIL_BYTES = 1 << 18
TAIL_MAX = 1 << 22
#: How much of a transcript's start is read for its first prompt.
HEAD_BYTES = 1 << 16
STATUSES = ("busy", "idle", "waiting")
SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{7,63}", re.ASCII)
ITEM_TEXT = re.compile(r"\bsd:(\d{1,9})\b", re.ASCII)
#: An item number in an agent's name: `b5106-sessions`, `sys-4914-4912`, `sd3456`.
NAME_ITEM = re.compile(r"(?:^|[-_/])(?:sd|b)?(\d{1,9})(?=$|[-_/])", re.ASCII)
#: Fix's builder prompt, read back: `fix_session.BUILD` up to its id.
BUILD = re.compile(re.escape(fix_session.BUILD.split("{id}", 1)[0]) + r"(\d{1,9})\.", re.ASCII)
WATCH = re.compile(r"(?:^|[\s/])sd-ship(?:\s+-C\s+\S+)?\s+lane\s+watch\b")
#: What a source's read may raise: each marks that fact unknown, and the rest still answer.
UNREAD = (ValueError, OverflowError, TypeError, KeyError, IndexError, AttributeError, OSError, sqlite3.Error, SdDbError)


# -- the readers: each is a seam a test replaces ------------------------------------------------------------------------


def _claude() -> str:
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    if fallback.is_file():
        return str(fallback)
    raise Unread("claude is not on PATH or at ~/.local/bin/claude, so no session could be listed")


def list_agents() -> list[dict]:
    """`claude agents --json`: this Mac's live sessions, interactive and background."""
    try:
        done = subprocess.run([_claude(), "agents", "--json"], capture_output=True, text=True,
                              timeout=CLAUDE_SECONDS, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Unread(f"claude agents --json gave no answer in {CLAUDE_SECONDS} s") from None
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}").splitlines()[-1]
        raise Unread(f"claude agents --json failed: {said}")
    try:
        listed = json.loads(done.stdout)
    except ValueError:
        raise Unread("claude agents --json answered no JSON") from None
    return listed


def list_processes() -> list[tuple[int, int, str]]:
    """One `ps`: every process's pid, parent pid and command line."""
    try:
        done = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True,
                              timeout=CLAUDE_SECONDS, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Unread(f"ps gave no answer in {CLAUDE_SECONDS} s") from None
    if done.returncode != 0:
        raise Unread(f"ps failed: exit {done.returncode}")
    rows = []
    for line in done.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) == 3 else ""))
    return rows


def _git(path: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=GIT_SECONDS,
                              check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Unread(f"git {args[0]} in {path} gave no answer in {GIT_SECONDS} s") from None


def read_work(path: str) -> dict | None:
    """A checkout's branch, uncommitted files and unpushed commits; None when `path` is no git checkout."""
    top = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if top.returncode != 0:
        if "not a git repository" in top.stderr:
            return None
        raise Unread(f"git rev-parse in {path} failed: {top.stderr.strip() or top.returncode}")
    status = _git(path, "status", "--porcelain")
    ahead = _git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
    if status.returncode != 0 or ahead.returncode != 0 or not ahead.stdout.strip().isdigit():
        raise Unread(f"git status or rev-list in {path} failed")
    return {"branch": top.stdout.strip(), "dirty": len(status.stdout.splitlines()), "unpushed": int(ahead.stdout.strip())}


# -- transcripts ----------------------------------------------------------------------------------------------------------


def _transcript(root: Path, session: str) -> Path | None:
    if not SESSION_ID.fullmatch(session):
        return None
    return next(iter(sorted((root / "projects").glob(f"*/{session}.jsonl"))), None)


def _records_from_end(path: Path):
    """The transcript's records, last first, from at most `TAIL_MAX` bytes of its end."""
    size = path.stat().st_size
    span = TAIL_BYTES
    while True:
        with path.open("rb") as handle:
            handle.seek(max(size - span, 0))
            chunk = handle.read(span)
        lines = chunk.split(b"\n")
        if size > span:
            lines = lines[1:]  # the first line was cut
        records = []
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        if records or size <= span or span >= TAIL_MAX:
            return records
        span *= 4


def _turn(path: Path) -> str:
    """`running` when the last turn record is not a finished answer, `idle` when it is, `unknown` when none was read."""
    for record in _records_from_end(path):
        kind = record.get("type")
        if kind == "user":
            return "running"
        if kind == "assistant":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            asks = any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content)
            return "idle" if message.get("stop_reason") == "end_turn" and not asks else "running"
    return "unknown"


def _first_prompt(path: Path) -> str:
    with path.open("rb") as handle:
        head = handle.read(HEAD_BYTES)
    for line in head.split(b"\n"):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "user":
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(block.get("text", "") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str))
        return ""
    return ""


def _subagents(transcript: Path, clock: float) -> list[dict]:
    folder = transcript.with_suffix("") / "subagents"
    if not folder.is_dir():
        return []
    found = []
    for meta_path in sorted(folder.glob("agent-*.meta.json")):
        log = meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".jsonl")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise Unread(f"{meta_path.name} is not an object")
        when = log.stat().st_mtime if log.is_file() else meta_path.stat().st_mtime
        state = "idle"
        if log.is_file() and clock - when < STALL:
            state = _turn(log)
        elif log.is_file() and _turn(log) == "running":
            state = "stalled"
        kinds = " ".join(str(meta.get(key) or "") for key in ("agentType", "customAgentType", "spawnedAgentType"))
        numbers = ITEM_TEXT.findall(str(meta.get("description") or "")) + NAME_ITEM.findall(str(meta.get("name") or ""))
        found.append({"name": str(meta.get("name") or meta.get("agentType") or log.stem), "builder": "builder" in kinds,
                      "description": str(meta.get("description") or ""), "state": state, "last": when,
                      "items": sorted({number for number in map(_item_id, numbers) if number is not None})})
    return found


# -- the join ---------------------------------------------------------------------------------------------------------------


def _iso(seconds: float | None) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if seconds is not None else ""


def _clock(now: str) -> float:
    try:
        value = datetime.fromisoformat(now.replace("Z", "+00:00"))
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    except (AttributeError, ValueError, OverflowError):
        return datetime.now(timezone.utc).timestamp()


def _descendants(rows: list[tuple[int, int, str]], pid: int) -> list[tuple[int, str]]:
    children: dict[int, list[tuple[int, str]]] = {}
    for child, parent, command in rows:
        children.setdefault(parent, []).append((child, command))
    out, todo, seen = [], [pid], {pid}
    while todo:
        for child, command in children.get(todo.pop(), []):
            if child not in seen:
                seen.add(child)
                out.append((child, command))
                todo.append(child)
    return out


def _session(raw: dict, *, root: Path, clock: float, procs, procs_error: str, trees, trees_error: str, work) -> dict:
    session = str(raw.get("sessionId") or "")
    pid = raw.get("pid") if type(raw.get("pid")) is int else None
    cwd = raw.get("cwd") if isinstance(raw.get("cwd"), str) else ""
    status = raw.get("status") if raw.get("status") in STATUSES else "unknown"
    started = raw.get("startedAt") / 1000 if isinstance(raw.get("startedAt"), (int, float)) else None
    no, unknown, tasks = [], [], []

    def task(item, via):
        if item is not None and all(t["item"] != item or t["via"] != via for t in tasks):
            tasks.append({"item": item, "via": via})

    agents, last, builder = [], None, False
    transcript = _transcript(root, session) if session else None
    if transcript is None:
        unknown.append("no transcript was found, so its last action and builders were not read")
    else:
        try:
            last = transcript.stat().st_mtime
            prompt = BUILD.search(_first_prompt(transcript))
            if prompt:
                builder = True
                task(_item_id(prompt[1]), "Fix builder prompt")
            agents = _subagents(transcript, clock)
        except UNREAD as failure:
            unknown.append(f"its transcript could not be read: {_reason(failure)}")
    for agent in agents:
        last = max(last or 0, agent["last"])
        if agent["state"] == "running":
            no.append(f"{'builder' if agent['builder'] else 'agent'} {agent['name']} is running")
            for item in agent["items"]:
                task(item, f"builder {agent['name']}" if agent["builder"] else f"agent {agent['name']}")
    if status == "busy":
        no.append("it is working on a turn")
    elif status == "waiting":
        no.append("it waits on you" + (f": {raw['waitingFor']}" if isinstance(raw.get("waitingFor"), str) and raw["waitingFor"] else ""))
    elif status == "unknown":
        unknown.append("claude agents gave no status")

    watches = []
    if procs_error:
        unknown.append(f"the process table was not read: {procs_error}")
    elif pid is not None:
        watches = [{"pid": child, "command": command} for child, command in _descendants(procs, pid) if WATCH.search(command)]
        no.extend(f"lane watch pid {watch['pid']} runs under it" for watch in watches)

    # The checkouts whose unpushed work the session answers for: its cwd, and the worktrees its builders' items name.
    paths = [cwd] if cwd else []
    items = {item for agent in agents if agent["builder"] for item in agent["items"]}
    if items and trees_error:
        unknown.append(f"its builders' worktrees were not read: {trees_error}")
    for tree in trees:
        found = BRANCH_ITEM.search(tree["branch"])
        if found and _item_id(found[1]) in items and tree["path"] not in paths:
            paths.append(tree["path"])
    checkouts = []
    for path in paths:
        try:
            state = work(path)
        except UNREAD as failure:
            unknown.append(f"{path} was not read: {_reason(failure)}")
            checkouts.append({"path": path, "error": _reason(failure)})
            continue
        if state is None:
            continue
        checkouts.append({"path": path, "branch": state["branch"], "dirty": state["dirty"], "unpushed": state["unpushed"]})
        if path == cwd:
            found = BRANCH_ITEM.search(state["branch"])
            task(_item_id(found[1]) if found else None, f"branch {state['branch']}")
        held = [f"{count} {words}{'' if count == 1 else 's'}"
                for count, words in ((state["dirty"], "uncommitted file"), (state["unpushed"], "unpushed commit")) if count]
        if held:
            no.append(f"{path} holds {' and '.join(held)}")
    safe = "no" if no else "unknown" if unknown else "yes"
    age = int(clock - last) if last is not None else None
    return {"id": session, "name": str(raw.get("name") or ""), "kind": str(raw.get("kind") or ""), "pid": pid, "cwd": cwd,
            "started": _iso(started), "status": status,
            "waiting": status == "waiting",
            "waiting_for": raw.get("waitingFor") if isinstance(raw.get("waitingFor"), str) else "",
            "last_action": _iso(last), "age": age, "age_text": elapsed(age) if age is not None else "",
            "builder": builder, "tasks": tasks,
            "agents": [{key: agent[key] for key in ("name", "builder", "state", "items", "description")} | {"last": _iso(agent["last"])}
                       for agent in agents if agent["state"] != "idle"],
            "watches": watches, "checkouts": checkouts,
            "safe": {"state": safe, "why": no + unknown}}


def builders(sessions: list[dict] | None, why: str) -> dict:
    """How many builders run on this Mac: running builder subagents and busy or waiting Fix sessions; None when not known."""
    if sessions is None:
        return {"running": None, "why": why or "no session was listed"}
    blind = [row["name"] or row["id"] for row in sessions
             if any(reason.startswith(("no transcript", "its transcript")) for reason in row["safe"]["why"])]
    if blind:
        return {"running": None, "why": f"the transcripts of {', '.join(blind)} were not read"}
    count = sum(1 for row in sessions for agent in row["agents"] if agent["builder"] and agent["state"] == "running")
    count += sum(1 for row in sessions if row["builder"] and row["status"] in ("busy", "waiting"))
    return {"running": count, "why": ""}


def document(connection, *, now: str, fleet=None, agents=None, processes=None, work=None, root=None) -> dict:
    """`sessions`, one per live Claude session, the ones waiting on the operator first; `sources`; `builders`.

    Every source is a seam: `fleet` is `fleet.collect`, `agents`, `processes` and `work` are `list_agents`,
    `list_processes` and `read_work`, and `root` is the Claude home (`~/.claude`).
    """
    clock = _clock(now)
    sources = {"agents": "", "processes": "", "worktrees": ""}
    root = Path(root) if root is not None else Path.home() / ".claude"
    try:
        listed = (agents or list_agents)()
        if not isinstance(listed, list) or any(not isinstance(row, dict) for row in listed):
            raise Unread("claude agents --json answered no session list")
    except UNREAD as failure:
        sources["agents"] = _reason(failure)
        return {"read": now, "sessions": None, "sources": sources, "builders": builders(None, sources["agents"])}
    procs = []
    try:
        procs = (processes or list_processes)()
    except UNREAD as failure:
        sources["processes"] = _reason(failure)
    trees = []
    try:
        if fleet is None:
            from . import fleet as fleet_module
            fleet = fleet_module.collect
        rows = fleet("sessions").get("worktrees")
        if not isinstance(rows, list):
            raise Unread("fleet collector returned no worktrees")
        trees = [tree for tree in rows if isinstance(tree, dict) and tree.get("live") is True
                 and isinstance(tree.get("path"), str) and isinstance(tree.get("branch"), str)]
    except UNREAD as failure:
        sources["worktrees"] = _reason(failure)
    sessions = [_session(raw, root=root, clock=clock, procs=procs, procs_error=sources["processes"], trees=trees,
                         trees_error=sources["worktrees"], work=work or read_work) for raw in listed]
    numbers = sorted({task["item"] for row in sessions for task in row["tasks"]})
    titles = None
    if numbers and connection is not None:
        marks = ", ".join("?" * len(numbers))
        try:
            titles = {row[0]: (row[1], row[2]) for row in connection.execute(
                f"SELECT id, title, status FROM item WHERE id IN ({marks})", numbers)}
        except (sqlite3.Error, SdDbError):
            titles = None
    for row in sessions:
        # A number no item holds is a name that only looked like one; with the items unread, each stays untitled.
        row["tasks"] = [task | {"title": "", "status": ""} if titles is None
                        else task | {"title": titles[task["item"]][0], "status": titles[task["item"]][1]}
                        for task in row["tasks"] if titles is None or task["item"] in titles]
    rank = {"waiting": 0, "unknown": 1, "busy": 2, "idle": 3}
    sessions.sort(key=lambda row: (rank[row["status"]], row["age"] if row["age"] is not None else 1 << 62, row["id"]))
    return {"read": now, "sessions": sessions, "sources": sources, "builders": builders(sessions, "")}
