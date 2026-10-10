"""Drain the lanes, then run `repo-sync.sh refresh` or `follow` as a child (sd:3099).

`repo-sync.sh refresh` and `follow` (sd:3100) call this first, because
POSIX sh cannot hold a flock. It takes every lane's runner lock under the lane root: while one is
held, that lane's `lane run` exits at once and its queued entries stay
pending. A running lane keeps its lock until its run ends. Each pass tries
every lock first and keeps the ones it gets, then waits for the busy ones:
waiting on one lane at a time let `lane run --hosted` go on to the next lane,
so the drain chased it through every queue (sd:3265). With every lock held it
waits until the gate holders `sd gate status --json` showed at that moment
have ended. A gate admitted later is no lane's: a lane gates only under its
held runner lock. Waiting for an idle gate starved a busy machine for hours,
since new gates kept arriving (sd:3265). Then it runs the refresh steps with
REPO_SYNC_LANES_HELD=1 and exits with their status. The child inherits the lock descriptors and this process keeps its
copies, so the locks stay held until both have exited: a `kill -9` of this
process leaves them with the child until its last step ends. No step leaves
a process behind to keep them: services restart through launchd, which
passes no descriptor on, and the child's git runs gc and maintenance in the
foreground and starts no fsmonitor daemon. TERM, INT and HUP do not end
this process before the child; it waits for it.

A lane whose runner never ran has no lock file yet: every folder under the
lane root, every repository in the registry (`sd-db.sh repo list`, which
`lane run --hosted` reads; for `refresh` only) and every name in
REPO_SYNC_LANE_NAMES (the conf's checkouts) gets one, made and held here; a
lane is named after its checkout's folder. A pass that takes a new lock
checks the gate again, so a gate a new lane's runner left is seen.

The wait is bounded: 45 minutes in total for the locks and the gate
together (operator ruling), or REPO_SYNC_DRAIN_WAIT seconds. Past it, refresh refuses with nothing moved and
names what was busy.

Usage: refresh_drain.py <repo-sync.sh> refresh|follow [path ...]
"""

import fcntl
import json
import os
import pathlib
import shutil
import signal
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


