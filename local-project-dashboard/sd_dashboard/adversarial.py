"""Adversarial review (sd:5064): one click runs `local-adversarial-gate` on a task, a brief, a repository or a change.

The operator asked on 2026-10-18 (a note on sd:5064) for a hostile second read by another model than the one that
wrote the work: codex, at the model and effort `<config>/adversarial-gate/.env` names (`ADVERSARIAL_MODEL`,
`ADVERSARIAL_EFFORT`), else `MODEL` at `EFFORT`. A page posts `/api/adversarial` with `{"kind", "target"}`:

  task    an item id. The item's repository is the checkout; an item without one is refused, as Fix refuses it.
  brief   the id of a routine brief (kind `report`, source `brief`). Its repository, else this checkout.
  repo    a registered repository, by its key or its path.
  change  a checkout directory, a worktree or a clone of a registered repository. Optional: `base`, the ref it is
          compared with (default `origin/HEAD`, then `origin/main`, then `main`); `item`, the item it belongs to; and
          `body`, the file holding its pull-request body.

`start` runs in the POST, in four steps, and answers 202 at once:

1. The lock, `<state>/lock`, taken without waiting: a second run while one holds it is a 409 naming the run.
2. The target, resolved to a checkout, its registered repository and the lens's inputs.
3. The run's state file, `<state>/runs/<id>.json`, as `queued`, with the item's or the change's text beside it as
   `<id>.input.md`. Codex writes its result beside them as `<id>.md`.
4. This file as a script, in a session of its own, holding the lock's descriptor: the lock is released only when
   the runner and every process under it have exited, so nothing runs codex beside it.

The runner (`run`) marks the run `running`, runs `adversarial-gate.sh run`, bounded by the gate at `TIMEOUT` (exit
124) and here at `TIMEOUT + GRACE`, and marks it `done` or `failed` with the reason. It then files the outcome in the
database the server reads, in one transaction, as the operator asked in note #22943 on sd:5064:

- Each titled hypothesis is a recommendation: one Approve-or-Reject question in a review batch (`sd_db.review`,
  queue `QUEUE`). An approval files a task in the batch's repository, titled with the question's first line
  (sd:4924), so the runner files one batch per repository the recommendations name.
- A routine brief (`sd_db.routine_briefs`) holds the target, the model and effort, the verdict and the numbered
  recommendations, each naming its batch. Each batch header's `Source:` names the brief's stem.
- When the target is an item (a task, a brief, or the change's `item`), a `comment` note names the brief and the
  batches, or the failure. A failed run files no brief and no batch.

Nothing here approves, merges, publishes or posts anywhere else; the operator picks recommendations on the Review page.

`status` answers `GET /api/adversarial/<id>` from the state file. A run whose runner is gone without an outcome reads
`failed`, with the reason, and a run queued for longer than `STARTUP` that never started reads `failed` too. A run
whose runner is gone after its outcome but before it recorded the filing reads `filed` as not known.

The runner is this file run by path. It imports `sd_db` from the folder the server loaded it from and opens the
database file the server's connection reads; the state file names both.
"""

from __future__ import annotations

import datetime
import fcntl
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

__all__ = ["EFFORT", "GATE", "KINDS", "MODEL", "NOTE", "QUEUE", "TIMEOUT", "recommendations", "request", "run",
           "start", "status"]

