"""Ask Jev one typed question from the shell, and print the answer.

TypeSafe ships an SDK and an agent skill, and no CLI. Every caller on this
machine is a shell: a cron `JOB_COMMAND`, a git hook, an agent running Bash.
So the useful shape is a command, not a library -- one binary on PATH that
Claude Code, Codex and cron all reach the same way, with no MCP server and no
tool schema loaded into a session to pay for it.

**It prints the answer and nothing else.** `noul` prints a probability,
`choice` prints the chosen key, `score` prints the number. A caller that wants
the distribution asks for `--json`. This is what makes it composable in sh:
the common case needs no parser on the other side.

**It never depends on a Claude session.** Thirteen agent-driven cron jobs died
on 2026-09-19 when that session expired; a job that calls this one cannot fail
that way. That is the whole reason a judgment worth making nightly belongs
here rather than in a `JOB_PROMPT`.

**Nothing here is load-bearing.** Jev is experimental, so a caller must keep
working when it is switched off, unkeyed, or failing. Two shapes make that the
easy path rather than the diligent one: `jev enabled` answers before any
network call, so the old mechanism can stay in the `else`; and `--fallback`
prints a given answer, says why on stderr, and exits 0. A machine with no key
and a machine with the flag off behave identically, which is the point.

The API key is read from the environment. It is never printed, never logged,
and never included in an error message.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import system_tools_config  # noqa: E402

try:
    import jev_meter
except ImportError:                          # pragma: no cover - a copy alone
    # The script still runs when the recorder is not beside it. Metering is
    # the one thing here that may never be the reason a judgment does not
    # happen, and that starts at the import.
    jev_meter = None

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNCONFIGURED = 3

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 3

# The documented backoff: 429 and 529 are "come back later", 5xx is the same
# answer wearing a different number. Anything else is the caller's mistake and
# retrying it just spends the budget twice.
RETRY_STATUS = {429, 500, 502, 503, 504, 529}
BACKOFF = 2.0

ANSWER_KEY = "answer"

#: What the ledger calls this tool's vendor. The ledger's own vocabulary is
#: provider-neutral -- no column there requires any vendor's name -- so this
#: is the one string in the whole path that says which model answered, and a
#: second judgment model records beside this one by passing its own.
PROVIDER = "typesafe"

#: What a row says when no caller named itself. Not an error: a caller that
#: has not been taught the flags yet still gets measured, under a name that
#: says plainly that nobody claimed the call.
UNNAMED = "unknown"

#: The primitive a gate event carries, kept in one place because `judgment.py`
#: reads the same word to keep gate events out of the decision aggregates.
GATE = "gate"

#: What answered on the control arm. The old mechanism has no vendor: it is
#: this machine running the code it ran before there was a model to ask.
BASELINE_PROVIDER = "local"

#: Why a judgment was not used, as a value and not a boolean. The ledger holds
#: the same seven, and they are seven because the repairs differ: a switch is
#: flipped, a key is exported, a link is made, a timeout is tuned, an answer is
#: a defect, an endpoint is chased, and a budget is raised. `no-path` is named
#: separately because it has already caused a silent outage here -- every gate
#: on, the key working, the probe answering, and every PATH consumer skipping
#: its step. `budget` is in the vocabulary before anything writes it.
DECLINES = ("switched-off", "unkeyed", "no-path", "timeout", "invalid",
            "unavailable", "budget")

# The kill switch. A file and not only an environment variable, because the
# callers that matter are cron and launchd, and neither reads a shell profile.
# Absent means enabled: a switch that defaults to off makes every integration
# added after it silently never run, which is the failure this repo already
# knows by name. A machine with no key is off either way.
FLAG_ON = ("1", "on", "true", "yes", "enabled")
FLAG_OFF = ("0", "off", "false", "no", "disabled")


class JevError(RuntimeError):
    """Something the caller can act on, printed without the key in it."""


# --- the switch --------------------------------------------------------------

def flag_file(env) -> str:
    if env.get("JEV_FLAG_FILE"):
        return env["JEV_FLAG_FILE"]
    base = env.get("XDG_CONFIG_HOME") or os.path.join(
        env.get("HOME", os.path.expanduser("~")), ".config")
    return os.path.join(base, "jev", "enabled")


def read_flag(env) -> bool:
    """The file's setting, or True when it says nothing.

    An unreadable or unrecognised file is treated as enabled and not as a
    failure: this is a switch, and a switch nobody set is not an outage.
    """
    try:
        with open(flag_file(env), "r", encoding="utf-8") as fh:
            word = fh.read().strip().lower()
    except OSError:
        return True
    if word in FLAG_OFF:
        return False
    return True


def switched_on(conf: dict, env) -> bool:
    """`JEV_ENABLED` wins over the file, so one call can differ from the fleet."""
    override = (env.get("JEV_ENABLED") or "").strip().lower()
    if override in FLAG_OFF:
        return False
    if override in FLAG_ON:
        return True
    return read_flag(env)


#: What `.env.example` ships. Convention 3 in CLAUDE.md is a committed
#: `.env.example` holding `change-me`, so a machine where someone copied it and
#: stopped is a machine that will exist, and its key is a string that is not
#: empty. Matched case-insensitively and with the separators people actually
#: type, rather than by the one spelling this repository happens to commit.
PLACEHOLDER_KEYS = ("change-me", "changeme", "change_me", "your-key-here", "xxx")


def why_unusable(conf: dict, env) -> tuple:
    """(reason word, prose), or ("", "") when Jev can answer here. No network
    call.

    Two returns and one set of branches. The prose is for a human and has
    always been here; the word is for the ledger, because a decline that reads
    as one string cannot be counted, and a stage whose fallbacks are all
    "something was wrong" is a stage nobody can repair.

    Every branch below is one answer on purpose: each means a caller must take
    its old path, and a caller that has to tell them apart to stay correct is a
    caller that will get it wrong. The reason is prose for a human; the exit
    code is what code reads.

    This sentence used to count the ways, and it said three while the function
    had four -- a fourth was added under a count nobody re-read. Read the
    branches; they are the enumeration.
    """
    if not switched_on(conf, env):
        return "switched-off", "switched off (jev on, or JEV_ENABLED=1, to use it)"
    key = conf["key"].strip()
    if not key:
        return "unkeyed", "no TYPESAFE_API_KEY on this machine"
    if key.lower() in PLACEHOLDER_KEYS:
        # Unconfigured, not broken. Sending `Bearer change-me` would earn a
        # 401 and `status` would report 1 -- a nightly health-check finding
        # that says the service is down when nobody has configured it yet.
        return "unkeyed", "TYPESAFE_API_KEY is still the .env.example placeholder"
    if conf.get("problem"):
        return "unavailable", conf["problem"]
    return "", ""


def unusable(conf: dict, env) -> str:
    """Why Jev cannot answer here, or "" when it can. The prose alone."""
    return why_unusable(conf, env)[1]


#: What a per-stage variable is allowed to say, and what it means when it says
#: nothing. Every integration was opt-in at first -- `JEV_ADVERSARIAL_GATE=1` and
#: one per caller beside it -- and a switch that defaults to off makes each one
#: added after it silently never run: exactly the failure the fleet switch
#: above was written to avoid, repeated once per caller. (How many callers
#: there are is not written here; `KNOWN_CALLERS` in the repository's
#: tests/test_jev_contract.py is the enumeration, and it is enforced.) So a stage nobody set is on,
#: and the machine-wide answers below still decide whether it can run at all.
#: A machine with no key, or with `jev off`, behaves as it always did.
def stage_off(name: str, env) -> bool:
    """Has this caller's own variable been used to switch its stage off?"""
    word = (env.get(name) or "").strip().lower()
    return word in FLAG_OFF


