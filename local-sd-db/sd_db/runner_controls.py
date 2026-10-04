"""Operator preparation and read-only readiness over the shared runner."""

import json
import math
import os
import plistlib
import re
import stat
import subprocess
from pathlib import Path

from . import paths, runner, workflow
from .database import transaction
from .operations import LABEL_PREFIX
from .writes import add_note, set_item_fields


def readiness(connection, item, *, state=None):
    """Whether `enqueue` takes the item now, and why not. `state` is the item's `workflow.item_state`, when the caller
    read it in the same snapshot (the Tasks rows, sd:2590)."""
    state = state or workflow.item_state(connection, item)
    row = state["item"]
    assignments = [runner.queue_state(connection, held["id"]) for held in connection.execute(
        "SELECT id FROM assignment WHERE item=? ORDER BY id DESC LIMIT 20", (item,))]
    reason = None
    try:
        runner._item(connection, item)
    except runner.RunnerRefused as error:
        reason = str(error)
    if row["parked_at"]:
        reason = "Revive this parked item before running it."
    if any(held["status"] in {"queued", "running", "ending"} for held in assignments):
        reason = "An assignment already owns this item."
    if any(held["run"] and held["run"].get("released_at") is None for held in assignments):
        reason = "A retained runner lease needs reconciliation before another run."
    repos = [dict(repo) for repo in connection.execute("SELECT path,remote FROM repo ORDER BY path")]
    return {"item": row, "revision": state["revision"], "allowed": reason is None,
            "reason": reason, "assignments": assignments, "repos": repos}


def configure_item(connection, item, *, repo, branch, expected_revision, who):
    with transaction(connection):
        state = workflow._checked_state(connection, item, expected_revision)
        row = state["item"]
        if row["kind"] not in {"task", "report", "work"} or row["status"] == "done":
            raise workflow.WorkflowError("this item uses its own assignment preparation")
        if connection.execute("SELECT 1 FROM assignment WHERE item=? AND status IN ('queued','running','ending')", (item,)).fetchone():
            raise workflow.WorkflowError("an active assignment owns this item's repository and branch")
        if connection.execute("SELECT 1 FROM runner_run JOIN assignment ON assignment.id=runner_run.assignment WHERE assignment.item=? AND runner_run.released_at IS NULL", (item,)).fetchone():
            raise workflow.WorkflowError("the retained runner lease needs reconciliation first")
        if row["kind"] == "work":
            owner = connection.execute("SELECT status_source FROM repo WHERE path=?", (row["repo"],)).fetchone()
            if not owner or owner["status_source"] != "row":
                raise workflow.WorkflowError("work metadata belongs to its file owner until database cutover completes")
        if row["kind"] == "work" and not paths.same(row["repo"], repo):
            raise workflow.WorkflowError("a work item's source repository cannot be changed here")
        if isinstance(repo, str):
            from .repos import row_for
            registered = row_for(connection, repo)
        else:
            registered = None
        if not registered or not registered["remote"]:
            raise workflow.WorkflowError("choose a registered repository with a remote")
        # The form the row holds, whichever form the caller passed (sd:1439).
        repo = registered["path"]
        if not isinstance(branch, str) or not branch or len(branch) > 250 or branch.startswith("-"):
            raise workflow.WorkflowError("provide an item branch name")
        def inspect(*args, required=True):
            try:
                result = subprocess.run(["git", "--no-optional-locks", "-C", str(paths.expand(repo)), *args],
                    capture_output=True, text=True, timeout=10, check=False,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            except (OSError, subprocess.SubprocessError) as error:
                raise workflow.WorkflowError("cannot verify repository branches; no setup was changed") from error
            if result.returncode and required:
                raise workflow.WorkflowError("cannot verify repository branches; no setup was changed")
            return result.stdout.strip() if result.returncode == 0 else ""
        inspect("check-ref-format", "--branch", branch)
        local = inspect("rev-parse", "--verify", f"refs/heads/{branch}", required=False)
        observed = inspect("ls-remote", "--symref", "--", registered["remote"], "HEAD", f"refs/heads/{branch}")
        default, seed, existing = None, None, None
        for line in observed.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] == "ref:" and parts[2] == "HEAD" and parts[1].startswith("refs/heads/"):
                default = parts[1][11:]
            if len(parts) == 2 and re.fullmatch(r"[a-f0-9]{40}", parts[0]):
                if parts[1] == "HEAD": seed = parts[0]
                if parts[1] == "refs/heads/" + branch: existing = parts[0]
        if not default or not seed:
            raise workflow.WorkflowError("the registered remote does not expose a verified default branch")
        if branch == default:
            raise workflow.WorkflowError("choose an item branch; the remote default branch is reserved for merged work")
        fields = json.loads(row["fields"] or "{}")
        if not isinstance(fields, dict):
            raise workflow.WorkflowError("item metadata must be an object before preparing a run")
        previous = fields.get("runner_branch")
        if not local and not existing:
            fields["runner_branch"] = {"source_commit": seed, "source_branch": default, "remote_absent": True}
        else:
            fields.pop("runner_branch", None)
        if row["repo"] != repo or row["branch"] != branch or fields.get("runner_branch") != previous:
            set_item_fields(connection, item, repo=repo, branch=branch, fields=fields,
                            source_commit=seed if fields.get("runner_branch") else row["source_commit"])
            detail = f"new isolated branch from {default} at {seed}" if fields.get("runner_branch") else "existing item branch"
            add_note(connection, item, "decision", f"Prepared for {repo} on {branch}: {detail} by {who}.", session=who)
        return workflow.item_state(connection, item)


