"""The service entrypoint, also used by the thin pack gateway."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import math
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_db.database import connect, default_path
from sd_db.errors import SdDbError

from . import load as load_limit
from . import maintenance, storage
from .runtime import Config, Runner


def configuration(path: Path | None = None) -> Config:
    home = Path.home()
    target = path or home / ".config/sd/runner.json"
    raw = json.loads(target.read_text()) if target.exists() else {}
    return Config(database=Path(raw.get("database", default_path(home))),
                  work=Path(raw.get("work", home / ".local/share/sd/worktrees")),
                  retention=Path(raw.get("retention", home / "Documents/sd-backups/worktrees")),
                  pack=Path(raw.get("pack", home / "repos/platypeeps/sd-ai-command-pack")), home=home,
                  interval=float(raw.get("interval_seconds", 10)), floor_gb=float(raw.get("free_floor_gb", 40)),
                  path=configured_path(raw.get("path", ())))


def configured_path(value) -> tuple[str, ...]:
    """`runner.json`'s `path`: a list of directories, or one `PATH`-style string (sd:1762)."""
    entries = value.split(os.pathsep) if isinstance(value, str) else value
    return tuple(str(entry) for entry in entries if entry)


# launchd label: <prefix>.sd-runner, with the prefix shared by every tool in
# this repository (SYSTEM_TOOLS_LABEL_PREFIX, default local.system-tools).
LABEL = os.environ.get("SYSTEM_TOOLS_LABEL_PREFIX", "local.system-tools") + ".sd-runner"


def agent_loaded(label: str = LABEL) -> bool:
    """Whether launchd holds the runner's agent: `launchctl print` exits 0.

    This is `status`'s third state. A heartbeat is only stale or healthy, and
    a machine that never installed the agent has no heartbeat to judge, so
    `status` answers 3 ("nothing to check", convention 6) when the agent is
    not loaded, and it decides that before it opens the store: such a machine
    usually has no store either (sd:927). `local-health-check` asked this
    same question in front of `runner.sh status` (sd:460); it belongs to the
    tool whose state it is. A host without `launchctl` at all has no agent to
    hold.
    """
    try:
        return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0
    except OSError:
        return False


def heartbeat_state(config: Config) -> dict:
    """The heartbeat state, with a hand-written row refused as SdDbError.

    The library refuses a body that is not an object, but one whose
    `interval_seconds` is a string or a list reaches the freshness
    comparison and raises TypeError, which neither `status` path named
    (sd:1221). Both paths name SdDbError, so the refusal arrives as one:
    the body when the agent is not loaded, `runner: ...` on stderr when it
    is.
    """
    connection = connect(config.database, write=False)
    try:
        return store.heartbeat_state(connection)
    except TypeError as error:
        raise SdDbError(f"the runner heartbeat body is unusable: {error}") from error
    finally:
        connection.close()


def archive_refresh_schedule(config: Config, *, loaded: bool) -> dict:
    """`status`'s `archive_refresh_schedule` (sd:2209): the last and next archive refresh, or the reason it is not read.

    The dashboard reads it from the status body, because it does not know the
    runner's config or retention folder. It never changes the exit code: the
    heartbeat alone decides that. An agent that is not loaded refreshes
    nothing, and its path reads no retention folder: the default one is under
    ~/Documents, where an ungranted read under launchd waits instead of
    failing, and local-health-check runs this verb from a cron job. A loaded
    agent's interpreter already reads that folder for the runner itself.
    """
    if not loaded:
        return {"reason": "the runner agent is not loaded, so no archive refresh is scheduled"}
    from . import archive_refresh
    try:
        report = archive_refresh.schedule(config)
    except (OSError, ValueError, KeyError, TypeError, SdDbError) as error:
        return {"reason": str(error) or type(error).__name__}
    return {key: report[key] for key in ("last_completed_at", "next_due_at", "due", "cadence_seconds")}


#: Seconds `restart` waits for the new daemon's healthy heartbeat.
RESTART_WAIT = 180
#: Seconds the verb retries its lock while the daemon probes it.
RESTART_LOCK_WAIT = 2.0


def agent_pid(label: str = LABEL) -> int | None:
    """The pid `launchctl print` names for the agent, or None when it runs no process."""
    try:
        done = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                              capture_output=True, text=True, check=False)
    except OSError:
        return None
    match = re.search(r"^\s*pid = (\d+)\s*$", done.stdout, re.MULTILINE) if done.returncode == 0 else None
    return int(match.group(1)) if match else None


