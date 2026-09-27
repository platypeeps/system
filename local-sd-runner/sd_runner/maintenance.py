"""Reviewable, exact-run maintenance. The one deletion removes exactly the clones a fingerprinted prune plan lists."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sd_db import reporting, workflow
from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_db import runner_retention as domain
from sd_db.database import transaction
from sd_db.writes import transition

from . import processes, restoration

#: The job and record name of the report `prune-apply` files (sd:770).
PRUNE_JOB = "runner-prune"
#: The job and record name of the record `retained-remove` files (sd:1780).
RETAINED_REMOVE_JOB = "runner-retained-remove"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _file(path):
    if path.is_symlink() or not path.is_file():
        raise store.RunnerRefused(f"maintenance expected a regular file: {path}")
    with path.open("rb") as stream:
        return {"sha256": hashlib.file_digest(stream, "sha256").hexdigest(), "bytes": path.stat().st_size}


def _clone(config, run):
    expected = config.retention / str(run["assignment"]) / str(run["run"]) / "clone"
    # Every component under the root, not only the clone: a linked
    # `<assignment>` that points elsewhere inside the root resolves inside it.
    if (Path(run["retained_path"]).absolute() != expected.absolute()
            or any(path.is_symlink() for path in (expected.parents[1], expected.parent, expected))):
        raise store.RunnerRefused("maintenance clone path does not match its owned assignment and run")
    root = config.retention.resolve()
    if not expected.resolve().is_relative_to(root):
        raise store.RunnerRefused("maintenance retained path escapes its configured volume")
    return expected


def _preserved(run, clone):
    result = {}
    for name, entry in json.loads(run["ignored_manifest"] or "{}").items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise store.RunnerRefused("preserved output manifest has an unsafe path")
        path = clone.parent / "ignored" / relative
        if "link" in entry:
            actual = {"link": os.readlink(path)} if path.is_symlink() else None
        else:
            actual = {"sha256": _file(path)["sha256"], "mode": stat.S_IMODE(path.stat().st_mode)}
        if actual != entry:
            raise store.RunnerRefused("ignored output is not independently preserved; prune refused")
        result[name] = actual
    return result


def plan_prune(config, connection, *, days=30) -> dict:
    if days < 30:
        raise store.RunnerRefused("retention cannot be shorter than 30 days")
    cutoff = datetime.now(UTC) - timedelta(days=days)
    entries, skipped = [], []
    for proof in domain.candidates(connection):
        run = proof["run"]
        clone = _clone(config, run)
        leftover = clone.parent / domain.PRUNING
        if os.path.lexists(leftover):
            # A removal that stopped part way is finished without reading
            # what is left: no age, no inventory, no preserved-output check.
            details = leftover.lstat()
            if not stat.S_ISDIR(details.st_mode):
                raise store.RunnerRefused(f"interrupted prune leftover is linked or not a directory: {leftover}")
            _one_device(leftover, clone.parent.lstat().st_dev)
            if os.path.lexists(clone):
                skipped.append({"run": run["id"], "reason": "retained clone and an interrupted prune leftover both exist"})
            elif holders := processes.survivors({**run, "work_path": str(leftover)}):
                skipped.append({"run": run["id"], "reason": "interrupted prune leftover has process holders", "holders": holders})
            else:
                entries.append({"run": run["id"], "operation": "finish", "proof": proof, "path": str(leftover),
                    "root_identity": [details.st_dev, details.st_ino], "bytes": None,
                    "reason": "finish a prune that stopped part way"})
            continue
        if not clone.exists():
            continue
        with ExitStack() as stack:
            try:
                stack.enter_context(journal.lock(clone.parent / ".archive.lock", blocking=False, noun="prune"))
            except store.RunnerRefused as error:
                # The removal path takes this lock `blocking=False`; a plan that
                # waits for it blocks behind the archive refresher or a restore,
                # and `apply_prune` replans through here (sd:1217). A held lock
                # is a skipped clone, not a plan that never answers. Only
                # contention skips: the cause names it, as `control_gate` reads
                # it, and every other refusal is still the plan's.
                if not isinstance(error.__cause__, BlockingIOError):
                    raise
                skipped.append({"run": run["id"], "reason": "archive lock is held by another holder"})
                continue
            receipt = clone.parent / "retention.json"
            if not receipt.is_file():
                skipped.append({"run": run["id"], "reason": "retention age has no receipt"})
                continue
            when = datetime.fromisoformat(json.loads(receipt.read_text())["retained_at"])
            if max(when, datetime.fromisoformat(run["released_at"])) > cutoff:
                continue
            holders = processes.survivors({**run, "work_path": str(clone)})
            if holders:
                skipped.append({"run": run["id"], "reason": "retained clone has process holders", "holders": holders})
                continue
            _one_device(clone, clone.parent.lstat().st_dev)
            manifest = restoration.inventory(clone)
            details = clone.stat()
            archive = clone.parent / "kept.tar"
            entries.append({"run": run["id"], "operation": "remove", "proof": proof, "path": str(clone),
                "root_identity": [details.st_dev, details.st_ino], "files": manifest,
                "bytes": sum(path.stat().st_size for path in clone.rglob("*") if path.is_file() and not path.is_symlink()),
                "retained_at": when.isoformat(), "retention_receipt": _file(receipt),
                "archive": {"path": str(archive), **_file(archive)} if archive.exists() else None,
                "preserved": _preserved(run, clone), "reason": f"released clone retained at least {days} days"})
    document = {"version": 1, "operation": "prune", "database": str(config.database.resolve()),
        "retention_root": str(config.retention.resolve()), "days": days, "entries": entries}
    return {**document, "fingerprint": _hash(document), "dry_run": True, "skipped": skipped}


def _refuse(error):
    raise error


def _entries(path, device, *, topdown=True):
    """Every entry under `path`, each checked to be on `device` before it is yielded.

    `os.walk` and `shutil.rmtree` cross a volume mounted inside the tree, and
    a mount inside a clone would lose its files. `os.fwalk` follows no link,
    so the caller checks `path` itself first.
    """
    for directory, directories, files, descriptor in os.fwalk(path, topdown=topdown, onerror=_refuse, follow_symlinks=False):
        for name in directories + files:
            details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            candidate = os.path.join(directory, name)
            if details.st_dev != device:
                raise store.RunnerRefused(f"retained clone crosses a mount point; unmount it and plan again: {candidate}")
            yield descriptor, name, candidate, details


def _one_device(path, device):
    """Refuse a tree that is a link or has an entry on another device than its run directory."""
    details = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(details.st_mode) or details.st_dev != device:
        raise store.RunnerRefused(f"retained clone crosses a mount point; unmount it and plan again: {path}")
    for _ in _entries(path, device):
        pass


def _thaw(path, device):
    """Clear the user immutable flag on every entry, never following a link or leaving the device."""
    for _, _, candidate, details in _entries(path, device):
        if details.st_flags & stat.UF_IMMUTABLE:
            os.chflags(candidate, details.st_flags & ~stat.UF_IMMUTABLE, follow_symlinks=False)


def _thaw_tree(path, device):
    """`_thaw`, and the flag on `path` itself, which `_thaw` leaves for its caller."""
    if (flags := os.lstat(path).st_flags) & stat.UF_IMMUTABLE:
        os.chflags(path, flags & ~stat.UF_IMMUTABLE, follow_symlinks=False)
    _thaw(path, device)


def _delete(path, device):
    """Remove a thawed tree bottom up, checking each entry's device before it goes."""
    for descriptor, name, _, details in _entries(path, device, topdown=False):
        if stat.S_ISDIR(details.st_mode):
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)
    os.rmdir(path)


