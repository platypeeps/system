"""Run (sd:5886, sd:5887): a button that gave a line to copy runs that line here, and Details shows the output.

Every page posts `/api/run` with `{"action": <name>, ...fields}`. `ACTIONS` is the one registry: each name maps to a
check that reads its fields against what the server holds now and answers one `Step`. The page never sends a
command: a step's argv is fixed text and values the check found -- an installed job, a configured storage folder, a
mounted volume, a port number, a Home Assistant tile, a registered repo row, a contribution row. No step runs a shell.

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
| idea.capture       | `notes_screen.quick_text`'s rules            | `sd store add sdw.blog-idea <text> ...`       |
| declaration.write  | a registered repo row, the page's `before`   | `repos.set_declaration`, as `repo declare`    |
| skill.run          | a skill on disk, a registered repo row       | claude `/<skill>` in herdr (`fix_session`)    |
| skill.adopt        | an owner/repo[/path] source                  | claude `/sd-skill-adopt <source>` in herdr    |
| contribution.nudge | a contribution row and its revision          | `gh pr|issue comment <url> --body-file -`     |
| repo.pull          | `pull_refusal`, read again under the lock    | `git pull --ff-only`, under the lane's lock   |

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
#: The longest nudge, in characters: a comment, not a page.
NUDGE_MAX = 4000
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")
SKILL = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
SOURCE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")


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


def _row(connection, key):
    """A registered repo row, by its key or its path."""
    from sd_db import repos

    row = repos.row_for(connection, _text(key, "repository"))
    if row is None:
        raise ValueError(f"{key} is not a registered repository; nothing ran.")
    return row


def _repo(connection, key) -> Path:
    """A registered repo row's checkout on this machine, never another path."""
    from sd_db import paths

    path = paths.disk(_row(connection, key)["path"])
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory on this machine; nothing ran.")
    return path


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


# ---------- Local writes (sd:5887) ----------

def idea_capture(connection, values, jobs=None) -> Step:
    """A blog idea in the vault's inbox, through `sd store add`, as Notes keeps a quick note."""
    import shlex

    from .notes_screen import quick_text

    text = quick_text(_fields(values, {"text"})["text"])
    if text.startswith("-"):
        # The idea is the note's title, a positional argument: a leading dash would read as an option.
        raise ValueError("An idea starts with a letter or digit, not a dash; nothing ran.")
    argv = (_command("sd"), "store", "add", "sdw.blog-idea", text, "--field", "status=inbox")
    return Step(line=f"sd store add sdw.blog-idea {shlex.quote(text)} --field status=inbox", argv=argv)


def declaration_write(connection, values, jobs=None):
    """`sd-db repo declare <path> review <json>`, on the database this server serves.

    `before` is the declaration text the page showed (`""` for none); a row that changed since refuses (409). The
    pack reader's own validator (`sd_lib.declaration_problems`) judges the value, as `bin/sd-db` asks before it writes.
    `value` null removes the kind, as `repo declare ... off` does: that is the Undo of a first declaration."""
    import shlex

    from sd_db import paths, repos, workflow

    from . import pack_module

    given = _fields(values, {"path", "kind", "value", "before"})
    if given["kind"] != "review":
        raise ValueError("Management writes the review declaration only; nothing ran.")
    value, before = given["value"], given["before"]
    if value is not None and not isinstance(value, dict) or not isinstance(before, str):
        raise ValueError("Send the declaration as a JSON object, or null to remove it, and the text the page showed.")
    path = _row(connection, given["path"])["path"]
    shown = repos.declarations(connection, path).get("review")
    if (json.loads(before) if before.strip() else None) != shown:
        raise workflow.StaleItem("The review declaration changed since the page read it; nothing ran. Read it again.")
    if value is not None:
        try:
            sd_lib = pack_module("sd_lib")
        except ImportError as error:
            return 503, {"error": f"The pack's declaration check was not found ({error}); nothing ran. Run the line by hand."}
        problems = sd_lib.declaration_problems("review", {k: v for k, v in value.items() if k != "$schema"})
        if problems:
            raise ValueError("; ".join(problems))
    word = "off" if value is None else json.dumps(value, separators=(",", ":"))

    def call():
        # set_declaration reads the row again in its own transaction; the check above is the page's reading.
        target, was = repos.set_declaration(connection, paths.disk(path), "review", value)
        return 0, f"{target}: review {'none' if was is None else 'declared'} -> {'none' if value is None else 'declared'}"

    return Step(line=f"sd-db repo declare {shlex.quote(given['path'])} review {shlex.quote(word)}", call=call)