def cmd_enabled(args, conf, out, env=None, **kw) -> int:
    """`jev enabled [STAGE]` -- can Jev answer here, and is that stage on?

    The stage name is optional so the fleet question is still askable on its
    own. Giving it collapses a caller's two checks into one call, which is why
    the vocabulary lives here and not in a copy per caller.
    """
    env = os.environ if env is None else env
    word, reason = why_unusable(conf, env)
    if not reason and args.stage and stage_off(args.stage, env):
        word = "switched-off"
        reason = "%s switched this stage off here" % args.stage
    if args.why:
        out.write("jev: %s\n" % (reason or "enabled"))
    if reason and args.record and args.stage:
        # The decline is the control arm's first fact. Most callers decline
        # here and never reach a judgment verb, so without this their old path
        # runs and nothing anywhere says it did -- and a stage whose declines
        # are silence cannot be compared with one whose calls are counted.
        #
        # Opt-in, and only with a stage. This verb promises to cost nothing
        # and every caller asks it on every run, including runs where it then
        # does nothing at all; a row per ask would be noise rather than
        # measurement. A decline with no stage has nothing to group by.
        #
        # `gate`, not `decline`, and that word is the whole of the second
        # review finding on this branch. A caller that declines here and then
        # records what its own mechanism did writes two rows for one decision:
        # this one, and the completed one. Both used to count as a call and
        # both as a decline, so one decision read as two. `gate` marks this
        # row as the gate saying the old path is about to run, which is a
        # different fact from the old path having run, and `judgment.py`'s
        # decision aggregates exclude it. Nothing is lost: the gate rows are
        # counted on their own, which is the only number a decline-only stage
        # ever had.
        write_event({
            "caller": whose(args, env, "caller"),
            "stage": whose(args, env, "stage"),
            "arm": "baseline",
            "provider": BASELINE_PROVIDER,
            "primitive": GATE,
            "outcome": "fallback",
            "cause": word,
        }, env)
    return EXIT_UNCONFIGURED if reason else EXIT_OK


def cmd_record(args, conf, out, env=None, **kw) -> int:
    """`jev record` -- write one event for a decision this tool did not make.

    The old mechanism lives in the caller, so only the caller can time it and
    say what it answered. This verb is where it says so: the same schema and
    the same stage key as a judgment, which is the whole point, because a
    fallback that cannot be counted against a judgment on the same stage
    answers nothing.

    It sends nothing, needs no key, prints nothing on stdout and always exits
    0. A caller must never fail, or change what it prints, because its
    bookkeeping did.
    """
    env = os.environ if env is None else env
    # A number or nothing. The ledger refuses anything else, and a refusal
    # loses the whole row -- the stage, the arm, the timing -- over the one
    # field the caller got wrong, so the field is dropped and the row is
    # kept. The mistake is said on stderr, because a label passed here is a
    # caller bug and a measurement that vanishes quietly is how it survives.
    answer = judged(args.answer)
    if args.answer is not None and answer is None:
        sys.stderr.write(
            "jev: --answer must be a number -- a probability, a score, or "
            "which of your own criteria won, counting from 1. %r was not "
            "recorded; the rest of the row was.\n" % (args.answer,))
    did = write_event({
        "caller": whose(args, env, "caller"),
        "stage": whose(args, env, "stage"),
        "arm": args.arm,
        "pair": named(args.pair),
        "shadow": args.shadow,
        "provider": args.provider,
        "primitive": args.primitive,
        "outcome": args.outcome,
        "cause": args.decline,
        "answer": answer,
        "ordering": args.positions,
        "duration_ms": args.duration_ms,
        "changed": args.changed or "unknown",
    }, env)
    if args.why:
        sys.stderr.write("jev: %s\n" % (did or "nothing recorded"))
    return EXIT_OK


