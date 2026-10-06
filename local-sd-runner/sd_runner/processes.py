"""Process ownership uses PID plus kernel start identity, never PID alone."""

from __future__ import annotations

import ctypes
import os
import re
import signal
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from sd_db.runner import RunnerRefused

# How long one process-table read and one `lsof` may take. A read past its
# bound is a refused probe: the ending holds and the next tick asks again.
# Module names, not literals, so a test fixture on a loaded machine can raise
# them; `ps` has taken longer than ten seconds under a gate at load 125.
PS_SECONDS = 10
LSOF_SECONDS = 20


def start_identity(pid: int) -> str | None:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        size = ctypes.c_size_t()
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) or size.value < 16:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 4, buffer, ctypes.byref(size), None, 0):
            return None
        if size.value < 16:
            return None
        seconds, microseconds = struct.unpack("qq", buffer.raw[:16])
        if seconds <= 0 or not 0 <= microseconds < 1000000:
            raise RunnerRefused("kernel process start identity has an unsupported layout")
        return f"darwin:{seconds}:{microseconds}"
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return f"linux:{boot}:{raw[19]}"
        except (OSError, IndexError):
            return None
    raise RunnerRefused("process identity is only implemented on macOS and Linux")


def table() -> list[dict]:
    done = subprocess.run(["ps", "-axo", "pid=,pgid=,uid=,stat=,command="], capture_output=True, text=True, timeout=PS_SECONDS, check=False)
    if done.returncode:
        raise RunnerRefused("cannot enumerate process ownership")
    rows = []
    for line in done.stdout.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) == 5 and all(word.isdigit() for word in parts[:3]) and "Z" not in parts[3]:
            rows.append({"pid": int(parts[0]), "pgid": int(parts[1]), "uid": int(parts[2]), "command": parts[4]})
    return rows


_UNSTATTABLE = re.compile(r"lsof: WARNING: can't stat\(\) \S+ file system (/.*)")
_INCOMPLETE = "Output information may be incomplete."
# lsof reports a process it could not read as a file named by the error, on
# stdout and with exit 0 (Apple lsof 4.91, `dproc.c`): that process's working
# directory, descriptors or mappings are unknown, so it may hold the clone (sd:2769).
_UNREADABLE = re.compile(rb"(?:cwd\|rtd|FD|FILEPORT|region|thread) info error: ")
_ASSUMED_DEVICE = re.compile(r'assuming "dev=([0-9a-fA-F]+)" from mount table')


def _elsewhere(block: list[str], path: Path) -> bool:
    """Whether one lsof warning is about a file system the clone is not on.

    lsof warns about every mount it cannot stat, not only the one it was asked
    about: a Carbon Copy Cloner snapshot left mounted at a root-only path made
    every call on the machine print one, and each release stayed `pending`
    (sd:759). That warning names a mount point, and says the output may be
    incomplete for that mount alone. The clone's own mount, a mount above it
    or a mount inside it is where incomplete output could hide a holder, so
    those still refuse. So does a device lsof took from the mount table that
    is the clone's own. `-w` would silence all of them, those included.
    """
    head = _UNSTATTABLE.fullmatch(block[0])
    if not head:
        return False
    devices = set()
    for line in block[1:]:
        assumed = _ASSUMED_DEVICE.fullmatch(line.strip())
        if assumed:
            devices.add(int(assumed.group(1), 16))
        elif line.strip() != _INCOMPLETE:
            return False
    # The mount point is compared as lsof printed it, from the mount table,
    # which holds resolved paths. Resolving it here would look it up, and a
    # mount lsof could not stat is one a lookup can hang on.
    mount = Path(head.group(1))
    if any(mount == own or mount in own.parents or own in mount.parents for own in {path, path.resolve()}):
        return False
    try:
        return path.stat().st_dev not in devices
    except OSError:
        return False


def _verbatim(name: str) -> bool:
    """Whether lsof prints `name` as itself in an `n` field.

    lsof escapes a control character (`\\n`, `^A`), a backslash (`\\\\`) and,
    outside a UTF-8 locale, a non-ASCII byte (`\\xNN`); a literal `^A` prints as
    control-A does. A name of printable ASCII with neither `\\` nor `^` holds no
    escape introducer, so any printed name starts with it exactly when the
    real name does.
    """
    return all(" " <= char <= "~" and char not in "\\^" for char in name)