def _remove(config, connection, entry):
    """Remove one planned clone, or finish one that stopped part way.

    The clone is renamed to `.pruning-clone` before a byte goes, so a stop
    inside the removal never leaves a partial tree under the name a restore
    reads as whole. The rename is the only change made to `clone`. Nothing is
    touched until the whole tree is known to sit on the run directory's
    device, and every flag clear and unlink checks that device again.
    """
    run = entry["proof"]["run"]
    clone = _clone(config, run)
    leftover = clone.parent / domain.PRUNING
    with ExitStack() as stack:
        for path in (config.database.parent / "runner-ending" / f"{run['id']}.lock", clone.parent / ".archive.lock"):
            stack.enter_context(journal.lock(path, blocking=False, noun="prune"))
        if store.run_state(connection, run["id"]) != run:
            raise store.RunnerRefused("run changed since the prune plan; prepare a new plan")
        target = clone if entry["operation"] == "remove" else leftover
        other = leftover if target == clone else clone
        if Path(entry["path"]) != target or os.path.lexists(other):
            raise store.RunnerRefused(f"retained clone changed since the prune plan: {target}")
        details = target.lstat()
        if not stat.S_ISDIR(details.st_mode) or [details.st_dev, details.st_ino] != entry["root_identity"]:
            raise store.RunnerRefused(f"retained clone changed since the prune plan: {target}")
        if holders := processes.survivors({**run, "work_path": str(target)}):
            raise store.RunnerRefused(f"retained clone has process holders: {holders}")
        device = clone.parent.lstat().st_dev
        _one_device(target, device)
        if target == clone:
            if details.st_flags & stat.UF_IMMUTABLE:
                os.chflags(clone, details.st_flags & ~stat.UF_IMMUTABLE, follow_symlinks=False)
            try:
                os.rename(clone, leftover)
            except OSError:
                # A clone left in place stays frozen: put back the flag just cleared.
                if details.st_flags & stat.UF_IMMUTABLE:
                    os.chflags(clone, details.st_flags, follow_symlinks=False)
                raise
            journal.fsync_directory(clone.parent)
        moved = leftover.lstat()
        if not stat.S_ISDIR(moved.st_mode) or (moved.st_dev, moved.st_ino) != (details.st_dev, details.st_ino):
            raise store.RunnerRefused(f"interrupted prune leftover changed during the removal: {leftover}")
        _thaw(leftover, device)
        _delete(leftover, device)
        journal.fsync_directory(clone.parent)


