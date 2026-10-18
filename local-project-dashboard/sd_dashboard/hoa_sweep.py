"""Run sweep (sd:5095): one click on the HOA page runs the hoa checkout's standard sweep, headless, end to end.

The sweep is the checkout's `tools/sweep.py run`: one `claude -p` with an exact allowlist that reviews the week's
changes, reads both Drives and the alias mail, updates the status and the VP brief, saves followups as Gmail drafts
(never sent) and hands its commit to the lane. That script holds the allowlist, its own lock and its record; this
module only starts it and reads it back, as `adversarial` starts the gate.

`start` answers `POST /api/hoa/sweep`, in three steps, and answers 202 at once:

1. The checkout: the repo the table holds as `hoa` (`hoa_screen.checkout`), with `SCRIPT` in it; none is a 400.
2. The lock, `<state>/lock`, taken without waiting: a second start while a run holds it is a 409 naming when it started.
3. `SCRIPT run` in a session of its own, holding the lock's descriptor, so the lock is released only when the run exits.
   Its stdout and stderr go to `<state>/start.log`: a run that dies before it writes its record leaves its reason there.

`status` answers `GET /api/hoa/sweep/status` (and `/api/hoa`'s `sweep` part): `SCRIPT status --json`, bounded by `SECONDS`,
plus whether this dashboard's lock is held. A start newer than the record's own start, with the lock free, means the
run ended without a record of its own: that reads `failed`, naming `start.log`, never the older run's outcome.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

__all__ = ["COMMAND", "SCRIPT", "SECONDS", "request", "start", "state_dir", "status"]

SCRIPT = "tools/sweep.py"
COMMAND = f"python3 {SCRIPT} run"
#: How long `SCRIPT status --json` may take before the read names it as not read.
SECONDS = 10


def state_dir() -> Path:
    """`st_state_dir hoa-sweep`: what the dashboard's starts write and nobody edits."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "system" / "hoa-sweep"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _place(connection) -> Path:
    from .hoa_screen import checkout

    _, place, problems = checkout(connection)
    if problems:
        raise ValueError(problems[0])
    if place is None:
        raise ValueError("No repo named hoa is registered, so there is no sweep to run (sd-db repo add).")
    if not (place / SCRIPT).is_file():
        raise ValueError(f"{SCRIPT} is not in the hoa checkout {place}; pull its main first.")
    return place


def _environment() -> dict:
    """The server's environment, with `~/.local/bin` on PATH: the run calls `sd` and `sd-ship`, which install there,
    and the LaunchAgent's PATH (`runtime.LAUNCH_PATH`) may not hold that folder."""
    environment = dict(os.environ)
    local = str(Path.home() / ".local" / "bin")
    parts = environment.get("PATH", "").split(":")
    if local not in parts:
        environment["PATH"] = ":".join([part for part in parts if part] + [local])
    return environment


def start(connection):
    """Steps 1 to 3 above: `(202, {"started", "repo", "command"})`, or `(409, {"error"})` while a run holds the lock."""
    place = _place(connection)
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    handle = open(state / "lock", "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            return 409, {"error": f"A sweep is already running (started {handle.read().strip() or 'at an unknown time'}); "
                                  f"one runs at a time. Wait for it to end, then start the next."}
        started = _now()
        handle.seek(0)
        handle.truncate()
        handle.write(started)
        handle.flush()
        with open(state / "start.log", "w", encoding="utf-8") as log:
            log.write(f"{started} {COMMAND} in {place}\n")
            log.flush()
            runner = subprocess.Popen([sys.executable, str(place / SCRIPT), "run"], cwd=str(place),
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                      pass_fds=(handle.fileno(),), start_new_session=True, env=_environment())
        # Reaped here when it ends, so a long-lived server keeps no zombie; a restart hands it to launchd instead.
        threading.Thread(target=runner.wait, name="hoa sweep", daemon=True).start()
        return 202, {"started": started, "repo": str(place), "command": COMMAND}
    finally:
        handle.close()


def request(values: dict):
    """The POST's checks, before the server opens a connection; the start as `action_route` returns one."""
    if values:
        raise ValueError("Run sweep takes no fields.")
    return start


def _held(state: Path) -> str | None:
    """The lock's start time while a run holds it, else None. A lock file not there is not held."""
    try:
        handle = open(state / "lock", encoding="utf-8")
    except FileNotFoundError:
        return None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return handle.read().strip() or "an unknown time"
        fcntl.flock(handle, fcntl.LOCK_UN)
        return None


def _since(text) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def status(place: Path | None) -> dict:
    """The sweep's state for the page: `running`, `last` (the record), `error`, `command`, `repo`, `start_log`."""
    state = state_dir()
    out = {"running": False, "last": None, "error": "", "command": COMMAND, "repo": str(place or ""),
           "start_log": str(state / "start.log")}
    if place is None:
        out["error"] = "no checkout: no repo named hoa is registered"
        return out
    script = place / SCRIPT
    if not script.is_file():
        out["error"] = f"{SCRIPT} is not in the checkout"
        return out
    held = _held(state)
    try:
        done = subprocess.run([sys.executable, str(script), "status", "--json"], cwd=place, capture_output=True,
                              text=True, timeout=SECONDS, check=False)
        record = json.loads(done.stdout) if done.returncode == 0 else None
        if record is not None and not isinstance(record, dict):
            raise TypeError("the status is not an object")
    except subprocess.TimeoutExpired:
        record, out["error"] = None, f"{SCRIPT} status ran past its {SECONDS} seconds"
    except OSError as error:
        record, out["error"] = None, f"{SCRIPT} status did not start: {error}"
    except (ValueError, TypeError) as error:
        record, out["error"] = None, f"{SCRIPT} status printed no record: {error}"
    else:
        if record is None:
            out["error"] = f"{SCRIPT} status exited {done.returncode}: {(done.stderr.strip().splitlines() or [''])[0]}"
    out["running"] = bool(held) or bool(record and record.get("running"))
    try:
        began = (state / "lock").read_text(encoding="utf-8").strip()
    except OSError:
        began = ""
    ours, theirs = _since(began), _since((record or {}).get("started") or "")
    if not out["running"] and ours and (theirs is None or ours > theirs) and not out["error"]:
        record = {"state": "failed", "started": began, "exit": None, "log": out["start_log"],
                  "reason": f"the sweep started at {began} ended without a record of its own; see {out['start_log']}"}
    out["last"] = record
    return out
