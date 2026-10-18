"""Management: the document behind the default UI's Management page (sd:2118).

The page is `v2/management.html`; it holds no rows. It reads one JSON
document, `/api/management`, built here from reads v1 already makes, each
source guarded on its own so one that fails is a reason and not an empty list
(`sources` holds the empty string for a source that was read):

- `repos`: `repos.registered`, the sd-db repo table, with each checkout's
  `.github/sd-review.json` read from disk and the protection reading
  `protection.rows` gives v1 /protection (the nightly collector's; nothing
  here calls GitHub).
- `pulls`: each GitHub repository's open pull requests, by lower-cased
  `owner/name`, from the shadow rows `sd shadow sync` keeps
  (`progress.tracker_items`, as Trackers reads them): the repo row's
  Adversarial review: PR picks one (sd:5100). Nothing here calls GitHub.
- `deep`: the Deep review's settings, `adversarial.deep_settings`: each
  setting row's value and whether it is the row's or the default (sd:5109).
- `git`: `fleet.collect("repos")`, v1 Operations > Repos, each row with
  `repos_screen.primary`'s state, headline, detail and remedy. Nothing fetches
  or pulls.
- `lane`: queued and running assignments, the latest merge assignments, and
  the items waiting at `ready_to_send`.
- `assignments`: the latest assignments and the history counted by status.
- `sessions`: `fleet.collect("sessions")`, v1 Operations > Sessions:
  worktree counts and the sd-* processes it lists.
- `services` and `jobs`: `services.inventory` and `operations.inventory`,
  v1 Operations > Services and > Jobs, with the revision each write sends.
  Each job carries `last_run`, read from `<cron_root>/logs/.<job>.stamp`, the
  start, end and exit the cron-jobs wrapper writes for every run (sd:2210):
  launchd keeps no run time, and a log's write time is not one. Each job
  also carries `log`, the end of `<cron_root>/logs/<job>.log` (`job_log`),
  and `last_request`, the last retry or cancel the page or the CLI sent and
  what launchctl answered, as v1 Operations > Jobs showed it.

Each repo also carries the overview's columns (sd:1629): its required checks
and `strict` from the protection reading, and `runtimes`, the Python and Node
versions its checkout pins (`_runtimes`). `dependabot` is the row's consent
for the daily Dependabot sweep, and `dependabot_gate` names the sweep's owner
rule when it decides first (`denied`, `owner`), read from
`local-dependabot/holds.py` (sd:3984).

Every write the page makes goes through a route `server.action_route`
answers: job retry and cancel, service start, stop
and restart, and the sd-db repo verbs (`repos.set_runner_merge`,
`repos.set_managed`, `repos.set_lane_host`, `repos.set_dependabot`, `repos.set_github_issues`), which refuse a stale `before`
and, on a satellite, refuse with `HubOnly` (sd:1629, sd:3075).
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import sqlite3
import stat
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from sd_db import channels, database, operations, progress, protection, reads, repos, workflow
from sd_db.errors import SdDbError

from . import adversarial, fix_session
from . import fleet as fleet_module
from .repos_screen import primary

__all__ = ["document", "job_log", "last_run", "log_tail", "set_repo"]

#: How many assignments and merges the page lists; the history counts every row.
LATEST = 40
MERGES = 12
#: The largest sd-review.json the page shows in full.
REVIEW_BYTES = 16384
FAILURES = (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error)
#: The largest run stamp read; the wrapper writes three short lines.
STAMP_BYTES = 512
#: The largest pin file read; a version file is one line, a manifest a few KiB.
PIN_BYTES = 65536
#: How much of a job's log the page shows, from its end: where a failing run says why.
LOG_BYTES = 4096


def _review(path: str) -> dict | None:
    """The checkout's `.github/sd-review.json`: None when the checkout is not on disk, `file: None` when absent."""
    root = Path(path).expanduser()
    if not root.is_dir():
        return None
    target = root / ".github" / "sd-review.json"
    try:
        raw = target.read_bytes()[:REVIEW_BYTES + 1]
    except FileNotFoundError:
        return {"file": None}
    except OSError as error:
        return {"file": None, "error": f"unreadable: {error.strerror or error}"}
    text = raw[:REVIEW_BYTES].decode("utf-8", "replace")
    try:
        body = json.loads(raw)
    except ValueError:
        return {"file": text, "error": "not valid JSON" if len(raw) <= REVIEW_BYTES else "too large to show"}
    except RecursionError:
        # A pure-Python JSON scanner recurses per nesting level (sd:2911); this repo's file, not the document, fails.
        return {"file": text, "error": "nests too deeply to read"}
    body = body if isinstance(body, dict) else {}
    copilot = body.get("copilot_review") if isinstance(body.get("copilot_review"), dict) else {}
    schema = body.get("$schema") if isinstance(body.get("$schema"), str) else None
    floor, deep = body.get("severity_floor"), copilot.get("automatic_deep")
    # Only the types the page renders pass; any other value is this repo's error, never a value (sd:1629).
    wrong = [words for value, kind, words in ((floor, str, "severity_floor is not a string"),
                                              (deep, bool, "copilot_review.automatic_deep is not true or false"))
             if value is not None and not isinstance(value, kind)]
    out = {"file": text, "severity_floor": floor if isinstance(floor, str) else None,
           "automatic_deep": deep if isinstance(deep, bool) else None, "schema": schema}
    return out | {"error": "; ".join(wrong)} if wrong else out


