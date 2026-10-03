"""A supervised assignment lifecycle; SQL stays in the shared domain library."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import selectors
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from xml.parsers.expat import ExpatError

paths = None  # bound at the end of this module (sd:1439)
from sd_db import registry as registry_lib
from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_db.database import connect, transaction
from sd_db.errors import SdDbError

from . import cargo_seed, gitops, hard_stops, processes, provider_protocol, session_record, storage, toolchain

SQL_RETRY_SECONDS = 10.0
OBSERVATION_FAILURES = (OSError, subprocess.SubprocessError, store.RunnerRefused, ExpatError)


def database_busy(error):
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    # Python versions without sqlite_errorcode still fail closed on other errors.
    return isinstance(error, sqlite3.OperationalError) and str(error) in {
        "database is locked", "database table is locked", "database schema is locked"}


def database_write(connection, operation, *args, **kwargs):
    """Retry one atomic SQL operation, never its surrounding external action."""
    if connection.in_transaction:
        return operation(connection, *args, **kwargs)
    timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    deadline = time.monotonic() + SQL_RETRY_SECONDS
    try:
        while True:
            remaining = max(0, deadline - time.monotonic())
            # Bound each SQLite wait as well as the delay between attempts.
            wait_ms = min(timeout, 250, max(1, int(remaining * 1000)))
            connection.execute(f"PRAGMA busy_timeout={wait_ms}")
            try:
                return operation(connection, *args, **kwargs)
            except sqlite3.Error as error:
                remaining = deadline - time.monotonic()
                if not database_busy(error) or connection.in_transaction or remaining <= 0:
                    raise
                time.sleep(min(0.1, remaining))
    finally:
        connection.execute(f"PRAGMA busy_timeout={timeout}")


def health_reasons(body: dict) -> list[str]:
    """Why a heartbeat body is unhealthy, one phrase per reason, for the err log's flip line (sd:1953)."""
    report = body.get("storage") or {}
    reasons = [str(problem) for problem in report.get("problems") or ()]
    if report.get("database_below_floor"):
        reasons.append("database volume below the free floor")
    for key in ("probe_holds", "restore_holds", "runtime_holds"):
        reasons += [str(hold.get("reason", hold)) if isinstance(hold, dict) else str(hold) for hold in body.get(key) or ()]
    if body.get("restoration_pending"):
        reasons.append("database restore pending")
    if body.get("database_holds"):
        reasons.append("ending held for " + ", ".join(map(str, body["database_holds"])))
    if (body.get("starting") or {}).get("reason"):
        reasons.append(body["starting"]["reason"])
    return reasons or ["no reason recorded"]


@dataclass(frozen=True)
class Config:
    database: Path
    work: Path
    retention: Path
    pack: Path
    home: Path
    interval: float = 10
    floor_gb: float = 40
    #: Directories searched before the login shell's `PATH` (sd:1762).
    path: tuple[str, ...] = ()


def registry_module(pack: Path):
    path = str(pack / "bin")
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module("sd_registry")


def provider_command(config: Config, request: dict, parent: dict) -> tuple[list[str], dict, dict]:
    registry_api = registry_module(config.pack)
    with closing(connect(config.database, write=False)) as connection:
        registry, error = registry_api.read_or_report(home=config.home, connection=connection)
        # The library's own view of the same file and rows: `calls.call`
        # takes the library's entry and reads the bill's cap from its registry.
        called = None if error else registry_lib.read(home=config.home, connection=connection)
    if error:
        raise store.RunnerRefused(error)
    role = "reviewer" if request["role"] == "reviewer" else "author"
    fields = json.loads(request["item_record"]["fields"] or "{}")
    authorship = fields.get("skill_review", {})
    excluded = (authorship.get("latest_author_vendors") or [authorship.get("latest_author_vendor")]) if role == "reviewer" else []
    excluded = {str(vendor).lower() for vendor in excluded if vendor}
    if role == "reviewer" and not excluded:
        raise store.RunnerRefused("reviewer dispatch requires the bound latest author vendor")
    problems = []
    for provider in registry.order(role):
        if provider.vendor.lower() in excluded:
            problems.append(f"{provider.name}: excluded author vendor {excluded}")
            continue
        if not provider.start:
            # sd:234 slice 8d: a `url` author is dispatched, not skipped. There
            # is no process, so no argv and no environment; the library makes
            # the one call in `Runner.answer`. A reviewer's answer must be the
            # structured review envelope a headless session is asked for, which
            # a chat completion is not, so a `url` reviewer is still passed over.
            if role == "reviewer":
                problems.append(f"{provider.name}: URL transport cannot run a structured skill review")
                continue
            return None, {}, {"provider": provider.name, "vendor": provider.vendor, "start": False, "bill": provider.bill,
                              "reader": provider.reader, "entry": called.providers[provider.name], "registry": called,
                              "reviewers": registry.order("reviewer")}
        argv = shlex.split(provider.start)
        executable = shutil.which(argv[0], path=parent.get("PATH"))
        if not executable:
            problems.append(f"{provider.name}: executable unavailable")
            continue
        argv[0] = executable
        # The registry declares command and credentials. No ambient key is passed.
        environment = registry_api.provider_environment(provider, parent)
        environment["HOME"] = str(config.home)
        environment["TMPDIR"] = str(Path(request["run"]["work_path"]) / ".git/sd-tmp")
        environment["XDG_CACHE_HOME"] = str(Path(request["run"]["work_path"]) / ".git/sd-cache")
        environment["SD_ASSIGNMENT"] = request["run"]["id"]
        try:
            argv = provider_protocol.argv(argv, reviewer=role == "reviewer", clone=Path(request["run"]["work_path"]))
        except store.RunnerRefused as error:
            problems.append(f"{provider.name}: {error}")
            continue
        # `start`, `bill` and `reader` are the cost row's: a `start` entry's
        # session reports its own total at exit and the runner writes it.
        # `reviewers` is who `sd-ship` may review this work with (sd:1762).
        return argv, environment, {"provider": provider.name, "vendor": provider.vendor, "start": True,
                                   "bill": provider.bill, "reader": provider.reader, "reviewers": registry.order("reviewer")}
    raise store.RunnerRefused("no usable session provider: " + "; ".join(problems))


def check_command(config: Config) -> list[str] | None:
    """The repository's own check, through the pack's `sd-check`; None without one.

    Without `sd-check` there is also no `sd-ship`, and `ship` refuses before
    anything is delivered, so nothing passes a failing test unnoticed: the
    refusal, not a silent pass, is what an absent pack produces.
    """
    executable = config.pack / "bin/sd-check"
    return [sys.executable, str(executable), "--json"] if executable.is_file() else None


def assignment_base(connection, assignment: int) -> str | None:
    """The base head the assignment's first attempt recorded, or None (sd:1802)."""
    row = connection.execute("SELECT base_head FROM runner_run WHERE assignment=? AND base_head IS NOT NULL "
                             "AND base_head<>'' ORDER BY run LIMIT 1", (assignment,)).fetchone()
    return row[0] if row else None


