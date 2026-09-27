"""Operations > Sessions: registered worktrees and running sd-* commands (sd:719 step 5).

The pack dashboard's Sessions tab, carried across as an Operations area. Two
lists from `fleet.collect("sessions")`: every worktree the fleet's checkouts
have registered under `.git/worktrees/`, abandoned ones first, and every
`sd-*` command in the process table. Neither is a ledger anybody wrote: a
worktree is registered by git and a running command is in the process
table, which is the pack's reason for reading them and not recording them.

The two halves fail separately. The worktree half is file reads and cannot
hang on its own; the process half is one `ps`, and a `ps` that did not
answer is said on the page beside the worktrees that did, rather than
costing them or passing for a quiet machine.
"""

from .fleet import CEILING, STATES, collect
from .listing import Column, Listing
from .markup import join, tag

#: What each list calls its own filter and page in the query string. Two
#: lists, two states: `?processes-q=sd-review` narrows the Running table and
#: leaves the worktrees whole.
WORKTREE_KEYS = "worktrees-"
PROCESS_KEYS = "processes-"


def _rows(document, key, fields):
    rows = document.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"fleet collector returned incomplete {key}")
    for row in rows:
        if any(not isinstance(row.get(field), str) for field in fields):
            raise ValueError(f"fleet collector returned incomplete {key}")
    return rows


def _nothing(query, cut):
    """What an empty process list says, which a cut table may not turn into an absence.

    A table cut at the ceiling holds an unknown number of rows past it, so
    "no sd-* command is running" is a claim the read cannot support, and a
    filter over it is narrower still (Codex review of PR #500). Only a whole
    table answers the question it was asked.
    """
    if cut:
        return (f"The table was cut at {CEILING}, so no command below the cut was read."
                + (" Nothing above it matches this filter." if query else ""))
    return "No sd-* command matches this filter." if query else "No sd-* command is running."


def sessions_panel(parameters, *, backend=None):
    """Compose one Operations panel; `backend` is a no-argument fixture seam."""
    try:
        document = (backend or (lambda: collect("sessions")))()
        trees = _rows(document, "worktrees", ("repo", "name", "path", "branch"))
        procs = _rows(document, "processes", ("pid", "elapsed", "command"))
        if any(not isinstance(row.get("live"), bool) or row.get("state") not in STATES for row in trees):
            raise ValueError("fleet collector returned incomplete worktrees")
        error = document.get("processes_error")
        if not isinstance(error, str):
            raise ValueError("fleet collector returned incomplete processes")  # noqa: TRY004 - external document validation
        table_cut = document.get("processes_truncated")
        if not isinstance(table_cut, bool):
            raise ValueError("fleet collector returned incomplete processes")  # noqa: TRY004 - external document validation
        root = str(document.get("root") or "")
    except (OSError, ValueError, TypeError) as failure:
        return tag("section", tag("h2", "Sessions"),
            tag("p", "Sessions could not be observed: " + str(failure), class_="notice"),
            tag("p", "No worktree or process state is inferred from a failed read; refresh to run the collector again.", class_="hint"))
    # One state per list, not one for the page. The two are independent row
    # sets, so a term that narrows the processes has nothing to say about the
    # worktrees, and page two of one is not page two of the other. Each list
    # carries the other's state in `extra`, so a filter or a page link on one
    # passes the other's along unchanged (PR #427 review).
    given = parameters or {}
    query, number, selected = Listing.read_query(given, keys=WORKTREE_KEYS)
    process_query, process_number, process_selected = Listing.read_query(given, keys=PROCESS_KEYS)
    worktrees = Listing("worktrees", (
        Column("repo", "Repository", lambda row: row["repo"], css="worktree-repo"),
        Column("name", "Worktree", lambda row: row["name"], css="worktree-name"),
        Column("branch", "Branch", lambda row: row["branch"], css="worktree-branch"),
        Column("state", "State", lambda row: row["state"].capitalize(), css="worktree-state"),
        Column("path", "Path", lambda row: row["path"] or "(unreadable registration)", css="worktree-path"),
    ), trees, path="/operations", query=query, page_number=number, selected=selected,
        keys=WORKTREE_KEYS,
        extra={"area": "sessions", **Listing.carried(PROCESS_KEYS, process_query, process_number)},
        empty="No worktrees match this filter." if query else "No checkout has a worktree registered.")
    processes = Listing("processes", (
        Column("pid", "PID", lambda row: row["pid"], css="process-pid"),
        Column("elapsed", "Elapsed", lambda row: row["elapsed"], css="process-elapsed"),
        Column("command", "Command", lambda row: row["command"], css="process-command"),
    ), procs, path="/operations", query=process_query, page_number=process_number,
        selected=process_selected, keys=PROCESS_KEYS,
        extra={"area": "sessions", **Listing.carried(WORKTREE_KEYS, query, number)},
        empty=_nothing(process_query, table_cut))
    abandoned = sum(1 for row in trees if row["state"] == "abandoned")
    unknown = sum(1 for row in trees if row["state"] == "unknown")
    summary = f"{len(trees)} registered worktrees · {abandoned} abandoned"
    if unknown:
        summary += f" · {unknown} unreadable"
    if not error:
        # A cut `ps` table counted what arrived, so the count is a floor and
        # says so, the way a cut `git status` does on Repos.
        counted = f"≥ {len(procs)}" if table_cut else str(len(procs))
        summary += f" · {counted} sd-* command{'' if len(procs) == 1 else 's'} running"
    # A root that is not there is said in place of the worktree count, as
    # Repos says it: no registry was read, so "0 registered" would be a claim
    # about a directory that does not exist. The process table does not
    # depend on the root and is still shown.
    if document.get("rootExists"):
        registry = join((tag("p", summary, class_="hint"),
                         tag("h3", "Worktrees"),
                         tag("p", "Every registration under a checkout's .git/worktrees, read from git's own files. An abandoned one names a directory that is gone and still holds its branch; git worktree prune clears it.", class_="hint"),
                         worktrees.render()))
    else:
        registry = tag("p", f"The checkout root {root} does not exist. REPO_ROOT names it; ~/repos is the default.", class_="notice")
    return tag("section", tag("h2", "Sessions"),
        registry,
        tag("h3", "Running"),
        join((tag("p", "The process table could not be read: " + error, class_="notice"),)) if error else
        join((tag("p", "Every sd or sd-* command in the process table at this read, by the command's basename."
                  + (f" The table was cut at {CEILING}, so these are a floor." if table_cut else ""),
                  class_="hint"),
              processes.render())))