def _skill_on_disk(name: str) -> bool:
    from . import pack_library

    pack = pack_library().parent
    return any((folder / name / "SKILL.md").is_file()
               for folder in (pack / "skills", pack / "contrib", Path.home() / ".claude" / "skills"))


def _herdr(directory: Path, label: str, text: str) -> Callable[[], tuple[int, str]]:
    def call():
        from . import fix_session

        pane = fix_session._launch(directory, label, text)
        if isinstance(pane, tuple):
            status, said = pane
            # _launch words its refusal for Fix; the action's own label leads in Details.
            return (None if status == 504 else 1), said["error"].removeprefix("Fix failed: ")
        return 0, f"Started claude in herdr workspace {label}, pane {pane}, in {directory}."

    return call


def skill_run(connection, values, jobs=None) -> Step:
    """Run: claude in a new herdr workspace in the chosen registered checkout, given `/<skill>` as typed text."""
    given = _fields(values, {"skill", "repo"})
    name = _text(given["skill"], "skill")
    if not SKILL.fullmatch(name) or not _skill_on_disk(name):
        raise ValueError(f"{name!r} is not an installed skill; nothing ran.")
    directory = _repo(connection, given["repo"])
    return Step(line=f"cd {directory} && claude '/{name}'", call=_herdr(directory, f"skill-{name}", f"/{name}"),
                seconds=100)


def skill_adopt(connection, values, jobs=None) -> Step:
    """Adopt: claude runs `/sd-skill-adopt <owner/repo[/path]>` in a new herdr workspace in the pack checkout."""
    from . import pack_library

    source = _text(_fields(values, {"source"})["source"], "source")
    source = re.sub(r"^https://github\.com/", "", source)
    source = re.sub(r"/(tree|blob)/[^/]+", "", source).rstrip("/")
    if not SOURCE.fullmatch(source):
        raise ValueError("Adopt takes owner/repo or owner/repo/path, or its github.com URL; nothing ran.")
    pack = pack_library().parent
    return Step(line=f"cd {pack} && claude '/sd-skill-adopt {source}'", call=_herdr(pack, "skill-adopt", f"/sd-skill-adopt {source}"),
                seconds=100)


def contribution_nudge(connection, values, jobs=None) -> Step:
    """Post nudge: the operator's comment on the contribution's own pull request or issue, at the revision the page read.

    The URL comes from the row's key, never from the page; the body goes on stdin, never into the argv."""
    from sd_db import contributions

    from .issue_filing import GH

    given = _fields(values, {"key", "revision", "body"})
    key, body = _text(given["key"], "contribution"), given["body"]
    if not isinstance(body, str) or not body.strip():
        raise ValueError("The nudge is empty; nothing was posted.")
    if len(body) > NUDGE_MAX:
        raise ValueError(f"A nudge holds at most {NUDGE_MAX} characters; this one has {len(body)}. Nothing was posted.")
    canonical = contributions._key(key)
    url = contributions._observed_url(canonical)
    if url is None:
        raise ValueError("Only a contribution filed on GitHub takes a nudge; nothing was posted.")
    state = contributions._checked(connection, canonical, given["revision"])  # StaleItem (409) when it changed
    if state["observation"] is None:
        raise ValueError(f"{url} has not been observed; nothing was posted.")
    noun = "issue" if canonical.startswith("issue:") else "pr"
    return Step(line=f"gh {noun} comment {url} --body-file -", argv=("sh", "-c", GH, "sh", noun, "comment", url, "--body-file", "-"),
                stdin=body.strip() + "\n", seconds=60)


# ---------- Pull (sd:5888) ----------