def prompt(request: dict, provider: dict) -> str:
    item = request["item_record"]
    if request["role"] == "reviewer":
        source = json.loads(item["fields"] or "{}")["skill_review"]
        return (f"Review the bound skill in this clone. Read repository instructions and the skill sources. "
                "You have read-only tools. Do not modify any source or contact external systems. "
                "Return your review as the final structured JSON response; the runner writes its artifact at .git/sd-skill-review.json. "
                f"Use version=1, item={item['id']}, source_sha256={source['source_sha256']}. "
                "proposals is an array of at most40 objects, each with path (repository-relative), line_start, line_end, body; "
                f"only these bound files may be cited: {json.dumps(source['files'])}. "
                f"Review brief: {item['body']}\n")
    return (f"Work only in this isolated clone on branch {request['run']['branch']}. "
            "Read repository instructions first. Do not merge, delete branches, or change the operator checkout. "
            "Complete the requested scope with relevant tests. Commit only enumerated intended files. "
            f"Every authored commit needs trailer Authored-with: {provider['provider']}/{provider['vendor']} "
            f"and Needed-by: {item['id']}. Stop at reviewed PR-ready work; the delivery lane owns merge.\n"
            f"Record every followup, decision, proposal and question as you produce it: append one JSON line "
            f'{{"kind": "followup", "body": "..."}} to {session_record.NOTES_FILE} in this clone; the runner files each on the item. '
            f"If you must stop -- a failing test you cannot make pass, a blocking review finding, a write outside this repository -- "
            f'write {hard_stops.SIGNAL_FILE} as {{"kind": "<one of: {"; ".join(store.HARD_STOPS)}>", "detail": "..."}} and exit; '
            "the runner marks the item blocked with that reason.\n"
            f"Assignment {request['id']}; role {request['role']}; scope {request.get('skill_context', request['scope'])}.\n"
            + (f"Use the selected skill instructions at {request['skill_context']}.\n" if request.get("skill_context") else "")
            +
            f"Title: {item['title']}\nBody: {item['body'] or ''}\nFields: {item['fields'] or '{}'}\n")


