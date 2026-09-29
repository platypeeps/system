"""The shared writing lifecycle, its evidence, and explicit file cutover.

The library reads registered piece paths. UI and CLI pass stable item IDs and
intent; neither reimplements the ladder or decides whether a gate is current.
Reports remain files, while structured gate records bind their verdicts to
the exact draft and research they reviewed. No verdict is guessed from prose.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from . import paths as sdpaths
from .database import transaction
from .progress import _fields, _git
from .sources.frontmatter import split as split_frontmatter
from .workflow import StaleItem, WorkflowError, _checked_state, _text, item_state
from .writes import _transition, add_note, create_item, now, record_state, resolve_state, set_item_fields, upsert_repo
from .yaml_lite import _Flow

STAGES = ("inbox", "accepted", "researching", "drafting", "review", "ready", "published", "declined")
STAGE_STATUS = {"inbox": "planning", "accepted": "ready", "researching": "in_progress",
                "drafting": "in_progress", "review": "in_progress", "ready": "ready_to_send",
                "published": "done", "declined": "done"}
FILE_STAGES = {"idea": "accepted", **{key: key for key in STAGES[2:7]}}
MARKER = "content/.status-source"
REPORTS = {"research": "research.md", "fact-check": "fact-check.md", "adversarial": "adversarial.md"}
STAMP = re.compile(r"<!-- reconciled-with-draft: ([0-9a-f]{12})\b(.*?)-->")
SOURCE = "writing-piece"


def _manual_destinations():
    """Publication targets whose URL the operator records by hand.

    `SD_WRITING_DESTINATIONS` adds names, comma-separated, such as an
    employer's blog; it is read at call time so a test can set it.
    """
    extra = os.environ.get("SD_WRITING_DESTINATIONS", "")
    return {"blog", "substack"} | {name.strip() for name in extra.split(",") if name.strip()}


def _canonical(connection: sqlite3.Connection, repo: str) -> str:
    """The form the `repo` row holds for this path, else its key (sd:1439).

    Identities, receipt keys and journals are built from it, so a caller
    that passes the disk path and one that passes the key land on one row.
    """
    from .repos import row_for
    row = row_for(connection, repo)
    return str(row["path"]) if row is not None else (sdpaths.key(repo) or repo)


def pieces_owner(connection: sqlite3.Connection, repo: str) -> str:
    from .repos import row_for
    row = row_for(connection, repo)
    if row is None:
        raise WorkflowError(f"repository {repo!r} is not registered")
    return row["pieces_source"]


def _key(piece: str) -> str:
    if not isinstance(piece, str) or re.fullmatch(r"[0-9]{4}/[A-Za-z0-9][A-Za-z0-9._-]*", piece) is None:
        raise WorkflowError("piece must be YEAR/SLUG without path traversal")
    return piece


# (registered root, worktree root), set by `checkout` (sd:2024). Only the
# repository it was validated for is redirected; every other one keeps its path.
_CHECKOUT: ContextVar[tuple[Path, Path] | None] = ContextVar("writing_checkout", default=None)


def _common_dir(root: Path) -> Path:
    return (root / _git(root, "rev-parse", "--git-common-dir")).resolve()


@contextmanager
def checkout(repo: str, root: Path | str):
    """Read and write piece files in `root`, a worktree of `repo` (sd:2024).

    Rows stay keyed to the registered repository, so a writer in its own
    worktree records gates against the prose it changed there. `root` must
    share the registered checkout's Git directory; any other path is refused.
    Another repository's rows keep reading their own registered checkout.
    Registration, cutover and recovery journal the registered checkout, so
    they refuse to run inside it.
    """
    registered = sdpaths.expand(repo).resolve()
    root = Path(root).resolve()
    if root != registered:
        try:
            same = _common_dir(root) == _common_dir(registered)
        except WorkflowError:
            same = False
        if not same:
            raise WorkflowError(f"{root} is not a worktree of the registered repository {repo}")
    token = _CHECKOUT.set((registered, root))
    try:
        yield root
    finally:
        _CHECKOUT.reset(token)


def _disk(repo: str) -> Path:
    """Where `repo`'s piece files live: its active `checkout`, else its registered path."""
    registered = sdpaths.expand(repo).resolve()
    active = _CHECKOUT.get()
    return active[1] if active is not None and active[0] == registered else registered


def _registered_only(repo: str) -> None:
    """Journals name the registered checkout, so their writers run only there."""
    if _disk(repo) != sdpaths.expand(repo).resolve():
        raise WorkflowError("writing registration, cutover and recovery run only in the registered checkout, not a worktree")


def _path(repo: str, piece: str, relative: str | None = None) -> Path:
    piece = _key(piece)
    root = _disk(repo)
    relative = relative or f"content/{piece}/index.md"
    wanted = {f"content/{piece}/index.md", f"content-parked/{piece}/index.md"}
    if relative not in wanted:
        raise WorkflowError("piece path must name its index.md under content or content-parked")
    target = root / relative
    if not target.resolve().is_relative_to(root) or not target.is_file():
        raise WorkflowError(f"piece file is unavailable inside its registered repository: {relative}")
    return target


