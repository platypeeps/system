"""The Skills page's Re-import and Evaluate (sd:3992): one `sd skill intake --json` run, whose report the page shows.

The run is an argv list, never a shell, and the source is one argument after `--`: a https://github.com/ URL or a
catalog skill's name. It installs only with `--approve` and the revision a report printed, for the URL and scope that
report named; the pack's verb checks that revision against what it fetches and what is installed, so an approve of a
stale report writes nothing. The verb fetches with git into its own temporary directory; nothing here calls git or gh,
and `skills_screen` stays free of any process call (test_skill_states).
"""

import json
import re
import subprocess

from sd_db import workflow

from .queue_screen import command

#: An intake fetches with git, each call bounded at 120 s by the verb; a run past this is stopped and its outcome unknown.
INTAKE_SECONDS = 300
SOURCE = re.compile(r"https://github\.com/[\x21-\x7e]{1,1000}|sd-[a-z0-9-]{1,100}")
APPROVE = re.compile(r"[0-9a-f]{16}")
REFUSAL = "Give a https://github.com/ skill URL or the name of a skill sd skill intake installed."
UNKNOWN = "Evaluate again: its next run finishes or undoes a stopped install, and its report says what is installed."


def intake(payload: dict):
    """Check the page's request, then return the run: `sd skill intake --json`, with `--approve` only when the page sent
    the revision a report printed, and then only for the URL and scope that report named."""
    values = dict(payload)
    source, scope, approve = values.get("source"), values.get("scope"), values.get("approve")
    if (set(values) - {"source", "scope", "approve"} or not isinstance(source, str) or not SOURCE.fullmatch(source)
            or "`" in source or source.rstrip("/") == "https://github.com"):
        raise ValueError(REFUSAL)
    if scope is not None and scope not in ("pack", "user"):
        raise ValueError("Choose the pack or user scope.")
    if "approve" in values and not (isinstance(approve, str) and APPROVE.fullmatch(approve)
                                    and source.startswith("https://") and scope is not None):
        raise ValueError("Approve the revision a report printed, with the URL and scope that report named.")
    argv = [command("sd"), "skill", "intake", "--json", *(["--scope", scope] if scope else []),
            *(["--approve", approve] if approve else []), "--", source]

    def run(_connection):
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=INTAKE_SECONDS, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            if approve:
                return 502, {"error": f"sd skill intake gave no answer ({error}). {UNKNOWN}", "outcome": "unknown"}
            return 502, {"error": f"Not evaluated: sd skill intake gave no answer ({error})."}
        said = (done.stderr or done.stdout).strip().removeprefix("sd: ")[-600:]
        try:
            report = json.loads(done.stdout) if done.returncode == 0 else None
        except ValueError:
            report = None
        if isinstance(report, dict) and isinstance(report.get("revision"), str):
            return report
        if approve and done.returncode and said.endswith("nothing written"):
            # The verb's two refusals of an approve, both before it writes (pack sd_skill_intake.intake).
            if said.startswith("approved revision"):
                raise workflow.StaleItem(f"Not installed: {said}")
            raise ValueError(f"Not installed: {said}")
        if approve:
            return 502, {"error": f"sd skill intake exited {done.returncode} without a report: {said or 'no output'}. {UNKNOWN}",
                         "outcome": "unknown"}
        raise ValueError(f"Not evaluated: {said or f'sd skill intake exited {done.returncode}'}")
    return run
