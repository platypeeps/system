"""The nightly row prune: what ages out of the database, after a backup passed.

One retention table, read by one prune that `sd-db.sh backup` runs after
its snapshot restored and compared, and never before. What the table says:

    cost rows        never.  The ledger: an assignment's budget is admitted
                     by summing its rows and the item screen reports an item
                     by them, so a hole in it is a budget spent twice or a
                     cost understated (prd C-70).
    exec notes       never.  The audit record of what the palette ran --
                     identity, entry, arguments, exit code -- and it leaves
                     only with its item (prd C-66).
    exec output      ninety days.  The output file under `<db>/executions/`
                     is removed and the note is marked `output expired`; the
                     palette then says so instead of reading nothing.
    heartbeat rows   one per key.  Schema 005 already keeps `runner` unique
                     with a partial index; every other key accumulates a row
                     per tick, and the newest is the only one any reader asks
                     for.
    clean reports    seven days in `planning`, then `done`.  `reporting.ingest`
                     opens every report as `planning`; an attention report
                     carries a followup and waits for a person, a clean one
                     carries nothing and waited for nobody -- 127 of them sat
                     in `planning` on the live database on 2026-09-12. The
                     row stays; only its status moves, by the transition
                     `acknowledge` uses, as `retention`.

                     Since sd:739 there are far fewer of them to settle: a
                     quiet tick through `reporting.ingest_log` writes a
                     heartbeat instead of an item, so a clean report from
                     that path now means a job that recovered, which is a
                     one-off rather than a thing arriving every fifteen
                     minutes.  It is `ingest_log` that changed, not
                     `ingest`: a caller that files a report directly still
                     files one per run, and `prune` below is such a caller
                     -- its own successful report is clean and is not a
                     recovery.  This rule is unchanged and still needed --
                     recoveries accumulate too, direct callers keep
                     arriving, and rows predating that change are still
                     here.
    backups          thirty files -- `backup --keep N`, not here. The
                     scheduled job runs `--keep all` by the September 9
                     amendment, and this module does not touch it. The
                     hourly job keeps a rolling week with `--keep-days 7`
                     and skips this prune with `--no-row-prune`, so it
                     files no report item.
    kept worktrees   never.  A kept clone may hold work; its retention is the
                     runner's (`runner_retention`), not the prune's.

There is no request-log rule, and the prd's table no longer names one (it
said "the request log thirty days" until the 2026-09-12 amendment, sd:541).
There is no such store: the dashboard silences its HTTP log (`server.py`'s
`log_message`) and writes no request rows; the `operator-action` checkpoint
rows are accepted mutations `reporting.metrics` counts by week, not a
request log. Should a store ever be written, its rule goes in the prd's
table first and here second.

The prune writes one `report` item with the counts it removed, so the
Operations screen shows what the night took, and it refuses to run against
a backup that did not pass: not the caller's word for it, but the dated
directory verified again and its checkpoint row found in this database.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import reporting
from .backup import Snapshot, passed
from .database import refuse_hub_only, transaction
from .errors import SdDbError
from .writes import transition

#: How long an `exec` note keeps its output file.
EXEC_OUTPUT_AGE = timedelta(days=90)

#: How long a clean run report waits in `planning` for an acknowledgement
#: before retention settles it.
CLEAN_REPORT_AGE = timedelta(days=7)

#: The `who` on the status_change note a settled report carries.
RETENTION = "retention"

#: The key the note's body carries once its output is gone. The body is the
#: execution descriptor; `verify_descriptor` refuses a descriptor with an
#: extra key, which is right: an expired execution is never run again.
OUTPUT_EXPIRED = "output_expired"

#: The job name the report row is filed under.
JOB = "sd-db-prune"

#: How many ids of `planning` reports with unreadable `fields` the prune
#: report names: all of them up to this many in its text, far inside
#: `reporting.MAX_REPORT`, and fewer in the attention basis, which `ingest`
#: refuses past 2000 characters -- about 280 four-digit ids. A refused prune
#: report would fail the backup job every night after the expiry, compaction
#: and settle had already committed.
UNREADABLE_TEXT_IDS = 1000
UNREADABLE_BASIS_IDS = 20

LOG_NAME = re.compile(r"[a-f0-9]{32}\.log")


class RetentionRefused(SdDbError):
    """The prune did not run, and nothing was removed."""


@dataclass(frozen=True)
class Pruned:
    """What one night's prune removed, and the report row that says so."""

    exec_outputs: int
    heartbeats: int
    clean_reports: int
    report: int

    @property
    def counts(self) -> dict[str, int]:
        return {"exec_outputs": self.exec_outputs, "heartbeats": self.heartbeats,
                "clean_reports": self.clean_reports}

    def __str__(self) -> str:
        return (f"{self.exec_outputs} exec output(s) expired, "
                f"{self.heartbeats} stale heartbeat row(s) removed, "
                f"{self.clean_reports} clean report(s) settled")


