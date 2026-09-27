"""Now: the fleet's loudest facts, ranked, at the top of Today (sd:719 step 6).

The pack dashboard's Now view, carried across last because it is the merge
of every source the earlier steps moved: the checkout fleet (Repos and
Sessions, step 5, read by `fleet.collect` as a budgeted child) and the pull
requests `sd shadow sync` keeps in `shadow` (Trackers, step 4, read through
`progress.tracker_items`). The ranking, the row text and the ids are the
pack's `dashboard/now.py` at 85c4fa1b, function for function, with the same
rank numbers; the one thing the pack left in the page, the `band`
function of the pack's `dashboard/app.js`, is `band` here, because the band is a name for a
server-side number and a test can hold a Python function to the pack's
thresholds where it cannot hold a script.

**The rows arrive by `/api/now`, not in Today's HTML.** Today is the front
door, and the fleet is eighty-one checkouts read by five `git` commands each
(1.1 s at load ~4 on 2026-09-16, 12 s at the budget, twice: one child per
area). Rendering the rows into the page would put that read in front of
every Today load and in front of every test that opens the page --
`BrowserSession.setUp` GETs `/` before each of its tests, and so do the
remote-access and disconnect suites. The pack answered the same problem with
a JSON endpoint and a twenty-second cache; this module keeps the endpoint
and drops the cache, since nothing here is stored. The page carries the
section, its heading, the sources a reader without JavaScript can open
instead, and a Refresh control; the script fetches the document once and on
demand and paints each row's `band` as the class it was told.

**A collector that goes dark is a row, never an empty list.** The design
(question 2) records this as the property the plugin loader had and the
native views gave up until this step: a child that exits non-zero, overruns
its budget, or returns an incomplete document, and a shadow read that
raises, each become a rank-0 row naming the collector and carrying the
reason, so a fleet nobody could read never looks like a fleet with nothing
to say.

**What the shadow row can and cannot say.** The pack ranked a pull request
on how long nobody had touched it, `updated_at` against `STALE_DAYS`. The
`shadow` table has no such column -- `shadow_sync.store` says which two of
the sync's ten keys it does not write, and `updated_at` is one -- so the
days here are since the sync first saw the pull request open, `first_seen`,
and the row says "first seen Nd ago" rather than "quiet Nd". That is age,
the measure the pack's docstring set aside; a column for the tracker's own
stamp is a `local-sd-db` change and not this one. `needs_you` only, as the
pack read `needsYou` only: a pull request that is merely yours is not one
that is waiting on you.

**No dismiss.** The pack's rows carried an ack key and a control that wrote
to an ack store; the decision that made the pack's dashboard read-only
deleted that store (sd:719, note 1347), and nothing here writes. The ids
keep the pack's shapes so the comparison holds row for row and so a later
ack store, if one is wanted, has a key to use.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from sd_db import progress
from sd_db.errors import SdDbError

from . import fleet as fleet_module
from .markup import join, tag

__all__ = ["band", "document", "now_panel"]

# Ranks, not severities. 0 is loudest, and the band is derived from the
# number alone -- `kind` is a category and never a severity (the pack's
# R11-D20). The numbers are the pack's; `tests/test_now_screen.py` holds
# them to a copy taken by hand from `dashboard/now.py` at 85c4fa1b.
DARK = 0
AHEAD = 3
DIRTY = 4
# A pull request nobody has looked at for a fortnight, and one that is simply
# open. `FRESH` shares rank 4 with `DIRTY` deliberately: both are reminders
# rather than problems, and the two tie into one band. Ties break on `id`.
STALE = 2
FRESH = 4
STALE_DAYS = 14
# One row for every abandoned worktree, not one row each.
ABANDONED = 3

#: The three bands, loudest first; the script paints each as a class name.
BANDS = ("broken", "look", "queued")

#: The three sources, in the order the document reads them. Each names an
#: Operations area a reader can open when the script is off.
SOURCES = ("repos", "sessions", "prs")


def band(rank: int) -> str:
    """The pack's `dashboard/app.js` `band` at 85c4fa1b: <= 1 broken, <= 3 look, else queued."""
    if rank <= 1:
        return "broken"
    if rank <= 3:
        return "look"
    return "queued"


def plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def backbone_rows(repos: list[dict]) -> list[dict]:
    """The fleet's own half of Now, from `fleet.collect("repos")["repos"]`.

    Ahead and dirty are one row, not two: a repo with unpushed commits and a
    dirty tree has one thing wrong with it, and the branch that says so is
    the more urgent of the pair. A checkout the child could not read has no
    counts (`dirty` and `ahead` are None) and raises no row here: the Repos
    area says why, row by row, and Now says when the whole collector is dark.
    """
    out: list[dict] = []
    for repo in repos:
        name, branch = repo["name"], repo.get("branch") or "?"
        ahead, dirty = repo.get("ahead") or 0, repo.get("dirty") or 0
        if ahead:
            out.append({
                "rank": AHEAD,
                "kind": "ahead",
                "id": f"ahead:{name}:{ahead}",
                "what": f"{name} has {ahead} unpushed {plural(ahead, 'commit', 'commits')}",
                "detail": f"{branch} · "
                          + (f"{dirty} dirty {plural(dirty, 'file', 'files')}" if dirty else "clean tree"),
                "source": "repos",
            })
        elif dirty:
            out.append({
                "rank": DIRTY,
                "kind": "dirty",
                "id": f"dirty:{name}:{dirty}",
                "what": f"{name} has {dirty} uncommitted {plural(dirty, 'file', 'files')}",
                "detail": f"{branch} · last commit {repo.get('last') or '?'}",
                "source": "repos",
            })
    return out


def stale_days(stamp: str, today: str) -> int | None:
    """Whole days between two `YYYY-MM-DD` prefixes, or None if either is unusable.

    Compared as dates rather than parsed as timestamps, as the pack did: the
    column holds whatever the sync wrote, and a row with a malformed stamp
    must still render -- it just cannot be ranked by one.
    """
    try:
        was = date.fromisoformat(stamp[:10])
        now = date.fromisoformat(today[:10])
    except (TypeError, ValueError):
        return None
    return (now - was).days


def pr_rows(rows: list[dict], today: str) -> list[dict]:
    """Open pull requests that are waiting on you, loudest when quiet.

    `rows` are `progress.tracker_items(connection, tracker="github")`'s;
    the `pull` rows carrying `needs_you` are the ones the contributions
    projection says are waiting on the operator. The rank is in the id
    deliberately, as the pack's was: every other id keys on the fact that
    changed, and a pull request's condition -- fresh or stale -- is the fact.
    """
    out = []
    for row in rows:
        if (row.get("kind") or "") != "pull" or not row.get("needs_you"):
            continue
        days = stale_days(row.get("first_seen") or "", today)
        quiet = days is not None and days >= STALE_DAYS
        rank = STALE if quiet else FRESH
        out.append({
            "rank": rank,
            "kind": "pr",
            "id": f"pr:{row.get('repo')}#{row.get('number')}:{rank}",
            "what": f"{row.get('repo')}#{row.get('number')} open"
                    + (f", first seen {days}d ago" if quiet else ""),
            "detail": row.get("title") or "",
            "source": "prs",
        })
    return out


def session_rows(trees: list[dict]) -> list[dict]:
    """Worktrees the fleet has registered whose directories are gone, as one row.

    Keyed on the count, like the repository rows and for the same reason: an
    ack of the eight that were dismissed should not cover whatever number
    this grows to next week.
    """
    count = sum(1 for tree in trees if not tree.get("live"))
    if not count:
        return []
    return [{
        "rank": ABANDONED,
        "kind": "worktree",
        "id": f"worktrees:{count}",
        "what": f"{count} abandoned {plural(count, 'worktree', 'worktrees')}",
        "detail": "registered in .git/worktrees with no directory left; "
                  "`git worktree prune` clears them",
        "source": "sessions",
    }]


def dark_row(source: str, reason: str) -> dict:
    """The row a collector that went dark raises: rank 0, the collector, the reason."""
    return {
        "rank": DARK,
        "kind": "dark",
        "id": f"dark:{source}",
        "what": f"{source} could not be read",
        "detail": reason,
        "source": source,
    }