def _protection(row: dict) -> dict:
    detail = row.get("detail") or {}
    required = detail.get("required_contexts")
    names = isinstance(required, list) and all(isinstance(name, str) for name in required)
    return {"status": row["status"], "observed_at": row["observed_at"], "default_branch": row["default_branch"],
            "reason": row["reason"], "gaps": [{"id": gap.get("id"), "gap": gap.get("gap")} for gap in row["gaps"]],
            # An older installed `sd_db` returns no `borrowed_from` (sd:1607): its row is the checkout's own.
            "borrowed_from": row.get("borrowed_from"),
            # None when no observation names them, which is not "none required" (sd:1629).
            "required": required if names else None,
            "required_error": "a required check is not a name" if isinstance(required, list) and not names else None,
            "strict": detail.get("strict") is True}


def _pin_text(root: Path, name: str) -> str | None:
    """A pin file's text, or None when it is absent; ValueError when it cannot be read.

    Opened without blocking and checked to be a regular file before any read,
    then read to `PIN_BYTES` + 1 at most (sd:2911): a FIFO, or a link to one,
    would hang `/api/management`, and a huge file would be loaded whole.
    """
    try:
        fd = os.open(root / name, os.O_RDONLY | os.O_NONBLOCK)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as error:
        raise ValueError(f"{name} is unreadable: {error.strerror or error}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{name} is not a regular file")
        raw = b""
        while len(raw) <= PIN_BYTES:
            part = os.read(fd, PIN_BYTES + 1 - len(raw))
            if not part:
                break
            raw += part
    except OSError as error:
        raise ValueError(f"{name} is unreadable: {error.strerror or error}") from None
    finally:
        os.close(fd)
    if len(raw) > PIN_BYTES:
        raise ValueError(f"{name} is larger than a pin file")
    return raw.decode("utf-8", "replace")


def _first_line(root, name):
    lines = (_pin_text(root, name) or "").strip().splitlines()
    return lines[0].strip() if lines else None


def _tool_versions(root, tool):
    for line in (_pin_text(root, ".tool-versions") or "").splitlines():
        words = line.split("#")[0].split()
        if len(words) > 1 and words[0] == tool:
            return words[1]
    return None


def _requires_python(root):
    text = _pin_text(root, "pyproject.toml")
    if text is None:
        return None
    try:
        project = tomllib.loads(text).get("project")
    except tomllib.TOMLDecodeError:
        raise ValueError("pyproject.toml is not valid TOML") from None
    except RecursionError:
        # tomllib recurses per nesting level; a file under PIN_BYTES can pass the interpreter's limit (sd:2911).
        raise ValueError("pyproject.toml nests too deeply to read") from None
    value = project.get("requires-python") if isinstance(project, dict) else None
    return value if isinstance(value, str) else None


def _engines_node(root):
    text = _pin_text(root, "package.json")
    if text is None:
        return None
    try:
        body = json.loads(text)
    except ValueError:
        raise ValueError("package.json is not valid JSON") from None
    except RecursionError:
        raise ValueError("package.json nests too deeply to read") from None
    engines = body.get("engines") if isinstance(body, dict) else None
    value = engines.get("node") if isinstance(engines, dict) else None
    return value if isinstance(value, str) else None


