"""Build deck (pack sd:5525): one experiment's status deck, `sd-research-kit deck` in its checkout, queued as a drain is.
Render (sd:5632) runs `sd-research-kit render` the same way, under the same lock, as a dashboard-triggered run.

An Experiments row posts `/api/research/deck` with `{"repo": "<key>"}`. `start` runs in the POST, in `publish_drain`'s run
shape, and answers 202 at once:

1. The lock, `<state>/lock`, taken without waiting: a second build while one holds it is a 409 naming the run.
2. The checkout: one `research_screen.checkout` serves, keeping its own `30-brief/deck.json`. Any other is a 400.
3. The run's state file, `<state>/runs/<id>.json`, as `queued`, with the checkout and the deck it writes,
   `docs/dashboard/deck.pptx` there. The kit takes no path (R10-D6): it runs in the checkout.
4. This file as a script, in a session of its own, holding the lock's descriptor.

The runner (`run`) marks the run `running` and runs the pack's `sd-research-kit deck`, bounded at `DECK_SECONDS` (its
process group killed past it). The run is `done` when the kit exits 0 and wrote the deck during the run, else `failed`
with the kit's refusal or usage line, or the log's last line. Nothing uploads or imports.

`status` answers `GET /api/research/deck/<id>` from the state file, as `publish_drain.status` does; `latest` answers
`GET /api/research/deck/status` with the last run, or none. `built` is the deck a row links to, for the download route.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

if __package__:
    from .publish_drain import _alive, _iso, _write
else:  # run as the runner: this folder is sys.path[0]
    from publish_drain import _alive, _iso, _write

__all__ = ["DECK_SECONDS", "STARTUP", "built", "latest", "request", "run", "start", "status"]

#: The bound on one build, in seconds: the kit may first fetch python-pptx.
DECK_SECONDS = 300
#: How long a queued run may wait for its runner to say it started before it reads as failed.
STARTUP = 60
RUN_ID = re.compile(r"(deck|render)-[0-9]{8}T[0-9]{6}Z")


def state_dir() -> Path:
    """`st_state_dir research-deck`: what runs write and nobody edits."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "system" / "research-deck"


def _root() -> Path:
    from . import research_screen

    return Path(research_screen._collectors().REPO_ROOT)


def built(key: str, root: Path | None = None) -> Path | None:
    """The deck `sd-research-kit deck` wrote in the checkout `key` names; None for any other key or a linked file."""
    from . import research_screen

    repo = research_screen.checkout(root or _root(), key)
    path = None if repo is None else repo / research_screen.BUILT
    return path if path is not None and path.is_file() and not path.is_symlink() else None


def start(key: str, root: Path, *, verb: str = "deck", now: datetime.datetime | None = None, spawn=subprocess.Popen):
    """Steps 1 to 4 above: `(202, run)`, or `(409, {"error"})` while a build holds the lock. `verb` is deck or render."""
    from . import pack_bin, research_screen

    now = now or datetime.datetime.now(datetime.timezone.utc)
    state = state_dir()
    runs = state / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    handle = open(state / "lock", "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            return 409, {"error": f"A deck build or render is already running (run {handle.read().strip() or 'unknown'}); "
                                  f"one runs at a time. Wait for it to finish, then press it again."}
        repo = research_screen.checkout(root, key)
        if repo is None:
            raise ValueError("No research checkout at this address.")
        deck = repo / research_screen.DECK
        if verb == "deck" and (not deck.is_file() or deck.is_symlink()):
            raise ValueError(f"{key} keeps no {research_screen.DECK}: add one beside its status brief.")
        run_id = f"{verb}-{now.astimezone(datetime.timezone.utc):%Y%m%dT%H%M%SZ}"
        record = runs / f"{run_id}.json"
        if record.exists():
            return 409, {"error": f"Run {run_id} started this second; press it again in a moment."}
        argv = [sys.executable, str(pack_bin() / "sd-research-kit"), verb]
        # A render writes many files, so its outcome is the exit alone (out None).
        out = str(repo / research_screen.BUILT) if verb == "deck" else None
        document = {"id": run_id, "verb": verb, "state": "queued", "reason": None, "repo": key, "out": out,
                    "argv": argv, "cwd": str(repo), "timeout": DECK_SECONDS, "started": _iso(now), "finished": None,
                    "pid": None}
        _write(record, document)
        handle.seek(0)
        handle.truncate()
        handle.write(run_id)
        handle.flush()
        try:
            runner = spawn([sys.executable, str(Path(__file__).resolve()), "run", str(record), str(handle.fileno())],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           pass_fds=(handle.fileno(),), start_new_session=True, cwd=str(state))
        except OSError as problem:
            _write(record, {**document, "state": "failed", "reason": f"the runner did not start: {problem}",
                            "finished": _iso(datetime.datetime.now(datetime.timezone.utc))})
            raise
        # Reaped here when it ends, so a long-lived server keeps no zombie.
        threading.Thread(target=runner.wait, name=f"research deck {run_id}", daemon=True).start()
        return 202, public(document)
    finally:
        handle.close()


