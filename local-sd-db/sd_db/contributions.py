"""Durable contribution work and attention, shared by every reader.

GitHub observations are context, never authority over local task status.
Network calls and notification delivery belong outside these transactions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import stat
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .database import transaction
from .workflow import (
    StaleItem,
    WorkflowError,
    _checked_state,
    _text,
    capture_task,
    item_state,
)
from .writes import add_note, now, record_state, resolve_note, set_item_fields

FIELDS = frozenset({"local_clone", "local_branch", "tested_commit", "pull_url", "evidence",
                    "blocked_on", "depends_on", "blocking_labels",
                    "issue_url", "target_repo", "draft_title", "draft_path"})
# `closed` is the terminal lane for an issue and for a pull request closed
# without merge -- neither awaits anyone; `merged` stays pull-request vocabulary.
LANES = {"newly_unblocked": 0, "awaiting_you": 1, "awaiting_them": 2, "merged": 3, "closed": 4}
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
PULL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?\Z")
# Its own pattern: PULL also validates merge and release dependency proof,
# so widening it would let an issue URL stand in for a merged pull request.
ISSUE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?\Z")
REPO = re.compile(r"[\w.-]+/[\w.-]+")
# The observed sources a row can carry beside `item:`; each key names a URL.
OBSERVED = {"github:": "pull_url", "issue:": "issue_url"}
# One event vocabulary for both sources. `closed_completed` and
# `closed_not_planned` carry the close reason GitHub reports as a field; a
# pull request has no reason and no issue is ever converted to draft.
EVENTS = frozenset({"comment", "label_added", "label_removed", "converted_to_draft", "ready_for_review",
                    "closed", "closed_completed", "closed_not_planned", "reopened"})
ISSUE_EVENTS = EVENTS - {"converted_to_draft", "ready_for_review"}
# An issue observation carries none of these; one that does was read as a pull.
PULL_ONLY = frozenset({"reviews", "ci", "ci_head", "ci_ids", "head", "base", "mergeable", "draft"})


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _url(value) -> str:
    if not isinstance(value, str) or not PULL.fullmatch(value):
        raise WorkflowError("pull_url must be a canonical GitHub pull-request URL")
    match = PULL.fullmatch(value)
    return f"https://github.com/{match[1].lower()}/{match[2].lower()}/pull/{match[3]}"


def _issue_url(value) -> str:
    if not isinstance(value, str) or not ISSUE.fullmatch(value):
        raise WorkflowError("issue_url must be a canonical GitHub issue URL")
    match = ISSUE.fullmatch(value)
    return f"https://github.com/{match[1].lower()}/{match[2].lower()}/issues/{match[3]}"


def _key(key: str) -> str:
    if isinstance(key, str) and key.startswith("github:"):
        return "github:" + _url(key[len("github:"):])
    if isinstance(key, str) and key.startswith("issue:"):
        return "issue:" + _issue_url(key[len("issue:"):])
    if isinstance(key, str) and re.fullmatch(r"item:[1-9][0-9]*", key):
        return key
    raise WorkflowError("contribution key must be item:ID, github:PR-URL or issue:ISSUE-URL")


def _observed_url(key: str) -> str | None:
    for prefix in OBSERVED:
        if key.startswith(prefix):
            return key[len(prefix):]
    return None


def _moment(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (AttributeError, TypeError, ValueError):
        raise WorkflowError("observation time must be an ISO timestamp with timezone") from None


def _strings(value, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v or "\0" in v for v in value):
        raise WorkflowError(f"{name} must be a list of nonempty strings")
    return value


def _sha(value, name: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise WorkflowError(f"{name} must be a full commit hash")
    return value


def _metadata(row: dict) -> dict | None:
    try:
        fields = json.loads(row.get("fields") or "{}")
        value = fields.get("contribution")
        if value is not None and not isinstance(value, dict):
            raise ValueError()
        return value
    except (TypeError, ValueError, AttributeError, RecursionError):
        raise WorkflowError(f"item {row.get('id')} has malformed contribution metadata") from None


def _registered(connection) -> list[tuple[dict, dict]]:
    result = []
    for raw in connection.execute("SELECT * FROM item WHERE kind='task' ORDER BY id"):
        row = dict(raw)
        metadata = _metadata(row)
        if metadata is not None:
            result.append((row, metadata))
    return result


def _local_proof(value: dict) -> bool:
    """Check supplied evidence bytes; never run the recorded command."""
    local = value.get("local_clone")
    branch = value.get("local_branch")
    tested = value.get("tested_commit")
    if not local and not branch and not tested and not value.get("evidence"):
        return False
    if not isinstance(local, str) or not Path(local).is_absolute() or not Path(local).is_dir():
        raise WorkflowError("local_clone must name an existing absolute directory")
    branch = _text(branch, "local_branch")
    if branch.startswith("-") or any(c in branch for c in "\n\r\0"):
        raise WorkflowError("local_branch must name a local branch")
    try:
        result = subprocess.run(["git", "-C", local, "rev-parse", "--verify", "--end-of-options",
                                 f"refs/heads/{branch}^{{commit}}"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise WorkflowError("local branch could not be inspected") from None
    if result.returncode or not SHA.fullmatch(result.stdout.strip()):
        raise WorkflowError("local_branch does not resolve to a local commit")
    if tested is not None and _sha(tested, "tested_commit") != result.stdout.strip():
        raise WorkflowError("tested_commit differs from the local branch tip")
    evidence = value.get("evidence", [])
    if not isinstance(evidence, list):
        raise WorkflowError("evidence must be a list")
    verified = bool(tested and evidence)
    for record in evidence:
        if not isinstance(record, dict) or set(record) - {"argv", "cwd", "exit_code", "commit", "artifact", "sha256"}:
            raise WorkflowError("evidence has unsupported fields")
        artifact = record.get("artifact")
        digest = record.get("sha256")
        if not isinstance(artifact, str) or not Path(artifact).is_absolute():
            raise WorkflowError("evidence artifact must be an absolute file path")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise WorkflowError("evidence needs a SHA256 digest")
        try:
            if not stat.S_ISREG(Path(artifact).stat().st_mode):
                raise WorkflowError("evidence artifact must be a regular file")
            with Path(artifact).open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
        except (OSError, ValueError):
            # ValueError: an embedded NUL, which `Path.stat` refuses before any I/O.
            raise WorkflowError("evidence artifact could not be read") from None
        if actual != digest:
            raise WorkflowError("evidence artifact hash differs from the recorded proof")
        if "argv" in record:
            _strings(record["argv"], "evidence argv")
        if "cwd" in record and (not isinstance(record["cwd"], str) or not Path(record["cwd"]).is_absolute()):
            raise WorkflowError("evidence cwd must be an absolute path")
        if record.get("exit_code") is not None and type(record["exit_code"]) is not int:
            raise WorkflowError("evidence exit_code must be an integer or null")
        if record.get("commit") is not None:
            _sha(record["commit"], "evidence commit")
        verified = verified and bool(record.get("argv")) and record.get("cwd") == local \
            and record.get("commit") == tested and record.get("exit_code") == 0
    return bool(verified)


def _draft_path(value) -> None:
    """Check the draft body's digest the way evidence artifacts are checked."""
    if not isinstance(value, dict) or set(value) != {"path", "sha256"} \
            or not isinstance(value["path"], str) or not Path(value["path"]).is_absolute() \
            or not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise WorkflowError("draft_path must be an absolute file path with a SHA256 digest")
    try:
        if not stat.S_ISREG(Path(value["path"]).stat().st_mode):
            raise WorkflowError("issue draft could not be read")
        with Path(value["path"]).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    except (OSError, ValueError):
        # ValueError: an embedded NUL, which `Path.stat` refuses before any I/O.
        raise WorkflowError("issue draft could not be read") from None
    if actual != value["sha256"]:
        raise WorkflowError("issue draft hash differs from the recorded digest")