#: Where a checkout pins each runtime, first match wins: the version managers' files, then the manifest's range.
PINS = {
    "python": ((".python-version", lambda root: _first_line(root, ".python-version")),
               (".tool-versions", lambda root: _tool_versions(root, "python")),
               ("pyproject.toml requires-python", _requires_python)),
    "node": ((".node-version", lambda root: _first_line(root, ".node-version")),
             (".nvmrc", lambda root: _first_line(root, ".nvmrc")),
             (".tool-versions", lambda root: _tool_versions(root, "nodejs")),
             ("package.json engines.node", _engines_node)),
}


def _runtimes(path: str) -> dict | None:
    """The Python and Node versions the checkout pins (sd:1629): None when it is not on disk.

    Each runtime is `{"value", "source"}`, `{"error"}` when a file that could
    name it cannot be read, or None when nothing pins it.
    """
    root = Path(path).expanduser()
    if not root.is_dir():
        return None
    out = {}
    for runtime, sources in PINS.items():
        out[runtime] = None
        for source, read in sources:
            try:
                value = read(root)
            except ValueError as error:
                out[runtime] = {"error": str(error)}
                break
            if value:
                out[runtime] = {"value": value, "source": source}
                break
    return out


def _repos(connection) -> list[dict]:
    guarded = {row["repo"]: _protection(row) for row in protection.rows(connection)}
    out = []
    for row in repos.registered(connection):
        out.append({
            "path": row["path"], "remote": row["remote"] or "", "mode": row["mode"], "ci": row["ci"],
            "runner_merge": row["runner_merge"], "managed": "yes" if row["managed"] else "no",
            # sd:3075: NULL is the hub; a database before migration 24 has no column and reads as the hub.
            "lane_host": (row["lane_host"] if "lane_host" in row.keys() else None) or repos.LANE_HUB,
            "pieces_source": row["pieces_source"],
            # sd:3465: the canonical text the form posts back as `before`, `none` for no setting, and why a stored value
            # cannot be used, which the form shows beside a Clear.
            "channels": channels.canonical(row["channels"]), "channels_problem": _channels_problem(row["channels"]),
            # sd:3984: a store before migration 33 has no column, and an unread flag is unknown, not no.
            "dependabot": ("yes" if row["dependabot"] else "no") if "dependabot" in row.keys() else None,
            "dependabot_gate": _dependabot_gate(row["remote"]),
            # sd:4840: a store before migration 35 has no column; an unread setting is unknown, not off.
            "github_issues": ("on" if row["github_issues"] else "off") if "github_issues" in row.keys() else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "review": _review(row["path"]), "protection": guarded.get(row["path"]), "runtimes": _runtimes(row["path"]),
        })
    return out


def _pulls(connection) -> dict:
    """`owner/name` lower-cased -> its open pull requests, newest seen first, as `sd shadow sync` last saw them."""
    out: dict = {}
    for row in progress.tracker_items(connection, tracker="github"):
        tail = (row["url"] or "").rpartition("/")[2]
        number = row["number"] if type(row["number"]) is int else int(tail) if tail.isdigit() else None
        if (row["kind"] or "") != "pull" or not row["repo"] or number is None:
            continue
        out.setdefault(row["repo"].lower(), []).append(
            {"number": number, "title": row["title"] or "", "author": row["author"] or "", "url": row["url"]})
    return out


def _channels_problem(text: str | None) -> str | None:
    """Why a stored channels value is not a setting `channels.validate` accepts; None for NULL or a good one."""
    if text is None:
        return None
    try:
        channels.validate(json.loads(text))
    except (ValueError, SdDbError) as error:
        return str(error)
    return None


