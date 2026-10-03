"""The checkout fleet, read at request time: Repos and Sessions (sd:719 step 5).

The pack dashboard's Repos and Sessions tabs read nothing but the filesystem,
git and the process table -- "Nothing here is stored as an input to anything"
is the docstring of the pack's `dashboard/collect.py`, and this module keeps
that rule: it opens no database and writes no file. What it carries across is
the pack's `discover_checkouts` (`checkouts` here), the worktree registry
reader and the `ps` filter of `dashboard/sessions.py`, and the row shapes
`/api/state` and `/api/sessions` answered with, so the one capture taken
before this commit compares row for row. `git_facts` is carried too, as
`facts`, for one reason: the `collectors.run` it reads through holds a
command's whole answer, and a `git status --porcelain` of a checkout with
enough untracked paths would hold as much as git wrote. Here every git read
goes through `read_output`, which holds at most the collectors' ceiling and
says when it stopped there (PR #427 review).

**One module, two roles.** Imported as `sd_dashboard.fleet`, it is the page
side: `collect(area)` runs this same file as a child, `python3 -I fleet.py
<area> <seconds> <since>`, under the collectors' `Budget`, and hands the
child's JSON to the screen. Run as that child, it loads `collectors.py` by
path, reads the fleet and prints the document. The two roles share nothing at
runtime but the constants below and the page's start, which is the point of
keeping them in one file: the child's deadline is the page's budget less the
margin, and a reader sees both numbers next to each other.

**Why a child and not an in-process read.** The Ports area reads its
collector in-process with the collector's own budget; a Resources view runs
`sd_tile.py` as a child under `VIEW_SECONDS`. This is the second shape, for a
reason the first cannot give: Repos runs five `git` commands per checkout,
eighty-one checkouts on 2026-09-16, and a checkout that hangs -- a stalled
network filesystem, a `git` waiting on a credential prompt -- has to be cut
without the server process waiting on it. `collectors.run` starts each
command in a session of its own so its timeout can kill the whole tree, and
`collectors.set_deadline` caps every one of those timeouts at one deadline
for the whole collection. Both are the child's: it sets the deadline at
`FLEET_SECONDS - FLEET_MARGIN` after the page started its `Budget`, so a
hung command is killed by the child and its reason reaches the page before
the page's own `Budget` kills the child at `FLEET_SECONDS`. The margin is the
tile's one second, for the reason `sd_tile.TILE_MARGIN` gives: a child that
loses the race to the outer kill leaves the page with "python ran past its
budget" and nothing about which checkout.

The page passes its start as `<since>`, a `CLOCK_MONOTONIC` reading, the
one clock both processes share. `sd_tile.main` and `research_screen.main`
take the same reading since sd:2501, and this child counted from its own
start until sd:2244: an interpreter that took a second to start under load
spent that second of the margin before its deadline began, and the page's
kill then arrived first.

A checkout that does not answer is a row, not a refusal. The rest of the
fleet was read by the other workers while that one waited, and dropping
eighty rows because one hung would hide the fleet to report the hang; the
row carries the reason in `error` and the page shows it. The whole
collection is refused only when the child itself dies or overruns.

**Budget.** `FLEET_SECONDS` is 12, the figure `ports_screen` read
`collect_ports` under before sd:722, and not the 5 seconds of a Resources
view: measured on 2026-09-16 through the pack's collectors at load average
~4, the fleet of 81 answered in 1.11 s with 8 workers, and the Resources
views' 5 seconds have been seen refused at load 11 to 45 on this machine.
Twelve is ten times the quiet measurement. The byte ceiling is the shared
`BUDGET_BYTES`, 64 KB, applied twice: the page holds at most that much of the
child's document -- the same 81 checkouts printed 29.6 KB, so the ceiling
holds a fleet about twice this size before the page refuses it with the
reason, and that is the day to file the number rather than raise it here --
and the child holds at most that much of any one git answer. A status past
it is cut there and the row says so, with the dirt it counted as a floor,
because one checkout with ten thousand untracked files is that checkout's
fact and not a reason to refuse the fleet or to let git decide how much
memory the child holds.
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

#: The two areas this child serves; each is an Operations area of its own
#: because each is its own source: git for Repos, the worktree registry and
#: the process table for Sessions.
AREAS = ("repos", "sessions")
#: The page's budget for one collection, and how much sooner the child gives
#: up so that its reason arrives before the page's kill does.
FLEET_SECONDS = 12.0
FLEET_MARGIN = 1.0
#: This file, run as the child; a page never supplies the path.
FLEET = Path(__file__).resolve()
#: `git` commands per checkout run this wide, the pack's figure: the fleet is
#: read in about an eighth of the sequential time, and a hung checkout holds
#: one worker while the other seven finish the rest.
GIT_WORKERS = 8
#: One `ps` for the whole machine, as the pack read it, and its own timeout;
#: the child's deadline caps it lower when less is left.
PS = ["ps", "-Ao", "pid=,etime=,command="]
PS_SECONDS = 5
#: One git command's own timeout, `collectors.run`'s default; the child's
#: deadline caps it lower when less is left.
GIT_SECONDS = 25
#: The byte ceiling in the page's words, for the row that says a status was
#: cut there; the number itself is the collectors' `BUDGET_BYTES`.
CEILING = "64 KB"


def _collectors():
    # `collectors.py` as a module, loaded by path as `ports_screen` and
    # `reports_screen` load it: the file sits beside the package in the
    # checkout and nowhere in site-packages.
    path = Path(__file__).resolve().parents[1] / "collectors.py"
    spec = importlib.util.spec_from_file_location("sd_dashboard_fleet_collectors", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("fleet collectors are unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- the page side ------------------------------------------------------------


def collect(area, *, within=None):
    """The child's document for `area`, read under the page's budget.

    `within` only lowers the budget, as `Budget`'s does. A child that exits
    non-zero, overruns or writes past the ceiling is a `ValueError` naming the
    reason; the screens show it in place of the rows, because a fleet that
    could not be read and a fleet with nothing in it must not look the same.
    """
    if area not in AREAS:
        raise ValueError("unknown fleet area")
    module = _collectors()
    budget = module.Budget(FLEET_SECONDS, within=within)
    since = time.clock_gettime(time.CLOCK_MONOTONIC)
    seconds = max(budget.seconds - FLEET_MARGIN, 0.0)
    try:
        process = budget.run([sys.executable, "-I", str(FLEET), area, f"{seconds:g}", f"{since:.6f}"], label=area)
    except module.OverBudget as error:
        raise ValueError(f"fleet collection was stopped at its budget: {error}") from None
    if process.returncode:
        raise ValueError(process.stderr.strip() or f"fleet collector exited {process.returncode} without a reason")
    try:
        document = json.loads(process.stdout)
    except ValueError:
        raise ValueError("fleet collector returned no document") from None
    if not isinstance(document, dict):
        raise ValueError("fleet collector returned incomplete output")  # noqa: TRY004 - external document validation
    return document


# -- the child ----------------------------------------------------------------


def checkouts(root):
    """Every checkout under the root, one level of grouping deep.

    Enumerated from the filesystem, never from a configured list, for the
    reason the pack gave: a checkout nobody registered is the interesting
    one. A checkout directly under the root belongs to the group `.`.
    """
    found = []
    if not root.is_dir():
        return found
    for group in sorted(root.glob("*")):
        if not group.is_dir() or group.name.startswith("."):
            continue
        if (group / ".git").exists():
            found.append((".", group))
            continue
        for repo in sorted(group.glob("*")):
            if repo.is_dir() and (repo / ".git").exists():
                found.append((group.name, repo))
    return found


def _stopped(collectors, command, error):
    """What a row says of a command the deadline cut, in the page's words."""
    terms = collectors.seconds_terms(collectors.DEADLINE_SECONDS)
    if getattr(error, "started", True):
        return f"{command} ran past the budget of {terms} and was stopped"
    return f"{command} was not started: the budget of {terms} was spent"


