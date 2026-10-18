"""Places: the HOA and Home pages' Tasks, Briefs and Reports views, and the area rule Tasks' HOA and Home views share.

Each view reads its own document: `/api/<area>/tasks`, `/api/<area>/briefs` and `/api/<area>/reports`, built here
from readers that already exist, with no store of its own. HOA's Tasks is the exception: its rows are
`/api/hoa`'s items (`hoa_screen`, ranked by urgency), which the page reads anyway for its annunciator and badge.

**Which rows are an area's.**

- An item is HOA when its repo is the hoa checkout (`hoa_screen.checkout`), and Home when it has no repo: the
  operator files personal and home items without one. A report is in neither: its family's Reports view holds it.
  `area_of` is that rule, and Tasks' rows carry its answer as `area`, so Tasks' HOA and Home views hold the place
  pages' Tasks rows.
- A brief is the area's when its repo is the area's checkout, or its series, lower-cased with spaces as hyphens, is a
  job of the area's report family (`series_job`): briefs always name a registered repo, so Home has only the second.
- A report is the area's when its job is in the area's report family. The family is the `report-families.conf` line
  whose key is the area's (`reports_screen.families`).

**Which realm a row is in (sd:5064).** Tasks' Personal, Work and other realm views: `realms.conf` names each realm and the
checkout path globs that are its (`realms`); `realm_of` gives a row the first realm whose glob matches its repo path. A
row with no repo, or a repo no line claims, is Personal; a report is in no realm. Without a readable file a repo row's
realm is unknown (None), so Personal holds only the rows with no repo and the page says why.

**What is not read.** A missing hoa checkout, family or file makes the view `unknown` with the reason and the line to
add, never an empty list. Nothing here writes.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from sd_db import reads

from .documents import _config_dir, pipe_lines

__all__ = ["AREAS", "area_of", "briefs", "realm_of", "realms", "reports", "series_job", "tasks"]

#: The places, by family key, and their labels.
AREAS = {"hoa": "HOA", "home": "Home"}

#: The ids a realms.conf line may not take: Personal is every row no line claims, and the others are Tasks' place views.
RESERVED = ("personal", "hoa", "home", "all")
REALM_ID = re.compile(r"[a-z0-9][a-z0-9-]*")


#: In this machine's config folder, never supplied by a request; the checkout ships `realms.conf.example`.
REALMS_CONFIG = _config_dir() / "realms.conf"


def realms(config: Path | None = None) -> dict:
    """`realms.conf`'s `realm|<id>|<label>|<glob>` lines, in file order; lines with one id add globs to it.

    `state` is read, partial (a line was skipped; `problems` names it), missing or error. A glob's `~` is the home folder.
    """
    config = REALMS_CONFIG if config is None else config
    source = f"<config>/project-dashboard/{config.name}"
    try:
        text = config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"state": "missing", "source": source, "problems": [], "list": []}
    except (OSError, UnicodeDecodeError) as error:
        return {"state": "error", "source": source, "problems": [f"not read: {error.__class__.__name__}"], "list": []}
    found: dict[str, dict] = {}
    problems = []
    for number, line, fields in pipe_lines(text):
        if len(fields) != 4 or fields[0] != "realm" or not all(fields[1:]):
            problems.append(f"line {number}: not realm|<id>|<label>|<glob>: {line}")
        elif not REALM_ID.fullmatch(fields[1]) or fields[1] in RESERVED:
            problems.append(f"line {number}: {fields[1]!r} is not a realm id (lower case and hyphens; not {', '.join(RESERVED)})")
        else:
            realm = found.setdefault(fields[1], {"id": fields[1], "label": fields[2], "globs": []})
            realm["globs"].append(os.path.expanduser(fields[3]))
    return {"state": "partial" if problems else "read", "source": source, "problems": problems, "list": list(found.values())}


def realm_of(repo: str | None, kind: str, rules: list | None) -> str | None:
    """The realm of an item: the first `rules` realm a glob of which matches its repo path, else `personal`.

    No repo is Personal. A report is in no realm: its family's Reports view lists it. `rules` None (realms.conf missing or
    unreadable) leaves a repo row's realm unknown, None.
    """
    if kind == "report":
        return None
    if not repo:
        return "personal"
    if rules is None:
        return None
    return next((r["id"] for r in rules if any(fnmatch.fnmatchcase(repo, glob) for glob in r["globs"])), "personal")


def area_of(repo: str | None, kind: str, hoa: str | None) -> str | None:
    """The area an item belongs to: `hoa` for the hoa checkout's, `home` for one with no repo, else None.

    A report is in no area's Tasks: the area's Reports view lists it by its job.
    """
    if kind == "report":
        return None
    if not repo:
        return "home"
    return "hoa" if hoa is not None and repo == hoa else None


def series_job(series: str) -> str:
    """The job name a brief series spells: lower case, runs of spaces as one hyphen."""
    return re.sub(r"\s+", "-", series.strip().lower())


def _hoa(connection) -> tuple[str | None, str]:
    """The hoa checkout's recorded path, and why there is none."""
    from .hoa_screen import checkout

    recorded, _, problems = checkout(connection)
    if problems:
        return None, problems[0]
    return recorded, "" if recorded else "no repo named hoa is registered"


