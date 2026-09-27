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
from pathlib import Path

from sd_db.runner import RunnerRefused


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
    done = subprocess.run(["ps", "-axo", "pid=,pgid=,uid=,stat=,command="], capture_output=True, text=True, timeout=10, check=False)
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


def holders(path: Path) -> set[int]:
    if not path.exists():
        return set()
    done = subprocess.run(["lsof", "-n", "-P", "-F", "p", "+D", str(path)], capture_output=True, text=True, timeout=20, check=False)
    blocks: list[list[str]] = []
    for line in done.stderr.splitlines():
        # No line is dropped. A blank or whitespace-only line used to be
        # discarded here, so a warning the parser does not recognise passed
        # the fail-close check below by being empty (sd:1221). A line that
        # starts no known block is its own block, which `_elsewhere` refuses.
        if line[:1].isspace() and blocks:
            blocks[-1].append(line)
        else:
            blocks.append([line])
    if done.returncode not in {0, 1} or not all(_elsewhere(block, path) for block in blocks):
        raise RunnerRefused(f"cannot verify clone holders: {done.stderr.strip()}")
    return {int(line[1:]) for line in done.stdout.splitlines() if line.startswith("p") and line[1:].isdigit()}


def _started_before(pid: int, since: str | None) -> bool:
    """Whether a Linux process started before the kernel start identity `since`.

    Both identities are `linux:<boot id>:<start ticks>`, as `start_identity`
    writes them. A different boot, or an identity that cannot be read,
    answers False.
    """
    own = start_identity(pid)
    if not own or not since:
        return False
    own_boot, _, own_ticks = own.removeprefix("linux:").rpartition(":")
    boot, _, ticks = since.removeprefix("linux:").rpartition(":")
    return own.startswith("linux:") and since.startswith("linux:") and own_boot == boot \
        and own_ticks.isdigit() and ticks.isdigit() and int(own_ticks) < int(ticks)


def marked(pid: int, ident: str, since: str | None = None) -> bool:
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
            # the run's supervisor cannot carry the run's marker. Any other
            # unreadable environ still refuses.
            if _started_before(pid, since):
                return False
            raise RunnerRefused(f"cannot inspect owned user process {pid}") from None
    done = subprocess.run(["ps", "eww", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=10, check=False)
    # Never expose this output: it can contain unrelated credentials.
    return f"SD_ASSIGNMENT={ident}" in done.stdout.split()


def survivors(run: dict) -> list[dict]:
    rows = table()
    held = holders(Path(run["work_path"]))
    tagged_pids = set()
    if sys.platform == "darwin":
        environment = subprocess.run(["ps", "eww", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10, check=False)
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
        tagged = row["uid"] == os.getuid() and (row["pid"] in tagged_pids if sys.platform == "darwin" else marked(row["pid"], run["id"], run.get("supervisor_start")))
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
