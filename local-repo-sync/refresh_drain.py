"""Drain the lanes, then run `repo-sync.sh refresh` as a child (sd:3099).

`repo-sync.sh refresh` calls this first, because POSIX sh cannot hold a
flock. It takes every lane's runner lock under the lane root: while one is
held, that lane's `lane run` exits at once and its queued entries stay
pending. A running lane keeps its lock until its run ends. With every lock
held it waits until `sd gate status --json` shows no holders and no waiters,
then runs the refresh steps with REPO_SYNC_LANES_HELD=1 and exits with their
status. The locks drop when this process exits, however it exits: the kernel
releases a flock with its holder, and the child never inherits the
descriptors (Python opens files non-inheritable).

The wait is bounded: 45 minutes in total for the locks and the gate
together (operator ruling), or REPO_SYNC_DRAIN_WAIT seconds. Past it, refresh refuses with nothing moved and
names what was busy.

Usage: refresh_drain.py <repo-sync.sh> [path ...]
"""

import fcntl
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

DEFAULT_WAIT = 45 * 60
NOTE_EVERY = 60
MANUAL = ("To move a checkout by hand while no lane runs: "
          "git -C <checkout> switch --detach origin/main; in the command pack, "
          "then make -C <checkout> setup.")


class Refused(Exception):
    def __init__(self, message, manual=False):
        super().__init__(message)
        self.manual = manual


def lane_root(env):
    """The pack's lane_root(): SD_LANE_ROOT, the sd.lane_root setting, the state default."""
    value = env.get("SD_LANE_ROOT") or lane_root_setting(env)
    if value:
        return pathlib.Path(os.path.expanduser(value))
    state = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local/state")
    return pathlib.Path(state) / "sd" / "lanes"


def lane_root_setting(env):
    """The sd.lane_root setting, or None when it is unset or sd is absent."""
    try:
        out = subprocess.run(["sd", "config", "get", "sd.lane_root"], capture_output=True,
                             text=True, env=env, timeout=120)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        raise Refused("sd config get sd.lane_root did not answer in 120s", manual=True) from None
    if out.returncode == 0:
        return out.stdout.strip() or None
    # Only "unset" falls back: guessing past a broken setting drains the wrong folder.
    if "is not set" in out.stderr:
        return None
    raise Refused(f"sd config get sd.lane_root failed: {out.stderr.strip()[-300:]}", manual=True)


def wait(busy, deadline, limit):
    """Call busy() each second until it returns None; it names what is busy."""
    noted = None
    while True:
        what = busy()
        if what is None:
            return
        now = time.monotonic()
        if now >= deadline:
            raise Refused(f"still waiting for {what} after {limit}s")
        if noted is None or now - noted >= NOTE_EVERY:
            print(f"waiting for {what}", flush=True)
            noted = now
        time.sleep(1)


def take_locks(root, held, deadline, limit):
    """Take each runner lock under root not yet held; returns how many were new."""
    new = [lock for lock in sorted(root.glob("*/lane/queue/runner.lock")) if lock not in held]
    for lock in new:
        try:
            handle = open(lock, "a", encoding="utf-8")
        except OSError as error:
            raise Refused(f"cannot open {lock}: {error.strerror}") from None
        held[lock] = handle

        def busy(handle=handle, lock=lock):
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return f"lane {lock.parent.parent.parent.name} (its runner holds {lock})"
            return None

        wait(busy, deadline, limit)
    return len(new)


def gate_busy(env):
    """None when the gate has no holders and no waiters, else what it holds."""
    try:
        out = subprocess.run(["sd", "gate", "status", "--json"], capture_output=True,
                             text=True, env=env, timeout=120)
    except subprocess.TimeoutExpired:
        raise Refused("sd gate status --json did not answer in 120s", manual=True) from None
    if out.returncode != 0:
        raise Refused(f"sd gate status --json exited {out.returncode}: {out.stderr.strip()[-300:]}",
                      manual=True)
    try:
        status = json.loads(out.stdout)
        holders, waiters = status["holders"], status["waiters"]
        if not isinstance(holders, list) or not isinstance(waiters, list):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise Refused("sd gate status --json printed no holders and waiters lists", manual=True) from None
    if not holders and not waiters:
        return None
    return f"the gate: {len(holders)} holder(s), {len(waiters)} waiter(s)"


def drain(env):
    """Hold every lane lock and see an idle gate; returns the held handles."""
    raw = env.get("REPO_SYNC_DRAIN_WAIT", str(DEFAULT_WAIT))
    if not raw.isdigit():
        raise Refused(f"REPO_SYNC_DRAIN_WAIT must be a number of seconds, got {raw!r}")
    limit = int(raw)
    deadline = time.monotonic() + limit
    root = lane_root(env)
    has_sd = shutil.which("sd", path=env.get("PATH")) is not None
    if not has_sd:
        print("sd is not on PATH: no gate to wait on", flush=True)
    held = {}
    # A lane folder made during the gate wait is taken on the next pass.
    while True:
        take_locks(root, held, deadline, limit)
        if has_sd:
            wait(lambda: gate_busy(env), deadline, limit)
        if not take_locks(root, held, deadline, limit):
            break
    print(f"lanes held: {len(held)} runner lock(s) under {root}; gate idle", flush=True)
    return held


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    script, args = argv[1], argv[2:]
    env = dict(os.environ)
    try:
        held = drain(env)
    except Refused as refusal:
        print(f"repo-sync.sh refresh: refused, nothing moved: {refusal}", file=sys.stderr)
        if refusal.manual:
            print(MANUAL, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("repo-sync.sh refresh: interrupted, nothing moved", file=sys.stderr)
        return 130
    child = subprocess.run(["sh", script, "refresh", *args], env={**env, "REPO_SYNC_LANES_HELD": "1"})
    for handle in held.values():
        handle.close()
    return child.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
