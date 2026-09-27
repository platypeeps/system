"""Contribution views consume the shared ordering and attention decisions."""

import json
import shlex
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
    ], rows, path="/contributions", query=query, page_number=number, selected=selected,
        empty="No contributions recorded. Register local work or collect authored pull requests.")
    return page("Contributions", "contributions",
        tag("p", "Newly unblocked work comes first, followed by work awaiting you, awaiting others, and merged contributions.", class_="lead"),
        tag("p", "Unknown or stale observations remain visible. Acknowledging events does not complete tasks or send notifications.", class_="hint"),
        listing.render(), cli_equivalents=False)
