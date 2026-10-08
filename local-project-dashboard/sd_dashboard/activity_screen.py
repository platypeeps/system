"""Activity: one timeline of the last 24 hours, behind the default UI's Activity page (sd:2111).

The page is `v2/activity.html`; it holds no rows. It reads one JSON
document, `/api/activity`, built here from records the library already
keeps, so the port adds no collector:

- **merge**: the `comment` note `ship.note_merge` writes (`reads.delivery_notes`) when `sd-ship`
  lands a pull request ("Code delivery <url> at <sha>", then the evidence
  JSON). The time is the note's, which is when the merge was observed.
- **review**: the review each of those merges carried, from the same note's
  evidence (operator ruling on sd:2211, 2026-10-03): `review_selection`
  names who reviewed, and `review_clearance` says how the review let the
  merge through -- none for a clean or advisory review, `adjudicated` for
  blocking findings whose dispositions the operator accepted. Only reviews
  that reached a merge are recorded, so a review that never shipped is not
  here; the time is the merge's, since the review's own is not recorded.
  A delivery in the window with no review record is counted in
  `review_unrecorded`, so the page says so rather than drop it.
- **run**: runner assignments that started or ended in the window (`reads.recent_assignments`, with the item's repository), as
  `operations.assignment_state` reads them. `exec` assignments are left to `command`.
- **job**: each launchd job whose log `cron-jobs.sh` wrote in the window,
  as `operations.job_state` reads it (Today's failed-job read, sd:2110).
  A failed run is a warning; an interrupted one (a signal nobody accounted
  for) is a caution, and both can retry, as Management shows them. Such a
  job whose log time cannot be read has no place on a timeline; its name
  goes in `undated`, so the page says so rather than drop it.
- **command**: the execution journal, `runner_exec.execution_journal`
  (v1 Operations > Commands; the design's journal, sd:2180): palette runs and
  runner runs alike (sd:2183), with the notes no known writer left counted in
  `journal_skipped`. Every record it returns comes, older ones too, so the
  page's "All read" range can show them; the other kinds stay in the window.
  The read is the latest `JOURNAL_CAP` notes: `journal_unread` counts the
  older ones it left out, so the page says so rather than cut them silently.

The design's other kinds, deploys and mail, have no collector here. Each is in `unknown` with
the reason, and the page draws it hatched: unknown is not zero.

A source that raises is named in `sources` with its reason and adds no
rows, so a failed read never looks like a quiet day. Nothing here writes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sd_db import operations, reads, runner_exec
from sd_db.errors import SdDbError

__all__ = ["JOURNAL_CAP", "KINDS", "UNKNOWN", "WINDOW", "document", "reviews"]

WINDOW = timedelta(hours=24)

#: The latest exec notes the journal read takes (the library's own bound).
JOURNAL_CAP = 100

#: The kinds the page draws, in lane order.
KINDS = ("merge", "run", "review", "deploy", "mail", "command")

#: Kinds with no collector, and why. The page shows each as unknown, never as zero.
UNKNOWN = {
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


#: `review_clearance.kind` -> (state, verdict). No clearance is a clean or advisory review.
_CLEARANCE = {None: ("ok", "clean or advisory"),
              "adjudicated": ("caution", "blocking findings; the operator accepted their dispositions")}


def _evidence(body: str) -> dict:
    """The JSON `ship.note_merge` writes under the delivery line, or {} when the note carries none."""
    _, _, rest = body.partition("\n")
    try:
        value = json.loads(rest)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def reviews(connection, start: datetime, end: datetime, unrecorded: dict) -> list[dict]:
    """The review each delivery in the window carried, at the merge's time; `unrecorded` counts deliveries with none."""
    out, missing = [], 0
    for row in reads.delivery_notes(connection, since=_iso(start)):
        at = _inside(row["timestamp"], start, end)
        match = _DELIVERY.match(row["body"] or "")
        if at is None or match is None:
            continue
        url, owner, repo, number, _ = match.groups()
        evidence = _evidence(row["body"])
        selection = evidence.get("review_selection")
        by = selection.get("reviewed_by") if isinstance(selection, dict) else None
        if not isinstance(by, list) or not by:
            missing += 1
            continue
        reviewers = [str(name) for name in by]
        clearance = evidence.get("review_clearance")
        kind = clearance.get("kind") if isinstance(clearance, dict) else None
        state, verdict = _CLEARANCE.get(kind, ("unknown", f"cleared as {kind!r}, a kind this page does not know"))
        head = evidence.get("reviewed_head") if isinstance(evidence.get("reviewed_head"), str) else None
        names = ", ".join(reviewers)
        out.append({"id": f"review:{row['id']}", "k": "review", "at": at, "s": state, "repo": repo,
                    "what": f"{names} reviewed {repo}#{number}", "detail": verdict + (f" · head {head[:12]}" if head else ""),
                    "ref": f"{repo}#{number}", "url": url, "owner": owner, "pr": int(number), "item": row["item"],
                    "title": row["title"], "reviewers": reviewers, "requested": selection.get("requested_provider"),
                    "verdict": verdict, "clearance": kind, "head": head,
                    "src": "sd-ship delivery note: review_selection and review_clearance"})
    unrecorded["n"] = missing  # Only a read that finished counts; a failed one leaves 0 and names itself in `sources`.
    return out