def _scalar(value: str):
    value = _uncomment(value).strip()
    if value in ("", "null", "~"):
        return None
    if value[:1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            pass
    if len(value) > 1 and value[0] == value[-1] == "'":
        return value[1:-1]
    if value.startswith(("[", "{")):
        flow = _Flow(value, 1)
        parsed = flow.value()
        flow.skip()
        if flow.at != len(value):
            raise WorkflowError("unparsed text after frontmatter collection")
        return parsed
    return value


def _uncomment(value: str) -> str:
    quote, escaped = None, False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def _metadata(text: str) -> dict:
    block, _ = split_frontmatter(text)
    if not block:
        raise WorkflowError("piece has no frontmatter block")
    result = {}
    parent = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") and parent:
            if result.get(parent) is None:
                result[parent] = []
            if not isinstance(result[parent], list):
                raise WorkflowError(f"unsupported frontmatter sequence for {parent}")
            result[parent].append(_scalar(line[4:]))
        elif line.startswith("  ") and parent and ":" in line:
            key, _, value = line.strip().partition(":")
            if result.get(parent) is None:
                result[parent] = {}
            if not isinstance(result[parent], dict):
                raise WorkflowError(f"unsupported frontmatter mapping for {parent}")
            result[parent][key] = _scalar(value)
        elif line[:1].isspace() or ":" not in line:
            raise WorkflowError(f"unsupported frontmatter line: {line}")
        else:
            parent, _, value = line.partition(":")
            if parent in result:
                raise WorkflowError(f"duplicate frontmatter field {parent}")
            result[parent] = _scalar(value)
    return result


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot(row) -> dict:
    path = _path(row["repo"], row["piece"], row["path"])
    data = path.read_bytes()
    text = data.decode("utf-8")
    prose = text.split("## Draft", 1)[1] if "## Draft" in text else ""
    digest = hashlib.sha1(" ".join(prose.split()).encode("utf-8")).hexdigest()[:12] if prose.strip() else ""
    files = {"index": data}
    for name, filename in REPORTS.items():
        candidate = path.with_name(filename)
        if candidate.exists():
            if not candidate.resolve().is_relative_to(_disk(row["repo"])):
                raise WorkflowError(f"{filename} escapes the registered repository")
            files[name] = candidate.read_bytes()
    return {"path": path, "text": text, "digest": digest, "files": files,
            "hashes": {key: _hash(value) for key, value in files.items()}}


def piece_for_key(connection: sqlite3.Connection, repo: str, piece: str) -> sqlite3.Row | None:
    probe = sdpaths.keys(repo)
    return connection.execute(
        f"SELECT * FROM item WHERE repo IN ({sdpaths.placeholders(probe)}) AND piece = ?",
        (*probe, _key(piece))).fetchone()


def list_pieces(connection: sqlite3.Connection, repo: str | None = None, *, include_parked: bool = False) -> list[dict]:
    where, values = ["kind = 'idea'", "piece IS NOT NULL"], []
    if repo is not None:
        probe = sdpaths.keys(repo)
        where.append(f"repo IN ({sdpaths.placeholders(probe)})")
        values.extend(probe)
    if not include_parked:
        where.append("parked_at IS NULL")
    return [dict(row) for row in connection.execute(
        f"SELECT * FROM item WHERE {' AND '.join(where)} ORDER BY priority IS NULL, priority, updated_at DESC, id", values,
    )]


def _piece(connection: sqlite3.Connection, item: int, expected: str | None = None) -> dict:
    state = _checked_state(connection, item, expected)
    if state["item"]["kind"] != "idea" or not state["item"]["piece"]:
        raise WorkflowError(f"item {item} is not a writing piece")
    return state


def _problems(row, snapshot: dict) -> list[str]:
    problems = []
    digest, generation = snapshot["digest"], row["gate_generation"]
    if not digest:
        problems.append("draft is missing or empty")
    research = snapshot["files"].get("research", b"").decode("utf-8")
    matched = list(STAMP.finditer(research))
    if len(matched) != 1 or matched[0].group(1) != digest:
        problems.append("research.md is missing, unstamped or stale against this draft")
    else:
        stamped_generation = re.search(r"\bgen=(\d+)\b", matched[0].group(2))
        if (int(stamped_generation.group(1)) if stamped_generation else 0) != generation:
            problems.append("research.md belongs to an earlier gate generation")
    gates = _fields(row["fields"]).get("writing_gates") or {}
    for name in ("fact-check", "adversarial"):
        gate = gates.get(name) if isinstance(gates, dict) else None
        if name not in snapshot["files"]:
            problems.append(f"{name}.md is missing")
        if not isinstance(gate, dict):
            problems.append(f"{name} has no explicit current gate record; record its verdict and findings")
            continue
        if gate.get("draft_digest") != digest or gate.get("generation") != generation:
            problems.append(f"{name} gate is stale against this draft or generation")
        if gate.get("evidence_sha256") != snapshot["hashes"].get(name) or gate.get("research_sha256") != snapshot["hashes"].get("research"):
            problems.append(f"{name} evidence or research changed after its gate record")
        if gate.get("verdict") != "pass":
            problems.append(f"{name} verdict is not pass")
        for finding in gate.get("findings", []):
            if finding.get("disposition") == "open" and (name == "fact-check" or finding.get("confidence") == "CERTAIN"):
                problems.append(f"{name} finding {finding['id']} is unresolved")
    return problems


def _normal(stage: str) -> list[str]:
    return {"inbox": ["accepted"], "accepted": ["researching"], "researching": ["drafting"],
            "drafting": ["review"], "review": ["ready"], "ready": ["review"], "published": [], "declined": []}.get(stage, [])


def piece_state(connection: sqlite3.Connection, item: int) -> dict:
    state = _piece(connection, item)
    row = state["item"]
    owner = pieces_owner(connection, row["repo"])
    try:
        snapshot = _snapshot(row)
        problems = _problems(row, snapshot)
        digest = snapshot["digest"]
        document = snapshot["text"]
    except (OSError, UnicodeError, WorkflowError) as error:
        problems, digest = [str(error)], ""
        document = None
    stages = _normal(row["stage"])
    corrections = [target for target in ("researching", "drafting", "review")
                   if row["stage"] in STAGES and STAGES.index(target) < STAGES.index(row["stage"])]
    if owner == "retiring" or row["parked_at"]:
        stages, corrections = [], []
    state["writing"] = {"stage": row["stage"], "available_stages": stages, "correction_stages": corrections,
                        "owner": owner, "parked": row["parked_at"] is not None,
                        "metadata": _fields(row["fields"]).get("writing") or {},
                        "document": document,
                        "gates": {"ok": not problems, "digest": digest,
                                  "generation": row["gate_generation"], "problems": problems}}
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_claim'").fetchone():
        claims = []
        for claim in connection.execute("SELECT id,active_item,state FROM publication_claim WHERE item=? ORDER BY created_at", (item,)):
            progress = json.loads(claim["state"])
            claims.append({"id": claim["id"], "active": claim["active_item"] is not None,
                           "phase": progress["phase"], "document_id": progress.get("document_id"),
                           "url": progress.get("url"), "current_draft_published": progress.get("current_draft_published"),
                           "reconcile_required": progress.get("reconcile_required", False)})
        state["writing"]["publications"] = claims
    return state


def _rewrite(text: str, changes: dict, *, remove: tuple[str, ...] = ()) -> str:
    head, separator, body = text.partition("\n---\n")
    if not separator or not head.startswith("---\n"):
        raise WorkflowError("piece frontmatter must be fenced")
    for name in remove:
        head = re.sub(rf"(?m)^{re.escape(name)}:.*(?:\n|$)", "", head)
    for name, value in changes.items():
        encoded = "null" if value is None else json.dumps(value, ensure_ascii=False) if isinstance(value, list) or name == "tip" else str(value)
        replacement = f"{name}: {encoded}"
        if isinstance(value, dict):
            replacement = f"{name}:" + "".join(f"\n  {key}: {json.dumps(held, ensure_ascii=False)}" for key, held in value.items())
        pattern = rf"(?m)^{re.escape(name)}:.*(?:\n[ \t]+[^\n]*)*"
        if re.search(pattern, head):
            head = re.sub(pattern, lambda _: replacement, head, count=1)
        else:
            head += "\n" + replacement
    return head + separator + body


def _replace(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.sd-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _import_piece(connection: sqlite3.Connection, repo: str, piece: str, *, path: str | None = None, who: str) -> dict:
    piece = _key(piece)
    repo = _canonical(connection, repo)
    with transaction(connection):
        owner = pieces_owner(connection, repo)
        if owner == "retiring":
            raise WorkflowError("writing cutover is in progress")
        existing = piece_for_key(connection, repo, piece)
        if existing is not None and owner == "row":
            raise WorkflowError("database owns this piece; importing its former status would overwrite progress")
        source = _path(repo, piece, path)
        text = source.read_text(encoding="utf-8")
        metadata = _metadata(text)
        stage = FILE_STAGES.get(metadata.get("status"))
        if stage is None:
            raise WorkflowError(f"piece {piece} has an unknown file status {metadata.get('status')!r}")
        title = _text(metadata.get("title"), "title")
        fields = _fields(existing["fields"]) if existing else {}
        fields["writing"] = metadata
        disk = _disk(repo)
        relative = source.relative_to(disk).as_posix()
        base_commit, source_commit = None, None
        if (disk / ".git").exists():
            try:
                base_commit = _git(disk, "rev-parse", "HEAD")
                # git show text strips terminal newlines in the shared helper;
                # exact blob identity comes from Git's own hash-object command.
                captured_oid = _git(disk, "hash-object", "--stdin", input=text)
                tree_oid = _git(disk, "rev-parse", f"HEAD:{relative}")
                source_commit = base_commit if captured_oid == tree_oid else None
            except WorkflowError:
                pass
        fields["writing_source"] = {"sha256": _hash(text.encode("utf-8")), "path": relative, "base_commit": base_commit}
        if existing is None:
            item = create_item(connection, kind="idea", title=title, status=STAGE_STATUS[stage], stage=stage,
                               repo=repo, path=relative, source=SOURCE, external_id=f"{repo}::{piece}",
                               fields=fields, body={"source": text}, session=who, source_commit=source_commit)
            set_item_fields(connection, item, piece=piece, parked_at=now() if relative.startswith("content-parked/") else None)
        else:
            item = existing["id"]
            values = {"title": title, "stage": stage, "path": relative, "fields": fields, "body": {"source": text}, "source_commit": source_commit}
            changed = {key: value for key, value in values.items()
                       if existing[key] != (json.dumps(value, sort_keys=True) if key in ("fields", "body") else value)}
            if changed:
                set_item_fields(connection, item, **changed)
            _transition(connection, item, STAGE_STATUS[stage], who=who, reason="historical writing source")
        return piece_state(connection, item)


def import_piece(connection: sqlite3.Connection, repo: str, piece: str, *, path: str | None = None, who: str) -> dict:
    """Import historical input, or explicitly register a newly created piece.

    Registration after cutover retires its template bookkeeping once. The
    journal distinguishes this from repository cutover because repository
    ownership is already row while the new item may still roll back.
    """
    repo = _canonical(connection, repo)
    _registered_only(repo)
    if pieces_owner(connection, repo) != "row":
        return _import_piece(connection, repo, piece, path=path, who=who)
    journal_path = _journal_path(connection, repo)
    if journal_path.exists() and json.loads(journal_path.read_text()).get("phase") not in ("complete", "rolled_back"):
        raise WorkflowError("unfinished writing operation; recover_cutover before registering a piece")
    journal = None
    touched = []
    try:
        with transaction(connection):
            state = _import_piece(connection, repo, piece, path=path, who=who)
            row = state["item"]
            source = _path(repo, piece, row["path"])
            original = _fields(row["body"])["source"].encode("utf-8")
            retired = _rewrite(original.decode("utf-8"), {}, remove=("status", "published")).encode("utf-8")
            backup = Path(tempfile.mkdtemp(prefix="writing-register-", dir=journal_path.parent))
            saved = backup / row["path"]
            saved.parent.mkdir(parents=True)
            saved.write_bytes(original)
            entry = {"path": row["path"], "before_sha256": _hash(original), "after_sha256": _hash(retired)}
            journal = {"repo": repo, "backup_path": str(backup), "phase": "prepared", "operation": "registration",
                       "piece": piece, "files": [entry], "at": now(), "who": who}
            _save_journal(journal_path, journal)
            if _file_hash(source) != entry["before_sha256"]:
                raise StaleItem("new piece changed before registration retirement")
            touched.append(entry)
            _replace(source, retired)
            result = piece_state(connection, row["id"])
        journal["phase"] = "complete"
        _save_journal(journal_path, journal)
        return result
    except BaseException as error:
        if journal is not None:
            if piece_for_key(connection, repo, piece) is not None:
                raise WorkflowError(f"registration committed; finalize journal with recover_cutover; backup {journal['backup_path']}") from error
            conflicts = _restore_journal_files(journal, touched)
            journal["phase"] = "conflict" if conflicts else "rolled_back"
            _save_journal(journal_path, journal)
            if conflicts:
                raise WorkflowError(f"registration preserved concurrent edits; backup {journal['backup_path']}") from error
        raise


def _inventory(repo: str) -> list[dict]:
    root = _disk(repo)
    entries = {}
    for tree in ("content", "content-parked"):
        for path in sorted((root / tree).glob("*/*/index.md")):
            piece = f"{path.parent.parent.name}/{path.parent.name}"
            _key(piece)
            if piece in entries:
                raise WorkflowError(f"piece {piece} exists in both active and parked trees")
            _path(repo, piece, path.relative_to(root).as_posix())
            data = path.read_bytes()
            entries[piece] = {"piece": piece, "path": path.relative_to(root).as_posix(), "sha256": _hash(data)}
    return list(entries.values())


def import_pieces(connection: sqlite3.Connection, repo: str, *, who: str) -> dict:
    repo = _canonical(connection, repo)
    with transaction(connection):
        items = [import_piece(connection, repo, entry["piece"], path=entry["path"], who=who) for entry in _inventory(repo)]
        return {"items": items, "warnings": [f"{state['item']['piece']}: historical readiness needs fresh gate evidence"
                                               for state in items if state["item"]["stage"] in ("ready", "published")
                                               and not state["writing"]["gates"]["ok"]]}


def record_gate(connection: sqlite3.Connection, item: int, artifact: str, *, verdict: str,
                findings: list[dict], reason: str, who: str, expected_revision: str | None = None,
                reviewed_digest: str | None = None) -> dict:
    if artifact not in ("fact-check", "adversarial") or verdict not in ("pass", "fail"):
        raise WorkflowError("gate needs fact-check or adversarial and an explicit pass or fail verdict")
    reason, who = _text(reason, "reason"), _text(who, "who")
    if not isinstance(findings, list):
        raise WorkflowError("findings must be an explicit list, empty when the review found none")
    held, ids = [], set()
    for finding in findings:
        if not isinstance(finding, dict) or not isinstance(finding.get("id"), str) or not finding["id"].strip():
            raise WorkflowError("each finding needs an explicit id")
        if finding["id"] in ids:
            raise WorkflowError("finding IDs must be unique")
        ids.add(finding["id"])
        if finding.get("disposition") not in ("open", "resolved", "rebutted"):
            raise WorkflowError("finding disposition must be open, resolved or rebutted")
        if finding["disposition"] != "open":
            _text(finding.get("evidence"), "resolution evidence")
        if artifact == "adversarial" and finding.get("confidence") not in ("CERTAIN", "LIKELY", "SPECULATIVE"):
            raise WorkflowError("adversarial finding needs CERTAIN, LIKELY or SPECULATIVE confidence")
        held.append(dict(finding))
    with transaction(connection):
        from .publication import assert_mutable
        assert_mutable(connection, item)
        state = _piece(connection, item, expected_revision)
        row = state["item"]
        if pieces_owner(connection, row["repo"]) == "retiring":
            raise WorkflowError("writing cutover is in progress")
        snapshot = _snapshot(row)
        if not snapshot["digest"] or artifact not in snapshot["files"] or "research" not in snapshot["files"]:
            raise WorkflowError("gate recording requires a draft, research and the full report file")
        evidence_stamps = list(STAMP.finditer(snapshot["files"][artifact].decode("utf-8")))
        if len(evidence_stamps) > 1:
            raise WorkflowError("gate report has ambiguous draft stamps")
        stamped_digest = evidence_stamps[0].group(1) if evidence_stamps else None
        if stamped_digest is not None and stamped_digest != snapshot["digest"]:
            raise WorkflowError("gate report's draft stamp is stale; review the current draft before recording it")
        reviewed = reviewed_digest or stamped_digest
        if reviewed != snapshot["digest"]:
            raise WorkflowError("gate needs the explicitly reviewed draft digest; it must match the current draft")
        fields = _fields(row["fields"])
        gates = fields.setdefault("writing_gates", {})
        if not isinstance(gates, dict):
            raise WorkflowError("stored writing gate metadata is malformed")
        record = {"verdict": verdict, "findings": held, "reason": reason, "who": who,
                  "draft_digest": snapshot["digest"], "generation": row["gate_generation"],
                  "evidence_sha256": snapshot["hashes"][artifact], "research_sha256": snapshot["hashes"]["research"]}
        existing = gates.get(artifact)
        if isinstance(existing, dict) and {key: value for key, value in existing.items() if key != "at"} == record:
            return piece_state(connection, item)
        gates[artifact] = {**record, "at": now()}
        set_item_fields(connection, item, fields=fields)
        add_note(connection, item, "decision", f"{artifact} gate {verdict}: {reason}", session=who)
        return piece_state(connection, item)


def preflight(connection: sqlite3.Connection, item: int) -> dict:
    state = piece_state(connection, item)
    gates = state["writing"]["gates"]
    if state["item"]["stage"] != "ready":
        raise WorkflowError("publication requires a ready piece")
    if not gates["ok"] or state["item"]["ready_digest"] != gates["digest"]:
        raise WorkflowError("publication gate refused: " + "; ".join(gates["problems"] or ["draft differs from the one granted readiness"]))
    return state


def readiness(connection: sqlite3.Connection, item: int) -> dict:
    """Read-only gate diagnosis at any stage, including before readiness."""
    return piece_state(connection, item)["writing"]["gates"]


def change_stage(connection: sqlite3.Connection, item: int, target: str, *, actor: str = "human", who: str,
                 correct: bool = False, reason: str | None = None, expected_revision: str | None = None,
                 confirmed: bool = False, require_row: bool = False) -> dict:
    if target not in STAGES or actor not in ("human", "session"):
        raise WorkflowError("unknown writing stage or actor")
    who = _text(who, "who")
    original = None
    source = None
    try:
        with transaction(connection):
            from .publication import assert_mutable
            assert_mutable(connection, item)
            state = _piece(connection, item, expected_revision)
            row = state["item"]
            owner = pieces_owner(connection, row["repo"])
            if require_row and owner != "row":
                raise WorkflowError("dashboard stage controls require database ownership after writing cutover")
            if owner == "retiring" or row["parked_at"]:
                raise WorkflowError("piece is parked or its source is retiring")
            current = row["stage"]
            if current == target:
                return piece_state(connection, item)
            if target in ("inbox", "declined"):
                raise WorkflowError("an existing piece is parked instead of declined or returned to inbox")
            correction = correct and target in ("researching", "drafting", "review") and STAGES.index(target) < STAGES.index(current)
            if correct and (not correction or actor != "human"):
                raise WorkflowError("only the human operator can correct a piece to an earlier research or draft stage")
            if correction:
                reason = _text(reason, "correction reason")
            elif target == "published":
                if current != "ready" or not confirmed:
                    raise WorkflowError("published requires ready plus explicit confirmation of the publication")
            elif target not in _normal(current):
                raise WorkflowError(f"writing stage cannot move from {current} to {target} without an explicit correction")
            snapshot = _snapshot(row)
            if target in ("ready", "published"):
                problems = _problems(row, snapshot)
                if problems:
                    raise WorkflowError("writing gate refused: " + "; ".join(problems))
                if target == "published" and row["ready_digest"] != snapshot["digest"]:
                    raise WorkflowError("draft differs from the one granted readiness")
            fields = _fields(row["fields"])
            metadata = fields.setdefault("writing", {})
            if target == "published" and not any((metadata.get("published_urls") or {}).values()):
                raise WorkflowError("published requires an actual recorded publication URL")
            manual_targets = {key: value for key, value in (metadata.get("published_urls") or {}).items()
                              if key in _manual_destinations() and value}
            if target == "published" and not manual_targets:
                raise WorkflowError("Drive publication requires a verified claim and native destination readback; a manually attached Drive URL is unverified")
            changed = {"stage": target, "ready_digest": snapshot["digest"] if target == "ready" else None}
            if correction:
                changed["gate_generation"] = row["gate_generation"] + 1
            if target == "published":
                changed["shipped_at"] = now()
                metadata["published"] = changed["shipped_at"][:10]
                metadata["publication_confirmation"] = {"kind": "manual-acknowledgement", "who": who,
                                                         "at": changed["shipped_at"], "targets": manual_targets}
            elif current == "published":
                changed["shipped_at"] = None
                metadata["published"] = None
            metadata["status"] = "idea" if target == "accepted" else target
            metadata["updated"] = now()[:10]
            changed["fields"] = fields
            if owner == "file":
                source, original = snapshot["path"], snapshot["files"]["index"]
                text = _rewrite(snapshot["text"], {key: metadata.get(key) for key in ("status", "updated", "published")})
                _replace(source, text.encode("utf-8"))
                changed["body"] = {"source": text}
            set_item_fields(connection, item, **changed)
            _transition(connection, item, STAGE_STATUS[target], who=who, reason=reason or f"writing {current} -> {target}")
            if STAGE_STATUS[current] == STAGE_STATUS[target]:
                add_note(connection, item, "comment", f"Writing stage {current} -> {target} by {who}" + (f": {reason}" if reason else ""), session=who)
            return piece_state(connection, item)
    except BaseException:
        if original is not None:
            _replace(source, original)
        raise


def park_piece(connection: sqlite3.Connection, item: int, *, parked: bool = True, actor: str = "human",
               who: str, expected_revision: str | None = None, require_row: bool = False) -> dict:
    if actor != "human" or type(parked) is not bool:
        raise WorkflowError("parking is the human operator's decision")
    with transaction(connection):
        from .publication import assert_mutable
        assert_mutable(connection, item)
        state = _piece(connection, item, expected_revision)
        owner = pieces_owner(connection, state["item"]["repo"])
        if require_row and owner != "row":
            raise WorkflowError("writing controls require row ownership; complete the writing cutover first")
        if owner == "retiring":
            raise WorkflowError("writing cutover is in progress")
        if bool(state["item"]["parked_at"]) == parked:
            return piece_state(connection, item)
        set_item_fields(connection, item, parked_at=now() if parked else None)
        add_note(connection, item, "decision", "Piece parked" if parked else "Piece revived", session=who)
        return piece_state(connection, item)


def update_piece_metadata(connection: sqlite3.Connection, item: int, changes: dict, *,
                          expected_revision: str | None = None, who: str) -> dict:
    who = _text(who, "who")
    allowed = {"published_urls", "review_urls", "tip"}
    if not isinstance(changes, dict) or set(changes) - allowed:
        raise WorkflowError("only published_urls, review_urls and tip are editable writing metadata")
    for name, value in changes.items():
        if name.endswith("_urls"):
            targets = ({"gdrive"} | _manual_destinations()) if name == "published_urls" else {"gdocs", "gdocs_digest"}
            if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
                raise WorkflowError(f"{name} must be a destination mapping")
            for target, url in value.items():
                if target.endswith("_digest"):
                    if url is not None and (not isinstance(url, str) or not re.fullmatch(r"[0-9a-f]{12}", url)):
                        raise WorkflowError("review digest must be a draft digest")
                elif url is not None and (not isinstance(url, str) or any(ord(char) < 32 for char in url) or urlsplit(url).scheme != "https" or not urlsplit(url).netloc):
                    raise WorkflowError("publication and review URLs must be actual https URLs or null")
        elif value is not None:
            _text(value, "tip")
    original = source = None
    try:
        with transaction(connection):
            from .publication import assert_mutable
            assert_mutable(connection, item)
            state = _piece(connection, item, expected_revision)
            row = state["item"]
            owner = pieces_owner(connection, row["repo"])
            if owner == "retiring":
                raise WorkflowError("writing source is retiring")
            fields = _fields(row["fields"])
            previous_fields = json.dumps(fields, sort_keys=True)
            metadata = fields.setdefault("writing", {})
            for key, value in changes.items():
                if isinstance(value, dict):
                    held = metadata.setdefault(key, {})
                    if held is None:
                        held = metadata[key] = {}
                    allowed_targets = ({"gdrive"} | _manual_destinations()) if key == "published_urls" else {"gdocs", "gdocs_digest"}
                    for target, url in value.items():
                        if target not in allowed_targets and (target not in held or held[target] != url):
                            raise WorkflowError(f"unknown {key} destination {target}")
                        if key == "published_urls" and row["stage"] not in ("ready", "published") and url is not None and held.get(target) != url:
                            raise WorkflowError("published URLs belong only to ready or published pieces")
                    changed_urls = {target: url for target, url in value.items() if held.get(target) != url}
                    held.update(value)
                    if key == "published_urls":
                        provenance = metadata.setdefault("publication_url_provenance", {})
                        for target, url in changed_urls.items():
                            if url is not None:
                                provenance[target] = {"kind": "manual-unverified", "who": who, "url": url}
                else:
                    metadata[key] = value
            if json.dumps(fields, sort_keys=True) == previous_fields:
                return piece_state(connection, item)
            values = {"fields": fields}
            if owner == "file":
                snapshot = _snapshot(row)
                source, original = snapshot["path"], snapshot["files"]["index"]
                text = _rewrite(snapshot["text"], {key: metadata[key] for key in changes})
                _replace(source, text.encode("utf-8"))
                values["body"] = {"source": text}
            set_item_fields(connection, item, **values)
            add_note(connection, item, "comment", f"Updated writing metadata: {', '.join(sorted(changes))}", session=who)
            return piece_state(connection, item)
    except BaseException:
        if original is not None:
            _replace(source, original)
        raise


def cutover_preview(connection: sqlite3.Connection, repo: str) -> dict:
    repo = _canonical(connection, repo)
    owner = pieces_owner(connection, repo)
    pending = _journal_path(connection, repo)
    if pending.exists():
        journal = json.loads(pending.read_text(encoding="utf-8"))
        if journal.get("phase") not in ("complete", "rolled_back"):
            raise WorkflowError(f"unfinished writing cutover; run recover_cutover before retrying; backup {journal.get('backup_path')}")
    files = _inventory(repo)
    rows = [{"id": row["id"], "piece": row["piece"], "revision": item_state(connection, row["id"])["revision"]}
            for row in list_pieces(connection, repo, include_parked=True)]
    payload = {"repo": repo, "owner": owner, "files": files, "rows": rows}
    payload["fingerprint"] = _hash(json.dumps(payload, sort_keys=True).encode())
    payload["warnings"] = []
    return payload


def verify_pieces(connection: sqlite3.Connection, repo: str) -> dict:
    repo = _canonical(connection, repo)
    owner = pieces_owner(connection, repo)
    files = _inventory(repo)
    rows = {row["piece"]: row for row in list_pieces(connection, repo, include_parked=True)}
    row_count = len(rows)
    differences = []
    for entry in files:
        row = rows.pop(entry["piece"], None)
        if row is None:
            differences.append(f"{entry['piece']}: missing database row")
            continue
        text = _path(repo, entry["piece"], entry["path"]).read_text(encoding="utf-8")
        metadata = _metadata(text)
        if row["path"] != entry["path"]:
            differences.append(f"{entry['piece']}: artifact path differs")
        if owner == "row":
            if "status" in metadata or "published" in metadata:
                differences.append(f"{entry['piece']}: retired status or published line remains")
        else:
            if FILE_STAGES.get(metadata.get("status")) != row["stage"]:
                differences.append(f"{entry['piece']}: file stage differs from imported row")
            if _fields(row["fields"]).get("writing") != metadata:
                differences.append(f"{entry['piece']}: imported metadata differs from source")
            if _fields(row["body"]).get("source") != text:
                differences.append(f"{entry['piece']}: imported source body differs")
            if bool(row["parked_at"]) != entry["path"].startswith("content-parked/"):
                differences.append(f"{entry['piece']}: parked identity differs from source")
    differences.extend(f"{piece}: database row has no artifact" for piece in rows)
    if owner == "row":
        marker = _disk(repo) / MARKER
        if not marker.exists() or marker.read_text(encoding="utf-8") != "row\n":
            differences.append("writing status marker is missing or incorrect")
    return {"ok": not differences, "owner": owner, "files": len(files), "rows": row_count, "differences": differences}


def _write_marker(repo: str) -> None:
    _replace(sdpaths.expand(repo) / MARKER, b"row\n")


def _journal_path(connection: sqlite3.Connection, repo: str) -> Path:
    """The journal is named by a hash of the repository, so it probes the hash
    of each stored form: a journal written before migration 014 is named by
    the absolute path (sd:1439). A new journal takes the key's name."""
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    names = [database.parent / f"writing-cutover-{_hash(form.encode())[:16]}.json"
             for form in sdpaths.keys(repo)]
    return next((name for name in names if name.exists()), names[0])


def _save_journal(path: Path, journal: dict) -> None:
    _replace(path, (json.dumps(journal, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def _file_hash(path: Path) -> str | None:
    return _hash(path.read_bytes()) if path.exists() else None


def _restore_journal_files(journal: dict, entries: list[dict]) -> list[str]:
    conflicts = []
    backup = Path(journal["backup_path"])
    root = sdpaths.expand(journal["repo"]).resolve()
    for entry in reversed(entries):
        target = root / entry["path"]
        if not target.resolve().is_relative_to(root):
            conflicts.append(entry["path"] + ": path escaped repository")
            continue
        current = _file_hash(target)
        if current == entry["before_sha256"]:
            continue
        if current != entry["after_sha256"]:
            conflicts.append(entry["path"] + ": changed by another writer; preserved")
            continue
        if entry["before_sha256"] is None:
            target.unlink()
        else:
            saved = backup / entry["path"]
            data = saved.read_bytes()
            if _hash(data) != entry["before_sha256"]:
                conflicts.append(entry["path"] + ": recovery backup hash differs")
                continue
            _replace(target, data)
    return conflicts


def recover_cutover(connection: sqlite3.Connection, repo: str, *, who: str) -> dict:
    """Recover a killed cutover without overwriting subsequent user edits."""
    repo = _canonical(connection, repo)
    _registered_only(repo)
    journal_path = _journal_path(connection, repo)
    if not journal_path.exists():
        raise WorkflowError("no writing cutover journal exists for this repository")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if not sdpaths.same(journal.get("repo"), repo):
        raise WorkflowError("cutover journal belongs to another repository")
    with transaction(connection):
        owner = pieces_owner(connection, repo)
        registration = journal.get("operation") == "registration"
        registered = not registration or piece_for_key(connection, repo, journal["piece"]) is not None
        if owner == "row" and registered:
            result = verify_pieces(connection, repo)
            if not result["ok"]:
                raise WorkflowError(f"cutover committed but files now differ; preserve edits and inspect {journal['backup_path']}")
            journal["phase"] = "complete"
            _save_journal(journal_path, journal)
            return {**result, "recovered": "completed", "backup_path": journal["backup_path"]}
        conflicts = _restore_journal_files(journal, journal["files"])
        journal["phase"] = "conflict" if conflicts else "rolled_back"
        journal["conflicts"] = conflicts
        _save_journal(journal_path, journal)
        if conflicts:
            raise WorkflowError(f"cutover recovery preserved concurrent edits: {'; '.join(conflicts)}; backup {journal['backup_path']}")
        if owner == "retiring":
            upsert_repo(connection, repo, pieces_source="file")
        return {"ok": True, "recovered": "rolled_back", "owner": owner, "backup_path": journal["backup_path"]}


def cutover_pieces(connection: sqlite3.Connection, repo: str, *, expected_fingerprint: str, who: str) -> dict:
    """Retire only status bookkeeping, retaining every source artifact.

    A dry-run fingerprint binds the source files and rows. Backups are retained
    outside the repository and returned to the caller. An ordinary exception
    restores exact files while SQLite rolls back the ownership and row writes.
    """
    original, rewritten, touched = {}, {}, []
    journal = None
    repo = _canonical(connection, repo)
    _registered_only(repo)
    disk = sdpaths.expand(repo)
    journal_path = _journal_path(connection, repo)
    try:
        with transaction(connection):
            preview = cutover_preview(connection, repo)
            if preview["fingerprint"] != expected_fingerprint:
                raise StaleItem("writing files or rows changed since cutover preview; review a fresh preview")
            if preview["owner"] == "row":
                return verify_pieces(connection, repo)
            if preview["owner"] != "file":
                raise WorkflowError("writing source is already retiring")
            import_pieces(connection, repo, who=who)
            verified = verify_pieces(connection, repo)
            if not verified["ok"]:
                raise WorkflowError("writing import verification failed: " + "; ".join(verified["differences"]))
            database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
            backup = Path(tempfile.mkdtemp(prefix="writing-cutover-", dir=database.parent))
            for entry in preview["files"]:
                path = disk / entry["path"]
                data = path.read_bytes()
                if _hash(data) != entry["sha256"]:
                    raise StaleItem(f"{entry['piece']} changed during cutover")
                original[path] = data
                rewritten[path] = _rewrite(data.decode("utf-8"), {}, remove=("status", "published")).encode("utf-8")
                target = backup / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            marker = disk / MARKER
            original[marker] = marker.read_bytes() if marker.exists() else None
            rewritten[marker] = b"row\n"
            if original[marker] is not None:
                saved_marker = backup / MARKER
                saved_marker.parent.mkdir(parents=True, exist_ok=True)
                saved_marker.write_bytes(original[marker])
            (backup / "manifest.json").write_text(json.dumps(preview, indent=2) + "\n", encoding="utf-8")
            entries = [{"path": path.relative_to(disk).as_posix(),
                        "before_sha256": _hash(data) if data is not None else None,
                        "after_sha256": _hash(rewritten[path])} for path, data in original.items()]
            journal = {"repo": repo, "backup_path": str(backup), "phase": "prepared", "files": entries,
                       "fingerprint": expected_fingerprint, "who": who, "at": now()}
            _save_journal(journal_path, journal)
            upsert_repo(connection, repo, pieces_source="retiring")
            for entry in entries:
                path = disk / entry["path"]
                if _file_hash(path) != entry["before_sha256"]:
                    raise StaleItem(f"{entry['path']} changed immediately before cutover write")
                touched.append(entry)
                if path == marker:
                    _write_marker(repo)
                else:
                    _replace(path, rewritten[path])
            upsert_repo(connection, repo, pieces_source="row")
            result = verify_pieces(connection, repo)
            if not result["ok"]:
                raise WorkflowError("writing cutover verification failed: " + "; ".join(result["differences"]))
            result["backup_path"] = str(backup)
            receipt = record_state(connection, "verified", key=f"{repo}:pieces_source",
                                   body={"fingerprint": expected_fingerprint, "rows": result["rows"], "files": preview["files"]})
            resolve_state(connection, receipt)
        journal["phase"] = "complete"
        _save_journal(journal_path, journal)
        return result
    except BaseException as error:
        if journal is not None:
            # After SQLite commits, retain the durable journal for explicit
            # recovery. Reverting files then would conflict with row authority.
            if pieces_owner(connection, repo) == "row":
                raise WorkflowError(f"cutover committed; finalize its journal with recover_cutover; backup {journal['backup_path']}") from error
            conflicts = _restore_journal_files(journal, touched)
            journal["phase"] = "conflict" if conflicts else "rolled_back"
            journal["conflicts"] = conflicts
            _save_journal(journal_path, journal)
            if conflicts:
                raise WorkflowError(f"cutover stopped and preserved concurrent edits: {'; '.join(conflicts)}; backup {journal['backup_path']}") from error
        raise
