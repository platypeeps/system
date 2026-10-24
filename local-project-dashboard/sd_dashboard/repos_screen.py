"""A checkout's state against its remote's default branch, `primary`, which Management reads (sd:2118).

The rows are `fleet.collect("repos")`'s, read by Management's collector child; this module only judges them. The
classic Operations > Repos area that rendered them was retired in sd:3402 (sd:719 step 5 carried it across).
"""

import shlex
from datetime import datetime, timedelta, timezone


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
    """The remedy, or why none is offered; Management's Pull runs the remedy after its own checks (sd:5888)."""
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


