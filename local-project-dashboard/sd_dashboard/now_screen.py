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
stamp is a change to the pack's `sd_db` and not this one. `needs_you` only, as the
pack read `needsYou` only: a pull request that is merely yours is not one
that is waiting on you.

**No dismiss, but a snooze.** The pack's rows carried an ack key and a
control that wrote to an ack store; the decision that made the pack's
dashboard read-only deleted that store (sd:719, note 1347). The ids keep
the pack's shapes so the comparison holds row for row, and a snooze
(sd:1896, `snooze`) keys on them: a snoozed row is in `snoozed` until its
time and back in `rows` after it. Nothing here writes.

**Failed scheduled jobs are the fourth source.** Four launchd cron jobs
failed on 2026-09-27 and Today said nothing, because the pack's Now never
read jobs. The read is `sd_db.operations.inventory`, the one the
Operations Jobs area renders, so the two agree on what `failed` means: a
non-zero last exit, or a signal the kernel sent for cause. Its backend is
the server's `operations_backend`, and the job's log time is the mtime of
`<cron_root>/logs/<job>.log`, the file `cron-jobs.sh` appends every run to.
A failed job ranks `FAILED`, above every other source's row and below a
dark collector, and carries the retry the Operations Jobs area sends:
`launchctl kickstart <service>`. `failed` is launchd's record of the last
run, so only a run launchd starts can clear it; a hand-run through
`cron-jobs.sh run` writes a newer log and leaves the row in place.

**A job that is not failed can still want you** (sd:2014). Operations lists
`interrupted`, `unknown` and `unloaded` jobs under attention beside `failed`,
and Now showed only `failed`. Each is a row at `ATTENTION`, the look band:
an interrupted run may not have finished and carries the same retry; an
unloaded job never runs until it is loaded again; an unknown one is a job
launchd could not describe. None is a failure on its own record, so none
outranks a failed job.

**A failed job says why it looks failed** (sd:1166, sd:2095). Its detail
ends with `job_failures.classify`'s label, from launchd's record and the last
run's log, and the row carries it as `triage`. The label is advisory: the
rank, the order and the retry are what they were. Jev is asked the same
question in shadow and its answer is recorded, never shown (`job_failures`).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sd_db import operations, progress, reads
from sd_db.errors import SdDbError

from . import active_sessions, job_failures, snooze, today_mail, today_reports
from . import fleet as fleet_module
from .operations_screen import _signal_name

__all__ = ["band", "document"]

# Ranks, not severities. 0 is loudest, and the band is derived from the
# number alone -- `kind` is a category and never a severity (the pack's
# R11-D20). The numbers are the pack's; `tests/test_now_screen.py` holds
# them to a copy taken by hand from `dashboard/now.py` at 85c4fa1b.
DARK = 0
# A scheduled job whose last run failed: not in the pack, which read no jobs.
# It sits in the loudest band with the dark rows, and after them.
FAILED = 1
# A job Operations lists under attention that did not fail: interrupted,
# unloaded or unknown (sd:2014). The look band, with the stale pull requests.
ATTENTION = 2
#: The job states Now raises a row for; Operations' "Jobs needing attention" holds the same four.
JOB_STATES = ("failed", "interrupted", "unknown", "unloaded")
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
# A pinned checkout behind its origin (sd:3539): on the hub, a merged fix that
# is not live until a refresh. Not in the pack; the look band, with AHEAD.
PINNED = 3
#: What moves a pinned checkout, named in its row.
REFRESH = "local-repo-sync/repo-sync.sh refresh"
#: Needs you's quiet rows (sd:5921): new briefs and items in progress with no session, the queued band.
QUIET = 5
#: A past-due member this many days late, and not recurring, breaks out of its group as its own row (sd:5921).
LATE_DAYS = 30
#: Members a group row shows when opened; the rest are "N more" (sd:5921).
GROUP_SHOWN = 5
#: Each source's lamp on Today (sd:5921): the abandoned worktrees are checkouts, so they light Repos.
LAMP = {"repos": "repos", "sessions": "repos", "prs": "pulls", "jobs": "jobs", "lane": "lane",
        "reports": "reports", "mail": "mail"}