def _identity(value: dict) -> None:
    """Exactly one of: pull request, filed issue, issue draft, unfiled branch.

    A field is present when it is not None, and a present field is checked
    by type: a truthiness test let `[]`, `{}` and `""` through unchecked.
    """
    def present(name):
        return value.get(name) is not None

    if present("pull_url"):
        value["pull_url"] = _url(value["pull_url"])
    if present("issue_url"):
        value["issue_url"] = _issue_url(value["issue_url"])
    if present("pull_url") and present("issue_url"):
        raise WorkflowError("a contribution is a pull request or an issue, not both")
    if present("target_repo") and (not isinstance(value["target_repo"], str) or not REPO.fullmatch(value["target_repo"])):
        raise WorkflowError("target_repo must be owner/repo")
    if present("draft_title"):
        _text(value["draft_title"], "draft_title")
    if present("draft_path"):
        _draft_path(value["draft_path"])
    if present("issue_url") and present("target_repo") \
            and value["target_repo"].lower() != "/".join(value["issue_url"].split("/")[3:5]):
        raise WorkflowError("target_repo does not match issue_url")
    draft = [present(name) for name in ("target_repo", "draft_title", "draft_path")]
    if present("pull_url") or present("issue_url"):
        # Filing preserves the draft fields; the URL is the identity now.
        return
    if all(draft):
        if value.get("local_clone") or value.get("local_branch"):
            raise WorkflowError("an issue draft has no clone or branch")
    elif any(draft):
        raise WorkflowError("an issue draft needs target_repo, draft_title and draft_path")
    elif not value.get("local_clone") or not value.get("local_branch"):
        raise WorkflowError("unfiled work needs local_clone and local_branch")


def _dependency(value) -> dict:
    if not isinstance(value, dict):
        raise WorkflowError("dependency must be an object")
    kind = value.get("kind")
    allowed = {"item": {"kind", "item"}, "merge": {"kind", "url"}, "issue": {"kind", "url"},
               "release": {"kind", "repo", "tag", "contains_pull", "package"}}
    if kind not in allowed or set(value) - allowed[kind]:
        raise WorkflowError("unsupported dependency kind or fields")
    if kind == "item":
        if type(value.get("item")) is not int or value["item"] < 1:
            raise WorkflowError("item dependency needs a positive item ID")
    elif kind == "merge":
        _url(value.get("url"))
    elif kind == "issue":
        _issue_url(value.get("url"))
    else:
        if not isinstance(value.get("repo"), str) or not REPO.fullmatch(value["repo"]):
            raise WorkflowError("release dependency needs owner/repo")
        _url(value.get("contains_pull"))
        if value.get("tag") is not None:
            _text(value["tag"], "release tag")
        package = value.get("package")
        if package is not None:
            if not isinstance(package, dict) or set(package) - {"name", "version"}:
                raise WorkflowError("release package needs name and optional exact version")
            _text(package.get("name"), "package name")
            if package.get("version") is not None:
                _text(package["version"], "package version")
    return value