#: This repository's checkout: a brief that names no repository is reviewed here.
CHECKOUT = Path(__file__).resolve().parents[2]
GATE = CHECKOUT / "local-adversarial-gate" / "adversarial-gate.sh"
RANK = CHECKOUT / "local-adversarial-gate" / "rank.py"
KINDS = ("task", "brief", "repo", "change")
#: The model and effort when `<config>/adversarial-gate/.env` names none.
MODEL = "gpt-6-astra"
EFFORT = "high"
#: The gate's bound on codex, in seconds; the runner's own bound on the gate is `GRACE` longer.
TIMEOUT = 1800
GRACE = 60
#: How long a queued run may wait for its runner to say it started before it reads as failed.
STARTUP = 60
GIT_SECONDS = 10
#: Hypotheses whose titles the note names.
TOP = 3
#: The longest pull-request body read into the input file, in bytes.
BODY_MAX = 200_000
#: The review queue the recommendations are asked in, and who the runner writes as.
QUEUE = "adversarial"
WHO = "dashboard-adversarial"
#: Per review: the most recommendations filed. Per recommendation: the longest title in characters, reasoning in
#: bytes and evidence line in characters, and the most evidence lines (`sd_db.review` takes five). 30 recommendations
#: at these sizes stay well under a brief's 200,000 bytes, so no overflow file is needed.
RECOMMENDATIONS = 30
TITLE_CHARS = 120
WHY_BYTES = 3000
EVIDENCE_CHARS = 300
EVIDENCE_LINES = 5
RUN_ID = re.compile(r"(task|brief|repo|change)-[A-Za-z0-9._-]{1,60}-[0-9]{8}T[0-9]{6}Z")
REF = re.compile(r"[A-Za-z0-9._/-]{1,200}")
#: A line the lenses ask for under each hypothesis, bulleted or bold or neither.
TAG = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*)?(Recommendation|Evidence|Repo)(?:\*\*)?\s*:(?:\*\*)?\s*(.*?)\s*$", re.IGNORECASE)
#: A line `sd_db.review` refuses in a question's text, as it would read it as the question's own field.
RESERVED = re.compile(r"^[ \t]*(Option|Subject|Evidence):")

NOTE = ("Adversarial review ({model}, {effort} effort): brief {stem} (sd:{brief}); recommendations: {count}, to pick in "
        "review batch {batches}. Top hypotheses: {titles}. These are hypotheses for a person to check, not findings.")
FAILED = "Adversarial review failed ({model}, {effort} effort): {reason}. Input: {input}."


# -- where things live -------------------------------------------------------------------------------------------


def state_dir() -> Path:
    """`st_state_dir adversarial-gate`: what runs write and nobody edits."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "system" / "adversarial-gate"


def settings() -> tuple[str, str]:
    """The model and effort: exported, else `<config>/adversarial-gate/.env`, else `MODEL` and `EFFORT`."""
    lib = str(CHECKOUT / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import system_tools_config

    saved = system_tools_config.read_env("adversarial-gate")
    model = system_tools_config.setting("ADVERSARIAL_MODEL", saved) or MODEL
    effort = system_tools_config.setting("ADVERSARIAL_EFFORT", saved) or EFFORT
    return model, effort


def _stamp(now: datetime.datetime) -> str:
    return now.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(now: datetime.datetime) -> str:
    return now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, document: dict) -> None:
    """The state file, replaced whole, so a reader never sees half of one."""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)


def _git(directory: Path, *args: str) -> str | None:
    """One bounded git read; None when it fails, so a caller names what it could not learn."""
    try:
        done = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=GIT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


# -- step 2: the target ------------------------------------------------------------------------------------------


def _text(body) -> str:
    """An item body's text: a task keeps `{"text": ...}`, an older row plain text."""
    try:
        value = json.loads(body or "")
    except ValueError:
        return body or ""
    return value.get("text") or "" if isinstance(value, dict) else body or ""


def _item_text(state: dict, *, heading: str, extra: list[str]) -> str:
    """The item as the reviewer reads it: its fields, its body and every note but the status changes."""
    row = state["item"]
    notes = [note for note in state["notes"] if note["kind"] != "status_change"]
    lines = [f"# {heading} sd:{row['id']}: {row['title']}", "",
             f"- kind: {row['kind']}; status: {row['status']}; priority: {row.get('priority') or 'none'}; "
             f"due: {row.get('due') or 'none'}",
             f"- repository: {row.get('repo') or 'none'}", *extra, "", "## Body", "",
             _text(row.get("body")).strip() or "(no body)", "", "## Notes", ""]
    for note in notes:
        resolved = " · resolved" if note.get("resolved_at") else ""
        lines += [f"### #{note['id']} · {note['kind']} · {note['timestamp']}{resolved}", "", note["body"].strip(), ""]
    if not notes:
        lines.append("(no notes)")
    return "\n".join(lines).rstrip() + "\n"


