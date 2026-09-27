#!/usr/bin/env python3
"""Prove the off-machine copy can be restored, and say so out loud when it cannot.

sd:1107 records what an unverified backup is worth. Time Machine had no
destination, `backup-verify-monthly` was never defined let alone loaded, and
the gap was found by losing six files of uncommitted work rather than by any
check reporting it. So this is not a file-exists test. It pulls the newest
database snapshot back off the share and hands it to `sd_db.backup`'s own
restore validator -- the one `sd-db.sh restore` runs, checking integrity, the
schema version, the table set, every table's columns and the foreign keys --
then compares the counts it returns against the manifest the snapshot was
written with, and re-hashes every companion file the manifest names. It does
NOT keep a second, weaker contract of its own: a verifier that passes what
restore would refuse is the failure this row is about, one layer down. It runs
under the interpreter that can import sd_db, and says so as a failure when it
cannot.

The database snapshot is the only thing on the share it checks. The repos
mirror that used to be here, with its sampled byte comparison, was retired on
2026-09-23: the repo fleet now goes to a USB disk attached to this Mac, which
the `repos-mirror` job writes and nothing here reads.

Every check runs; every failure is printed; the exit code is 1 if any failed.
An unmounted share is a failure, not a quiet pass: it is exactly what this row
was created not to miss again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import system_tools_config  # noqa: E402

#: This tool's settings file, `<config>/mirror-sync/.env`, outside the checkout.
ENV_FILE = system_tools_config.config_dir("mirror-sync") / ".env"


def _load_config_env() -> None:
    """Read KEY=VALUE lines from `<config>/mirror-sync/.env`, if present.

    An exported variable wins over the file, so a job or a test can still
    override a value without editing it.
    """
    for key, value in system_tools_config.read_env("mirror-sync").items():
        os.environ.setdefault(key, value)


_load_config_env()

#: The share root the offsite copies live under. Overridable so the tests can
#: run against a fixture, and so a second machine can name its own mount.
DEFAULT_ROOT = Path(os.environ.get("OFFSITE_VERIFY_ROOT", "/Volumes/Offsite/Backup"))

# A filesystem that is served by another machine. A local disk -- internal,
# USB, a disk image -- is none of these, and it dies in the fire, theft or
# spilled drink that the off-machine copy exists to survive.
NETWORK_FILESYSTEMS = frozenset({"smbfs", "cifs", "nfs", "afpfs", "webdav", "fuse.sshfs"})

# The server and share `sd-db-backup` copies to, as `<server>/<share>`: the
# NAS this account backs up to. A network filesystem alone says only that some
# other machine serves the bytes; naming the share says which one, so a
# different NAS mounted at the same path does not pass for it. It is a local
# value, so it comes from OFFSITE_VERIFY_EXPECTED_SHARE (exported, or in
# `<config>/mirror-sync/.env`); there is no built-in default.
EXPECTED_SHARE_VARIABLE = "OFFSITE_VERIFY_EXPECTED_SHARE"
DEFAULT_EXPECTED_SHARE = os.environ.get(EXPECTED_SHARE_VARIABLE)

#: `sd-db-backup` writes one directory per run, named for the day with a `.N`
#: suffix when a day has more than one. The suffix is not zero padded, so
#: lexicographic order is NOT chronological: it puts `.9` after `.10`, and the
#: verifier would then check the ninth snapshot of the day and call it newest.
#: `_snapshot_sort_key` reads these groups and orders by the number instead.
SNAPSHOT_NAME = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?:\.(?P<serial>\d+))?$")

#: How stale the snapshot may be before it stops counting as a backup. It is
#: nightly, so 36 hours tolerates one missed run without crying wolf on a
#: machine that was asleep.
DEFAULT_MAX_DB_AGE_HOURS = 36.0


class Report:
    """Collects every check's outcome so one run names every problem."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failures: list[str] = []

    def ok(self, message: str) -> None:
        self.lines.append(f"ok    {message}")

    def fail(self, message: str) -> None:
        self.lines.append(f"FAIL  {message}")
        self.failures.append(message)

    def check(self, condition: bool, message: str) -> bool:
        (self.ok if condition else self.fail)(message)
        return condition


def _age_hours(when: datetime, now: datetime) -> float:
    return (now - when).total_seconds() / 3600.0