def _configuration(connection, item: int, old: dict, changes: dict) -> dict:
    if not isinstance(changes, dict) or set(changes) - FIELDS:
        raise WorkflowError("unsupported contribution fields")
    value = {**old, **copy.deepcopy(changes)}
    _identity(value)
    if "blocked_on" in value:
        _text(value["blocked_on"], "blocked_on", blank=True)
    if "blocking_labels" in value:
        _strings(value["blocking_labels"], "blocking_labels")
    dependencies = value.get("depends_on", [])
    if not isinstance(dependencies, list):
        raise WorkflowError("depends_on must be a list")
    for dependency in dependencies:
        _dependency(dependency)
    if len({_digest(d) for d in dependencies}) != len(dependencies):
        raise WorkflowError("duplicate dependencies are not allowed")
    graph = {row["id"]: metadata.get("depends_on", []) for row, metadata in _registered(connection)}
    graph[item] = dependencies

    def visit(node, ancestors):
        if node in ancestors:
            raise WorkflowError("contribution dependency cycle")
        for dependency in graph.get(node, []):
            if dependency["kind"] == "item":
                target = dependency["item"]
                item_state(connection, target)
                visit(target, ancestors | {node})

    visit(item, set())
    _local_proof(value)
    for row, metadata in _registered(connection):
        if row["id"] == item:
            continue
        if value.get("pull_url") and metadata.get("pull_url") == value["pull_url"]:
            raise WorkflowError("this pull request already belongs to another contribution")
        if value.get("issue_url") and metadata.get("issue_url") == value["issue_url"]:
            raise WorkflowError("this issue already belongs to another contribution")
        if value.get("local_clone") and metadata.get("local_clone") \
                and value.get("local_branch") == metadata.get("local_branch") \
                and Path(value["local_clone"]).resolve() == Path(metadata["local_clone"]).resolve():
            raise WorkflowError("this local clone and branch already belong to another contribution")
        if value.get("target_repo") and value.get("draft_path") and metadata.get("target_repo") \
                and isinstance(metadata.get("draft_path"), dict) \
                and value["target_repo"].lower() == metadata["target_repo"].lower() \
                and Path(value["draft_path"]["path"]).resolve() == Path(metadata["draft_path"]["path"]).resolve():
            raise WorkflowError("this issue draft already belongs to another contribution")
    return value


def capture(connection, *, title, changes, who) -> dict:
    """Create an explicit local task; ordinary observed PRs never call this."""
    with transaction(connection):
        result = capture_task(connection, title=title, who=who)
        return configure(connection, result["item"]["id"], changes, who=who,
                         expected_revision=result["revision"])


def configure(connection, item, changes, *, who, expected_revision=None) -> dict:
    who = _text(who, "who")
    with transaction(connection):
        current = _checked_state(connection, item, expected_revision)
        if current["item"]["kind"] != "task":
            raise WorkflowError("contributions use local task items")
        old = _metadata(current["item"]) or {}
        value = _configuration(connection, item, old, changes)
        if old != value:
            fields = json.loads(current["item"]["fields"] or "{}")
            fields["contribution"] = value
            set_item_fields(connection, item, fields=fields)
            add_note(connection, item, "comment", f"Updated contribution metadata by {who}", session=who)
            if old.get("depends_on", []) != value.get("depends_on", []):
                key = f"item:{item}"
                state = snapshot(connection, key)
                state["observation"] = {"configured": value.get("depends_on", []), "satisfied": False}
                _publish_attention(connection, state, [], item)
                _save(connection, state)
            if old.get("blocking_labels", []) != value.get("blocking_labels", []):
                # Invalidate collectors that read the prior operator policy.
                for prefix, name in OBSERVED.items():
                    if value.get(name):
                        _save(connection, snapshot(connection, prefix + value[name]))
        return item_state(connection, item)


def snapshot(connection, key) -> dict:
    key = _key(key)
    row = connection.execute("SELECT id,body FROM state WHERE kind='checkpoint' AND key=? ORDER BY id DESC LIMIT 1",
                             ("contribution:" + key,)).fetchone()
    body = {"observation": None, "attention": [], "notifications": {}, "acknowledged": [], "ci_baseline": None}
    if row:
        try:
            stored = json.loads(row["body"])
            if not isinstance(stored, dict):
                raise ValueError()
            body.update(stored)
        except (TypeError, ValueError, RecursionError):
            raise WorkflowError("contribution checkpoint is malformed") from None
    return {**body, "key": key, "revision": _digest({"key": key, "id": row["id"] if row else None, "body": body})}


def _save(connection, state: dict) -> dict:
    body = {k: v for k, v in state.items() if k not in {"key", "revision"}}
    record_state(connection, "checkpoint", key="contribution:" + state["key"], body=body)
    return snapshot(connection, state["key"])