def read_output(collectors, argv, *, timeout=GIT_SECONDS, keep=None):
    """`argv`'s stdout, stripped, whether the ceiling cut it, and its exit: `(text, cut, code)`.

    `collectors.run`'s shape -- a session of its own so the kill takes the
    whole tree, the child's deadline capping the timeout, `PastDeadline` for
    a command the deadline cut or refused, `''` for one that failed on its
    own -- with one difference: at most `collectors.BUDGET_BYTES` of stdout
    is ever held. A command that writes past that is killed there and what
    arrived is returned with `cut` true, so a caller can say what it holds is
    a floor. `Budget.run` bounds the same way and refuses instead; a refusal
    is right for the page's read of the child, and wrong for one git answer
    inside it, where the branch and the last commit were read fine. `code`
    is the exit status, `None` for a command that could not be started or
    was killed here, so a caller can tell an empty answer from a failure.

    `keep` filters the stream a line at a time and the ceiling then applies
    to the lines it keeps, not to everything the command printed. A caller
    that wants a few rows out of a long table needs this: with the ceiling on
    the raw output, unrelated lines spend the budget and the rows it came for
    are cut away unseen, which reads on the page as "none of them are there"
    rather than "the table was cut" (Codex review of this branch, reproduced:
    70 KB of other processes hid a running `sd-review`). Only whole lines are
    offered to `keep` and only whole lines are held, so a record the ceiling
    or the kill cut in half is dropped rather than read as a short one --
    `sdiff a b` cut after `sd` was a running `sd` command in the same review.
    """
    limit = collectors.BUDGET_BYTES
    name = Path(str(argv[0])).name
    left = collectors.time_left()
    capped = left is not None and left < timeout
    if capped:
        if left <= 0:
            raise collectors.PastDeadline(f"{name} was not started: the deadline had passed", started=False)
        timeout = left
    stop = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError:
        return "", False, None
    out, cut, pending = bytearray(), False, bytearray()

    def take(line):
        """Hold one whole line, or say that it crossed the ceiling."""
        if not keep(line.decode("utf-8", "replace")):
            return True
        if len(out) + len(line) + 1 > limit:
            return False
        out.extend(line)
        out.append(0x0A)
        return True

    try:
        fd = proc.stdout.fileno()
        while True:
            left = stop - time.monotonic()
            if left <= 0:
                if capped:
                    raise collectors.PastDeadline(f"{name} ran past the deadline", started=True)
                return "", False, None
            if not select.select([fd], [], [], left)[0]:
                continue
            # One byte past the ceiling is enough to know it was crossed, and
            # asking for more would allocate it first (`Budget.run`). Under a
            # filter the ceiling is on what is kept, so the bound is on the
            # record being assembled rather than on `out`: `pending` is the
            # one buffer that a single oversized record can grow without end.
            room = limit - len(pending if keep else out) + 1
            chunk = os.read(fd, min(65536, room))
            if not chunk:
                # The pipe closed, so what is left is a whole last record even
                # without its newline, and the filter sees it like any other.
                if keep and pending and not take(pending):
                    cut = True
                break
            if keep is None:
                out += chunk
                if len(out) > limit:
                    del out[limit:]
                    cut = True
                    break
                continue
            pending += chunk
            while True:
                end = pending.find(b"\n")
                if end < 0:
                    break
                line = pending[:end]
                del pending[:end + 1]
                if not take(line):
                    cut = True
                    break
            if not cut and len(pending) > limit:
                # A record with no newline yet is held whole, so one that
                # outgrows the ceiling would put the command in charge of how
                # much this holds -- and the docstring above says at most
                # `BUDGET_BYTES` of stdout is ever held. Such a record could
                # not be kept in any case: `take` needs
                # `len(line) + 1 <= limit` and would refuse it here. It is cut
                # without being offered to `keep`, because only whole lines
                # are offered and this one is not whole yet; `cut` then says
                # what is held is a floor, which it is -- the record dropped
                # unread may have been one the caller came for (Copilot review
                # of this branch).
                cut = True
            if cut:
                break
        if not cut:
            # The pipe closed; the exit follows inside what is left. A command
            # that has said all it will say and then stays is a hang, said as
            # one (PR #427 verification round), not an answer.
            while proc.poll() is None:
                if stop - time.monotonic() <= 0:
                    if capped:
                        raise collectors.PastDeadline(f"{name} ran past the deadline after closing its output", started=True)
                    return "", False, None
                time.sleep(0.01)
    finally:
        # Cut, timed out, or exited: the group is killed either way, since a
        # command that was read to its end has nothing left to hold, and one
        # that was not must not keep the pipe.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        proc.stdout.close()
    return out.decode("utf-8", "replace").strip(), cut, (None if cut else proc.returncode)


