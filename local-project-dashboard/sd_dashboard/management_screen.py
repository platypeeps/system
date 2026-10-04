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
  Each job carries `last_run`, read from `<cron_root>/logs/.<job>.stamp`, the
  start, end and exit the cron-jobs wrapper writes for every run (sd:2210):
  launchd keeps no run time, and a log's write time is not one.
- `archive`: the runner's last and next archive refresh, as `runner.sh
  status` prints them (sd:2209). The dashboard does not know the runner's
  config or retention folder, so it runs this checkout's `runner.sh status`
  and reads its one-line body.

Every write the page makes goes through a route `server.action_route`
answers: runner requeue and cancel, job retry, service start, stop and
restart, and the two sd-db repo verbs (`repos.set_runner_merge`,
`repos.set_managed`), which refuse a stale `before`.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sd_db import operations, protection, reads, repos, runner, workflow
from sd_db.errors import SdDbError

from . import fleet as fleet_module
from .repos_screen import primary

__all__ = ["document", "last_run", "runner_status", "set_repo"]

#: How many assignments and merges the page lists; the history counts every row.
LATEST = 40
MERGES = 12
#: The largest sd-review.json the page shows in full.
REVIEW_BYTES = 16384
FAILURES = (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error)
#: The largest run stamp read; the wrapper writes three short lines.
STAMP_BYTES = 512
#: `runner.sh status`'s ceiling: one Python start and one database read.
RUNNER_SECONDS = 15.0
RUNNER = Path(__file__).resolve().parents[2] / "local-sd-runner" / "runner.sh"


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


def _time(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def last_run(cron_root, name: str) -> dict:
    """The job's last run, from the stamp the cron-jobs wrapper writes beside its log (sd:2210).

    `state` is `finished` (a start, an end and an exit code), `open` (a start
    and no end: a run in progress, or one no trap saw end), `none` (no stamp:
    the job has not run since the wrapper began writing one) or `unread`;
    `reason` says why for the last two.
    """
    out = {"state": "unread", "started": None, "ended": None, "exit": None, "reason": ""}
    if not isinstance(cron_root, (str, Path)):
        return out | {"reason": "the jobs backend names no logs folder"}
    try:
        raw = (Path(cron_root) / "logs" / f".{name}.stamp").read_bytes()[:STAMP_BYTES + 1]
    except FileNotFoundError:
        return out | {"state": "none", "reason": "no run stamp: the job has not run since its wrapper began writing one"}
    except OSError as error:
        return out | {"reason": f"the run stamp is unreadable: {error.strerror or error}"}
    if len(raw) > STAMP_BYTES:
        return out | {"reason": "the run stamp is larger than the wrapper writes"}
    fields = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = value
    started, ended, code = fields.get("started"), fields.get("ended"), fields.get("exit")
    if not _time(started):
        return out | {"reason": "the run stamp names no start time"}
    if ended is None and code is None:
        return out | {"state": "open", "started": started}
    if not _time(ended):
        return out | {"reason": "the run stamp has an exit code without an end time"}
    if code is None or not code.isdigit():
        return out | {"reason": "the run stamp has an end without an exit code"}
    return out | {"state": "finished", "started": started, "ended": ended, "exit": int(code)}


def _jobs(connection, backend) -> list[dict]:
    keep = ("name", "label", "service", "schedule", "state", "pid", "last_exit", "last_signal", "revision", "capabilities")
    # `cron_root` places the logs folder, as Activity and Now read it; a backend without one still lists its jobs.
    root = getattr(backend, "cron_root", None)
    return [{key: job.get(key) for key in keep} | {"last_run": last_run(root, job.get("name") or "")}
            for job in operations.inventory(connection, backend=backend)["jobs"]]


def runner_status() -> tuple[int, str, str]:
    """This checkout's `runner.sh status`: its exit code, stdout and stderr, within `RUNNER_SECONDS` (sd:2209)."""
    try:
        done = subprocess.run(["sh", str(RUNNER), "status"], capture_output=True, text=True, timeout=RUNNER_SECONDS,
                              check=False)
    except subprocess.TimeoutExpired:
        raise ValueError(f"runner.sh status ran past its {RUNNER_SECONDS:g} seconds") from None
    return done.returncode, done.stdout, done.stderr


def _archive(status) -> dict:
    """The last and next archive refresh from `runner.sh status`'s body; ValueError names what it does not say.

    The exit code is the heartbeat's verdict (0, 1 or 3) and not this
    source's: a stale heartbeat still prints the schedule.
    """
    code, out, err = status()
    said = (err.strip().splitlines() or [""])[-1]
    if code not in (0, 1, 3):
        raise ValueError(f"runner.sh status exited {code}" + (f": {said}" if said else ""))
    line = (out.strip().splitlines() or [""])[0]
    if not line:
        raise ValueError("runner.sh status printed no body" + (f": {said}" if said else ""))
    try:
        body = json.loads(line)
    except ValueError:
        raise ValueError("runner.sh status printed a body that is not JSON") from None
    if not isinstance(body, dict):
        raise ValueError("runner.sh status printed a body that is not an object")
    schedule = body.get("archive_refresh_schedule")
    if schedule is None:
        reason = body.get("reason")
        raise ValueError(f"runner.sh status names no archive refresh: {reason}" if isinstance(reason, str) and reason
                         else "runner.sh status names no archive refresh; the runner predates sd:2209")
    if not isinstance(schedule, dict):
        raise ValueError("runner.sh status named an archive refresh that is not an object")
    if schedule.get("reason"):
        raise ValueError(f"the archive refresh was not read: {schedule['reason']}")
    last, upcoming = schedule.get("last_completed_at"), schedule.get("next_due_at")
    if last is not None and not _time(last):
        raise ValueError("runner.sh status named a last refresh that is not a time")
    if not _time(upcoming):
        raise ValueError("runner.sh status named a next refresh that is not a time")
    return {"last": last, "next": upcoming, "due": schedule.get("due") is True}


def document(connection: sqlite3.Connection, *, now: str, fleet=None, jobs=None, services=None, runner=None) -> dict:
    """Every source the page reads, and the reason for each one that could not be read.

    `fleet` is `fleet.collect`'s shape, the seam a test fills; `jobs` and
    `services` are the operations and services backends, the launchd ones
    by default; `runner` is `runner_status`'s shape.
    """
    read = fleet or fleet_module.collect
    status = runner or runner_status
    out: dict = {"read": now, "sources": {}}
    for source, collect in (("repos", lambda: _repos(connection)),
                            ("git", lambda: _git(read, now)),
                            ("lane", lambda: _lane(connection)),
                            ("assignments", lambda: _assignments(connection)),
                            ("sessions", lambda: _sessions(read)),
                            ("services", lambda: _services(connection, services)),
                            ("jobs", lambda: _jobs(connection, jobs or operations.LaunchdBackend())),
                            ("archive", lambda: _archive(status))):
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
    """One sd-db repo verb, refused when the row moved since the page read it; returns the new setting.

    The read, the check and the write hold one `BEGIN IMMEDIATE`: the server
    answers requests on threads, and two that read the same old value must
    not both write.
    """
    setter, column = SETTERS[field]
    with workflow.transaction(connection):
        row = repos.row_for(connection, path)
        if row is None:
            raise repos.RepoRefusal(f"{path} is not a registered repository")
        current = ("yes" if row["managed"] else "no") if column == "managed" else row[column]
        if current != before:
            raise StaleSetting(f"{column} for {path} is {current} now, not {before}; read the page again")
        _, was = setter(connection, row["path"], value)
    return {"path": row["path"], "field": column, "value": value, "before": was}