def enqueue(connection, items, *, revisions, parallel=False, role="author", budget_minutes=90, budget_usd=None, who, skill=None, skill_revision=None):
    if not isinstance(items, list) or not 1 <= len(items) <= 50 or any(type(item) is not int or item < 1 for item in items):
        raise workflow.WorkflowError("select between one and fifty items")
    if type(parallel) is not bool or role not in {"author", "reviewer"}:
        raise workflow.WorkflowError("choose author or reviewer and a valid run mode")
    if type(budget_minutes) is not int or not 1 <= budget_minutes <= 1440:
        raise workflow.WorkflowError("choose a time limit from one to 1440 minutes")
    if budget_usd is not None:
        try:
            sound = type(budget_usd) in (int, float) and math.isfinite(budget_usd) and budget_usd >= 0
        except OverflowError:  # a JSON integer too large for a float
            sound = False
        if not sound:
            raise workflow.WorkflowError("choose a budget in US dollars of zero or more, or leave it empty")
    if not isinstance(revisions, dict):
        raise workflow.WorkflowError("the selection needs its current item revisions")
    with transaction(connection):
        scope = "item"
        if skill:
            from . import skills_catalog

            root, chosen, _ = skills_catalog._selected(connection, skill, skill_revision)
            if not skill_revision:
                raise workflow.WorkflowError("the selected skill needs its current revision")
            scope = "skill-use:" + json.dumps({"root": str(root), **{key: chosen[key] for key in ("name", "path", "source_sha256", "files")}}, sort_keys=True)
        for item in items:
            ready = readiness(connection, item)
            if not ready["allowed"]:
                raise workflow.WorkflowError(ready["reason"])
        return {"assignments": runner.enqueue(connection, items, parallel=parallel, role=role,
            budget_minutes=budget_minutes, budget_usd=budget_usd, expected_revisions=revisions, who=who, scope=scope)}


def _owned_file(path, *, executable=False, symlink=False):
    path = Path(path)
    if not path.is_absolute() or (path.is_symlink() and not symlink):
        raise workflow.WorkflowError("runner installation contains an unsafe file path")
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or details.st_uid not in {0, os.getuid()} or details.st_mode & 0o022:
        raise workflow.WorkflowError("runner installation file ownership or permissions are unsafe")
    if executable and not os.access(path, os.X_OK):
        raise workflow.WorkflowError("runner installation entrypoint is not executable")
    return path.resolve()


def service_installation(*, database, home=None):
    """Discover only the installed service's finite, vetted launchd invocation."""
    home = Path(home or Path.home()).resolve()
    plist_path = home / f"Library/LaunchAgents/{LABEL_PREFIX}.sd-runner.plist"
    if not plist_path.exists():
        raise workflow.WorkflowError("Runner controls are unavailable until the runner service is installed.")
    _owned_file(plist_path)
    if plist_path.stat().st_size > 65536:
        raise workflow.WorkflowError("runner installation record exceeds its limit")
    try:
        record = plistlib.loads(plist_path.read_bytes())
    except (ValueError, plistlib.InvalidFileException) as error:
        raise workflow.WorkflowError("runner installation record is unreadable") from error
    args = record.get("ProgramArguments") if isinstance(record, dict) else None
    if not isinstance(record, dict) or record.get("Label") != LABEL_PREFIX + ".sd-runner" or not isinstance(args, list) or len(args) not in {2, 4} or any(not isinstance(part, str) for part in args) or args[1] != "serve" or (len(args) == 4 and args[2] != "--config"):
        raise workflow.WorkflowError("runner installation has unsupported launch arguments")
    launcher = _owned_file(args[0], executable=True)
    if launcher.name != "runner.sh" or launcher.parent.name != "local-sd-runner":
        raise workflow.WorkflowError("runner installation does not name its supported service entrypoint")
    _owned_file(launcher.parent / "sd_runner/bootstrap.py")
    environment = record.get("EnvironmentVariables")
    if not isinstance(environment, dict) or not isinstance(environment.get("SD_RUNNER_PYTHON"), str):
        raise workflow.WorkflowError("runner installation has no provisioned interpreter")
    _owned_file(environment["SD_RUNNER_PYTHON"], executable=True, symlink=True)
    interpreter = Path(environment["SD_RUNNER_PYTHON"])
    config = Path(args[3]) if len(args) == 4 else home / ".config/sd/runner.json"
    raw = {}
    if config.exists():
        _owned_file(config)
        if config.stat().st_size > 65536:
            raise workflow.WorkflowError("runner configuration exceeds its limit")
        try:
            raw = json.loads(config.read_text())
        except ValueError as error:
            raise workflow.WorkflowError("runner configuration is unreadable") from error
    elif len(args) == 4:
        raise workflow.WorkflowError("installed runner configuration is missing")
    if not isinstance(raw, dict):
        raise workflow.WorkflowError("runner configuration must be an object")
    from .database import default_path
    named_database = raw.get("database", str(default_path(home)))
    if not isinstance(named_database, str):
        raise workflow.WorkflowError("runner database path must be an absolute string")
    configured = Path(named_database)
    if not configured.is_absolute() or configured.resolve() != Path(database).resolve():
        raise workflow.WorkflowError("the installed runner uses a different workflow database")
    return {"launcher": str(launcher), "interpreter": str(interpreter),
            "config": str(config) if config.exists() else None, "home": str(home),
            "database": str(configured.resolve())}


