"""Run sweep: one click runs a realm's standard sweep, headless, end to end. One mechanism, three realms (`REALMS`).

- `hoa` (sd:5095), on the HOA page: the hoa checkout's `tools/sweep.py run`, one `claude -p` with an exact allowlist
  that reviews the week's changes, reads both Drives and the alias mail, updates the status and the VP brief, saves
  followups as Gmail drafts (never sent) and hands its commit to the lane. That script holds its prompt and record.
- `work` and `personal` (sd:5096, sd:5097), on the Tasks page's Work and Personal views: `realm_sweep run <realm>`
  from this checkout. It leaves a brief, sd followups and Gmail drafts, and commits to no repository.

This module only starts a run and reads it back, as `adversarial` starts the gate. `start` answers `POST /api/hoa/sweep`
and `POST /api/tasks/sweep {"realm"}`, in three steps, and answers 202 at once:

1. The runner: for `hoa`, the repo the table holds as `hoa` (`hoa_screen.checkout`) with `SCRIPT` in it, none a 400;
   for the others, `REALM_RUNNER` from this checkout.
2. The realm's lock, `<state>/lock`, taken without waiting: a second start of that realm while a run holds it is a 409
   naming when it started. Each realm has its own state folder, so the Work and Personal sweeps may run together.
3. The runner in a session of its own, holding the lock's descriptor, so the lock is released only when the run exits.
   Its stdout and stderr go to `<state>/start.log`: a run that dies before it writes its record leaves its reason there.

`status` answers `GET /api/hoa/sweep/status` (and `/api/hoa`'s `sweep` part) with `SCRIPT status --json`, bounded by
`SECONDS`, and `GET /api/tasks/sweep/status` (and `/api/tasks`' `sweeps`) with `realm_sweep.status`, read in process;
each plus whether this dashboard's lock is held. A start newer than the record's own start, with the lock free, means the
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

__all__ = ["COMMAND", "REALMS", "REALM_RUNNER", "SCRIPT", "SECONDS", "request", "request_realm", "spawn", "start",
           "state_dir", "status"]

SCRIPT = "tools/sweep.py"
COMMAND = f"python3 {SCRIPT} run"
#: How long `SCRIPT status --json` may take before the read names it as not read.
SECONDS = 10
REALMS = ("hoa", "work", "personal")
#: The Work and Personal runner, before its `run <realm>`; it runs from `DASHBOARD`.
REALM_RUNNER = (sys.executable, "-m", "sd_dashboard.realm_sweep")
DASHBOARD = Path(__file__).resolve().parent.parent


def state_dir(realm: str = "hoa") -> Path:
    """`st_state_dir <realm>-sweep`: what the dashboard's starts, and the Work and Personal runs, write."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "system" / f"{realm}-sweep"


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


def _runner(connection, realm: str) -> tuple[Path, list[str], str]:
    """Step 1: the folder the run starts in, its argv, and the command as the page shows it."""
    if realm == "hoa":
        place = _place(connection)
        return place, [sys.executable, str(place / SCRIPT), "run"], COMMAND
    return DASHBOARD, [*REALM_RUNNER, "run", realm], f"python3 -m sd_dashboard.realm_sweep run {realm}"


def start(connection, realm: str = "hoa"):
    """Steps 1 to 3 above: `(202, {"started", "repo", "command"})`, or `(409, {"error"})` while a run holds the lock."""
    place, argv, command = _runner(connection, realm)
    return spawn(state_dir(realm), place, argv, command, f"A {realm} sweep")


def spawn(state: Path, place: Path, argv: list[str], command: str, what: str, prepare=None):
    """Steps 2 and 3, for any headless run: `state`'s lock without waiting, then `argv` in `place`, in its own session,
    holding the lock. `prepare()` runs under the lock before the start, so a refused start writes nothing of its own.
    `what` names the run in the 409 (`A work sweep`); `research_experiments` starts its runs here too (sd:5296)."""
    state.mkdir(parents=True, exist_ok=True)
    handle = open(state / "lock", "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            return 409, {"error": f"{what} is already running (started {handle.read().strip() or 'at an unknown time'}); "
                                  f"one runs at a time. Wait for it to end, then start the next."}
        started = _now()
        handle.seek(0)
        handle.truncate()
        handle.write(started)
        handle.flush()
        if prepare is not None:
            prepare()
        with open(state / "start.log", "w", encoding="utf-8") as log:
            log.write(f"{started} {command} in {place}\n")
            log.flush()
            runner = subprocess.Popen(argv, cwd=str(place),
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                      pass_fds=(handle.fileno(),), start_new_session=True, env=_environment())
        # Reaped here when it ends, so a long-lived server keeps no zombie; a restart hands it to launchd instead.
        threading.Thread(target=runner.wait, name=what, daemon=True).start()
        return 202, {"started": started, "repo": str(place), "command": command}
    finally:
        handle.close()


def request(values: dict):
    """The POST's checks, before the server opens a connection; the start as `action_route` returns one."""
    if values:
        raise ValueError("Run sweep takes no fields.")
    return start


def request_realm(values: dict):
    """`POST /api/tasks/sweep`'s checks: one field, `realm`, work or personal; the HOA sweep has its own route."""
    realm = values.get("realm")
    if set(values) != {"realm"} or realm not in REALMS[1:]:
        raise ValueError("Run sweep takes one field, realm: work or personal.")
    return lambda connection: start(connection, realm)


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


def status(place: Path | None, realm: str = "hoa") -> dict:
    """The sweep's state for the page: `running`, `last` (the record), `error`, `command`, `repo`, `start_log`."""
    state = state_dir(realm)
    if realm != "hoa":
        from . import realm_sweep

        place, _, command = _runner(None, realm)
        out = {"running": False, "last": None, "error": "", "command": command, "repo": str(place),
               "start_log": str(state / "start.log")}
        try:
            record = realm_sweep.status(realm)
        except (OSError, ValueError) as error:
            record, out["error"] = None, f"{state / 'last.json'} did not read: {error}"
        return _judged(out, state, record)
    out = {"running": False, "last": None, "error": "", "command": COMMAND, "repo": str(place or ""),
           "start_log": str(state / "start.log")}
    if place is None:
        out["error"] = "no checkout: no repo named hoa is registered"
        return out
    script = place / SCRIPT
    if not script.is_file():
        out["error"] = f"{SCRIPT} is not in the checkout"
        return out
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
    return _judged(out, state, record)


def _judged(out: dict, state: Path, record: dict | None) -> dict:
    """`out` with `running` and `last`: a start of ours newer than the record's, with the lock free, reads failed."""
    out["running"] = bool(_held(state)) or bool(record and record.get("running"))
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