@functools.cache
def _holds():
    """`local-dependabot/holds.py`, loaded by path as `active_sessions` loads herdr's: its owner gate is the sweep's."""
    spec = importlib.util.spec_from_file_location("sd_dashboard_dependabot_holds",
                                                  fix_session.CHECKOUT / "local-dependabot" / "holds.py")
    if spec is None or spec.loader is None:
        raise ValueError("local-dependabot/holds.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up in sys.modules while the class is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _dependabot_gate(remote: str | None) -> str | None:
    """`denied` or `owner` when the sweep's owner gate decides before the row's flag (sd:3984), else None."""
    found = repos.parse_remote(remote)
    if found.kind != "github":
        return None
    holds = _holds()
    slug = f"{found.owner}/{found.name}"
    return "denied" if slug in holds.DENIED else "owner" if found.owner in holds.ALLOWED_OWNERS else None


def _seen(document: dict) -> dict:
    """When the fleet snapshot read `document` and why its newest read failed; empty for a direct read (sd:TBD)."""
    return {"observed": document.get("observed"), "stale": document.get("stale")} if "observed" in document else {}


def _git(read, now: str) -> dict:
    document = read("repos")
    rows = document.get("repos") if isinstance(document, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("path"), str) for row in rows):
        raise ValueError("fleet collector returned an incomplete repos document")
    when = datetime.fromisoformat(now.replace("Z", "+00:00")) if now else datetime.now(timezone.utc)
    out = []
    for row in rows:
        state, headline, detail, remedy = primary(row, when)
        out.append({key: row.get(key) for key in ("name", "path", "branch", "default", "dirty", "ahead", "behind",
                                                   "behind_default", "fetched_iso", "last_iso", "subject", "error")}
                   | {"truncated": list(row.get("truncated") or []), "state": state, "headline": headline,
                      "detail": detail, "remedy": remedy})
    return {"root": document.get("root"), "counts": document.get("counts") or {}, "repos": out} | _seen(document)


def _assignment(connection, row) -> dict:
    return {"id": row["id"], "item": row["item"], "title": row["title"], "repo": row["repo"], "role": row["role"],
            "provider": row["provider"], "status": row["status"], "lane": row["lane"], "queued_at": row["queued_at"],
            "started": row["started"], "ended": row["ended"]}


def _lane(connection) -> dict:
    live = [_assignment(connection, row) for row in reads.assignment_ledger(connection, live=True)]
    merges = [_assignment(connection, row) for row in reads.assignment_ledger(connection, role="merge", limit=MERGES)]
    ready = [dict(row) for row in reads.ready_to_send(connection)]
    return {"live": live, "merges": merges, "ready": ready}


def _assignments(connection) -> dict:
    latest = [_assignment(connection, row) for row in reads.assignment_ledger(connection, exclude_role="merge", limit=LATEST)]
    return {"latest": latest, "history": reads.assignment_counts(connection)}


def _sessions(read) -> dict:
    document = read("sessions")
    trees = document.get("worktrees") if isinstance(document, dict) else None
    procs = document.get("processes") if isinstance(document, dict) else None
    if not isinstance(trees, list) or not isinstance(procs, list) or any(
            not isinstance(tree, dict) or not isinstance(tree.get("live"), bool) for tree in trees):
        raise ValueError("fleet collector returned an incomplete sessions document")
    abandoned = sum(1 for tree in trees if not tree["live"])
    return {"registered": len(trees), "abandoned": abandoned,
            "processes": [{key: proc.get(key) for key in ("pid", "elapsed", "command")} for proc in procs if isinstance(proc, dict)],
            "processes_error": document.get("processes_error") or ""} | _seen(document)


def _services(connection, backend) -> list[dict]:
    from sd_db import services

    keep = ("name", "label", "scope", "domain", "category", "state", "pid", "last_exit", "last_signal", "revision")
    return [{key: entry.get(key) for key in keep} | {"capabilities": entry.get("capabilities") or {}}
            for entry in services.inventory(connection, backend=backend)["services"]]