def _snapshot_sort_key(match: re.Match[str]) -> tuple[str, int]:
    """Order by the day, then by the run within it, as a number.

    The groups come from `SNAPSHOT_NAME`, so the name is parsed once. A bare
    date is the day's first run, which is suffix 0.
    """
    serial = match.group("serial")
    return (match.group("date"), int(serial) if serial is not None else 0)


def _newest_snapshot(backups: Path) -> Path | None:
    matches: list[re.Match[str]] = []
    try:
        for entry in os.scandir(backups):
            match = SNAPSHOT_NAME.match(entry.name)
            if match and entry.is_dir():
                matches.append(match)
    except OSError:
        return None
    if not matches:
        return None
    return backups / max(matches, key=_snapshot_sort_key).group(0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_database(root: Path, report: Report, *, max_age_hours: float, now: datetime) -> None:
    """Restore the newest snapshot off the share and compare it with its manifest."""
    try:
        share = root.resolve()
    except OSError:
        share = root
    backups = root / "sd-backups"
    if not report.check(backups.is_dir(), f"database snapshots directory exists: {backups}"):
        return
    snapshot = _newest_snapshot(backups)
    if not report.check(snapshot is not None, f"a dated snapshot exists under {backups}"):
        return
    assert snapshot is not None
    report.ok(f"newest snapshot: {snapshot.name}")
    # Every check below follows links, so a snapshot directory or a database file
    # pointed back at this machine restores, validates and hashes exactly like
    # a real backup. Losing the Mac would then destroy the snapshots this run
    # certified as being off it.
    if not report.check(
        _on_the_share(snapshot, share),
        f"the snapshot is on the share, not a link off it: {snapshot}",
    ):
        return

    database = snapshot / "sd.db"
    manifest_path = snapshot / "backup-manifest.json"
    if not report.check(database.is_file(), f"{snapshot.name}/sd.db is present"):
        return
    if not report.check(manifest_path.is_file(), f"{snapshot.name}/backup-manifest.json is present"):
        return
    for name, path in (("sd.db", database), ("backup-manifest.json", manifest_path)):
        if not report.check(
            _on_the_share(path, share),
            f"{snapshot.name}/{name} is on the share, not a link off it",
        ):
            return

    age = _age_hours(datetime.fromtimestamp(database.stat().st_mtime, UTC), now)
    report.check(
        age <= max_age_hours,
        f"newest snapshot is {age:.1f}h old (limit {max_age_hours:.0f}h)",
    )

    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as error:
        report.fail(f"manifest unreadable: {error}")
        return
    # Valid JSON is not a manifest. `sd-db-backup` writes one object, and a
    # list or a `null` here used to reach `.get` and end the run in a
    # traceback instead of a line saying what was wrong.
    if not report.check(
        isinstance(manifest, dict),
        f"manifest is a JSON object (found {type(manifest).__name__})",
    ):
        return
    counts = manifest.get("counts")
    if not report.check(
        isinstance(counts, dict) and bool(counts),
        "manifest carries per-table row counts",
    ):
        return
    # The writer records `count(*)` per table, so anything but a nonnegative
    # integer is damage. `bool` is excluded because it is an `int` to Python
    # and `true` would otherwise compare equal to a table of one row.
    malformed = sorted(
        table for table, value in counts.items()
        if type(value) is not int or value < 0
    )
    if not report.check(
        not malformed,
        "manifest row counts are nonnegative integers"
        + (f" (not for: {', '.join(malformed)})" if malformed else ""),
    ):
        return

    # The restore. The bytes come back across the share into a local file, and
    # everything after this point reads that copy -- never the original.
    work = Path(tempfile.mkdtemp(prefix="offsite-verify-"))
    try:
        restored = work / "restored.db"
        try:
            shutil.copyfile(database, restored)
        except OSError as error:
            report.fail(f"snapshot could not be copied back off the share: {error}")
            return
        report.ok(f"restored {database.stat().st_size} bytes to a local copy")

        # THE restore validation, not a second one. `sd-db.sh restore` refuses
        # a candidate this rejects, and an earlier version of this file kept
        # its own weaker contract: integrity_check, foreign_key_check and row
        # counts all pass on a database whose table set restore will not
        # accept, so it would have reported a good backup for a snapshot that
        # cannot be restored. Import the committed validator instead, and if
        # it cannot be imported say so as a failure -- a verifier that quietly
        # falls back to a weaker check is the shape of this whole row.
        try:
            from sd_db.backup import check_restorable
        except ImportError as error:
            report.fail(
                f"the sd_db restore validator could not be imported ({error});"
                " run this under the interpreter sd-db.sh uses"
            )
            return
        # The library opens the file, not this script: one store, one caller,
        # and the validator stays the committed one. Everything restore
        # refuses comes back as a refusal in its own words -- a failed
        # integrity_check, an unsupported schema version, an incompatible
        # table set or columns, a file that is not a database at all, and the
        # foreign key violations that make a snapshot forensic rather than
        # restorable.
        check = check_restorable(restored)
        if not check.accepted:
            report.fail(f"restore would refuse this snapshot: {check.refusal}")
            return
        actual_counts = check.counts
        report.ok(
            f"the sd_db restore validator accepts the restored copy (schema v{check.version})"
        )

        # Both directions. The writer counts every table the snapshot holds
        # (`_counts` in sd_db.backup, the same function the validator's
        # counts come from), so the two key sets are equal on a sound
        # manifest. Walking only the manifest's tables let one that dropped
        # a table report `1/1` and pass while the copy held more.
        compared = 0
        total = 0
        tables = sorted(set(counts) | set(actual_counts))
        for table in tables:
            if table not in actual_counts:
                report.fail(f"table {table} is in the manifest but not in the restored copy")
                continue
            if table not in counts:
                report.fail(
                    f"table {table} is in the restored copy ({actual_counts[table]} rows)"
                    " but the manifest does not count it"
                )
                continue
            expected = counts[table]
            if actual_counts[table] != expected:
                report.fail(
                    f"table {table}: {actual_counts[table]} rows restored,"
                    f" manifest says {expected}"
                )
                continue
            compared += 1
            total += actual_counts[table]
        report.check(
            compared == len(tables),
            f"{compared}/{len(tables)} tables match the manifest ({total} rows)",
        )
        report.check(total > 0, f"the restored copy holds rows ({total})")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    _prove_restore(snapshot, report)

    # A database alone does not restore this machine's state. The manifest
    # names the companion files the snapshot was taken with -- providers.yaml,
    # commands.yaml, the execution receipts, the runner journal -- each with
    # the SHA256 it had when it was written. Checking them here is what turns
    # "the file is present" into "the bytes came back unchanged".
    _verify_companions(snapshot, manifest, report)


def _prove_restore(snapshot: Path, report: Report) -> None:
    """Run the real restore into a throwaway home, not a selection of its checks.

    `_check_restore` covers the database. It does not cover the companion
    journals: restore also reconciles the runner journal, the publication
    claims and the execution receipts, and refuses -- for one example -- a
    runner journal holding a `.partial` file, which no hash of unchanged bytes
    can notice. Picking internal checks rebuilt the second contract this file
    was just taught not to keep, so run the whole verb.

    It is safe to run from a cron job. The snapshot is copied off the share
    first, so nothing writes to the NAS; `home` is a throwaway directory, so
    the restored database, configuration and journals land there and the live
    store is never touched; and `control_gate` is a file lock under that same
    throwaway home, not a service control.
    """
    try:
        from sd_db.backup import restore
        from sd_db.errors import BackupError
    except ImportError as error:
        report.fail(f"the sd_db restore verb could not be imported ({error})")
        return
    work = Path(tempfile.mkdtemp(prefix="offsite-restore-"))
    try:
        local = work / "snapshot"
        try:
            # symlinks=True, because copying is supposed to move the snapshot,
            # not transform it. The default dereferences, so a linked journal
            # entry or a linked providers.yaml would arrive as an ordinary
            # file and pass a restore that refuses the original -- the same
            # false pass, one layer further in. sd_db's own snapshot code
            # copies with symlinks=True for this reason.
            shutil.copytree(snapshot, local, symlinks=True)
        except OSError as error:
            report.fail(f"snapshot directory could not be copied off the share: {error}")
            return
        home = work / "home"
        home.mkdir()
        try:
            restored = restore(local, home=home)
        except Exception as error:  # noqa: BLE001 - any refusal is the answer
            kind = "restore refused this snapshot" if isinstance(error, BackupError) else "restore failed"
            report.fail(f"{kind}: {error}")
            return
        report.check(
            Path(restored).is_file() and Path(restored).stat().st_size > 0,
            f"a full restore of {snapshot.name} into a throwaway home succeeded",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


#: A SHA256 as `sd-db-backup` writes it: lowercase hex, 64 characters.
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

#: What Finder leaves behind when someone browses the share. The writer never
#: records them, and they carry nothing restore reads, so finding one is not
#: a snapshot that differs from its manifest.
FINDER_METADATA = re.compile(r"^(?:\.DS_Store|\._.*)$")


def _snapshot_inventory(snapshot: Path) -> tuple[dict[str, bool], list[str]]:
    """What the snapshot directory holds, the way the writer inventories it.

    `sd-db-backup` records every entry under the snapshot -- each file with
    its hash, each directory as `null` -- except the manifest itself, and it
    refuses to record a link. Walking the same way here is what lets the
    manifest be compared with the directory rather than only trusted about
    it. Returns each name mapped to whether it is a directory, and the links
    found, which the writer never produces.
    """
    inventory: dict[str, bool] = {}
    links: list[str] = []
    manifest = snapshot / "backup-manifest.json"
    for path in sorted(snapshot.rglob("*")):
        if path == manifest or FINDER_METADATA.match(path.name):
            continue
        name = path.relative_to(snapshot).as_posix()
        try:
            details = path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(details.st_mode):
            links.append(name)
            continue
        inventory[name] = stat.S_ISDIR(details.st_mode)
    return inventory, links


def _verify_companions(snapshot: Path, manifest: dict, report: Report) -> None:
    """Hold the snapshot to its manifest's inventory, in both directions.

    The manifest lists every entry the writer put in the snapshot. Hashing
    only the names it lists passed a manifest that had lost `sd.db` or
    `commands.yaml`, so the directory is walked as well: an entry the
    manifest does not name, or a manifest entry that is not a hash or a
    directory marker, is a failure of its own.
    """
    entries = manifest.get("entries")
    if not report.check(
        isinstance(entries, dict) and bool(entries),
        "manifest names the companion files",
    ):
        return
    malformed = sorted(
        name for name, expected in entries.items()
        if not (expected is None or (isinstance(expected, str) and SHA256_HEX.match(expected)))
    )
    for name in malformed:
        report.fail(f"the manifest entry for {name!r} is neither a SHA256 nor a directory marker")
    report.check(
        isinstance(entries.get("sd.db"), str),
        "the manifest records the database, sd.db, with a hash",
    )

    missing: list[str] = []
    drifted: list[str] = []
    escaped: list[str] = []
    checked = 0
    try:
        inside = snapshot.resolve()
    except OSError:
        inside = snapshot
    try:
        on_disk, links = _snapshot_inventory(snapshot)
    except OSError as error:
        report.fail(f"the snapshot's contents could not be listed: {error}")
        on_disk, links = {}, []
    for name in links:
        report.fail(f"{name} is a link, which the snapshot writer never records")
    unlisted = sorted(name for name in on_disk if name not in entries)
    for name in unlisted:
        kind = "directory" if on_disk[name] else "file"
        report.fail(f"{name} is a {kind} in the snapshot the manifest does not name")
    for name, expected in sorted(entries.items()):
        if name in malformed:
            continue
        path = snapshot / name
        if expected is None:
            # A directory entry. Its contents are listed separately; what is
            # left to check is that it is there, and is a directory.
            if not _on_the_share(path, inside):
                escaped.append(name)
            elif not (path.is_dir() and not path.is_symlink()):
                missing.append(name)
            continue
        if not _on_the_share(path, inside):
            # The manifest is part of the snapshot, so it is as trustworthy as
            # the snapshot is. An absolute name, a `..`, or a link would have
            # this hash a file somewhere else on the machine and report the
            # companion as present when the snapshot does not hold it.
            escaped.append(name)
            continue
        if not path.is_file():
            missing.append(name)
            continue
        try:
            if _sha256(path) != expected:
                drifted.append(name)
                continue
        except OSError as error:
            report.fail(f"{name} could not be read back off the share: {error}")
            continue
        checked += 1
    for name in missing:
        report.fail(f"{name} is in the manifest but missing from the snapshot")
    for name in drifted:
        report.fail(f"{name} came back with different bytes than the manifest recorded")
    for name in escaped:
        report.fail(f"the manifest names {name!r}, which is not inside the snapshot")
    report.check(checked > 0, f"{checked} companion file(s) match their manifest SHA256")


def _on_the_share(path: Path, share: Path) -> bool:
    """Does this path, links and all, end up inside the backup root?"""
    try:
        return path.resolve().is_relative_to(share)
    except OSError:
        return False


def _mount_point(path: Path) -> Path:
    """The mount the path is served from, which is the path's own if it is one."""
    point = path.resolve()
    while point != point.parent and not os.path.ismount(point):
        point = point.parent
    return point


def _mount_entries(output: str) -> dict[str, tuple[str, str]]:
    """Map each mount point to the device serving it and its filesystem type.

    `mount` writes `DEVICE on POINT (TYPE, option...)` on macOS and
    `DEVICE on POINT type TYPE (option...)` on Linux. Both forms are read
    here so the check does not silently pass on a machine it cannot parse.
    """
    entries: dict[str, tuple[str, str]] = {}
    for line in output.splitlines():
        device, separator, rest = line.partition(" on ")
        if not separator:
            continue
        point, _, trailing = rest.partition(" (")
        kind = trailing.split(",")[0].strip().rstrip(")").strip()
        point, linux, linux_kind = point.partition(" type ")
        if linux:
            kind = linux_kind.strip()
        entries[point.strip()] = (device.strip(), kind)
    return entries


def _remote(device: str) -> tuple[str, str] | None:
    """The server and share a mount device names, or None if it names neither.

    `mount` writes an SMB or AFP device as `//user;auth@server/share` and an
    NFS one as `server:/export`. Both are split here rather than searched,
    because a search matches `//user@192.0.2.100/Offsite` and
    `.../Offsite-old` for the server and share this job expects, and
    either one is a different machine's disk wearing the right text.
    """
    if device.startswith("//"):
        authority, _, share = device[2:].partition("/")
        server = authority.rpartition("@")[2]
        return server.lower(), share.strip("/").lower()
    server, colon, share = device.partition(":")
    if colon:
        return server.lower(), share.strip("/").lower()
    return None


def _off_machine(root: Path, expected: str, report: Report,
                 *, table: str | None = None) -> bool:
    """Prove the root is a share on another machine, not local storage.

    Containment proves the copies are under the root, and a different st_dev
    proves only a different filesystem: an attached USB disk is one too, and
    it is lost with the Mac in every event this backup exists for. The mount
    table is the one place that says what the root really is, so ask it for
    the filesystem type and for the server the share came from.

    `table` stands in for that command's output, so a test can state which
    kind of disk serves the root without mounting one.
    """
    point = _mount_point(root)
    if table is None:
        command = shutil.which("mount") or "/sbin/mount"
        try:
            table = subprocess.run([command], capture_output=True, text=True,
                                   check=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError) as error:
            report.fail(f"the mount table could not be read, so {root} cannot be shown"
                        f" to be off this machine: {error}")
            return False

    entry = _mount_entries(table).get(str(point))
    if entry is None:
        report.fail(f"no mount serves {root}: its mount point is {point}, which the"
                    " mount table does not list, so the backup root is this machine's"
                    " own storage")
        return False

    device, kind = entry
    if kind not in NETWORK_FILESYSTEMS:
        report.fail(f"the backup root is on a {kind} filesystem served by {device},"
                    " which is storage attached to this machine; it is lost with the"
                    " machine, so it is not an off-machine backup")
        return False
    if expected:
        wanted = _remote("//" + expected.lstrip("/"))
        served = _remote(device)
        if served != wanted:
            report.fail(f"the backup root is served by {device}, not by the expected"
                        f" share {expected}")
            return False

    report.ok(f"the backup root is a {kind} share served by {device}")
    return True


def _writable_destination(destination: Path, root: Path, report: Report) -> bool:
    """A destination has to be on the share itself, not a link off it.

    The root being the NAS says nothing about where `Backup/sd-backups`
    points. A symlink there sends the night's writes to
    local storage while the preflight passes, and every later check follows
    the same link and agrees. A destination that does not exist yet is judged
    by the nearest parent that does, because that is the directory the writer
    will create it in.
    """
    share = root.resolve()
    existing = destination
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent

    if not _on_the_share(existing, share):
        report.fail(f"{destination} is not written inside the backup root: it resolves"
                    f" to {existing.resolve()}, which is outside {share}")
        return False
    if _mount_point(existing) != _mount_point(share):
        report.fail(f"{destination} is served by {_mount_point(existing)}, a different"
                    f" mount from the backup root's {_mount_point(share)}, so writing"
                    " there does not write to the share")
        return False

    report.ok(f"the write destination is on the share: {destination}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help=f"off-machine backup root (default {DEFAULT_ROOT})")
    parser.add_argument("--max-db-age-hours", type=float, default=DEFAULT_MAX_DB_AGE_HOURS)
    parser.add_argument("--allow-local-root", action="store_true",
                        help="accept a backup root on this machine's own filesystem"
                             " (for tests; the job never passes it)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="check only that the root is the expected off-machine share"
                             " and exit; this is what a writer runs before it writes")
    parser.add_argument("--destination", type=Path, action="append", default=[],
                        help="a directory the writer is about to write into; it has to be"
                             " on the share rather than linked off it (repeatable)")
    parser.add_argument("--expected-share", default=DEFAULT_EXPECTED_SHARE,
                        help="the server and share the backup root must be mounted from"
                             f" (default: ${EXPECTED_SHARE_VARIABLE}; pass an empty value to"
                             " accept any network share)")
    arguments = parser.parse_args(argv)


    now = datetime.now(UTC)
    report = Report()
    root = arguments.root

    # An unmounted share looks like a missing directory, and reporting that is
    # the whole point of this job. Nothing else can be checked without it.
    if not root.is_dir():
        print(f"FAIL  off-machine backup root is not there: {root}", file=sys.stderr)
        print("      the share is unmounted, renamed, or the NAS is unreachable", file=sys.stderr)
        return 1

    report.ok(f"off-machine backup root is mounted: {root}")

    # Containment proves the backups are under the root. It cannot prove the
    # root is off this machine: a `Backup` linked at local storage resolves to
    # a real directory, and every check below then passes against snapshots
    # that would die with the Mac. Nor does a different filesystem
    # prove it, because an attached USB disk is a different filesystem and is
    # lost with the machine all the same. Only the mount table says which
    # machine serves the bytes, so the root must be a network share, and the
    # share the job expects. A test fixture is a local directory by
    # construction, which is what the flag is for; nothing in the job passes
    # it.
    # With no expected share configured, the network-filesystem half of the
    # check still runs, and the missing setting is itself a failure: a run
    # that cannot say which NAS it expects has not shown the copy left home.
    if not arguments.allow_local_root:
        off_machine = _off_machine(root, arguments.expected_share or "", report)
        if arguments.expected_share is None:
            report.fail(system_tools_config.missing(
                            EXPECTED_SHARE_VARIABLE, "mirror-sync", ".env",
                            "local-mirror-sync")
                        + f" Set it as {EXPECTED_SHARE_VARIABLE}=<server>/<share>"
                        " (--expected-share overrides both).")
            off_machine = False
        if not off_machine:
            for line in report.lines:
                print(line)
            print("-" * 40)
            print(f"offsite-verify: {len(report.failures)} check(s) FAILED", file=sys.stderr)
            for failure in report.failures:
                print(f"  {failure}", file=sys.stderr)
            return 1

    # A writer asks the same question this job asks, and has to ask it before
    # it writes rather than the next morning: a snapshot written onto local
    # storage has already put the backup somewhere the fire takes with the
    # Mac. One preflight answers for both, so the two sides cannot drift into
    # disagreeing about what off-machine means.
    if arguments.preflight_only:
        for destination in arguments.destination:
            _writable_destination(destination, root, report)
        for line in report.lines:
            print(line)
        if report.failures:
            print(f"offsite-verify: {len(report.failures)} check(s) FAILED", file=sys.stderr)
            for failure in report.failures:
                print(f"  {failure}", file=sys.stderr)
            return 1
        return 0
    verify_database(root, report, max_age_hours=arguments.max_db_age_hours, now=now)

    for line in report.lines:
        print(line)
    print("-" * 40)
    if report.failures:
        print(f"offsite-verify: {len(report.failures)} check(s) FAILED", file=sys.stderr)
        for failure in report.failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"offsite-verify: all {len(report.lines)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