def _git(path: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(path), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _pinned() -> list[Path]:
    """The hub's pinned checkouts: this repository's and the pack's, which only the lane or repo-sync move (sd:3097)."""
    from . import pack_library

    found = [CHECKOUT, pack_library().parent]
    for checkout in list(found):
        common = _git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common is not None and common.returncode == 0:
            found.append(Path(common.stdout.strip()).parent)
    return [Path(os.path.realpath(path)) for path in found]


def pull_refusal(path: Path) -> str:
    """Why Pull does not run in `path`, or "": a check that gave no answer refuses, since its answer is not known."""
    if Path(os.path.realpath(path)) in _pinned():
        return f"{path} is a pinned checkout: only the lane or local-repo-sync/repo-sync.sh refresh moves it"
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    own = _git(path, "rev-parse", "--path-format=absolute", "--git-dir")
    if common is None or own is None or common.returncode or own.returncode:
        return "git did not say whether this is the main checkout, so it is not known"
    if common.stdout.strip() != own.stdout.strip():
        return f"{path} is a linked worktree: a builder or a session holds it"
    branch = _git(path, "symbolic-ref", "-q", "--short", "HEAD")
    if branch is None:
        return "git did not say which branch is checked out"
    if branch.returncode:
        return "HEAD is detached: Pull moves only the default branch"
    default = _git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if default is None or default.returncode or not default.stdout.strip().startswith("origin/"):
        return "origin/HEAD is not set, so the default branch is not known"
    name = default.stdout.strip().removeprefix("origin/")
    if branch.stdout.strip() != name:
        return f"it is on {branch.stdout.strip()}, not the default branch {name}"
    status = _git(path, "status", "--porcelain")
    if status is None or status.returncode:
        return "git status gave no answer, so whether the tree is clean is not known"
    if status.stdout.strip():
        count = len(status.stdout.splitlines())
        return f"{count} uncommitted or untracked {'file' if count == 1 else 'files'}"
    return ""


def _lane_lock(path: Path):
    """The lane's runner lock for this checkout, held now; None when this machine has no lane folder for it.

    `BlockingIOError` when a lane holds it. The lock is the one `sd_lane.runner_lock` takes, never waited for."""
    import fcntl

    from . import pack_module

    lane = pack_module("sd_lane")
    lock = lane.lane_dir(path, dict(os.environ)) / "queue" / "runner.lock"
    if not lock.exists():
        return None
    handle = open(lock, "a", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


def repo_pull(connection, values, jobs=None) -> Step:
    """`git pull --ff-only` in a registered checkout on its default branch, clean, and held by no lane or worktree.

    Every check runs here, just before the pull, whatever the page read; a refusal is a ValueError (400), and nothing
    ran. The lane's runner lock is held across the checks and the pull, so a lane that starts meanwhile finds it held
    and tries again at its next tick."""
    path = _repo(connection, _fields(values, {"path"})["path"])

    def call():
        try:
            held = _lane_lock(path)
        except BlockingIOError:
            raise ValueError(f"No pull: the lane runs in {path} now, and it fast-forwards the checkout after it lands.") from None
        except (ImportError, OSError) as error:
            raise ValueError(f"No pull: whether a lane holds {path} is not known ({error}).") from None
        try:
            why = pull_refusal(path)
            if why:
                raise ValueError(f"No pull: {why}.")
            done = subprocess.run(["git", "-C", str(path), "pull", "--ff-only"], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=110, check=False,
                                  env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            return done.returncode, (done.stdout + done.stderr).strip()
        except subprocess.TimeoutExpired:
            return None, f"git pull gave no answer in 110 s, so whether {path} moved is not known. Read Repos again."
        finally:
            if held is not None:
                held.close()

    return Step(line=f"git -C {path} pull --ff-only", call=call, seconds=120)


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
    "idea.capture": ("Capture idea", idea_capture),
    "declaration.write": ("Write declaration", declaration_write),
    "skill.run": ("Run skill", skill_run),
    "skill.adopt": ("Adopt skill", skill_adopt),
    "contribution.nudge": ("Post nudge", contribution_nudge),
    "repo.pull": ("Pull", repo_pull),
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
