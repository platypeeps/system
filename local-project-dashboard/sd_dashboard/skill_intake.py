"""The Skills page's Re-import and Evaluate (sd:3992): one `sd skill intake --json` run, whose report the page shows.

The run is an argv list, never a shell, and the source is one argument after `--`: a https://github.com/ URL or the name
of an installed skill, a catalog one or one in ~/.claude/skills (sd:3997). It installs only with `--approve` and the
revision a report printed, for the URL and scope that report named; the pack's verb checks that revision against what it
fetches and what is installed, so an approve of a stale report writes nothing. The verb fetches with git into its own
temporary directory; nothing here calls git or gh, and `skills_screen` stays free of any process call (test_skill_states).

The Intake view's tracked repositories (system sd:5478) are the pack's too: `repos` reads `sd skill intake --tracked --json`
and `tracking` runs `--track`, `--untrack` or `--refresh` with one https://github.com/<owner>/<repo> URL. The list is one
workflow database setting row the verb writes; a refresh fetches and judges, and installs nothing.
"""

import json
import re
import subprocess

from sd_db import workflow

from .queue_screen import command

#: An intake fetches with git, each call bounded at 120 s by the verb; a run past this is stopped and its outcome unknown.
INTAKE_SECONDS = 300
#: A user-scope skill keeps its upstream name, so a name is any one path segment sd-skill-adopt accepts (SAFE_NAME_RE).
SOURCE = re.compile(r"https://github\.com/[\x21-\x7e]{1,1000}|[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
APPROVE = re.compile(r"[0-9a-f]{16}")
REFUSAL = "Give a https://github.com/ skill URL or the name of a skill sd skill intake installed."
UNKNOWN = "Evaluate again: its next run finishes or undoes a stopped install, and its report says what is installed."
#: A tracked repository: the pack's `repo_url` shape, owner then repository, nothing after.
REPO = re.compile(r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
#: Reading the list or writing it is local: the verb reads installed skills and one setting row.
LIST_SECONDS = 60
TRACKING = {"track": "--track", "untrack": "--untrack", "refresh": "--refresh"}


def _run(argv: list[str], seconds: float):
    """The one process call: `sd skill intake`, as an argv list, never a shell."""
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=seconds, check=False)


def _said(done) -> str:
    return (done.stderr or done.stdout).strip().removeprefix("sd: ")[-600:]


def _json(done):
    try:
        return json.loads(done.stdout) if done.returncode == 0 else None
    except ValueError:
        return None


def repos():
    """The tracked repositories (`sd skill intake --tracked --json`), or `(503, {error})` naming why they were not read."""
    try:
        done = _run([command("sd"), "skill", "intake", "--json", "--tracked"], LIST_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        return 503, {"error": f"The tracked repositories were not read: sd skill intake gave no answer ({error})."}
    view = _json(done)
    if isinstance(view, dict) and isinstance(view.get("repos"), list):
        return view
    return 503, {"error": f"The tracked repositories were not read: {_said(done) or f'sd skill intake exited {done.returncode}'}"}


def tracking(payload: dict):
    """Check the page's request, then return the run: `--track URL [--scope]`, `--untrack URL` or `--refresh URL`.

    A refresh reports; it never installs: each skill it lists installs only through `intake` with `approve`."""
    values = dict(payload)
    action, url, scope = values.get("action"), values.get("url"), values.get("scope")
    if set(values) - {"action", "url", "scope"} or action not in TRACKING:
        raise ValueError("Track, untrack or refresh one repository.")
    if not isinstance(url, str) or not REPO.fullmatch(url) or url.rsplit("/", 1)[1] in (".", ".."):
        raise ValueError("Give a repository URL: https://github.com/<owner>/<repo>.")
    if scope is not None and (action != "track" or scope not in ("pack", "user")):
        raise ValueError("A scope goes with Track only: pack or user.")
    argv = [command("sd"), "skill", "intake", "--json", f"{TRACKING[action]}={url}", *([f"--scope={scope}"] if scope else [])]
    seconds = INTAKE_SECONDS if action == "refresh" else LIST_SECONDS

    def run(_connection):
        try:
            done = _run(argv, seconds)
        except (OSError, subprocess.SubprocessError) as error:
            if action == "refresh":
                return 502, {"error": f"Not refreshed: sd skill intake gave no answer ({error}). Nothing was installed."}
            return 502, {"error": f"sd skill intake gave no answer ({error}); read the list again to see whether it changed.",
                         "outcome": "unknown"}
        answer = _json(done)
        if isinstance(answer, dict) and isinstance(answer.get("url"), str):
            return answer
        said = _said(done)
        if done.returncode and "changed since they were read" in said:
            raise workflow.StaleItem(f"Not changed: {said}")
        if done.returncode and (action == "refresh" or said.endswith("nothing written") or "not a GitHub repository" in said
                                or "already" in said):
            raise ValueError(f"Not {'refreshed' if action == 'refresh' else 'changed'}: {said}")
        if action == "refresh":
            return 502, {"error": f"Not refreshed: sd skill intake exited {done.returncode}: {said or 'no output'}. Nothing was installed."}
        return 502, {"error": f"sd skill intake exited {done.returncode} without the list: {said or 'no output'}; read the list "
                              "again to see whether it changed.", "outcome": "unknown"}
    return run


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
            done = _run(argv, INTAKE_SECONDS)
        except (OSError, subprocess.SubprocessError) as error:
            if approve:
                return 502, {"error": f"sd skill intake gave no answer ({error}). {UNKNOWN}", "outcome": "unknown"}
            return 502, {"error": f"Not evaluated: sd skill intake gave no answer ({error})."}
        said, report = _said(done), _json(done)
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