def _checked(connection, key, revision) -> dict:
    state = snapshot(connection, key)
    if not isinstance(revision, str) or revision != state["revision"]:
        raise StaleItem("contribution changed; reload before applying this observation or acknowledgement")
    return state


def _hold(connection, state, reason) -> dict:
    record_state(connection, "heartbeat", key="contribution-observe:" + state["key"],
                 body={"ok": False, "reason": str(reason)})
    return state


def _fresh(state, observed_at) -> None:
    moment = _moment(observed_at)
    previous = (state["observation"] or {}).get("observed_at")
    if previous and moment < _moment(previous):
        raise WorkflowError("observation is older than the last complete observation")


def _event(identifier, reason, *, kind="activity", url=None) -> dict:
    return {"id": str(identifier), "reason": reason, "kind": kind, "url": url}


def _context_ids(observation: dict, previous: dict) -> dict[str, str]:
    prior = previous.get("observation") or {}
    identifiers = {}
    for why in observation.get("why", []) if observation["state"] == "open" else []:
        if why not in {"assigned", "review-requested"}:
            continue
        identifier = "context:" + why
        if prior.get("state") == "open" and why in prior.get("why", []):
            identifier = previous.get("context_events", {}).get(why, identifier)
        elif identifier in previous["notifications"]:
            # A confirmed disappearance ends the episode, not its delivery history.
            identifier += ":" + _digest({"revision": previous["revision"], "why": why})
        identifiers[why] = identifier
    return identifiers


def _pull_attention(observation: dict, previous: dict, contexts: dict[str, str]) -> tuple[list[dict], str | None]:
    attention = []
    operator = str(observation["operator"]["id"])
    current_state = observation["state"]
    context = [_event(identifier, why.replace("-", " ").capitalize()) for why, identifier in contexts.items()]
    if str(observation["author"]["id"]) != operator:
        return context, previous.get("ci_baseline")
    latest = {}
    for position, review in enumerate(observation["reviews"]):
        actor = str(review["actor_id"])
        if actor == operator or review["state"] not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        order = (_moment(review["at"]), position)
        if actor not in latest or order > latest[actor][0]:
            latest[actor] = (order, review)
    if current_state == "open":
        for _, review in latest.values():
            if review["state"] == "CHANGES_REQUESTED":
                attention.append(_event("review:" + str(review["id"]), "Changes requested"))
    lifecycle = {}
    for position, event in enumerate(observation["events"]):
        kind = event["kind"]
        family = "label:" + str(event.get("label")) if kind in {"label_added", "label_removed"} else \
            "draft" if kind in {"converted_to_draft", "ready_for_review"} else \
            "closed" if kind in {"closed", "reopened"} else None
        if family:
            order = (_moment(event["at"]), position)
            if family not in lifecycle or order > lifecycle[family][0]:
                lifecycle[family] = (order, event)
    events = [event for event in observation["events"] if event["kind"] == "comment"]
    events.extend(value[1] for value in lifecycle.values())
    for event in events:
        if str(event["actor_id"]) == operator:
            continue
        kind = event["kind"]
        reason = None
        if kind == "comment" and (event.get("maintainer") is True or event.get("mentions_operator") is True):
            reason = "Maintainer comment" if event.get("maintainer") else "Mentioned you"
        elif kind == "label_added" and event.get("label") in observation.get("blocking_labels", []) \
                and event.get("label") in observation.get("labels", []):
            reason = "Blocking label: " + event["label"]
        elif kind == "converted_to_draft" and observation["draft"] is True:
            reason = "Maintainer converted pull request to draft"
        elif kind == "closed" and current_state == "closed":
            reason = "Closed without merge"
        if reason and current_state != "merged":
            attention.append(_event("event:" + str(event["id"]), reason, url=event.get("url")))
    baseline = previous.get("ci_baseline")
    if current_state == "open":
        ci = observation["ci"]
        prior_ci = [v for v in previous["attention"] if v["kind"] == "ci"]
        if ci == "failure" and baseline == "success":
            identity = _digest({"head": observation["head"], "checks": sorted(observation["ci_ids"])})
            attention.append(_event("ci:" + identity, "CI changed from passing to failing", kind="ci"))
        elif ci in {"failure", "pending", "unknown"}:
            attention.extend(prior_ci)
        if observation["mergeable"] == "conflicting":
            if previous.get("mergeability_baseline") == "conflicting":
                attention.extend(v for v in previous["attention"] if v["kind"] == "conflict")
            else:
                identity = _digest({"head": observation["head"], "base": observation["base"]})
                attention.append(_event("conflict:" + identity, "Pull request has merge conflicts", kind="conflict"))
        elif observation["mergeable"] == "unknown":
            attention.extend(v for v in previous["attention"] if v["kind"] == "conflict")
        attention.extend(context)
        if ci in {"success", "failure"}:
            baseline = ci
    return attention, baseline


def _publish_attention(connection, state, attention, item=None) -> None:
    unique = {event["id"]: event for event in attention}
    active = [event for identifier, event in unique.items() if identifier not in state["acknowledged"]]
    for old in state["attention"]:
        if old["id"] not in unique:
            notice = state["notifications"].get(old["id"], {})
            if notice.get("note_id"):
                resolve_note(connection, notice["note_id"])
    state["attention"] = active
    for event in active:
        if event["id"] in state["notifications"]:
            continue
        notice = {"status": "pending", "event": event, "created_at": now()}
        if item is not None:
            notice["note_id"] = add_note(connection, item, "followup", event["reason"] +
                                        ("\n" + event["url"] if event.get("url") else ""), session="contribution-observer")
        state["notifications"][event["id"]] = notice


