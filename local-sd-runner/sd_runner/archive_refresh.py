"""Append whole-clone archive generations without replacing retained evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tarfile
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_db.database import connect

from . import maintenance, processes, reconciliation, restoration, storage

CADENCE_SECONDS = 24 * 60 * 60
GENERATION = re.compile(r"\d{8}T\d{12}Z-[a-f0-9]{32}")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _stamp(now):
    return now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex


def _write_new(path, document):
    with path.open("x") as stream:
        json.dump(document, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    journal.fsync_directory(path.parent)


def _generation(run, path):
    root = Path(run["retained_path"]).parent / "archives"
    if not GENERATION.fullmatch(path.name) or path.parent != root:
        raise store.RunnerRefused("archive generation has an invalid identity")
    if root.is_symlink() or path.is_symlink() or not path.is_dir():
        raise store.RunnerRefused("archive generation directory is linked or absent")
    receipt = path / "manifest.json"
    if not receipt.exists():
        return None  # Interrupted generations remain on disk and are never selected.
    if receipt.is_symlink() or not receipt.is_file():
        raise store.RunnerRefused("archive generation receipt is linked or absent")
    record = json.loads(receipt.read_text())
    if (not isinstance(record, dict) or record.get("version") != 1 or record.get("run") != run["id"]
            or record.get("original_retained_path") != run["retained_path"]
            or record.get("inventory_sha256") != _digest(record.get("files"))):
        raise store.RunnerRefused("archive generation receipt differs from its owned run")
    if maintenance._file(path / "kept.tar") != record.get("archive"):
        raise store.RunnerRefused("archive generation bytes differ from their receipt")
    return record


def _generations(run):
    root = Path(run["retained_path"]).parent / "archives"
    if root.is_symlink():
        raise store.RunnerRefused("archive generation root is linked")
    if not root.exists():
        return
    for path in sorted(root.iterdir(), reverse=True):
        record = _generation(run, path)
        if record is not None:
            yield path, record


def latest(run, *, expected_sha256=None):
    """Select a verified generation; pin interrupted restores by inventory hash."""
    for path, record in _generations(run):
        if expected_sha256 is None or record["inventory_sha256"] == expected_sha256:
            return {**run, "retained_path": str(path / "clone")}
    return run


def schedule(config, *, now=None):
    """Report the persisted daily cadence without installing or writing a job."""
    now = now or datetime.now(UTC)
    root = config.retention / ".archive-refresh"
    if root.is_symlink():
        raise store.RunnerRefused("archive refresh receipt directory is linked")
    last = None
    for path in root.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            raise store.RunnerRefused("archive refresh cadence receipt is linked")
        record = json.loads(path.read_text())
        if (not isinstance(record, dict) or record.get("version") != 1 or record.get("operation") != "archive-refresh"
                or record.get("database") != str(config.database.resolve())
                or record.get("retention_root") != str(config.retention.resolve())):
            raise store.RunnerRefused("archive refresh cadence belongs to another configuration")
        completed = datetime.fromisoformat(record["completed_at"])
        if completed.tzinfo is None:
            raise store.RunnerRefused("archive refresh cadence timestamp has no timezone")
        last = max(last, completed) if last else completed
    due = last + timedelta(seconds=CADENCE_SECONDS) if last else now
    return {"dry_run": True, "cadence_seconds": CADENCE_SECONDS,
            "last_completed_at": last.isoformat() if last else None,
            "next_due_at": due.isoformat(), "due": now >= due,
            "activation": "called by the runner; no independent scheduler is installed"}


def _space(config, size):
    if config.floor_gb <= 0:
        raise store.RunnerRefused("archive refresh needs a positive free-space floor")
    free = storage.capacity(config.retention)["free"]
    database_free = storage.capacity(config.database)["free"]
    if min(free - size, database_free - size) < config.floor_gb * 1e9:
        raise store.RunnerRefused("archive refresh would cross the configured free-space floor")


def _issue_hold(issues):
    """Name each held entry beside the verb that will actually accept it."""
    # `reconciliation.quarantine` refuses an issue whose `blocked` is set, and
    # the only repair for a blocked entry is `recovery-unlink`, which takes the
    # restore-linked shape alone. So a blocked entry gets pointed at recovery
    # rather than at a verb the library would refuse -- a refusal naming an
    # impossible action leaves the operator with no next step at all.
    blocked = sorted(issue["entry"] for issue in issues if issue["blocked"])
    plain = sorted(issue["entry"] for issue in issues if not issue["blocked"])
    clauses = [text for text in (f"recovery for {', '.join(blocked)}" if blocked else "",
                                 f"quarantine for {', '.join(plain)}" if plain else "") if text]
    return ("archive refresh held by journal entries; runner.sh recovery-plan names their fingerprints: "
            + "; ".join(clauses))


def _safe_state(config):
    report = reconciliation.plan(config)
    # `reconcile` refuses on the same field: an issue is a journal entry whose
    # ownership authority could not be read, and a blocked one is a second path
    # into the runner's durable evidence. Since #362 an issue withholds only the
    # run it names, so an issue beside no other run leaves `entries` empty --
    # holding on `entries` alone would let the nightly write generations under
    # ownership the operator's repair path refuses to touch.
    if report["journal_issues"]:
        raise store.RunnerRefused(_issue_hold(report["journal_issues"]))
    if report["entries"] or report["restore_pending"]:
        raise store.RunnerRefused("archive refresh held by unresolved restore or ownership evidence")


def _archive_inventory(path):
    result = {}
    with tarfile.open(path) as archive:
        for name, member in restoration._archive_members(archive).items():
            entry = {"mode": member.mode}
            if member.isdir():
                entry["directory"] = True
            elif member.issym():
                entry = {"link": member.linkname}
            else:
                with archive.extractfile(member) as stream:
                    entry["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            result[name] = entry
    return result


def _holders(survivors):
    """Name each holder the refusal is about: pid, ownership, command.

    The first time this refusal fired in CI (run 34700878618, 2026-09-12) it
    said only that the clone "gained process holders", so nothing recorded
    which process it was -- the suspect is Spotlight's indexer reaching into a
    just-rewritten fixture file, and the message could not say. `maintenance`
    already lists its holders on the skipped entry; this puts them in the
    sentence, where `sd runner status` and the test both read them.
    """
    return ", ".join(f"{row['pid']} ({row['ownership']}) {row['command']}" for row in survivors)

def _one(config, connection, ident, now):
    with journal.lock(config.database.parent / "runner-ending" / f"{ident}.lock", blocking=False):
        _safe_state(config)
        run = store.run_state(connection, ident)
        request = store.queue_state(connection, run["assignment"])
        if (request["status"] != "ending" or run["end_step"] != "kept"
                or run["released_at"] or run["quarantine"] or run["end_action"] == "resume"):
            raise store.RunnerRefused("attempt is no longer an unquarantined kept clone")
        clone = Path(run["work_path"])
        expected = config.work / str(request["item"]) / f"{run['assignment']}-{run['run']}-{ident}"
        if (clone.absolute() != expected.absolute() or clone.is_symlink() or not clone.is_dir()
                or not clone.resolve().is_relative_to(config.work.resolve())):
            raise store.RunnerRefused("kept clone differs from its configured owned path")
        retained = maintenance._clone(config, run)
        with journal.lock(retained.parent / ".archive.lock", blocking=False):
            if held := processes.survivors(run):
                raise store.RunnerRefused(f"kept clone has process holders: {_holders(held)}")
            # The archive leaves caches and Cargo output out; so do the
            # inventories it is compared with (sd:1774).
            skip = storage.archive_skip(clone)
            before = storage.walk(clone, skip=skip)
            identity = (clone.stat().st_dev, clone.stat().st_ino)
            files = restoration.inventory(clone, skip=skip)
            if storage.walk(clone, skip=skip) != before:
                raise store.RunnerRefused("kept clone changed during inventory")
            known = next(_generations(run), None)
            if known and known[1]["files"] == files:
                return {"run": ident, "status": "unchanged", "generation": str(known[0])}
            # Tar headers, padding, and PAX paths need headroom beyond file
            # bytes. The formula lives with the writer, so this check and the
            # bytes a no-space hold reports cannot drift apart (sd:1221).
            _space(config, storage.tar_needed(before))
            root = retained.parent / "archives"
            root.mkdir(mode=0o700, exist_ok=True)
            generation = root / _stamp(now)
            generation.mkdir(mode=0o700)
            journal.fsync_directory(root)
            archive = storage.archive(clone, generation / "kept.tar")
            if (storage.walk(clone, skip=skip) != before or restoration.inventory(clone, skip=skip) != files
                    or (clone.stat().st_dev, clone.stat().st_ino) != identity
                    or _archive_inventory(archive) != files):
                raise store.RunnerRefused(f"kept clone changed during refresh; unselected generation retained at {generation}")
            if held := processes.survivors(run):
                raise store.RunnerRefused(f"kept clone gained process holders: {_holders(held)}; unselected generation retained at {generation}")
            _safe_state(config)
            if store.run_state(connection, ident) != run:
                raise store.RunnerRefused(f"owned attempt changed during refresh; unselected generation retained at {generation}")
            record = {"version": 1, "run": ident, "original_retained_path": run["retained_path"],
                      "source_path": str(clone), "created_at": now.isoformat(), "files": files,
                      "inventory_sha256": _digest(files), "archive": maintenance._file(archive)}
            _write_new(generation / "manifest.json", record)
            return {"run": ident, "status": "archived", "generation": str(generation),
                    "inventory_sha256": record["inventory_sha256"], "archive": str(archive),
                    "restore_run": {**run, "retained_path": str(generation / "clone")}}


def refresh(config, *, now=None):
    """Refresh once daily, preserving every prior complete or partial generation."""
    now = now or datetime.now(UTC)
    report = {"version": 1, "operation": "archive-refresh", "entries": [], "skipped": [],
              "database": str(config.database.resolve()), "retention_root": str(config.retention.resolve())}
    try:
        _safe_state(config)
        _space(config, 65536)
        with journal.lock(config.retention / ".archive-refresh.lock", blocking=False):
            cadence = schedule(config, now=now)
            if not cadence["due"]:
                return {**report, "status": "not-due", "schedule": cadence}
            with closing(connect(config.database, write=False)) as connection:
                for run in store.active_runs(connection):
                    if run["end_step"] != "kept":
                        continue
                    try:
                        report["entries"].append(_one(config, connection, run["id"], now))
                    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError,
                            tarfile.TarError, store.RunnerRefused) as error:
                        report["skipped"].append({"run": run["id"], "reason": str(error)})
            # A skipped candidate remains retryable; it does not advance cadence.
            if report["skipped"]:
                return {**report, "status": "held"}
            _safe_state(config)
            root = config.retention / ".archive-refresh"
            root.mkdir(mode=0o700, exist_ok=True)
            receipt = root / (_stamp(now) + ".json")
            _write_new(receipt, {**report, "completed_at": now.isoformat()})
            return {**report, "status": "complete", "receipt": str(receipt), "schedule": schedule(config, now=now)}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError,
            tarfile.TarError, store.RunnerRefused) as error:
        return {**report, "status": "held", "reason": str(error)}