def _checkout_of(row: dict, *, fallback: Path | None) -> Path:
    from sd_db import paths, workflow

    if not row.get("repo"):
        if fallback is None:
            raise workflow.WorkflowError(f"item {row['id']} names no repository, so the review has no checkout to read; "
                                         f"set its repo first")
        return fallback
    directory = paths.expand(row["repo"])
    if not directory.is_dir():
        raise workflow.WorkflowError(f"{row['repo']} is not a directory on this machine, so the review has no checkout")
    return directory


def _resolve_task(connection, target) -> dict:
    from sd_db import workflow

    state = workflow.item_state(connection, _item_id(target, "target"))
    row = state["item"]
    checkout = _checkout_of(row, fallback=None)
    design, extra = "none", ["- design document: none"]
    if row.get("path"):
        linked = Path(row["path"]) if Path(row["path"]).is_absolute() else checkout / row["path"]
        if linked.is_file():
            design, extra = str(linked), [f"- design document: {linked}"]
        else:
            extra = [f"- design document: {row['path']}, not found in the checkout"]
    return {"checkout": checkout, "repo": checkout, "label": str(row["id"]), "item": row["id"],
            "input": _item_text(state, heading="Item", extra=extra), "sets": {"DESIGN_PATH": design}}


def _resolve_brief(connection, target) -> dict:
    from sd_db import workflow

    state = workflow.item_state(connection, _item_id(target, "target"))
    row = state["item"]
    if row["kind"] != "report" or row.get("source") != "brief":
        raise workflow.WorkflowError(f"item {row['id']} is not a routine brief; review it as a task")
    checkout = _checkout_of(row, fallback=CHECKOUT)
    text = (f"# Brief sd:{row['id']}: {row['title']}\n\n- filed: {row['created_at']}\n"
            f"- filed as: {row.get('external_id') or 'unknown'}\n\n## Brief\n\n{_text(row.get('body')).strip() or '(empty)'}\n")
    return {"checkout": checkout, "repo": checkout, "label": str(row["id"]), "item": row["id"], "input": text,
            "sets": {}}


def _registered(connection, path) -> Path | None:
    from sd_db import paths, repos

    row = repos.row_for(connection, path)
    return paths.expand(row["path"]) if row is not None else None


def _resolve_repo(connection, target) -> dict:
    from sd_db import workflow

    if not isinstance(target, str) or not target:
        raise ValueError("A repo review names a registered repository by its key or path.")
    checkout = _registered(connection, target)
    if checkout is None:
        raise workflow.WorkflowError(f"{target} is not a registered repository (see sd-db repo list)")
    if not checkout.is_dir():
        raise workflow.WorkflowError(f"{target} is not a directory on this machine")
    return {"checkout": checkout, "repo": checkout, "label": checkout.name, "item": None, "input": None,
            "sets": {"REPO_NAME": checkout.name}}