def observe_pull(connection, url, observation, *, expected_revision) -> dict:
    url = _url(url)
    with transaction(connection):
        state = _checked(connection, "github:" + url, expected_revision)
        if not isinstance(observation, dict) or observation.get("complete") is not True:
            reason = observation.get("reason") if isinstance(observation, dict) else None
            return _hold(connection, state, "Incomplete GitHub observation" + (f": {reason}" if isinstance(reason, str) and reason else ""))
        value = copy.deepcopy(observation)
        _fresh(state, value.get("observed_at"))
        for name in ("operator", "author"):
            if not isinstance(value.get(name), dict) or not value[name].get("id"):
                return _hold(connection, state, "GitHub actor identity is unknown")
        for name in ("head", "base"):
            _sha(value.get(name), name)
        if value.get("state") not in {"open", "closed", "merged"} or type(value.get("draft")) is not bool:
            raise WorkflowError("invalid pull-request lifecycle observation")
        if value.get("mergeable") not in {"mergeable", "conflicting", "unknown"} \
                or value.get("ci") not in {"success", "failure", "pending", "unknown"}:
            raise WorkflowError("invalid mergeability or CI observation")
        _strings(value.get("ci_ids"), "ci_ids")
        if value.get("ci_head") != value["head"]:
            raise WorkflowError("CI observation does not describe the current pull-request head")
        if value["ci"] in {"success", "failure"} and not value["ci_ids"]:
            raise WorkflowError("conclusive CI needs check-run or status identities")
        for name in ("why", "blocking_labels", "labels"):
            _strings(value.get(name, []), name)
        _text(value.get("title"), "title")
        if not isinstance(value.get("repo"), str) or value["repo"].lower() != "/".join(url.split("/")[3:5]):
            raise WorkflowError("observation repository differs from pull URL")
        value["repo"] = value["repo"].lower()
        for name in ("reviews", "events"):
            if not isinstance(value.get(name), list):
                raise WorkflowError(f"{name} must be a list")
            for event in value[name]:
                if not isinstance(event, dict) or not event.get("id") or not event.get("actor_id"):
                    return _hold(connection, state, "GitHub event identity is unknown")
                _moment(event.get("at"))
                if name == "reviews" and not isinstance(event.get("state"), str):
                    raise WorkflowError("review state is missing")
                if name == "events" and event.get("kind") not in EVENTS:
                    raise WorkflowError("unsupported contribution event kind")
        registered = next(((row, metadata) for row, metadata in _registered(connection) if metadata.get("pull_url") == url), None)
        item = registered[0]["id"] if registered else None
        if registered and value.get("blocking_labels", []) != registered[1].get("blocking_labels", []):
            raise StaleItem("blocking-label policy changed; recollect with the current configuration")
        contexts = _context_ids(value, state)
        attention, baseline = _pull_attention(value, state, contexts)
        state["context_events"] = contexts
        state["observation"] = value
        state["ci_baseline"] = baseline
        if value["mergeable"] != "unknown":
            state["mergeability_baseline"] = value["mergeable"]
        _publish_attention(connection, state, attention, item)
        result = _save(connection, state)
        record_state(connection, "heartbeat", key="contribution-observe:" + state["key"], body={"ok": True})
        return result


def _issue_attention(observation: dict, previous: dict) -> list[dict]:
    """Issue events that need the operator; wider than the pull rule on comments.

    A watched issue has no review machinery to carry a maintainer's answer,
    so any comment by someone else is the signal. The label rule is the pull
    rule unchanged, and the close reason is the operator-facing string:
    "Closed without merge" is pull-request vocabulary and never appears here.
    """
    attention = []
    operator = str(observation["operator"]["id"])
    current_state = observation["state"]
    lifecycle = {}
    for position, event in enumerate(observation["events"]):
        kind = event["kind"]
        family = "label:" + str(event.get("label")) if kind in {"label_added", "label_removed"} else \
            "closed" if kind in {"closed", "closed_completed", "closed_not_planned", "reopened"} else None
        if family:
            order = (_moment(event["at"]), position)
            if family not in lifecycle or order > lifecycle[family][0]:
                lifecycle[family] = (order, event)
    events = [event for event in observation["events"] if event["kind"] == "comment"]
    events.extend(value[1] for value in lifecycle.values())
    for event in events:
        if str(event["actor_id"]) == operator:
            continue
        kind = event["kind"]
        reason = None
        if kind == "comment":
            reason = "Maintainer comment" if event.get("maintainer") is True \
                else "Mentioned you" if event.get("mentions_operator") is True else "New comment"
        elif kind == "label_added" and event.get("label") in observation.get("blocking_labels", []) \
                and event.get("label") in observation.get("labels", []):
            reason = "Blocking label: " + event["label"]
        elif kind == "closed_completed" and current_state == "closed":
            reason = "Closed as completed"
        elif kind == "closed_not_planned" and current_state == "closed":
            reason = "Closed as not planned"
        elif kind == "closed" and current_state == "closed":
            reason = "Closed"
        elif kind == "reopened" and current_state == "open":
            reason = "Reopened"
        if reason:
            attention.append(_event("event:" + str(event["id"]), reason, url=event.get("url")))
    return attention