def take_locks(root, held, deadline, limit, names=()):
    """Take each runner lock under root not yet held, making the missing ones
    of lane folders and named lanes; returns how many were new. It tries them
    all before it waits, so a lane it already holds starts no new run."""
    lanes = {folder.parent.name for folder in root.glob("*/lane")} | set(names)
    locks = {root / name / "lane" / "queue" / "runner.lock" for name in lanes}
    new = [lock for lock in sorted(locks | set(root.glob("*/lane/queue/runner.lock"))) if lock not in held]
    for lock in new:
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            held[lock] = open(lock, "a", encoding="utf-8")
        except OSError as error:
            raise Refused(f"cannot open {lock}: {error.strerror}") from None
    busy = list(new)

    def still_busy():
        for lock in list(busy):
            try:
                fcntl.flock(held[lock], fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            busy.remove(lock)
        if not busy:
            return None
        return "; ".join(f"lane {lock.parent.parent.parent.name} (its runner holds {lock})" for lock in busy)

    wait(still_busy, deadline, limit)
    return len(new)


def gate_holders(env):
    """The holders `sd gate status --json` lists now, as a key per holder."""
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
        if not isinstance(holders, list) or not isinstance(waiters, list) \
                or not all(isinstance(holder, dict) for holder in holders):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise Refused("sd gate status --json printed no holders and waiters lists", manual=True) from None
    # A slot's next holder has another pid or start, so a key names one gate run.
    return {(holder.get("slot"), holder.get("pid"), holder.get("since")): holder for holder in holders}


def wait_gate(env, deadline, limit):
    """Wait until each gate holder present now has ended; later ones do not count."""
    first = gate_holders(env)

    def busy():
        left = [first[key] for key in gate_holders(env) if key in first]
        if not left:
            return None
        pids = ", ".join(f"pid {holder.get('pid') or '?'}" for holder in left)
        return f"the gate: {len(left)} holder(s) that started before the lanes were held ({pids})"

    if first:
        wait(busy, deadline, limit)


def registry_names(script, env):
    """The checkout folder names of every repository `sd-db.sh repo list` holds."""
    sd_db = pathlib.Path(script).resolve().parent.parent / "local-sd-db" / "sd-db.sh"
    try:
        out = subprocess.run(["sh", str(sd_db), "repo", "list"], capture_output=True,
                             text=True, env=env, timeout=120)
    except subprocess.TimeoutExpired:
        raise Refused(f"{sd_db} repo list did not answer in 120s", manual=True) from None
    if out.returncode != 0:
        raise Refused(f"{sd_db} repo list exited {out.returncode}: "
                      f"{(out.stderr or out.stdout).strip()[-300:]}", manual=True)
    # "sd-db: <path>  <remote>  ...": two spaces end the path, which may hold one.
    return [os.path.basename(line[len("sd-db: "):].split("  ")[0].rstrip("/"))
            for line in out.stdout.splitlines() if line.startswith("sd-db: ")]


def drain(env, names=()):
    """Hold every lane lock and outwait the gates from before; returns the held handles."""
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
    # A lane folder made during the gate wait is taken on the next pass, and
    # its runner's gate is seen: each pass that takes a lock checks the gate.
    # The first pass checks it even with no lane at all.
    first = True
    while True:
        new = take_locks(root, held, deadline, limit, names)
        if not new and not first:
            break
        first = False
        if has_sd:
            wait_gate(env, deadline, limit)
    print(f"lanes held: {len(held)} runner lock(s) under {root}; "
          "every gate from before is done", flush=True)
    return held


def main(argv):
    if len(argv) < 3 or argv[2] not in ("refresh", "follow"):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    script, verb, args = argv[1], argv[2], argv[3:]
    env = dict(os.environ)
    try:
        names = env.get("REPO_SYNC_LANE_NAMES", "").split()
        # Not on a satellite: `lane run` refuses there, and its sd_db may be
        # a build the hub refuses until follow moves it.
        if verb == "refresh":
            names += registry_names(script, env)
        held = drain(env, names)
    except Refused as refusal:
        print(f"repo-sync.sh {verb}: refused, nothing moved: {refusal}", file=sys.stderr)
        if refusal.manual:
            print(MANUAL, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"repo-sync.sh {verb}: interrupted, nothing moved", file=sys.stderr)
        return 130
    code = run_child(["sh", script, verb, *args], {**env, "REPO_SYNC_LANES_HELD": "1"}, held)
    for handle in held.values():
        handle.close()
    return code


# Git settings that would leave a process behind holding the inherited locks.
FOREGROUND_GIT = (("gc.autoDetach", "false"), ("maintenance.autoDetach", "false"),
                  ("core.fsmonitor", "false"))


def foreground_git(env):
    """env with FOREGROUND_GIT added as command-line git config (GIT_CONFIG_COUNT)."""
    env = dict(env)
    count = int(env.get("GIT_CONFIG_COUNT") or 0)
    for key, value in FOREGROUND_GIT:
        env[f"GIT_CONFIG_KEY_{count}"], env[f"GIT_CONFIG_VALUE_{count}"] = key, value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    return env


def run_child(command, env, held):
    """Run the child in this process group and wait for it, through TERM, INT
    and HUP. One group, so a terminal's signal or a group kill reaches the
    child and its steps too, and the child keeps the terminal for a git
    prompt. A signal to this process alone is not passed on, so the refresh
    steps finish. The child gets the held lock descriptors."""
    child = subprocess.Popen(command, env=foreground_git(env),
                             pass_fds=[handle.fileno() for handle in held.values()])

    def hold(signum, _frame):
        print(f"refresh: got signal {signum}; the lanes stay held until the refresh steps end",
              file=sys.stderr, flush=True)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, hold)
    code = child.wait()
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
