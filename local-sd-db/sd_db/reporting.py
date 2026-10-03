"""Observed cron reports and explicit interaction counts, never inferred effort."""

import hashlib
import json
import os
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from . import reads, workflow
from .database import transaction
from .writes import STATUSES, add_note, create_item, record_state, resolve_note, set_item_fields, stamp, transition

MAX_REPORT = 200_000

#: The `state` key a job's health is recorded under, one row per job by the
#: `heartbeat` retention rule. Prefixed, because `key` is a flat namespace
#: shared with every other heartbeat writer and a bare job name would collide
#: with whatever else chose the same word.
HEARTBEAT_KEY = "cron-report:"

#: How far past the clock a recorded `ended` may be and still count as the
#: newest tick. `cron-jobs.sh` stamps `ended` on this machine moments before it
#: ingests, so a real run is seconds ahead at most; five minutes covers a
#: slewing clock and a hand recovery typed to the minute. A row beyond it is a
#: bad stamp, and letting it win would drop every later heartbeat until the
#: clock caught up. A bad row inside it holds health for five minutes at most.
FUTURE_ALLOWANCE = timedelta(minutes=5)

#: The job-name shape `ingest` accepts, and the shape of a record marker.
JOB_NAME = r"[a-z0-9][a-z0-9_-]{0,99}"

#: The `session` `ingest` writes on the report and on the followup it opens
#: for a run that needs attention. Half of how `acknowledge` tells that
#: followup from one a person wrote; the other half is the note's text,
#: which `_ingest_followup` derives from the report's own provenance.
#: `session` alone is not proof: `workflow.add_item_note` writes whatever
#: `who` it is handed into the same column.
INGEST_SESSION = "cron"


def _ingest_followup(job, ended, attention_basis):
    """The one followup `ingest` writes, so `acknowledge` can recognise it."""
    return f"Review {job} findings from {ended}; {attention_basis}."


def _ingest_followup_for(fields):
    """The followup text `ingest` wrote for a report, read back from its `fields`.

    `None` when the fields cannot say: not JSON, no `report`, a provenance
    missing a key. A note can then match nothing, so a report whose fields
    are unreadable is refused with the note's id rather than resolved on a
    guess, and the default path -- which never reads `fields` at all --
    still finishes such a report the way the dashboard's notice promises.
    """
    try:
        report = json.loads(fields)["report"]
        return _ingest_followup(report["job"], report["ended"], report["attention_basis"])
    except (TypeError, ValueError, KeyError):
        return None

#: The most reports one bulk acknowledge moves. A guard against a mistaken
#: cutoff, not a measured limit: a 281-row apply held the write lock for about
#: 25 ms on a copy of the live store.
MAX_BATCH = 1000

#: The job a bulk acknowledge files its batch report under, and the record
#: marker that report carries so a later bulk run declines it.
BULK_JOB = "reports-acknowledge"

#: How many folded run ids an open report remembers under `fields.repeats`,
#: newest last. Enough to answer the replay of any run the operator could
#: plausibly recover by hand; bounded, because remembering every one is the
#: unbounded growth the fold exists to stop.
REMEMBERED_RUNS = 20


def _folded_into(connection, job, run_id):
    """The `cron-report` row of `job` that remembers `run_id` under `repeats.run_ids`, or `None`.

    Done or not: a folded run's identity lives only here, so this is how a
    replay of one is told from a new failure after the report it folded into
    was acknowledged (sd:955). The newest such row, because a run older than
    the `REMEMBERED_RUNS` remembered is counted again into a later report, and
    the later one is then the report that answers for it. Read behind
    `json_valid` the way `_fold_target` reads; `json_each` over NULL yields
    no row, so an unreadable `fields` simply does not match.

    Every report-worthy ingest asks this, so it reads one job's rows and not
    the whole table (the #401 review). `ingest` writes `external_id` as
    `job:run_id`, and `JOB_NAME` has no `:`, so the job's reports are exactly
    the `external_id` values between `job:` and `job;` (`;` sorts right after
    `:`). That range seeks `item_by_external`, whose `external_id IS NOT NULL`
    condition the comparison implies. The JSON is then read for this job's
    reports only: on the live store on 2026-09-23, 469 `cron-report` rows and 156 for the
    busiest job, one lookup went from 0.67 ms to 0.15 ms. It still grows with
    the job's own report history; a lookup that does not would need a
    run-id column or table, which is a migration.
    """
    return connection.execute(
        "SELECT id FROM item WHERE source='cron-report' AND external_id > ? AND external_id < ?"
        " AND kind='report'"
        " AND CASE WHEN json_valid(fields) THEN json_extract(fields, '$.report.job') END = ?"
        " AND EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(fields) THEN fields END, '$.repeats.run_ids')"
        "             WHERE value = ?)"
        " ORDER BY id DESC LIMIT 1", (job + ":", job + ";", job, run_id)).fetchone()


