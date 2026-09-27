"""Operations > Trackers: PRs and Issues from `sd_db.shadow` (sd:719 step 4).

The pack dashboard's PRs and Issues tabs read `index.sqlite`, a cache the pack
filled with its own `sqlite3` connection -- the one thing a port of those
tabs could not carry, because nothing in this package opens a database of its
own (criterion 2). So the views land here as an Operations area, beside Ports
and Resources, the two other pack tabs that came across, and read the rows
`sd shadow sync` keeps in `shadow` through `progress.tracker_items`, with each
tracker's health from `progress.tracker_freshness`. Both functions already
take a tracker argument; this module adds no query of its own.

A row's reference is the last path segment of its URL, `url.rpartition("/")[2]`,
for both trackers -- the rule sd:361 step 7b wrote into the pack's `where`,
carried across rather than reinvented. It is derived and not stored: a `key`
column would be a second copy of a fact the URL already holds (sd:603). For
Jira it is the ticket, `LOG-23818`; the `repo` column is the project, `LOG`,
and showing that would name the wrong thing. A GitHub row reads `repo#tail`.
"""

from datetime import datetime
from urllib.parse import urlsplit

from sd_db import progress
from sd_db.shadow_sync import TRACKERS

from .listing import Column, Listing
from .markup import join, tag
from .screens import readable_time

#: The two views the pack had. PRs are GitHub `pull` rows; Issues are the
#: `issue` rows of every tracker in `TRACKERS`, Jira's among them.
VIEWS = (("prs", "PRs"), ("issues", "Issues"))
KINDS = {"prs": ("pull",), "issues": ("issue",)}


def reference(row):
    """What names the row: `LOG-23818`, or `acme/widgets#4321` on GitHub."""
    tail = (row["url"] or "").rpartition("/")[2]
    return f"{row['repo']}#{tail}" if row["tracker"] == "github" and row["repo"] else tail


def _link(row):
    url = row["url"] or ""
    try:
        parsed = urlsplit(url)
        # `hostname`, not `netloc`: `https://@/x` has an authority and no host.
        safe = parsed.scheme == "https" and bool(parsed.hostname)
    except ValueError:
        safe = False
    label = reference(row)
    return tag("a", label, href=url, rel="noopener noreferrer") if safe else tag("span", label)


def _rows(connection, view):
    trackers = ("github",) if view == "prs" else TRACKERS
    kinds = KINDS[view]
    return [row for tracker in trackers
            for row in progress.tracker_items(connection, tracker=tracker)
            if (row["kind"] or "") in kinds]


def _health(connection, view, now):
    moment = datetime.fromisoformat(now.replace("Z", "+00:00"))
    lines = []
    for tracker in ("github",) if view == "prs" else TRACKERS:
        freshness = progress.tracker_freshness(connection, tracker=tracker, now=moment)
        state = "never synced" if freshness["state"] == "never" else freshness["state"]
        text = f"{tracker}: {state}"
        if freshness["last_success_at"]:
            text = join((text, " · last successful sync ", readable_time(freshness["last_success_at"])))
        if freshness["reason"]:
            text = join((text, " · ", freshness["reason"]))
        lines.append(tag("li", text))
    return tag("ul", join(lines), class_="hint tracker-health")


def trackers_panel(connection, parameters, *, now):
    view = (parameters.get("view") or ["prs"])[0]
    if view not in dict(VIEWS):
        view = "prs"
    query, number, selected = Listing.read_query(parameters)
    listing = Listing("trackers", (
        Column("reference", "Reference", _link, text=reference, css="tracker-reference"),
        Column("title", "Title", lambda row: row["title"] or ""),
        Column("repo", "Repository" if view == "prs" else "Repository or project", lambda row: row["repo"] or ""),
        Column("author", "Author", lambda row: row["author"] or ""),
        Column("state", "State", lambda row: row["state"] or ""),
        Column("last_seen", "Last seen", lambda row: readable_time(row["last_seen"]), text=lambda row: row["last_seen"]),
    ), _rows(connection, view), path="/operations", query=query, page_number=number,
        selected=selected, extra={"area": "trackers", "view": view},
        empty="No open rows in the local snapshot for this view.")
    # Through the listing's own link, as the Backlog's view toggle is, so the
    # filter and the page ride along: a toggle written as a bare path reset
    # both, and switching from a filtered or paged view lost it (PR #411
    # review). A page past the other view's count lands on its last page,
    # which is what `Listing.page` does with any page number.
    navigation = tag("nav", join(tag("a", label, href=listing.link(view=key),
        class_="toggle-current" if key == view else None,
        aria_current="page" if key == view else None) for key, label in VIEWS),
        class_="view-toggle", aria_label="Trackers")
    return tag("section", tag("h2", "Trackers"), navigation,
        tag("p", "Open pull requests and issues as `sd shadow sync` last saw them. External state is context and does not hold a local item open.", class_="hint"),
        _health(connection, view, now), listing.render())
