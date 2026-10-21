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

Copilot review (sd:5633) is this view's one write: `POST /api/today/github/copilot-review` `{repo, number}` asks GitHub
for one Copilot review of an open pull request, once per press, through the reviewer endpoint sd-ship's
`--copilot-review request` calls. The pull request must be one `sd shadow sync` saw open in that repository; nothing is
recorded here, and the lane's own request count is sd-ship's. An answer that proves GitHub or gh refused is a 409; any
other failure is a 502 that says whether Copilot was asked is not known.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import timedelta

from sd_db import contributions, progress, reads, repos, writes
from sd_db.errors import SdDbError

from .contribution_screen import OPEN_LANES, _freshness, _moment, _registered, _scope

#: The settled window, in days.
DAYS = 7
#: The source of an item filed under a tracker ref (`review.REF_SOURCE`).
REF_SOURCE = "task-ref"
READ_ERRORS = (SdDbError, sqlite3.Error, ValueError, TypeError, KeyError)
#: The shadow's own columns a row's Details show (sd:5633); a projection-only row has none of them.
FACTS = ("author", "branch", "assignee", "created_at", "updated_at", "merged_at", "closed_at")


def _checkouts(connection) -> dict[str, str]:
    """Remote identity -> the registered repository's stored path, the first in path order: the checkout an
    Adversarial review of one of its pull requests runs in (`adversarial._resolve_pull`)."""
    found = {}
    for row in repos.registered(connection):
        identity = repos.remote_identity(row["remote"])
        if identity:
            found.setdefault(identity, row["path"])
    return found


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
    did not read leaves out, by source) and `error`, the reason nothing could be read or "".

    Each row also carries what its Details show (sd:5633): the shadow's `FACTS`, `attention` (the projection's reasons
    it waits on the operator) and `checkout`, the registered repository holding it, or None."""
    moment = _moment(now)
    since = moment - timedelta(days=DAYS)
    found = {"now": now, "days": DAYS, "pulls": [], "issues": [], "settled": [], "undated": 0, "unread": {},
             "collector": _freshness(connection, now), "error": ""}
    try:
        shadow = progress.tracker_items(connection, tracker="github", state=None)
        # GitHub reads an owner and a name in any case, and the two stores do not always agree on one: keyed lower-cased.
        projected = {row["url"].lower(): row for row in contributions.projection(connection) if row.get("url")}
        remotes, places = _registered(connection)
        checkouts = _checkouts(connection)
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
                 "at": at, "at_what": what, "item": contribution.get("item_id") or delivered.get(key),
                 # sd:5633: what a row's Details show, each None when the store did not record it.
                 **{name: row.get(name) for name in FACTS}, "attention": list(contribution.get("reasons") or []),
                 "checkout": checkouts.get(f"github.com/{str(row['repo']).lower()}")}
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


#: The reviewer GitHub's endpoint takes for Copilot: sd-ship's `COPILOT_REVIEWER` (`sd_ship_remote.py`).
COPILOT = "copilot-pull-request-reviewer[bot]"
COPILOT_TIMEOUT = 60
SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def copilot_request(values: dict):
    """The POST's checks, before the server opens a connection; the request as `action_route` returns one."""
    repo, number = values.get("repo"), values.get("number")
    if (set(values) != {"repo", "number"} or not isinstance(repo, str) or not SLUG.fullmatch(repo)
            or type(number) is not int or not 1 <= number <= 10_000_000):
        raise ValueError("A Copilot review takes a pull request's repo, as owner/name, and its number.")
    return lambda connection: copilot_review(connection, repo, number)


def copilot_review(connection: sqlite3.Connection, repo: str, number: int, *, run=subprocess.run):
    """`(status, document)`: 200 once GitHub answered with this pull request, 409 refused, 502 not known."""
    from .issue_filing import GH, GH_REFUSED

    # The pull request must be one the shadow sync saw open in this repository: the number alone could name another's.
    url = f"https://github.com/{repo}/pull/{number}".lower()
    if not any((row.get("url") or "").lower() == url for row in progress.tracker_items(connection, tracker="github")):
        return 409, {"error": f"{repo}#{number} is not an open pull request as sd shadow sync last saw it; "
                              f"Copilot was not asked"}
    try:
        done = run(["sh", "-c", GH, "sh", "api", f"repos/{repo}/pulls/{number}/requested_reviewers", "--method", "POST",
                    "--input", "-"], input=json.dumps({"reviewers": [COPILOT]}), capture_output=True, text=True,
                   timeout=COPILOT_TIMEOUT, start_new_session=True, check=False)
    except subprocess.TimeoutExpired:
        return 502, {"error": f"gh gave no answer in {COPILOT_TIMEOUT} s, so whether Copilot was asked is not known"}
    except OSError as problem:
        return 409, {"error": f"gh did not run ({type(problem).__name__}); Copilot was not asked"}
    if done.returncode != 0:
        said = [line for line in done.stderr.splitlines() if line.strip()]
        why = f"gh exited {done.returncode}: {said[-1] if said else 'no message'}"
        # A nonzero exit is no proof: GitHub can record the request and the answer still fail on the way back. Exit 127
        # is the shell's own: gh was not found, so nothing was sent.
        if done.returncode == 127 or GH_REFUSED.search(done.stderr):
            return 409, {"error": f"{why}; Copilot was not asked"}
        return 502, {"error": f"{why}, so whether Copilot was asked is not known"}
    try:
        answer = json.loads(done.stdout)
        named = answer["number"] == number and answer["base"]["repo"]["full_name"].lower() == repo.lower()
    except (ValueError, TypeError, KeyError):
        named = False
    if not named:
        return 502, {"error": f"gh exited 0 but its answer did not name {repo}#{number}, so whether Copilot was "
                              f"asked is not known"}
    return 200, {"repo": repo, "number": number, "requested": COPILOT}
