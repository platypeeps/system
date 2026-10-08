"""Durable code-delivery receipts; the caller supplies observed GitHub evidence.

The database owns progress. GitHub owns whether a commit was merged. A receipt
does not make an item complete: only progress.deliver_work verifies that claim.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import paths, runner_journal
from .database import connect, served_by, transaction
from .errors import SdDbError
from .workflow import WorkflowError, item_state
from .writes import add_note, now


def identity(connection: sqlite3.Connection, item: int) -> dict:
    row = item_state(connection, item)["item"]
    repo = connection.execute("SELECT * FROM repo WHERE path = ?", (row["repo"],)).fetchone()
    if repo is None:
        raise WorkflowError("code delivery requires an item with a registered repository")
    return {"item": row, "repo": dict(repo)}


def receipt_key(repository: str, branch: str, item: int) -> str:
    return "ship:" + hashlib.sha256(f"{repository}\0{branch}\0{item}".encode()).hexdigest()


def _manual_merge_repository(connection: sqlite3.Connection, repo: str, repository: str | None) -> str:
    """Resolve identity without registering an operator's temporary checkout."""
    from .protection import github_slug
    from .repos import _git, row_for

    registered = row_for(connection, repo)
    if registered is None:
        if repository is None:
            raise WorkflowError("manual merge requires a registered repository or an explicit GitHub repository identity")
        root = paths.expand(repo).resolve()
        observed_root = _git(root, "rev-parse", "--show-toplevel")
        if not observed_root or Path(observed_root).resolve() != root:
            raise WorkflowError("manual merge repository identity requires a readable checkout root")
        # `remote get-url` applies transport rewrites; identity is the configured URL.
        remote = _git(root, "config", "--get", "remote.origin.url")
    else:
        remote = registered["remote"]
    slug = github_slug(remote)
    if slug is None:
        raise WorkflowError("manual merge repository has no verified GitHub remote identity")
    canonical = "/".join(slug).lower()
    if repository is not None and repository.lower() != canonical:
        raise WorkflowError("manual merge repository identity does not match its remote")
    return canonical


def _other_remote_host(remote: str) -> bool:
    """Recognize another transport host without trusting user or path text."""
    try:
        if "://" in remote:
            parsed = urlsplit(remote)
            if parsed.scheme not in {"http", "https", "ssh", "git"}:
                return False
        else:
            authority, separator, _path = remote.partition(":")
            if not separator or "/" in authority or "\\" in authority:
                return False
            parsed = urlsplit("ssh://" + authority)
        # Reject malformed ports as ambiguous instead of guessing a host.
        _port = parsed.port
        host = parsed.hostname
    except ValueError:
        return False
    # Malformed GitHub-looking hosts remain unknown, never unrelated.
    return bool(host and "github.com" not in host.lower())


def manual_merge_guard(connection: sqlite3.Connection, repo: str, *, repository: str | None = None) -> None:
    """Block manual authority while any registered clone retains active ownership."""
    from .protection import github_slug

    canonical = _manual_merge_repository(connection, repo, repository)
    # Item metadata can move or clear during a run. Durable ownership remains
    # bound to the repository claimed by the run and lease until each releases.
    active = connection.execute(
        "WITH ownership(id,status,repo,source) AS ("
        "SELECT a.id, a.status, i.repo, 'assignment' FROM assignment a JOIN item i ON a.item=i.id "
        "WHERE a.status IN ('running','ending') AND i.repo IS NOT NULL "
        "UNION ALL "
        "SELECT run.assignment, a.status, run.repo, 'runner run ' || run.id FROM runner_run run "
        "LEFT JOIN assignment a ON a.id=run.assignment WHERE run.released_at IS NULL "
        "UNION ALL "
        "SELECT run.assignment, a.status, lease.repo, 'runner lease ' || lease.run FROM runner_lease lease "
        "LEFT JOIN runner_run run ON run.id=lease.run LEFT JOIN assignment a ON a.id=run.assignment "
        "WHERE lease.released_at IS NULL) "
        "SELECT ownership.*, r.remote FROM ownership LEFT JOIN repo r ON r.path=ownership.repo "
        "ORDER BY ownership.id, ownership.source, ownership.repo"
    )
    for assignment in active:
        remote = assignment["remote"]
        slug = github_slug(remote)
        if slug is None:
            if not remote or ("github.com" in remote.lower() and not _other_remote_host(remote)):
                raise WorkflowError(f"assignment {assignment['id']} has an unknown repository identity; manual merge authority is held")
            continue
        if "/".join(slug).lower() == canonical:
            held = (f"is {assignment['status']}" if assignment["source"] == "assignment"
                    else f"retains an unreleased {assignment['source']}")
            raise WorkflowError(
                f"assignment {assignment['id']} {held} in repository {canonical}; "
                "it cannot borrow manual merge authority"
            )