def _resolve_change(connection, target, base, item, body) -> dict:
    from sd_db import paths, workflow

    if not isinstance(target, str) or not target:
        raise ValueError("A change review names a checkout directory: a worktree or a clone.")
    directory = paths.disk(target)
    if not directory.is_absolute() or not directory.is_dir():
        raise workflow.WorkflowError(f"{target} is not a directory on this machine")
    checkout = _git(directory, "rev-parse", "--show-toplevel")
    common = _git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not checkout or not common:
        raise workflow.WorkflowError(f"{target} is not a git checkout, or git could not read it")
    main = Path(common).parent if Path(common).name == ".git" else None
    repo = _registered(connection, main) if main else None
    if repo is None:
        raise workflow.WorkflowError(f"{target} is not a checkout of a registered repository (see sd-db repo list)")
    if base is not None and (not isinstance(base, str) or not REF.fullmatch(base) or base.startswith("-")):
        raise ValueError("A base is a git ref: letters, digits and . _ / -.")
    head = _git(Path(checkout), "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    named = None
    for ref in ([base] if base else ["origin/HEAD", "origin/main", "main"]):
        if _git(Path(checkout), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            named = ref
            break
    if not head or named is None:
        raise workflow.WorkflowError(f"{target} has no {'base ' + base if base else 'origin/HEAD, origin/main or main'} "
                                     f"to compare with, or no commit")
    merge_base = _git(Path(checkout), "merge-base", named, head)
    if not merge_base:
        raise workflow.WorkflowError(f"git found no merge base of {named} and HEAD in {target}")
    if merge_base == head:
        raise workflow.WorkflowError(f"{target} has no commits on top of {named}; there is no change to review")
    owner = None
    if item is not None:
        owner = workflow.item_state(connection, _item_id(item, "item"))["item"]["id"]
    if body is None:
        text = "No pull-request body was given; review the diff and the commits alone."
    else:
        found = paths.disk(body) if isinstance(body, str) and body else None
        if found is None or not found.is_absolute() or not found.is_file():
            raise workflow.WorkflowError(f"the pull-request body {body!r} is not a file on this machine")
        text = found.read_bytes()[:BODY_MAX].decode("utf-8", "replace").strip() or "(empty)"
    branch = _git(Path(checkout), "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    lines = [f"# Change: {branch} in {checkout}", "", f"- checkout: {checkout}", f"- repository: {paths.key(str(repo))}",
             f"- branch: {branch}", f"- head: {head}", f"- base: {named}, merge base {merge_base}",
             f"- item: {'sd:' + str(owner) if owner else 'none'}", "", "## Pull-request body", "", text]
    return {"checkout": Path(checkout), "repo": repo, "label": Path(checkout).name, "item": owner,
            "input": "\n".join(lines) + "\n", "sets": {"BASE": merge_base, "HEAD": head}}


def _item_id(value, name: str) -> int:
    if type(value) is str and re.fullmatch(r"[1-9][0-9]{0,18}", value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise ValueError(f"The {name} is an item id.")
    return value


# -- the POST ----------------------------------------------------------------------------------------------------


def _held_by(handle) -> str:
    handle.seek(0)
    return handle.read().strip() or "unknown"


def _database(connection) -> str:
    """The file the server's connection reads: the runner opens it to file the outcome."""
    for row in connection.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return row[2]
    raise ValueError("The dashboard's database has no file the review's runner could open.")


def start(connection, kind: str, target, *, base=None, item=None, body=None, now: datetime.datetime | None = None):
    """Steps 1 to 4 above: `(202, run)`, or `(409, {"error"})` when a run holds the lock."""
    import sd_db

    now = now or datetime.datetime.now(datetime.timezone.utc)
    state = state_dir()
    runs = state / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    handle = open(state / "lock", "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 409, {"error": f"An adversarial review is already running (run {_held_by(handle)}); one runs at a "
                                  f"time. Wait for it to finish, then start this one."}
        if kind == "task":
            plan = _resolve_task(connection, target)
        elif kind == "brief":
            plan = _resolve_brief(connection, target)
        elif kind == "repo":
            plan = _resolve_repo(connection, target)
        else:
            plan = _resolve_change(connection, target, base, item, body)
        label = re.sub(r"[^A-Za-z0-9._-]", "-", plan["label"])[:60].strip("-.") or "x"
        run_id = f"{kind}-{label}-{_stamp(now)}"
        record = runs / f"{run_id}.json"
        if record.exists():
            return 409, {"error": f"Run {run_id} started this second; start it again in a moment."}
        result = runs / f"{run_id}.md"
        written = runs / f"{run_id}.input.md" if plan["input"] is not None else None
        sets = {**plan["sets"], **({"INPUT_PATH": str(written)} if written else {})}
        # The gate passes --set values through an `eval` of single-quoted words: a quote or a newline would break it.
        for key, value in [*sets.items(), ("checkout", str(plan["checkout"])), ("result", str(result))]:
            if "'" in value or "\n" in value:
                raise ValueError(f"The review's {key} holds a quote or a newline, which the gate cannot pass: {value!r}")
        database = _database(connection)
        if written:
            written.write_text(plan["input"], encoding="utf-8")
        model, effort = settings()
        document = {"id": run_id, "kind": kind, "target": str(target), "label": label, "state": "queued",
                    "reason": None, "repo": str(plan["repo"]), "checkout": str(plan["checkout"]),
                    "result": str(result), "input": str(written) if written else None, "item": plan["item"],
                    "sets": sets, "model": model, "effort": effort, "timeout": TIMEOUT, "started": _iso(now),
                    "finished": None, "filed": None, "brief": None, "brief_item": None, "batches": None, "pid": None,
                    "database": database, "library": str(Path(sd_db.__file__).resolve().parent.parent)}
        _write(record, document)
        handle.seek(0)
        handle.truncate()
        handle.write(run_id)
        handle.flush()
        environment = {**os.environ}
        # Jev ordering sends each finding's text to a third party; a review of private items keeps it off unless
        # the server was started with the switch set (local-adversarial-gate/README.md, "What leaves the machine").
        environment.setdefault("JEV_ADVERSARIAL_GATE", "0")
        try:
            runner = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", str(record),
                                       str(handle.fileno())],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      pass_fds=(handle.fileno(),), start_new_session=True, env=environment,
                                      cwd=str(state))
        except OSError as problem:
            _write(record, {**document, "state": "failed", "reason": f"the runner did not start: {problem}",
                            "finished": _iso(datetime.datetime.now(datetime.timezone.utc))})
            raise
        # Reaped here when it ends, so a long-lived server keeps no zombie; a restart hands it to launchd instead.
        threading.Thread(target=runner.wait, name=f"adversarial {run_id}", daemon=True).start()
        return 202, public(document)
    finally:
        handle.close()


def request(values: dict):
    """The POST's checks, before the server opens a connection; the start as `action_route` returns one."""
    if set(values) - {"kind", "target", "base", "item", "body"} or not {"kind", "target"} <= set(values):
        raise ValueError("An adversarial review takes a kind and a target, and for a change an optional base, item "
                         "and body.")
    kind = values["kind"]
    if kind not in KINDS:
        raise ValueError(f"An adversarial review's kind is one of {', '.join(KINDS)}.")
    if kind != "change" and set(values) - {"kind", "target"}:
        raise ValueError("Only a change review takes a base, an item or a body.")
    return lambda connection: start(connection, kind, values["target"], base=values.get("base"),
                                    item=values.get("item"), body=values.get("body"))


# -- the GET -----------------------------------------------------------------------------------------------------


def public(document: dict) -> dict:
    """What a page reads: the state file less the runner's own fields."""
    return {key: value for key, value in document.items()
            if key not in ("pid", "sets", "timeout", "database", "library")}


def _alive(pid) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _files(document: dict) -> bool:
    """Whether the run writes to the database: a done run files its brief; a failed one notes its item, if any."""
    return document["state"] == "done" or (document["state"] == "failed" and document.get("item") is not None)


def status(run_id: str, *, now: datetime.datetime | None = None):
    """`(status, document)` for one run; a run whose runner is gone without an outcome reads failed."""
    if not RUN_ID.fullmatch(run_id):
        return 404, {"error": "No such review run."}
    try:
        document = json.loads((state_dir() / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 404, {"error": "No such review run."}
    except (OSError, ValueError) as problem:
        return 503, {"error": f"The state of run {run_id} could not be read: {problem}"}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if document["state"] == "running" and not _alive(document.get("pid")):
        document.update(state="failed", reason=f"the runner (pid {document.get('pid')}) stopped without recording an "
                                               f"outcome; look for {document['result']}")
    elif document["state"] == "queued":
        started = datetime.datetime.fromisoformat(document["started"].replace("Z", "+00:00"))
        if (now - started).total_seconds() > STARTUP:
            document.update(state="failed", reason=f"the runner never started within {STARTUP} s")
    elif _files(document) and document.get("filed") is None and not _alive(document.get("pid")):
        # The outcome was recorded and the runner stopped before it said whether its one transaction committed.
        document["filed"] = (f"not known: the runner stopped before it recorded the filing; look for brief "
                             f"{_stem(document)} on the Briefs page")
    return 200, public(document)


# -- the runner: the outcome in the database ---------------------------------------------------------------------


def _stem(document: dict) -> str:
    """The brief's stem: `<day> - Adversarial review · <kind> <label> · <time>Z`; the Briefs page cuts it at the ` · `."""
    started = datetime.datetime.fromisoformat(document["started"].replace("Z", "+00:00"))
    return f"{started:%Y-%m-%d} - Adversarial review · {document['kind']} {document['label']} · {started:%H%M%S}Z"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _plain(text: str) -> str:
    return " ".join(text.replace("**", "").replace("`", "").split())


def recommendations(result: str, top: int = RECOMMENDATIONS) -> tuple[list[dict], str]:
    """The result's titled hypotheses as recommendations, found as `rank.py` finds them, and its verdict line.

    Each is `{heading, title, why, evidence, repo}`: the title is the `Recommendation:` line, else the heading; `why`
    is the rest of the hypothesis, with a line `sd_db.review` would read as a field quoted; `repo` is the `Repo:`
    line as written, or None. The verdict is the last line, which the last hypothesis holds when nothing follows it.
    """
    spec = importlib.util.spec_from_file_location("adversarial_gate_rank", RANK)
    rank = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rank)
    lines = result.splitlines()
    filled = [number for number, line in enumerate(lines) if line.strip()]
    last = filled[-1] if filled else None
    out = []
    for first, end in rank.finding_blocks(lines)[:top]:
        heading = _cut(_plain(re.sub(r"^#+\s*(?:[0-9]+[.)]\s+)?", "", lines[first])), TITLE_CHARS)
        found = {"heading": heading, "recommendation": None, "evidence": [], "repo": None}
        why = []
        for number in range(first + 1, end):
            if number == last:
                continue
            tagged = TAG.match(lines[number])
            if tagged is None:
                why.append(("> " if RESERVED.match(lines[number]) else "") + lines[number])
                continue
            tag, value = tagged[1].lower(), " ".join(tagged[2].replace("**", "").split())
            if not value:
                continue
            if tag == "evidence" and len(found["evidence"]) < EVIDENCE_LINES:
                found["evidence"].append(_cut(value, EVIDENCE_CHARS))
            elif tag in ("repo", "recommendation") and found[tag] is None:
                found[tag] = value
        text = "\n".join(why).strip().encode("utf-8")
        found["why"] = (text[:WHY_BYTES].decode("utf-8", "ignore").rstrip() + " …") if len(text) > WHY_BYTES \
            else text.decode("utf-8")
        found["title"] = _cut(_plain(found.pop("recommendation") or "") or heading, TITLE_CHARS)
        if RESERVED.match(found["title"]):
            found["title"] = "> " + found["title"]
        out.append(found)
    return out, lines[last].strip() if last is not None else ""


def _repo_of(connection, named: str | None, home: str) -> str:
    """The registered repository a `Repo:` line names, by key, path or last path part; else the review's own."""
    from sd_db import repos

    if not named:
        return home
    value = named.strip("`'\" ")
    row = repos.row_for(connection, value)
    if row is not None:
        return row["path"]
    found = [row["path"] for row in repos.registered(connection) if Path(row["path"]).name == value]
    return found[0] if len(found) == 1 else home


def _question(found: dict, subject: str, stem: str) -> dict:
    """One recommendation as a question: its first line is the title an approval files its task under."""
    text = "\n\n".join(part for part in (found["title"], found["why"], f"From the adversarial review in brief {stem}.")
                       if part)
    return {"subject": subject, "text": text, "evidence": found["evidence"], "options": ["Approve", "Reject"]}


def _brief_text(document: dict, verdict: str, found: list[dict], batches: list[tuple[int, str]]) -> str:
    target = f"{document['kind']} {document['target']}"
    named = ", ".join(f"sd:{item} ({repo})" for item, repo in batches) or "none"
    lines = ["---", "review: adversarial", f"run: {document['id']}", f"target: {target}",
             f"model: {document['model']}", f"effort: {document['effort']}",
             f"batches: {', '.join(f'sd:{item}' for item, _ in batches) or 'none'}", "---", "",
             f"# Adversarial review of {target}", "",
             f"- Target: {target}, read in {document['checkout']}",
             f"- Model: {document['model']} at {document['effort']} effort",
             f"- Review batches: {named}; approve a question there to file its task, reject it to drop it",
             f"- Result as codex wrote it: {document['result']}", "", "## Summary", "",
             verdict or "The result ends with no verdict line.", "",
             f"Recommendations: {len(found)}. These are hypotheses for a person to check, not findings.", "",
             "## Recommendations", ""]
    for number, one in enumerate(found, 1):
        lines += [f"### {number}. {one['title']}", ""]
        if one["title"] != one["heading"]:
            lines.append(f"- Hypothesis: {one['heading']}")
        lines.append(f"- Repo: {one['repo']}")
        lines.append(f"- Review batch: sd:{one['batch']}" if one.get("batch") else f"- Not asked: {one['dropped']}")
        lines += [*(f"- Evidence: {line}" for line in one["evidence"]), "", one["why"] or "(no reasoning given)", ""]
    return "\n".join(lines).rstrip() + "\n"


def _file_done(connection, document: dict) -> tuple[dict, str]:
    """A done run's batches, then its brief, which names them: the fields to record and the note's body."""
    from sd_db import repos, review, routine_briefs

    found, verdict = recommendations(Path(document["result"]).read_text(encoding="utf-8", errors="replace"))
    stem, kind, label = _stem(document), document["kind"], document["label"]
    home = repos.row_for(connection, document["repo"])
    if home is None:
        raise ValueError(f"{document['repo']} is no longer a registered repository")
    groups, subjects = {home["path"]: []}, set()
    for one in found:
        one["repo"] = _repo_of(connection, one["repo"], home["path"])
        subject = _cut(f"{kind} {label}: {one['heading']}", 190)
        while subject in subjects:  # Two hypotheses with one heading: a batch takes each subject once.
            subject += "'"
        subjects.add(subject)
        one["subject"] = subject
        groups.setdefault(one["repo"], []).append(one)
    started = datetime.datetime.fromisoformat(document["started"].replace("Z", "+00:00"))
    name = f"{kind}-{label[:30]}-{_stamp(started)}"
    batches = []
    for number, (repo, asked) in enumerate([group for group in groups.items() if group[1]], 1):
        filed = review.file(connection, {
            "schema": review.SCHEMA, "queue": QUEUE, "batch": name if number == 1 else f"{name}-{number}",
            "title": f"Adversarial review of {kind} {label}: pick the recommendations to file as tasks",
            "repo": repo, "source": f"local-adversarial-gate; brief {stem}", "mode": "open",
            "questions": [_question(one, one["subject"], stem) for one in asked]}, who=WHO)
        # A subject the queue rejected in the last 90 days is dropped, not asked again.
        dropped = {entry["subject"]: entry["reason"] for entry in filed["dropped"]}
        for one in asked:
            one["batch"] = None if one["subject"] in dropped else filed["item"]
            one["dropped"] = dropped.get(one["subject"])
        if filed["item"] is not None:
            batches.append((filed["item"], repo))
    brief = routine_briefs.add(connection, stem, _brief_text(document, verdict, found, batches),
                               repo=home["path"], who=WHO)
    titles = "; ".join(f"{number}. {one['heading']}" for number, one in enumerate(found[:TOP], 1))
    body = NOTE.format(model=document["model"], effort=document["effort"], stem=stem, brief=brief["item"],
                       count=len(found), batches=", ".join(f"sd:{item}" for item, _ in batches) or "none",
                       titles=titles or "none titled in the result; read the brief")
    return {"brief": stem, "brief_item": brief["item"], "batches": [item for item, _ in batches]}, body


def _file(document: dict) -> str | None:
    """Step 7: the outcome in the database, in one transaction, so all of it lands or none; None when it landed.

    Any failure is recorded as the reason nothing was written: an `sd_db` refusal, a locked database, a result that
    cannot be read. The runner has no one else to tell.
    """
    if document["library"] not in sys.path:
        sys.path.insert(0, document["library"])
    try:
        import sd_db
        from sd_db import workflow
        from sd_db.database import transaction

        connection = sd_db.connect(document["database"])
    except Exception as problem:  # noqa: BLE001 - see the docstring
        return f"the database did not open: {type(problem).__name__}: {problem}"
    try:
        with transaction(connection):
            fields, body = {}, None
            if document["state"] == "done":
                fields, body = _file_done(connection, document)
            else:
                body = FAILED.format(model=document["model"], effort=document["effort"], reason=document["reason"],
                                     input=document["input"] or document["checkout"])
            if document["item"] is not None:
                workflow.add_item_note(connection, document["item"], kind="comment", body=body, who=WHO)
    except Exception as problem:  # noqa: BLE001 - see the docstring
        return f"{type(problem).__name__}: {problem}"
    finally:
        connection.close()
    document.update(fields)
    return None


# -- the runner --------------------------------------------------------------------------------------------------


def _reason(log: str, code: int) -> str:
    if code == 124:
        return f"codex did not answer within {TIMEOUT} s; its process group was killed"
    said = [line.strip() for line in log.splitlines() if line.strip() and not line.startswith("# adversarial gate:")]
    return f"{said[-1]} (exit {code})" if said else f"the gate exited {code} with no message"


def run(record: Path, lock: int | None = None) -> int:
    """Steps 5 to 7: the gate, its outcome and the filing; the exit code is 0 for a done run and 1 for a failed one.

    `lock` is the lock's descriptor, handed on to the gate, so the lock outlives a runner that dies before codex.
    """
    document = json.loads(record.read_text(encoding="utf-8"))
    document.update(state="running", pid=os.getpid())
    _write(record, document)
    argv = ["sh", str(GATE), "run", "--lens", document["kind"]]
    for key, value in document["sets"].items():
        argv += ["--set", f"{key}={value}"]
    argv += ["--repo", document["checkout"], "--out", document["result"], "--model", document["model"],
             "--effort", document["effort"], "--timeout", str(document["timeout"])]
    log = record.with_suffix(".log")
    with open(log, "w", encoding="utf-8") as sink:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                                 start_new_session=True, pass_fds=(lock,) if lock is not None else ())
        try:
            code = child.wait(timeout=document["timeout"] + GRACE)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            code = None
    said = log.read_text(encoding="utf-8", errors="replace")
    result = Path(document["result"])
    if code is None:
        reason = f"the gate did not end within {document['timeout'] + GRACE} s; its process group was killed"
    elif code != 0:
        reason = _reason(said, code)
    elif not result.is_file() or not result.read_text(encoding="utf-8", errors="replace").strip():
        reason = "codex exited 0 but wrote no result"
    else:
        reason = None
    finished = _iso(datetime.datetime.now(datetime.timezone.utc))
    document.update(state="failed" if reason else "done", reason=reason, finished=finished)
    _write(record, document)
    if _files(document):
        problem = _file(document)
        document["filed"] = "written" if problem is None else f"not written: {problem}"
        _write(record, document)
    return 1 if reason else 0


if __name__ == "__main__":
    # `run <state file> <lock descriptor>`: the descriptor stays open, unread, until this process and the gate exit.
    if len(sys.argv) != 4 or sys.argv[1] != "run":
        sys.exit("usage: adversarial.py run <state file> <lock descriptor>")
    sys.exit(run(Path(sys.argv[2]), int(sys.argv[3])))