def set_flag(args, conf, out, env=None, **kw) -> int:
    path = flag_file(env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(args.word + "\n")
    out.write("jev: %s (%s)\n" % (args.word, path))
    return EXIT_OK


# --- what the call cost -------------------------------------------------------
#
# Nothing here may change what a caller sees or how long it waits for it. Every
# function below runs after the answer has been written, or gathers a number
# that was already in hand, and `flush` is the only one that touches a
# database -- after the verb has printed.
#
# The fields are gathered in four places: the parser names the caller, `post`
# times the request and reads the counts the response has always carried,
# the verb holds the judgment, and `main` knows how it ended. `_EVENT` is a
# module global rather than a parameter threaded through all four, the same
# choice `_STDIN_SPENT` makes above and for the same reason: metering should
# not be in the shape of every signature it passes.

#: The call being measured, or None when nothing is.
_EVENT = None


def write_event(event: dict, env) -> str:
    """Hand one finished event to the recorder. Never raises.

    The one door to the ledger, so every caller of it -- a judgment, a
    decline, a caller's own report of its old path -- is refused, degraded and
    recorded in exactly the same way.
    """
    if jev_meter is None:
        return ""
    try:
        return jev_meter.record(event, env)
    except Exception:                        # pragma: no cover - belt and brace
        # `jev_meter.record` promises not to raise. This is the promise being
        # kept anyway, because the cost of being wrong about it is a caller
        # that stopped working over its own bookkeeping.
        return ""


def measure(args, env) -> None:
    """Begin measuring one call.

    Armed for the four verbs that send a request a caller waits on. `status`
    sends one too and is deliberately not armed: it is the tool probing
    itself for `local-health-check`, one fixed question that no caller reads,
    and a per-stage comparison of callers is the wrong place for it.
    """
    global _EVENT
    _EVENT = {
        "caller": whose(args, env, "caller"),
        "stage": whose(args, env, "stage"),
        "provider": PROVIDER,
        "primitive": args.verb,
        "model": None,
        "question_id": named(getattr(args, "id", None)),
        "questions": None,
        "answer": None,
        "confidence": None,
        "tokens_in": None,
        "tokens_out": None,
        "duration_ms": None,
    }


def note(**fields) -> None:
    """Add to the call being measured, or do nothing when none is."""
    if _EVENT is not None:
        _EVENT.update(fields)


def usage_of(response: dict) -> dict:
    """The token counts the response reports, under the ledger's names.

    The response has carried `usage.input_tokens` and `usage.output_tokens`
    since the first call, and every one of them was parsed out of the JSON
    and dropped. A count that is not a whole number is left out rather than
    stored as one.
    """
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return {}
    counts = {}
    for name, column in (("input_tokens", "tokens_in"),
                         ("output_tokens", "tokens_out")):
        value = usage.get(name)
        if type(value) is int and value >= 0:
            counts[column] = value
    return counts


def confidence_of(answer: dict):
    """The answer's confidence when it carries one, else None.

    A `noul` carries none, because a `noul` **is** the probability; `choice`
    and `score` carry one beside the judgment. Anything that is not a number
    from 0 to 1 is read as no confidence rather than written as a wrong one.
    """
    value = answer.get("confidence")
    if type(value) in (int, float) and 0.0 <= value <= 1.0:
        return float(value)
    return None


def judged(value) -> str | None:
    """The judgment as the ledger stores it: a number, or nothing.

    `noul` and `score` already answer with one. `choice` answers with a
    criterion key, which is text the caller wrote and can be a path or a
    subject, so it never reaches here -- `position_of` turns it into which
    one won instead. A value that is not a number is dropped rather than
    stored, because the ledger would refuse it and losing a measurement is
    cheaper than arguing about it after the answer has been printed.
    """
    if value is None:
        return None
    token = f"{value}"
    return token if NUMBER.match(token) else None


#: The one shape an answer may take, kept here as well as in the ledger so
#: `jev` drops a value the ledger would refuse instead of recording a refusal.
NUMBER = re.compile(r"^-?\d+(\.\d+)?$")

#: And the one shape a name may take, for the same reason. `--id`, `--caller`
#: and `--stage` are caller-controlled, so `--id "the quarterly numbers"` and
#: `--stage /Users/someone/private.txt` are both a caller away; the ledger refuses
#: either, and a refusal loses the whole row.
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

#: The ledger's own cap on a name, mirrored here for the same reason the
#: grammar is: this tool is standalone and cannot import `sd_db` to ask. A
#: name longer than this is refused there, and a refusal loses the row.
MAX_NAME = 96


def named(value) -> str | None:
    """The name as the ledger stores it: an identifier, or nothing.

    A subject line, a path or a prompt is submitted content. The ledger
    refuses it and this drops it first, so the stage, the arm and the timing
    survive the one field a caller got wrong.
    """
    if value is None:
        return None
    token = f"{value}"
    if len(token) > MAX_NAME or not IDENTIFIER.match(token):
        return None
    return token


def whose(args, env, field: str) -> str:
    """`--caller` or `--stage`, then its variable, then `UNNAMED`.

    Each is held to the identifier grammar on the way through: a row filed
    under `unknown` is a row that can still be counted, and a refused row is
    not.
    """
    variable = {"caller": "JEV_CALLER", "stage": "JEV_STAGE"}[field]
    return named(getattr(args, field, None)) or named(env.get(variable)) or UNNAMED


def position_of(key, criteria) -> str | None:
    """Which criterion won, counting from 1, or nothing when it is not one of them.

    The caller passed the criteria, so it can read the position back; the
    ledger gets the fact without the string. A key the response invented is
    not a position into anything, and is dropped.
    """
    if key is None:
        return None
    keys = list(criteria)
    return str(keys.index(key) + 1) if key in keys else None


def _changed(outcome: str, declared, printed, fallback) -> str:
    """Did the judgment change what the caller did?

    Three answers and one default. A caller that knows says so with
    `--changed`. A caller that handed over its own answer in `--fallback` has
    already said what it would have done, so the two are compared and no flag
    is needed -- which is why the callers that pass a fallback are measured
    for free. Everyone else is `unknown`, which is most of them: a caller that
    reorders a list and prints it has no baseline in hand at the moment of the
    call, and guessing one would put a number in the report that nothing
    checked.
    """
    if declared:
        return declared
    if outcome == "fallback":
        # The caller's own answer was printed. Whatever the model would have
        # said, it changed nothing, because nothing of it was used.
        return "no"
    if outcome != "ok" or printed is None or fallback is None:
        return "unknown"
    return "yes" if printed.strip() != fallback.strip() else "no"


def flush(outcome: str, *, cause=None, fallback=None, baseline=None,
          env=None) -> str:
    """Write the measured call down, after the answer has been printed.

    `fallback` is the caller's own answer when it gave one: an outcome other
    than `ok` with a fallback in hand is a `fallback` row, because the fact a
    reader wants first is that the old path ran, and `cause` keeps what made
    it run. Returns what the recorder did, for the suite; no caller reads it,
    because no caller may care.
    """
    global _EVENT
    event, _EVENT = _EVENT, None
    if event is None:
        return ""
    printed = event.pop("_printed", None)
    declared = event.pop("_declared", None)
    event.pop("_cause", None)
    if fallback is not None and outcome != "ok":
        outcome = "fallback"
        cause = cause or "unavailable"
    event["outcome"] = outcome
    # Kept on every row that did not end `ok`, and not only on a fallback. A
    # row that says `unavailable` and nothing else sends the reader to look
    # for an outage on a machine where somebody had simply run `jev off`, and
    # in shadow mode there is no fallback to hang the reason on at all.
    event["cause"] = None if outcome == "ok" else cause
    # In shadow mode the caller's own answer is in hand whether or not the
    # call failed, so it, and not the fallback, is what the judgment is
    # compared against.
    against = fallback if baseline is None else baseline
    event["changed"] = _changed(outcome, declared, printed, against)
    return write_event(event, env)


def cause_of(exc) -> str:
    """Which failure class ended the call: the request's word, or the default.

    `post` and `answer_of` know the difference between a request that timed
    out, an endpoint that refused, and a response that carried no answer; by
    the time `main` holds the exception, only the note they left says which.
    """
    if isinstance(exc, TimeoutError):
        return "timeout"
    if _EVENT is not None and _EVENT.get("_cause"):
        return _EVENT["_cause"]
    return "unavailable"


# --- state ------------------------------------------------------------------

#: Stdin can be read once, and more than one option defaults to it. Tracked
#: here rather than compared in each verb, because the collision is a property
#: of stdin and not of any one pair: `--questions -` with the default
#: `--state -` is the one a caller meets first, but `--criteria @-` and
#: `--levels @-` reach the same place, and a verb added later would have to
#: remember a rule it never saw. `main` clears it, so one process may run
#: several invocations -- which is what the suite does.
_STDIN_SPENT = False


def read_source(path: str) -> str:
    """The bytes at `path`, or stdin once.

    The second reader is refused rather than served an empty string. It used
    to be served one: `jev ask --questions -` consumed stdin and the default
    `--state -` then read EOF, so the request went out with an empty state and
    came back answered -- a judgment about nothing, reported as a judgment.
    """
    global _STDIN_SPENT
    if path == "-":
        if _STDIN_SPENT:
            raise JevError("two options read stdin, and stdin can only be read "
                           "once; give one of them a file")
        _STDIN_SPENT = True
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def load_state(path: str, fmt: str):
    """Return the `state` field. Text stays text; json is parsed, not guessed.

    Guessing would read a state file holding only `2026` as a number and send
    a different request than the one the caller wrote, on a day that happened
    to look numeric. The format is a flag for that reason.
    """
    raw = read_source(path)
    if fmt == "text":
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        where = "stdin" if path == "-" else path
        raise JevError(f"{where}: not valid JSON ({exc})") from exc


# --- criteria ---------------------------------------------------------------

def parse_mapping(spec: str) -> dict:
    """`a,b=desc` -> {"a": None, "b": "desc"}; `@file` -> that JSON object.

    Splitting on the first `=` only lets a description carry one. A
    description carrying a comma has no escape here on purpose: `@file.json`
    is the escape, and inventing a quoting dialect for a shell argument is how
    a criteria string starts meaning something different than it reads.
    """
    if spec.startswith("@"):
        loaded = json.loads(read_source(spec[1:]))
        if not isinstance(loaded, dict):
            raise JevError("--criteria @file must hold a JSON object")
        return loaded
    out: dict = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        key, sep, desc = part.partition("=")
        key = key.strip()
        if not key:
            raise JevError(f"criteria entry has no name: {part!r}")
        out[key] = desc.strip() if sep else None
    if not out:
        raise JevError("--criteria is empty")
    return out


def parse_levels(spec: str) -> list:
    """`low,high` -> ["low", "high"]; `@file` -> that JSON array."""
    if spec.startswith("@"):
        loaded = json.loads(read_source(spec[1:]))
        if not isinstance(loaded, list):
            raise JevError("--levels @file must hold a JSON array")
        return loaded
    out = [p.strip() for p in spec.split(",") if p.strip()]
    if not out:
        raise JevError("--levels is empty")
    return out


# --- redaction --------------------------------------------------------------
#
# "Pipe nothing sensitive into Jev" was a rule with nothing behind it. Every
# string in a request's state and questions now passes two sets of patterns
# before it leaves: credential shapes, below, and the operator's own
# `<config>/privacy-patterns`, the file the leak guard reads. A pattern file
# that cannot be read or compiled sends nothing: it is a setting that does not
# parse, so it takes the same road as a malformed JEV_TIMEOUT, and every
# caller falls back to its old path.

REDACTED = "[REDACTED]"

#: A credential must start at a word edge, or `task-...` reads as an `sk-` key.
_EDGE = r"(?<![A-Za-z0-9_])"

#: Credential shapes. Ported from jonathanavis96/jev-kit, `airlock/redact.py`
#: (MIT), and narrowed: a `password` rule there also ate prose such as "the
#: password field", which the docs lint sends.
TOKEN_PATTERNS = tuple(re.compile(p, re.DOTALL) for p in (
    _EDGE + r"apikey_[A-Za-z0-9_]{8,}",
    _EDGE + r"sk-[A-Za-z0-9_-]{10,}",
    _EDGE + r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    _EDGE + r"xox[abprs]-[A-Za-z0-9-]{10,}",
    _EDGE + r"AKIA[0-9A-Z]{16}",
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    r"(?i)\bBearer\s+\S+",
    r"(?i)(?:--password[= ]+|\bpassword=)\S+",
    r"(?i)[A-Za-z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY)[A-Za-z0-9_]*=\S+",
    r"(?<![A-Za-z0-9])[A-Fa-f0-9]{32,}(?![A-Za-z0-9])",
))

#: A long run of base64 characters is a key only when it mixes cases and
#: digits; `homeassistant/components/unifi/sensor` is a path, and sd-review
#: sends paths.
_BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")

#: The POSIX classes `grep -E` knows and Python's `re` does not. Python reads
#: `[[:digit:]]` as a set of punctuation and letters and compiles it, so an
#: untranslated class is a pattern that silently never matches.
_POSIX_CLASSES = {
    "alpha": "A-Za-z", "digit": "0-9", "alnum": "A-Za-z0-9", "upper": "A-Z",
    "lower": "a-z", "space": r"\s", "xdigit": "0-9A-Fa-f", "blank": r" \t",
    "punct": r"!-/:-@\[-`{-~",
}


def privacy_file(env) -> Path:
    """The leak guard's pattern file, resolved the way `lib/config.sh` does."""
    return system_tools_config.root(env) / "privacy-patterns"


def ere_to_python(line: str) -> str:
    def swap(match):
        name = match.group(1)
        if name not in _POSIX_CLASSES:
            raise re.error(f"[:{name}:] has no translation here")
        return _POSIX_CLASSES[name]
    return re.sub(r"\[:([a-z]+):\]", swap, line)


def privacy_patterns(env) -> tuple[list, str]:
    """The operator's patterns and an empty problem, or none and what is wrong.

    Read as `grep -E -f` reads them, after the leak guard drops blank and
    comment lines: one pattern per line, case-sensitive. A missing file is no
    patterns; the leak guard warns about that, and this is not the place.
    """
    path = privacy_file(env)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], ""
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"{path} cannot be read ({exc.__class__.__name__}); nothing was sent"
    compiled = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            # MULTILINE because grep matches line by line: `^host$` must hold
            # for a line inside multi-line state, not only for the whole.
            compiled.append(re.compile(ere_to_python(line), re.MULTILINE))
        except re.error:
            # The line number and not the line: the line is the secret.
            return [], f"{path} line {line_number} does not compile; nothing was sent"
    return compiled, ""


