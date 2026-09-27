"""Durable code-delivery receipts; the caller supplies observed GitHub evidence.

The database owns progress. GitHub owns whether a commit was merged. A receipt
does not make an item complete: only progress.deliver_work verifies that claim.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from . import paths, runner_journal
from .database import transaction
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


@contextmanager
def repository_lock(database: Path, repository: str):
    """Serial per remote across clones; never lock or alter an operator checkout."""
    directory = database.parent / "ship-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(repository.encode()).hexdigest() + ".lock"
    with runner_journal.lock(directory / name, blocking=False, noun="ship", error=WorkflowError,
                             held="another ship operation owns this repository; retry after it finishes"):
        yield