def read(connection: sqlite3.Connection, key: str) -> tuple[int, dict]:
    row = connection.execute(
        "SELECT id, body FROM state WHERE kind = 'checkpoint' AND key = ? ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    if row is None:
        return 0, {}
    try:
        value = json.loads(row["body"])
        if not isinstance(value, dict) or value.get("protocol") != 1:
            raise ValueError("unsupported receipt protocol")
    except (TypeError, ValueError) as error:
        raise WorkflowError(f"unreadable ship receipt: {error}") from None
    return row["id"], value


def save(connection: sqlite3.Connection, key: str, previous: int, value: dict) -> int:
    """Append before each side effect, preserving every interrupted phase."""
    with transaction(connection):
        if read(connection, key)[0] != previous:
            raise WorkflowError("ship receipt changed concurrently; reconcile before continuing")
        cursor = connection.execute(
            "INSERT INTO state(kind,key,timestamp,body) VALUES ('checkpoint',?,?,?)",
            (key, now(), json.dumps({**value, "protocol": 1}, sort_keys=True)),
        )
        if cursor.lastrowid is None:
            raise WorkflowError("ship receipt was not stored")
        return cursor.lastrowid


def for_item(connection: sqlite3.Connection, repository: str, item: int, branch: str | None) -> tuple[str, int, dict]:
    """Resolve a watcher receipt without borrowing the operator's current branch."""
    keys = connection.execute(
        "SELECT key FROM state WHERE kind='checkpoint' AND key LIKE 'ship:%' GROUP BY key"
    )
    matches = []
    for row in keys:
        revision, value = read(connection, row["key"])
        if value.get("item") == item and value.get("repository") == repository and (branch is None or value.get("branch") == branch):
            matches.append((row["key"], revision, value))
    if len(matches) != 1:
        raise WorkflowError("item has no unique ship receipt; select its branch before observing")
    return matches[0]


def note_merge(connection: sqlite3.Connection, item: int, evidence: dict) -> None:
    """A slice records its commit once, without changing item status."""
    marker = f"Code delivery {evidence['pull_request']['url']} at {evidence['merge_commit']}"
    with transaction(connection):
        if not connection.execute("SELECT 1 FROM note WHERE item = ? AND body LIKE ?",
                                  (item, marker + "\n%" )).fetchone():
            add_note(connection, item, "comment", marker + "\n" + json.dumps(evidence, sort_keys=True))


def prepare_delivery(connection: sqlite3.Connection, run_id: str, *, verification_root: Path | str) -> dict:
    """Prove delivery before retention, after the runner's merge note is recorded.

    The returned descriptor names library-written evidence. It is not a caller
    supplied success object. Finalization consumes the stored proof under the
    same transaction that releases the owned merge assignment.
    """
    from . import runner
    from .progress import _delivery_evidence
    run = runner.run_state(connection, run_id)
    assignment = runner.queue_state(connection, run["assignment"])
    runner.merge_authority(connection, run_id, run["authored_head"], automatic=assignment["scope"] != "reconcile")
    root = Path(verification_root).resolve()
    if root != Path(run["work_path"]).resolve():
        raise WorkflowError("delivery proof must use the owned merge clone")
    info = identity(connection, assignment["item"])
    item, repo = info["item"], info["repo"]
    if item["kind"] != "work" or repo["status_source"] != "row":
        raise WorkflowError("only database-owned work can prepare a delivery proof")
    key = receipt_key(_github_repository(repo["remote"]), run["branch"], item["id"])
    _, receipt = read(connection, key)
    scope = hashlib.sha256(json.dumps({k: item[k] for k in ("title", "body", "path")}, sort_keys=True).encode()).hexdigest()
    if (receipt.get("phase") != "merged" or not receipt.get("deliver")
            or receipt.get("reviewed_head") != run["authored_head"]
            or receipt.get("acceptance", {}).get("item_scope") != scope):
        raise WorkflowError("owned run has no matching verified whole-item delivery claim")
    initial = item_state(connection, item["id"])
    evidence = _delivery_evidence(item, receipt["merge_commit"], verification_root=root, registered_remote=repo["remote"])
    with transaction(connection):
        if item_state(connection, item["id"])["revision"] != initial["revision"]:
            raise WorkflowError("item changed during delivery proof verification")
        runner.merge_authority(connection, run_id, run["authored_head"], automatic=assignment["scope"] != "reconcile")
        proof = {"run": run_id, "item": item["id"], "repo": item["repo"], "assignment": assignment["id"],
                 "revision": initial["revision"], "evidence": evidence, "verified_at": now(), "protocol": 1,
                 "ship_key": key, "reviewed_head": run["authored_head"]}
        cursor = connection.execute("INSERT INTO state(kind,key,timestamp,body) VALUES ('verified',?,?,?)",
                                    (f"runner-delivery:{run_id}", now(), json.dumps(proof, sort_keys=True)))
        if cursor.lastrowid is None:
            raise WorkflowError("delivery proof was not stored")
        return {"receipt": cursor.lastrowid, "run": run_id, "commit": evidence["commit"]}


def _github_repository(remote: str) -> str:
    from .repos import remote_identity
    value = remote_identity(remote)
    if not value.startswith("github.com/"):
        raise WorkflowError("runner delivery requires the registered GitHub remote")
    return value[len("github.com/"):]


def finalize_delivery(connection: sqlite3.Connection, run_id: str, descriptor: dict) -> dict:
    """Apply a prepared proof inside release's transaction, with no external calls."""
    from . import runner
    from .progress import _complete_delivery
    if not connection.in_transaction:
        raise WorkflowError("delivery finalization belongs to the runner release transaction")
    if not isinstance(descriptor, dict) or descriptor.get("run") != run_id or type(descriptor.get("receipt")) is not int:
        raise WorkflowError("delivery descriptor does not identify this owned run")
    row = connection.execute("SELECT body, resolved_at FROM state WHERE kind='verified' AND key=? AND id=?",
                             (f"runner-delivery:{run_id}", descriptor["receipt"])).fetchone()
    if row is None:
        raise WorkflowError("delivery proof is missing; a descriptor alone proves nothing")
    proof = json.loads(row["body"])
    run = runner.run_state(connection, run_id)
    assignment = runner.queue_state(connection, run["assignment"])
    if (proof["run"] != run_id or proof["assignment"] != assignment["id"] or proof["item"] != assignment["item"]
            or not paths.same_key(proof["repo"], run["repo"]) or descriptor.get("commit") != proof["evidence"]["commit"]
            or run["end_step"] != "released" or not run["released_at"] or not run["retained_path"]
            or run["quarantine"] or assignment["status"] != "done" or assignment["phase"] != "merged"):
        raise WorkflowError("delivery finalization requires its retained, successfully released merge run")
    if row["resolved_at"]:
        return item_state(connection, proof["item"])
    # A verified merge is a historical fact. Time spent retaining the clone
    # cannot invalidate its immutable ancestry witness and stall release.
    _, current = read(connection, proof["ship_key"])
    merged = json.loads(assignment["result"] or "{}")
    if (current.get("phase") != "merged" or current.get("merge_commit") != proof["evidence"]["commit"]
            or current.get("reviewed_head") != proof["reviewed_head"]
            or merged.get("merge_commit") != proof["evidence"]["commit"]
            or merged.get("head") != proof["reviewed_head"]):
        raise WorkflowError("current merge evidence conflicts with the prepared delivery proof")
    state = _complete_delivery(connection, proof["item"], proof["revision"], proof["evidence"], who="runner")
    connection.execute("UPDATE state SET resolved_at=? WHERE id=?", (now(), descriptor["receipt"]))
    return state


#: How often a waiting ship lock retries, in seconds; the deadline bounds the wait.
WAIT_POLL_SECONDS = 0.5
#: The holder record is small; a longer file is not one.
HOLDER_BYTES = 4096
HELD = "another ship operation owns this repository"


#: The directory beside the database that holds one lock file per repository.
LOCK_DIRECTORY = "ship-locks"


class LaneElsewhere(WorkflowError):
    """Another machine runs this repository's lane; nothing was locked (sd:3075).

    `host` is the `repo.lane_host` that runs it, None for the hub. The text
    names the host, then the dashboard control, then the verb, in that order:
    the dashboard is the operator's surface.
    """

    code = "lane_elsewhere"

    def __init__(self, repository: str, host: str | None, path: str | None) -> None:
        self.repository, self.host, self.path = repository, host, path
        name = re.sub(r"^~/(repos/)?", "", path) if path else repository.rpartition("/")[2]
        super().__init__(
            f"The lane for {repository} runs on {host or 'the hub'}, not on this machine.\n"
            f"Run it there, or move the lane: dashboard, Management, {name}, Move lane;\n"
            f"or sd-db.sh repo lane-host {path or '<path>'} {this_host()}.")


def this_host() -> str:
    """This machine's lane host name: `hostname -s`, lower-cased, the `local-cron-jobs` folder rule."""
    return socket.gethostname().split(".")[0].lower()


class LaneUnknown(WorkflowError):
    """This machine cannot tell who runs this repository's lane; nothing was locked (sd:3075).

    A read fault or clone rows that name different hosts. Uncertain ownership
    refuses: granting the hub here could run a second ship beside the lane host's.
    """

    code = "lane_unknown"


def _lane(connection, database, repository: str) -> tuple[bool, str | None, str | None]:
    """`(hosted here, lane host, registered path)` for `repository`, an `owner/name`.

    The rows are the ones whose remote gives `repository` (`protection.github_slug`).
    NULL is the hub; so is no row, and a database without the column, where no
    host can be named. A read fault or clones that disagree raise `LaneUnknown`.
    """
    from .protection import github_slug

    matched = []
    try:
        for row in connection.execute("SELECT * FROM repo ORDER BY path"):
            slug = github_slug(row["remote"])
            if slug is not None and "/".join(slug).lower() == repository.lower():
                matched.append((row["path"], row["lane_host"] if "lane_host" in row.keys() else None))
    except (SdDbError, sqlite3.Error, OSError) as error:
        raise LaneUnknown(
            f"Cannot read the lane host for {repository}: {error}. Nothing was locked; "
            f"retry when the database answers.") from error
    hosts = {host for _, host in matched}
    path = matched[0][0] if matched else None
    if len(hosts) > 1:
        rows = ", ".join(f"{clone} {host or 'hub'}" for clone, host in matched)
        raise LaneUnknown(
            f"The clones of {repository} name different lane hosts: {rows}. Nothing was locked.\n"
            f"Move the lane once to set every clone: dashboard, Management, Move lane;\n"
            f"or sd-db.sh repo lane-host {path} <host>.")
    host = hosts.pop() if hosts else None
    if host is None:
        return served_by(database) is None, None, path
    return host == this_host(), host, path


def hosts_lane(connection, database, repository: str) -> bool:
    """Whether this machine runs `repository`'s lane (sd:3075).

    True when `repo.lane_host` names this machine, or when it is NULL and this
    machine is the hub (`served_by(database)` is None). Raises `LaneUnknown`
    when it cannot tell.
    """
    return _lane(connection, database, repository)[0]


def _owner(database: Path, repository: str) -> None:
    """Refuse unless this machine runs `repository`'s lane, read on a fresh connection."""
    try:
        connection = connect(database, write=False)
    except (SdDbError, sqlite3.Error, OSError) as error:
        raise LaneUnknown(
            f"Cannot open the database to read the lane host for {repository}: {error}. "
            f"Nothing was locked; retry when the database answers.") from error
    try:
        hosted, host, registered = _lane(connection, database, repository)
    finally:
        connection.close()
    if not hosted:
        raise LaneElsewhere(repository, host, registered)


def _lock_directory(database: Path) -> Path:
    """Beside the database on the hub; on a satellite that hosts a lane, `$XDG_STATE_HOME/sd/ship-locks/`.

    Only a repository's lane host takes its lock, so one machine-local lock is enough.
    """
    if served_by(database) is None:
        return Path(database).parent / LOCK_DIRECTORY
    state = os.environ.get("XDG_STATE_HOME", "")
    if not os.path.isabs(state):
        state = str(Path(getattr(database, "sd_home", None) or Path.home()) / ".local" / "state")
    return Path(state) / "sd" / LOCK_DIRECTORY


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_holder(path: Path) -> dict | None:
    """The advisory record in a ship lock file, or None when it holds none.

    The flock is the lock; this record only names who took it. `alive` says
    whether its pid still runs: a record whose pid is gone is what a killed
    holder leaves, and it names nobody. A reused pid can make a stale record
    read as alive; the record is a hint for a person, never a hold.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        raw = os.read(descriptor, HOLDER_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        record = json.loads(raw) if 0 < len(raw) <= HOLDER_BYTES else None
    except ValueError:
        return None
    if not isinstance(record, dict) or type(record.get("pid")) is not int or record["pid"] <= 0:
        return None
    age = None
    try:
        age = max(0, int(time.time() - datetime.fromisoformat(record["started_at"]).timestamp()))
    except (KeyError, TypeError, ValueError):
        pass
    fields = ("pid", "command", "item", "review", "repository", "started_at")
    return {**{name: record.get(name) for name in fields}, "age_seconds": age, "alive": _alive(record["pid"])}


def _holder_text(record: dict | None) -> str:
    if record is None or not record["alive"]:
        return "holder not recorded"
    parts = [f"pid {record['pid']}"]
    if record.get("command"):
        parts.append(str(record["command"]))
    if record.get("item") is not None and f"--item {record['item']}" not in str(record.get("command") or ""):
        parts.append(f"item {record['item']}")
    if record.get("review"):
        parts.append(f"review {record['review']}")
    if record["age_seconds"] is not None:
        parts.append(f"held {record['age_seconds']}s")
    return ", ".join(parts)


def lock_files(database: Path) -> list[dict]:
    """Every ship lock file, as held, stale or idle; nothing here removes one.

    A lock file is a flock target and stays after its holder ends, so a file
    is not a hold (sd:1940). `held` means its record names a live pid; `stale`
    means the record names a pid that is gone; `idle` means no record.
    """
    directory = _lock_directory(database)
    if not directory.is_dir():
        return []
    entries = []
    for path in sorted(directory.glob("*.lock")):
        record = read_holder(path)
        state = "idle" if record is None else "held" if record["alive"] else "stale"
        entries.append({"path": str(path), "state": state, **(record or {})})
    return entries


def held_locks(database: Path) -> list[dict]:
    """The ship locks a live process holds, for `sd runner status`."""
    return [{key: value for key, value in entry.items() if key not in ("state", "alive")}
            for entry in lock_files(database) if entry["state"] == "held"]


def _write_holder(descriptor: int, record: dict) -> None:
    """Replace the record in place; the flock is on this inode, so no rename."""
    data = json.dumps(record, sort_keys=True).encode()[:HOLDER_BYTES]
    os.ftruncate(descriptor, 0)
    os.pwrite(descriptor, data, 0)


@contextmanager
def repository_lock(database: Path, repository: str, *, holder: dict | None = None, wait: float = 0):
    """Serial per remote across clones; never lock or alter an operator checkout.

    Under the flock, the holder writes pid, command, item or review, repository
    and start time into the lock file, and clears it on release. A refusal
    reads it back. `wait` seconds block on the flock, polled every
    WAIT_POLL_SECONDS, then refuse as `wait=0` refuses at once.

    Only the repository's lane host takes it (sd:3075, `hosts_lane`): any
    other machine raises `LaneElsewhere`, and uncertain ownership raises
    `LaneUnknown`; both lock nothing. Ownership is read before the flock and
    again once it is held. The hub's lock file sits beside the database; a
    satellite host's under its state folder.
    """
    if wait < 0:
        raise WorkflowError("ship lock wait must be zero or more seconds")
    # sd:3075: only the lane host takes the lock; any other machine refuses before it locks.
    _owner(database, repository)
    directory = _lock_directory(database)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(repository.encode()).hexdigest() + ".lock")

    def held() -> str:
        waited = f"; waited {wait:g}s" if wait else ""
        return f"{HELD} ({_holder_text(read_holder(path))}){waited}; retry after it finishes"

    with runner_journal.lock(path, blocking=False, noun="ship", error=WorkflowError, held=held,
                             wait=wait, poll=WAIT_POLL_SECONDS) as descriptor:
        # The lane may have moved while this waited on the flock: read it again, and release on a refusal.
        _owner(database, repository)
        given = holder or {}
        record = {"pid": os.getpid(), "repository": repository, "started_at": now(),
                  "command": given.get("command") or " ".join([Path(sys.argv[0]).name, *sys.argv[1:]])[:512],
                  "item": given.get("item"), "review": given.get("review")}
        _write_holder(descriptor, record)
        try:
            yield
        finally:
            os.ftruncate(descriptor, 0)