def restart(config: Config, *, max_load: float | None = None, wait: float = RESTART_WAIT, label: str = LABEL,
            clock=time.monotonic, sleep=time.sleep, stop: bool = False) -> dict:
    """Drain the runner, then kick its agent, and wait for the new daemon (sd:1951).

    A snapshot of an idle queue proves nothing about the moment of the
    kick: the daemon can claim a queued row in between, and `kickstart -k`
    would kill that row's supervisor. So the verb writes a drain marker
    (`runtime.drain_path`), which every pulse reads before its tick claims,
    and waits for a heartbeat that names the marker's token: from then on
    the daemon claims nothing. That heartbeat must come from the agent's
    own pid, so a `--config` naming a database the agent does not serve
    refuses instead of kicking the agent. Then an active assignment (it
    would lose its supervisor), a `recovery-plan` that is not clean (the
    new daemon's recovery holds on it) or a load average at or above
    `max_load` (default: the core count; a cold start under load stalled
    on `diskutil`, sd:1950) refuses. Every refusal leaves launchd alone and
    removes the marker. After `launchctl kickstart -k` the verb waits up to
    `wait` seconds for a healthy heartbeat with a new pid, then removes the
    marker so the new daemon dispatches. A marker left by a verb that died
    expires on its own. `runner_commit` is None from a daemon that does
    not write it.

    The drain must hold from the acknowledgement to the kick, however long
    the verb sleeps in between. So it has no expiry: the daemon honours the
    marker only while this verb holds the restart lock beside the database
    (`runtime.drain_request`), and the kernel drops that lock when the verb
    dies. The marker is written once and never renewed, so a lapsed token
    cannot come back. Just before the kick the verb checks that the marker
    still names its token; a removed or replaced marker refuses.

    `stop` runs every check and the drain, then `launchctl bootout` in place
    of the kick, and returns without a new daemon: `deploy.sh upgrade` replaces
    sd_db while nothing imports it, then calls `start` (sd:2812).
    """
    if not agent_loaded(label):
        return {"ok": False, "reason": f"the {label} agent is not loaded; install it first (runner.sh install-plan)"}
    agent = agent_pid(label)
    if agent is None:
        return {"ok": False, "reason": f"the {label} agent has no running process to drain; start it with launchctl kickstart"}
    refused = load_limit.refusal(max_load)
    if refused:
        return refused
    from .runtime import drain_path, restart_lock_path
    marker = drain_path(config.database)
    with contextlib.ExitStack() as held:
        lock = restart_lock_path(config.database)
        try:
            # The one lock opener both packages share; no other module reaches fcntl.
            # The short wait rides over the daemon's own probe of this lock.
            held.enter_context(journal.lock(lock, blocking=False, noun="restart", wait=RESTART_LOCK_WAIT, poll=0.05,
                                            held=f"another restart is running ({lock}); wait for it"))
        except store.RunnerRefused as refusal:
            return {"ok": False, "reason": str(refusal)}
        # Any marker here is stale: its restart no longer holds the lock.
        token = uuid.uuid4().hex
        _publish(marker, token)
        held.callback(_withdraw, marker, token)
        return _drained_restart(config, token, agent, wait=wait, label=label, clock=clock, sleep=sleep, stop=stop)


def _publish(marker: Path, token: str) -> None:
    """Write the marker atomically, through a temporary file of this writer's own."""
    handle, name = tempfile.mkstemp(dir=marker.parent, prefix=f".{marker.name}.", suffix=".partial")
    try:
        with os.fdopen(handle, "w") as partial:
            json.dump({"token": token, "pid": os.getpid(), "by": "runner.sh restart"}, partial)
        os.replace(name, marker)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _withdraw(marker: Path, token: str) -> None:
    """Remove the marker when it is this verb's."""
    try:
        mine = json.loads(marker.read_text()).get("token") == token
    except (OSError, ValueError, AttributeError):
        mine = False
    if mine:
        marker.unlink(missing_ok=True)


