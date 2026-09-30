"""Management: the document behind the default UI's Management page (sd:2118).

The page is `v2/management.html`; it holds no rows. It reads one JSON
document, `/api/management`, built here from reads v1 already makes, each
source guarded on its own so one that fails is a reason and not an empty list
(`sources` holds the empty string for a source that was read):

- `repos`: `repos.registered`, the sd-db repo table, with each checkout's
  `.github/sd-review.json` read from disk and the protection reading
  `protection.rows` gives v1 /protection (the nightly collector's; nothing
  here calls GitHub).
- `git`: `fleet.collect("repos")`, v1 Operations > Repos, each row with
  `repos_screen.primary`'s state, headline, detail and remedy. Nothing fetches
  or pulls.
- `lane`: the runner heartbeat, queued and running assignments, the latest
  merge assignments, and the items waiting at `ready_to_send`.
- `assignments`: the latest assignments with the queue revision `sd runner
  requeue` and `cancel` check, and the history counted by status.
- `sessions`: `fleet.collect("sessions")`, v1 Operations > Sessions:
  worktree counts and the sd-* processes it lists.
- `services` and `jobs`: `services.inventory` and `operations.inventory`,
  v1 Operations > Services and > Jobs, with the revision each write sends.

Every write the page makes goes through a route `server.action_route`
answers: runner requeue and cancel, job retry, service start, stop and
restart, and the two sd-db repo verbs (`repos.set_runner_merge`,
`repos.set_managed`), which refuse a stale `before`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sd_db import operations, protection, reads, repos, runner, workflow
from sd_db.errors import SdDbError

from . import fleet as fleet_module
from .repos_screen import primary

__all__ = ["document", "set_repo"]

#: How many assignments and merges the page lists; the history counts every row.
LATEST = 40
MERGES = 12
#: The largest sd-review.json the page shows in full.
REVIEW_BYTES = 16384
FAILURES = (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error)


def _review(path: str) -> dict | None:
    """The checkout's `.github/sd-review.json`: None when the checkout is not on disk, `file: None` when absent."""
    root = Path(path).expanduser()
    if not root.is_dir():
        return None
    target = root / ".github" / "sd-review.json"
    try:
        raw = target.read_bytes()[:REVIEW_BYTES + 1]
    except FileNotFoundError:
        return {"file": None}
    except OSError as error:
        return {"file": None, "error": f"unreadable: {error.strerror or error}"}
    text = raw[:REVIEW_BYTES].decode("utf-8", "replace")
    try:
        body = json.loads(raw)
    except ValueError:
        return {"file": text, "error": "not valid JSON" if len(raw) <= REVIEW_BYTES else "too large to show"}
    body = body if isinstance(body, dict) else {}
    copilot = body.get("copilot_review") if isinstance(body.get("copilot_review"), dict) else {}
    schema = body.get("$schema") if isinstance(body.get("$schema"), str) else None
    return {"file": text, "severity_floor": body.get("severity_floor"), "automatic_deep": copilot.get("automatic_deep"),
            "schema": schema}


def _protection(row: dict) -> dict:
    return {"status": row["status"], "observed_at": row["observed_at"], "default_branch": row["default_branch"],
            "reason": row["reason"], "gaps": [{"id": gap.get("id"), "gap": gap.get("gap")} for gap in row["gaps"]]}


def _repos(connection) -> list[dict]:
    guarded = {row["repo"]: _protection(row) for row in protection.rows(connection)}
    out = []
    for row in repos.registered(connection):
        out.append({
            "path": row["path"], "remote": row["remote"] or "", "mode": row["mode"], "ci": row["ci"],
            "runner_merge": row["runner_merge"], "managed": "yes" if row["managed"] else "no",
            "status_source": row["status_source"], "pieces_source": row["pieces_source"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "review": _review(row["path"]), "protection": guarded.get(row["path"]),
        })
    return out


def _git(read, now: str) -> dict:
    document = read("repos")
    rows = document.get("repos") if isinstance(document, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("path"), str) for row in rows):
        raise ValueError("fleet collector returned an incomplete repos document")
    when = datetime.fromisoformat(now.replace("Z", "+00:00")) if now else datetime.now(timezone.utc)
    out = []
    for row in rows:
        state, headline, detail, remedy = primary(row, when)
        out.append({key: row.get(key) for key in ("name", "path", "branch", "default", "dirty", "ahead", "behind",
                                                   "behind_default", "fetched_iso", "last_iso", "subject", "error")}
                   | {"truncated": list(row.get("truncated") or []), "state": state, "headline": headline,
                      "detail": detail, "remedy": remedy})
    return {"root": document.get("root"), "counts": document.get("counts") or {}, "repos": out}


