"""The service entrypoint, also used by the thin pack gateway."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from sd_db import runner as store
from sd_db.database import connect, default_path
from sd_db.errors import SdDbError

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


#: Seconds `restart` waits for the new daemon's healthy heartbeat.
RESTART_WAIT = 180


def restart(config: Config, *, max_load: float | None = None, wait: float = RESTART_WAIT, label: str = LABEL,
            clock=time.monotonic, sleep=time.sleep) -> dict:
    """Kick the runner agent when it is safe to, and wait for the new daemon (sd:1951).

    Three guards refuse with a reason before launchd is touched. An active
    assignment would lose its supervisor to the kick. A `recovery-plan`
    that is not clean is what the new daemon's recovery holds on, so it
    would start unhealthy. A load average at or above `max_load` (default:
    the core count) is what starved the cold start's `diskutil` into a
    relaunch loop on 2026-09-28 (sd:1950). Then `launchctl kickstart -k`
    replaces the process, and the verb waits up to `wait` seconds for a
    heartbeat that is healthy and names a new pid. `runner_commit` is the
    checkout commit the new daemon read at start, None from a daemon that
    does not write it.
    """
    if not agent_loaded(label):
        return {"ok": False, "reason": f"the {label} agent is not loaded; install it first (runner.sh install-plan)"}
    connection = connect(config.database, write=False)
    try:
        active = sorted({row["id"] for row in connection.execute(
            "SELECT id FROM assignment WHERE status IN ('running', 'ending')")}
            | {run["assignment"] for run in store.active_runs(connection)})
        before = store.heartbeat_state(connection)
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
    limit = float(max_load if max_load is not None else os.cpu_count() or 1)
    load = os.getloadavg()[0]
    if load >= limit:
        return {"ok": False, "reason": f"the 1-minute load average {load:.1f} is at or above {limit:g}; "
                "a cold start under load can stall on diskutil, so restart when the machine is quieter",
                "load": load, "max_load": limit}
    previous = before.get("pid")
    kick = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                          capture_output=True, text=True, check=False)
    if kick.returncode:
        return {"ok": False, "reason": f"launchctl kickstart exited {kick.returncode}: {kick.stderr.strip()}"}
    deadline = clock() + wait
    while True:
        state = heartbeat_state(config)
        if state.get("pid") not in (None, previous) and state.get("ok"):
            return {"ok": True, "pid": state["pid"], "previous_pid": previous,
                    "runner_commit": state.get("runner_commit")}
        if clock() >= deadline:
            return {"ok": False, "previous_pid": previous, "pid": state.get("pid"), "healthy": state.get("healthy"),
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
    command.add_argument("--wait", type=float, default=RESTART_WAIT)
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
                print(json.dumps(result, default=str))
                return 3
            result = heartbeat_state(config)
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