def redact(value, patterns, counted: list):
    """`value` with every match replaced; `counted[0]` gains the number."""
    if isinstance(value, str):
        for pattern in (*TOKEN_PATTERNS, *patterns):
            value, hits = pattern.subn(REDACTED, value)
            counted[0] += hits

        def mixed(match):
            run = match.group(0)
            if (any(c.isdigit() for c in run) and any(c.isupper() for c in run)
                    and any(c.islower() for c in run)):
                counted[0] += 1
                return REDACTED
            return run

        return _BASE64_RUN.sub(mixed, value)
    if isinstance(value, list):
        return [redact(item, patterns, counted) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            # A key is refused, not renamed: a renamed key can collide with
            # another, or break an identifier the caller reads back, such as
            # a question id or a criterion. The message names no key.
            if isinstance(key, str) and redact(key, patterns, [0]) != key:
                raise JevError("a key in the request matches a redaction "
                               "pattern; nothing was sent")
            out[key] = redact(item, patterns, counted)
        return out
    return value


def redacted_payload(payload: dict, patterns) -> tuple[dict, int]:
    """The payload as it may leave, and how many spans were taken out."""
    counted = [0]
    out = dict(payload)
    for field in ("state", "questions"):
        if field in out:
            out[field] = redact(out[field], patterns, counted)
    return out, counted[0]


# --- request ----------------------------------------------------------------

def build_payload(state, questions: dict, model: str) -> dict:
    if not questions:
        raise JevError("no questions to ask")
    return {"state": state, "model": model, "questions": questions}


def number(env, name: str, default, cast):
    """A numeric setting and an empty problem, or the default and what is wrong.

    It does not raise. `settings` is read before `main` reaches its error
    handling and before the kill switch is consulted, so a `ValueError` here
    was an uncaught traceback out of every verb -- including `enabled`, whose
    whole promise is that it costs nothing and answers 0 or 3. A typo in a
    `.env` took down the one call every caller makes to find out whether it
    should call at all.

    Returning the problem instead routes it to `unusable`, so a machine whose
    configuration does not parse behaves exactly like a machine with no key:
    `enabled` says 3, a `--fallback` is honoured, and the reason is named. The
    default that comes back with it is never used for a request, because the
    same problem stops the request.
    """
    raw = env.get(name)
    if raw is None or not raw.strip():
        return cast(default), ""
    try:
        return cast(raw), ""
    except ValueError:
        return cast(default), f"{name} is not a number: {raw.strip()[:40]!r}"


def settings(env=None) -> dict:
    env = os.environ if env is None else env
    timeout, timeout_problem = number(env, "JEV_TIMEOUT", DEFAULT_TIMEOUT, float)
    retries, retries_problem = number(env, "JEV_RETRIES", DEFAULT_RETRIES, int)
    patterns, patterns_problem = privacy_patterns(env)
    return {
        "key": env.get("TYPESAFE_API_KEY", ""),
        "url": env.get("JEV_URL", DEFAULT_URL),
        "model": env.get("JEV_MODEL", DEFAULT_MODEL),
        "timeout": timeout,
        "retries": retries,
        "privacy": patterns,
        "problem": timeout_problem or retries_problem or patterns_problem,
    }


def retry_after(headers, attempt: int) -> float:
    """Honour Retry-After when it is a plain number, else back off."""
    raw = headers.get("Retry-After") if headers else None
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return BACKOFF * (2 ** attempt)


def post(conf: dict, payload: dict, opener=None, sleep=time.sleep) -> dict:
    """POST the payload, retrying the statuses the API says to retry.

    `opener` and `sleep` are injected so the suite can drive a throttled
    server and a doubling backoff without waiting for either.
    """
    payload, taken = redacted_payload(payload, conf.get("privacy", ()))
    if taken:
        sys.stderr.write(f"jev: redacted {taken} span(s) before sending\n")
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        conf["url"],
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {conf['key']}",
            "Content-Type": "application/json",
        },
    )
    send = opener or urllib.request.urlopen
    last = ""
    # The clock the ledger records: from the first send to the final outcome,
    # retries and their backoff included. That is what the caller waited for,
    # which is the number a per-stage latency column has to mean.
    started = time.monotonic()
    try:
        for attempt in range(conf["retries"] + 1):
            try:
                with send(request, timeout=conf["timeout"]) as response:
                    raw = response.read().decode("utf-8")
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        # An endpoint that answered with something that is not
                        # JSON answered. Leaving the note unset would let
                        # `cause_of` fall through to `unavailable` and report
                        # it as an outage, which is the one distinction this
                        # column exists to keep.
                        note(_cause="invalid")
                        raise
                    note(model=parsed.get("model"), **usage_of(parsed))
                    return parsed
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace").strip()[:400]
                exc.close()   # an HTTPError is an open response, not just an exception
                last = f"HTTP {exc.code}: {detail}" if detail else f"HTTP {exc.code}"
                note(_cause="unavailable")
                if exc.code == 401:
                    raise JevError(
                        "HTTP 401: the API key was rejected "
                        "(export TYPESAFE_API_KEY, or copy local-jev/.env.example to "
                        f"{system_tools_config.config_dir('jev') / '.env'})"
                    ) from exc
                if exc.code not in RETRY_STATUS or attempt == conf["retries"]:
                    raise JevError(last) from exc
                sleep(retry_after(exc.headers, attempt))
            except urllib.error.URLError as exc:
                last = f"{conf['url']}: {exc.reason}"
                # A timeout arrives wrapped, and it is the one failure class a
                # caller can act on differently from an endpoint that refused.
                note(_cause="timeout" if isinstance(exc.reason, TimeoutError)
                     else "unavailable")
                if attempt == conf["retries"]:
                    raise JevError(last) from exc
                sleep(BACKOFF * (2 ** attempt))
        raise JevError(last or "request failed")
    finally:
        note(duration_ms=int(round((time.monotonic() - started) * 1000)))


