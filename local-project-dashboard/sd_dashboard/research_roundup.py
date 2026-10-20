"""Build roundup (pack sd:5525): one deck from every research checkout's `30-brief/deck.json`, queued as a drain is.

The Experiments view posts `/api/research/roundup` with `{}`. `start` runs in the POST, in `publish_drain`'s run shape,
and answers 202 at once:

1. The lock, `<state>/lock`, taken without waiting: a second build while one holds it is a 409 naming the run.
2. The decks: each board row whose checkout keeps a `deck.json`, newest Updated first; none is a 400. The kit finds its own.
3. The run's state file, `<state>/runs/<id>.json`, as `queued`, with the decks and the deck it writes. The kit takes no
   path (R10-D6): it walks the checkouts under `~/repos` itself and writes `docs/dashboard/roundup.pptx` in its working
   directory, `<state>/runs/<id>/`.
4. This file as a script, in a session of its own, holding the lock's descriptor.

The runner (`run`) marks the run `running` and runs the pack's `sd-research-kit roundup` there, bounded at
`ROUNDUP_SECONDS` (its process group killed past it). The run is `done` when the kit exits 0 and the deck exists, with
the decks the kit lists; else `failed` with the kit's refusal or usage line, or the log's last line. Nothing uploads or imports: the deck stays in the state folder for the operator.

`status` answers `GET /api/research/roundup/<id>` from the state file, as `publish_drain.status` does; `latest` answers
`GET /api/research/roundup/status` with the last run, or none.
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

__all__ = ["ROUNDUP_SECONDS", "STARTUP", "decks", "latest", "request", "run", "start", "status"]

#: The bound on one build, in seconds: the kit may first fetch python-pptx.
ROUNDUP_SECONDS = 300
#: How long a queued run may wait for its runner to say it started before it reads as failed.
STARTUP = 60
RUN_ID = re.compile(r"roundup-[0-9]{8}T[0-9]{6}Z")
#: Where `sd-research-kit roundup` writes, under its working directory.
ROUNDUP = Path("docs") / "dashboard" / "roundup.pptx"


def state_dir() -> Path:
    """`st_state_dir research-roundup`: what runs write and nobody edits."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "system" / "research-roundup"


def decks(board: dict, root: Path) -> list[Path]:
    """Each listed checkout's deck.json, newest Updated first, then by key; only a checkout `research_screen` serves."""
    from . import research_screen

    rows = [p for p in board.get("projects") or [] if (p.get("experiment") or {}).get("deck")]
    rows.sort(key=lambda p: p["key"])
    rows.sort(key=lambda p: p["experiment"].get("updated") or "", reverse=True)
    found = []
    for row in rows:
        repo = research_screen.checkout(root, row["key"])
        if repo is not None and (repo / research_screen.DECK).is_file() and not (repo / research_screen.DECK).is_symlink():
            found.append(repo / research_screen.DECK)
    return found


def start(inputs: list[Path], *, now: datetime.datetime | None = None, spawn=subprocess.Popen):
    """Steps 1 to 4 above: `(202, run)`, or `(409, {"error"})` while a build holds the lock."""
    from . import pack_bin

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
            return 409, {"error": f"A roundup is already building (run {handle.read().strip() or 'unknown'}); one builds "
                                  f"at a time. Wait for it to finish, then press Build roundup again."}
        if not inputs:
            raise ValueError("No research checkout keeps a 30-brief/deck.json yet: add one beside its status brief.")
        run_id = f"roundup-{now.astimezone(datetime.timezone.utc):%Y%m%dT%H%M%SZ}"
        record = runs / f"{run_id}.json"
        if record.exists() or (runs / run_id).exists():
            return 409, {"error": f"Run {run_id} started this second; press Build roundup again in a moment."}
        here = runs / run_id
        here.mkdir()
        argv = [sys.executable, str(pack_bin() / "sd-research-kit"), "roundup"]
        document = {"id": run_id, "state": "queued", "reason": None, "decks": [str(p) for p in inputs],
                    "out": str(here / ROUNDUP), "argv": argv, "cwd": str(here), "timeout": ROUNDUP_SECONDS,
                    "started": _iso(now), "finished": None, "pid": None}
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
        threading.Thread(target=runner.wait, name=f"research roundup {run_id}", daemon=True).start()
        return 202, public(document)
    finally:
        handle.close()


def request(values: dict, *, board=None):
    """The POST's checks: an empty body; the start as a write, over the decks the board lists now."""
    from . import research_screen

    if values:
        raise ValueError("Send no fields: the roundup takes every deck.json the Experiments view lists.")

    def write(connection):
        found = (board or research_screen.collect)("board")
        return start(decks(found, Path(research_screen._collectors().REPO_ROOT)))

    return write


def public(document: dict) -> dict:
    """What a page reads: the state file less the runner's own fields."""
    return {key: value for key, value in document.items() if key not in ("pid", "argv", "cwd", "timeout")}


def status(run_id: str, *, now: datetime.datetime | None = None):
    """`(status, document)` for one run; a run whose runner is gone without an outcome reads failed."""
    if not RUN_ID.fullmatch(run_id):
        return 404, {"error": "No such roundup run."}
    try:
        document = json.loads((state_dir() / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 404, {"error": "No such roundup run."}
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
        return 503, {"error": f"The last roundup run could not be read: {problem}"}
    if not run_id:
        return 200, {"run": None}
    code, document = status(run_id, now=now)
    return (200, {"run": document}) if code == 200 else (code, document)


def run(record: Path, lock: int | None = None) -> int:
    """The runner: the kit's roundup, then the outcome from its exit and the deck; 0 when the deck was written."""
    document = json.loads(record.read_text(encoding="utf-8"))
    document.update(state="running", pid=os.getpid())
    _write(record, document)
    log = record.with_suffix(".log")
    with open(log, "w", encoding="utf-8") as sink:
        child = subprocess.Popen(document["argv"], stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                                 cwd=document["cwd"], start_new_session=True)
        try:
            code = child.wait(timeout=document["timeout"])
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            code = None
    said = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    # The kit's refusal, or an older pack's usage line, names the problem better than the usage text's last line.
    told = [line for line in said if line.startswith(("sd-research-kit", "usage:"))][:1] or said[-1:]
    # Its success lists each deck it laid out, indented under the `wrote` line.
    used = [line.strip() for line in said if line.startswith("  /")]
    if code is None:
        reason = f"the build did not end within {document['timeout']} s; its process group was killed"
    elif code or not Path(document["out"]).is_file():
        reason = f"sd-research-kit roundup exited {code}" + (f": {told[0][:300]}" if told else "")
    else:
        reason = None
        document["decks"] = used or document["decks"]
    document.update(state="failed" if reason else "done", reason=reason,
                    finished=_iso(datetime.datetime.now(datetime.timezone.utc)))
    if lock is not None:
        os.close(lock)
    _write(record, document)
    return 1 if reason else 0


if __name__ == "__main__":
    # `run <state file> <lock descriptor>`: the descriptor stays open, unread, until this process exits.
    if len(sys.argv) != 4 or sys.argv[1] != "run":
        sys.exit("usage: research_roundup.py run <state file> <lock descriptor>")
    sys.exit(run(Path(sys.argv[2]), int(sys.argv[3])))