def _file_record(connection, *, job, run_id, text, noun, who, principal, program, session, now):
    """File one operator removal record, `done`, before a byte goes; return its id and actor.

    `prune-apply` and `retained-remove` both record through here, so their
    records share one store and one shape: a `cron-report` item whose
    `fields.record` is the job and whose `fields.report.actor` says who and
    what acted. A replay of the same `job:run_id` answers the record it filed.
    """
    stamped = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    actor = {"who": who, "principal": principal, "program": program,
             "pid": os.getpid(), "ppid": os.getppid(), "session": session}
    if len(text.encode()) > reporting.MAX_REPORT:
        raise store.RunnerRefused(f"{noun} record would pass the {reporting.MAX_REPORT}-byte report bound; "
                                  "nothing was filed or removed")
    with transaction(connection):
        existing = connection.execute("SELECT id FROM item WHERE source='cron-report' AND external_id=?",
                                      (f"{job}:{run_id}",)).fetchone()
        if existing:
            # The same removal was applied before and stopped: its record stands.
            return existing["id"], actor
        record = reporting.ingest(connection, job=job, run_id=run_id, started=stamped, ended=stamped,
                                  exit_code=0, text=text, source_path=program, attention=False,
                                  actor=actor, record=job)["item"]["id"]
        transition(connection, record, "done", who=who, reason=f"runner {noun} record")
    return record, actor