def _fold_target(connection, job, attention_basis):
    """The open attention report a repeat of `job` folds into, or `None`.

    The newest `cron-report` row for this job that is not `done`, has
    `fields.attention` exactly true, the same `attention_basis`, and no
    `record` marker. Every JSON function sits inside
    `CASE WHEN json_valid(fields)`, the way `clean_candidates` and
    `clean_reports` read: a `CASE` evaluates its branch only when the test
    is true, whatever order the query plan picks for `AND` terms, so a row
    whose `fields` are unreadable (`UNREADABLE_FIELDS`) is skipped rather
    than raising. The `NOT (UNREADABLE_FIELDS)` term names that intent; it
    is not what makes the read safe. Such a row is for a person to look at,
    and it can neither carry a count nor say what it was.
    """
    return connection.execute(
        "SELECT id, fields FROM item WHERE kind='report' AND source='cron-report' AND status != 'done'"
        f" AND NOT ({UNREADABLE_FIELDS})"
        " AND CASE WHEN json_valid(fields) THEN json_extract(fields, '$.report.job') END = ?"
        " AND CASE WHEN json_valid(fields) THEN json_type(fields, '$.attention') END = 'true'"
        " AND CASE WHEN json_valid(fields) THEN json_extract(fields, '$.report.attention_basis') END = ?"
        " AND CASE WHEN json_valid(fields) THEN json_type(fields, '$.record') END IS NULL"
        " ORDER BY id DESC LIMIT 1", (job, attention_basis)).fetchone()


def _check_identity(job, run_id):
    """The job name and run id every run is checked against before anything is written for it."""
    if not isinstance(job, str) or not re.fullmatch(JOB_NAME, job or "") or not isinstance(run_id, str) or not 1 <= len(run_id) <= 160:
        raise workflow.WorkflowError("report needs a known job name and stable run identity")


def _check_times(started, ended):
    """Both stamps in the one shape, and a run that does not end before it starts."""
    started, ended = stamp(started), stamp(ended)
    if ended < started:
        raise workflow.WorkflowError("report cannot end before it starts")
    return started, ended


