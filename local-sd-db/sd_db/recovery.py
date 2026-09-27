"""Rebuild retired source rows from immutable evidence after a restore.

This recovers migration input, not changes made after a snapshot. Source files
are never rewritten. A whole repository is prepared before a transaction lands
any row or changes its authority; an unreadable source leaves it held.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from . import paths as sdpaths
from .database import transaction
from .errors import SdDbError
from .sources import docs_work
from .sources.frontmatter import read as read_frontmatter
from .writes import (
    _transition,
    add_note,
    create_item,
    record_state,
    resolve_state,
    set_item_fields,
    upsert_repo,
)

AUTHORITIES = {"status_source": ("docs/work", "docs/work/.status-source"),
               "pieces_source": ("writing-piece", "content/.status-source")}


class RecoveryRefused(SdDbError):
    """The available artifacts cannot prove a complete recovery."""


def _json(value):
    try:
        result = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _historical(root, relative, commit, valid):
    """Use the recorded commit; otherwise locate the last committed source."""
    refs = [commit] if commit else docs_work._read(
        root, "log", "--format=%H", "HEAD", "--", relative).splitlines()
    for ref in refs:
        if re.fullmatch(r"[0-9a-f]{40}", ref or "") is None:
            raise RecoveryRefused(f"{relative}: source_commit is not a complete Git commit")
        text = docs_work._read(root, "show", f"{ref}:{relative}")
        if valid(text):
            return text, ref
    raise RecoveryRefused(f"{relative}: no committed historical source with a status; restore a newer backup")


def _work(connection, repo):
    root = sdpaths.expand(repo)
    held = {r["path"]: dict(r) for r in connection.execute(
        "SELECT * FROM item WHERE repo=? AND source='docs/work'", (repo,))}
    paths = set(held) | {p.relative_to(root).as_posix() for p in root.glob("docs/work/*/prd.md")}
    if not paths:
        raise RecoveryRefused("no docs/work source inventory is available to prove this repository")
    prepared = []
    for path in sorted(paths):
        if not isinstance(path, str) or re.fullmatch(r"docs/work/[^/]+/prd\.md", path) is None:
            raise RecoveryRefused(f"invalid docs/work source path: {path!r}")
        old = held.get(path)
        commit = old["source_commit"] if old else None
        text, commit = _historical(root, path, commit,
            lambda text: read_frontmatter(text)[0].get("status") in docs_work.STATUSES)
        matter, _ = read_frontmatter(text)
        candidate = docs_work.Reader(paths=[repo])._candidate(root, commit, path)
        values = {"kind": "work", "repo": repo, "path": path, "source": "docs/work",
                  "external_id": f"{repo}::{path}", "title": candidate.title,
                  "status": candidate.status, "source_commit": commit,
                  "branch": old["branch"] if old else matter.get("branch"),
                  "created_at": old["created_at"] if old else candidate.created}
        prepared.append((old, values, {"commit": commit, "path": path}))
    return prepared


def _pieces(connection, repo):
    from . import writing
    root = sdpaths.expand(repo)
    held = {r["piece"]: dict(r) for r in connection.execute(
        "SELECT * FROM item WHERE repo=? AND source=?", (repo, writing.SOURCE))}
    inventory = {entry["piece"]: entry for entry in writing._inventory(repo)}
    if not inventory or set(held) - set(inventory):
        raise RecoveryRefused("writing inventory is empty or a restored piece has no artifact")
    prepared = []
    for piece, entry in sorted(inventory.items()):
        path, old = entry["path"], held.get(piece)
        fields = _json(old["fields"]) if old else {}
        source = _json(old["body"]).get("source") if old else None
        evidence = fields.get("writing_source", {})
        commit = old["source_commit"] if old else None
        if source is not None:
            if not isinstance(source, str) or not isinstance(evidence, dict) or evidence.get("sha256") != hashlib.sha256(source.encode()).hexdigest() or evidence.get("path") != path:
                raise RecoveryRefused(f"{piece}: captured source does not match its recorded hash and path")
        else:
            source, commit = _historical(root, path, commit,
                lambda text: writing._metadata(text).get("status") in writing.FILE_STAGES)
        metadata = writing._metadata(source)
        stage = writing.FILE_STAGES.get(metadata.get("status"))
        if stage is None:
            raise RecoveryRefused(f"{piece}: captured source has no recognized historical stage")
        fields["writing"] = metadata
        fields["writing_source"] = {"sha256": hashlib.sha256(source.encode()).hexdigest(),
                                     "path": path, "base_commit": commit}
        values = {"kind": "idea", "repo": repo, "path": path, "source": writing.SOURCE,
                  "external_id": f"{repo}::{piece}", "title": writing._text(metadata.get("title"), "title"),
                  "status": writing.STAGE_STATUS[stage], "stage": stage, "piece": piece,
                  "source_commit": commit, "fields": fields, "body": {"source": source},
                  "ready_digest": None}
        prepared.append((old, values, {"sha256": fields["writing_source"]["sha256"], "path": path}))
    return prepared


def reimport(connection: sqlite3.Connection, repo: str, *, dry_run: bool = False, expected_fingerprint: str | None = None) -> dict:
    """Recover one repository atomically and retain the global restore hold."""
    with transaction(connection):
        restores = list(connection.execute("SELECT id FROM state WHERE kind='restore' AND resolved_at IS NULL"))
        if len(restores) != 1:
            raise RecoveryRefused("reimport requires exactly one unresolved restore")
        from .repos import row_for
        row = row_for(connection, repo)
        if row is None:
            raise RecoveryRefused(f"{repo} is not a registered repository")
        # The form the row holds: identities, the receipt key and the
        # fingerprint are built from it, not from what the caller typed.
        repo = row["path"]
        columns = [name for name in AUTHORITIES if row[name] == "retiring"]
        if not columns:
            raise RecoveryRefused(f"{repo} is not awaiting a reimport")
        if not sdpaths.expand(repo).is_dir():
            raise RecoveryRefused(f"repository is unavailable: {repo}")
        prepared = {name: (_work if name == "status_source" else _pieces)(connection, repo) for name in columns}
        fingerprint = hashlib.sha256(json.dumps({"restore": restores[0]["id"], "repo": repo,
                                                "prepared": prepared}, sort_keys=True).encode()).hexdigest()
        if expected_fingerprint is not None and expected_fingerprint != fingerprint:
            raise RecoveryRefused("recovery sources or rows changed since preview; review a fresh preview")
        for records in prepared.values():
            for old, values, _ in records:
                if old is not None:
                    # A migration replay must not silently reset work subsequently
                    # performed against the database, even in an unproven snapshot.
                    changed = [
                        field for field, proposed in values.items()
                        if field not in ("ready_digest",)
                        and (_json(old[field]) if field in ("fields", "body") else old[field]) != proposed
                    ]
                    # Public field updates need not emit a note. Without an
                    # immutable rehearsal baseline, any difference is evidence
                    # we must preserve rather than infer safe to overwrite.
                    if changed:
                        raise RecoveryRefused(
                            f"item {old['id']} has later progress in {', '.join(changed)}; "
                            "restore a verified newer snapshot rather than overwrite it"
                        )
        if dry_run:
            return {"repo": repo, "authorities": {name: len(records) for name, records in prepared.items()},
                    "fingerprint": fingerprint, "dry_run": True,
                    "warning": "Preview only. Reimport recovers historical input, not post-snapshot progress."}
        if expected_fingerprint is None:
            raise RecoveryRefused("reimport apply requires the preview fingerprint; run a preview then pass --if-fingerprint")
        counts = {}
        for name, records in prepared.items():
            for old, values, _ in records:
                values = dict(values)
                piece, ready = values.pop("piece", None), values.pop("ready_digest", None)
                if old is None:
                    item = create_item(connection, session="restore-reimport", **values)
                else:
                    item = old["id"]
                    for field in ("kind", "repo", "source", "external_id"):
                        values.pop(field, None)
                    target = values.pop("status")
                    set_item_fields(connection, item, **values)
                    _transition(connection, item, target, who="restore-reimport", reason="verified historical source")
                if piece:
                    set_item_fields(connection, item, piece=piece, ready_digest=ready,
                                    gate_generation=(old["gate_generation"] if old else 0) + 1)
                add_note(connection, item, "comment", "Recovered historical migration input; post-snapshot progress requires reconciliation.", session="restore-reimport")
            receipt = record_state(connection, "verified", key=f"{repo}:{name}",
                                   body={"restore_id": restores[0]["id"], "recovered": [proof for _, _, proof in records]})
            resolve_state(connection, receipt)
            upsert_repo(connection, repo, **{name: "row"})
            counts[name] = len(records)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RecoveryRefused("reimport would leave broken foreign keys")
        return {"repo": repo, "authorities": counts, "restore_id": restores[0]["id"], "fingerprint": fingerprint,
                "warning": "Recovered historical input only. Reconcile post-snapshot work before sd restore resume."}


def prepare_restore(connection: sqlite3.Connection) -> None:
    """Freeze work that an old snapshot must never dispatch a second time."""
    with transaction(connection):
        connection.execute("UPDATE assignment SET status='blocked' WHERE status IN ('queued','running')")
        for row in list(connection.execute("SELECT * FROM repo")):
            for name, (_, marker) in AUTHORITIES.items():
                path = sdpaths.expand(row["path"]) / marker
                retired = path.is_file() and path.read_text(encoding="utf-8").strip() == "row"
                # A row-owned snapshot already carries the atomic cutover.
                # A newer checkout marker cannot prove a file-owned snapshot does.
                if row[name] == "retiring" or (retired and row[name] != "row"):
                    upsert_repo(connection, row["path"], **{name: "retiring"})