def _repo_label(path: str | None) -> str | None:
    """An item's repository as merges label theirs: the last path part, which names the GitHub repository it clones."""
    return (Path(path).name or None) if path else None


def runs(connection, start: datetime, end: datetime) -> list[dict]:
    rows = reads.recent_assignments(connection, since=_iso(start))
    out = []
    for ident, repo in rows:
        state = operations.assignment_state(connection, ident)
        at = _inside(state["ended"], start, end) or _inside(state["started"], start, end)
        if at is None:
            continue
        status = state["status"]
        out.append({"id": f"run:{ident}", "k": "run", "at": at, "s": _RUN_STATE.get(status, "unknown"),
                    "repo": _repo_label(repo), "what": state["title"] or f"assignment #{ident}",
                    "detail": f"{state['role']} · {state['provider'] or 'no provider'} · {status}",
                    "ref": f"sd:{state['item']}" if state["item"] else f"assignment #{ident}",
                    "n": ident, "item": state["item"], "role": state["role"], "provider": state["provider"],
                    "status": status, "started": state["started"], "ended": state["ended"],
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
        failed, interrupted = job["state"] == "failed", job["state"] == "interrupted"
        if logged is None or not start <= logged <= end:
            if (failed or interrupted) and logged is None:
                undated.append(name)
            continue
        code, killed = job.get("last_exit"), job.get("last_signal")
        outcome = _signal_name(killed) if killed is not None else f"exit {code}" if code is not None else "no exit code"
        state = "warning" if failed else "caution" if interrupted else "queued" if job["state"] == "running" else "ok"
        what = (f"{name} failed with {outcome}" if failed else f"{name} was interrupted ({outcome})" if interrupted
                else f"{name} ran ({job['state']})")
        out.append({"id": f"job:{name}", "k": "run", "at": _iso(logged), "s": state, "repo": None,
                    "what": what, "interrupted": interrupted,
                    "detail": f"launchd job · {job.get('schedule') or 'no schedule'}", "ref": name, "job": name,
                    "service": job.get("service"), "failed": failed or interrupted, "rc": outcome, "exit": code, "revision": job["revision"],
                    "retry": job["capabilities"]["retry"], "src": "launchd jobs and their cron-jobs.sh log time"})
    return out, undated


def commands(connection, end: datetime, skipped: dict, read: dict) -> list[dict]:
    """Every journal record up to `end`, at its end time. No exit code on an ended run means it was stopped: caution."""
    out = []
    journal = runner_exec.execution_journal(connection, limit=JOURNAL_CAP)
    skipped.update(journal["skipped"])
    taken = len(journal["executions"]) + sum(journal["skipped"].values())
    read["unread"] = max(0, reads.exec_note_count(connection) - taken)
    for row in journal["executions"]:
        stamp = _utc(row["ended"]) or _utc(row["timestamp"])
        if stamp is None or stamp > end:
            continue
        code, command = row["exit_code"], row.get("command")
        state = "queued" if row["ended"] is None else "ok" if code == 0 else "caution" if code is None else "warning"
        text = " ".join(map(str, command)) if isinstance(command, list) else str(command or "a registered command")
        outcome = "running" if row["ended"] is None else "no exit code" if code is None else f"exit {code}"
        out.append({"id": f"cmd:{row['id']}", "k": "command", "at": _iso(stamp), "s": state, "repo": None,
                    "what": f"{text} · {outcome}", "ref": f"note {row['id']}",
                    "detail": row.get("detail") or row["title"] or "", "source": row.get("source") or "palette",
                    "title": row["title"], "command": text, "assignment": row.get("assignment"),
                    "note": row["id"], "item": row["item"], "exit": code, "who": row["session"],
                    "scope": row.get("scope"), "started": row["started"], "ended": row["ended"],
                    "expired": row.get("output_expired"), "src": "the execution journal (runner_exec.executions)"})
    return out


def document(connection: sqlite3.Connection, *, now: str, jobs_backend=None) -> dict:
    """Every event in the 24 hours before `now`, and every journal record, newest first, and what each source said."""
    end = _utc(now)
    start = end - WINDOW
    events: list[dict] = []
    sources: dict[str, str] = {}
    undated: list[str] = []
    skipped: dict[str, int] = {}
    journal: dict[str, int] = {"unread": 0}
    unrecorded: dict[str, int] = {"n": 0}

    def read_jobs():
        found, missing = jobs(connection, jobs_backend or operations.LaunchdBackend(), start, end)
        undated.extend(missing)
        return found

    for source, collect in (("merge", lambda: merges(connection, start, end)),
                            ("review", lambda: reviews(connection, start, end, unrecorded)),
                            ("run", lambda: runs(connection, start, end)),
                            ("job", read_jobs),
                            ("command", lambda: commands(connection, end, skipped, journal))):
        try:
            found = collect()
        except (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error) as failure:
            sources[source] = str(failure) or f"{source} read failed without a reason"
            continue
        sources[source] = ""
        events.extend(found)
    events.sort(key=lambda event: (event["at"], event["id"]), reverse=True)
    return {"read": _iso(end), "from": _iso(start), "to": _iso(end), "kinds": list(KINDS), "unknown": UNKNOWN,
            "sources": sources, "undated": undated, "review_unrecorded": unrecorded["n"], "journal_skipped": skipped,
            "journal_cap": JOURNAL_CAP, "journal_unread": journal["unread"], "events": events}
