"""Today's GitHub view (sd:5608): the operator's open pull requests and issues, and those merged or closed in the last
`DAYS` days, newest activity first, each linked to GitHub and to its sd item when one is known.

No collector and no GitHub call: every row is something the dashboard already reads. `shadow` is what `sd shadow sync`
stored (`progress.tracker_items`, which Now's pull request rows read too); the Contributions projection
(`contributions.projection`) adds upstream work the shadow sync does not hold and the item each contribution carries;
`repos.intake_repos` names the repositories whose third-party issues Issue intake lists; sd-ship's delivery notes
(`reads.delivery_notes`) name the item a merged pull request delivered; and an item filed under the ref
`github:<owner>/<name>#N` (`sd task add --ref`) is that issue's or pull request's item.

What is listed, open or settled alike: a pull request waiting on the operator (`needs_you`, as on Now), assigned to them,
or theirs (the `filed` flag, else a `reason` of filed); an issue assigned to them or theirs; a third-party issue in a
repository with GitHub issues on; and any GitHub row the Contributions projection holds. Theirs in a repository sd does
not register is upstream. Watched rows and other people's pull requests stay on Trackers.

Activity is GitHub's own stamp: merged or closed for a settled row, else `updated_at`, else `created_at`. A row synced
before those columns has none, so it says not recorded and sorts last; `last_seen` is when the collector looked, never
activity. A settled row with no merge or close date cannot be placed in the window: `undated` counts it.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from sd_db import contributions, progress, reads, repos, writes
from sd_db.errors import SdDbError

from .contribution_screen import OPEN_LANES, _freshness, _moment, _registered, _scope

#: The settled window, in days.
DAYS = 7
#: The source of an item filed under a tracker ref (`review.REF_SOURCE`).
REF_SOURCE = "task-ref"
READ_ERRORS = (SdDbError, sqlite3.Error, ValueError, TypeError, KeyError)


def _mine(row) -> bool:
    filed = row.get("filed")
    return filed == 1 if filed is not None else row.get("reason") == "filed"


def _activity(row) -> tuple[str | None, str | None]:
    """`(at, what)`: GitHub's newest stamp for the row and its name, or `(None, None)` when none reads."""
    for what, key in (("merged", "merged_at"), ("closed", "closed_at"), ("updated", "updated_at"), ("opened", "created_at")):
        if _moment(row.get(key)) is not None:
            return row[key], what
    return None, None


def _ref(url: str) -> tuple[str, str | None, int | None]:
    """`(kind, owner/name, number)` from a GitHub pull request or issue address; kind is "" for any other address."""
    for kind, pattern in (("pull", contributions.PULL), ("issue", contributions.ISSUE)):
        found = pattern.fullmatch(url or "")
        if found:
            return kind, f"{found[1]}/{found[2]}", int(found[3])
    return "", None, None


def document(connection: sqlite3.Connection, *, now: str) -> dict:
    """`pulls` and `issues` open, `settled` in the window, `undated`, `collector` freshness, `unread` (what a source that
    did not read leaves out, by source) and `error`, the reason nothing could be read or ""."""
    moment = _moment(now)
    since = moment - timedelta(days=DAYS)
    found = {"now": now, "days": DAYS, "pulls": [], "issues": [], "settled": [], "undated": 0, "unread": {},
             "collector": _freshness(connection, now), "error": ""}
    try:
        shadow = progress.tracker_items(connection, tracker="github", state=None)
        # GitHub reads an owner and a name in any case, and the two stores do not always agree on one: keyed lower-cased.
        projected = {row["url"].lower(): row for row in contributions.projection(connection) if row.get("url")}
        remotes, places = _registered(connection)
    except READ_ERRORS as failure:
        return {**found, "error": str(failure) or "the GitHub rows could not be read"}
    unread = found["unread"]
    try:
        intake = {slug.lower() for slug in repos.intake_repos(connection).values()}
    except READ_ERRORS as failure:
        intake, unread["intake"] = set(), str(failure) or "the repositories with GitHub issues on could not be read"
    try:
        delivered = {}
        for note in reads.delivery_notes(connection, since=since.strftime("%Y-%m-%dT%H:%M:%SZ")):
            parts = (note["body"] or "").split()
            if len(parts) > 2 and parts[:2] == ["Code", "delivery"]:
                delivered.setdefault(parts[2].lower(), note["item"])
    except READ_ERRORS as failure:
        delivered, unread["delivery"] = {}, str(failure) or "the delivery notes could not be read"

    rows = {}
    for row in shadow:
        kind, slug, number = _ref(row.get("url") or "")
        if kind:
            rows[row["url"].lower()] = {**row, "kind": kind, "repo": row.get("repo") or slug,
                                        "number": row.get("number") or number}
    # Contributions the shadow sync does not hold, such as an upstream issue an item tracks: the projection's own fields.
    for key, row in projected.items():
        kind, slug, number = _ref(row["url"])
        if kind and key not in rows:
            state = row.get("external_state") or ("open" if row["lane"] in OPEN_LANES else row["lane"])
            rows[key] = {"url": row["url"], "kind": kind, "repo": slug, "number": number, "title": row.get("title"),
                         "state": state, "merged_at": row.get("merged_at"), "closed_at": row.get("closed_at")}

    for key, row in rows.items():
        contribution = projected.get(key) or {}
        state = str(row.get("state") or "").lower()
        # Waiting on you is an open row's: the projection can still say it of one GitHub has since closed.
        if state == "open" and (row.get("needs_you") or contribution.get("needs_you")):
            why = "waiting on you"
        elif row.get("reason") == "assigned":
            why = "assigned to you"
        elif _mine(row) or contribution:
            why = "yours" if _scope(row["repo"], remotes, places) else "upstream"
        elif row["kind"] == "issue" and str(row["repo"]).lower() in intake:
            why = f"opened by {row['author']}" if row.get("author") else "opened by someone else"
        else:
            continue
        at, what = _activity(row)
        url = row["url"]
        entry = {"key": url, "kind": row["kind"], "repo": row["repo"], "number": row["number"],
                 "ref": f"{row['repo']}#{row['number']}", "title": row.get("title") or "",
                 "url": url if url.startswith("https://github.com/") else None, "why": why, "state": state,
                 "at": at, "at_what": what, "item": contribution.get("item_id") or delivered.get(key)}
        if state == "open":
            found["pulls" if row["kind"] == "pull" else "issues"].append(entry)
        elif what not in ("merged", "closed"):
            found["undated"] += 1
        elif _moment(at) > since:
            found["settled"].append(entry)

    try:
        for entry in (*found["pulls"], *found["issues"], *found["settled"]):
            for ref in dict.fromkeys((f"github:{entry['ref']}", f"github:{entry['ref']}".lower())):
                held = None if entry["item"] else writes.item_by_external(connection, REF_SOURCE, ref)
                entry["item"] = entry["item"] or (held["id"] if held is not None else None)
    except READ_ERRORS as failure:
        unread["refs"] = str(failure) or "the items filed under a GitHub ref could not be read"
    # Newest activity first; a row with no stamp last, then by address so a tie keeps one order.
    for key in ("pulls", "issues", "settled"):
        found[key].sort(key=lambda entry: entry["key"])
        found[key].sort(key=lambda entry: (entry["at"] is not None, _moment(entry["at"]) or moment), reverse=True)
    return found