def invoke_service(installation, verb, assignment, *, revision, run, who, destination=None, historical_run=None):
    args = [installation["launcher"], verb, str(assignment), "--if-revision", revision,
            "--database", installation["database"], "--expected-run", run]
    # The service records the operator for the two verbs that write one. Without
    # `--who` it would record its own login account, which is the same for
    # the dashboard, the palette and a person at the terminal.
    if verb in {"cancel", "resume"}:
        args += ["--who", who]
    if installation["config"]:
        args += ["--config", installation["config"]]
    if destination is not None:
        args += ["--destination", str(destination)]
    if historical_run is not None:
        if type(historical_run) is not int or historical_run < 1 or verb not in {"restore", "restore-status"}:
            raise workflow.WorkflowError("historical attempt must be a positive restore run number")
        args += ["--run", str(historical_run)]
    environment = {"HOME": installation["home"], "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                   "SD_RUNNER_PYTHON": installation["interpreter"]}
    try:
        result = subprocess.run(args, env=environment, capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as error:
        raise workflow.WorkflowError("runner control response timed out; inspect the assignment before trying again") from error
    if result.returncode:
        raise workflow.WorkflowError((result.stderr or result.stdout or "runner control was refused")[:2000])
    if len(result.stdout.encode()) > 65536:
        raise workflow.WorkflowError("runner control response exceeded its limit; inspect the assignment")
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise workflow.WorkflowError("runner control response was incomplete; inspect the assignment") from error
    if not isinstance(response, dict):
        raise workflow.WorkflowError("runner control returned an invalid response")
    return response


def control(connection, assignment, verb, *, expected_revision, destination=None, who, backend=None, home=None):
    if verb not in {"cancel", "resume", "restore"}:
        raise workflow.WorkflowError("unknown runner control")
    current = runner.queue_state(connection, assignment)
    if current["revision"] != expected_revision:
        raise workflow.StaleItem("assignment changed; reload before controlling it")
    held = current["run"]
    # Queued work, and a running row no attempt owns (sd:991), have no
    # process for the service to stop: the library's cancel ends the row.
    if verb == "cancel" and (current["status"] == "queued" or (current["status"] == "running" and (not held or held["released_at"]))):
        return runner.request_cancel(connection, assignment, expected_revision=expected_revision, who=who)
    if not held:
        raise workflow.WorkflowError("this assignment has no owned runner attempt to control")
    if verb == "cancel" and current["status"] != "running":
        raise workflow.WorkflowError("only a running attempt can be stopped")
    if verb == "resume" and (current["status"] != "ending" or held.get("end_step") != "kept"):
        raise workflow.WorkflowError("only a kept checkout can be resumed")
    if verb == "restore":
        if not held.get("released_at") or not isinstance(destination, str) or not Path(destination).is_absolute():
            raise workflow.WorkflowError("restore needs retained output and a new absolute destination path")
        if Path(destination).exists() or Path(destination).is_symlink():
            raise workflow.WorkflowError("restore destination already exists")
    database = connection.execute("PRAGMA database_list").fetchone()[2]
    try:
        installation = service_installation(database=database, home=home)
    except OSError as error:
        raise workflow.WorkflowError("runner installation files are unavailable") from error
    return (backend or invoke_service)(installation, verb, assignment, revision=expected_revision,
                                      run=held["id"], who=who, destination=destination)