def _marker_names(marker: Path, token: str) -> bool:
    """Whether the marker still names `token`."""
    try:
        return json.loads(marker.read_text())["token"] == token
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _drained_restart(config: Config, token: str, agent: int, *, wait, label, clock, sleep, stop=False) -> dict:
    deadline = clock() + wait
    while (state := heartbeat_state(config)).get("drain") != token:
        if clock() >= deadline:
            return {"ok": False, "reason": f"the daemon serving {config.database} did not acknowledge the drain within {wait:g}s; "
                    "the agent may run code older than the drain, or not serve this database"}
        sleep(min(2.0, max(deadline - clock(), 0.0)))
    if state.get("pid") != agent:
        return {"ok": False, "reason": f"the daemon serving {config.database} is pid {state.get('pid')}; "
                f"the {label} agent runs pid {agent}; --config names a different runner"}
    connection = connect(config.database, write=False)
    try:
        active = sorted({row["id"] for row in connection.execute(
            "SELECT id FROM assignment WHERE status IN ('running', 'ending')")}
            | {run["assignment"] for run in store.active_runs(connection)})
    finally:
        connection.close()
    if active:
        return {"ok": False, "reason": f"the queue is not idle: assignment {', '.join(map(str, active))} is active; "
                "restart when it has ended", "active": active}
    from . import reconciliation
    plan = reconciliation.plan(config)
    if plan["entries"] or plan["journal_issues"] or plan["restore_pending"]:
        return {"ok": False, "reason": f"recovery-plan is not clean ({len(plan['entries'])} entries, "
                f"{len(plan['journal_issues'])} journal issues, restore pending {plan['restore_pending']}); "
                "read runner.sh recovery-plan and resolve it first"}
    from .runtime import drain_path
    if not _marker_names(drain_path(config.database), token):
        return {"ok": False, "reason": "the drain marker was removed or replaced before the kick; the daemon may "
                "have claimed work, so run runner.sh restart again"}
    if stop:
        out = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                             capture_output=True, text=True, check=False)
        if out.returncode:
            return {"ok": False, "reason": f"launchctl bootout exited {out.returncode}: {out.stderr.strip()}"}
        return {"ok": True, "stopped": True, "previous_pid": agent}
    kick = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                          capture_output=True, text=True, check=False)
    if kick.returncode:
        return {"ok": False, "reason": f"launchctl kickstart exited {kick.returncode}: {kick.stderr.strip()}"}
    return _await_new(config, agent, wait=wait, clock=clock, sleep=sleep)


def start(config: Config, *, wait: float = RESTART_WAIT, label: str = LABEL,
          clock=time.monotonic, sleep=time.sleep) -> dict:
    """Bootstrap the agent `stop` booted out, and wait for a healthy heartbeat with a new pid (sd:2812)."""
    if agent_loaded(label):
        return {"ok": False, "reason": f"the {label} agent is already loaded; use runner.sh restart"}
    previous = heartbeat_state(config).get("pid")
    plist = config.home / "Library/LaunchAgents" / f"{label}.plist"
    boot = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                          capture_output=True, text=True, check=False)
    if boot.returncode:
        return {"ok": False, "reason": f"launchctl bootstrap exited {boot.returncode}: {boot.stderr.strip()}"}
    result = _await_new(config, previous, wait=wait, clock=clock, sleep=sleep)
    if not result["ok"]:
        # Stopped is the state the caller knows how to finish from, not a
        # loaded agent that never turned healthy.
        out = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                             capture_output=True, text=True, check=False)
        result["reason"] += ("; booted the agent out again, so it stays stopped" if out.returncode == 0
                             else f"; launchctl bootout exited {out.returncode}, so the agent may still be loaded")
    return result


def _await_new(config: Config, agent, *, wait, clock, sleep) -> dict:
    deadline = clock() + wait
    while True:
        state = heartbeat_state(config)
        if state.get("pid") not in (None, agent) and state.get("ok"):
            return {"ok": True, "pid": state["pid"], "previous_pid": agent,
                    "runner_commit": state.get("runner_commit")}
        if clock() >= deadline:
            return {"ok": False, "previous_pid": agent, "pid": state.get("pid"), "healthy": state.get("healthy"),
                    "reason": f"no healthy heartbeat with a new pid within {wait:g}s; read runner.sh status and the err log"}
        sleep(min(2.0, max(deadline - clock(), 0.0)))