def request(values: dict, verb: str = "deck"):
    """The POST's checks: one field, `repo`, a checkout key; the start as a write."""
    if set(values) != {"repo"} or not isinstance(values["repo"], str):
        raise ValueError('Send {"repo": "<checkout key>"}: the experiment whose deck to build.')

    def write(connection):
        return start(values["repo"], _root(), verb=verb)

    return write


def public(document: dict) -> dict:
    """What a page reads: the state file less the runner's own fields."""
    return {key: value for key, value in document.items() if key not in ("pid", "argv", "cwd", "timeout")}


def status(run_id: str, *, now: datetime.datetime | None = None):
    """`(status, document)` for one run; a run whose runner is gone without an outcome reads failed."""
    if not RUN_ID.fullmatch(run_id):
        return 404, {"error": "No such deck run."}
    try:
        document = json.loads((state_dir() / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 404, {"error": "No such deck run."}
    except (OSError, ValueError) as problem:
        return 503, {"error": f"The state of run {run_id} could not be read: {problem}"}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if document["state"] == "running" and not _alive(document.get("pid")):
        document.update(state="failed", reason=f"the runner (pid {document.get('pid')}) stopped without recording an outcome")
    elif document["state"] == "queued":
        started = datetime.datetime.fromisoformat(document["started"].replace("Z", "+00:00"))
        if (now - started).total_seconds() > STARTUP:
            document.update(state="failed", reason=f"the runner never started within {STARTUP} s")
    return 200, public(document)


def latest(*, now: datetime.datetime | None = None):
    """The last run the lock names, or `{"run": None}` before the first; its state as `status` reads it."""
    try:
        run_id = (state_dir() / "lock").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        run_id = ""
    except OSError as problem:
        return 503, {"error": f"The last deck run could not be read: {problem}"}
    if not run_id:
        return 200, {"run": None}
    code, document = status(run_id, now=now)
    return (200, {"run": document}) if code == 200 else (code, document)


def _wrote(out: Path, since: float) -> bool:
    try:
        return out.is_file() and out.stat().st_mtime >= since
    except OSError:
        return False


def run(record: Path, lock: int | None = None) -> int:
    """The runner: the kit's deck, then the outcome from its exit and the file; 0 when the deck was written."""
    document = json.loads(record.read_text(encoding="utf-8"))
    document.update(state="running", pid=os.getpid())
    _write(record, document)
    log = record.with_suffix(".log")
    # A deck left by an earlier build is not this run's: the file must be written after this point, in whole seconds, as
    # some file systems keep them.
    since = float(int(datetime.datetime.now(datetime.timezone.utc).timestamp()))
    with open(log, "w", encoding="utf-8") as sink:
        # A render nobody typed: the kit then executes only a research.conf.py a render by hand recorded as read.
        env = dict(os.environ, SD_RESEARCH_TRIGGER="dashboard") if document.get("verb") == "render" else None
        child = subprocess.Popen(document["argv"], stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                                 cwd=document["cwd"], start_new_session=True, env=env)
        try:
            code = child.wait(timeout=document["timeout"])
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            code = None
    said = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    # The kit's refusal, or an older pack's usage line, names the problem better than the usage text's last line.
    told = [line for line in said if line.startswith(("sd-research-kit", "usage:"))][:1] or said[-1:]
    if code is None:
        reason = f"the build did not end within {document['timeout']} s; its process group was killed"
    elif code or (document["out"] and not _wrote(Path(document["out"]), since)):
        reason = f"sd-research-kit {document.get('verb', 'deck')} exited {code}" + (f": {told[0][:300]}" if told else "")
    else:
        reason = None
    document.update(state="failed" if reason else "done", reason=reason,
                    finished=_iso(datetime.datetime.now(datetime.timezone.utc)))
    if lock is not None:
        os.close(lock)
    _write(record, document)
    return 1 if reason else 0


if __name__ == "__main__":
    # `run <state file> <lock descriptor>`: the descriptor stays open, unread, until this process exits.
    if len(sys.argv) != 4 or sys.argv[1] != "run":
        sys.exit("usage: research_deck.py run <state file> <lock descriptor>")
    sys.exit(run(Path(sys.argv[2]), int(sys.argv[3])))
