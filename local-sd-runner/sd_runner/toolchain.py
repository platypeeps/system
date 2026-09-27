"""What the runner's tools resolve against, and what a clone installs first (sd:1762).

Two assignments on 2026-09-26 authored correct commits and then blocked on
the runner rather than the code. A clone has no `node_modules`, so the
repository's check ran `eslint` and `playwright` that were never installed
and exited 127, which the runner named a failing test. And launchd starts
the runner with a short fixed `PATH` that does not hold `~/.local/bin`, so
`sd-ship`'s readiness could not find the configured `claude` reviewer.

Both are the environment's fault, so both end the row with an
`EnvironmentBlock`, whose detail begins `environment:` rather than
`hard stop: failing test:`. It is a refusal, not one of the three hard
stops: the session did nothing wrong and leaves no followup to fix.

The search path is the operator's: the directories `runner.json` names
under `path` first, then the login shell's `PATH`, then the runner's own.
"""

from __future__ import annotations

import json
import os
import pwd
import shlex
import shutil
import subprocess
from pathlib import Path

from sd_db.runner import RunnerRefused

EVIDENCE_LIMIT = 4000
LOGIN_PROBE_SECONDS = 15
#: Brackets the login shell's answer, so a profile that prints is not read as `PATH`.
MARKER = "__SD_RUNNER_PATH__"

#: The lockfile that declares a clone's dependencies, and the command that
#: installs exactly it. First match wins. The fleet's other lockfiles need no
#: step here: `cargo` and `uv run` install what they need on first use.
INSTALLERS = (
    ("package-lock.json", ("npm", "ci")),
    ("npm-shrinkwrap.json", ("npm", "ci")),
    ("pnpm-lock.yaml", ("pnpm", "install", "--frozen-lockfile")),
)


class EnvironmentBlock(RunnerRefused):
    """A stop the runner's environment caused, not the authored code."""

    def __init__(self, evidence: str):
        self.evidence = " ".join(str(evidence).split())[:EVIDENCE_LIMIT]
        super().__init__(f"environment: {self.evidence}")


def login_path(*, run=subprocess.run) -> list[str]:
    """The `PATH` the operator's login shell sets, or [] when it cannot say.

    The shell is the account's, not `$SHELL`, which launchd does not set.
    A probe that fails costs only its directories: the configured and the
    inherited ones still stand.
    """
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
    except KeyError:
        shell = "/bin/sh"
    environment = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "LANG") if key in os.environ}
    try:
        completed = run([shell, "-lc", f'printf "{MARKER}%s{MARKER}" "$PATH"'], env=environment, stdin=subprocess.DEVNULL,
                        capture_output=True, text=True, timeout=LOGIN_PROBE_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    parts = (completed.stdout or "").split(MARKER)
    if completed.returncode != 0 or len(parts) < 3:
        return []
    return [entry for entry in parts[-2].split(os.pathsep) if entry]


def search_path(*groups) -> str:
    """One `PATH` from ordered groups of directories: absolute, first mention wins."""
    seen = []
    for group in groups:
        for entry in group or ():
            entry = str(Path(entry).expanduser())
            if os.path.isabs(entry) and entry not in seen:
                seen.append(entry)
    return os.pathsep.join(seen)


def installer(clone: Path, path: str) -> list[str] | None:
    """The install argv for the clone's lockfile, its program made absolute; None without a lockfile.

    A lockfile whose installer is not on the search path is an environment
    block naming the program: the check would otherwise run without the
    dependencies and report their absence as a failing test.
    """
    for lockfile, argv in INSTALLERS:
        if (Path(clone) / lockfile).is_file():
            program = shutil.which(argv[0], path=path)
            if not program:
                raise EnvironmentBlock(f"{argv[0]} not found for {lockfile}; dependencies not installed; "
                                       f"searched PATH {path}")
            return [program, *argv[1:]]
    return None


def from_install(argv: list[str], result: dict) -> EnvironmentBlock | None:
    """The block a failed dependency install leaves, from the supervisor's `install` result."""
    if result.get("exit_code") == 0:
        return None
    tail = (result.get("stderr") or "").strip() or (result.get("stdout") or "").strip()
    named = " ".join([Path(argv[0]).name, *argv[1:]])
    return EnvironmentBlock(f"dependency install `{named}` exited {result.get('exit_code')}" + (f": {tail}" if tail else ""))


def executable(start: str) -> str:
    """The program a registry `start` line runs, split as the runner splits it."""
    try:
        words = shlex.split(start or "")
    except ValueError:
        return ""
    return words[0] if words else ""


def resolve(providers, path: str) -> dict[str, str | None]:
    """Each `start` provider's program as an absolute path on `path`, or None where it does not resolve."""
    return {provider.name: shutil.which(executable(provider.start), path=path) if executable(provider.start) else None
            for provider in providers if provider.start}


def require_reviewer(reviewers, author_vendor: str, path: str) -> None:
    """Refuse an author row whose delivery review has no executable to run.

    `sd-ship prepare` reviews with a provider of another vendor than the
    author's, and its readiness refuses `executable_missing` when that
    provider's program does not resolve. That refusal comes after the
    session has spent its budget; this one comes before. A registry with no
    such reviewer at all is policy, and `sd-ship` names it.
    """
    candidates = [provider for provider in reviewers
                  if provider.start and str(provider.vendor).lower() != str(author_vendor).lower()]
    resolved = resolve(candidates, path)
    if candidates and not any(resolved.values()):
        missing = ", ".join(f"{provider.name} ({executable(provider.start) or 'no program'})" for provider in candidates)
        raise EnvironmentBlock(f"review provider executable not found: {missing}; searched PATH {path}")


def from_ship_refusal(message: str) -> EnvironmentBlock | None:
    """The block an `sd-ship` refusal names as the runtime's, or None.

    The supervisor reports a failed adapter as `ship adapter exited N:`
    followed by its JSON; readiness marks a program it cannot run as the
    blocker code `executable_missing` and names the provider in its next
    action.
    """
    _, separator, body = str(message).partition(": ")
    if not separator or not str(message).startswith("ship adapter exited"):
        return None
    try:
        payload = json.loads(body)
        blocker = payload["workflow"]["blocker"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(blocker, dict) or blocker.get("code") != "executable_missing":
        return None
    action = payload["workflow"].get("next_action") or payload.get("error") or ""
    return EnvironmentBlock(f"sd-ship readiness: executable_missing: {action}")