def install_plan(config: Config, *, config_path=None) -> dict:
    launcher = Path(__file__).resolve().parents[1] / "runner.sh"
    label = LABEL
    target = config.home / "Library/LaunchAgents" / f"{label}.plist"
    plist = {"Label": label, "ProgramArguments": [str(launcher), "serve"], "RunAtLoad": True, "KeepAlive": True,
             "ThrottleInterval": 30, "ProcessType": "Background",
             "StandardOutPath": str(config.home / "Library/Logs" / f"{label}.log"),
             "StandardErrorPath": str(config.home / "Library/Logs" / f"{label}.err"),
             "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                      "SD_RUNNER_PYTHON": sys.executable}}
    if config_path is not None:
        plist["ProgramArguments"] += ["--config", str(config_path.resolve())]
    return {"path": str(target), "plist": plistlib.dumps(plist).decode(),
            "storage": storage.preflight(config.database, config.work, config.retention, floor_gb=config.floor_gb),
            "apply": "install the reviewed plist only after provisioning storage and confirming preflight passes"}


class StampedLines:
    """A text stream that starts every line it passes on with the local time (sd:1953).

    launchd sends the daemon's stderr to its err log. Without a time on each
    line, the log could not say when a refusal or a health flip happened.
    `print` and a thread's traceback write through here; a write to file
    descriptor 2 itself, by a child or by the interpreter, does not.
    """

    def __init__(self, stream, clock=time.localtime):
        self.stream = stream
        self.clock = clock
        self.partial = ""
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        with self.lock:
            lines = (self.partial + text).split("\n")
            self.partial = lines.pop()
            for line in lines:
                self.stream.write(f"{self.stamp()} {line}\n")
        return len(text)

    def stamp(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", self.clock())

    def flush(self) -> None:
        self.stream.flush()

    def close(self) -> None:
        """Write a last line that has no newline yet; the stream itself stays open."""
        with self.lock:
            if self.partial:
                self.stream.write(f"{self.stamp()} {self.partial}\n")
                self.partial = ""
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


@contextlib.contextmanager
def stamped_stderr():
    stamped = StampedLines(sys.stderr)
    previous, sys.stderr = sys.stderr, stamped
    # An exception leaves the stream in place: the interpreter writes its
    # traceback after this frame is gone, and that report is an err log line too.
    yield stamped
    sys.stderr = previous
    stamped.close()


def positive_seconds(text: str) -> float:
    """A `--wait` value: a deadline of nan or inf is never reached, so the wait never ends (sd:2837)."""
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a finite number of seconds above 0, not {text!r}")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Owned workflow queue runner")
    parser.add_argument("--config", type=Path)
    sub = parser.add_subparsers(dest="verb", required=True)
    for name in ("preflight", "status", "serve", "once", "install-plan", "prune", "recovery-plan", "archive-plan", "archive-refresh"):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if name == "prune":
            command.add_argument("--days", type=int, default=30)
    command = sub.add_parser("restart")
    command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    command.add_argument("--max-load", type=float)
    command.add_argument("--wait", type=positive_seconds, default=RESTART_WAIT)
    command = sub.add_parser("stop")
    command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    command.add_argument("--max-load", type=float)
    command = sub.add_parser("start")
    command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    command.add_argument("--wait", type=positive_seconds, default=RESTART_WAIT)
    command = sub.add_parser("prune-apply")
    command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    command.add_argument("--days", type=int, default=30)
    command.add_argument("--fingerprint", required=True)
    command.add_argument("--who", required=True)
    command = sub.add_parser("retained-remove")
    command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    # Both are checked by `remove_retained`, so a missing or malformed value
    # is a named refusal, as every other precondition is (sd:1780).
    command.add_argument("--assignment")
    command.add_argument("--who")
    # Only `clone` and a `.pruning-clone` of each checked attempt (sd:1793).
    command.add_argument("--clone-only", action="store_true")
    command = sub.add_parser("commands", add_help=False)
    command.add_argument("arguments", nargs=argparse.REMAINDER)
    command = sub.add_parser("recovery-reconcile")
    command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    command.add_argument("--run", required=True)
    command.add_argument("--fingerprint", required=True)
    command = sub.add_parser("recovery-quarantine")
    command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    command.add_argument("--entry", required=True)
    command.add_argument("--fingerprint", required=True)
    command = sub.add_parser("recovery-unlink")
    command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    command.add_argument("--entry", required=True)
    command.add_argument("--fingerprint", required=True)
    command = sub.add_parser("discard-plan")
    command.add_argument("assignment", type=int)
    command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    command.add_argument("--if-revision", required=True)
    command.add_argument("--expected-run", required=True)
    for name in ("cancel", "resume", "restore", "restore-status"):
        command = sub.add_parser(name)
        command.add_argument("assignment", type=int)
        command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        command.add_argument("--database", type=Path)
        command.add_argument("--if-revision", required=True)
        command.add_argument("--expected-run", required=True)
        if name in {"cancel", "resume"}:
            command.add_argument("--who")
        if name in {"restore", "restore-status"}:
            command.add_argument("--run", type=int)
            command.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.verb in {"serve", "once"}:
        # Their stderr is the err log (sd:1953).
        with stamped_stderr():
            return dispatch(args)
    return dispatch(args)


def dispatch(args) -> int:
    try:
        if args.verb == "commands":
            from sd_db.runner_palette import main as palette_main
            return palette_main(args.arguments)
        config = configuration(args.config)
        if getattr(args, "database", None):
            config = replace(config, database=args.database.resolve())
        if args.verb == "preflight":
            result = storage.preflight(config.database, config.work, config.retention, floor_gb=config.floor_gb)
        elif args.verb == "restart":
            result = restart(config, max_load=args.max_load, wait=args.wait)
        elif args.verb == "stop":
            result = restart(config, max_load=args.max_load, stop=True)
        elif args.verb == "start":
            result = start(config, wait=args.wait)
        elif args.verb == "install-plan":
            result = install_plan(config, config_path=args.config)
        elif args.verb in {"prune", "discard-plan"}:
            connection = connect(config.database, write=False)
            try:
                result = (maintenance.plan_prune(config, connection, days=args.days) if args.verb == "prune"
                    else maintenance.plan_discard(config, connection, args.assignment,
                        expected_revision=args.if_revision, expected_run=args.expected_run))
            finally:
                connection.close()
        elif args.verb == "prune-apply":
            connection = connect(config.database)
            try:
                result = maintenance.apply_prune(config, connection, fingerprint=args.fingerprint, days=args.days,
                    who=args.who, principal=getpass.getuser(), program="runner.sh prune-apply")
            finally:
                connection.close()
        elif args.verb == "retained-remove":
            connection = connect(config.database)
            try:
                result = maintenance.remove_retained(config, connection, args.assignment, who=args.who,
                    principal=getpass.getuser(), program="runner.sh retained-remove", clone_only=args.clone_only)
            finally:
                connection.close()
        elif args.verb in {"recovery-plan", "recovery-reconcile", "recovery-quarantine", "recovery-unlink"}:
            from . import reconciliation
            if args.verb == "recovery-plan":
                result = reconciliation.plan(config)
            elif args.verb == "recovery-quarantine":
                result = reconciliation.quarantine(config, args.entry, fingerprint=args.fingerprint)
            elif args.verb == "recovery-unlink":
                result = reconciliation.unlink_restore_link(config, args.entry, fingerprint=args.fingerprint)
            else:
                result = reconciliation.reconcile(config, args.run, fingerprint=args.fingerprint)
        elif args.verb in {"archive-plan", "archive-refresh"}:
            from . import archive_refresh
            result = archive_refresh.schedule(config) if args.verb == "archive-plan" else archive_refresh.refresh(config)
            if result.get("status") == "held":
                result["ok"] = False
        elif args.verb == "status":
            # 0 healthy / 3 agent not loaded / 1 stale or unhealthy. The
            # agent-not-loaded verdict is decided before the store is opened:
            # a machine that never installed the agent holds no store either,
            # and `connect` refusing the missing file must not turn "nothing
            # to check" into 1 (sd:927). The JSON body is the heartbeat state
            # when the store can be read, so the dashboard and sd-plan.sh read
            # what they read; when it cannot, for any reason a read can fail
            # (absent, not a database, a heartbeat row that is not the JSON
            # object the runner writes: the store refuses that shape as
            # SdDbError, sd:934), the body names why in the shape a missing
            # heartbeat uses. local-health-check reads the code.
            #
            # The body is one line, because that sweep quotes the first line
            # of stdout as the finding text when a tool exits 1
            # (`health-check.sh` `head -1`). Indented, the nightly finding
            # read `sd-runner: {` and named nothing (sd:1237).
            if not agent_loaded():
                try:
                    result = heartbeat_state(config)
                except (OSError, ValueError, SdDbError, sqlite3.Error) as error:
                    result = {"ok": False, "reason": str(error)}
                result = {**result, "archive_refresh_schedule": archive_refresh_schedule(config, loaded=False)}
                print(json.dumps(result, default=str))
                return 3
            result = heartbeat_state(config)
            result = {**result, "archive_refresh_schedule": archive_refresh_schedule(config, loaded=True)}
            print(json.dumps(result, default=str))
            # A body that does not say it is healthy is not healthy: a health
            # verb that defaults to 0 passes whatever forgot the field (sd:1387).
            return 0 if result.get("ok", False) else 1
        elif args.verb in {"cancel", "resume", "restore", "restore-status"}:
            from . import controls
            if args.verb in {"restore", "restore-status"}:
                result = getattr(controls, args.verb.replace("-", "_"))(config, args.assignment, args.destination, run=args.run,
                    expected_revision=args.if_revision, expected_run=args.expected_run)
            else:
                result = getattr(controls, args.verb)(config, args.assignment, expected_revision=args.if_revision, expected_run=args.expected_run,
                    who=args.who or getpass.getuser())
        else:
            Runner(config).serve(once=args.verb == "once")
            result = {"ok": True}
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok", True) else 1
    except (OSError, ValueError, SdDbError) as error:
        print(f"runner: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