def answer_of(response: dict, qid: str) -> dict:
    answers = response.get("answers")
    if not isinstance(answers, dict) or qid not in answers:
        # A response arrived and carried no judgment. That is its own outcome
        # class: the endpoint is up, and what came back is unusable.
        note(_cause="invalid")
        raise JevError(f"no answer for {qid!r} in the response")
    return answers[qid]


# --- verbs ------------------------------------------------------------------

def emit(obj, out) -> None:
    json.dump(obj, out, indent=2, sort_keys=True)
    out.write("\n")


def cmd_ask(args, conf, out, **kw) -> int:
    questions = json.loads(read_source(args.questions))
    if not isinstance(questions, dict):
        raise JevError("--questions must hold a JSON object of question definitions")
    # A batch has one id and one answer per question and no single judgment, so
    # the row measures the request: how many questions it carried, what it
    # cost, how long it took and how it ended. The answers are the caller's to
    # read, and putting one of them in the row would say the batch was that.
    note(questions=len(questions))
    payload = build_payload(load_state(args.state, args.state_format), questions,
                            args.model or conf["model"])
    emit(post(conf, payload, **kw), out)
    return EXIT_OK


def one_question(args, conf, definition: dict, **kw) -> dict:
    note(questions=1)
    payload = build_payload(
        load_state(args.state, args.state_format),
        {args.id: definition},
        args.model or conf["model"],
    )
    return answer_of(post(conf, payload, **kw), args.id)