def merge(rows: list[dict]) -> list[dict]:
    """Every row there is, loudest first.

    Sorted on `(rank, id)` rather than on rank alone: rank ties are the common
    case, and a sort that leaves them in collection order reshuffles the list
    under the operator every time the fleet answers in a different order.
    """
    return sorted(rows, key=lambda row: (row.get("rank", 9), row.get("id", "")))


def _repos(fleet_read) -> list[dict]:
    document = fleet_read("repos")
    rows = document.get("repos") if isinstance(document, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                                         or (row.get("ahead") is not None and type(row.get("ahead")) is not int)
                                         or (row.get("dirty") is not None and type(row.get("dirty")) is not int)
                                         for row in rows):
        raise ValueError("fleet collector returned an incomplete repos document")
    return backbone_rows(rows)


def _sessions(fleet_read) -> list[dict]:
    document = fleet_read("sessions")
    trees = document.get("worktrees") if isinstance(document, dict) else None
    if not isinstance(trees, list) or any(not isinstance(tree, dict) or not isinstance(tree.get("live"), bool)
                                          for tree in trees):
        raise ValueError("fleet collector returned an incomplete sessions document")
    return session_rows(trees)


def _prs(connection, today: str) -> list[dict]:
    return pr_rows(progress.tracker_items(connection, tracker="github"), today)


def document(connection: sqlite3.Connection, *, now: str, fleet=None) -> dict:
    """The merged, ranked, banded rows, and what each source said if it said nothing.

    `fleet` is `fleet.collect`'s shape, `area -> document`, and the seam
    a test fills; the default runs the child. Each source is guarded on its
    own so one collector's failure is one row and the other two still
    answer. `sources` carries the empty string for a source that was read
    and the reason for one that was not, the same text its row shows.
    """
    read = fleet or fleet_module.collect
    rows: list[dict] = []
    sources: dict[str, str] = {}
    for source, collect in (("repos", lambda: _repos(read)),
                            ("sessions", lambda: _sessions(read)),
                            ("prs", lambda: _prs(connection, now))):
        try:
            found = collect()
        except (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error) as failure:
            reason = str(failure) or f"{source} collector failed without a reason"
            sources[source] = reason
            rows.append(dark_row(source, reason))
            continue
        sources[source] = ""
        rows.extend(found)
    return {"now": now, "sources": sources,
            "rows": [{**row, "band": band(row["rank"])} for row in merge(rows)]}


def now_panel() -> object:
    """The section Today renders; the script fills its rows from `/api/now`.

    What is in the HTML is what a reader without the script needs: the
    heading, the three sources as the Operations areas that show them in
    full, and the fact that the rows are on their way. The table's body is
    the one placeholder row until the script replaces it.
    """
    areas = join((
        tag("a", "Repos", href="/operations?area=repos"), ", ",
        tag("a", "Sessions", href="/operations?area=sessions"), " and ",
        tag("a", "Trackers", href="/operations?area=trackers"),
    ))
    return tag("section",
        tag("h2", "Now"),
        tag("p", join(("Unpushed commits, uncommitted files, abandoned worktrees and pull requests waiting on you, ",
                       "loudest first, read at this moment from ", areas, ". Nothing here is stored.")), class_="hint"),
        tag("table",
            tag("thead", tag("tr", tag("th", "Band"), tag("th", "What"), tag("th", "Detail"), tag("th", "Source"))),
            tag("tbody", tag("tr", tag("td", "Reading the fleet…", colspan="4", class_="now-empty")), data_now_rows=True),
            class_="listing-table now-table"),
        tag("p", "", class_="hint", role="status", aria_live="polite", data_now_status=True),
        tag("div", tag("button", "Refresh", type="button", class_="listing-go", data_now_refresh=True),
            class_="form-actions"),
        tag("noscript", tag("p", join(("Now is filled by the page script. Without it, the same facts are under Operations: ",
                                        areas, ".")), class_="hint")),
        id="now", class_="now-panel", data_now="/api/now")
