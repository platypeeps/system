"""Run (sd:5886): a button that gave a line to copy runs that line here, and Details shows the output.

Every page posts `/api/run` with `{"action": <name>, ...fields}`. `ACTIONS` is the one registry: each name maps to a
check that reads its fields against what the server holds now and answers one `Step`. The page never sends a
command: a step's argv is fixed text and values the check found -- an installed job, a configured storage folder, a
mounted volume, a port number, a Home Assistant tile. No step runs a shell.

| action             | checked against                              | step                                          |
| ------------------ | -------------------------------------------- | --------------------------------------------- |
| job.log            | an installed job (`operations.job_state`)    | `cron-jobs.sh logs <job> 200`                 |
| job.print          | the same; its launchd service                | `launchctl print <service>`                   |
| storage.du         | a configured storage folder or one inside it | `du -k -d 1 <folder>`, largest first          |
| volume.df          | a mount point or a configured storage folder | `df -h <path>`                                |
| port.lsof          | an integer port                              | `lsof -nP -iTCP:<port> -sTCP:LISTEN`          |
| credentials.probe  | nothing                                      | `credentials.check`, as `sd-db credentials`   |
| check.run          | a Health check row id this module knows      | the row's read, see `CHECKS`                  |
| sensor.read        | a tile in Home's tile list                   | `GET /api/states/<entity>`                    |
| skill.scan         | nothing                                      | `sd skill scan`                               |

A step is bounded (`Step.seconds`), and its output is kept to its last `OUTPUT_CHARS` characters. The answer is
`{action, line, exit, output, cut}` for a step that ran, whatever its exit. A refusal before the step ran is a
`ValueError` or `WorkflowError` (400), a `StaleItem` (409), or `(status, {"error"})`, and ran nothing. A step past its
bound is 504 with `outcome: unknown`: a write may have landed. The command journal records each answered run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

__all__ = ["ACTIONS", "CHECKOUT", "OUTPUT_CHARS", "SECONDS", "Step", "request", "run"]

#: This repository's checkout: `local-cron-jobs` and `local-machine-setup` run from here.
CHECKOUT = Path(__file__).resolve().parents[2]
#: The bound on a step with none of its own, in seconds.
SECONDS = 30
#: The most of a step's output an answer carries: the tail, where a failure says why.
OUTPUT_CHARS = 64 * 1024
#: The lines of a job's log Show log prints.
LOG_LINES = 200
#: The entries Show biggest lists.
BIGGEST = 15
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")


@dataclass(frozen=True)
class Step:
    """What one action runs: `argv` in `cwd` with `stdin`, or `call()` in this process, which answers `(exit, output)`.

    `line` is the command a reader would type for the same thing; Details names it beside the output."""

    line: str
    argv: tuple[str, ...] = ()
    cwd: str | None = None
    stdin: str | None = None
    call: Callable[[], tuple[int, str]] | None = None
    seconds: int = SECONDS
    env: dict | None = None
    #: How the output reads in Details: a step may sort or word it (`du`'s sizes, the largest first).
    shape: Callable[[int, str], str] | None = None


def _fields(values: dict, names: set[str]) -> dict:
    """The action's fields, exactly `names`, each a string unless the check says otherwise."""
    given = set(values) - {"action"}
    if given != names:
        raise ValueError(f"{values['action']} takes {', '.join(sorted(names)) or 'no fields'}; nothing ran.")
    return {name: values[name] for name in names}