def _lsof(path: Path, fields: str, *selection: str) -> bytes:
    """`lsof -n -P -F <fields> [selection]`'s stdout; `RunnerRefused` when it cannot vouch for `path`."""
    try:
        done = subprocess.run(["lsof", "-n", "-P", "-F", fields, *selection], capture_output=True,
                              timeout=LSOF_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise RunnerRefused(f"cannot verify clone holders: lsof did not answer in {LSOF_SECONDS} s") from None
    stderr = done.stderr.decode(errors="replace")
    blocks: list[list[str]] = []
    for line in stderr.splitlines():
        # No line is dropped. A blank or whitespace-only line used to be
        # discarded here, so a warning the parser does not recognise passed
        # the fail-close check below by being empty (sd:1221). A line that
        # starts no known block is its own block, which `_elsewhere` refuses.
        if line[:1].isspace() and blocks:
            blocks[-1].append(line)
        else:
            blocks.append([line])
    if done.returncode not in {0, 1} or not all(_elsewhere(block, path) for block in blocks):
        raise RunnerRefused(f"cannot verify clone holders: {stderr.strip()}")
    return done.stdout


def _mounted_inside(roots: set[str]) -> bool:
    """Whether a file system is mounted inside the clone; True when the table cannot be read.

    A hard link stays on its own device, so `holders` matches aliases on the
    clone's device alone, and a mount inside the clone sends it to `+D`.
    """
    try:
        done = subprocess.run(["mount"], capture_output=True, timeout=PS_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return True
    if done.returncode:
        return True
    # Linux escapes a space in a mount point as `\040`.
    return any(f" on {form}/".encode() in done.stdout
               for root in roots for form in {root, root.replace(" ", "\\040")})


def _files(table: bytes):
    """`(pid, fields)` for each open file in `lsof -F` output, fields keyed by letter."""
    pid, fields = None, None
    for line in table.split(b"\n"):
        tag, value = line[:1], line[1:]
        if tag in {b"p", b"f"} and fields is not None:
            yield pid, fields
            fields = None
        if tag == b"p":
            pid = int(value) if value.isdigit() else None
        elif tag == b"f":
            fields = {}
        elif fields is not None and tag:
            fields[tag] = value
    if fields is not None:
        yield pid, fields


def _linked_inside(path: Path, device: int, inodes: set[int]) -> set[int]:
    """Which of `inodes` have a name under `path`, which is on `device`.

    One walk of the directory entries, which carry the inode without a stat
    of each file; only directories are stat'ed, to stay on the device.
    """
    deadline = time.monotonic() + LSOF_SECONDS
    found, pending = set(), [path]
    while pending:
        if time.monotonic() > deadline:
            raise RunnerRefused(f"cannot verify clone holders: the hard-link walk took over {LSOF_SECONDS} s")
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        # A child removed since the listing skips itself, not
                        # its later siblings (sd:1775 review 4).
                        try:
                            child = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if child.st_dev != device:
                            raise RunnerRefused(f"cannot verify clone holders: a file system is mounted at {entry.path}")
                        pending.append(entry.path)
                    elif entry.inode() in inodes and entry.is_file(follow_symlinks=False):
                        found.add(entry.inode())
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RunnerRefused(f"cannot verify clone holders: {error}") from None
    return found


def holders(path: Path) -> set[int]:
    """The processes with an open file or a working directory under `path`.

    One read of the whole open-file table, filtered by name here. `+D <path>`
    stats every file under the clone before it answers: at load 71 it took
    28 s over a checkout of 105,000 files, past `LSOF_SECONDS`, so each tick
    held the ending as `cleanup held` (sd:1775). The table took a third of a
    second on the same machine, and its cost does not grow with the clone.
    The kernel names an open file, a working directory and a mapped binary
    by its resolved path, so the clone is matched as named and as resolved.

    A process can open a clone file through a hard link outside the clone,
    and the table then names the outside link. `+D` matched by device and
    inode, so that process held the clone; it still does. Every regular file
    on the clone's device named outside the clone is looked up by inode in
    one walk of the clone's entries (`_linked_inside`), whatever its link
    count: an outside link unlinked after the open leaves one link, the
    clone's, while the table still names the outside path (sd:1775 review 3).
    So the walk runs whenever such a file is open, which on the system disk
    is nearly always; it reads directory entries, not a stat of each file.
    A mount inside the clone sends the whole question to `+D`.

    lsof escapes the names it prints (`_verbatim`). A clone whose path holds a
    character it escapes is selected by filesystem with `+D` instead, since
    its names cannot be matched as text; that walk is slow over a large
    clone, and a walk that does not finish refuses, so the clone stays held.
    The table is read as bytes: a name that is not UTF-8 is not a reason to fail.
    """
    if not path.exists():
        return set()
    roots = {str(path), str(path.resolve())}
    if not all(_verbatim(root) for root in roots) or _mounted_inside(roots):
        # Selected by filesystem: every process the walk lists holds the clone.
        walk = _lsof(path, "pn", "+D", str(path))
        return {int(line[1:]) for line in walk.split(b"\n") if line.startswith(b"p") and line[1:].isdigit()}
    device = path.stat().st_dev
    prefixes = {os.fsencode(root) for root in roots}
    held, aliases = set(), {}
    for pid, fields in _files(_lsof(path, "ptDin")):
        name = fields.get(b"n", b"")
        if _UNREADABLE.match(name):
            raise RunnerRefused(f"cannot verify clone holders: lsof could not read process {pid}: {os.fsdecode(name)}")
        if pid is None:
            continue
        if any(name == root or name.startswith(root + b"/") for root in prefixes):
            held.add(pid)
        elif fields.get(b"t") == b"REG" and fields.get(b"D", b"").lower() in {b"", f"0x{device:x}".encode()}:
            inode = fields.get(b"i", b"")
            if not inode.isdigit():
                raise RunnerRefused(f"cannot verify clone holders: lsof gave no inode for {os.fsdecode(name)}")
            aliases.setdefault(int(inode), set()).add(pid)
    if aliases:
        for inode in _linked_inside(path, device, set(aliases)):
            held |= aliases[inode]
    return held


def _started_before(pid: int, run: dict) -> bool:
    """Whether a Linux process started before `run` could mark any process.

    The marker is set only in processes the runner starts after it writes the
    run row, and first in the supervisor. With a recorded supervisor, compare
    kernel start ticks on the same boot. Without one, compare the process's
    wall-clock start with the row's `created_at`, with a margin for the
    second-granular boot time. Anything that cannot be read answers False.
    """
    own = start_identity(pid)
    if not own or not own.startswith("linux:"):
        return False
    own_boot, _, own_ticks = own.removeprefix("linux:").rpartition(":")
    if not own_ticks.isdigit():
        return False
    since = run.get("supervisor_start") or ""
    if since:
        boot, _, ticks = since.removeprefix("linux:").rpartition(":")
        return since.startswith("linux:") and own_boot == boot and ticks.isdigit() and int(own_ticks) < int(ticks)
    try:
        created = datetime.fromisoformat(run["created_at"]).timestamp()
        booted = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
        started = booted + int(own_ticks) / os.sysconf("SC_CLK_TCK")
    except (KeyError, TypeError, ValueError, OSError, StopIteration, IndexError):
        return False
    return started + 2 < created


def marked(pid: int, ident: str, run: dict | None = None) -> bool:
    if sys.platform.startswith("linux"):
        try:
            return f"SD_ASSIGNMENT={ident}".encode() in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except FileNotFoundError:
            return False
        except PermissionError:
            # A process that changed credentials without an exec is not
            # dumpable, so even its own user cannot read its environ:
            # GitHub's Ubuntu runner has one from boot. The environment is
            # fixed when a program starts, so a process that started before
            # the run existed cannot carry the run's marker. Any other
            # unreadable environ still refuses.
            if run is not None and _started_before(pid, run):
                return False
            raise RunnerRefused(f"cannot inspect owned user process {pid}") from None
    done = subprocess.run(["ps", "eww", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=PS_SECONDS, check=False)
    # Never expose this output: it can contain unrelated credentials.
    return f"SD_ASSIGNMENT={ident}" in done.stdout.split()


def survivors(run: dict) -> list[dict]:
    rows = table()
    held = holders(Path(run["work_path"]))
    tagged_pids = set()
    if sys.platform == "darwin":
        environment = subprocess.run(["ps", "eww", "-axo", "pid=,command="], capture_output=True, text=True, timeout=PS_SECONDS, check=False)
        if environment.returncode:
            raise RunnerRefused("cannot enumerate escaped process markers")
        for line in environment.stdout.splitlines():
            words = line.split()
            if words and words[0].isdigit() and f"SD_ASSIGNMENT={run['id']}" in words[1:]:
                tagged_pids.add(int(words[0]))
    result = []
    for row in rows:
        if row["pid"] == os.getpid():
            continue
        group = run.get("supervisor_pgid") and row["pgid"] == run["supervisor_pgid"]
        tagged = row["uid"] == os.getuid() and (row["pid"] in tagged_pids if sys.platform == "darwin" else marked(row["pid"], run["id"], run))
        if group or tagged or row["pid"] in held:
            result.append({**row, "ownership": "group" if group else "marker" if tagged else "holder"})
    return result


def terminate_owned(run: dict, *, grace_seconds=3) -> bool:
    """Signal only the group whose live leader still has the recorded start."""
    pid, pgid, identity = (run.get(name) for name in ("supervisor_pid", "supervisor_pgid", "supervisor_start"))
    if not pid or not pgid or pid != pgid or not identity:
        return False
    if start_identity(pid) != identity:
        return False
    # The check above and this signal cannot be made atomic: the leader can
    # exit between them, and the kernel reuses pids. A group that stopped
    # being ours inside that window answers ESRCH -- already gone -- or
    # EPERM, meaning the pid now belongs to somebody else. Both say there was
    # no owned group left to signal, which is what `False` says here and what
    # `signalled_owned_group`, in `local-sd-runner/sd_runner/controls.py`, reports.
    #
    # Letting them raise turned a pid race into a recovery hold, because
    # `runtime.stop_owned` reports any OSError as a probe failure. CI saw
    # `{'probe': 'owned_stop', 'reason': '[Errno 1] Operation not permitted'}`
    # block a recovery that had nothing to clean.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if start_identity(pid) != identity or not any(row["pgid"] == pgid for row in table()):
            return True
        time.sleep(0.05)
    if start_identity(pid) == identity and any(row["pgid"] == pgid for row in table()):
        # The escalation races the same way, and the answer differs: the
        # SIGTERM above did signal an owned group, so the group dying first
        # is this call succeeding, not this call finding nothing.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return True