class Unreadable(Exception):
    """A checkout whose git answered with a failure on a command the row needs."""


UNKNOWN = {"branch": "?", "dirty": None, "ahead": None, "behind": None,
           "last": "", "last_iso": "", "subject": "", "author": "", "web": "", "truncated": [],
           "default": "", "behind_default": None, "fetched_iso": ""}

#: What `symbolic-ref` answers for a remote whose default branch is known.
ORIGIN_HEAD = "refs/remotes/origin/"


def fetched(repo):
    """When the checkout last fetched, as `.git/FETCH_HEAD`'s mtime in UTC ISO; `''` if none is recorded.

    A stat and no git: the fleet never fetches (sd:1676), so how far behind a
    checkout is can only be as fresh as its last fetch, and the page says how
    old that is. A clone that never fetched since has no `FETCH_HEAD`, and a
    `.git` that is a file (a linked worktree) keeps it elsewhere; both are
    "not recorded", never "just now".
    """
    try:
        stamp = (repo / ".git" / "FETCH_HEAD").stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")


def facts(collectors, repo):
    """`collectors.git_facts` read through `read_output`: the same five commands, the same keys, plus `truncated`.

    `None` when the checkout's `.git` is gone, as `git_facts` answers; `Unreadable`
    when `rev-parse`, `status` or `log` exits non-zero, because a row with no
    branch and no dirt is not a clean checkout and must not read as one.
    `rev-list` and `remote get-url` may fail on their own: no upstream and no
    remote are facts, and the row says "unknown" and no link for them.
    """
    p = str(repo)
    if not (repo / ".git").exists():
        return None
    cut = []

    def git(*args, needed=False):
        text, was_cut, code = read_output(collectors, ["git", "-C", p, *args])
        if was_cut:
            cut.append(args[0])
        if needed and not was_cut and code != 0:
            # `code is None` is "no exit status": the command could not be
            # started, or was killed here. `if code:` read that as success, so
            # a machine with no `git` on PATH filled the row with `?` and
            # `None` and left `error` empty -- an unread checkout shown as a
            # read one. A cut answer also has no exit status and is not a
            # failure: it is the ceiling working, and the row says the count
            # is a floor.
            raise Unreadable(f"git {args[0]} failed (exit {code})" if code is not None
                             else f"git {args[0]} did not run")
        return text

    branch = git("rev-parse", "--abbrev-ref", "HEAD", needed=True) or "?"
    dirty = sum(1 for line in git("status", "--porcelain", needed=True).split("\n") if line.strip())
    when, subject, author = (git("log", "-1", "--format=%cI%x1f%s%x1f%an", needed=True).split("\x1f") + ["", "", ""])[:3]
    ahead = behind = None
    counts = git("rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if counts and "\t" in counts:
        b, a = counts.split("\t")[:2]
        behind, ahead = int(b), int(a)
    # Behind the remote's default branch, from local refs only (sd:1676): the
    # upstream count above follows whatever branch is checked out, and a
    # primary checkout parked on a feature branch still lags `main`. Counted
    # only on the default branch; on any other the page says why it cannot.
    default, behind_default = "", None
    head = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if head.startswith(ORIGIN_HEAD):
        default = head[len(ORIGIN_HEAD):]
        if branch == default:
            count = git("rev-list", "--count", f"HEAD..{head}")
            if count.isdigit():
                behind_default = int(count)
    remote = git("remote", "get-url", "origin")
    web = ""
    m = re.match(r"(?:git@github\.com:|https://github\.com/)(.+?)(?:\.git)?$", remote)
    if m:
        web = "https://github.com/" + m.group(1)
    return {
        "branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind,
        "last": when[:10], "last_iso": when, "subject": subject, "author": author,
        "web": web, "truncated": sorted(set(cut)),
        "default": default, "behind_default": behind_default, "fetched_iso": fetched(repo),
    }


def _repo_row(collectors, group, repo):
    row = {"name": repo.name, "group": group, "path": str(repo), "error": ""}
    # Three ways to know nothing, one row shape for all of them: `dirty` is
    # None and `error` says which, so the page counts the checkout as unread
    # and never as clean (PR #427 verification round).
    try:
        read = facts(collectors, repo)
    except collectors.PastDeadline as error:
        row["error"] = _stopped(collectors, "git", error)
        read = None
    except Unreadable as error:
        row["error"] = str(error)
        read = None
    else:
        if read is None:
            row["error"] = "the checkout's .git is gone: removed between the walk and the read"
    return {**row, **(read or UNKNOWN)}


def collect_repos(collectors, root):
    """The fleet, newest commit first: the pack's `/api/state` document."""
    found = checkouts(root)
    repos = []
    if found:
        with concurrent.futures.ThreadPoolExecutor(max_workers=GIT_WORKERS) as pool:
            repos = list(pool.map(lambda item: _repo_row(collectors, *item), found))
    repos.sort(key=lambda row: (row["last_iso"] or "", row["name"]), reverse=True)
    return {
        "root": str(root),
        # An unreadable root and a root holding no checkouts both collect
        # nothing, and only one of them is a mistake; the page tells them apart.
        "rootExists": root.is_dir(),
        "repos": repos,
        "counts": {
            "repos": len(repos),
            "dirty": sum(1 for row in repos if row["dirty"]),
            "ahead": sum(1 for row in repos if row["ahead"]),
            "unread": sum(1 for row in repos if row["error"]),
        },
    }


#: A registration's states, in the order the page lists them: the ones that
#: need pruning, the ones nobody can say, the ones in use.
STATES = ("abandoned", "unknown", "live")


def branch_of(head):
    """`ref: refs/heads/x` is a branch; a bare sha is a detached HEAD."""
    head = head.strip()
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    return "detached" if head else "?"


def read_worktrees(group, repo):
    """Every worktree this checkout has registered, live or not.

    Read from git's own files, not from `git worktree list`: `gitdir` names
    the worktree and `HEAD` names its branch, three reads per checkout where
    a second git fan-out would double the cost of the page. A registration
    whose directory is gone is the point of the view -- a parallel run that
    ended badly, or whose scratchpad was cleared, leaves it behind holding a
    branch, and nothing else surfaces it.
    """
    registry = repo / ".git" / "worktrees"
    if not registry.is_dir():
        return []
    where = repo.name if group == "." else f"{group}/{repo.name}"
    out = []
    for entry in sorted(registry.iterdir()):
        if not entry.is_dir():
            continue
        try:
            gitdir = (entry / "gitdir").read_text(encoding="utf-8").strip()
            head = (entry / "HEAD").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # A registration this incomplete is still a registration, and
            # dropping it would hide the one that needs pruning most. It is
            # not known to be abandoned, though: its files could not be read,
            # and "unknown" is the honest state (PR #427 verification round).
            # `UnicodeDecodeError` is not an `OSError`, so a `gitdir` or
            # `HEAD` that is not UTF-8 used to abort the whole Sessions
            # collection rather than mark this one registration unknown.
            gitdir, head, state = "", "", "unknown"
        else:
            state = "live" if gitdir and Path(gitdir).exists() else "abandoned"
        # `gitdir` points at the worktree's `.git` file; the worktree is its
        # parent. Reported even when unreadable, as the empty string, because
        # "registered and unnameable" is a state and not an absence.
        path = str(Path(gitdir).parent) if gitdir else ""
        out.append({
            "repo": where,
            "name": entry.name,
            "path": path,
            "branch": branch_of(head),
            "live": state == "live",
            "state": state,
        })
    return out


def _sd_row(line):
    """`(pid, elapsed, command)` for one `ps` line naming an `sd-*` command, else `None`."""
    parts = line.split(None, 2)
    if len(parts) != 3 or not parts[2].split():
        return None
    # The basename, so a full path and a bare invocation both match, and so a
    # command that merely *mentions* one -- an editor holding the file open --
    # does not.
    head = Path(parts[2].split()[0]).name
    return tuple(parts) if head == "sd" or head.startswith("sd-") else None


def running(collectors):
    """`(rows, error, cut)`: every `sd-*` command right now, why not if not, and whether the table was cut.

    `ps` printing nothing is a failure and not a quiet machine: with the
    header suppressed the table still holds `ps` itself, so an empty answer
    is a `ps` that did not run, and a page that showed it as zero commands
    would be the calm tab the pack's loader existed to prevent. Filtered, no
    line is the ordinary answer on a machine running none, so the failure is
    a table with no lines at all rather than no rows out of it.

    Read through `read_output`, not `collectors.run`: `run`'s `communicate()`
    holds the whole `ps -Ao` table in the child before anything filters it,
    and only the JSON this returns was ever bounded. A machine with many
    processes or long command lines therefore paid for the full table twice.
    The filter runs inside that read, so `collectors.BUDGET_BYTES` bounds the
    `sd-*` rows rather than the table they are in: 64 KB of unrelated
    processes would otherwise cut the rows away and leave the page saying no
    sd command is running (Codex review of this branch).
    """
    seen = 0

    def keep(line):
        nonlocal seen
        seen += 1
        return _sd_row(line) is not None

    try:
        text, cut, code = read_output(collectors, PS, timeout=PS_SECONDS, keep=keep)
    except collectors.PastDeadline as error:
        return [], _stopped(collectors, "ps", error), False
    # `code` is the completion, and only a cut table is allowed to lack one:
    # there the kill is this reader's own. Lines already read say nothing
    # about a table that then stopped, and counting them as the whole of it
    # is how a `ps` that printed two rows and hung became a quiet machine
    # (Codex review of this branch). An uncut table with no line at all is
    # the older failure: with the header suppressed `ps` still lists itself.
    if not cut and (code != 0 or not seen):
        return [], f"ps gave no answer within {PS_SECONDS} seconds", False
    out = []
    for line in text.splitlines():
        row = _sd_row(line)
        if row:
            out.append({"pid": row[0], "elapsed": row[1], "command": row[2]})
    # A cut table is not a failed read: what arrived is real and the rows
    # below it are a floor. The page says so rather than counting them as all.
    return out, "", cut


def collect_sessions(collectors, root):
    """Every registered worktree, abandoned first, and every sd-* now running."""
    trees = []
    for group, repo in checkouts(root):
        trees.extend(read_worktrees(group, repo))
    trees.sort(key=lambda row: (STATES.index(row["state"]), row["repo"], row["name"]))
    procs, error, cut = running(collectors)
    return {
        "root": str(root),
        # As `collect_repos` says: a root that is not there and a root with
        # no worktree registered both collect nothing, and only one of them
        # is a calm page (PR #427 review).
        "rootExists": root.is_dir(),
        "worktrees": trees,
        "processes": procs,
        "processes_error": error,
        "processes_truncated": cut,
        "abandoned": sum(1 for row in trees if row["state"] == "abandoned"),
        "counts": {"worktrees": len(trees), "processes": len(procs)},
    }


BUILD = {"repos": collect_repos, "sessions": collect_sessions}


def main(argv):
    started = time.monotonic()
    number = r"\d+(\.\d+)?"
    if (len(argv) not in (2, 3) or argv[0] not in BUILD
            or not all(re.fullmatch(number, value) for value in argv[1:])):
        print(f"usage: fleet.py <{'|'.join(AREAS)}> <seconds> [<since>]", file=sys.stderr)
        return 2
    area, seconds = argv[0], float(argv[1])
    if len(argv) == 3:
        # The page's start on the shared clock: what this interpreter spent
        # starting is taken from the deadline, never added to it. A reading
        # from the future counts as now, and one older than the budget as a
        # deadline already passed.
        spent = time.clock_gettime(time.CLOCK_MONOTONIC) - float(argv[2])
        started -= min(max(spent, 0.0), seconds)
    try:
        collectors = _collectors()
        with collectors.set_deadline(seconds, started=started):
            document = BUILD[area](collectors, collectors.REPO_ROOT)
    except Exception as error:  # noqa: BLE001 - the page shows this reason
        # Not an empty document: a collector that failed and a fleet with
        # nothing in it must not look the same, and the page is what makes
        # the difference visible.
        print(f"{area}: {error!r}"[:4096], file=sys.stderr)
        return 1
    json.dump(document, sys.stdout, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