def observe_issue(connection, url, observation, *, expected_revision) -> dict:
    """The issue counterpart of `observe_pull`: no CI, mergeability or reviews."""
    url = _issue_url(url)
    with transaction(connection):
        state = _checked(connection, "issue:" + url, expected_revision)
        if not isinstance(observation, dict) or observation.get("complete") is not True:
            reason = observation.get("reason") if isinstance(observation, dict) else None
            return _hold(connection, state, "Incomplete GitHub observation" + (f": {reason}" if isinstance(reason, str) and reason else ""))
        value = copy.deepcopy(observation)
        _fresh(state, value.get("observed_at"))
        for name in ("operator", "author"):
            if not isinstance(value.get(name), dict) or not value[name].get("id"):
                return _hold(connection, state, "GitHub actor identity is unknown")
        if PULL_ONLY & set(value):
            raise WorkflowError("issue observation carries pull-request fields")
        if value.get("state") not in {"open", "closed"} or value.get("state_reason") not in {None, "completed", "not_planned"}:
            raise WorkflowError("invalid issue lifecycle observation")
        for name in ("why", "blocking_labels", "labels"):
            _strings(value.get(name, []), name)
        _text(value.get("title"), "title")
        if not isinstance(value.get("repo"), str) or value["repo"].lower() != "/".join(url.split("/")[3:5]):
            raise WorkflowError("observation repository differs from issue URL")
        value["repo"] = value["repo"].lower()
        if not isinstance(value.get("events"), list):
            raise WorkflowError("events must be a list")
        for event in value["events"]:
            if not isinstance(event, dict) or not event.get("id") or not event.get("actor_id"):
                return _hold(connection, state, "GitHub event identity is unknown")
            _moment(event.get("at"))
            if event.get("kind") not in ISSUE_EVENTS:
                raise WorkflowError("unsupported contribution event kind")
        registered = next(((row, metadata) for row, metadata in _registered(connection) if metadata.get("issue_url") == url), None)
        item = registered[0]["id"] if registered else None
        if registered and value.get("blocking_labels", []) != registered[1].get("blocking_labels", []):
            raise StaleItem("blocking-label policy changed; recollect with the current configuration")
        attention = _issue_attention(value, state)
        state["observation"] = value
        _publish_attention(connection, state, attention, item)
        result = _save(connection, state)
        record_state(connection, "heartbeat", key="contribution-observe:" + state["key"], body={"ok": True})
        return result


def _satisfied(dependency, proof) -> bool:
    if not isinstance(proof, dict):
        return False
    if dependency["kind"] == "merge":
        return proof.get("url") == dependency["url"] and proof.get("merged") is True \
            and isinstance(proof.get("merge_commit_sha"), str) and bool(SHA.fullmatch(proof["merge_commit_sha"]))
    if dependency["kind"] == "issue":
        # Closed as completed, or a referencing pull request merged; a
        # not-planned close resolves nothing.
        if proof.get("url") != dependency["url"]:
            return False
        if proof.get("closed") is True and proof.get("state_reason") == "completed":
            return True
        merged = proof.get("merged_pull")
        try:
            return isinstance(merged, str) and _url(merged) == merged
        except WorkflowError:
            return False
    if not dependency.get("tag"):
        return False
    if not all(proof.get(k) == dependency.get(k) for k in ("repo", "tag", "contains_pull")):
        return False
    if proof.get("merged") is not True or proof.get("published") is not True or proof.get("ancestor") is not True:
        return False
    if not all(isinstance(proof.get(k), str) and SHA.fullmatch(proof[k]) for k in ("tag_commit", "merge_commit_sha")):
        return False
    package = dependency.get("package")
    if package:
        actual = proof.get("package", {})
        if not package.get("version") or not isinstance(actual, dict) \
                or any(actual.get(k) != package[k] for k in ("name", "version")):
            return False
        files = actual.get("files", [])
        if not isinstance(files, list) or not any(isinstance(f, dict) and isinstance(f.get("filename"), str)
                and bool(f["filename"].strip()) and "\0" not in f["filename"]
                and f.get("yanked") is False and isinstance(f.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) for f in files):
            return False
    return True


