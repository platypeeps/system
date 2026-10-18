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

`start` runs in the POST, in five steps, and answers 202 at once:

1. The lock, `<state>/lock`, taken without waiting: a second run while one holds it is a 409 naming the run.
2. The target, resolved to a checkout, a result folder in its main repository and the lens's inputs.
3. `<repo>/docs/dashboard/adversarial/`, with a `docs/dashboard/` line added to `.gitignore` when git does not
   already ignore that folder; the item's or the change's text as `<id>.input.md` there.
4. The run's state file, `<state>/runs/<id>.json`, as `queued`.
5. This file as a script, in a session of its own, holding the lock's descriptor: the lock is released only when
   the runner and every process under it have exited, so nothing runs codex beside it.

The runner (`run`) marks the run `running`, runs `adversarial-gate.sh run`, bounded by the gate at `TIMEOUT` (exit
124) and here at `TIMEOUT + GRACE`, and marks it `done` or `failed` with the reason. When the target is an item (a
task, a brief, or the change's `item`) it then adds a `comment` note through `sd task note`: the result path and the
top hypotheses' titles, or the failure. Nothing here approves, merges, publishes or posts anywhere else.

`status` answers `GET /api/adversarial/<id>` from the state file. A run whose runner is gone without an outcome reads
`failed`, with the reason, and a run queued for longer than `STARTUP` that never started reads `failed` too. A run
whose runner is gone after its outcome but before its note reads the note as not known.

The runner is this file run by path: everything it needs is the standard library and `rank.py` by path, so the
`sd_db` imports stay inside the functions the server calls.
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

__all__ = ["EFFORT", "GATE", "KINDS", "MODEL", "NOTE", "TIMEOUT", "request", "run", "start", "status"]

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
NOTE_SECONDS = 30
#: Hypotheses whose titles the note names.
TOP = 3
#: The longest pull-request body read into the input file, in bytes.
BODY_MAX = 200_000
RESULTS = Path("docs") / "dashboard" / "adversarial"
RUN_ID = re.compile(r"(task|brief|repo|change)-[A-Za-z0-9._-]{1,60}-[0-9]{8}T[0-9]{6}Z")
REF = re.compile(r"[A-Za-z0-9._/-]{1,200}")

NOTE = "Adversarial review ({model}, {effort} effort): {result}. Top hypotheses: {titles}. These are hypotheses for a person to check, not findings."
FAILED = "Adversarial review failed ({model}, {effort} effort): {reason}. Input: {input}."


class Busy(Exception):
    """Another run holds the lock."""


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


# -- step 3: the result folder -----------------------------------------------------------------------------------


def results_folder(repo: Path) -> Path:
    """`<repo>/docs/dashboard/adversarial/`, made; a `.gitignore` line when git does not ignore `docs/dashboard/`."""
    folder = repo / RESULTS
    folder.mkdir(parents=True, exist_ok=True)
    probe = (RESULTS / "probe.md").as_posix()
    try:
        ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", probe], capture_output=True,
                                 stdin=subprocess.DEVNULL, timeout=GIT_SECONDS, check=False).returncode
    except (OSError, subprocess.TimeoutExpired):
        ignored = None
    # 1 is git's "not ignored"; 0 is ignored, and anything else (no repository, git failed) changes nothing.
    if ignored == 1:
        ignore = repo / ".gitignore"
        before = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
        ignore.write_text(before + ("" if not before or before.endswith("\n") else "\n")
                          + "# Generated pages and reports the dashboard reads from disk.\ndocs/dashboard/\n",
                          encoding="utf-8")
    return folder


# -- the POST ----------------------------------------------------------------------------------------------------


def _held_by(handle) -> str:
    handle.seek(0)
    return handle.read().strip() or "unknown"


