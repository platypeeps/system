"""Contribution views consume the shared ordering and attention decisions."""

import json
import shlex
import sqlite3
from urllib.parse import urlsplit

from sd_db import contributions

from .controls import form
from .listing import Column, Listing
from .markup import join, tag
from .pages import page

# One label per lane in `contributions.LANES`; the screen's test holds the two sets equal.
LABELS = {"newly_unblocked": "Newly unblocked", "awaiting_you": "Awaiting you",
          "awaiting_them": "Awaiting them", "merged": "Merged", "closed": "Closed"}


def _text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value or "Not recorded")


def _link(url, label):
    address = urlsplit(str(url or ""))
    if address.scheme == "https" and address.hostname == "github.com" and not address.username and not address.password:
        return tag("a", label, href=url, rel="noopener noreferrer")
    return tag("span", label)


def _filed(url):
    """The word for what `url` is: a pull request, or a filed issue.

    A row's `url` is whichever of `pull_url`, `issue_url` or the observed
    key's URL the projection found, and the library's `ISSUE` pattern is
    what admits an issue URL into any of those, so it is what tells the two
    apart here. Every filed row was called "Pull request" before this (PR
    #308 review), which made a watched issue read as a pull.
    """
    return "Issue" if contributions.ISSUE.fullmatch(str(url)) else "Pull request"


def _filing(row):
    """What the row is filed as, or which kind of unfiled work it is.

    An issue draft has `draft_path` and no URL; it read as unfiled branch
    work before (sd:1205). `draft_verified` says whether its body still
    matches the recorded digest.
    """
    if row.get("url"):
        return _link(row["url"], _filed(row["url"]))
    if row.get("draft_path"):
        return "Unfiled issue draft" if row.get("draft_verified") else "Unfiled issue draft; body unverified"
    return "Unfiled local work"


def _evidence(row):
    checks = row.get("evidence") or []
    return tag("section", tag("h4", "Local test evidence"),
        tag("p", "Evidence identity verified." if row.get("evidence_verified") else "Evidence identity is unverified.", class_="hint"),
        join(tag("div", tag("p", "Exit code: ", tag("code", check.get("exit_code")),
            " · Commit: ", tag("code", check.get("commit", "Not recorded"))),
            tag("pre", tag("code", shlex.join(check.get("argv") or []))),
            tag("p", "Directory: ", tag("code", check.get("cwd", "Not recorded"))),
            tag("p", "Artifact: ", tag("code", check.get("artifact", "Not recorded"))),
            tag("p", "SHA-256: ", tag("code", check.get("sha256", "Not recorded"))),
            class_="contribution-evidence") for check in checks)
        if checks else tag("p", "No test evidence recorded.", class_="hint"))


def _acknowledgments(row):
    sources = row.get("attention_sources") or []
    return join(form("/api/contributions/acknowledge",
        tag("input", type="hidden", name="key", value=source["key"]),
        join(tag("input", type="hidden", name="event_ids", value=event) for event in source["event_ids"]),
        tag("p", "Acknowledge: ", "; ".join(source["reasons"])),
        label="Acknowledge these events", command=shlex.join(["sd", "task", "contribution", "ack", source["key"],
            *[part for event in source["event_ids"] for part in ("--event", event)],
            "--if-revision", source["revision"]]), revision=source["revision"], compact=True)
        for source in sources if source["event_ids"])


def _dependencies(row):
    entries = []
    for dependency in row.get("depends_on") or []:
        kind = dependency["kind"]
        if kind == "item":
            detail = tag("a", f"Complete local item #{dependency['item']}", href=f"/item/{dependency['item']}")
        elif kind == "merge":
            detail = join(("Merge ", _link(dependency["url"], dependency["url"])))
        elif kind == "issue":
            detail = join(("Resolve issue ", _link(dependency["url"], dependency["url"])))
        else:
            package = dependency.get("package")
            detail = join(("Publish ", dependency["repo"], " release ", tag("code", dependency.get("tag") or "Tag not selected"),
                " containing ", _link(dependency["contains_pull"], dependency["contains_pull"]),
                f"; package {package['name']} {package.get('version') or 'Version not selected'}" if package else ""))
        entries.append(tag("li", detail))
    return tag("section", tag("h4", "Dependencies"), tag("ul", join(entries)) if entries else tag("p", "No dependencies recorded."))