#: The three bands, loudest first; the script paints each as a class name.
BANDS = ("broken", "look", "queued")

#: The four sources, in the order the document reads them. Each names an
#: Operations area a reader can open when the script is off.
SOURCES = ("repos", "sessions", "prs", "jobs")


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
        # pin_state's "pinned at <sha>, N behind origin/<default>" (sd:3539),
        # beside whatever else the checkout says; an unread count raises none.
        behind = repo.get("pinned_behind") or 0
        if behind:
            target = f"origin/{repo.get('default') or '?'}"
            out.append({
                "rank": PINNED,
                "kind": "pinned",
                "id": f"pinned:{name}:{behind}",
                "what": f"{name} pinned {behind} behind {target}",
                "detail": f"merged commits on {target} are not live here until a refresh · refresh: {REFRESH}",
                "source": "repos",
            })
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
            # A snooze's problem (sd:1896): the days since first seen drift each day; the rank is in the id.
            "problem": [f"{row.get('repo')}#{row.get('number')}", row.get("title") or ""],
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


def _log_time(log: Path | None) -> str:
    if log is None:
        return "log unknown"
    try:
        return "log " + datetime.fromtimestamp(log.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except FileNotFoundError:
        return "no log file"
    except OSError:
        return "log unreadable"


def _log_at(log: Path | None) -> str | None:
    """The log's last write as UTC ISO, the time the day track marks a failed run at; None when it cannot be read."""
    try:
        return _utc(log.stat().st_mtime) if log is not None else None
    except OSError:
        return None


def _utc(when) -> str | None:
    """A time as `YYYY-MM-DDTHH:MM:SSZ`: an epoch, or ISO text, a naive one read as UTC. None when it is not a time."""
    try:
        at = (datetime.fromtimestamp(when, timezone.utc) if isinstance(when, (int, float))
              else datetime.fromisoformat(str(when).replace(" ", "T").replace("Z", "+00:00")))
    except (TypeError, ValueError, OSError):
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def job_rows(jobs: list[dict], cron_root: Path | None) -> list[dict]:
    """One row for each job `operations.inventory` reads as needing attention.

    The id keys on the outcome, the signal when launchd names one and the
    exit code otherwise, as the other ids key on the fact that changed. The
    log time is when the last run wrote its log; a job with no log file, an
    unreadable one, or a backend with no `cron_root` says so rather than
    guess, and still gets its row. The retry line is in the detail, where
    the page shows it, and in `retry`, for a script that acts on it. Only a
    failed or interrupted job has one: Operations refuses retry for the rest.
    `state` is the job's own, for a page that words the rows by it. A
    failed job also carries `triage`, `job_failures.classify`'s label, at the
    end of its detail; it changes neither the rank nor the retry. Its `run`
    is the job's, launchd's last run, so Jev's shadow asks once per run and
    not once per outcome (sd:2904). A row with a retry also carries what
    Today's Retry posts and reads (sd:3386): the job's `revision`, which
    `operations.retry_job` checks, so a job that changed since the read is
    refused; its retry `capability`; and its last `exit`, as Retry asks
    first after 127 (sd:3392).
    """
    out = []
    for job in jobs:
        state = job.get("state")
        if state not in JOB_STATES:
            continue
        name, code, killed = job["name"], job.get("last_exit"), job.get("last_signal")
        service = job.get("service")
        row = {"rank": FAILED if state == "failed" else ATTENTION, "kind": "job", "state": state, "source": "jobs",
               "service": service}
        log = None if cron_root is None else cron_root / "logs" / f"{name}.log"
        if state in ("failed", "interrupted"):
            logged = _log_time(log)
            retry = f"launchctl kickstart {service}" if service else "retry from Operations Jobs"
            row.update(detail=f"{logged} · retry: {retry}", retry=retry, job=name, exit=code, at=_log_at(log),
                       revision=job.get("revision"), capability=(job.get("capabilities") or {}).get("retry"))
        if state == "failed":
            # launchd can report `last exit code = 0` beside a terminating signal;
            # the signal is the cause, so it names the row.
            outcome = _signal_name(killed) if killed is not None else f"exit {code}" if code is not None else "no exit code"
            triage = job_failures.classify(job, log)
            row.update(id=f"job:{name}:{f'signal{killed}' if killed is not None else code}", what=f"{name} failed with {outcome}",
                       job=name, run=job.get("run"), triage=triage, detail=f"{row['detail']} · triage: {triage['class']}, {triage['why']}")
            # A snooze's problem (sd:1896) is the outcome: each run's log time moves the detail.
            row["problem"] = [row["what"]]
        elif state == "interrupted":
            stop = _signal_name(killed) if killed is not None else "a stop with no signal recorded"
            row.update(id=f"job:{name}:interrupted{killed if killed is not None else ''}",
                       what=f"{name} was interrupted by {stop}")
            row["problem"] = [row["what"]]
        elif state == "unloaded":
            row.update(id=f"job:{name}:unloaded", what=f"{name} is installed but not loaded",
                       detail=f"launchd will not run it until it is loaded · load: local-cron-jobs/cron-jobs.sh install {name}")
        else:
            # The inventory carries no observation reason; Operations Jobs shows it, and launchctl prints the record.
            check = f"launchctl print {service}" if service else "launchctl print <service>"
            row.update(id=f"job:{name}:unknown", what=f"{name}: launchd state unknown",
                       detail=f"launchctl gave no state Operations recognises · check: {check}")
        out.append(row)
    return out


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
                                         or (row.get("pinned_behind") is not None
                                             and type(row.get("pinned_behind")) is not int)
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


def _jobs(connection, backend) -> list[dict]:
    # `cron_root` only places the log; a backend without one still lists jobs.
    root = getattr(backend, "cron_root", None)
    return job_rows(operations.inventory(connection, backend=backend)["jobs"],
                    Path(root) if isinstance(root, (str, Path)) else None)


def work_rows(connection: sqlite3.Connection, now: str) -> list[dict]:
    """The items `sd today` lists, in its order, as rows a snooze can hold (sd:3271).

    The id is `item:<id>`, so the key is `today:item:<id>`. The row's problem is its status and due date: a changed
    item is not the one the operator snoozed, and a new title is not a new problem. `item_kind` and `revision` are what
    Today's Fix reads and posts (sd:3456): the item's kind names the session, and the revision binds the click to this read.
    """
    rows = reads.today_items(connection, now=now)
    # One batched revision read for the list, not an `item_state` per row (sd:3531).
    revisions = reads.revisions(connection, [row["id"] for row in rows])
    def column(row, name):
        return row[name] if name in row.keys() else None

    # `repo`, `priority` and `recurring` place the row in Needs you's groups (sd:5921); an older schema reads none.
    return [{"id": f"item:{row['id']}", "kind": "item", "item": row["id"], "what": f"#{row['id']} {row['title']}",
             "detail": f"{row['status']} · due {row['due']}" if row["due"] else row["status"],
             "state": row["status"], "due": row["due"], "problem": [row["status"], row["due"]],
             "item_kind": row["kind"], "revision": revisions[row["id"]],
             "repo": Path(column(row, "repo")).name if column(row, "repo") else "", "priority": column(row, "priority"),
             "recurring": bool(column(row, "recurrence")), "since": column(row, "status_since")}
            for row in rows]


def lane_rows(lanes: list[dict]) -> list[dict]:
    """Each lane's blocked entries as Needs you rows (sd:5921): broken, and keyed on the head the entry ended at.

    The row carries what Queue's Retry and Approve post (`/api/queue/move`): the repo path, the item, the lane's
    revision and the full head, so Today's Retry acts on the entry the read showed and nothing newer.
    """
    out = []
    for lane in lanes:
        for row in lane.get("rows") or []:
            if row.get("state") != "blocked":
                continue
            where = row.get("step") or row.get("status") or "the lane"
            out.append({"rank": FAILED, "kind": "lane", "id": f"lane:{lane['repo']}:{row['item']}:{row.get('head') or ''}",
                        "what": f"{lane['repo']} sd:{row['item']} blocked at {where}",
                        "detail": f"{row.get('who') or 'lane'}: {row.get('reason') or row.get('status')}",
                        "source": "lane", "at": row.get("finished"), "item": row["item"], "title": row.get("title") or "",
                        "repo": lane["repo"], "repo_path": lane["path"], "lane_revision": lane["revision"],
                        "expected_head": row.get("expected_head") or "", "runner_merge": lane.get("runner_merge")})
    return out


def report_rows(problems: list[dict]) -> list[dict]:
    """The open problem reports as Needs you rows (sd:5921), in the look band; Acknowledge is each one's act."""
    return [{**problem, "rank": ATTENTION, "kind": "report", "what": f"Report: {problem['title']}",
             "detail": " · ".join(part for part in (problem.get("why"), f"on {problem['host']}" if problem.get("host") else "",
                                                     f"repeats {problem['repeats']}×" if problem.get("repeats") else "") if part)
                       or "asks for a person",
             "source": "reports", "problem": [problem["title"], problem.get("why")]}
            for problem in problems]


def mail_rows(mail: list[dict]) -> list[dict]:
    """The mail threads waiting on the operator as Needs you rows (sd:5921), in the look band."""
    return [{**thread, "rank": ATTENTION, "kind": "mail", "id": f"mail:{thread['thread']}",
             "what": f"Mail: {thread['subject'] or '(no subject)'}",
             "detail": f"from {thread['who'] or 'someone'} to the {thread['repo']} alias", "source": "mail",
             "problem": [thread["thread"], thread["at"]]}
            for thread in mail]


def _group(group_id: str, what: str, detail: str, rank: int, members: list[dict], *, source: str = "",
           kind: str = "group") -> dict:
    return {"rank": rank, "kind": kind, "id": group_id, "what": what, "detail": detail, "source": source,
            "group": True, "members": members, "more": max(0, len(members) - GROUP_SHOWN),
            "at": min((member.get("at") or "" for member in members), default="") or None}


def _late_days(row: dict, today: str) -> int | None:
    days = stale_days(row.get("due") or "", today)
    return days if days is not None and days > 0 else None


def work_needs(work: list[dict], live: set[int], today: str) -> list[dict]:
    """The work items as Needs you rows (sd:5921): one group row per kind, so a list of 142 reads as a few.

    Ready to send, past due, and in progress with no live session each collapse into one group row once there are two;
    an item in progress with a session is In motion only. A past-due member with priority 1, or one more than
    `LATE_DAYS` late that does not recur, breaks out as its own row, so a group never hides it. What is left, due
    today and not started, is one queued group.
    """
    ready, late, idle, due, alone = [], [], [], [], []
    for row in work:
        row = {**row, "source": "tasks", "at": row.get("since")}
        days = _late_days(row, today)
        if row["state"] == "ready_to_send":
            ready.append({**row, "rank": ATTENTION})
        elif days is not None:
            row = {**row, "rank": ATTENTION}
            (alone if row.get("priority") == 1 or (days > LATE_DAYS and not row.get("recurring")) else late).append(row)
        elif row["state"] == "in_progress":
            if row["item"] not in live:
                idle.append({**row, "rank": QUIET})
        else:
            due.append({**row, "rank": QUIET})

    def by_repo(rows):
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.get("repo") or "no repo"] = counts.get(row.get("repo") or "no repo", 0) + 1
        return " · ".join(f"{repo} {count}" for repo, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def oldest(rows):
        return min((row["due"] for row in rows if row.get("due")), default="")

    out = list(alone)
    for group_id, rows, what, detail, rank in (
            ("group:ready", ready, f"{len(ready)} ready to send", f"finished, not sent · oldest #{ready[0]['item']}"
             if ready else "", ATTENTION),
            ("group:late", late, f"{len(late)} items past due", f"{by_repo(late)} · oldest due {oldest(late)}", ATTENTION),
            ("group:idle", idle, f"{len(idle)} in progress, no session", ", ".join(f"#{row['item']}" for row in idle[:4]),
             QUIET),
            ("group:due", due, f"{len(due)} due today", by_repo(due), QUIET)):
        if len(rows) == 1:
            out.append(rows[0])
        elif rows:
            out.append(_group(group_id, what, detail, rank, rows, source="tasks"))
    return out


def brief_needs(briefs: list[dict]) -> list[dict]:
    """The unseen briefs as one queued group row (sd:5921); Mark all seen posts each member's Seen."""
    rows = [{**brief, "rank": QUIET, "kind": "brief", "what": brief.get("subj") or brief["id"],
             "detail": f"{brief.get('src') or 'brief'} · {brief.get('words')} words", "source": "briefs",
             "at": brief.get("at") or brief.get("day")} for brief in briefs]
    if len(rows) < 2:
        return rows
    series = sorted({row.get("src") or "brief" for row in rows})
    return [_group("group:briefs", f"{len(rows)} new briefs", ", ".join(series[:4]) + (" …" if len(series) > 4 else ""),
                   QUIET, rows, source="briefs")]


def needs(rows: list[dict], *, work: list[dict], live: set[int], today: str, lanes: list[dict],
          briefs: list[dict]) -> list[dict]:
    """Needs you (sd:5921): every fact that asks the operator to act, ranked in the bands, as one list.

    `rows` are the rows each source already raised and the snooze left: Now's, the reports' and the mail's. The work
    items, the briefs and the lanes' blocked entries are made rows here. Each row names its `lamp`, the Today lamp
    whose filter shows it. Ordered on band, then rank, then id, so the list does not reshuffle between reads.
    """
    found = [*rows, *work_needs(work, live, today), *brief_needs(briefs), *lane_rows(lanes)]
    out = []
    for row in found:
        row = {**row, "band": row.get("band") or band(row["rank"]), "lamp": LAMP.get(row.get("source"), "")}
        if row.get("group"):
            row["members"] = [{**member, "band": band(member["rank"]), "lamp": row["lamp"]} for member in row["members"]]
        out.append(row)
    return sorted(out, key=lambda row: (BANDS.index(row["band"]), row["rank"], row["id"]))


def _lanes(connection, now: str) -> dict:
    from . import queue_screen

    return queue_screen.document(connection, now=now)


def lane_summary(queue: dict) -> dict:
    """What Today's Lane and Gate lamps and In motion's tally read from Queue's document (sd:5921)."""
    states: dict[str, int] = {}
    landed_in = set()
    for lane in queue.get("lanes") or []:
        for row in lane.get("rows") or []:
            states[row["state"]] = states.get(row["state"], 0) + 1
            if row["state"] == "landed":
                landed_in.add(lane["repo"])
    return {"merging": states.get("merging", 0), "next": states.get("next", 0), "blocked": states.get("blocked", 0),
            "landed": states.get("landed", 0), "landed_lanes": len(landed_in), "gates": queue.get("gates") or {},
            "load": queue.get("load") or {}, "unread": len(queue.get("problems") or []),
            "unread_why": "; ".join(f"{Path(p['repo']).name}: {p['error']}" for p in (queue.get("problems") or [])[:3])}


TRACK_HOURS = 24


def track(connection: sqlite3.Connection, now: str, *, rows: list[dict], briefs: list[dict], active: dict,
          lane: dict) -> dict:
    """The day track (sd:5921): the last 24 hours, rolling, as marks in four lanes, and its key's four counts.

    Landed is each item a merge delivered (`sd_db.today.delivered`); Jobs each failed run at its log time; Briefs each new
    brief at its filing; Live each session's start. Every mark names its row. A read that failed is `error`, so the page
    says the track was not read, never an empty day.
    """
    from sd_db import today

    end = datetime.fromisoformat(now.replace("Z", "+00:00"))
    since, until = _utc((end - timedelta(hours=TRACK_HOURS)).timestamp()), _utc(end.timestamp())
    week = (end + timedelta(days=7)).strftime("%Y-%m-%d")

    def within(marks):
        return sorted((mark for mark in marks if mark["at"] and since <= mark["at"] <= until), key=lambda mark: mark["at"])

    try:
        delivered = today.delivered(connection, now=now, hours=TRACK_HOURS)
        due = sum(1 for row in today.planned(connection, now=now) if row["due"] <= week)
        blocked = len(today.blocked(connection))
    except (SdDbError, sqlite3.Error, KeyError, TypeError) as failure:
        return {"error": str(failure) or "the delivered, planned and blocked items could not be read"}
    marks = {
        "landed": within({"at": _utc(row["at"]), "id": f"item:{row['id']}", "item": row["id"],
                          "what": f"sd:{row['id']} {row.get('title') or ''}".strip()} for row in delivered),
        "jobs": within({"at": row.get("at"), "id": row["id"], "what": row["what"]}
                       for row in rows if row["kind"] == "job" and row.get("state") == "failed"),
        "briefs": within({"at": _utc(row["at"]) if row.get("at") else None, "id": row["id"], "what": row["subj"]}
                         for row in briefs),
        "live": within({"at": _utc(row["started"]) if row.get("started") else None, "id": f"session:{row['item']}:{n}",
                        "item": row["item"], "what": f"sd:{row['item']} {row['session']}"}
                       for n, row in enumerate(active.get("rows") or [])),
    }
    return {"since": since, "until": until, **marks,
            "key": {"landed": len(marks["landed"]), "landing": lane["merging"] + lane["next"], "due_week": due,
                    "blocked": blocked}}


def document(connection: sqlite3.Connection, *, now: str, fleet=None, jobs=None, jev=None, herdr=None,
             lanes=None) -> dict:
    """The merged, ranked, banded rows, and what each source said if it said nothing.

    `fleet` is `fleet.collect`'s shape, `area -> document`, and the seam
    a test fills; the default runs the child. `jobs` is an operations
    backend, `operations.LaunchdBackend` by default. Each source is guarded
    on its own so one collector's failure is one row and the others still
    answer. `sources` carries the empty string for a source that was read
    and the reason for one that was not, the same text its row shows.
    `jev` is the command `job_failures.shadow` asks about failed jobs, in
    the background and without changing a row; None, the default, asks nothing.
    A row the operator snoozed (sd:1896) is in `snoozed`, with its `until`,
    and not in `rows`; `snooze_error` says why none could be read.
    `work` is the work items `sd today` lists (sd:3271), snoozed on the same page: a snoozed item is in `snoozed`
    with its `until` and not in `work`; `work_error` says why the items could not be read, and is never an empty list.
    `active` is Working now (sd:3466), `active_sessions.document` over the sessions document this read already holds;
    `herdr` is its seam for the live agents.
    `observed` holds, for each fleet area the read came from the server's snapshot, when that area was read (`at`)
    and why its newest read failed (`stale`, else None); the page says both (sd:TBD).
    `mail` is Your mail (sd:4191), `today_mail.document`: the mail threads waiting on the operator under each repo's aliases,
    with `mail_more`, `mail_unread` and `mail_error` saying what is left out and why.
    `problems` and `briefs` are `today_reports.document`: the open reports that ask for a person, and the briefs of the
    last week not marked seen (a Today snooze), each with its `_more` and `_error`.
    `needs` is Needs you (sd:5921, `needs`): Now's rows, the reports, the mail, each lane's blocked entries and the
    work and briefs as group rows, ranked in one list; the badge counts its rows and nothing else. `lane` is what the
    Lane and Gate lamps read (`lane_summary`); `lanes` is the seam for Queue's document, `queue_screen.document` by
    default, and a lane read that failed is a dark `lane` row, as a dark collector is.
    """
    collect_area, kept = fleet or fleet_module.collect, {}

    def read(area):
        kept[area] = collect_area(area)
        return kept[area]

    rows: list[dict] = []
    sources: dict[str, str] = {}
    for source, collect in (("repos", lambda: _repos(read)),
                            ("sessions", lambda: _sessions(read)),
                            ("prs", lambda: _prs(connection, now)),
                            ("jobs", lambda: _jobs(connection, jobs or operations.LaunchdBackend()))):
        try:
            found = collect()
        except (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error) as failure:
            reason = str(failure) or f"{source} collector failed without a reason"
            sources[source] = reason
            rows.append(dark_row(source, reason))
            continue
        sources[source] = ""
        rows.extend(found)
    job_failures.shadow(rows, jev)
    snoozes, snooze_error = snooze.held(connection, now=now)
    shown, snoozed = snooze.split("today", [{**row, "band": band(row["rank"])} for row in merge(rows)], snoozes)
    try:
        items, work_error = work_rows(connection, now), ""
    except (SdDbError, sqlite3.Error) as failure:
        items, work_error = [], str(failure) or "the work items could not be read"
    work, work_snoozed = snooze.split("today", items, snoozes)
    active = active_sessions.document(connection, now=now, sessions=kept.get("sessions") or {},
                                      sessions_error=sources["sessions"], herdr=herdr)
    # When each fleet area was read, and why its newest read failed, from the server's snapshot (sd:TBD).
    observed = {area: {"at": found.get("observed"), "stale": found.get("stale")} for area, found in kept.items()
                if isinstance(found, dict) and found.get("observed")}
    reported = today_reports.document(connection, now=now, snoozes=snoozes)
    mail = today_mail.document(connection, now=now)
    try:
        queue, sources["lane"] = (lanes or _lanes)(connection, now), ""
    except (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error) as failure:
        queue, sources["lane"] = {}, str(failure) or "lane collector failed without a reason"
    # Reports and mail snooze as Now's rows do; a lane entry does not: Retry or Cancel ends it, on Today or on Queue.
    reports, reports_snoozed = snooze.split("today", report_rows(reported["problems"]), snoozes)
    threads, mail_snoozed = snooze.split("today", mail_rows(mail["mail"]), snoozes)
    live = {row["item"] for row in active.get("rows") or []}
    dark = [dark_row("lane", sources["lane"])] if sources["lane"] else []
    listed = needs([*shown, *dark, *reports, *threads], work=work, live=live, today=now[:10],
                   lanes=queue.get("lanes") or [], briefs=reported["briefs"])
    summary = lane_summary(queue)
    return {"now": now, "sources": sources, "observed": observed, "rows": shown, "active": active,
            "snoozed": snoozed + work_snoozed + reports_snoozed + mail_snoozed, "snooze_error": snooze_error,
            "work": work, "work_error": work_error, "needs": listed, "lane": summary,
            "track": track(connection, now, rows=rows, briefs=reported["briefs"], active=active, lane=summary),
            "attention": _attention(listed), **mail, **reported}


def _attention(rows: list[dict]) -> dict:
    """The rail badge (sd:3446): Needs you's broken rows, else its look rows (sd:5921). A group is one row, and the badge
    counts nothing the list does not show. The decisions that wait count on Decide's badge (sd:5781)."""
    warning = sum(row["band"] == "broken" for row in rows)
    caution = sum(row["band"] == "look" for row in rows)
    if warning:
        return {"state": "warning", "n": warning, "what": "broken rows"}
    if caution:
        return {"state": "caution", "n": caution, "what": "rows to look at"}
    return {"state": "ok", "n": 0, "what": "rows"}