def start(connection, kind: str, target, *, base=None, item=None, body=None, now: datetime.datetime | None = None):
    """Steps 1 to 5 above: `(202, run)`, or `(409, {"error"})` when a run holds the lock."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    state = state_dir()
    (state / "runs").mkdir(parents=True, exist_ok=True)
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
        record = state / "runs" / f"{run_id}.json"
        if record.exists():
            return 409, {"error": f"Run {run_id} started this second; start it again in a moment."}
        folder = plan["repo"] / RESULTS
        result = folder / f"{run_id}.md"
        written = folder / f"{run_id}.input.md" if plan["input"] is not None else None
        sets = {**plan["sets"], **({"INPUT_PATH": str(written)} if written else {})}
        # The gate passes --set values through an `eval` of single-quoted words: a quote or a newline would break it.
        for key, value in [*sets.items(), ("checkout", str(plan["checkout"])), ("result", str(result))]:
            if "'" in value or "\n" in value:
                raise ValueError(f"The review's {key} holds a quote or a newline, which the gate cannot pass: {value!r}")
        results_folder(plan["repo"])
        if written:
            written.write_text(plan["input"], encoding="utf-8")
        model, effort = settings()
        document = {"id": run_id, "kind": kind, "target": str(target), "state": "queued", "reason": None,
                    "repo": str(plan["repo"]), "checkout": str(plan["checkout"]), "result": str(result),
                    "input": str(written) if written else None, "item": plan["item"], "sets": sets,
                    "model": model, "effort": effort, "timeout": TIMEOUT, "started": _iso(now), "finished": None,
                    "note": None, "pid": None}
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
    return {key: value for key, value in document.items() if key not in ("pid", "sets", "timeout")}


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
    elif document["state"] in ("done", "failed") and document.get("item") is not None and document.get("note") is None \
            and not _alive(document.get("pid")):
        # The outcome was recorded and the runner stopped before it said whether the note landed.
        document["note"] = "not known: the runner stopped before it recorded the note"
    return 200, public(document)


# -- the runner --------------------------------------------------------------------------------------------------


def titles(result: Path, top: int = TOP) -> list[str]:
    """The headings of the first `top` hypotheses, found as `rank.py` finds them; [] when none is titled."""
    try:
        spec = importlib.util.spec_from_file_location("adversarial_gate_rank", RANK)
        rank = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rank)
        lines = result.read_text(encoding="utf-8", errors="replace").splitlines()
        found = rank.finding_blocks(lines)
    except (OSError, ImportError, AttributeError, SyntaxError):
        return []
    out = []
    for start_line, _ in found[:top]:
        title = re.sub(r"^#+\s*(?:[0-9]+[.)]\s+)?", "", lines[start_line]).replace("**", "").replace("`", "").strip()
        out.append(title if len(title) <= 100 else title[:99] + "…")
    return out


def _reason(log: str, code: int) -> str:
    if code == 124:
        return f"codex did not answer within {TIMEOUT} s; its process group was killed"
    said = [line.strip() for line in log.splitlines() if line.strip() and not line.startswith("# adversarial gate:")]
    return f"{said[-1]} (exit {code})" if said else f"the gate exited {code} with no message"


def _note(document: dict) -> str | None:
    """Step 8: the comment note, as `None` when written, else why it was not."""
    if document["state"] == "done":
        named = titles(Path(document["result"]))
        listed = "; ".join(f"{n}. {title}" for n, title in enumerate(named, 1)) or "none titled in the result; read the file"
        body = NOTE.format(model=document["model"], effort=document["effort"], result=document["result"], titles=listed)
    else:
        body = FAILED.format(model=document["model"], effort=document["effort"], reason=document["reason"],
                             input=document["input"] or document["checkout"])
    try:
        done = subprocess.run(["sd", "task", "note", str(document["item"]), body, "--kind", "comment"],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=NOTE_SECONDS,
                              check=False)
    except FileNotFoundError:
        return "sd is not on the dashboard's PATH"
    except subprocess.TimeoutExpired:
        return f"sd task note gave no answer in {NOTE_SECONDS} s, so whether the note landed is not known"
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}").splitlines()[-1]
        return f"sd task note failed: {said}"
    return None


def run(record: Path, lock: int | None = None) -> int:
    """Steps 6 to 8 above, in the runner; the exit code is 0 for a done run and 1 for a failed one.

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
    if document["item"] is not None:
        problem = _note(document)
        document["note"] = "written" if problem is None else f"not written: {problem}"
        _write(record, document)
    return 1 if reason else 0


if __name__ == "__main__":
    # `run <state file> <lock descriptor>`: the descriptor stays open, unread, until this process and the gate exit.
    if len(sys.argv) != 4 or sys.argv[1] != "run":
        sys.exit("usage: adversarial.py run <state file> <lock descriptor>")
    sys.exit(run(Path(sys.argv[2]), int(sys.argv[3])))