def apply_prune(config, connection, *, fingerprint, who, principal, program, session=None, days=30, now=None):
    """Remove exactly the clones `plan_prune` lists, after filing their record.

    The plan is made again and must carry `fingerprint`. Before the first
    removal one `runner-prune` report is filed through `reporting.ingest`,
    with the fingerprint as its run id and every entry's run, path, bytes and
    proof in its text, and moved to `done` in the same transaction, as a
    bulk acknowledge files its batch (sd:755 D3). Only the clone goes:
    `retention.json`, `ignored/`, `kept.tar` and `archives/` stay. An entry
    whose lock is held, whose clone changed, or that fails part way is
    skipped with its reason, at the path its tree is under now; a
    `.pruning-clone` left behind is the next plan's `finish` entry.

    The record lists what the plan selected and is `done` once filed. What
    was removed or skipped is the returned result, which the CLI prints; the
    record is not changed afterwards. A plan whose record would pass the
    report size bound is refused before anything is filed or removed.
    """
    if sys.platform != "darwin":
        raise store.RunnerRefused("retained clone removal requires macOS APFS")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise store.RunnerRefused("prune apply needs the fingerprint of a prune plan")
    who = workflow._text(who, "who")
    document = plan_prune(config, connection, days=days)
    if document["fingerprint"] != fingerprint:
        raise store.RunnerRefused("prune selection changed; prepare a new plan")
    result = {"version": 1, "operation": "prune-apply", "fingerprint": fingerprint, "dry_run": False,
              "record": None, "removed": [], "skipped": document["skipped"]}
    if not document["entries"]:
        return {**result, "ok": True}
    text = json.dumps({"fingerprint": fingerprint, "days": days, "retention_root": document["retention_root"],
                       "entries": [{name: entry[name] for name in ("run", "operation", "path", "bytes", "proof")}
                                   for entry in document["entries"]]}, sort_keys=True, indent=2, default=str) + "\n"
    result["record"] = _file_record(connection, job=PRUNE_JOB, run_id=fingerprint, text=text, noun="prune",
        who=who, principal=principal, program=program, session=session, now=now)[0]
    for entry in document["entries"]:
        try:
            _remove(config, connection, entry)
        except (OSError, subprocess.SubprocessError, store.RunnerRefused) as error:
            # A holder check that times out skips its entry, as archive refresh does.
            # A removal that stopped after the rename left its tree at `.pruning-clone`.
            moved = Path(entry["path"]).parent / domain.PRUNING
            path = str(moved) if not os.path.lexists(entry["path"]) and os.path.lexists(moved) else entry["path"]
            result["skipped"] = [*result["skipped"], {"run": entry["run"], "path": path, "reason": str(error)}]
        else:
            result["removed"].append({"run": entry["run"], "operation": entry["operation"],
                                      "path": entry["path"], "bytes": entry["bytes"]})
    return {**result, "ok": len(result["removed"]) == len(document["entries"])}


def _assignment_runs(connection, number):
    """Every run row of assignment `number`, refused unless there is one and all are released."""
    runs = [dict(row) for row in connection.execute(
        "SELECT * FROM runner_run WHERE assignment=? ORDER BY run", (number,))]
    if not runs:
        raise store.RunnerRefused(f"assignment {number} has no runner run; nothing proves its retained copy is released")
    if unreleased := [run["id"] for run in runs if not run["released_at"]]:
        raise store.RunnerRefused(f"assignment {number} has unreleased runs: {', '.join(unreleased)}")
    return runs


def _assignment_directory(config, number, runs):
    """`<retention_root>/<number>`, refused unless it is exactly that real directory and the runs' own."""
    root = config.retention.resolve()
    if not root.is_dir():
        raise store.RunnerRefused(f"retention root is not a directory; is its volume mounted? {root}")
    target = root / str(number)
    if target.is_symlink():
        raise store.RunnerRefused(f"retained copy is a symlink; refusing to follow it: {target}")
    if not os.path.lexists(target):
        raise store.RunnerRefused(f"assignment {number} has no retained copy: {target}")
    if target.resolve() != target:
        raise store.RunnerRefused(f"retained copy resolves outside its retention root: {target}")
    details = target.lstat()
    if not stat.S_ISDIR(details.st_mode):
        raise store.RunnerRefused(f"retained copy is not a directory: {target}")
    if details.st_dev != root.stat().st_dev:
        raise store.RunnerRefused(f"retained copy is on another device than its retention root: {target}")
    for run in runs:
        expected = config.retention / str(number) / str(run["run"]) / "clone"
        if Path(run["retained_path"]).absolute() != expected.absolute():
            raise store.RunnerRefused(f"run {run['id']} retains its clone outside {target}; check --config")
    return target