def _time(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def last_run(cron_root, name: str) -> dict:
    """The job's last run, from the stamp the cron-jobs wrapper writes beside its log (sd:2210).

    `state` is `finished` (a start, an end and an exit code), `open` (a start
    and no end: a run in progress, or one no trap saw end), `none` (no stamp:
    the job has not run since the wrapper began writing one) or `unread`;
    `reason` says why for the last two.
    """
    out = {"state": "unread", "started": None, "ended": None, "exit": None, "reason": ""}
    if not isinstance(cron_root, (str, Path)):
        return out | {"reason": "the jobs backend names no logs folder"}
    try:
        raw = (Path(cron_root) / "logs" / f".{name}.stamp").read_bytes()[:STAMP_BYTES + 1]
    except FileNotFoundError:
        return out | {"state": "none", "reason": "no run stamp: the job has not run since its wrapper began writing one"}
    except OSError as error:
        return out | {"reason": f"the run stamp is unreadable: {error.strerror or error}"}
    if len(raw) > STAMP_BYTES:
        return out | {"reason": "the run stamp is larger than the wrapper writes"}
    fields = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = value
    started, ended, code = fields.get("started"), fields.get("ended"), fields.get("exit")
    if not _time(started):
        return out | {"reason": "the run stamp names no start time"}
    if ended is None and code is None:
        return out | {"state": "open", "started": started}
    if not _time(ended):
        return out | {"reason": "the run stamp has an exit code without an end time"}
    if code is None or not code.isdigit():
        return out | {"reason": "the run stamp has an end without an exit code"}
    return out | {"state": "finished", "started": started, "ended": ended, "exit": int(code)}


def job_log(cron_root, name: str) -> dict:
    """The end of the job's log, `<cron_root>/logs/<job>.log`, the file `cron-jobs.sh` appends every run to.

    `state` is `read`, `none` (no log yet) or `unread`, with `reason` for the
    last two. `text` is the last `LOG_BYTES` at most, from the first whole
    line in them; `cut` says the log is longer. The file is opened without
    blocking and checked to be a regular file before any read, as a pin file
    is (sd:2911): a FIFO in its place would hang `/api/management`.
    """
    out = {"state": "unread", "text": "", "cut": False, "reason": ""}
    if not isinstance(cron_root, (str, Path)):
        return out | {"reason": "the jobs backend names no logs folder"}
    if not isinstance(name, str) or not operations.JOB_NAME.fullmatch(name):
        return out | {"reason": "the job name is not one a log file can have"}
    try:
        _, raw, start = log_tail(Path(cron_root) / "logs" / f"{name}.log", LOG_BYTES)
    except FileNotFoundError:
        return out | {"state": "none", "reason": "no log: the job has not run through the cron-jobs wrapper"}
    except OSError as error:
        return out | {"reason": unreadable(error)}
    return out | {"state": "read", "text": raw.decode("utf-8", "replace"), "cut": bool(start)}


class NotRegular(OSError):
    """A log path that holds something other than a regular file, such as a FIFO or a folder."""


def unreadable(error: OSError) -> str:
    """Why a log was not read, in the words Management and Reports both show."""
    if isinstance(error, NotRegular):
        return "the log is not a regular file"
    return f"the log is unreadable: {error.strerror or error}"


def log_tail(path: Path, limit: int) -> tuple[bytes, bytes, int]:
    """A job log's first line, its last `limit` bytes from the first whole line in them, and the offset read from.

    Management's log and Reports' cadence read through it (sd:3532). The file is opened without blocking and checked
    to be a regular file before any read, as a pin file is (sd:2911): a FIFO in its place would hang the page. A log
    that is not there raises `FileNotFoundError`; one that is not a regular file, `NotRegular`; any other failed read,
    its `OSError`.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode):
            raise NotRegular(f"{path} is not a regular file")
        first = os.pread(fd, 4096, 0).partition(b"\n")[0]
        start = max(0, found.st_size - limit)
        os.lseek(fd, start, os.SEEK_SET)
        raw = b""
        while len(raw) < limit:
            part = os.read(fd, limit - len(raw))
            if not part:
                break
            raw += part
    finally:
        os.close(fd)
    if start:
        # The read began part way through a line: whole lines only.
        raw = raw.partition(b"\n")[2]
    return first, raw, start


def _jobs(connection, backend) -> list[dict]:
    keep = ("name", "label", "service", "schedule", "state", "pid", "last_exit", "last_signal", "revision", "capabilities",
            "last_request")
    # `cron_root` places the logs folder, as Activity and Now read it; a backend without one still lists its jobs.
    root = getattr(backend, "cron_root", None)
    return [{key: job.get(key) for key in keep} | {"last_run": last_run(root, job.get("name") or ""),
                                                   "log": job_log(root, job.get("name") or "")}
            for job in operations.inventory(connection, backend=backend)["jobs"]]


def document(connection: sqlite3.Connection, *, now: str, fleet=None, jobs=None, services=None) -> dict:
    """Every source the page reads, and the reason for each one that could not be read.

    `fleet` is `fleet.collect`'s shape, the seam a test fills; `jobs` and
    `services` are the operations and services backends, the launchd ones
    by default.
    """
    read = fleet or fleet_module.collect
    out: dict = {"read": now, "sources": {}}
    for source, collect in (("repos", lambda: _repos(connection)),
                            ("pulls", lambda: _pulls(connection)),
                            ("deep", lambda: adversarial.deep_settings(connection)),
                            ("git", lambda: _git(read, now)),
                            ("lane", lambda: _lane(connection)),
                            ("assignments", lambda: _assignments(connection)),
                            ("sessions", lambda: _sessions(read)),
                            ("services", lambda: _services(connection, services)),
                            ("jobs", lambda: _jobs(connection, jobs or operations.LaunchdBackend()))):
        try:
            out[source] = collect()
        except FAILURES as failure:
            out[source] = None
            out["sources"][source] = str(failure) or f"{source} could not be read"
            continue
        out["sources"][source] = ""
    out["attention"] = _attention(out["jobs"])
    return out


def _attention(jobs) -> dict:
    """The rail badge (sd:3446): scheduled jobs whose last run failed. A jobs read that failed is unknown, not none failed."""
    if jobs is None:
        return {"state": "unknown", "n": 0, "what": "scheduled jobs not read"}
    failed = sum(job.get("state") == "failed" for job in jobs)
    return {"state": "warning" if failed else "ok", "n": failed, "what": "scheduled jobs failed"}


#: The sd-db repo verbs the page runs: the field, the library call, and how the row reads the value back.
SETTERS = {"runner-merge": (repos.set_runner_merge, "runner_merge"), "managed": (repos.set_managed, "managed"),
           "lane-host": (repos.set_lane_host, "lane_host"), "channels": (repos.set_channels, "channels"),
           "dependabot": (repos.set_dependabot, "dependabot"), "github-issues": (repos.set_github_issues, "github_issues")}


class StaleSetting(workflow.StaleItem):
    """The row no longer holds the value the page showed."""


def set_repo(connection: sqlite3.Connection, field: str, path: str, value, before: str) -> dict:
    """One sd-db repo verb, refused when the row moved since the page read it; returns the new setting.

    The read, the check and the write hold one `BEGIN IMMEDIATE`: the server
    answers requests on threads, and two that read the same old value must
    not both write.
    """
    setter, column = SETTERS[field]
    # The runner that reads these columns runs on the hub; a satellite's page reads them and changes nothing (sd:1629).
    database.refuse_hub_only(connection, f"repo {field}")
    if field == "lane-host":
        # sd:3075: the move commits under the ship lock, so it holds its own transaction and checks `before` in it.
        path, was = setter(connection, path, value, before=before)
        return {"path": path, "field": column, "value": value, "before": was}
    if field == "channels":
        # sd:3465: `set_channels` validates the object and checks `before` inside its own write transaction.
        # sd:4632: the library keeps the inbound keys a set omits, so the row, not the posted object, is the new setting
        # that the form's Undo compares against. The read-back shares the write's transaction: another writer between
        # them would hand this form its setting as the Undo's `before`. A row that cannot be read back is unknown.
        with workflow.transaction(connection):
            path, was = setter(connection, path, value, before=before)
            row = repos.row_for(connection, path)
        if row is None:
            raise repos.RepoRefusal(f"{path} was written but cannot be read back; read the page again")
        return {"path": path, "field": column, "value": channels.canonical(row[column]),
                "before": was, "remedy": f"Run `sd-research-kit render` in {path} to publish under it, or wait for the next commit."}
    with workflow.transaction(connection):
        row = repos.row_for(connection, path)
        if row is None:
            raise repos.RepoRefusal(f"{path} is not a registered repository")
        current = (("yes" if row[column] else "no") if column in ("managed", "dependabot")
                   else ("on" if row[column] else "off") if column == "github_issues" else row[column])
        if current != before:
            raise StaleSetting(f"{column} for {path} is {current} now, not {before}; read the page again")
        _, was = setter(connection, row["path"], value)
    return {"path": row["path"], "field": column, "value": value, "before": was}