def ingest(connection, *, job, run_id, started, ended, exit_code, text, source_path,
           attention=False, attention_basis="explicit report", truncated=False, removed=None,
           actor=None, record=None):
    """File one run's report, or answer a replay of it with the report it filed.

    `actor` and `record` are for a report that records an operator act rather
    than a cron run. `actor` says who and what acted, stored beside `removed`
    as `fields.report.actor`. `record` marks the row as such a record, stored
    as top-level `fields.record`, which a bulk acknowledge declines by the
    key's existence. A call that gives neither writes the `fields` it always
    did.

    A run that needs attention while an open attention report for the same
    job and the same `attention_basis` exists folds into that report instead
    of opening another (sd:920: sd:880's watchdog opened 51 rows in thirteen
    hours, one every fifteen minutes, each with its own followup). The open
    report's `fields.repeats` becomes `{"count", "first_ended",
    "last_run_id", "last_ended", "run_ids"}`: the count starts at 2 on the
    first fold, `first_ended` is the open report's own `ended`, and `run_ids`
    holds the last `REMEMBERED_RUNS` folded run ids, newest last. Nothing else
    on the row changes -- not the title, not the body, not the provenance
    under `fields.report`, and no second followup is written. The folded
    run's `text` is not stored: the body stays the first run's, because the
    count and the last run's identity are what the operator needs, and a
    body per run is the growth this bounds. `text` differing between runs is
    expected (a timestamp in stderr) and does not block the fold; a different
    `attention_basis` is a different failure and opens its own report. The
    returned state is the open report's, so a caller tells a fold from a new
    row by `external_id`.

    Never folded: a clean report (`attention` false), a `record` report, and
    a report whose `fields` are unreadable is never a target
    (`_fold_target`). An acknowledged report is `done` and never a target
    either, so the run after an acknowledge opens a fresh report, and the
    single-failure case -- one bad night, one report -- is exactly as before.

    Replay: a `run_id` in `repeats.run_ids` of any of the job's reports, or
    a report's own, is answered with that report's state and counts nothing
    -- whether the report is still open or already `done`. The run id is
    looked up before a fold target is chosen (`_folded_into`), so a hand
    recovery of a folded run after the acknowledge neither opens a fresh
    report nor writes a followup (sd:955). A `record` report is not asked:
    it was never folded, so one that reuses a folded run's `job` and
    `run_id` is filed as its own row, with its own `record` and `actor`
    fields, and replays only against that row. A folded run older than the
    `REMEMBERED_RUNS` remembered has left `run_ids`, so its replay is
    counted again: the count can overstate by hand recoveries of runs that
    old, and it never understates.
    """
    _check_identity(job, run_id)
    if type(exit_code) is not int or type(attention) is not bool or type(truncated) is not bool:
        raise workflow.WorkflowError("report exit code and attention must be explicit typed values")
    if not isinstance(text, str) or len(text.encode()) > MAX_REPORT or not isinstance(source_path, str):
        raise workflow.WorkflowError("report text exceeds the bounded report size")
    if not isinstance(attention_basis, str) or len(attention_basis) > 2000 or len(source_path) > 4000:
        raise workflow.WorkflowError("report provenance exceeds its size limit")
    if removed is not None and (not isinstance(removed, dict) or len(removed) > 20
                                or any(not isinstance(key, str) or type(count) is not int or count < 0
                                       for key, count in removed.items())):
        raise workflow.WorkflowError("report removal counts must be small nonnegative integers by name")
    if actor is not None and (not isinstance(actor, dict) or len(actor) > 10
                              or any(not isinstance(key, str)
                                     or not (value is None or type(value) is int
                                             or isinstance(value, str) and len(value) <= 200)
                                     for key, value in actor.items())):
        raise workflow.WorkflowError("report actor must be at most ten short text, integer or empty values by name")
    if record is not None and (not isinstance(record, str) or not re.fullmatch(JOB_NAME, record)):
        raise workflow.WorkflowError("report record marker must be a job name")
    started, ended = _check_times(started, ended)
    external = job + ":" + run_id
    provenance = {"job": job, "run_id": run_id, "started": started, "ended": ended,
                  "exit_code": exit_code, "source_path": source_path, "truncated": truncated,
                  "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "attention_basis": attention_basis}
    if removed is not None:
        # What a prune took, by name, so the counts are read back as numbers
        # and not parsed out of the report's prose.
        provenance["removed"] = removed
    if actor is not None:
        provenance["actor"] = actor
    fields = {"attention": attention, "report": provenance}
    if record is not None:
        fields["record"] = record
    with transaction(connection):
        previous = connection.execute("SELECT id,fields FROM item WHERE source='cron-report' AND external_id=?", (external,)).fetchone()
        if previous:
            # `repeats` is written by later runs, not by this one, so the
            # open report's own run replays against its fields without it.
            stored = json.loads(previous["fields"])
            stored.pop("repeats", None)
            if stored != fields:
                raise workflow.WorkflowError("this report identity already has different evidence")
            return workflow.item_state(connection, previous["id"])
        # A record replays only through the exact identity above: it was
        # never folded, so a folded run id it happens to reuse is not its
        # replay, and answering with that report would drop its own fields.
        folded = _folded_into(connection, job, run_id) if record is None else None
        if folded:
            return workflow.item_state(connection, folded["id"])
        target = _fold_target(connection, job, attention_basis) if attention and record is None else None
        if target:
            stored = json.loads(target["fields"])
            repeats = stored.get("repeats") or {"count": 1, "first_ended": stored["report"]["ended"], "run_ids": []}
            repeats.update(count=repeats["count"] + 1, last_run_id=run_id, last_ended=ended,
                           run_ids=(repeats["run_ids"] + [run_id])[-REMEMBERED_RUNS:])
            set_item_fields(connection, target["id"], fields={**stored, "repeats": repeats})
            return workflow.item_state(connection, target["id"])
        item = create_item(connection, kind="report", title=f"{job}: {'needs attention' if attention else 'run report'}",
                           status="planning", source="cron-report", external_id=external,
                           fields=fields, body={"text": text}, session=INGEST_SESSION)
        if attention:
            add_note(connection, item, "followup", _ingest_followup(job, ended, attention_basis), session=INGEST_SESSION)
        return workflow.item_state(connection, item)


def health(connection, job):
    """What this job's last recorded tick said, or `None` if it has never run.

    Read from the heartbeat rather than from the report items, because the
    reports are the exceptions now and a job with no report is the normal case
    rather than a job that has never run.
    """
    row = connection.execute(
        "SELECT body FROM state WHERE kind='heartbeat' AND key=? ORDER BY id DESC LIMIT 1",
        (HEARTBEAT_KEY + job,)).fetchone()
    return json.loads(row["body"])["healthy"] if row else None


def _beat(connection, *, job, run_id, healthy, ended, exit_code, now):
    """Record this tick's health, once per run, and only if it is the newest.

    Guarded on `run_id` rather than written unconditionally, for the same
    reason `ingest` is guarded on `external_id`: one run is one tick, and the
    ingest for a given run can be entered twice. Nothing retries it on its own
    -- `cron-jobs.sh` calls it once and only warns on stderr if it fails, and a
    re-run of the job mints a fresh run id rather than resending the old one --
    so the way one run id arrives twice is an operator recovering a report by
    hand. An appended second row would make that one run look like two to
    anything counting them, and `ingest`'s own replay is a no-op, so this one
    has to be too, or the two halves of `ingest_log` disagree about what a
    replay means. Returns whether a row was written, so a replay can say it
    recorded nothing.

    The guard is any row carrying this run, not only the latest: a hand
    recovery is exactly the case where newer ticks have landed since (sd:757).
    And it is also on `ended`, because `retention` keeps one heartbeat per key,
    so an older run's own row may already be gone. A run that ended before the
    newest recorded tick is never written, replayed or not: `health` reads the
    newest row by id, and appending an older tick after a newer one would make
    the older one the job's current health.

    Except against a row stamped past `now` plus `FUTURE_ALLOWANCE`: that row
    is a bad stamp rather than a newer tick, and it does not outrank anything.
    The alternative was refusing such an `ended` at ingest. That loses the
    run's report -- `cron-jobs.sh` only warns when the ingest fails -- and does
    nothing about a row already written.

    A tie on `ended` is the case the `ended` guard cannot decide: `stamp` keeps
    whole seconds, so two runs of one job can share one (sd:757, the #326
    review). Between two runs that each arrive for the first time, arrival
    order is the order: `cron-jobs.sh` holds a lock per job and ingests a run
    as it ends, so the second arrival ended second, and a job that failed and
    recovered inside one second still reads as recovered. What arrival order
    cannot rank is a *replay* of the older run, which arrives last. Only its
    run id tells it apart, and compaction can delete the row carrying that id.
    So a row written at a tie also carries `same_second`: the run ids of the
    newest row and of that row's own `same_second`, newest last, bounded at
    `REMEMBERED_RUNS` as a folded report's run ids are. Compaction keeps that
    newest row, so the run-id guard still finds every run of that second, and
    such a replay returns `"recorded"` instead of overwriting current health.
    What stays open is a run that never reached the database at all, recovered
    by hand, that ended in the very second of a newer tick: nothing recorded
    it, so nothing can rank it, and it is written as the newest. Closing that
    needs an order finer than a whole second, outside this function.

    Returns `"written"`, `"tied"` when written at a tie with a different run,
    `"recorded"` when a row already carries this run, or `"older"` when a
    newer tick is recorded, so a result can say which.
    """
    key = HEARTBEAT_KEY + job
    if connection.execute(
            "SELECT 1 FROM state WHERE kind='heartbeat' AND key=?"
            " AND (CASE WHEN json_valid(body) THEN json_extract(body,'$.run_id') END = ?"
            "      OR EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(body) THEN body END, '$.same_second')"
            "                 WHERE value = ?))",
            (key, run_id, run_id)).fetchone():
        return "recorded"
    latest = connection.execute(
        "SELECT body FROM state WHERE kind='heartbeat' AND key=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    newest_body = json.loads(latest["body"]) if latest else {}
    newest = newest_body.get("ended") or ""
    if ended < newest <= stamp((now + FUTURE_ALLOWANCE).isoformat()):
        return "older"
    body = {"healthy": healthy, "run_id": run_id, "ended": ended, "exit_code": exit_code}
    tied = ended == newest
    if tied:
        earlier = newest_body.get("same_second")
        earlier = [value for value in earlier if isinstance(value, str)] if isinstance(earlier, list) else []
        previous = [newest_body["run_id"]] if isinstance(newest_body.get("run_id"), str) else []
        body["same_second"] = [*earlier, *previous][-REMEMBERED_RUNS:]
    record_state(connection, "heartbeat", key=key, body=body)
    return "tied" if tied else "written"


def _quiet(beat, previous):
    """Why a clean run filed no report, true of this run (the #315 review).

    `previous` is the job's health before this run: `None` when it had
    never ticked, which is a baseline and not a tick that followed a clean
    one. A replay and a tie say so too, rather than borrowing the ordinary
    sentence.
    """
    if beat == "older":
        return "an older run than the job's newest recorded tick, so no heartbeat was written"
    if beat == "recorded":
        return "a replay of a run whose heartbeat is already recorded, so nothing was written"
    if previous is None:
        return "the job's first recorded tick, a baseline and not an event"
    if beat == "tied":
        return ("a clean tick that ended in the same second as the job's newest recorded tick, which was"
                " also clean; recorded after it, in the order the two arrived")
    return "a clean tick of a job whose last tick was also clean"


def ingest_log(connection, *, job, run_id, started, ended, exit_code, log_path, offset=0, now=None):
    """One cron run, recorded as an item only when something happened.

    A job that runs every fifteen minutes and succeeds every time used to file
    a report item every fifteen minutes. `claude-mem-pro-watchdog` had 102 of
    them open on the live database, every one `exit_code` 0 with `attention`
    false, and `retention` settles a clean report after seven days without
    removing the row -- so the count converges on the tick rate times seven
    days, not on anything a person needs to read.

    So a quiet tick writes a heartbeat and nothing else, which is the shape
    `retention`'s table already prescribes for heartbeats: one row per key, the
    newest being the only one any reader asks for. An item is created for the
    three things that are actually events:

    * the job failed, or declared findings with `SD_REPORT_ATTENTION`;
    * the job recovered -- this tick is clean and the last one was not, which
      is the transition nobody would otherwise see, because the failing report
      stays `planning` and never says it stopped being true;
    * nothing else. A first-ever clean tick is a baseline, not an event.

    A failure that repeats an open one -- same job, same basis -- is one
    event still counted, not a new one: it folds into the open report
    (`ingest`, sd:920) and the result says `folded` with the new `count`.

    The alternative was a per-job knob saying which jobs are noisy. That puts
    the decision in configuration a reader has to find, and it would still file
    a row for the tick where a quiet job broke.

    `now` is the clock a recorded `ended` is checked against (see `_beat`),
    an aware datetime; it defaults to the real one and exists for tests.

    The run is checked as `ingest` checks it -- the job name, the run id, an
    integer exit code, and an end not before the start -- before the log is
    read or a heartbeat written (the #315 review): the quiet path returns
    without reaching `ingest`, so its checks alone let a bad run write a
    heartbeat.
    """
    _check_identity(job, run_id)
    if type(exit_code) is not int:
        raise workflow.WorkflowError("report exit code and attention must be explicit typed values")
    _, ended = _check_times(started, ended)
    path = Path(log_path)
    if type(offset) is not int or offset < 0 or not path.is_file() or path.is_symlink():
        raise workflow.WorkflowError("report log must be a regular file with a valid starting offset")
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(MAX_REPORT + 1)
    truncated = len(data) > MAX_REPORT
    text = data[:MAX_REPORT].decode("utf-8", errors="replace")
    # A successful exit does not prove there were no findings. Jobs can emit
    # this explicit marker; absent it, a report filed for a clean run (a
    # recovery) says attention was not supplied.
    declared = re.search(r"(?m)^SD_REPORT_ATTENTION: (.+)$", text)
    attention = exit_code != 0 or bool(declared)
    basis = f"job exited {exit_code}" if exit_code else declared[1] if declared else "not supplied by the job"
    text = text.encode()[:MAX_REPORT].decode("utf-8", errors="ignore")
    with transaction(connection):
        # A run that already filed a report is answered with that report, the
        # way `ingest` answers an attention replay: a recovery's own heartbeat
        # makes the job read healthy, so its replay cannot be told by health.
        filed = connection.execute("SELECT 1 FROM item WHERE source='cron-report' AND external_id=?",
                                   (job + ":" + run_id,)).fetchone()
        # Read the previous health before this tick overwrites it, or a
        # recovery reads as a run that was always fine. And only a tick that
        # became the current health can recover from anything: an older run
        # was never the tick after the failure.
        previous = health(connection, job)
        beat = _beat(connection, job=job, run_id=run_id, healthy=not attention,
                     ended=ended, exit_code=exit_code, now=now or datetime.now(UTC))
        wrote = beat in ("written", "tied")
        recovered = not attention and wrote and previous is False
        # This run's own heartbeat says it was clean, and it filed no report,
        # so its first ingest took the quiet path. The run id is the key: a
        # replay that now reads a finding -- the log has grown a later run's
        # `SD_REPORT_ATTENTION` line, or the exit code was typed differently
        # -- is not a new event (the #315 review). The read runs only on such
        # a replay, never on a new run. A run named only in a `same_second`
        # list has no health of its own to read, and is left to `ingest`.
        if beat == "recorded" and attention and not filed and connection.execute(
                "SELECT 1 FROM state WHERE kind='heartbeat' AND key=?"
                " AND CASE WHEN json_valid(body) THEN json_extract(body, '$.run_id') END = ?"
                " AND CASE WHEN json_valid(body) THEN json_type(body, '$.healthy') END = 'true'",
                (HEARTBEAT_KEY + job, run_id)).fetchone():
            return {"recorded": "nothing", "job": job, "run_id": run_id, "healthy": False,
                    "exit_code": exit_code, "ended": ended,
                    "why": "a replay of a run whose heartbeat recorded it clean;"
                           " the run id is the key, so its changed evidence was not written"}
        if not (attention or recovered or filed):
            return {"recorded": "heartbeat" if wrote else "nothing", "job": job, "run_id": run_id,
                    "healthy": True, "exit_code": exit_code, "ended": ended, "why": _quiet(beat, previous)}
        state = ingest(connection, job=job, run_id=run_id, started=started, ended=ended,
                       exit_code=exit_code, text=text, source_path=str(path.resolve()),
                       attention=attention, attention_basis=basis, truncated=truncated)
        if state["item"]["external_id"] != job + ":" + run_id:
            # `ingest` answered with another run's report: this run folded
            # into it (sd:920), or is the replay of one that did.
            return {"recorded": "folded", "why": basis,
                    "count": json.loads(state["item"]["fields"])["repeats"]["count"], **state}
        return {"recorded": "report", "why": basis if attention else "the job recovered", **state}


def reports(connection, *, limit=200):
    return [dict(row) for row in connection.execute(
        "SELECT * FROM item WHERE kind='report' ORDER BY created_at DESC,id DESC LIMIT ?", (min(200, max(1, limit)),))]


def acknowledge(connection, item, *, expected_revision, who, resolve_ingest_followups=False):
    """Move one report to `done` on the operator's say-so, naming the operator.

    `who` has no default. The `transition` this makes is the one thing the
    store keeps about who ended the report, and a default writes a name that
    is true of every caller and identifies none of them: 281 reports went
    `planning -> done by user` in a single second, and the row cannot say what
    ran them. Both shipped callers already name themselves -- the dashboard
    `"dashboard"`, the pack CLI the login account -- so the only caller a
    missing argument ever served was an unattributed script, which is the one
    this refuses.

    An unresolved followup refuses the move, and the refusal names each note
    and `sd note resolve <id>`, the verb that closes one. `ingest` opens such
    a followup on every report for a run that needs attention, under
    `INGEST_SESSION`, so every failure report is born refusing this verb
    (sd:921: 51 watchdog reports on the live store, none acknowledgeable).
    `resolve_ingest_followups=True` resolves the followups the ingest wrote
    -- those and no other -- in the transaction that makes the transition, so
    a refusal or a failed write leaves them open, and the status note says
    which notes it resolved under the same `who`. `resolve_note` itself
    records only `resolved_at`; the status note is where the name lives. A
    followup a person wrote is work somebody still owes and blocks whatever
    the keyword says: acknowledging the report is not the same statement as
    having done that work.

    The ingest's followup is recognised by two things together: `session` is
    `INGEST_SESSION`, and the text is exactly what `ingest` derives from this
    report's own `fields.report`. The session alone would not do -- a caller
    of `workflow.add_item_note` chooses its `who`, and `"cron"` is a word --
    but a note that also reproduces the report's job, end stamp and
    attention basis word for word was written by the ingest or by someone
    forging it on purpose. Only this path reads `fields`; the default one
    never does, so a report whose fields are unreadable still finishes from
    the command line. The keyword is exactly `True` or `False`, as
    `ingest` takes `attention`: it changes what is persisted, so a truthy
    string does not get to mean yes.
    """
    if type(resolve_ingest_followups) is not bool:
        raise workflow.WorkflowError("resolve_ingest_followups must be exactly True or False")
    with transaction(connection):
        state = workflow._checked_state(connection, item, expected_revision)
        if state["item"]["kind"] != "report":
            raise workflow.WorkflowError("only report items can be acknowledged here")
        open_followups = [note for note in state["notes"] if note["kind"] == "followup" and not note["resolved_at"]]
        ingests = _ingest_followup_for(state["item"]["fields"]) if resolve_ingest_followups else None
        resolving = [note["id"] for note in open_followups
                     if note["session"] == INGEST_SESSION and note["body"] == ingests]
        blocking = [note["id"] for note in open_followups if note["id"] not in resolving]
        if blocking:
            raise workflow.WorkflowError("resolve the report's followups before acknowledging it: "
                                         + "; ".join(f"sd note resolve {note}" for note in blocking))
        for note in resolving:
            resolve_note(connection, note)
        reason = ("resolved the ingest's followup" + ("s " if len(resolving) > 1 else " ")
                  + ", ".join(str(note) for note in resolving)) if resolving else None
        transition(connection, item, "done", who=who, reason=reason)
        return workflow.item_state(connection, item)



def reopen(connection, item, *, expected_revision, who, reason=None):
    """Undo `acknowledge`: move one `done` report back, naming the operator (sd:2395).

    The report returns to the status its newest `<status> -> done` history
    note names, which is the status it was acknowledged from; with no such
    note, or one naming no known status, it returns to `planning`, the
    status every report is filed in. A report that is not `done` is
    returned as it stands: an Undo that races a reload changes nothing.

    `who` has no default, as on `acknowledge`. Followups that the
    acknowledgement resolved stay resolved: `resolve_ingest_followups`
    records them in the status note, and this verb does not guess which
    ones to open again. The dashboard's Acknowledge never resolves any.
    """
    who = workflow._text(who, "who")
    if reason is not None:
        reason = workflow._text(reason, "reason")
    with transaction(connection):
        state = workflow._checked_state(connection, item, expected_revision)
        if state["item"]["kind"] != "report":
            raise workflow.WorkflowError("only report items can be reopened here")
        if state["item"]["status"] != "done":
            return state
        target = "planning"
        for note in reversed(state["notes"]):
            match = note["kind"] == "status_change" and re.match(r"(\w+) -> done by ", note["body"])
            if match:
                if match[1] in STATUSES and match[1] != "done":
                    target = match[1]
                break
        transition(connection, item, target, who=who, reason="reopened" + (f": {reason}" if reason else ""))
        return workflow.item_state(connection, item)

def clean_candidates(connection, *, before):
    """The reports retention's rule calls clean at `before`, in id order.

    `kind='report'`, `status='planning'`, `fields.attention` exactly 0 (JSON
    false, or the number), `created_at` earlier than `before`, and no
    unresolved followup. Retention settles these at seven days; the bulk
    acknowledge starts from the same rows and refuses more, so the two cannot
    drift apart.

    `json_extract` sits inside `CASE WHEN json_valid(...)`, as in `_beat`: a
    `CASE` evaluates its branch only when the test is true, whatever order the
    query plan picks for `AND` terms, so a row whose `fields` is not JSON is
    not a candidate instead of failing the whole read.

    No transaction and no savepoint: the caller owns the read. Retention reads
    under its write lock, the dry run under one snapshot.
    """
    return [row["id"] for row in connection.execute(
        "SELECT id FROM item WHERE kind='report' AND status='planning'"
        " AND CASE WHEN json_valid(fields) THEN json_extract(fields, '$.attention') END = 0"
        " AND created_at < ?"
        " AND NOT EXISTS (SELECT 1 FROM note WHERE note.item = item.id"
        "                 AND note.kind='followup' AND note.resolved_at IS NULL)"
        " ORDER BY id", (before,))]


def cutoff(value):
    """The one `before` both surfaces pass on: a date, meaning 00:00 UTC.

    `YYYY-MM-DD` becomes `YYYY-MM-DDT00:00:00+00:00`, and that exact stamp is
    returned as it is, so the stamped value can travel through a hidden field
    or a pasted command and come back unchanged. Everything else is refused:
    another time or offset, `Z`, a date that does not exist. `clean_reports`
    takes only the stamp this returns, and refuses even another spelling of
    the same instant, so a caller that skipped this fails there rather than
    getting a different cutoff, or a different plan token, by accident
    (sd:1168).
    """
    match = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(T00:00:00\+00:00)?", value) if isinstance(value, str) else None
    if match:
        try:
            date.fromisoformat(match[1])
        except ValueError:
            pass
        else:
            return match[1] + "T00:00:00+00:00"
    raise workflow.WorkflowError("give the cutoff as a date, YYYY-MM-DD, meaning 00:00 UTC")


#: A `fields` that cannot say the report is clean, in SQL over the `item`
#: table. One meaning for the three shapes the nullable, unchecked column can
#: hold besides a document: NULL, the empty string and text that is not JSON
#: (sd:873). Such a report is for a person to look at. Retention never settles
#: it and names it in the prune report; the bulk clean declines it; the
#: backlog does not list it on its own word, because it has none; the page
#: renders it, says why it is here and withholds the one-click acknowledge,
#: so the command line, whose acknowledge takes a named `who`, finishes it.
#: `json_valid(NULL)` is NULL and `json_valid('')` is 0, so `NOT json_valid`
#: alone would miss the first: the `coalesce` is what makes the three one.
#: `retention` reads this name, not a copy, so the two cannot drift; the
#: tests at each site pin the same three inputs.
UNREADABLE_FIELDS = "coalesce(json_valid(fields), 0) = 0"


class UnreadableFields(workflow.WorkflowError):
    """A stored `fields` that `UNREADABLE_FIELDS` says no reader can read."""


def _not_json(constant):
    # `json.loads` accepts `NaN`, `Infinity` and `-Infinity`; `json_valid`
    # does not. SQLite has already refused them by the time this runs, so
    # this is the belt to that brace.
    raise UnreadableFields(f"{constant} is not JSON")


def _json_int(token):
    # `int` refuses a decimal string of 4300 digits or more (Python's
    # `sys.int_max_str_digits`), and `json_valid` accepts one. `Decimal`
    # holds the same exact value without that limit and prints it back
    # digit for digit, so a reader shows it rather than refusing the row.
    try:
        return int(token)
    except ValueError:
        return Decimal(token)


def fields_document(connection, value):
    """The stored `fields` parsed, or `UnreadableFields` when SQLite says so.

    For a reader that shows a report -- the dashboard's item page -- and must
    give the verdict retention and the bulk clean give. It asks SQLite first,
    with `UNREADABLE_FIELDS` over the value, so the verdict is theirs by
    construction and not Python's: `json.loads` accepts `NaN` and `Infinity`,
    which `json_valid` refuses, and it reads nesting past `json_valid`'s
    depth limit (1000 in SQLite 3.53) up to its own recursion limit, where it
    raises `RecursionError` instead (the review of #395, both rounds). An
    integer of 4300 or more digits, which `json_valid` accepts and `int`
    refuses, parses as a `Decimal` of the same value (`_json_int`), so the
    page agrees with retention there too (the third round). The
    query runs on `connection` because the library is the one thing that
    issues SQL; it reads no table, so a read-only connection serves.
    """
    unreadable = connection.execute(
        f"SELECT {UNREADABLE_FIELDS} FROM (SELECT ? AS fields)", (value,)).fetchone()[0]
    if unreadable:
        raise UnreadableFields("the stored fields are missing or not valid JSON")
    try:
        return json.loads(value, parse_constant=_not_json, parse_int=_json_int)
    except (TypeError, ValueError, RecursionError) as error:
        raise UnreadableFields(f"the stored fields could not be parsed: {error}") from error


#: Why a `planning` report created before the cutoff is not selected, in the
#: order the reasons are tested. The first that applies is the one given.
DECLINED = (
    ("unreadable", "its fields are not valid JSON, so it cannot say it is clean"),
    ("record", "an operator record, not a run report"),
    ("attention", "it needs attention, which waits for a person"),
    ("unclean", "it never said it was clean"),
    ("followup", "it has an unresolved followup"),
    ("assignment", "work on it is queued, running or ending"),
    ("late", "its run ended after the job's newest recorded tick"),
)


def clean_reports(connection, *, before, now=None):
    """The dry run of a bulk acknowledge: what it would move, what it declines.

    Writes nothing and opens no transaction. Every read runs between one
    `SAVEPOINT` and its `RELEASE`, the way `workflow.item_state` reads, so all
    rows come from one snapshot and the call works on a `write=False`
    connection, where `BEGIN IMMEDIATE` does not.

    `before` is the stamp `cutoff` returns, exactly, and may not be later
    than `now`, an aware `datetime` that defaults to the real clock. `selected` is
    `clean_candidates` less the refusals retention does not make: a record
    marker, work in flight, a run newer than its job's recorded health.
    `declined` is every other `planning` report created before `before`, with
    the first reason that applies; a later or already-moved report is in
    neither list.

    `plan` is a sha256 over `before` and each selected id with its revision,
    so it changes when a report joins or leaves the selection or a listed one
    changes. It is `None` when nothing is selected or more than `MAX_BATCH`
    is, because no apply of that selection can succeed, and the revisions are
    then not read.
    """
    if cutoff(before) != before:
        raise workflow.WorkflowError(f"pass the cutoff as `cutoff` stamps it, {cutoff(before)}, not {before!r}")
    current = stamp((now or datetime.now(UTC)).isoformat())
    if before > current:
        raise workflow.WorkflowError(f"the cutoff {before} is later than now ({current}); give a date that has begun")
    connection.execute("SAVEPOINT sd_clean_reports")
    try:
        candidates = set(clean_candidates(connection, before=before))
        active = ", ".join("?" for _ in workflow.ACTIVE_ASSIGNMENTS)
        # The first five reasons in one read. Every JSON function sits inside
        # `CASE WHEN json_valid(fields)`, so no row can fail the query.
        rows = connection.execute(
            "SELECT id, title, created_at,"
            " CASE WHEN json_valid(fields) THEN json_extract(fields, '$.report.job') END AS job,"
            " CASE WHEN json_valid(fields) THEN json_extract(fields, '$.report.ended') END AS ended,"
            f" {UNREADABLE_FIELDS} AS unreadable,"
            " CASE WHEN json_valid(fields) THEN json_type(fields, '$.record') END IS NOT NULL AS record,"
            " CASE WHEN json_valid(fields) THEN json_extract(fields, '$.attention') END = 1 AS attention,"
            " CASE WHEN json_valid(fields) THEN json_extract(fields, '$.attention') END IS NOT 0 AS unclean,"
            " EXISTS (SELECT 1 FROM note WHERE note.item = item.id"
            "         AND note.kind='followup' AND note.resolved_at IS NULL) AS followup,"
            " EXISTS (SELECT 1 FROM assignment WHERE assignment.item = item.id"
            f"         AND assignment.status IN ({active})) AS assignment"
            " FROM item WHERE kind='report' AND status='planning' AND created_at < ? ORDER BY id",
            (*workflow.ACTIVE_ASSIGNMENTS, before)).fetchall()
        beats = {}
        selected, declined = [], []
        for row in rows:
            reasons = {key for key, _ in DECLINED[:6] if row[key]}
            job, ended = row["job"], row["ended"]
            if isinstance(job, str) and isinstance(ended, str):
                if job not in beats:
                    # The newest row by id, the one `health` and `_beat` read.
                    beat = connection.execute(
                        "SELECT CASE WHEN json_valid(body) THEN json_extract(body, '$.ended') END"
                        " FROM state WHERE kind='heartbeat' AND key=? ORDER BY id DESC LIMIT 1",
                        (HEARTBEAT_KEY + job,)).fetchone()
                    beats[job] = beat[0] if beat else None
                if isinstance(beats[job], str) and ended > beats[job]:
                    reasons.add("late")
            if row["id"] in candidates and not reasons & {"record", "assignment", "late"}:
                selected.append({"id": row["id"], "revision": None, "job": job,
                                 "created_at": row["created_at"], "title": row["title"]})
            else:
                declined.append({"id": row["id"], "why": next((why for key, why in DECLINED if key in reasons),
                                                              DECLINED[3][1])})
        plan = None
        if 1 <= len(selected) <= MAX_BATCH:
            for entry in selected:
                entry["revision"] = workflow.item_state(connection, entry["id"])["revision"]
            token = json.dumps([before, sorted([entry["id"], entry["revision"]] for entry in selected)],
                               separators=(",", ":"))
            plan = hashlib.sha256(token.encode()).hexdigest()
    finally:
        connection.execute("RELEASE sd_clean_reports")
    return {"before": before, "selected": selected, "declined": declined, "count": len(selected),
            "declined_count": len(declined), "plan": plan, "max_batch": MAX_BATCH}


def acknowledge_clean(connection, *, before, expected_plan, who, principal, program, session=None, now=None):
    """Move the previewed clean reports to `done`, all or none, naming who did.

    `expected_plan` is the `plan` of `clean_reports` at the same `before`.
    Inside one `BEGIN IMMEDIATE` the selection and its plan are computed
    again; any difference, including an empty or oversized selection, whose
    preview issues no plan, is `StaleItem` and writes nothing.

    That sentence is about the selection computed here, not about the token
    passed in. `expected_plan` must be a plan, 64 lowercase hex digits. A
    preview's `None` is its answer "no apply can succeed", not a token: the
    dashboard renders no apply form for it and refuses any other plan
    shape, and the CLI prints no apply command. So `None`, like any other
    malformed token, is a bad request refused before the transaction
    (`WorkflowError`, not `StaleItem`; the #347 review asked otherwise).

    Then one batch report is filed as job `reports-acknowledge`, with the plan
    as its run id, the moved ids as its text, `who`, the `principal` the
    caller's channel authenticated, the program and the process as its
    `actor`, and the `record` marker. Each report moves with a note naming
    that batch report, and the batch report itself moves to `done`, so it is
    never a `planning` report for retention or a later bulk run to take.

    `who` and `principal` have no default, for the reason `acknowledge` gives.
    `session` is not a name the caller chooses, so an over-long one is cut to
    200 characters rather than refused.
    """
    if not isinstance(expected_plan, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_plan):
        raise workflow.WorkflowError("preview the clean reports and pass its plan")
    who, principal, program = (workflow._text(value, name) for value, name in
                               ((who, "who"), (principal, "principal"), (program, "program")))
    for value, name in ((who, "who"), (principal, "principal"), (program, "program")):
        if len(value) > 200:
            raise workflow.WorkflowError(f"{name} must be at most 200 characters")
    if session is not None and not isinstance(session, str):
        raise workflow.WorkflowError("session must be text")
    session = session[:200] if session and session.strip() else None
    now = now or datetime.now(UTC)
    stamped_now = now.astimezone(UTC).isoformat(timespec="seconds")
    with transaction(connection):
        preview = clean_reports(connection, before=before, now=now)
        if preview["plan"] != expected_plan:
            raise workflow.StaleItem("the clean-report selection changed since the preview; preview it again")
        ids = [entry["id"] for entry in preview["selected"]]
        if not 1 <= len(ids) <= MAX_BATCH:
            raise workflow.WorkflowError(f"a bulk acknowledge moves between 1 and {MAX_BATCH} reports")
        actor = {"who": who, "principal": principal, "program": program,
                 "pid": os.getpid(), "ppid": os.getppid(), "session": session}
        batch = ingest(connection, job=BULK_JOB, run_id=expected_plan, started=stamped_now, ended=stamped_now,
                       exit_code=0, text="".join(f"{item}\n" for item in ids), source_path=program,
                       attention=False, actor=actor, record=BULK_JOB)["item"]["id"]
        for item in ids:
            transition(connection, item, "done", who=who, reason=f"bulk acknowledge, report #{batch}")
        transition(connection, batch, "done", who=who, reason="bulk acknowledge record")
        state = workflow.item_state(connection, batch)
        return {"item": state["item"], "revision": state["revision"], "acknowledged": ids, "actor": actor}


def observed_action(connection, action, *, item=None, channel="dashboard"):
    """Count accepted requests explicitly; a browser request is not human labor."""
    record_state(connection, "checkpoint", key="operator-action",
                 body={"action": action, "item": item, "channel": channel})


def metrics(connection, *, now):
    since, until = reads._week(now)
    upper = until + "T23:59:59Z"
    actions = connection.execute("SELECT count(*) FROM state WHERE kind='checkpoint' AND key='operator-action' AND timestamp BETWEEN ? AND ?", (since, upper)).fetchone()[0]
    reviews = connection.execute("SELECT count(*) FROM assignment WHERE role='reviewer' AND ended BETWEEN ? AND ?", (since, upper)).fetchone()[0]
    delivered = connection.execute("SELECT count(*) FROM item WHERE shipped_at BETWEEN ? AND ?", (since, upper)).fetchone()[0]
    return {"since": since, "until": until, "recorded_requests": actions,
            "finished_review_attempts": reviews, "recorded_deliveries": delivered,
            "interpretation": "Observed database events. Requests may come from a person or an agent; they do not measure human interventions, time saved or adoption."}