def cmd_noul(args, conf, out, **kw) -> int:
    answer = one_question(args, conf, {"type": "noul", "instructions": args.instructions}, **kw)
    # The judgment, and separately the bytes the caller read. They differ under
    # `--gate`, where the caller sees yes or no and the judgment is the
    # probability behind it; the ledger keeps the judgment and compares the
    # bytes against the caller's own answer.
    note(answer=judged(answer.get("noul")), confidence=confidence_of(answer))
    if args.json:
        emit(answer, out)
    elif args.gate is None:
        printed = f"{answer['noul']}"
        out.write(printed + "\n")
        note(_printed=printed)
    else:
        printed = "yes" if answer["noul"] >= args.gate else "no"
        out.write(printed + "\n")
        note(_printed=printed)
    return EXIT_OK


def cmd_choice(args, conf, out, **kw) -> int:
    definition = {
        "type": "choice",
        "instructions": args.instructions,
        "criteria": parse_mapping(args.criteria),
    }
    answer = one_question(args, conf, definition, **kw)
    # The position, never the key: see `position_of`.
    note(answer=position_of(answer.get("choice"), definition["criteria"]),
         confidence=confidence_of(answer))
    if args.json:
        emit(answer, out)
    elif args.unsure_below is not None and answer.get("confidence", 0.0) < args.unsure_below:
        # Confidence-gated routing: the caller asked to be told "I do not
        # know" rather than to be handed the top of a flat distribution.
        out.write("unsure\n")
        note(_printed="unsure")
    else:
        printed = f"{answer['choice']}"
        out.write(printed + "\n")
        note(_printed=printed)
    return EXIT_OK


def cmd_score(args, conf, out, **kw) -> int:
    definition = {
        "type": "score",
        "instructions": args.instructions,
        "criteria": parse_levels(args.levels),
    }
    answer = one_question(args, conf, definition, **kw)
    note(answer=judged(answer.get("score")), confidence=confidence_of(answer))
    if args.json:
        emit(answer, out)
    else:
        printed = f"{answer['score']}"
        out.write(printed + "\n")
        note(_printed=printed)
    return EXIT_OK


# The smallest question that still exercises the whole path: key, network,
# auth, model, and a parsed answer. A status that only tested for the env var
# could not tell "not configured here" from "up and broken", which is the one
# distinction local-health-check reads.
PROBE = {"type": "noul", "instructions": "Is this sentence written in English?"}


#: Everything outside this folder resolves `jev` from PATH -- the pack's
#: `sd-docs-lint` and `sd-review` both do. The siblings here call
#: `../local-jev/jev.sh` by path instead, so this script can be perfectly
#: healthy while every PATH consumer silently skips its Jev step.
#:
#: That is not hypothetical. On 2026-09-20 all eleven gates were set to 1, the
#: switch was on, the key worked and the probe answered 0.98 -- and
#: `sd-docs-lint` printed `rule 6 claim support: not run (jev is not on PATH)`,
#: because `local-bin-links install` had never run since the merge. `status`
#: reported ok throughout. A status that cannot see that is a status that lies.
#:
#: MISSING and DIFFERS are machine-setup's drift words, which the nightly job
#: counts by grepping for them.
def path_state(env):
    """How a PATH consumer resolves `jev`, as (marker, detail).

    An empty marker means it resolves to this very script. An env carrying no
    PATH at all is not a shell and has nothing to resolve, so it reports
    nothing rather than inventing a failure.
    """
    search = (env or {}).get("PATH")
    if not search:
        return "", ""
    found = shutil.which("jev", path=search)
    if not found:
        return ("MISSING",
                "jev is not on PATH; run local-bin-links/bin-links.sh install")
    mine = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "jev.sh"))
    theirs = os.path.realpath(found)
    if theirs != mine:
        return "DIFFERS", f"jev on PATH is {theirs}, not {mine}"
    return "", found