def _notifications(row):
    entries = [tag("li", notice["event"]["reason"], ": ", notice["status"].replace("_", " "),
        " · Acknowledged " + notice["acknowledged_at"] if notice.get("acknowledged_at") else "")
        for source in (row.get("notification_state") or {}).values() for notice in source.values()]
    return tag("section", tag("h4", "Notification delivery"),
        tag("ul", join(entries)) if entries else tag("p", "No notifications recorded."))


def card(row, *, details=True):
    identity = tag("a", f"Local item #{row['item_id']}", href=f"/item/{row['item_id']}") if row.get("item_id") else "No local item"
    return tag("article", tag("h3", row["title"]),
        tag("p", row.get("repo") or "Local contribution", " · ", identity, " · ",
            _filing(row), class_="hint"),
        tag("p", "External: ", _text(row.get("external_state")), " · Local: ", _text(row.get("local_status"))),
        tag("ul", join(tag("li", reason) for reason in row["reasons"])) if row["reasons"] else "",
        tag("p", "Blocked on: ", row["blocked_on"]) if row.get("blocked_on") else "",
        tag("details", tag("summary", "Branch, dependencies and evidence"),
            tag("dl", join(tag("div", tag("dt", label), tag("dd", tag("code", _text(row.get(key)))))
                for key, label in (("local_clone", "Local clone"), ("local_branch", "Branch"),
                                   ("tested_commit", "Tested commit")))),
            _dependencies(row), _notifications(row),
            _evidence(row), tag("p", "Recorded commands are evidence. This page does not run them.", class_="hint")) if details else "",
        _acknowledgments(row) if details else "",
        data_contribution_key=row["key"], data_lane=row["lane"], class_="contribution-card")


def preview(connection):
    rows = contributions.projection(connection)
    return tag("section", tag("h2", "Contributions"),
        tag("p", "External activity and dependency readiness are separate from local task completion.", class_="hint"),
        join(tag("div", tag("p", LABELS.get(row["lane"], row["lane"]), class_="contribution-lane"),
            card(row, details=False)) for row in rows[:5]),
        tag("p", f"Showing {min(5, len(rows))} of {len(rows)} contributions. ",
            tag("a", "Open contributions and acknowledgments", href="/contributions"))
        if rows else tag("p", "No contributions recorded. ", tag("a", "Open contributions", href="/contributions")),
        class_="contribution-preview")


def render(connection, *, parameters):
    rows = contributions.projection(connection)
    query, number, selected = Listing.read_query(parameters)
    listing = Listing("contributions", [
        Column("lane", "Waiting on", lambda row: LABELS.get(row["lane"], row["lane"])),
        Column("title", "Contribution", card, text=lambda row: " ".join(_text(row.get(key))
            for key in ("title", "repo", "local_status", "external_state", "reasons", "blocked_on", "local_branch", "depends_on"))),
        Column("observed_at", "Last observation", lambda row: tag("div",
            tag("p", _text(row.get("observed_at"))),
            tag("p", row["freshness"]["status"], ": ", row["freshness"].get("reason") or "Complete observation", class_="hint"))),
    ], rows, path="/classic/contributions", query=query, page_number=number, selected=selected,
        empty="No contributions recorded. Register local work or collect authored pull requests.")
    return page("Contributions", "contributions",
        tag("p", "Newly unblocked work comes first, followed by work awaiting you, awaiting others, and merged contributions.", class_="lead"),
        tag("p", "Unknown or stale observations remain visible. Acknowledging events does not complete tasks or send notifications.", class_="hint"),
        listing.render(), cli_equivalents=False)


# ---------- The default UI's Contributions page (sd:2113) ----------
# `v2/contributions.html` holds no rows. It reads one JSON document, `/api/contributions/page`, built here by `document`
# from the same `contributions.projection` the screen above renders; nothing here reads GitHub or writes.

#: Lanes that list rows on the page, in the order the page shows them; merged and closed rows are counted, never listed.
OPEN_LANES = ("newly_unblocked", "awaiting_you", "awaiting_them")
#: Open rows the document carries at most; `open_total` still counts every one, and `truncated` says rows were left out.
OPEN_LIMIT = 500
#: Repositories with settled rows the document names at most; the rest fold into `settled_other`, per scope.
SETTLED_REPOS = 100
#: What the page may show of a row: the projection's fields it reads, nothing more (no evidence, argv or local paths).
#: `repo` is rewritten by `_shown` and `draft_path` becomes `has_draft`: the page only asks whether a draft exists.
ROW_FIELDS = ("key", "revision", "event_ids", "reasons", "lane", "title", "url", "external_state", "local_status",
              "item_id", "local_branch", "blocked_on", "observed_at")


