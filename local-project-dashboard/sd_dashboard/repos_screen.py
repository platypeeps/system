"""Operations > Repos: the checkout fleet as git reports it (sd:719 step 5).

The pack dashboard's Repos tab, carried across as an Operations area beside
Ports, Resources and Trackers. The rows are `fleet.collect("repos")`'s, read
by a child under the collectors' `Budget` (the `fleet` docstring says why a
child); this module only renders them. Nothing here opens a database, and
nothing here is stored: the fleet is read on every GET, as the pack read it,
because a checkout list written down anywhere is stale the next time a
branch moves.

An area of its own rather than a view beside Sessions, because the two read
different sources -- git here, the worktree registry and the process table
there -- where Trackers' two views read one table. The nav grows by two
entries, which is what `AREAS` is for.
"""

import shlex
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .fleet import CEILING, collect
from .listing import Column, Listing
from .markup import join, tag
from .screens import readable_time


def _name(row):
    return row["name"] if row["group"] == "." else f"{row['group']}/{row['name']}"


def _link(row):
    url = row.get("web") or ""
    try:
        parsed = urlsplit(url)
        # `hostname`, not `netloc`: `https://@/x` has an authority and no host.
        safe = parsed.scheme == "https" and bool(parsed.hostname)
    except ValueError:
        safe = False
    label = _name(row)
    return tag("a", label, href=url, rel="noopener noreferrer") if safe else tag("span", label)


def _divergence(row):
    if row["ahead"] is None or row["behind"] is None:
        return "unknown"
    return f"+{row['ahead']} / −{row['behind']}"


def _last(row):
    if not row["last_iso"]:
        return ""
    return tag("div", readable_time(row["last_iso"]),
        tag("p", join((row["subject"], " — ", row["author"])) if row["author"] else row["subject"], class_="hint"))


#: A fetch older than this cannot vouch for "not behind" (sd:1676). The fleet
#: never fetches, so a count of zero is only as good as the last fetch.
STALE_FETCH = timedelta(hours=24)