def cmd_status(args, conf, out, env=None, **kw) -> int:
    reason = unusable(conf, env)
    if reason:
        # 3 and not 1: a tool switched off on purpose, or never keyed here, is
        # not a broken tool, and local-health-check must stay quiet about it.
        out.write("jev: %s\n" % reason)
        if not conf["key"]:
            out.write("jev: %s\n" % system_tools_config.missing(
                "TYPESAFE_API_KEY", "jev", ".env", environ=env))
        return EXIT_UNCONFIGURED
    probe = dict(conf, timeout=min(conf["timeout"], 10.0), retries=0)
    try:
        response = post(probe, build_payload("This sentence is in English.",
                                             {ANSWER_KEY: PROBE}, conf["model"]), **kw)
        answer = answer_of(response, ANSWER_KEY)
    except JevError as exc:
        out.write(f"jev: {conf['url']} unreachable or refusing: {exc}\n")
        return EXIT_ERROR
    # Probed first, so a machine that cannot answer at all is never told
    # about PATH instead. 1 and not 3 when PATH is wrong: the switch is on
    # and the key works, so Jev is this machine's job and a consumer that
    # cannot find it is a broken one.
    #
    # The marker line goes FIRST, because local-health-check quotes a tool's
    # first line of output as the finding. Printing `ok` above it produced
    # `- local-jev: jev: ok ... probe=0.98` as the text of a failure, which
    # tells the reader the opposite of what happened.
    marker, detail = path_state(env)
    endpoint = (f"model={response.get('model', '?')}  url={conf['url']}  "
                f"probe={answer.get('noul')}  switch={flag_file(env)}")
    if marker:
        out.write(f"jev: PATH {marker} -- {detail}\n")
        out.write(f"jev: the endpoint itself answered  {endpoint}\n")
        return EXIT_ERROR
    out.write(f"jev: ok  {endpoint}\n")
    if detail:
        out.write(f"jev: path ok  {detail}\n")
    return EXIT_OK


# --- cli --------------------------------------------------------------------

#: The three options every judgment verb takes so its call can be found again
#: in the ledger. They change nothing a caller sees: a call with none of them
#: is still made, still answered and still recorded, under `unknown`.
#:
#: `--caller` and `--stage` also read `JEV_CALLER` and `JEV_STAGE`, because
#: half the callers on this machine are shell and reach this script through a
#: wrapper that already exports its own variables.
def add_measurement(parser) -> None:
    parser.add_argument("--caller", default=None, metavar="NAME",
                        help="which tool is asking, for the judgment ledger "
                             "(or JEV_CALLER)")
    parser.add_argument("--stage", default=None, metavar="NAME",
                        help="which decision this is, for the judgment ledger "
                             "(or JEV_STAGE)")
    parser.add_argument("--changed", choices=("yes", "no", "unknown"),
                        default=None,
                        help="did this judgment change what you did? Only a "
                             "caller that knows should say; --fallback answers "
                             "it on its own, and silence records `unknown`")
    parser.add_argument("--shadow", default=None, metavar="ANSWER",
                        help="shadow mode: ask anyway, record both answers, "
                             "and print this one. The stage's behaviour does "
                             "not change and a paired sample is produced. Off "
                             "unless given, because it costs a real call")
    parser.add_argument("--shadow-ms", type=int, default=None, dest="shadow_ms",
                        metavar="N",
                        help="how long your own path took, for the paired row")


