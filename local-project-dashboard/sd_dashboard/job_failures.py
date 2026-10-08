"""Why a scheduled job failed, by rule, and Jev's shadow opinion (sd:1166, sd:2095).

**Rules first.** `classify` places one failed job on four levels, from
`transient` (the next scheduled run will likely pass) to `needs a person`.
It reads launchd's record -- the signal, else the exit code -- and then the
last run's section of `<cron_root>/logs/<job>.log` for a few known
signatures. The first rule that matches wins; no rule matching is `unclear`.
The answer names the rule, never the log text that matched it.

**Nothing acts on it.** The class is a label on the Now row and the
Operations card. It does not retry, disable, reorder or change a
notification; the retry stays the operator's.

**Jev is a shadow, second.** `shadow` asks `jev score` the same question on
the same four levels and passes the rule's answer as `--shadow`, so `jev`
prints the rule's answer back whatever it judged, and records both as one
pair in the judgment ledger. What Jev sees is the job name, its state, the
outcome and the rule's class and reason: no log line leaves the machine.
The call runs in a background thread, once per failure id per server, so a
page load never waits on it. `jev enabled JEV_JOB_TRIAGE --record` gates it:
switched off, unkeyed or absent, nothing is asked and the page is the same.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path

__all__ = ["LEVELS", "classify", "shadow"]

#: Jev's levels and the rule's, in order: the score is the index.
LEVELS = ("transient", "probably transient", "unclear", "needs a person")

#: Who this is in the judgment ledger, and which decision.
JEV_CALLER = "local-project-dashboard"
JEV_STAGE = "JEV_JOB_TRIAGE"

QUESTION = ("A scheduled job on a workstation failed. The state holds its name, the outcome "
            "launchd recorded, and the class a fixed rule gave it. Will the next scheduled "
            "run likely pass on its own, or does a person need to look?")

#: A process that crashed rather than being stopped.
CRASH_SIGNALS = {int(signal.SIGILL), int(signal.SIGABRT), int(signal.SIGFPE),
                 int(signal.SIGBUS), int(signal.SIGSEGV)}

#: Exit codes that say what happened on their own.
EXIT_RULES = {
    124: (1, "timed out (exit 124)"),
    126: (3, "command not executable (exit 126)"),
    127: (3, "command not found (exit 127)"),
    77: (3, "permission refused (exit 77)"),
    78: (3, "configuration error (exit 78)"),
}

#: Log signatures, a person's first: a run that met both needs a look.
LOG_RULES = [(score, why, re.compile(pattern, re.IGNORECASE)) for score, why, pattern in (
    (3, "disk full", r"no space left on device"),
    (3, "an authentication failure", r"unauthori[sz]ed|authentication (?:failed|required)|invalid (?:x-)?api[ -]key"
                                      r"|token (?:has )?expired|expired token|please run /login|bad credentials"
                                      r"|(?:HTTP|status)\D{0,12}\b40[13]\b"),
    (3, "a missing command", r"command not found"),
    (3, "permission denied", r"permission denied|operation not permitted"),
    (0, "a DNS failure", r"could not resolve host|temporary failure in name resolution|name or service not known"
                         r"|nodename nor servname"),
    (0, "a network failure", r"connection (?:refused|reset|timed out)|network is unreachable|operation timed out"),
    (0, "an upstream service failure", r"too many requests|rate.?limit|overloaded|service unavailable|bad gateway"
                                       r"|gateway time-?out|(?:HTTP|status)\D{0,12}\b(?:429|5\d\d)\b"),
)]

#: How much of the log's end is read; a run's own section is far shorter.
TAIL_BYTES = 64 * 1024


def last_run(log: Path | None, name: str) -> str:
    """The log's text from this job's last `starting` line, or "" when unreadable."""
    if log is None:
        return ""
    try:
        with log.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    starts = [match.start() for match in
              re.finditer(rf"^\[{re.escape(name)}\] \S+ starting", text, re.MULTILINE)]
    return text[starts[-1]:] if starts else text


def classify(job: dict, log: Path | None) -> dict:
    """`{"score", "class", "why"}` for one failed job; the first rule that matches wins."""
    killed, code = job.get("last_signal"), job.get("last_exit")
    found = None
    if killed is not None:
        found = ((3, f"crashed with signal {killed}") if killed in CRASH_SIGNALS
                 else (1, f"stopped by signal {killed}"))
    elif code in EXIT_RULES:
        found = EXIT_RULES[code]
    else:
        text = last_run(log, job["name"])
        found = next(((score, f"{why} in the last run's log") for score, why, pattern in LOG_RULES
                      if pattern.search(text)), None)
    score, why = found or (2, "no rule matched" + (f" exit {code}" if code is not None else ""))
    return {"score": score, "class": LEVELS[score], "why": why}


_asked: set[str] = set()
_lock = threading.Lock()


def subject(row: dict) -> str:
    """The ledger's name for one row's triage: `job-triage:<16 hex>` (sd:2953).

    No job name leaves in it. The key is the first 16 hex of the sha256 of
    the row's id, `job:<name>:<exit code or signalN>`, followed by a newline;
    an outcome -- did the next run pass -- recomputes it from the same id as
    `printf '%s\\n' ID | shasum -a 256 | cut -c1-16`.
    """
    key = f"{row['id']}\n".encode()
    return f"job-triage:{hashlib.sha256(key).hexdigest()[:16]}"


def _ask(jev: list[str], rows: list[dict]) -> None:
    """The gate once, then one shadow `score` per row; every failure is swallowed.

    One pass is one run: its calls share a `JEV_RUN` of
    `job-triage-<UTC yyyymmddThhmmss>-<4 hex>` (sd:2953). It is made here and
    never inherited, because the server outlives every pass it makes.
    """
    run = f"job-triage-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{secrets.token_hex(2)}"
    env = {**os.environ, "JEV_RUN": run}
    try:
        gate = subprocess.run(jev + ["enabled", JEV_STAGE, "--record",
                                     "--caller", JEV_CALLER, "--stage", JEV_STAGE],
                              capture_output=True, text=True, env=env)
        if gate.returncode != 0:
            return
        for row in rows:
            state = {"job": row["job"], "state": "failed", "outcome": row["what"],
                     "rule_class": row["triage"]["class"], "rule_reason": row["triage"]["why"]}
            # No caller deadline: `jev` bounds its own request (JEV_TIMEOUT)
            # and writes its rows only if it is left to finish.
            subprocess.run(jev + ["score", QUESTION, "--levels", ",".join(LEVELS),
                                  "--state", "-", "--state-format", "json",
                                  "--shadow", str(row["triage"]["score"]),
                                  "--caller", JEV_CALLER, "--stage", JEV_STAGE,
                                  "--subject", subject(row)],
                           input=json.dumps(state), capture_output=True, text=True, env=env)
    except (OSError, subprocess.SubprocessError):
        pass  # A shadow may never fail the page.


def shadow(rows: list[dict], jev: list[str] | None) -> threading.Thread | None:
    """Ask Jev, in the background, about each failed-job row not asked before.

    `jev` is the command, or None for no Jev at all: the default everywhere
    but the server's `main`, so no test reaches the real one. Returns the
    thread, for a test to join, or None when there was nothing to ask.
    """
    if not jev:
        return None
    with _lock:
        fresh = [row for row in rows if "triage" in row and row["id"] not in _asked]
        _asked.update(row["id"] for row in fresh)
    if not fresh:
        return None
    worker = threading.Thread(target=_ask, args=(jev, fresh), daemon=True)
    worker.start()
    return worker