def _age(delta):
    minutes = max(int(delta.total_seconds() // 60), 0)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h" if minutes < 24 * 60 else f"{minutes // (24 * 60)} d"


def _fetch(row, now):
    """`(words, stale)` for the last fetch: how long ago, and whether too long for a zero to count."""
    try:
        when = datetime.fromisoformat(row["fetched_iso"])
    except ValueError:
        return "no fetch recorded", True
    delta = now - when
    return f"fetched {_age(delta)} ago", delta > STALE_FETCH


def _pull(row, target):
    """The remedy, or why none is offered: the dashboard never pulls a checkout it did not open."""
    if row["dirty"] is None or "status" in row["truncated"]:
        return "No pull offered: the tree's state was not fully read."
    if row["dirty"]:
        count = row["dirty"]
        return f"No pull offered: {count} uncommitted {'file' if count == 1 else 'files'}."
    if row["ahead"]:
        return f"No pull offered: {row['ahead']} local {'commit' if row['ahead'] == 1 else 'commits'} not on {target}."
    return f"git -C {shlex.quote(row['path'])} pull --ff-only"


def primary(row, now=None):
    """`(state, headline, detail, remedy)` for a checkout against its remote's default branch.

    `state` is `behind`, `current` or `unknown`. Counted from local refs on the
    default branch only (sd:1676): detached, another branch, no `origin/HEAD`
    and a stale or missing fetch each say why the page cannot tell, and a
    remedy is shown only for a clean checkout on the default branch.
    """
    if row["error"]:
        return "unknown", "?", "", ""
    default = row["default"]
    if not default:
        return "unknown", "unknown", "No default branch: origin/HEAD is not set.", ""
    target = f"origin/{default}"
    if row["branch"] == "HEAD":
        return "unknown", "unknown", f"HEAD is detached; the lag behind {target} is counted on {default} only.", ""
    if row["branch"] != default:
        return "unknown", f"not on {default}", f"On {row['branch']}; the lag behind {target} is counted on {default} only.", ""
    if row["behind_default"] is None:
        return "unknown", "unknown", f"git rev-list could not count HEAD..{target}.", ""
    fetch, stale = _fetch(row, now or datetime.now(timezone.utc))
    count = row["behind_default"]
    if not count:
        if stale:
            return "unknown", "unknown", f"Level with {target} as of the last fetch; {fetch}.", ""
        return "current", "current", f"Level with {target}; {fetch}.", ""
    floor = "at least " if stale else ""
    return ("behind", f"behind {floor}{count}",
            f"{count} {'commit' if count == 1 else 'commits'} behind {target}; {fetch}.", _pull(row, target))


def _default(row):
    _, headline, detail, remedy = primary(row)
    return tag("div", headline,
        tag("p", detail, class_="hint") if detail else "",
        tag("p", tag("code", remedy) if remedy.startswith("git ") else remedy, class_="hint") if remedy else "")


def _default_text(row):
    return " ".join(part for part in primary(row)[1:] if part)


def _rows(document):
    rows = document.get("repos")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("fleet collector returned incomplete rows")
    for row in rows:
        for key in ("name", "group", "path", "branch", "subject", "author", "web", "last_iso", "error",
                    "default", "fetched_iso"):
            if not isinstance(row.get(key), str):
                raise ValueError("fleet collector returned incomplete rows")  # noqa: TRY004 - external document validation
        for key in ("ahead", "behind", "behind_default"):
            if row.get(key) is not None and type(row.get(key)) is not int:
                raise ValueError("fleet collector returned incomplete rows")
        if row.get("dirty") is not None and type(row.get("dirty")) is not int:
            raise ValueError("fleet collector returned incomplete rows")
        cut = row.get("truncated")
        if not isinstance(cut, list) or any(not isinstance(name, str) for name in cut):
            raise ValueError("fleet collector returned incomplete rows")  # noqa: TRY004 - external document validation
    return rows


def _dirty(row):
    # A status the child cut at its ceiling counted what arrived; the number
    # is a floor and is shown as one. A checkout that could not be read has
    # no number, and "?" is not "0".
    #
    # `truncated` names the git commands that were cut, not a single boolean:
    # a cut `log` or `remote` says nothing about the dirt count,
    # and collapsing them made every cut read as a cut `status`.
    if row["dirty"] is None:
        return "?"
    return f"≥ {row['dirty']}" if "status" in row["truncated"] else str(row["dirty"])


def _note(row):
    if row["error"]:
        return row["error"]
    cut = row["truncated"]
    if not cut:
        return ""
    said = f"git {', '.join(cut)} was cut at {CEILING}"
    return f"{said}; the dirt count is a floor" if "status" in cut else said


def repos_panel(parameters, *, backend=None):
    """Compose one Operations panel; `backend` is a no-argument fixture seam."""
    try:
        document = (backend or (lambda: collect("repos")))()
        rows = _rows(document)
        root = str(document.get("root") or "")
    except (OSError, ValueError, TypeError) as error:
        return tag("section", tag("h2", "Repos"),
            tag("p", "Repos could not be observed: " + str(error), class_="notice"),
            tag("p", "No checkout state is inferred from a failed read; refresh to run the collector again.", class_="hint"))
    if not document.get("rootExists"):
        return tag("section", tag("h2", "Repos"),
            tag("p", f"The checkout root {root} does not exist. REPO_ROOT names it; ~/repos is the default.", class_="notice"))
    query, number, selected = Listing.read_query(parameters or {})
    listing = Listing("repos", (
        Column("name", "Repository", _link, text=_name, css="repo-name"),
        Column("branch", "Branch", lambda row: row["branch"], css="repo-branch"),
        Column("dirty", "Dirty", _dirty, css="repo-dirty"),
        Column("divergence", "Ahead / behind", _divergence, css="repo-divergence"),
        Column("default", "Default branch", _default, text=_default_text, css="repo-default"),
        Column("last", "Last commit", _last, text=lambda row: " ".join((row["last_iso"], row["subject"], row["author"])), css="repo-last"),
        Column("path", "Path", lambda row: row["path"], css="repo-path"),
        Column("note", "Note", _note, css="repo-note", hide_empty=True),
    ), rows, path="/operations", query=query, page_number=number, selected=selected,
        extra={"area": "repos"}, empty="No checkouts match this filter." if query else f"No checkouts under {root}.")
    dirty = sum(1 for row in rows if row["dirty"])
    ahead = sum(1 for row in rows if row["ahead"])
    unread = sum(1 for row in rows if row["error"])
    behind = sum(1 for row in rows if primary(row)[0] == "behind")
    return tag("section", tag("h2", "Repos"),
        tag("p", f"{len(rows)} checkouts under {root} · {dirty} dirty · {ahead} ahead · {behind} behind default", class_="hint"),
        tag("p", f"{unread} checkout{'' if unread == 1 else 's'} could not be read; each says why in its row.",
            role="status") if unread else "",
        tag("p", "Every checkout under the root, one level of grouping deep, as git reports it right now. Nothing here is stored; a branch shown is the branch checked out at this read.", class_="hint"),
        tag("p", "Default branch compares each checkout with its origin's default branch as last fetched. The dashboard never fetches or pulls; run the command shown in a checkout you own.", class_="hint"),
        listing.render())