def add_common(parser) -> None:
    parser.add_argument("--state", default="-",
                        help="file holding the state, or - for stdin (default)")
    parser.add_argument("--state-format", choices=("text", "json"), default="text")
    parser.add_argument("--model", default=None)
    parser.add_argument("--id", default=ANSWER_KEY,
                        help="question id, which code sees and the model does not")
    parser.add_argument("--json", action="store_true",
                        help="print the whole answer, including the distribution")
    parser.add_argument("--fallback", default=None, metavar="ANSWER",
                        help="print this and exit 0 when Jev is off, unkeyed "
                             "or failing; the reason goes to stderr")
    add_measurement(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev.sh", add_help=False)
    subs = parser.add_subparsers(dest="verb", required=True)

    ask = subs.add_parser("ask", add_help=False)
    ask.add_argument("--questions", required=True,
                     help="file holding a JSON object of question definitions, or -")
    ask.add_argument("--state", default="-")
    ask.add_argument("--state-format", choices=("text", "json"), default="text")
    ask.add_argument("--model", default=None)
    # `ask` takes `--fallback` like every other verb, and did not until a
    # caller tried to use it. It is the one verb built for a batch, so it is
    # the one a caller reaches for when a lane has many questions and can
    # afford none of them taking the lane down -- exactly the case the
    # fallback exists for. It does not take `--id` (the ids are the keys in
    # `--questions`), `--json` (it prints the whole response either way) or
    # `--gate` (there is no single answer to gate), so it spells out the one
    # option it shares rather than calling `add_common`.
    ask.add_argument("--fallback", default=None, metavar="ANSWER",
                     help="print this and exit 0 when Jev is off, unkeyed "
                          "or failing; the reason goes to stderr")
    add_measurement(ask)
    ask.set_defaults(run=cmd_ask)

    noul = subs.add_parser("noul", add_help=False)
    noul.add_argument("instructions")
    noul.add_argument("--gate", type=float, default=None,
                      help="print yes/no against this probability instead of the number")
    add_common(noul)
    noul.set_defaults(run=cmd_noul)

    choice = subs.add_parser("choice", add_help=False)
    choice.add_argument("instructions")
    choice.add_argument("--criteria", required=True,
                        help="name[=description],... or @file.json")
    choice.add_argument("--unsure-below", type=float, default=None,
                        help="print `unsure` when confidence falls under this")
    add_common(choice)
    choice.set_defaults(run=cmd_choice)

    score = subs.add_parser("score", add_help=False)
    score.add_argument("instructions")
    score.add_argument("--levels", required=True,
                       help="ordered level descriptions, low first, or @file.json")
    add_common(score)
    score.set_defaults(run=cmd_score)

    status = subs.add_parser("status", add_help=False)
    status.set_defaults(run=cmd_status)

    enabled = subs.add_parser("enabled", add_help=False)
    enabled.add_argument("stage", nargs="?", default="",
                         help="a caller's own variable, e.g. JEV_ADVERSARIAL_GATE")
    enabled.add_argument("--why", action="store_true",
                         help="say which way it went, instead of only exiting")
    enabled.add_argument("--record", action="store_true",
                         help="write one decline event when the answer is no, "
                              "so the old path that is about to run is counted. "
                              "Needs a STAGE. Off unless asked: this verb costs "
                              "nothing and every caller asks it on every run")
    enabled.add_argument("--caller", default=None, metavar="NAME",
                         help="which tool is asking, for a recorded decline "
                              "(or JEV_CALLER)")
    enabled.set_defaults(run=cmd_enabled)

    # `record` says what the caller's own mechanism did. It is local because
    # it sends nothing: the whole verb is one row about work that happened
    # somewhere else, in the one schema both arms share.
    rec = subs.add_parser("record", add_help=False)
    rec.add_argument("--caller", default=None, metavar="NAME")
    rec.add_argument("--stage", default=None, metavar="NAME")
    rec.add_argument("--arm", choices=("jev", "baseline"), default="baseline",
                     help="which mechanism answered; the old one by default")
    rec.add_argument("--provider", default=BASELINE_PROVIDER, metavar="NAME")
    rec.add_argument("--primitive", default="baseline", metavar="NAME")
    rec.add_argument("--outcome", choices=("ok", "timeout", "fallback",
                                           "unavailable", "invalid"),
                     default="fallback")
    rec.add_argument("--decline", choices=DECLINES, default=None,
                     help="why the judgment was not used")
    rec.add_argument("--answer", default=None, metavar="NUMBER",
                     help="what your mechanism answered, as a number: a "
                          "probability, a score, or which of your own "
                          "criteria won, counting from 1. The ledger refuses "
                          "anything else, because a label is text you wrote "
                          "and can be a path or a subject")
    rec.add_argument("--positions", default=None, metavar="3,1,2",
                     help="the ordering you produced, as positions into your "
                          "own input. Positions only; the ledger refuses "
                          "anything that is not whole numbers and commas")
    rec.add_argument("--duration-ms", type=int, default=None, dest="duration_ms")
    rec.add_argument("--pair", default=None, metavar="ID",
                     help="ties this row to the other arm of the same decision")
    rec.add_argument("--shadow", action="store_true")
    rec.add_argument("--changed", choices=("yes", "no", "unknown"), default=None)
    rec.add_argument("--why", action="store_true",
                     help="say on stderr what the recorder did")
    rec.set_defaults(run=cmd_record)

    subs.add_parser("on", add_help=False).set_defaults(run=set_flag, word="on")
    subs.add_parser("off", add_help=False).set_defaults(run=set_flag, word="off")
    return parser


def degrade(reason: str, fallback, out) -> int:
    """Hand back the caller's own answer, loudly, and do not fail the job.

    Silence here would be the worse bug: a lane that quietly stops running is
    exactly what this repo has been bitten by before. So the reason always
    reaches stderr, whether or not there is a fallback to print.
    """
    if fallback is None:
        sys.stderr.write(f"jev: {reason}\n")
        return EXIT_UNCONFIGURED
    sys.stderr.write(f"jev: {reason}; using the fallback\n")
    out.write(f"{fallback}\n")
    return EXIT_OK


# The verbs that answer about the tool rather than through it. They run with
# no key and with the switch off, or `jev enabled` could not be asked on the
# machine where the answer matters most.
LOCAL_VERBS = (cmd_enabled, set_flag, cmd_status, cmd_record)


def main(argv=None, out=None, env=None, **kw) -> int:
    out = sys.stdout if out is None else out
    env = os.environ if env is None else env
    global _STDIN_SPENT
    _STDIN_SPENT = False
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    conf = settings(env)
    fallback = getattr(args, "fallback", None)
    if args.run in LOCAL_VERBS:
        return args.run(args, conf, out, env=env, **kw)
    shadow = getattr(args, "shadow", None)
    if shadow is not None and getattr(args, "json", False):
        sys.stderr.write("jev: --shadow prints your one answer, so it cannot "
                         "be combined with --json\n")
        return EXIT_ERROR
    # Measured here and flushed below, always after the answer has been
    # written. A row is bookkeeping and an answer is the job, so the job goes
    # first and the bookkeeping never delays it.
    measure(args, env)
    pair = os.urandom(8).hex() if shadow is not None else None
    note(_declared=getattr(args, "changed", None), pair=pair,
         shadow=shadow is not None)
    # In shadow mode the judgment is measured and not used, so it is written
    # to a sink and the caller is handed back its own answer instead.
    sink = io.StringIO() if shadow is not None else out
    word, reason = why_unusable(conf, env)
    if reason:
        # Nothing was sent: switched off here, unkeyed, or configured with a
        # number that does not parse. The row says which, and says it as a
        # fallback when the caller handed one over.
        code, outcome, cause = degrade(reason, fallback, sink), "unavailable", word
    else:
        outcome, cause = "ok", None
        try:
            code = args.run(args, conf, sink, **kw)
        except (JevError, OSError, json.JSONDecodeError) as exc:
            outcome = cause = cause_of(exc)
            if fallback is not None:
                code = degrade(str(exc), fallback, sink)
            else:
                sys.stderr.write(f"jev: {exc}\n")
                code = EXIT_ERROR
    if shadow is not None:
        # The caller's own answer, always, and exit 0. A stage in shadow mode
        # changes no behaviour, and that has to hold on the run where the call
        # it also made failed, or shadow mode is a new way to break a caller.
        out.write(f"{shadow}\n")
        code = EXIT_OK
    flush(outcome, cause=cause, fallback=fallback, baseline=shadow, env=env)
    if shadow is not None:
        # The control arm of the pair, from this process, because a pair whose
        # other half is written by somebody else is a pair that goes missing.
        write_event({
            "caller": whose(args, env, "caller"),
            "stage": whose(args, env, "stage"),
            "arm": "baseline",
            "pair": named(pair),
            "shadow": True,
            "provider": BASELINE_PROVIDER,
            "primitive": "baseline",
            "outcome": "ok",
            # The caller's own answer is the caller's own text -- `no`, a
            # folder name, a subject line -- so it is not stored, and
            # `judged` drops it if it is not a number. What the comparison
            # needs is already here: `changed` says whether the two arms
            # differed, computed in this process where both were in hand.
            "answer": judged(shadow),
            "duration_ms": getattr(args, "shadow_ms", None),
            # The baseline is what happened, so relative to itself it changed
            # nothing. The delta is on the judgment's row.
            "changed": "no",
        }, env)
    return code


if __name__ == "__main__":
    sys.exit(main())
