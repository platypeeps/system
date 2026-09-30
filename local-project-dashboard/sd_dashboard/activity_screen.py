"""Activity: one timeline of the last 24 hours, behind the default UI's Activity page (sd:2111).

The page is `v2/activity.html`; it holds no rows. It reads one JSON
document, `/api/activity`, built here from records the library already
keeps, so the port adds no collector:

- **merge**: the `comment` note `ship.note_merge` writes (`reads.delivery_notes`) when `sd-ship`
  lands a pull request ("Code delivery <url> at <sha>", then the evidence
  JSON). The time is the note's, which is when the merge was observed.
- **run**: runner assignments that started or ended in the window (`reads.recent_assignments`), as
  `operations.assignment_state` reads them, with the queue revision
  `sd runner requeue` checks. `exec` assignments are left to `command`.
- **job**: each launchd job whose log `cron-jobs.sh` wrote in the window,
  as `operations.job_state` reads it (Today's failed-job read, sd:2110).
  A failed job whose log time cannot be read has no place on a timeline;
  its name goes in `undated`, so the page says so rather than drop it.
- **command**: `runner_exec.executions`, the dashboard's own runs of
  registered commands (v1 Operations > Commands; review 2026-09-29 item 15).

The design's other kinds have no collector here. Each is in `unknown` with
the reason, and the page draws it hatched: unknown is not zero.

A source that raises is named in `sources` with its reason and adds no
rows, so a failed read never looks like a quiet day. Nothing here writes.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sd_db import operations, reads, runner, runner_exec
from sd_db.errors import SdDbError

__all__ = ["KINDS", "UNKNOWN", "WINDOW", "document"]

WINDOW = timedelta(hours=24)

#: The kinds the page draws, in lane order.
KINDS = ("merge", "run", "command", "review", "deploy", "mail")

#: Kinds with no collector, and why. The page shows each as unknown, never as zero.
UNKNOWN = {
    "review": "No collector reads reviews. The design read Copilot review mail from Gmail; the dashboard reads no mail.",
    "deploy": "No collector reads deploys. No repository records GitHub deployments, and no deploy log is read.",
    "mail": "No collector reads mail. Brief and status mail stay in Gmail; the dashboard reads no mail.",
}

_DELIVERY = re.compile(r"Code delivery (https://github\.com/([^/\s]+)/([^/\s]+)/pull/([0-9]+)) at ([0-9a-f]{7,64})")

#: Assignment status -> the state grammar's glyph class.
_RUN_STATE = {"done": "ok", "cancelled": "ok", "blocked": "caution", "failed": "warning",
              "queued": "queued", "running": "queued", "ending": "queued"}


def _utc(stamp) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _inside(stamp, start: datetime, end: datetime) -> str | None:
    """The stamp as `YYYY-MM-DDTHH:MM:SSZ` when it falls in [start, end], else None."""
    value = _utc(stamp)
    return _iso(value) if value is not None and start <= value <= end else None


def merges(connection, start: datetime, end: datetime) -> list[dict]:
    rows = reads.delivery_notes(connection, since=_iso(start))
    out = []
    for row in rows:
        at = _inside(row["timestamp"], start, end)
        match = _DELIVERY.match(row["body"] or "")
        if at is None or match is None:
            continue
        url, owner, repo, number, sha = match.groups()
        out.append({"id": f"merge:{row['id']}", "k": "merge", "at": at, "s": "ok", "repo": repo,
                    "what": row["title"] or f"{repo}#{number}", "detail": f"{owner}/{repo} · {sha[:12]}",
                    "ref": f"{repo}#{number}", "url": url, "owner": owner, "pr": int(number), "item": row["item"],
                    "commit": sha, "src": "sd-ship delivery note (ship.note_merge)"})
    return out


def runs(connection, start: datetime, end: datetime) -> list[dict]:
    ids = reads.recent_assignments(connection, since=_iso(start))
    out = []
    for ident in ids:
        state = operations.assignment_state(connection, ident)
        at = _inside(state["ended"], start, end) or _inside(state["started"], start, end)
        if at is None:
            continue
        status = state["status"]
        out.append({"id": f"run:{ident}", "k": "run", "at": at, "s": _RUN_STATE.get(status, "unknown"),
                    "repo": None, "what": state["title"] or f"assignment #{ident}",
                    "detail": f"{state['role']} · {state['provider'] or 'no provider'} · {status}",
                    "ref": f"sd:{state['item']}" if state["item"] else f"assignment #{ident}",
                    "n": ident, "item": state["item"], "role": state["role"], "provider": state["provider"],
                    "status": status, "started": state["started"], "ended": state["ended"],
                    "revision": runner.queue_state(connection, ident)["revision"],
                    "src": "runner assignments (operations.assignment_state)"})
    return out


def _log_stamp(cron_root, name: str) -> datetime | None:
    if not isinstance(cron_root, (str, Path)):
        return None
    try:
        return datetime.fromtimestamp((Path(cron_root) / "logs" / f"{name}.log").stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def jobs(connection, backend, start: datetime, end: datetime) -> tuple[list[dict], list[str]]:
    from .operations_screen import _signal_name

    root = getattr(backend, "cron_root", None)
    out, undated = [], []
    for name in backend.names():
        try:
            job = operations.job_state(connection, name, backend=backend)
        except SdDbError:
            continue  # A job can disappear between enumeration and read, as operations.inventory allows.
        logged = _log_stamp(root, name)
        failed = job["state"] == "failed"
        if logged is None or not start <= logged <= end:
            if failed and logged is None:
                undated.append(name)
            continue
        code, killed = job.get("last_exit"), job.get("last_signal")
        outcome = _signal_name(killed) if killed is not None else f"exit {code}" if code is not None else "no exit code"
        state = "warning" if failed else "queued" if job["state"] == "running" else "ok"
        out.append({"id": f"job:{name}", "k": "run", "at": _iso(logged), "s": state, "repo": None,
                    "what": f"{name} failed with {outcome}" if failed else f"{name} ran ({job['state']})",
                    "detail": f"launchd job · {job.get('schedule') or 'no schedule'}", "ref": name, "job": name,
                    "service": job.get("service"), "failed": failed, "rc": outcome, "revision": job["revision"],
                    "retry": job["capabilities"]["retry"], "src": "launchd jobs and their cron-jobs.sh log time"})
    return out, undated


def commands(connection, start: datetime, end: datetime) -> list[dict]:
    out = []
    for row in runner_exec.executions(connection, limit=100):
        at = _inside(row["timestamp"], start, end)
        if at is None:
            continue
        code, command = row["exit_code"], row.get("command")
        state = "queued" if row["ended"] is None else "ok" if code == 0 else "warning"
        text = " ".join(map(str, command)) if isinstance(command, list) else str(command or "a registered command")
        out.append({"id": f"command:{row['id']}", "k": "command", "at": at, "s": state, "repo": None,
                    "what": text, "detail": row["title"] or "", "ref": f"sd:{row['item']}" if row["item"] else "",
                    "note": row["id"], "item": row["item"], "exit": code, "who": row["session"],
                    "scope": row.get("scope"), "started": row["started"], "ended": row["ended"],
                    "expired": row.get("output_expired"), "src": "registered command runs (runner_exec.executions)"})
    return out


def document(connection: sqlite3.Connection, *, now: str, jobs_backend=None) -> dict:
    """Every event in the 24 hours before `now`, newest first, and what each source said."""
    end = _utc(now)
    start = end - WINDOW
    events: list[dict] = []
    sources: dict[str, str] = {}
    undated: list[str] = []

    def read_jobs():
        found, missing = jobs(connection, jobs_backend or operations.LaunchdBackend(), start, end)
        undated.extend(missing)
        return found

    for source, collect in (("merge", lambda: merges(connection, start, end)),
                            ("run", lambda: runs(connection, start, end)),
                            ("job", read_jobs),
                            ("command", lambda: commands(connection, start, end))):
        try:
            found = collect()
        except (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error) as failure:
            sources[source] = str(failure) or f"{source} read failed without a reason"
            continue
        sources[source] = ""
        events.extend(found)
    events.sort(key=lambda event: (event["at"], event["id"]), reverse=True)
    return {"read": _iso(end), "from": _iso(start), "to": _iso(end), "kinds": list(KINDS), "unknown": UNKNOWN,
            "sources": sources, "undated": undated, "events": events}