def observe_dependencies(connection, item, observations, *, observed_at, expected_revision, complete=True, reason="") -> dict:
    with transaction(connection):
        state = _checked(connection, f"item:{item}", expected_revision)
        metadata = _metadata(item_state(connection, item)["item"])
        if metadata is None:
            raise WorkflowError("item is not a registered contribution")
        _fresh(state, observed_at)
        if complete is not True or not isinstance(observations, list):
            # `reason` is the collector's account of why -- it is what the
            # item's freshness shows, so a hold names the missing proof.
            return _hold(connection, state, "Incomplete dependency observation" + (f": {reason}" if reason else ""))
        dependencies = metadata.get("depends_on", [])
        supplied = {}
        for row in observations:
            if not isinstance(row, dict) or row.get("dependency") not in dependencies:
                raise WorkflowError("dependency observation does not match current configuration")
            identity = _digest(row["dependency"])
            if identity in supplied:
                raise WorkflowError("duplicate dependency observation")
            supplied[identity] = row
        verified = []
        for dependency in dependencies:
            if dependency["kind"] == "item":
                current = item_state(connection, dependency["item"])
                verified.append({"dependency": dependency, "state": "satisfied" if current["item"]["status"] == "done" else "pending",
                                 "proof": {"item": dependency["item"], "revision": current["revision"], "status": current["item"]["status"]}})
                continue
            row = supplied.get(_digest(dependency))
            if row is None or row.get("state") == "unknown":
                return _hold(connection, state, "Dependency state is unknown")
            if row.get("state") not in {"satisfied", "pending"}:
                raise WorkflowError("invalid dependency state")
            if row["state"] == "satisfied" and not _satisfied(dependency, row.get("proof")):
                return _hold(connection, state, "Dependency satisfaction lacks exact publication or merge proof")
            verified.append(row)
        for row in verified:
            if row["dependency"]["kind"] == "release" and row["state"] == "satisfied":
                merges = [m for m in verified if m["dependency"]["kind"] == "merge"
                          and m["dependency"]["url"] == row["dependency"]["contains_pull"]]
                if merges and any(m["state"] != "satisfied" or m["proof"]["merge_commit_sha"] != row["proof"]["merge_commit_sha"] for m in merges):
                    return _hold(connection, state, "Release proof does not contain the configured merged commit")
        satisfied = bool(dependencies) and all(row["state"] == "satisfied" for row in verified)
        previous = state["observation"] or {}
        attention = state["attention"] if satisfied else []
        if satisfied and previous.get("satisfied") is not True:
            identity = _digest({"configuration": dependencies, "proofs": verified})
            attention = [_event("unblocked:" + identity, "All contribution dependencies are satisfied", kind="newly_unblocked")]
        state["observation"] = {"observed_at": observed_at, "configured": dependencies, "dependencies": verified, "satisfied": satisfied}
        _publish_attention(connection, state, attention, item)
        result = _save(connection, state)
        record_state(connection, "heartbeat", key="contribution-observe:" + state["key"], body={"ok": True})
        return result


def _freshness(connection, sources) -> dict:
    times = [(source["observation"] or {}).get("observed_at") for source in sources]
    times = [value for value in times if value]
    result = {"status": "current" if times else "unknown", "reason": "" if times else "Not yet observed", "observed_at": max(times) if times else None}
    for source in sources:
        row = connection.execute("SELECT body FROM state WHERE kind='heartbeat' AND key=? ORDER BY id DESC LIMIT 1",
                                 ("contribution-observe:" + source["key"],)).fetchone()
        if row:
            body = json.loads(row["body"])
            if body.get("ok") is False:
                result.update(status="unknown", reason=body.get("reason", "Observation failed"))
    if times and (datetime.now(timezone.utc) - max(map(_moment, times))).total_seconds() > 86400:
        result.update(status="unknown", reason=result["reason"] or "Last complete observation is older than one day")
    return result


def _projection_row(connection, row, metadata, sources) -> dict:
    source = next((s for s in sources if _observed_url(s["key"])), sources[0])
    observation = source["observation"] or {}
    url = metadata.get("pull_url") or metadata.get("issue_url") or _observed_url(source["key"])
    active = [{"key": s["key"], "revision": s["revision"], "event_ids": [e["id"] for e in s["attention"]],
               "reasons": [e["reason"] for e in s["attention"]]} for s in sources if s["attention"]]
    active.sort(key=lambda a: (0 if any(e["kind"] == "newly_unblocked" for s in sources if s["key"] == a["key"] for e in s["attention"]) else 1, a["key"]))
    primary = active[0] if active else {"key": source["key"], "revision": source["revision"], "event_ids": [], "reasons": []}
    waiting = bool(metadata.get("blocked_on") or metadata.get("depends_on"))
    needs_you = bool(active) or (not url and not waiting)
    # An issue or a pull request closed without merge awaits no one; a later
    # observation that reads `open` returns the row to live work.
    closed = observation.get("state") == "closed"
    lane = "awaiting_you" if needs_you else "closed" if closed \
        else "merged" if observation.get("state") == "merged" else "awaiting_them"
    if any(e["kind"] == "newly_unblocked" for s in sources for e in s["attention"]):
        lane = "newly_unblocked"
    try:
        verified = _local_proof(metadata)
    except WorkflowError:
        verified = False
    draft = metadata.get("draft_path")
    draft_verified = False
    if draft:
        try:
            _draft_path(draft)
            draft_verified = True
        except WorkflowError:
            pass
    reasons = list(dict.fromkeys(reason for a in active for reason in a["reasons"]))
    if not url and draft:
        reasons.append("Issue draft has not been filed" if draft_verified else "Issue draft has not been filed; draft body is unverified")
    elif not url:
        reasons.append("Local contribution has not been filed" if verified else "Local contribution has not been filed; test evidence is unverified")
    if waiting and not active:
        reasons.append(metadata.get("blocked_on") or "Waiting for contribution dependencies")
    freshness = _freshness(connection, sources)
    target = metadata.get("target_repo")
    return {**primary, "item_id": row.get("id"), "url": url,
            "repo": observation.get("repo") or row.get("repo") or ("/".join(url.split("/")[3:5]) if url
                                                                  else target.lower() if target else metadata.get("local_clone")),
            "title": row.get("title") or observation.get("title", url or "Local contribution"),
            "local_status": row.get("status"), "external_state": observation.get("state"), "lane": lane,
            "needs_you": needs_you, "reasons": reasons, "attention_sources": active,
            "observed_at": freshness["observed_at"], "freshness": freshness, "blocked_on": metadata.get("blocked_on", ""),
            "depends_on": metadata.get("depends_on", []), "blocking_labels": metadata.get("blocking_labels", []),
            "local_clone": metadata.get("local_clone"), "local_branch": metadata.get("local_branch"),
            "tested_commit": metadata.get("tested_commit"), "evidence": metadata.get("evidence", []), "evidence_verified": verified,
            "issue_url": metadata.get("issue_url"), "target_repo": target, "draft_title": metadata.get("draft_title"),
            "draft_path": draft, "draft_verified": draft_verified,
            "notification_state": {s["key"]: s["notifications"] for s in sources}}


