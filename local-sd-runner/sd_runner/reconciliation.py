"""Exact-state recovery of durable ownership, without deleting evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import ExitStack
from pathlib import Path

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_db.database import connect

from . import processes
from .runtime import journal_differs


MAX_JOURNAL_BYTES = 16 * 1024 * 1024


def _private_directory(path):
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise store.RunnerRefused(f"recovery directory must be private, owned and unlinked: {path}")


def _metadata(details):
    return {"device": details.st_dev, "inode": details.st_ino, "mode": stat.S_IMODE(details.st_mode),
            "uid": details.st_uid, "gid": details.st_gid, "size": details.st_size,
            "mtime_ns": details.st_mtime_ns, "ctime_ns": details.st_ctime_ns}


def _journal_view(database):
    """Describe invalid entries without granting them ownership authority."""
    root = journal.directory(database)
    if not root.exists() and not root.is_symlink():
        return [], []
    _private_directory(root)
    records, issues = [], []
    # C-50: `removal.apply` renames journal files into quarantine, and a rename
    # can land between the listing and a read here. An entry that is gone by
    # the time it is read is skipped, not reported unreadable. That hides
    # nothing: a run whose journal is missing still shows up in `plan` as a
    # `journal-from-database` entry, and `restore_holds` still holds it. Only
    # `FileNotFoundError` is caught -- an entry swapped for a symlink still
    # lstats, and is blocked (or refused by `O_NOFOLLOW`) exactly as before.
    for path in sorted(root.iterdir()):
        try:
            details = path.lstat()
        except FileNotFoundError:
            continue
        metadata = _metadata(details)
        blocked = (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                   or details.st_nlink != 1 or details.st_size > MAX_JOURNAL_BYTES)
        digest, reason = None, "unsafe, unowned, linked or oversized journal entry"
        if not blocked:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            with os.fdopen(fd, "rb") as source:
                opened = os.fstat(source.fileno())
                if (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino):
                    raise store.RunnerRefused("journal entry changed while preparing recovery")
                content = source.read(MAX_JOURNAL_BYTES + 1)
                if len(content) > MAX_JOURNAL_BYTES:
                    raise store.RunnerRefused("journal entry grew beyond the recovery read limit")
                digest = hashlib.sha256(content).hexdigest()
                try:
                    recheck = path.lstat()
                except FileNotFoundError:
                    continue
                if _metadata(os.fstat(source.fileno())) != _metadata(opened) or _metadata(recheck) != metadata:
                    raise store.RunnerRefused("journal entry changed while preparing recovery")
            if re.fullmatch(r"[a-f0-9]{32}\.lock", path.name):
                continue
            if re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
                try:
                    records.append(journal.read(path))
                    continue
                except store.RunnerRefused as error:
                    # `journal.read` opens the name again; gone by then is C-50 too.
                    if not os.path.lexists(path):
                        continue
                    reason = str(error)
            else:
                reason = "interrupted journal write" if path.suffix == ".partial" else "unknown journal entry"
        issue = {"entry": path.name, "sha256": digest, "metadata": metadata,
                 "reason": reason, "blocked": reason if blocked else None}
        issues.append({**issue, "fingerprint": store.revision(issue)})
    return records, issues


def _entry(connection, ident, external):
    snapshot = store.recovery_snapshot(connection, ident, assignment=external["assignment"] if external else None)
    current, assignment = snapshot["run"], snapshot["assignment"]
    operation, reason = None, None
    if current is not None and external is None:
        operation = "journal-from-database"
    elif current is None or external["journal_version"] > current["journal_version"]:
        operation = "database-from-journal"
        if assignment is None:
            reason = "assignment is absent; restore a database backup containing its original work"
    elif external["journal_version"] == current["journal_version"] and journal_differs(external, current):
        reason = "same-version conflict requires a compatible database backup; neither side is authoritative"
    if operation is None and reason is None:
        return None
    document = {"run": ident, "operation": operation, "snapshot": snapshot, "journal": external, "blocked": reason}
    return {**document, "fingerprint": store.revision(document)}


def plan(config):
    connection = connect(config.database, write=False)
    try:
        valid, issues = _journal_view(config.database)
        records = {record["id"]: record for record in valid}
        # An issue withholds only the run it names; other runs keep their entries beside it.
        named = {match[1] for issue in issues if (match := re.fullmatch(r"([a-f0-9]{32})\.(json|lock|partial)", issue["entry"]))}
        identities = sorted((set(records) | {run["id"] for run in store.active_runs(connection)}) - named)
        entries = [entry for ident in identities if (entry := _entry(connection, ident, records.get(ident)))]
        return {"dry_run": True, "database": str(config.database), "entries": entries, "journal_issues": issues,
                "restore_pending": store.restoration_pending(connection) or any((config.database.parent / name).exists()
                    for name in ("runner-restore-intent.json", "publication-restore-intent.json"))}
    finally:
        connection.close()


def _paths(config, record, assignment):
    expected_work = config.work / str(assignment["item"]) / f"{record['assignment']}-{record['run']}-{record['id']}"
    expected_retained = config.retention / str(record["assignment"]) / str(record["run"]) / "clone"
    for name, expected, root in (("work_path", expected_work, config.work), ("retained_path", expected_retained, config.retention)):
        actual = Path(record[name])
        if actual.absolute() != expected.absolute() or actual.is_symlink() or not actual.resolve().is_relative_to(root.resolve()):
            raise store.RunnerRefused("recovery paths differ from configured attempt ownership")
    if processes.survivors(record) or processes.survivors({**record, "work_path": str(expected_retained)}):
        raise store.RunnerRefused("owned or foreign processes still hold this run; recovery does not signal them")


def _receipt(config, entry):
    root = config.database.parent / "runner-reconciliation"
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / (entry["fingerprint"] + ".json")
    content = json.dumps(entry, sort_keys=True, indent=2).encode() + b"\n"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise store.RunnerRefused("recovery evidence conflicts with an existing receipt")
        return path
    with path.open("xb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    journal.fsync_directory(root)
    return path


def reconcile(config, ident, *, fingerprint):
    # This lock excludes daemon recovery and dispatch, including a second process.
    with journal.lock(config.database.parent / "runner.lock", blocking=False):
        document = plan(config)
        if document["journal_issues"]:
            raise store.RunnerRefused("journal entries require explicit quarantine before ownership reconciliation")
        if document["restore_pending"]:
            raise store.RunnerRefused("finish database restore reimport before reconciling runner ownership")
        entry = next((entry for entry in document["entries"] if entry["run"] == ident), None)
        if entry is None or entry["fingerprint"] != fingerprint:
            raise store.RunnerRefused("recovery selection changed; prepare a new plan")
        if entry["blocked"]:
            raise store.RunnerRefused(entry["blocked"])
        record = entry["journal"] or entry["snapshot"]["run"]
        _paths(config, record, entry["snapshot"]["assignment"])
        evidence = _receipt(config, entry)
        connection = connect(config.database)
        try:
            if entry["operation"] == "database-from-journal":
                record = store.recover_from_journal(connection, record, expected_snapshot=store.revision(entry["snapshot"]))
            elif store.revision(store.recovery_snapshot(connection, ident)) != store.revision(entry["snapshot"]):
                raise store.RunnerRefused("recovery selection changed; prepare a new plan")
            journal.persist(config.database, record)
            return {"ok": True, "run": ident, "operation": entry["operation"], "evidence": str(evidence),
                    "next": "restart the runner to retain the owned clone; review the item before requeuing"}
        finally:
            connection.close()


def _enter_locks(stack, locks):
    for path in locks:
        stack.enter_context(journal.lock(path, blocking=False, noun="recovery"))


def quarantine(config, entry, *, fingerprint):
    """Move one explicitly selected invalid entry, retaining all bytes and mode."""
    if not entry or entry in (".", "..") or Path(entry).name != entry or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise store.RunnerRefused("quarantine needs one journal entry name and its current fingerprint")
    root = journal.directory(config.database)
    _private_directory(root)
    locks = [config.database.parent / "runner.lock"]
    named_run = re.fullmatch(r"([a-f0-9]{32})\.(json|partial)", entry)
    if named_run:
        locks.append(root / (named_run[1] + ".lock"))
    with ExitStack() as stack:
        _enter_locks(stack, locks)
        _, issues = _journal_view(config.database)
        issue = next((row for row in issues if row["entry"] == entry), None)
        if issue is None or issue["fingerprint"] != fingerprint or issue["blocked"]:
            raise store.RunnerRefused("journal selection changed, is healthy, or has unsafe authority; prepare a new recovery plan")
        archive = config.database.parent / "runner-recovery-evidence"
        archive.mkdir(mode=0o700, exist_ok=True)
        _private_directory(archive)
        destination = Path(tempfile.mkdtemp(prefix="quarantine-" + fingerprint + "-", dir=archive))
        receipt = destination / "receipt.json"
        with receipt.open("x") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump({"source": str(root / entry), "issue": issue}, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        # Both daemon and per-run writer locks remain held through publication.
        # Unknown files require the operator to stop their external writer first.
        _, current = _journal_view(config.database)
        if issue not in current:
            raise store.RunnerRefused("journal entry changed before quarantine; evidence directory retained")
        evidence = destination / "entry"
        os.rename(root / entry, evidence)
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != issue["sha256"]:
            raise store.RunnerRefused("quarantined evidence changed; preserve its directory for inspection")
        journal.fsync_directory(destination)
        journal.fsync_directory(archive)
        journal.fsync_directory(root)
        return {"ok": True, "entry": entry, "evidence": str(evidence), "receipt": str(receipt),
                "next": "run recovery-plan again; quarantine does not resume or reconstruct any run"}


def _restore_links(archive, entry, details):
    """Every restore evidence copy of entry that is the same inode as the live file."""
    matches = []
    for restore in sorted(archive.iterdir()):
        material = restore / "runner-journal"
        if not restore.name.startswith("restore-"):
            continue
        for path in (restore, material):
            if not path.exists() and not path.is_symlink():
                break
            owned = path.lstat()
            if not stat.S_ISDIR(owned.st_mode) or owned.st_uid != os.getuid():
                raise store.RunnerRefused(f"restore evidence directory is linked or unowned: {path}")
        else:
            try:
                found = (material / entry).lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(found.st_mode) and (found.st_dev, found.st_ino) == (details.st_dev, details.st_ino):
                matches.append(material / entry)
    return matches


def unlink_restore_link(config, entry, *, fingerprint):
    """Give one restore-linked journal file back a single link, keeping the evidence bytes.

    A full restore before sd:779 linked each installed journal file to its
    restore material, and the second link makes the entry a blocked issue.
    Only that exact shape is changed: the evidence name gets its own fsynced
    copy of the same bytes, so the live file keeps its inode and bytes.
    """
    named_run = re.fullmatch(r"([a-f0-9]{32})\.json", entry or "")
    if not named_run or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise store.RunnerRefused("restore unlink needs one run journal name and its current fingerprint")
    root = journal.directory(config.database)
    _private_directory(root)
    state = config.database.parent
    with ExitStack() as stack:
        _enter_locks(stack, [state / "runner.lock", root / (named_run[1] + ".lock")])
        if journal.restore_pending(config.database) or (state / "runner-restore-intent.json").is_symlink():
            raise store.RunnerRefused("finish the pending runner journal restore before changing its links")
        _, issues = _journal_view(config.database)
        issue = next((row for row in issues if row["entry"] == entry), None)
        if issue is None or issue["fingerprint"] != fingerprint:
            raise store.RunnerRefused("journal selection changed or is healthy; prepare a new recovery plan")
        live = root / entry
        details = live.lstat()
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_nlink != 2
                or details.st_size > MAX_JOURNAL_BYTES or _metadata(details) != issue["metadata"]):
            raise store.RunnerRefused("restore unlink needs an owned journal file with exactly one other link")
        archive = state / "runner-recovery-evidence"
        if not archive.exists() and not archive.is_symlink():
            raise store.RunnerRefused("the other link is not a restore evidence copy; the entry is left unchanged")
        _private_directory(archive)
        # With exactly two links, at most one evidence name can share the inode.
        matches = _restore_links(archive, entry, details)
        if not matches:
            raise store.RunnerRefused("the other link is not a restore evidence copy; the entry is left unchanged")
        evidence = matches[0]
        with live.open("rb") as source:
            content = source.read(MAX_JOURNAL_BYTES + 1)
        # A crash before the replace leaves this copy; the next attempt removes it.
        copy = evidence.parent.parent / f"unlink-{entry}"
        if copy.exists() or copy.is_symlink():
            leftover = copy.lstat()
            if not stat.S_ISREG(leftover.st_mode) or leftover.st_nlink != 1:
                raise store.RunnerRefused(f"restore unlink copy is linked or invalid: {copy}")
            copy.unlink()
        fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), stat.S_IMODE(details.st_mode))
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        journal.fsync_directory(copy.parent)
        current, witness = live.lstat(), evidence.lstat()
        if (_metadata(current) != issue["metadata"] or current.st_nlink != 2
                or (witness.st_dev, witness.st_ino) != (details.st_dev, details.st_ino)):
            raise store.RunnerRefused("journal entry changed before unlink; its copy is retained for inspection")
        os.replace(copy, evidence)
        journal.fsync_directory(evidence.parent)
        journal.fsync_directory(copy.parent)
        after = live.lstat()
        if after.st_nlink != 1 or (after.st_dev, after.st_ino) != (details.st_dev, details.st_ino):
            raise store.RunnerRefused("journal entry still has another link; preserve the evidence directory for inspection")
        return {"ok": True, "entry": entry, "evidence": str(evidence),
                "next": "run recovery-plan again; unlink does not change journal bytes or any run"}