def _assignment(connection, row) -> dict:
    return {"id": row["id"], "item": row["item"], "title": row["title"], "repo": row["repo"], "role": row["role"],
            "provider": row["provider"], "status": row["status"], "lane": row["lane"], "queued_at": row["queued_at"],
            "started": row["started"], "ended": row["ended"],
            "revision": runner.queue_state(connection, row["id"])["revision"]}


def _lane(connection) -> dict:
    try:
        heartbeat = runner.heartbeat_state(connection)
    except SdDbError as error:
        heartbeat = {"ok": False, "reason": str(error)}
    live = [_assignment(connection, row) for row in reads.assignment_ledger(connection, live=True)]
    merges = [_assignment(connection, row) for row in reads.assignment_ledger(connection, role="merge", limit=MERGES)]
    ready = [dict(row) for row in reads.ready_to_send(connection)]
    beat = {key: heartbeat.get(key) for key in ("ok", "reason", "timestamp", "interval_seconds", "healthy")}
    return {"heartbeat": beat, "live": live, "merges": merges, "ready": ready}


def _assignments(connection) -> dict:
    latest = [_assignment(connection, row) for row in reads.assignment_ledger(connection, exclude_role="merge", limit=LATEST)]
    return {"latest": latest, "history": reads.assignment_counts(connection)}


def _sessions(read) -> dict:
    document = read("sessions")
    trees = document.get("worktrees") if isinstance(document, dict) else None
    procs = document.get("processes") if isinstance(document, dict) else None
    if not isinstance(trees, list) or not isinstance(procs, list) or any(
            not isinstance(tree, dict) or not isinstance(tree.get("live"), bool) for tree in trees):
        raise ValueError("fleet collector returned an incomplete sessions document")
    abandoned = sum(1 for tree in trees if not tree["live"])
    return {"registered": len(trees), "abandoned": abandoned,
            "processes": [{key: proc.get(key) for key in ("pid", "elapsed", "command")} for proc in procs if isinstance(proc, dict)],
            "processes_error": document.get("processes_error") or ""}


def _services(connection, backend) -> list[dict]:
    from sd_db import services

    keep = ("name", "label", "scope", "domain", "category", "state", "pid", "last_exit", "last_signal", "revision")
    return [{key: entry.get(key) for key in keep} | {"capabilities": entry.get("capabilities") or {}}
            for entry in services.inventory(connection, backend=backend)["services"]]


def _jobs(connection, backend) -> list[dict]:
    keep = ("name", "label", "service", "schedule", "state", "pid", "last_exit", "last_signal", "revision", "capabilities")
    return [{key: job.get(key) for key in keep} for job in operations.inventory(connection, backend=backend)["jobs"]]


def document(connection: sqlite3.Connection, *, now: str, fleet=None, jobs=None, services=None) -> dict:
    """Every source the page reads, and the reason for each one that could not be read.

    `fleet` is `fleet.collect`'s shape, the seam a test fills; `jobs` and
    `services` are the operations and services backends, the launchd ones
    by default.
    """
    read = fleet or fleet_module.collect
    out: dict = {"read": now, "sources": {}}
    for source, collect in (("repos", lambda: _repos(connection)),
                            ("git", lambda: _git(read, now)),
                            ("lane", lambda: _lane(connection)),
                            ("assignments", lambda: _assignments(connection)),
                            ("sessions", lambda: _sessions(read)),
                            ("services", lambda: _services(connection, services)),
                            ("jobs", lambda: _jobs(connection, jobs or operations.LaunchdBackend()))):
        try:
            out[source] = collect()
        except FAILURES as failure:
            out[source] = None
            out["sources"][source] = str(failure) or f"{source} could not be read"
            continue
        out["sources"][source] = ""
    return out


#: The two sd-db repo verbs the page runs: the field, the library call, and how the row reads the value back.
SETTERS = {"runner-merge": (repos.set_runner_merge, "runner_merge"), "managed": (repos.set_managed, "managed")}


class StaleSetting(workflow.StaleItem):
    """The row no longer holds the value the page showed."""


def set_repo(connection: sqlite3.Connection, field: str, path: str, value: str, before: str) -> dict:
    """One sd-db repo verb, refused when the row moved since the page read it; returns the new setting."""
    setter, column = SETTERS[field]
    row = repos.row_for(connection, path)
    if row is None:
        raise repos.RepoRefusal(f"{path} is not a registered repository")
    current = ("yes" if row["managed"] else "no") if column == "managed" else row[column]
    if current != before:
        raise StaleSetting(f"{column} for {path} is {current} now, not {before}; read the page again")
    _, was = setter(connection, row["path"], value)
    return {"path": row["path"], "field": column, "value": value, "before": was}
