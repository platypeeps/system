"""Tasks > Start next (sd:5107): start builders on the next ready items, within the builder and load caps.

`plan` is what the dialog shows before anything starts. The next items are the `ready` rows of `reads.backlog_items`,
in its order, less reports, items a Fix note says were started already, items a live Claude session names
(`claude_sessions`), and items with no checkout on this Mac (`fix_session.checkout`); each one left out is in
`skipped` with its reason. The room is the builder cap, `CAP` per machine, less the builders running now
(`claude_sessions.builders`), and none while the five-minute load is above `sd gate status --json`'s `load_max`.
A count or a load that could not be read is a refusal with its reason, never room.

`run` is the start, in steps, under `STARTING`:

1. A fresh `plan`. A refusal, any item asked for that is no longer in it, or more items than the room: nothing
   starts, and the answer says why (a 400, `WorkflowError`).
2. For each item in turn, `fix_session.start` with the revision the dialog read: a herdr workspace with claude as a
   builder, then the Fix note on the item. The first item that does not start stops the run; the items after it are
   `not_started`. Each started item has its note, so a second run leaves it out (step 1's plan), and a session whose
   note was not written still names its item in its first prompt, which `claude_sessions` reads.
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone

from sd_db import reads, workflow
from sd_db.errors import SdDbError

from . import claude_sessions, fix_session
from .active_sessions import Unread, _reason, elapsed

__all__ = ["CAP", "plan", "request", "run"]

#: Code-writing builders per machine: the operator's standing rule (at most 6 at once).
CAP = 6
GATE_SECONDS = 15
#: One run at a time, so two dialogs cannot both spend the same room.
STARTING = threading.Lock()
UNREAD = claude_sessions.UNREAD


def gate_status() -> dict:
    """`sd gate status --json`: the load averages and the load cap the gate admits under."""
    sd = shutil.which("sd")
    if sd is None:
        raise Unread("sd is not on PATH, so the load was not read")
    try:
        done = subprocess.run([sd, "gate", "status", "--json"], capture_output=True, text=True, timeout=GATE_SECONDS,
                              check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Unread(f"sd gate status gave no answer in {GATE_SECONDS} s") from None
    if done.returncode != 0:
        raise Unread(f"sd gate status failed: exit {done.returncode}")
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise Unread("sd gate status answered no JSON") from None


def _load(gate) -> tuple[float, float]:
    answer = (gate or gate_status)()
    load, cap = answer.get("load") if isinstance(answer, dict) else None, answer.get("load_max") if isinstance(answer, dict) else None
    # NaN compares false with everything, so a NaN load would never be above the cap: it is unread, like inf.
    if not (isinstance(load, list) and len(load) >= 2 and all(isinstance(x, (int, float)) and math.isfinite(x) for x in load[:2])
            and isinstance(cap, (int, float)) and math.isfinite(cap)):
        raise Unread("sd gate status gave no load or load cap")
    return float(load[1]), float(cap)


def _holders(holders: list[dict]) -> str:
    """The sessions holding the slots, by state, stalled first: `. Stalled: traces-research b4211 (3h); …`."""
    if not holders:
        return ""
    groups: dict[str, list[str]] = {}
    for holder in holders:
        name = f"{holder['repo']} {holder['agent'] or 'Fix session ' + holder['session']}"
        age = f" ({elapsed(holder['age'])})" if isinstance(holder.get("age"), int) else ""
        groups.setdefault(holder["state"], []).append(name + age)
    return "".join(f". {state.capitalize()}: {', '.join(names)}" for state, names in groups.items()) + \
        ". End a session in Management > Sessions to free its slots"


def plan(connection, *, now: str, sessions=None, gate=None) -> dict:
    """What Start next would start now: `start`, the items in order with their revisions; `room`; `refusal`.

    `sessions` is `claude_sessions.document` and `gate` is `gate_status`, the seams a test fills.
    """
    claude = (sessions or claude_sessions.document)(connection, now=now)
    running, why = claude["builders"]["running"], claude["builders"]["why"]
    named = {task["item"] for row in claude["sessions"] or [] for task in row["tasks"]}
    launched = {row["item"] for row in reads.launch_notes(connection, fix_session.NOTE.split("{", 1)[0])}
    eligible, skipped = [], []
    for row in reads.backlog_items(connection, status="ready", now=now):
        if row["kind"] == "report":
            continue
        reason = ("Fix started a session for it already" if row["id"] in launched
                  else "a live Claude session works on it" if row["id"] in named else "")
        if not reason:
            try:
                fix_session.checkout(dict(row))
            except workflow.WorkflowError as problem:
                reason = str(problem)
        (skipped if reason else eligible).append({"id": row["id"], "title": row["title"], "repo": row["repo"]}
                                                 | ({"why": reason} if reason else {}))
    revisions = reads.revisions(connection, [row["id"] for row in eligible]) if eligible else {}
    for row in eligible:
        row["revision"] = revisions[row["id"]]
    load = load_max = None
    refusal = ""
    try:
        load, load_max = _load(gate)
    except UNREAD as failure:
        refusal = f"the load was not read: {_reason(failure)}"
    if running is None:
        refusal = refusal or f"the running builders were not counted: {why}"
    elif running >= CAP:
        refusal = refusal or f"{running} builders run already; the cap is {CAP}" + _holders(claude["builders"].get("holders") or [])
    if load is not None and load > load_max:
        refusal = refusal or f"the five-minute load is {load:g}, above the cap of {load_max:g}"
    if not eligible and not refusal:
        refusal = "no ready item is left to start"
    room = 0 if refusal else min(CAP - running, len(eligible))
    return {"read": now, "cap": CAP, "running": running, "load": load, "load_max": load_max, "room": room,
            "refusal": refusal, "start": eligible[:room], "eligible": eligible, "skipped": skipped,
            "holders": claude["builders"].get("holders") or [] if running is not None and running >= CAP else []}


def run(connection, *, now: str, items: list[dict], who: str, sessions=None, gate=None, launch=None) -> dict:
    """Steps 1 and 2 above: `started`, `failed` (the item and the reason, or None) and `not_started`."""
    with STARTING:
        fresh = plan(connection, now=now, sessions=sessions, gate=gate)
        if fresh["refusal"]:
            raise workflow.WorkflowError(f"Nothing started: {fresh['refusal']}.")
        ready = {row["id"] for row in fresh["eligible"]}
        gone = [item["id"] for item in items if item["id"] not in ready]
        if gone:
            why = {row["id"]: row["why"] for row in fresh["skipped"]}
            raise workflow.WorkflowError("Nothing started: " + "; ".join(
                f"#{item} {why.get(item, 'is not ready')}" for item in gone) + "; read the plan again.")
        if len(items) > fresh["room"]:
            raise workflow.WorkflowError(f"Nothing started: {fresh['room']} builders fit now, not {len(items)}; read the plan again.")
        started, failed = [], None
        for index, item in enumerate(items):
            try:
                out = (launch or fix_session.start)(connection, item["id"], expected_revision=item["revision"], who=who)
            except (workflow.WorkflowError, SdDbError, sqlite3.Error, OSError) as problem:
                out = (409 if isinstance(problem, workflow.StaleItem) else 400, {"error": str(problem)})
            if isinstance(out, tuple):
                failed = {"id": item["id"], "status": out[0], "error": out[1].get("error") or "it did not start"}
                return {"started": started, "failed": failed, "not_started": [rest["id"] for rest in items[index + 1:]]}
            started.append({"id": item["id"]} | out.get("fix", {}))
        return {"started": started, "failed": None, "not_started": []}


def request(values: dict):
    """The POST's checks, before the server opens a connection: `items`, each `{id, revision}`, at most `CAP`."""
    items = values.get("items")
    if (set(values) != {"items"} or not isinstance(items, list) or not 1 <= len(items) <= CAP
            or any(not isinstance(item, dict) or set(item) != {"id", "revision"} or type(item["id"]) is not int
                   or not 1 <= item["id"] <= 9223372036854775807 or not isinstance(item["revision"], str) for item in items)
            or len({item["id"] for item in items}) != len(items)):
        raise ValueError(f"Start next takes items: 1 to {CAP} distinct {{id, revision}} pairs from its plan.")
    return lambda connection: run(connection, now=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), items=items,
                                  who="dashboard")