def verify_skill_source(request: dict) -> None:
    source = json.loads(request["item_record"]["fields"] or "{}").get("skill_review")
    if not source:
        return
    root = Path(request["run"]["work_path"]).resolve()
    from sd_db.skills_catalog import MAX_TEXT, _hash, _snapshot

    def bound_file(relative: Path) -> Path:
        path = root / relative
        if (relative.is_absolute() or ".." in relative.parts
                or not path.resolve().is_relative_to(root)
                or any(root.joinpath(*relative.parts[:index]).is_symlink()
                       for index in range(1, len(relative.parts) + 1))
                or not path.is_file() or path.stat().st_size > MAX_TEXT):
            raise store.RunnerRefused("skill source contains an invalid, linked or oversized bound file")
        return path

    named = source.get("path")
    if not isinstance(named, str) or Path(named).name != "SKILL.md":
        raise store.RunnerRefused("skill source must name its bound SKILL.md file")
    selected = bound_file(Path(named))
    # Catalog paths name SKILL.md; the binding includes its entire directory.
    observed = _snapshot(root, selected.parent)
    if source.get("action") in {"promote", "demote"}:
        manifest = bound_file(Path("skills/paths.json"))
        observed["skills/paths.json"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if observed != source["files"] or _hash(observed) != source["source_sha256"]:
        raise store.RunnerRefused("skill source changed since the reviewed selection; refresh before dispatch")


#: The checkout this runner runs from: its modules, the program launchd starts.
CHECKOUT = Path(__file__).resolve().parents[2]


def checkout_commit(root: Path = CHECKOUT) -> str | None:
    """The checkout's HEAD, or None when `root` is not a checkout (sd:1952).

    Read from its files, as `status` reads it: `serve` runs this before its
    first pulse, and a `git` child polls the global `time.sleep` while it
    waits, which a slow start under load reaches (sd:2254).
    """
    return store.checkout_head(root)


#: Seconds a cold start waits for `diskutil` to answer before it refuses (sd:1950).
COLD_START_WINDOW = 600


def drain_path(database: Path) -> Path:
    """The marker `runner.sh restart` writes to stop claims before it kicks the agent (sd:1951)."""
    return database.parent / "runner-drain.json"


def restart_lock_path(database: Path) -> Path:
    """The lock one `runner.sh restart` holds from before its marker to after its removal."""
    return database.parent / "runner-restart.lock"


def drain_request(database: Path) -> str | None:
    """The token of a drain marker whose restart still runs, or None.

    The drain is latched to the restart lock, not to a clock: it holds for
    as long as the verb that wrote it lives, and the kernel drops the lock
    when that verb dies. A marker that does not read, or that no restart
    holds the lock for, is ignored, so a dead restart never stops the queue.
    A lock that cannot be taken for any other reason is one the verb cannot
    take either, so no kick can follow and the marker is ignored too.
    """
    try:
        marker = json.loads(drain_path(database).read_text())
        token = marker["token"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not isinstance(token, str) or not token:
        return None
    try:
        with journal.lock(restart_lock_path(database), blocking=False, noun="restart"):
            return None
    except store.RunnerRefused as refusal:
        return token if isinstance(refusal.__cause__, BlockingIOError) else None


class Runner:
    def __init__(self, config: Config, *, freezer=storage.freeze, observer=processes.survivors, transport=None):
        self.config = config
        self.owner = uuid.uuid4().hex
        self.freezer = freezer
        self.observer = observer
        #: The wire for a `url` author's call; None is `calls.call`'s urllib.
        self.transport = transport
        self.threads = {}
        self.supervisors = {}
        self.last_delivery_watch = 0
        self.watcher = None
        self.delivery_watch_result = {}
        self.archive_watch = None
        self.last_archive_watch = 0
        self.archive_watch_result = {}
        self.pending_endings = {}
        #: The login shell's `PATH` entries, which `resolve_tools` reads once at startup.
        self.login_path = []
        #: A fixed search path, for a test; None searches `path`, login, then the inherited `PATH`.
        self.fixed_path = None
        #: Each registry `start` provider's program, absolute, or None; set by `resolve_tools`.
        self.executables = {}
        #: `storage.preflight`'s remembered `diskutil` answers by mount identity (sd:1941).
        self.storage_verified = {}
        #: The checkout's HEAD when `serve` started, the code this process runs (sd:1952).
        self.runner_commit = None
        #: The last heartbeat body this process wrote, and the health the err log last named (sd:1953).
        self.last_beat = None
        self.logged_healthy = True

    @property
    def search_path(self) -> str:
        """Where every tool the runner starts resolves: configured, login, then inherited (sd:1762).

        The inherited `PATH` is read at each call, as it was before this
        property: a caller that changes the runner's environment is heard.
        """
        if self.fixed_path is not None:
            return self.fixed_path
        return toolchain.search_path(self.config.path, self.login_path, os.environ.get("PATH", "").split(os.pathsep))

    @search_path.setter
    def search_path(self, value: str) -> None:
        self.fixed_path = value

    def resolve_tools(self, *, probe=toolchain.login_path) -> None:
        """Read the login shell's `PATH` and resolve the providers' programs once, at startup (sd:1762).

        launchd hands the runner a short fixed `PATH`; the operator's
        programs live where the login shell puts them. The registry file is
        read without the database: this only names programs, and a registry
        that does not read is `provider_command`'s refusal at dispatch.
        """
        self.login_path = probe()
        registry, error = None, "registry not read"
        try:
            registry, error = registry_module(self.config.pack).read_or_report(home=self.config.home)
        except (ImportError, OSError) as failure:
            error = str(failure)
        self.executables = {} if error else toolchain.resolve(registry.providers.values(), self.search_path)

    def persist(self, connection, ident, **fields):
        row = database_write(connection, store.update_run, ident, **fields) if fields else store.run_state(connection, ident)
        journal.persist(self.config.database, row)
        return row

    def heartbeat(self, connection, body: dict) -> dict:
        """Write the heartbeat and keep its body for `log_health` (sd:1953)."""
        written = database_write(connection, store.heartbeat, body)
        self.last_beat = body
        return written

    def log_health(self) -> None:
        """Write one err log line when the last heartbeat's health differs from the last line's (sd:1953).

        A tick can write a healthy pulse and then an unhealthy hold, so the
        caller asks once the tick is done, and the line names its final state.
        A daemon starts as healthy, so an unhealthy first beat is a flip.
        Reasons that change while health stays the same write nothing.
        """
        if self.last_beat is None:
            return
        healthy = bool(self.last_beat.get("healthy"))
        if healthy == self.logged_healthy:
            return
        self.logged_healthy = healthy
        message = "healthy again" if healthy else "unhealthy: " + "; ".join(health_reasons(self.last_beat))
        print(f"runner: {' '.join(message.split())}", file=sys.stderr, flush=True)

    @staticmethod
    def stop_owned(run):
        try:
            processes.terminate_owned(run)
        except OBSERVATION_FAILURES as error:
            return {"run": run["id"], "probe": "owned_stop", "reason": str(error)}
        return None

    def restore_holds(self, connection) -> list[dict]:
        result = []
        if store.restoration_pending(connection):
            result.append({"reason": "database restore reimport is incomplete"})
        for marker in ("runner-restore-intent.json", "publication-restore-intent.json"):
            if (self.config.database.parent / marker).exists():
                result.append({"reason": f"incomplete restore marker {marker}"})
        records = journal.records(self.config.database)
        by_id = {record["id"]: record for record in records}
        for active in store.active_runs(connection):
            if active["id"] not in by_id:
                result.append({"run": active["id"], "reason": "active database run has no durable ownership journal"})
        for external in records:
            try:
                current = store.run_state(connection, external["id"])
            except store.RunnerRefused:
                if not external.get("released_at"):
                    # Killing strangers is never a remedy for a restored snapshot.
                    if failed := self.stop_owned(external):
                        result.append(failed)
                    result.append({"run": external["id"], "reason": "run absent from restored database", "journal": external})
                continue
            if external["journal_version"] > current["journal_version"]:
                if failed := self.stop_owned(external):
                    result.append(failed)
                result.append({"run": external["id"], "reason": "durable run journal is newer than restored database", "journal": external})
            elif external["journal_version"] == current["journal_version"] and journal_differs(external, current):
                result.append({"run": external["id"], "reason": "same-version database and ownership journal conflict"})
        return result

    def recover(self, connection) -> list[dict]:
        holds = self.restore_holds(connection)
        if holds:
            return holds
        for run in store.active_runs(connection):
            if failed := self.stop_owned(run):
                return [failed]
            completed = self.execution_result(connection, run)
            # A dead leader can leave a group or escaped child. End cleanup will
            # quarantine them; no terminal row is written until the lease is safe.
            if not run["outcome"]:
                outcome = "done" if completed and completed["exit_code"] == 0 else "blocked"
                detail = (f"registered command exit {completed['exit_code']} recovered from durable receipt" if completed
                          else "runner restarted; operator requeue required")
                database_write(connection, store.begin_ending, run["id"], outcome=outcome, detail=detail)
            self.finish(connection, run["id"])
        return []

    @staticmethod
    def execution_result(connection, run):
        """Recover completed output after a joined supervisor or a stopped attempt."""
        if store.queue_state(connection, run["assignment"])["role"] != "exec" or run["start_step"] != "started":
            return None
        from sd_db.runner_exec import assignment_result
        try:
            return assignment_result(connection, run["assignment"], run["id"])
        except (OSError, ValueError, SdDbError):
            # Missing or conflicting evidence remains uncertain; never replay.
            return None

    def _response(self, connection, child, request, *, deadline):
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        try:
            while True:
                if self._restore_pending(connection):
                    processes.terminate_owned(request["run"])
                    raise store.RunnerRefused("database restore interrupted this owned attempt; recovery reconciliation is required")
                run = store.run_state(connection, request["run"]["id"])
                if run["cancel_requested"] or time.monotonic() > deadline:
                    processes.terminate_owned(run)
                    raise store.RunnerRefused(run["cancel_requested"] or "assignment time budget exceeded")
                if selector.select(timeout=min(self.config.interval, 1)):
                    line = child.stdout.readline()
                    if not line:
                        raise store.RunnerRefused("supervisor exited without an acknowledgement")
                    result = json.loads(line)
                    if not result.get("ok"):
                        raise store.RunnerRefused(result.get("error", "supervisor action failed"))
                    return result
        finally:
            selector.close()

    def action(self, connection, child, request, action, deadline, **extra):
        if self._restore_pending(connection):
            raise store.RunnerRefused("database restore holds every new supervisor acknowledgement")
        current = store.run_state(connection, request["run"]["id"])
        if current["owner"] != request["run"]["owner"] or current["journal_version"] < request["run"]["journal_version"]:
            raise store.RunnerRefused("owned run changed before supervisor acknowledgement")
        child.stdin.write(json.dumps({"action": action, "request": request, **extra}) + "\n")
        child.stdin.flush()
        return self._response(connection, child, request, deadline=deadline)

    def tool_environment(self, ident: str, *, remote=False) -> dict:
        """What a runner-owned tool in the clone sees: the base, and the remote's keys only when it needs them."""
        names = ("PATH", "HOME", "LANG", "TERM")
        if remote:
            names += ("SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")
        environment = {key: os.environ[key] for key in names if key in os.environ}
        environment["PATH"] = self.search_path
        environment["HOME"] = str(self.config.home)
        environment["SD_ASSIGNMENT"] = ident
        return environment

    def ship(self, connection, child, request, verb, deadline):
        executable = self.config.pack / "bin/sd-ship"
        if not executable.is_file():
            raise store.RunnerRefused("mechanical sd-ship adapter is absent; preserving authored work for manual delivery")
        argv = [sys.executable, str(executable), verb, "--database", str(self.config.database), "--item", str(request["item"]), "--json"]
        if verb == "merge":
            argv += ["--run", request["run"]["id"], "--expected-head", request["run"]["authored_head"], "--watch"]
        environment = self.tool_environment(request["run"]["id"], remote=True)
        try:
            result = self.action(connection, child, request, "ship", deadline, argv=argv, environment=environment)["ship"]
            if not result.get("ok"):
                raise store.RunnerRefused(f"ship {verb} did not complete: {result}")
        except store.RunnerRefused as error:
            # `prepare` holds the review cap. When it refuses, the receipt it
            # left says whether a hard stop is why; the row then names it.
            # The receipt is the branch's, not this run's, so the head this
            # run authored selects it: an earlier run's pass classifies
            # nothing here (sd:1221).
            if verb == "prepare" and (stop := hard_stops.from_receipt(
                    store.ship_receipt(connection, request["run"]["id"]), head=request["run"]["authored_head"])):
                raise stop from error
            if block := toolchain.from_ship_refusal(str(error)):
                raise block from error
            raise
        if result.get("reviewed_head"):
            request["run"] = self.persist(connection, request["run"]["id"], reviewed_head=result["reviewed_head"])
        if result.get("phase") == "merged":
            pr = result["pull_request"]
            database_write(connection, store.record_merge, request["item"], run_id=request["run"]["id"], evidence={
                "url": pr["url"], "head": result["head"], "merge_commit": result["merge_commit"],
                "base": pr["base"], "repository": pr["repository"], "observed_at": result["observed_at"]})
            if result.get("deliver"):
                from sd_db.ship import prepare_delivery
                proof = prepare_delivery(connection, request["run"]["id"], verification_root=Path(request["run"]["work_path"]))
                request["run"] = self.persist(connection, request["run"]["id"], delivery_proof=json.dumps(proof, sort_keys=True))
        return result

    def watch_deliveries(self):
        if time.monotonic() - self.last_delivery_watch < 60:
            return
        self.last_delivery_watch = time.monotonic()
        executable = self.config.pack / "bin/sd-ship"
        if not executable.is_file():
            return
        connection = connect(self.config.database)
        try:
            observed = 0
            for item in store.delivery_candidates(connection):
                result = subprocess.run([sys.executable, str(executable), "observe", "--database", str(self.config.database), "--item", str(item["id"]), "--json"],
                    cwd=paths.disk(item["repo"]), capture_output=True, text=True, timeout=45, check=False,
                    env={**{key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TERM", "USER", "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN") if key in os.environ},
                         "PATH": self.search_path})
                if result.returncode == 0:
                    observation = json.loads(result.stdout)
                    if observation.get("phase") == "merged":
                        database_write(connection, store.queue_reconciliation, item["id"], observation)
                    observed += 1
            self.delivery_watch_result = {"observed": observed}
        except (OSError, ValueError, SdDbError, subprocess.SubprocessError, sqlite3.Error) as error:
            self.delivery_watch_result = {"error": str(error)}
        finally:
            connection.close()

    def pulse(self, connection):
        # Read before this tick claims anything: the heartbeat that names the
        # token tells `restart` that no claim follows it (sd:1951).
        drain = drain_request(self.config.database)
        holds = []
        try:
            report = storage.preflight(self.config.database, self.config.work, self.config.retention, floor_gb=self.config.floor_gb,
                                       verified=self.storage_verified)
        except (OSError, ValueError, subprocess.SubprocessError, ExpatError) as error:
            holds.append({"probe": "storage", "reason": str(error)})
            database = None
            try:
                # APFS/quota probing can fail after observing low database space.
                # Reobserve that safety floor independently; never invent a value.
                database = storage.capacity(self.config.database)
            except (OSError, ValueError, store.RunnerRefused) as capacity_error:
                holds.append({"probe": "database_capacity", "reason": str(capacity_error)})
            report = {"ok": False, "problems": [str(error)], "dispatch_allowed": False,
                      "database": database, "observation": "unavailable",
                      "database_below_floor": database["free"] < self.config.floor_gb * 1e9 if database is not None else None}
        if report["database_below_floor"]:
            for row in store.active_runs(connection):
                if failed := self.stop_owned(row):
                    holds.append(failed)
        commit = None
        try:
            commit = gitops.head(self.config.pack, "HEAD") or None
            if commit is None:
                raise ValueError("pack HEAD is unavailable")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            holds.append({"probe": "pack_head", "reason": str(error)})
        if holds or drain:
            report = {**report, "dispatch_allowed": False}
        # Today reads the copies waiting on space from here: the floor, the free space, the space needed.
        space_holds = []
        for row in store.active_runs(connection):
            try:
                held = json.loads(row["quarantine"]) if row["quarantine"] else {}
            except ValueError:
                continue
            if isinstance(held, dict) and held.get("reason") == "no space":
                space_holds.append({"run": row["id"], "assignment": row["assignment"], **held})
        return self.heartbeat(connection, {"pid": os.getpid(), "owner": self.owner, "interval_seconds": self.config.interval,
            "healthy": report["ok"] and not holds and not report["database_below_floor"] and not self._restore_pending(connection) and not self.pending_endings, "storage": report,
            "restoration_pending": self._restore_pending(connection),
            "database_holds": sorted(self.pending_endings), "probe_holds": holds, "space_holds": space_holds,
            "pack_commit": commit, "delivery_watch": self.delivery_watch_result, "executables": self.executables,
            "archive_refresh": self.archive_watch_result, "drain": drain, **self.deployed()})

    def deployed(self) -> dict:
        """The heartbeat's `runner_commit` and `runner_checkout`, which `heartbeat_state` compares (sd:1952)."""
        return {"runner_commit": self.runner_commit, "runner_checkout": str(CHECKOUT)}

    def refresh_archives(self):
        from .archive_refresh import refresh
        try:
            self.archive_watch_result = refresh(self.config)
        except sqlite3.Error as error:
            self.archive_watch_result = {"ok": False, "error": str(error)}

    def _restore_pending(self, connection):
        return store.restoration_pending(connection) or any((self.config.database.parent / name).exists()
            for name in ("runner-restore-intent.json", "publication-restore-intent.json"))

    def answer(self, connection, request: dict, provider: dict, deadline: float) -> tuple[str, str]:
        """The `url` author's provider step: the library's one call in place of a supervised process (sd:234 slice 8d).

        The prompt is the one a `start` session reads on stdin, and the
        response body goes where that session's output goes, the clone's
        `.git/sd-provider.log`, which `finish` retains and `release` names
        on the `exec` note, written before the ending so a `done` never
        stands without its work product: a failed write is the `OSError`
        `_execute` ends the row `blocked` on. The call is not wrapped in
        `database_write`: a retry would be a second attempt on the wire
        under the same call id. The remaining time budget is the call's
        timeout, and a budget already spent is refused before the wire, as
        `_response` refuses it before a supervised action; a cancel request
        is read only after the answer, as the ending's.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise store.RunnerRefused("assignment time budget exceeded")
        log = Path(request["run"]["work_path"]) / ".git/sd-provider.log"
        answered = store.answer_url(connection, request["run"]["id"], entry=provider["entry"], prompt=prompt(request, provider),
                                    environ=dict(os.environ), registry=provider.get("registry"), transport=self.transport,
                                    timeout=remaining, retain=lambda body: log.write_text(body, encoding="utf-8"))
        return answered["outcome"], answered["detail"]

    def execute(self, connection, request: dict, *, command=None, environment=None, provider=None, check=None) -> dict:
        """Run one claimed row to its recorded ending.

        `command`, `environment`, `provider` and `check` are the fixture seams:
        a test supplies the provider argv, its environment, what the registry
        would have resolved (`provider`, `vendor`, `start`, `bill`, `reader`)
        and the repository check argv. Production resolves all four itself.
        """
        ident = request["run"]["id"]
        try:
            result = self._execute(connection, request, command=command, environment=environment, provider=provider, check=check)
        except sqlite3.Error as error:
            detail = ("database contention exhausted retry budget" if database_busy(error) else "database failure")
            detail += f"; owned supervisor stopped or awaiting join; ending held: {error}"
            self.pending_endings.setdefault(ident, {"run": dict(request["run"]), "outcome": "blocked",
                                                    "detail": detail, "exit_code": None})
            self.pending_endings[ident]["database_error"] = detail
            if detail not in self.pending_endings[ident]["detail"]:
                self.pending_endings[ident]["detail"] += "; " + detail
            return {**request["run"], "database_hold": True, "detail": detail}
        self.pending_endings.pop(ident, None)
        return result

    def record_session(self, connection, request: dict, provider: dict, clone: Path) -> None:
        """Record a stopped session's notes and cost without replacing its failure.

        The caller is unwinding an error that ends the row and names its
        detail. A notes file this session cannot parse, or a database this
        attempt can no longer write, must not become that name, so the
        second failure is dropped here and the first one stands.
        """
        try:
            session_record.record(connection, database_write, request, provider, clone)
        except (OSError, ValueError, subprocess.SubprocessError, SdDbError, sqlite3.Error):
            pass

    def _execute(self, connection, request: dict, *, command=None, environment=None, provider=None, check=None) -> dict:
        ident = request["run"]["id"]
        self.persist(connection, ident)
        child = None
        joined = True
        exit_code = None
        stop = None
        outcome, detail = "blocked", "setup did not complete"
        #: The checkout file the clone's check overrides came from, or None (sd:1752).
        overrides = None
        deadline = time.monotonic() + request["budget_minutes"] * 60
        try:
            if self._restore_pending(connection):
                raise store.RunnerRefused("database restore holds dispatch")
            if request["role"] == "exec":
                from sd_db import runner_exec
                runner_exec.resolve_assignment(connection, request["id"])
            env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TERM", "SSH_AUTH_SOCK") if key in os.environ}
            env["SD_ASSIGNMENT"] = ident
            import sd_db
            env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).resolve().parents[1]), str(Path(sd_db.__file__).resolve().parent.parent)])
            child = subprocess.Popen([sys.executable, "-m", "sd_runner.supervisor"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True, env=env)
            self.supervisors[ident] = child
            identity = processes.start_identity(child.pid)
            if identity is None:
                raise store.RunnerRefused("cannot register supervisor kernel start identity")
            ownership = {"supervisor_pid": child.pid, "supervisor_pgid": child.pid,
                         "supervisor_start": identity, "start_step": "supervised"}
            # Preserve proven child identity locally even if its SQL write stalls.
            request["run"] = {**request["run"], **ownership}
            request["run"] = self.persist(connection, ident, **ownership)
            for action, step in (("clone", "cloned"), ("branch", "branched"), ("merge", "merged")):
                if request["scope"] == "reconcile" and action == "merge":
                    continue
                result = self.action(connection, child, request, action, deadline)
                fields = {"start_step": step}
                if "base_head" in result:
                    fields["base_head"] = result["base_head"]
                request["run"] = self.persist(connection, ident, **fields)
                if result.get("prepared_branch"):
                    database_write(connection, store.branch_prepared, ident, result["prepared_branch"])
                overrides = result.get("local_overrides") or overrides
            if request["role"] == "exec":
                descriptor = runner_exec.resolve_assignment(connection, request["id"])
                request["run"] = self.persist(connection, ident, start_step="started")
                result = self.action(connection, child, request, "exec", deadline,
                                     descriptor=descriptor, home=str(self.config.home))
                exit_code = result["exit_code"]
                database_write(connection, runner_exec.complete, descriptor["note"], exit_code=exit_code,
                                     output_path=result["output_path"])
                outcome = "done" if exit_code == 0 else "blocked"
                detail = f"registered command exited {exit_code}; output note {descriptor['note']}"
            elif request["role"] == "merge":
                current = (json.loads(request["result"])["head"] if request["scope"] == "reconcile"
                           else gitops.head(Path(request["run"]["work_path"]), "HEAD"))
                request["run"] = self.persist(connection, ident, authored_head=current, start_step="started")
                if request["scope"] != "reconcile" and request["run"]["reviewed_head"] != current:
                    self.ship(connection, child, request, "prepare", deadline)
                result = self.ship(connection, child, request, "reconcile" if request["scope"] == "reconcile" else "merge", deadline)
                if result.get("phase") != "merged":
                    raise store.RunnerRefused("automatic merge did not verify delivery; manual action required")
                outcome, detail = "done", "remote merge verified"
            else:
                verify_skill_source(request)
                if request["scope"].startswith("skill-use:"):
                    request.update(self.action(connection, child, request, "skill-context", deadline, pack_root=str(self.config.pack)))
                if command is None:
                    argv, provider_env, provider = provider_command(self.config, request, {**os.environ, "PATH": self.search_path})
                    check = check_command(self.config)
                    if request["role"] != "reviewer":
                        # Delivery reviews with another vendor; a session whose
                        # work nothing here can review is not started (sd:1762).
                        toolchain.require_reviewer(provider.get("reviewers", ()), provider["vendor"], self.search_path)
                else:
                    argv, provider_env = command, environment or {}
                    provider = provider or {"provider": "fixture", "vendor": "fixture"}
                    provider_env = {**provider_env, "SD_ASSIGNMENT": ident}
                # Directory creation is setup, under the acknowledged supervisor.
                request["run"] = self.persist(connection, ident, provider=provider["provider"], vendor=provider["vendor"], start_step="started")
                if provider.get("entry") is not None:
                    outcome, detail = self.answer(connection, request, provider, deadline)
                else:
                    clone = Path(request["run"]["work_path"])
                    try:
                        result = self.action(connection, child, request, "provider", deadline,
                                             argv=argv, environment=provider_env, prompt=prompt(request, provider),
                                             reviewer=request["role"] == "reviewer")
                    except BaseException:
                        # A cancellation, an expired deadline, an interrupted
                        # restore or a lost supervisor raises here, before the
                        # normal record below, and the ending records no usage:
                        # the session's spend was lost with it (sd:1221).
                        self.record_session(connection, request, provider, clone)
                        raise
                    exit_code = result["exit_code"]
                    # What the session left is recorded before its exit code is
                    # judged: a stopped session's notes and cost are still its own.
                    session_record.record(connection, database_write, request, provider, clone)
                    if stop := hard_stops.signal(clone):
                        raise stop
                    if exit_code != 0:
                        raise store.RunnerRefused(f"provider exited {exit_code}; see retained provider log")
                    if request["role"] == "reviewer":
                        from sd_db.skills_catalog import record_review_proposals
                        output = Path(request["run"]["work_path"]) / ".git/sd-skill-review.json"
                        if output.stat().st_size > 65536:
                            raise store.RunnerRefused("skill review result exceeds 64 KiB")
                        database_write(connection, record_review_proposals, request["item"], request["id"], provider["provider"], json.loads(output.read_text()))
                        outcome, detail = "done", "skill review proposals recorded for operator selection"
                    else:
                        if check:
                            # A Rust clone starts from its repository's last
                            # passing build, in its own `target/`; a clone the
                            # copy fails for builds cold (sd:1814).
                            seed = cargo_seed.seed_path(self.config.work, request["run"]["repo"])
                            cargo_seed.seed(clone, seed)
                            # A fresh clone holds no dependencies. Installing
                            # them is setup, and its failure is the runner's
                            # environment, never the failing test (sd:1762).
                            if install := toolchain.installer(clone, self.search_path):
                                installed = self.action(connection, child, request, "install", deadline, argv=install,
                                                        environment=self.tool_environment(ident))["install"]
                                if block := toolchain.from_install(install, installed):
                                    raise block
                            # The failing test is decided here, in the clone,
                            # before anything is pushed or shipped.
                            verified = self.action(connection, child, request, "check", deadline, argv=check,
                                                   environment=self.tool_environment(ident))["check"]
                            if stop := hard_stops.from_check(verified):
                                raise stop
                            cargo_seed.refresh(clone, seed)
                            # The passed check goes on the row, keyed by the tree
                            # it ran against, for sd-review to read instead of
                            # running the same gate on the same tree again
                            # (sd:495). A stop above leaves no record.
                            database_write(connection, store.record_check, ident, head=gitops.head(clone, "HEAD"),
                                           tree=gitops.tree(clone), exit_code=verified["exit_code"], argv=check,
                                           checks=verified.get("checks"))
                        authored = gitops.check_attribution(request["run"], assignment_base(connection, request["id"]))
                        request["run"] = self.persist(connection, ident, authored_head=authored)
                        if command is None:
                            self.ship(connection, child, request, "prepare", deadline)
                        outcome, detail = "done", "author completed; delivery awaits verified merge"
        except (OSError, ValueError, subprocess.SubprocessError, SdDbError, sqlite3.Error) as error:
            detail = str(error)
            try:
                row = store.run_state(connection, ident)
            except store.RunnerRefused:
                row = request["run"]
            outcome = "cancelled" if row["cancel_requested"] else "blocked"
            if isinstance(error, hard_stops.HardStop) and outcome == "blocked":
                stop = error
                database_write(connection, store.record_hard_stop, ident, kind=stop.kind, evidence=stop.evidence)
        finally:
            if overrides:
                # The ending's `exec` note says which overrides the check read.
                detail = f"{detail}; local overrides copied from {overrides}"
            if child is not None:
                # C133: close and join the supervisor before entering ending.
                try:
                    child.stdin.close()
                    child.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    processes.terminate_owned(request["run"])
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        joined = False
                        self.persist(connection, ident, quarantine="supervisor remains alive; waiting to join", detail=detail)
                if joined:
                    child.stdout.close()
                    child.stderr.close()
                    self.supervisors.pop(ident, None)
            recovery_hold = self._restore_pending(connection)
            try:
                observed = store.run_state(connection, ident)
                recovery_hold = recovery_hold or observed["owner"] != request["run"]["owner"] or observed["journal_version"] < request["run"]["journal_version"]
            except store.RunnerRefused:
                recovery_hold = True
            if joined and not recovery_hold:
                self.pending_endings[ident] = {"run": dict(request["run"]), "outcome": outcome,
                                               "detail": detail, "exit_code": exit_code}
                completed = self.execution_result(connection, observed)
                if completed:
                    exit_code = completed["exit_code"]
                    if outcome != "cancelled":
                        outcome = "done" if exit_code == 0 else "blocked"
                    detail = f"registered command exit {exit_code} verified from durable receipt"
                self.pending_endings[ident].update(outcome=outcome, detail=detail, exit_code=exit_code)
                database_write(connection, store.begin_ending, ident, outcome=outcome, detail=detail)
                self.persist(connection, ident)
        if recovery_hold:
            return {**request["run"], "detail": detail, "recovery_hold": True}
        if not joined:
            return store.run_state(connection, ident)
        return self.finish(connection, ident, exit_code=exit_code)

    def finish(self, connection, ident: str, *, exit_code=None) -> dict:
        # A stopped daemon's operator controls can race its restart. Only one
        # owner may push, archive, or rename this attempt at a time.
        with journal.lock(self.config.database.parent / "runner-ending" / f"{ident}.lock"):
            return self._finish(connection, ident, exit_code=exit_code)

    def _finish(self, connection, ident: str, *, exit_code=None) -> dict:
        run = store.run_state(connection, ident)
        if run["released_at"]:
            return run
        try:
            survivors = self.observer(run)
            if survivors:
                return self.persist(connection, ident, quarantine=json.dumps(survivors), detail="clone busy; lease retained")
            run = self.persist(connection, ident, quarantine=None, end_step="survivors_clear")
            beat = self.ending_keepalive(connection, ident)
            clone, retained = Path(run["work_path"]), Path(run["retained_path"])
            if clone.exists():
                # C134: a setup failure can precede any branch. Retain its whole
                # partial clone without running Git against an incomplete store.
                has_branch = run["start_step"] in {"branched", "merged", "started"}
                assignment = store.queue_state(connection, run["assignment"])
                if has_branch and assignment["role"] != "reviewer" and assignment["phase"] != "merged":
                    try:
                        if gitops.dirty(clone):
                            raise store.RunnerRefused("clone has uncommitted work")
                        gitops.durable(run)
                    except store.RunnerRefused as error:
                        storage.archive(clone, retained.parent / "kept.tar", progress=beat, floor_gb=self.config.floor_gb)
                        return self.persist(connection, ident, end_step="kept", detail=f"{error}; whole clone archived; resume after resolving it")
                self.persist(connection, ident, end_step="durable" if has_branch else "partial_setup")
                if has_branch and run["ignored_manifest"] is None:
                    manifest = storage.ignored_manifest(clone, progress=beat)
                    run = self.persist(connection, ident, ignored_manifest=json.dumps(manifest, sort_keys=True))
                storage.retain(clone, retained, freezer=self.freezer)
            elif retained.exists():
                self.freezer(retained)
            else:
                # Claimed then killed before mkdir owns no bytes; a durable empty
                # retained directory records that observed setup boundary.
                retained.mkdir(parents=True)
                self.freezer(retained)
            self.persist(connection, ident, end_step="retained_clone")
            stored = json.loads(run["ignored_manifest"] or "{}")
            manifest = storage.kept_ignored(retained, stored)
            if len(manifest) != len(stored):
                # A manifest stored before sd:1774 lists Cargo output. Storing
                # what the copy keeps makes the journal and prune read that.
                run = self.persist(connection, ident, ignored_manifest=json.dumps(manifest, sort_keys=True))
            storage.preserve_ignored(retained, manifest, progress=beat, floor_gb=self.config.floor_gb)
            self.persist(connection, ident, end_step="retained")
            log = retained / ".git/sd-provider.log"
            result = database_write(connection, store.release, ident, output_path=str(log) if log.exists() else None, exit_code=exit_code)
            journal.persist(self.config.database, result)
            return result
        except storage.NoSpace as error:
            # A second copy met ENOSPC. The retained clone or the kept worktree
            # holds the bytes, the partial copy stays where it stopped, and the
            # next tick tries again; nothing is removed to make the copy fit.
            held = self.space_hold(error)
            return self.persist(connection, ident, quarantine=json.dumps(held, sort_keys=True),
                                detail=f"{error}; work volume free {held['free_bytes']} bytes, free_floor_gb={self.config.floor_gb}; "
                                       "clone retained, partial copy kept, retried every tick")
        except (OSError, subprocess.SubprocessError, store.RunnerRefused) as error:
            return self.persist(connection, ident, quarantine=json.dumps({"reason": str(error)}), detail=f"cleanup held: {error}")

    def ending_keepalive(self, connection, ident: str):
        """A heartbeat for the file copies of one ending, at most once per interval.

        The ignored inventory and copy run file by file, for minutes on a large
        clone, and `pulse` writes only between ticks; the row went stale while
        the runner worked (sd:1774). This refreshes the row without probing:
        the last body stays, `probes_from` names when its probes ran, and
        `ending` names the run. No row yet means nothing to refresh.
        """
        written = None

        def beat():
            nonlocal written
            if written is not None and time.monotonic() - written < self.config.interval:
                return
            written = time.monotonic()
            try:
                current = store.heartbeat_state(connection)
                if "timestamp" not in current:
                    return
                body = {key: value for key, value in current.items() if key not in {"timestamp", "age_seconds", "ok"}}
                database_write(connection, store.heartbeat, {**body, "pid": os.getpid(), "owner": self.owner,
                    "interval_seconds": self.config.interval, "probes_from": body.get("probes_from", current["timestamp"]),
                    "ending": ident})
            except (sqlite3.Error, SdDbError):
                pass  # The copy matters more; a row left stale says so on its own.
        return beat

    def space_hold(self, error: storage.NoSpace) -> dict:
        """What Today reads for a copy waiting on space: the floor, the free space, the space needed."""
        try:
            free = storage.capacity(error.path)["free"]
        except (OSError, store.RunnerRefused):
            free = None
        return {"reason": "no space", "copy": error.copy, "path": str(error.path), "needed_bytes": error.needed,
                "free_bytes": free, "free_floor_gb": self.config.floor_gb}

    def _drain_endings(self, connection):
        for ident, pending in list(self.pending_endings.items()):
            thread = self.threads.get(ident)
            if (thread is not None and thread.is_alive()) or self._restore_pending(connection):
                continue
            child = self.supervisors.get(ident)
            if child is not None:
                if child.poll() is None:
                    continue
                child.wait()
                child.stdout.close()
                child.stderr.close()
                self.supervisors.pop(ident)

            # Evidence reads can touch retained files. Keep them outside the
            # short transaction that checks and records the ending decision.
            observed = store.run_state(connection, ident)
            evidence_path = journal.directory(self.config.database) / f"{ident}.json"
            external = journal.read(evidence_path) if evidence_path.exists() else None
            completed = self.execution_result(connection, observed)

            def begin(current, *, ident=ident, pending=pending, observed=observed,
                      external=external, completed=completed):
                with transaction(current):
                    row = store.run_state(current, ident)
                    cached = pending["run"]
                    if (row != observed or self._restore_pending(current) or row["owner"] != cached["owner"]
                            or row["journal_version"] < cached["journal_version"]):
                        raise store.RunnerRefused("database ownership changed during deferred ending")
                    if external and (external["journal_version"] > row["journal_version"] or
                            (external["journal_version"] == row["journal_version"] and journal_differs(external, row))):
                        raise store.RunnerRefused("journal ownership changed during deferred ending")
                    if not row["outcome"] and not row["released_at"]:
                        outcome, detail = pending["outcome"], pending["detail"]
                        if row["cancel_requested"]:
                            outcome, detail = "cancelled", row["cancel_requested"]
                        if completed and outcome != "cancelled":
                            pending["exit_code"] = completed["exit_code"]
                            outcome = "done" if completed["exit_code"] == 0 else "blocked"
                            detail = "registered command outcome recovered from durable receipt"
                        store.begin_ending(current, ident, outcome=outcome, detail=detail)

            try:
                database_write(connection, begin)
            except store.RunnerRefused:
                continue
            self.persist(connection, ident)
            self.finish(connection, ident, exit_code=pending["exit_code"])
            self.pending_endings.pop(ident, None)

    def tick(self, connection):
        try:
            return self._tick(connection)
        except OBSERVATION_FAILURES as error:
            # Preserve this owner even after a partial tick. Restart recovery
            # would reinterpret unrelated live supervisors as abandoned runs.
            self.heartbeat(connection, {
                "pid": os.getpid(), "owner": self.owner, "interval_seconds": self.config.interval,
                "healthy": False, "runtime_holds": [{"reason": str(error)}],
                "storage": {"dispatch_allowed": False, "observation": "unavailable"}})
            return []
        finally:
            self.log_health()

    def _tick(self, connection):
        self._drain_endings(connection)
        pulse = self.pulse(connection)
        try:
            holds = self.restore_holds(connection)
        except store.RunnerRefused as error:
            holds = [{"reason": str(error)}]
        if holds:
            self.heartbeat(connection, {**pulse, "healthy": False, "restore_holds": holds})
            return []
        results = []
        for ident, thread in list(self.threads.items()):
            if not thread.is_alive():
                thread.join()
                del self.threads[ident]
        for ident, child in list(self.supervisors.items()):
            if ident not in self.threads and child.poll() is not None:
                child.wait()
                child.stdout.close()
                child.stderr.close()
                database_write(connection, store.begin_ending, ident, outcome="blocked", detail="supervisor joined after an interrupted end; requeue required")
                self.supervisors.pop(ident)
                self.finish(connection, ident)
        for run in store.active_runs(connection):
            if run["id"] not in self.threads and run["id"] not in self.supervisors and run["outcome"] and (run["end_step"] != "kept" or run["end_action"] == "resume"):
                self.finish(connection, run["id"])
        if not pulse["storage"]["dispatch_allowed"]:
            return []
        if self.pending_endings:
            return []
        database_write(connection, store.queue_automatic_merges)
        holds = []
        for row in store.queued(connection):
            try:
                request = database_write(connection, store.claim, row["id"], owner=self.owner, work_root=self.config.work, retention_root=self.config.retention)
            except store.RunnerRefused as error:
                # A refusal is about this row, so it costs this row its turn and
                # not, as it did before, the turn of every row behind it.
                #
                # It does not cost that row nothing. The skip leaves it
                # `queued`, and `claim`'s ordering gate holds a parallel-lane
                # author row behind any earlier queued serial row in the same
                # repository that `_eligible_after` still allows -- which a
                # refused row, with no predecessor, always is. So a serial row
                # behind this one runs and a parallel author row in its
                # repository does not, on any tick, until someone clears the
                # refused row. That case stalls exactly as it did before this
                # change; what is new is that it is now the only one that does.
                #
                # Skipped and not blocked: `claim` raises the same exception for
                # a permanent refusal (an unrunnable kind, a done item, a
                # missing remote) and for a passing one (a restore mid-reimport,
                # a row deleted between `queued` and `claim`), so blocking here
                # would end queued work on a signal that cannot tell them apart.
                # `_eligible_after` blocks only on a predecessor that has
                # already settled, which is a fact and not a guess. The cost of
                # skipping is one refused claim per tick, which is what a row
                # whose lease is contended already costs.
                holds.append({"assignment": row["id"], "reason": str(error)})
                continue
            if request:
                def execute_owned(selected=request):
                    worker = None
                    try:
                        worker = connect(self.config.database)
                        self.execute(worker, selected)
                    except sqlite3.Error as error:
                        self.pending_endings[selected["run"]["id"]] = {"run": dict(selected["run"]), "outcome": "blocked",
                            "detail": f"worker database unavailable before dispatch; ending held: {error}", "exit_code": None}
                    finally:
                        if worker is not None:
                            worker.close()
                thread = threading.Thread(target=execute_owned, name=f"sd-run-{request['run']['id']}")
                self.threads[request["run"]["id"]] = thread
                thread.start()
                results.append(request)
        if holds:
            # Dispatch continued, so the reasons are the only record of the rows
            # that did not: every refused row, not the first alone. `healthy`
            # stays false until the queue holds none, because a refused row is a
            # row a person has to cancel, requeue or repair. The reasons reach
            # that person through `runner.sh status`, which prints the whole
            # body; `runner_screen.jobs_panel` reads three scalars off the
            # heartbeat and renders none of this.
            self.heartbeat(connection, {**pulse, "healthy": False, "runtime_holds": holds})
        return results

    def await_storage(self, report: dict) -> None:
        """Ask the first preflight again while its only problem is `diskutil` giving no answer (sd:1950).

        The cache is empty at a cold start, so under load a `diskutil` timeout
        refused it, and launchd's KeepAlive relaunched the daemon into the
        same timeout every ThrottleInterval, writing no heartbeat. Here no
        answer is "not yet verified": each interval writes an unhealthy
        heartbeat naming the problems, dispatches nothing, and asks again,
        for COLD_START_WINDOW seconds, after which no answer refuses the
        start as before (sd:970). A definitive problem refuses at once. The
        caller holds `runner.lock`, so the heartbeat is this owner's.
        """
        config = self.config
        deadline = time.monotonic() + COLD_START_WINDOW
        with closing(connect(config.database)) as connection:
            while not report["ok"]:
                unverified = report.get("unverified", [])
                if any(problem not in unverified for problem in report["problems"]) or time.monotonic() >= deadline:
                    raise store.RunnerRefused("; ".join(report["problems"]))
                try:
                    self.heartbeat(connection, {"pid": os.getpid(), "owner": self.owner,
                        "interval_seconds": config.interval, "healthy": False, "storage": report,
                        "starting": {"reason": "work volume not yet verified", "window_seconds": COLD_START_WINDOW,
                                     "remaining_seconds": round(deadline - time.monotonic())}})
                except sqlite3.Error:
                    # The heartbeat only reports the wait; a busy store does not end it.
                    pass
                self.log_health()
                time.sleep(config.interval)
                report = storage.preflight(config.database, config.work, config.retention, floor_gb=config.floor_gb,
                                           verified=self.storage_verified)

    def serve(self, *, once=False):
        config = self.config
        # The cache is empty here (sd:1941), so an answer seeds the pulses that
        # follow, and no answer is waited out for a bounded window (sd:1950).
        report = storage.preflight(config.database, config.work, config.retention, floor_gb=config.floor_gb,
                                   verified=self.storage_verified)
        if not report["ok"] and any(problem not in report.get("unverified", []) for problem in report["problems"]):
            raise store.RunnerRefused("; ".join(report["problems"]))
        # Serial ownership of reconciliation prevents a second daemon treating a
        # first daemon's live runs as abandoned. Parallel sessions are children.
        with journal.lock(config.database.parent / "runner.lock", blocking=False):
            self.await_storage(report)
            self.resolve_tools()
            connection = connect(config.database)
            # Read once: Python keeps the modules it loaded, whatever a pull changes on disk.
            self.runner_commit = checkout_commit()
            try:
                try:
                    holds = self.recover(connection)
                except store.RunnerRefused as error:
                    holds = [{"reason": str(error)}]
                if holds:
                    try:
                        self.heartbeat(connection, {"healthy": False, "interval_seconds": config.interval, "restore_holds": holds,
                                                    **self.deployed()})
                    except sqlite3.Error as error:
                        raise store.RunnerRefused(f"runner recovery held: {holds}; diagnostic heartbeat unavailable: {error}") from error
                    self.log_health()
                    raise store.RunnerRefused("runner recovery has unresolved durable-journal holds")
                while True:
                    if time.monotonic() - self.last_archive_watch >= 60 and (self.archive_watch is None or not self.archive_watch.is_alive()):
                        self.last_archive_watch = time.monotonic()
                        self.archive_watch = threading.Thread(target=self.refresh_archives, name="sd-archive-refresh")
                        self.archive_watch.start()
                    if self.watcher is None or not self.watcher.is_alive():
                        self.watcher = threading.Thread(target=self.watch_deliveries, name="sd-delivery-watch")
                        self.watcher.start()
                    try:
                        self.tick(connection)
                    except sqlite3.Error as error:
                        if not database_busy(error) or (once and not self.threads):
                            raise
                        # Keep this owner and its active children alive. A later
                        # tick retries SQL/ending only, never the external action.
                        if not once:
                            time.sleep(config.interval)
                            continue
                        # A partial once tick already owns workers. Monitor them
                        # below without retrying dispatch or claiming more work.
                    if once:
                        # Workers only watch their own cancellation and deadline.
                        # Keep one global monitor active without claiming more work.
                        while any(thread.is_alive() for thread in self.threads.values()):
                            time.sleep(config.interval)
                            try:
                                self.pulse(connection)
                            except sqlite3.Error as error:
                                if not database_busy(error):
                                    raise
                            self.log_health()
                        self.watcher.join()
                        if self.archive_watch is not None:
                            self.archive_watch.join()
                        for thread in self.threads.values():
                            thread.join()
                        self._drain_endings(connection)
                        if self.pending_endings:
                            raise store.RunnerRefused("database ending remains held; leases and journals retained")
                        return
                    time.sleep(config.interval)
            finally:
                connection.close()


# A library before schema 14 (sd:1439) has no `sd_db.paths` and stores every
# repository path absolute. The services run from this checkout, so a pull can
# reach them before the library moves; against that library a key is the path.
try:
    from sd_db import paths
except ImportError:
    from types import SimpleNamespace as _Namespace
    paths = _Namespace(
        key=lambda value: value,
        disk=lambda value: Path(value).expanduser(),
        same=lambda left, right: left is not None and right is not None and (
            Path(left).expanduser().resolve() == Path(right).expanduser().resolve()),
    )


# A library before sd:1447 has no `runner_journal.canonical`; against it a
# record and a row compare as written, as they did before.
_canonical = getattr(journal, "canonical", lambda record: record)


def journal_differs(external: dict, current: dict) -> bool:
    """Whether a journal record and a run row (or two records) disagree.

    Both sides keyed: a record written before migration 014 names its
    repository by the absolute path the row now holds as a `~/` key, and is
    the same run (sd:1447). A different repository still differs.
    """
    return _canonical(external) != _canonical(current)