def _family(area: str, config: Path | None) -> tuple[list[str] | None, str]:
    """The jobs of the area's report family, and why there are none."""
    from .reports_screen import families

    found = families(config)
    line = f"add family|{area}|{AREAS[area]}|<icon>|<job>,<job>,... to {found['source']}"
    if found["state"] == "missing":
        return None, f"not configured: {found['source']} is missing; {line}"
    if found["state"] == "error":
        return None, f"not configured: {found['source']} was {found['problems'][0]}"
    family = next((f for f in found["list"] if f["key"] == area), None)
    if family is None:
        return None, f"not configured: {found['source']} names no family {area}; {line}"
    return family["jobs"], ""


def _unknown(now: str, area: str, reason: str, **more) -> dict:
    return {"read": now, "area": area, "state": "unknown", "reason": reason, "rows": [], **more}


def tasks(connection, area: str, *, now: str) -> dict:
    """The area's open items: `reads.backlog_items`' unparked open rows `area_of` places in it."""
    hoa, why = _hoa(connection)
    if area == "hoa" and hoa is None:
        return _unknown(now, area, why)
    from .screens import _repo_labels

    found = [row for row in reads.backlog_items(connection, now=now)
             if row["status"] != "done" and area_of(row["repo"], row["kind"], hoa) == area]
    label = _repo_labels(row["repo"] for row in found)
    rows = [{"id": row["id"], "title": row["title"], "kind": row["kind"], "status": row["status"],
             "priority": row["priority"], "due": row["due"], "repo": label(row["repo"]) if row["repo"] else None,
             "since": row["status_since"]} for row in found]
    return {"read": now, "area": area, "state": "read", "reason": "", "rows": rows}


#: How many briefs are read before the area filter: every one the store holds, in practice; a page shows `ROWS`.
BRIEF_SCAN = 100_000


def briefs(connection, area: str, *, now: str, config: Path | None = None) -> dict:
    """The area's briefs, in the Briefs page's row shape (`briefs_screen.document`)."""
    from . import briefs_screen

    jobs, unconfigured = _family(area, config)
    repo = _hoa(connection)[0] if area == "hoa" else None
    if jobs is None and repo is None:
        return _unknown(now, area, unconfigured)
    wanted = set(jobs or ())

    def reader(conn):
        got = briefs_screen._library().rows(conn, limit=BRIEF_SCAN)
        return [raw for raw in got if (repo is not None and raw.get("repo") == repo)
                or series_job(str((raw.get("brief") or {}).get("series") or "")) in wanted][:briefs_screen.ROWS + 1]

    doc = briefs_screen.document(connection, now=now, reader=reader)
    state = "error" if doc["reader"]["state"] == "error" else "read"
    # A family not configured still leaves the checkout's briefs; the reason says what else would be read.
    return {"read": now, "area": area, "state": state, "reason": doc["reader"]["reason"] or unconfigured,
            "rows": doc["briefs"], "reader": doc["reader"]}


def reports(connection, area: str, *, now: str, config: Path | None = None) -> dict:
    """The reports of the area's family jobs, from the newest `reporting.reports` the Reports page reads."""
    from .reports_screen import FAILURES, _reports

    jobs, unconfigured = _family(area, config)
    if jobs is None:
        return _unknown(now, area, unconfigured, jobs=[])
    try:
        found = _reports(connection)
    except FAILURES as failure:
        return {**_unknown(now, area, f"reports not read: {failure}", jobs=jobs), "state": "error"}
    keep = ("id", "title", "status", "at", "job", "attention", "exit", "ended", "followups")
    rows = [{key: row[key] for key in keep} for row in found if row["job"] in jobs]
    return {"read": now, "area": area, "state": "read", "reason": "", "rows": rows, "jobs": jobs, "limit": 200}