def _recovery_clear(config, number, runs):
    """Refuse while the recovery plan has any journal issue, a pending restore, or an entry for these runs."""
    from . import reconciliation
    document = reconciliation.plan(config)
    if document["journal_issues"]:
        names = ", ".join(issue["entry"] for issue in document["journal_issues"])
        raise store.RunnerRefused(f"the recovery plan has journal issues ({names}); resolve them with "
                                  "recovery-plan and recovery-quarantine first")
    if document["restore_pending"]:
        raise store.RunnerRefused("a database restore reimport is pending; finish it before removing retained copies")
    ids = {run["id"] for run in runs}
    for entry in document["entries"]:
        owners = {(entry.get("journal") or {}).get("assignment"),
                  ((entry.get("snapshot") or {}).get("run") or {}).get("assignment")}
        if entry["run"] in ids or number in owners:
            raise store.RunnerRefused(f"the recovery plan has an entry for run {entry['run']} of assignment {number}; "
                                      "reconcile it first")


def _attempts(target, runs):
    """The attempt directories `<target>/<run>` the validated released runs own, and that exist.

    Each is a real directory, never a link. A run whose directory is gone
    (a stop part way already removed it) owns nothing left to remove.
    """
    result = []
    for run in runs:
        directory = target / str(run["run"])
        if not os.path.lexists(directory):
            continue
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            raise store.RunnerRefused(f"retained attempt directory is linked or not a directory: {directory}")
        result.append(directory)
    return result


#: What `retained-remove --clone-only` removes in each attempt directory (sd:1793).
CLONE_NAMES = ("clone", domain.PRUNING)


def _drop(path, device):
    """Remove one tree, or one entry that is not a tree, never following a link."""
    _thaw_tree(path, device)
    if stat.S_ISDIR(os.lstat(path).st_mode):
        _delete(path, device)
    else:
        os.unlink(path)  # A link or a file by that name is not a tree; it is never followed.


def _remove_clone(directory, device):
    """Remove the clone of one validated attempt directory, and a `.pruning-clone` left before.

    The clone is renamed to `.pruning-clone` first, so a stop part way never
    leaves a clone a restore reads as whole: a restore refuses
    `.pruning-clone`, and the same command finishes it. Nothing else in
    `directory` is read or changed.
    """
    clone, leftover = directory / "clone", directory / domain.PRUNING
    if os.path.lexists(clone):
        if os.path.lexists(leftover):
            _drop(leftover, device)
        details = os.lstat(clone)
        if details.st_flags & stat.UF_IMMUTABLE:
            os.chflags(clone, details.st_flags & ~stat.UF_IMMUTABLE, follow_symlinks=False)
        try:
            os.rename(clone, leftover)
        except OSError:
            # A clone left in place stays frozen: put back the flag just cleared.
            if details.st_flags & stat.UF_IMMUTABLE:
                os.chflags(clone, details.st_flags, follow_symlinks=False)
            raise
        journal.fsync_directory(directory)
    if os.path.lexists(leftover):
        _drop(leftover, device)
        journal.fsync_directory(directory)


def _remove_attempt(directory, device):
    """Remove one validated attempt directory whole, its clone first (`_remove_clone`)."""
    _remove_clone(directory, device)
    _thaw_tree(directory, device)
    _delete(directory, device)


def _clone_only(directory, device):
    """`_remove_clone` inside an attempt directory whose own user immutable flag it clears and puts back."""
    flags = os.lstat(directory).st_flags
    if flags & stat.UF_IMMUTABLE:
        os.chflags(directory, flags & ~stat.UF_IMMUTABLE, follow_symlinks=False)
    try:
        _remove_clone(directory, device)
    finally:
        if flags & stat.UF_IMMUTABLE:
            os.chflags(directory, flags, follow_symlinks=False)