def _cutoff(now: datetime, age: timedelta) -> str:
    return (now - age).astimezone(UTC).isoformat(timespec="seconds")


def _output_files(connection, row) -> list[Path]:
    """The note's log and its receipt, only where the note says they are.

    A path that spells `<db>/executions` is not inside it when `executions`
    is itself a symlink onto somewhere else. The textual comparison passed
    that, and `_unlink` rejects a symlinked *file* but never looked at the
    directory above it, so a replaced `executions` made the prune delete
    through the link. So the component is checked as well as the spelling.

    Only that component. Resolving both sides would not do: a symlinked
    `executions` resolves to the same place from either side and compares
    equal to itself. And a link above it, in the database's own directory,
    is the operator's choice of where the store lives, not a redirection of
    what the prune may delete.
    """
    refuse_hub_only(connection, "the executions prune")
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    executions = database.parent / "executions"
    path = Path(row["output_path"] or "")
    if path.parent != executions or executions.is_symlink() or not LOG_NAME.fullmatch(path.name):
        raise RetentionRefused(f"exec note {row['id']} names output outside the executions directory: {path}")
    return [path, path.with_suffix(".receipt.json")]


def _unlink(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


def expire_exec_outputs(connection, *, now: datetime) -> int:
    """Remove output files older than `EXEC_OUTPUT_AGE`; keep every note.

    The mark is written first and the file removed second: a marked note
    whose file is still on disk is harmless (the next prune removes it), a
    removed file whose note is unmarked would fail the next backup's
    evidence check. An unfinished note is never touched, however old --
    `reconcile` needs its file to decide what happened.
    """
    cutoff = _cutoff(now, EXEC_OUTPUT_AGE)
    rows = connection.execute(
        "SELECT id, body, output_path FROM note WHERE kind='exec' AND ended IS NOT NULL AND ended < ? ORDER BY id",
        (cutoff,)).fetchall()
    stamp = now.astimezone(UTC).isoformat(timespec="seconds")
    files: list[Path] = []
    with transaction(connection):
        for row in rows:
            try:
                value = json.loads(row["body"])
            except ValueError:
                continue
            if not isinstance(value, dict) or value.get("note") != row["id"]:
                continue
            files.extend(_output_files(connection, row))
            if OUTPUT_EXPIRED not in value:
                value[OUTPUT_EXPIRED] = stamp
                connection.execute("UPDATE note SET body=? WHERE id=?", (json.dumps(value, sort_keys=True), row["id"]))
    removed = 0
    for path in files:
        if _unlink(path) and path.suffix == ".log":
            removed += 1
    return removed


def compact_heartbeats(connection) -> int:
    """One `heartbeat` row per key: the one every reader orders first.

    An open row ranks before a resolved one: a satellite claim (sd:2918) is
    written on the satellite's clock, so its replacement may carry an older
    timestamp than the claim it resolved. Other heartbeats are never
    resolved, so for them the order is the timestamp, as before."""
    with transaction(connection):
        cursor = connection.execute(
            "DELETE FROM state WHERE kind='heartbeat' AND id NOT IN ("
            "  SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY key ORDER BY resolved_at IS NULL DESC,"
            "                  timestamp DESC, id DESC) AS rank"
            "                  FROM state WHERE kind='heartbeat') WHERE rank = 1)")
    return cursor.rowcount


def settle_clean_reports(connection, *, now: datetime) -> int:
    """Move a clean run report nobody acknowledged to `done` after seven days.

    A clean report is `kind='report'` with `fields.attention` false and no
    *open* followup on it, so `reporting.acknowledge` -- the operator's
    verb -- is the only thing that ever moved one, and the operator has
    nothing left to review on it. A resolved followup does not hold the row:
    it was reviewed, and the review is what `resolved_at` records. An
    attention report, or any report with an open followup, is never touched
    however old: those wait for a person.
    The write is the same `transition` `acknowledge` makes, so the history
    reads `planning -> done by retention` and the row itself stays.

    The candidate read sits inside the transaction, which is `BEGIN
    IMMEDIATE`: a followup landing between the read and the write would
    otherwise be settled over, so the read takes the write lock first and a
    followup either arrives before it and is seen, or waits behind the sweep.
    `json_extract` answers NULL for a row with no `attention` key, and NULL
    `= 0` is not true: a report that never said it was clean is not settled.
    A report whose `fields` is not valid JSON is not settled either, and the
    prune report names it.

    The rule is `reporting.clean_candidates`, which the bulk acknowledge
    starts from too, so the two read one predicate.
    """
    cutoff = _cutoff(now, CLEAN_REPORT_AGE)
    with transaction(connection):
        rows = reporting.clean_candidates(connection, before=cutoff)
        for row in rows:
            transition(connection, row, "done", who=RETENTION)
    return len(rows)


def _unreadable(ids: list[int], limit: int) -> str:
    named = ", ".join(f"#{item}" for item in ids[:limit])
    more = f" and {len(ids) - limit} more" if len(ids) > limit else ""
    return f"{len(ids)} report(s) in planning have unreadable fields: {named}{more}"


def prune(connection, backup: Snapshot, *, now: datetime | None = None) -> Pruned:
    """Run the retention table against this database, after `backup` passed."""
    refuse_hub_only(connection, "the retention prune")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise RetentionRefused("the prune needs an aware time; a naive one read as UTC is wrong by the offset")
    if not isinstance(backup, Snapshot) or not passed(connection, backup):
        raise RetentionRefused(
            "the prune refuses to run: the night's backup did not pass, or is not this database's; nothing was removed")
    # The report is the last write and `ingest` refuses a second one for the
    # same run id with different evidence -- so without this the second prune
    # against one snapshot expired outputs, compacted heartbeats and settled
    # reports in three committed transactions, and only then failed. Each of
    # those is irreversible and none of them is the report the operator reads.
    # One backup is one prune, so the identity is checked while nothing has
    # been removed rather than after.
    if connection.execute(
            "SELECT 1 FROM item WHERE source = 'cron-report' AND external_id = ?",
            (f"{JOB}:{backup.run_id}",)).fetchone():
        raise RetentionRefused(
            f"the prune refuses to run: backup {backup.directory.name} ({backup.run_id}) already has its "
            f"prune report; one backup is one prune, and nothing was removed")
    stamp = now.astimezone(UTC).isoformat(timespec="seconds")
    exec_outputs = expire_exec_outputs(connection, now=now)
    heartbeats = compact_heartbeats(connection)
    clean_reports = settle_clean_reports(connection, now=now)
    result = Pruned(exec_outputs=exec_outputs, heartbeats=heartbeats, clean_reports=clean_reports, report=0)
    text = (f"sd-db prune after backup {backup.directory.name} ({backup.run_id}): {result}; "
            f"cost rows and exec notes are never pruned.\n")
    # A report the settle cannot read would otherwise sit in `planning`
    # silently, where before the guard it failed the whole prune. So the
    # prune report names it and asks for a person. The predicate is the dry
    # run's own, `reporting.UNREADABLE_FIELDS`, so NULL, '' and text that is
    # not JSON are one case here as they are there (sd:873).
    unreadable = [row[0] for row in connection.execute(
        "SELECT id FROM item WHERE kind='report' AND status='planning'"
        f" AND {reporting.UNREADABLE_FIELDS} ORDER BY id")]
    attention = {}
    if unreadable:
        text += _unreadable(unreadable, UNREADABLE_TEXT_IDS) + "\n"
        attention = {"attention": True, "attention_basis": _unreadable(unreadable, UNREADABLE_BASIS_IDS)}
    # One backup, one prune, one report: the row's identity is the backup's
    # run id, so a second prune against the same snapshot is refused by
    # `ingest` as a report that already exists with different evidence.
    state = reporting.ingest(connection, job=JOB, run_id=backup.run_id, started=stamp, ended=stamp,
                             exit_code=0, text=text, source_path=str(backup.directory), removed=result.counts,
                             **attention)
    return Pruned(exec_outputs=exec_outputs, heartbeats=heartbeats, clean_reports=clean_reports,
                  report=state["item"]["id"])