def _registered(connection):
    """The sd repo table as two lookups: remote identity -> managed, and stored path -> managed."""
    from sd_db import repos

    remotes, places = {}, {}
    for row in repos.registered(connection):
        identity = repos.remote_identity(row["remote"])
        if identity:
            remotes[identity] = remotes.get(identity, False) or bool(row["managed"])
        places[row["path"]] = places.get(row["path"], False) or bool(row["managed"])
    return remotes, places


def _scope(repo, remotes, places):
    """`managed` or `registered` when sd's repo table holds the row's repository, else None (external)."""
    from sd_db import paths

    text = str(repo or "")
    if contributions.REPO.fullmatch(text):
        found = remotes.get(f"github.com/{text.lower()}")
    else:
        found = next((places[key] for key in paths.keys(text) if key in places), None) if text else None
    return None if found is None else "managed" if found else "registered"


def _shown(repo):
    """The repository as the page shows it: `owner/name`, or `local: <folder>` for a checkout path, never the path itself."""
    text = str(repo or "")
    if not text or contributions.REPO.fullmatch(text):
        return text or None
    return "local: " + (text.rstrip("/").rpartition("/")[2] or "checkout")


def _freshness(connection, now):
    from datetime import datetime

    from sd_db import progress

    try:
        found = progress.tracker_freshness(connection, tracker="github", now=datetime.fromisoformat(now.replace("Z", "+00:00")))
    except (ValueError, sqlite3.Error) as error:
        return {"state": "unknown", "last_success_at": None, "reason": f"not read: {error}"}
    return {"state": found["state"], "last_success_at": found["last_success_at"], "reason": found["reason"]}


def document(connection, *, now):
    """The Contributions page's document: open rows by lane, settled rows counted per repository, collector freshness."""
    rows = contributions.projection(connection)
    remotes, places = _registered(connection)
    lanes = dict.fromkeys(contributions.LANES, 0)
    fresh = {"current": 0, "unknown": 0}
    opened, settled = [], {}
    # Per scope, over every open row, so a cut list still counts in full: lanes, unknown freshness, linked and unfiled.
    counts = {side: dict.fromkeys((*OPEN_LANES, "unknown", "linked", "unfiled"), 0) for side in ("internal", "external")}
    for row in rows:
        lanes[row["lane"]] = lanes.get(row["lane"], 0) + 1
        status = (row.get("freshness") or {}).get("status")
        fresh["current" if status == "current" else "unknown"] += 1
        why = _scope(row.get("repo"), remotes, places)
        if row["lane"] in OPEN_LANES:
            side = counts["internal" if why is not None else "external"]
            side[row["lane"]] += 1
            side["unknown"] += status != "current"
            side["linked"] += bool(row.get("item_id"))
            side["unfiled"] += not row.get("url")
            opened.append({key: row.get(key) for key in ROW_FIELDS} | {
                "repo": _shown(row.get("repo")), "has_draft": bool(row.get("draft_path")),
                "freshness": {"status": status or "unknown", "reason": (row.get("freshness") or {}).get("reason") or ""},
                "internal": why is not None, "why_internal": why})
        else:
            name = _shown(row.get("repo")) or "local"
            entry = settled.setdefault(name, {"repo": name, "internal": why is not None, "merged": 0, "closed": 0})
            entry["merged" if row["lane"] == "merged" else "closed"] += 1
    # Newest observation first within a lane, never-observed rows last: two stable sorts.
    opened.sort(key=lambda row: str(row["observed_at"] or ""), reverse=True)
    opened.sort(key=lambda row: OPEN_LANES.index(row["lane"]))
    ranked = sorted(settled.values(), key=lambda entry: (-(entry["merged"] + entry["closed"]), entry["repo"]))
    other = {"internal": {"merged": 0, "closed": 0}, "external": {"merged": 0, "closed": 0}}
    for entry in ranked[SETTLED_REPOS:]:
        side = other["internal" if entry["internal"] else "external"]
        side["merged"] += entry["merged"]
        side["closed"] += entry["closed"]
    return {
        "read": now,
        "total": len(rows),
        "lanes": lanes,
        "fresh": fresh,
        "open_total": len(opened),
        "truncated": len(opened) > OPEN_LIMIT,
        "counts": counts,
        "rows": opened[:OPEN_LIMIT],
        "settled": ranked[:SETTLED_REPOS],
        "settled_other": other,
        "collector": _freshness(connection, now),
    }