def projection(connection, *, repo=None) -> list[dict]:
    """One ordered answer, including unfiled tasks and closed-unmerged PRs."""
    if repo is not None and not isinstance(repo, (str, list, tuple)):
        raise WorkflowError("repo filter must be an identity or list of identities")
    filters = None if repo is None else {repo} if isinstance(repo, str) else set(_strings(list(repo), "repo filters"))
    if filters is not None:
        # A local path filter matches its `~/` key and its legacy absolute
        # form alike; an `owner/repo` identity probes as itself (sd:1439).
        filters = {form for value in filters for form in paths.keys(value)}
    connection.execute("SAVEPOINT contribution_projection")
    try:
        result = []
        registered_urls = set()
        for row, metadata in _registered(connection):
            sources = [snapshot(connection, f"item:{row['id']}")]
            for prefix, name in OBSERVED.items():
                if metadata.get(name):
                    registered_urls.add(metadata[name])
                    sources.append(snapshot(connection, prefix + metadata[name]))
            value = _projection_row(connection, row, metadata, sources)
            if filters is None or filters.intersection({value["repo"], row.get("repo"), metadata.get("local_clone")}):
                result.append(value)
        keys = connection.execute("SELECT DISTINCT key FROM state WHERE kind='checkpoint' AND ("
                                  + " OR ".join("key LIKE ?" for _ in OBSERVED) + ")",
                                  tuple(f"contribution:{prefix}%" for prefix in OBSERVED))
        for entry in keys:
            key = entry["key"][len("contribution:"):]
            if _observed_url(key) in registered_urls:
                continue
            value = _projection_row(connection, {}, {}, [snapshot(connection, key)])
            if filters is None or value["repo"] in filters:
                result.append(value)
        return sorted(result, key=lambda value: (LANES[value["lane"]], value["key"]))
    finally:
        connection.execute("RELEASE contribution_projection")


def claim_notification(connection, key, event_id, *, owner) -> dict | None:
    owner = _text(owner, "notification owner")
    with transaction(connection):
        state = snapshot(connection, key)
        notice = state["notifications"].get(event_id)
        if not notice or notice["status"] != "pending" or event_id not in {e["id"] for e in state["attention"]}:
            return None
        token = uuid.uuid4().hex
        notice.update(status="sending", owner=owner, token=token, claimed_at=now())
        _save(connection, state)
        return {"key": state["key"], "event_id": event_id, "token": token, "owner": owner,
                "title": "Contribution needs attention", "message": notice["event"]["reason"],
                "url": notice["event"].get("url") or _observed_url(state["key"])}


def finish_notification(connection, key, event_id, token, *, outcome) -> dict:
    if outcome not in {"sent", "not_started", "uncertain"}:
        raise WorkflowError("notification outcome must be sent, not_started, or uncertain")
    with transaction(connection):
        state = snapshot(connection, key)
        notice = state["notifications"].get(event_id)
        if not notice or notice.get("status") != "sending" or notice.get("token") != token:
            raise WorkflowError("notification claim is absent, stale, or belongs to another owner")
        notice.update(status="pending" if outcome == "not_started" else outcome, outcome=outcome, finished_at=now())
        return _save(connection, state)


def acknowledge(connection, key, event_ids, *, who, expected_revision) -> dict:
    who = _text(who, "who")
    _strings(event_ids, "event_ids")
    if not event_ids or len(set(event_ids)) != len(event_ids):
        raise WorkflowError("acknowledgement needs distinct event IDs")
    with transaction(connection):
        state = _checked(connection, key, expected_revision)
        active = {event["id"] for event in state["attention"]}
        if not set(event_ids) <= active:
            raise WorkflowError("acknowledgement includes an event that is no longer active")
        for identifier in event_ids:
            notice = state["notifications"][identifier]
            if notice.get("note_id"):
                resolve_note(connection, notice["note_id"])
            notice["acknowledged_by"] = who
            notice["acknowledged_at"] = now()
        state["acknowledged"] = sorted(set(state["acknowledged"]) | set(event_ids))
        state["attention"] = [event for event in state["attention"] if event["id"] not in event_ids]
        return _save(connection, state)