def _text(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Name the {what}; nothing ran.")
    return value.strip()


def _command(name: str) -> str:
    from .queue_screen import command

    return command(name)


# ---------- Reads (sd:5886) ----------

def _job(connection, values, jobs):
    """The installed job a row names: `operations.job_state` refuses a name launchd does not run as a trusted job."""
    from sd_db import operations

    name = _text(values["job"], "job")
    if not NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not a job name; nothing ran.")
    return operations.job_state(connection, name, backend=jobs or operations.LaunchdBackend())


def job_log(connection, values, jobs=None) -> Step:
    state = _job(connection, _fields(values, {"job"}), jobs)
    script = CHECKOUT / "local-cron-jobs" / "cron-jobs.sh"
    return Step(line=f"local-cron-jobs/cron-jobs.sh logs {state['name']}",
                argv=("/bin/bash", str(script), "logs", state["name"], str(LOG_LINES)))


def job_print(connection, values, jobs=None) -> Step:
    state = _job(connection, _fields(values, {"job"}), jobs)
    return Step(line=f"launchctl print {state['service']}", argv=("launchctl", "print", state["service"]))


def _storage_roots(connection) -> list[Path]:
    from . import health_collectors

    text, _, problem = health_collectors.storage_list(connection)
    if text is None:
        raise ValueError(f"The storage folders were not read ({problem or 'none configured'}); nothing ran.")
    roots, _ = health_collectors.parse_storage(text)
    return [Path(os.path.realpath(root)) for root in roots]


def _sizes(code: int, out: str) -> str:
    """`du -k -d 1`'s lines, largest first, in human sizes; the folder's own total line leads."""
    rows = []
    for line in out.splitlines():
        size, _, path = line.partition("\t")
        if size.strip().isdigit() and path:
            rows.append((int(size), path))
    if not rows:
        return out
    rows.sort(key=lambda row: -row[0])
    human = lambda kb: next(f"{kb / 1024 ** n:.1f} {unit}" for n, unit in ((3, "TiB"), (2, "GiB"), (1, "MiB"), (0, "KiB"))
                            if kb >= 1024 ** n or n == 0)
    shown = "\n".join(f"{human(kb):>10}  {path}" for kb, path in rows[:BIGGEST + 1])
    return shown + (f"\n... {len(rows) - BIGGEST - 1} more" if len(rows) > BIGGEST + 1 else "")


def storage_du(connection, values, jobs=None) -> Step:
    """A configured storage folder, or a folder directly in one: what Health's storage rows name, never another path."""
    path = _text(_fields(values, {"path"})["path"], "folder")
    real = Path(os.path.realpath(path))
    roots = _storage_roots(connection)
    if not (real in roots or real.parent in roots) or not real.is_dir():
        raise ValueError(f"{path} is not a storage folder Health reads; nothing ran.")
    return Step(line=f"du -sh {path}/* | sort -h | tail -5", argv=("du", "-k", "-d", "1", str(real)), seconds=60,
                shape=_sizes)


def volume_df(connection, values, jobs=None) -> Step:
    path = _text(_fields(values, {"path"})["path"], "volume")
    real = os.path.realpath(path)
    if not os.path.ismount(real) and Path(real) not in _storage_roots(connection):
        raise ValueError(f"{path} is not a mounted volume or a storage folder; nothing ran.")
    return Step(line=f"df -h {path}", argv=("df", "-h", real))


def port_lsof(connection, values, jobs=None) -> Step:
    port = _fields(values, {"port"})["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("A port is a whole number from 1 to 65535; nothing ran.")
    line = f"lsof -nP -iTCP:{port} -sTCP:LISTEN"
    # lsof exits 1 when nothing matches: that is an answer, not a failure.
    return Step(line=line, argv=tuple(line.split()),
                shape=lambda code, out: out if out.strip() else f"Nothing listens on TCP port {port}.")


def _credentials(connection) -> Callable[[], tuple[int, str]]:
    def call():
        from sd_db import credentials

        body = credentials.check(connection, env=os.environ)
        return 0, "\n".join(credentials.describe(probe) for probe in body["probes"])

    return call


def credentials_probe(connection, values, jobs=None) -> Step:
    _fields(values, set())
    return Step(line="sd-db credentials", call=_credentials(connection), seconds=60)


#: Health check rows whose read this module runs, by the id's prefix; every other check's Re-check reads Health again.
CHECKS = {
    "doc:": lambda connection: Step(line="local-machine-setup/machine-setup.sh doctor", seconds=120,
                                    argv=("/bin/sh", str(CHECKOUT / "local-machine-setup" / "machine-setup.sh"), "doctor")),
    "cred:": lambda connection: Step(line="sd-db credentials", call=_credentials(connection), seconds=60),
}


def check_run(connection, values, jobs=None) -> Step:
    row = _text(_fields(values, {"row"})["row"], "check")
    found = next((make for prefix, make in CHECKS.items() if row.startswith(prefix)), None)
    if found is None:
        raise ValueError(f"{row} has no read the dashboard runs; Re-check reads Health again.")
    return found(connection)


def sensor_read(connection, values, jobs=None):
    """One tile's state, as Home reads it after a service call: the tile must be in the tile list."""
    from . import home_screen

    entity = _text(_fields(values, {"entity_id"})["entity_id"], "tile")
    if not home_screen.ENTITY.fullmatch(entity):
        raise ValueError(f"{entity!r} is not an entity id (domain.object_id); nothing ran.")
    try:
        _, text = home_screen._current(connection, home_screen.CONFIG)
    except home_screen._Unread as unread:
        return 503, {"error": f"The tile list was not read: {unread}; nothing ran."}
    headline, groups, _ = home_screen.parse(text or "")
    if entity not in {tile["id"] for tile in ([headline] if headline else []) + [t for g in groups for t in g["tiles"]]}:
        raise ValueError(f"{entity} is not a tile in the tile list; nothing ran.")

    def call():
        try:
            base, token = home_screen._settings(os.environ)
        except home_screen._Failed as failed:
            return 1, failed.reason
        state, unread = home_screen._reread(base, token, entity, home_screen.TIMEOUT)
        return (1, unread) if state is None else (0, json.dumps({"entity_id": entity, **state}, indent=2))

    return Step(line=f'curl -H "Authorization: Bearer $HA_TOKEN" "$HA_URL/api/states/{entity}"', call=call)


def skill_scan(connection, values, jobs=None) -> Step:
    _fields(values, set())
    return Step(line="sd skill scan", argv=(_command("sd"), "skill", "scan"), seconds=120)


#: Every action a page may post, by name: its label for the journal and Details, and its check.
ACTIONS = {
    "job.log": ("Show log", job_log),
    "job.print": ("Print", job_print),
    "storage.du": ("Show biggest", storage_du),
    "volume.df": ("Show use", volume_df),
    "port.lsof": ("Inspect listener", port_lsof),
    "credentials.probe": ("Probe again", credentials_probe),
    "check.run": ("Re-check", check_run),
    "sensor.read": ("Read state", sensor_read),
    "skill.scan": ("Scan", skill_scan),
}


def run(name: str, step: Step) -> tuple[int, dict]:
    """The step, bounded: 200 with its exit and output tail, or 504 when it ran past its bound."""
    answer = {"action": name, "label": ACTIONS[name][0], "line": step.line}
    if step.call is not None:
        code, output = step.call()
    else:
        try:
            done = subprocess.run(list(step.argv), cwd=step.cwd, input=step.stdin if step.stdin is not None else "",
                                  capture_output=True, text=True, timeout=step.seconds, check=False, env=step.env)
            code, output = done.returncode, (done.stdout + (("\n" if done.stdout and done.stderr else "") + done.stderr))
        except subprocess.TimeoutExpired:
            code, output = None, f"It gave no answer in {step.seconds} s; its process was stopped."
        except OSError as error:
            # Nothing started: the program is missing or cannot run, so nothing ran.
            return 503, {**answer, "error": f"{step.argv[0]} did not start: {error}; nothing ran."}
    if code is None:
        return 504, {**answer, "error": output, "outcome": "unknown"}
    if step.shape is not None:
        output = step.shape(code, output)
    output = output.rstrip()
    cut = len(output) > OUTPUT_CHARS
    return 200, {**answer, "exit": code, "output": output[-OUTPUT_CHARS:], "cut": cut}


def request(values: dict, *, jobs=None):
    """`POST /api/run`'s checks before the server opens a writer; the write, as `action_route` takes it."""
    values = dict(values)
    name = values.get("action")
    if not isinstance(name, str) or name not in ACTIONS:
        raise ValueError(f"Choose one of {', '.join(ACTIONS)}; nothing ran.")
    check = ACTIONS[name][1]

    def write(connection):
        step = check(connection, values, jobs)
        return step if isinstance(step, tuple) else run(name, step)

    return write