def _size(path, device):
    """The bytes of the regular files in one tree, or of one regular file."""
    details = os.lstat(path)
    if not stat.S_ISDIR(details.st_mode):
        return details.st_size if stat.S_ISREG(details.st_mode) else 0
    return sum(entry[3].st_size for entry in _entries(path, device) if stat.S_ISREG(entry[3].st_mode))


def remove_retained(config, connection, assignment, *, who, principal, program, session=None, now=None,
                    clone_only=False):
    """Remove one released assignment's retained attempts before prune's 30-day floor (sd:1780).

    The operator's early removal under `<retention_root>/<assignment>`, which
    replaces a raw `chflags -R nouchg` and `rm -rf`. It refuses unless `who`
    names someone, the assignment is a positive integer, every one of its
    run rows is released, the recovery plan is clean for it, and the target
    is exactly that real directory, on the root's device. Under each run's
    `runner-ending` lock and `.archive.lock` it checks the rows and the
    recovery plan again, and the process holders. It files one
    `runner-retained-remove` record, `done`, through the same path and shape
    as `prune-apply`, before a byte goes.

    It removes only the attempt directories `<assignment>/<run>` of the runs
    it validated, never the assignment directory as a whole: a requeue can
    claim a new run after the check, and `claim` takes none of these locks,
    so a new attempt's directory may appear beside them. Each clone is
    renamed to `.pruning-clone`, the user immutable flag is cleared entry by
    entry without following a link, and the tree is removed bottom up, each
    entry checked to be on the directory's device. The assignment directory
    goes only by `rmdir`, which succeeds only once it is empty; what is left
    in it is named in the result and kept. A stop part way leaves the
    directory in place, and the same command finishes it.

    `clone_only` (sd:1793) narrows the removal to each checked attempt's
    `clone` and a `.pruning-clone` an earlier stop left: `kept.tar`,
    `archives/`, `ignored/`, `retention.json`, the attempt directory and the
    assignment directory all stay, with their flags. It is the scope the raw
    `chflags -R nouchg` and `rm -rf` on one clone had. Every check above still
    runs first. The record and the result carry `scope` (`clone` or
    `attempt`), the paths removed, and the paths each attempt keeps. A run
    that finishes a stopped one removes `.pruning-clone`, so its record names
    that path and is filed as its own. With no clone left to remove it files
    nothing and removes nothing.
    """
    if sys.platform != "darwin":
        raise store.RunnerRefused("retained copy removal requires macOS APFS")
    if not isinstance(who, str) or not who.strip() or "\x00" in who:
        raise store.RunnerRefused("retained-remove needs --who naming the operator")
    who = who.strip()
    if isinstance(assignment, bool) or not re.fullmatch(r"[1-9][0-9]{0,17}", str(assignment or "")):
        raise store.RunnerRefused(f"retained-remove needs --assignment as a positive integer, not {assignment!r}")
    number = int(assignment)
    runs = _assignment_runs(connection, number)
    target = _assignment_directory(config, number, runs)
    _recovery_clear(config, number, runs)
    with ExitStack() as stack:
        attempts = _attempts(target, runs)
        for run in runs:
            stack.enter_context(journal.lock(config.database.parent / "runner-ending" / f"{run['id']}.lock",
                                             blocking=False, noun="retained-remove"))
        for directory in attempts:
            stack.enter_context(journal.lock(directory / ".archive.lock", blocking=False, noun="retained-remove"))
        if (_assignment_runs(connection, number) != runs or _assignment_directory(config, number, runs) != target
                or _attempts(target, runs) != attempts):
            raise store.RunnerRefused(f"assignment {number} changed while taking its locks; run the command again")
        _recovery_clear(config, number, runs)
        details = target.lstat()
        device = details.st_dev
        for run in runs:
            directory = target / str(run["run"])
            if directory in attempts and (holders := processes.survivors({**run, "work_path": str(directory)})):
                raise store.RunnerRefused(f"retained attempt has process holders: {holders}")
        for directory in attempts:
            _one_device(directory, device)
        if clone_only:
            scope = "clone"
            removing = [directory / name for directory in attempts for name in CLONE_NAMES
                        if os.path.lexists(directory / name)]
            keeping = [directory / name for directory in attempts for name in sorted(os.listdir(directory))
                       if name not in CLONE_NAMES]
        else:
            scope, removing, keeping = "attempt", attempts, []
        freed = sum(_size(path, device) for path in removing)
        runs_out = [run["id"] for run in runs]
        if not removing and clone_only:
            # Nothing is removed, so nothing is recorded: a replay after a finished run is a no-op.
            return {"ok": True, "operation": "retained-remove", "scope": scope, "assignment": number,
                    "path": str(target), "removed": [], "kept": [str(path) for path in keeping],
                    "left": sorted(os.listdir(target)), "bytes": 0, "runs": runs_out, "record": None, "actor": None}
        document = {"version": 1, "operation": "retained-remove", "scope": scope, "assignment": number,
                    "path": str(target), "retention_root": str(target.parent),
                    "root_identity": [details.st_dev, details.st_ino],
                    "attempts": [str(directory) for directory in attempts],
                    "remove": [str(path) for path in removing], "keep": [str(path) for path in keeping],
                    "bytes": freed,
                    "runs": [{name: run[name] for name in ("id", "run", "released_at", "retained_path")}
                             for run in runs]}
        text = json.dumps(document, sort_keys=True, indent=2) + "\n"
        record, actor = _file_record(connection, job=RETAINED_REMOVE_JOB, run_id=_hash(document), text=text,
            noun="retained-remove", who=who, principal=principal, program=program, session=session, now=now)
        if clone_only:
            # The assignment directory is never removed, so its flag is never touched.
            for directory in attempts:
                _clone_only(directory, device)
        else:
            # Only the directory's own flag: an entry no validated run owns is never changed.
            frozen = details.st_flags & stat.UF_IMMUTABLE
            if frozen:
                os.chflags(target, details.st_flags & ~stat.UF_IMMUTABLE, follow_symlinks=False)
            try:
                for directory in attempts:
                    _remove_attempt(directory, device)
                try:
                    os.rmdir(target)  # Succeeds only when nothing else is left in it.
                except OSError as error:
                    if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                        raise
                journal.fsync_directory(target.parent)
            finally:
                if frozen and os.path.lexists(target):
                    os.chflags(target, details.st_flags, follow_symlinks=False)
        left = sorted(os.listdir(target)) if os.path.lexists(target) else []
    return {"ok": True, "operation": "retained-remove", "scope": scope, "assignment": number, "path": str(target),
            "removed": [str(path) for path in removing], "kept": [str(path) for path in keeping], "left": left,
            "bytes": freed, "runs": runs_out, "record": record, "actor": actor}


def plan_discard(config, connection, assignment, *, expected_revision, expected_run):
    current = store.queue_state(connection, assignment)
    run = current["run"]
    if current["revision"] != expected_revision or not run or run["id"] != expected_run:
        raise store.RunnerRefused("discard selection changed; refresh before planning")
    if current["status"] != "ending" or run["end_step"] != "kept" or run["quarantine"]:
        raise store.RunnerRefused("discard requires an unquarantined kept attempt")
    clone = Path(run["work_path"])
    if clone.is_symlink() or not clone.is_dir() or processes.survivors(run):
        raise store.RunnerRefused("kept clone is absent, linked, or held by a process")
    document = {"version": 1, "operation": "discard", "proof": domain.evidence(connection, expected_run),
        "path": str(clone), "retained_path": str(_clone(config, run)), "files": restoration.inventory(clone),
        "bytes": sum(path.stat().st_size for path in clone.rglob("*") if path.is_file() and not path.is_symlink()),
        "reason": "retain the whole kept clone for 30 days, publish nothing, and release its branch lease"}
    return {**document, "fingerprint": _hash(document), "dry_run": True}
