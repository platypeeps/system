"""The judgment ledger: one row per decision a judgment model was asked to
make, and one per decision the mechanism it replaced made instead.

Every caller on this machine that routes a decision through a judgment model
keeps its old mechanism as a fallback. Until this table existed, nothing said
how often the fallback ran, what the calls cost, how long a caller waited, or
which stage spent the most. The response carried token counts and they were
read and dropped.

**Both arms, one table, one stage key.** A row for the model alone cannot
answer whether it helped. `arm` says which mechanism answered, `pair` ties the
two halves of one decision together, and both halves carry the same `stage`,
so a fallback is countable against a judgment rather than merely present.

**The vocabulary is provider-neutral.** `provider`, `model` and `primitive`
are required fields whose values are free strings, so a second judgment model
records here without a migration and no value in the table names a vendor.

**Identifiers and counts, and nothing that was submitted.** No column holds
prompt text, state text, a path, a subject or a body. Two could carry content
by accident, and both are shaped rather than merely capped, because a cap is
not a guarantee: anything short enough passes one, and `/Users/someone/private.txt`
is short. `answer` is a **number** -- a probability, a score, or a position
into the caller's own criteria -- and `NUMBER` shapes it. `ordering` is
positions into the caller's own input, and `POSITIONS` shapes it: digits and
commas, nothing else, so the things at those positions can never arrive here.
Both are **refused**, not truncated: a truncated value is a prefix of content,
stored, which is the thing the table promises not to do. Losing one row of
metering is the cheaper mistake, and the refusal names what it found.

A criterion key is caller-authored text, so it is never stored. `jev choice`
records the position of the key that won, counting from 1 into the criteria
the caller passed; the caller already has the list, so a position is the same
fact without the string.

**Outcome and cause are two questions.** `outcome` says how it ended: `ok`,
`timeout`, `unavailable`, `invalid`, or `fallback` when the old mechanism's
answer was used instead of a judgment. `fallback` wins over the other four
when it happens, because the fact a reader wants first is that the old path
ran -- and `cause` then keeps why, as one of seven values and not as a
boolean, so the per-stage report can say whether a stage's fallbacks come from
timeouts, from an unkeyed machine, or from a `jev` nobody linked onto PATH.

**`changed` is `unknown` until a caller can prove otherwise.** Only a caller
that also hands over what it would have done can be compared against; the rest
say `unknown` rather than guess. Nothing here infers it.
"""

from __future__ import annotations

import json
import re
import sqlite3

from .database import transaction
from .errors import SdDbError
from .writes import now as _now
from .writes import stamp

__all__ = [
    "ARMS",
    "CAUSES",
    "CHANGED",
    "JudgmentRefused",
    "MAX_ANSWER",
    "MAX_NAME",
    "NUMBER",
    "MAX_ORDERING",
    "OUTCOMES",
    "by_stage",
    "document",
    "json_text",
    "record",
    "text",
]

#: Which mechanism answered. `jev` is the judgment model's arm, whatever the
#: vendor; `baseline` is whatever the caller did before it, and does after it
#: when the model declines.
ARMS = ("jev", "baseline")

#: How it ended. The five classes, in one column.
OUTCOMES = ("ok", "timeout", "fallback", "unavailable", "invalid")

#: Why the model was not used. A value and not a boolean, because the six
#: reasons need different repairs: a switch is flipped, a key is exported, a
#: link is made, a timeout is tuned, an answer is a defect, and a budget is
#: raised. `unavailable` is the seventh and the catch-all -- the endpoint
#: refused or could not be reached.
CAUSES = ("switched-off", "unkeyed", "no-path", "timeout", "invalid",
          "unavailable", "budget")

#: Whether this changed what the caller did.
CHANGED = ("yes", "no", "unknown")

#: The privacy backstop, and a length is not one. A cap keeps a paragraph out
#: and lets `/Users/someone/private.txt` straight in, which is what the first
#: review of this table found: any value short enough passed, and a criterion
#: key is caller-authored text that can be a path, a subject or a name.
#:
#: So an answer is a **number**, in the shape `NUMBER` below: a probability,
#: a score, or a position into the caller's own criteria. The length cap
#: stays as a second wall -- a number that long is a mistake either way --
#: but the shape is the guarantee, the same one `ordering` already had.
MAX_ANSWER = 64

#: What an answer may look like: a whole number or a decimal, optionally
#: negative. `0.87`, `3`, `-1`. Nothing that matches this can be content.
NUMBER = re.compile(r"^-?\d+(\.\d+)?$")

#: The same backstop for the identifiers. A caller, a stage, a model name, a
#: pair id and a question id are names, not text.
MAX_NAME = 96

#: What a name the caller chooses may look like. A cap and a newline check let
#: `--stage /Users/someone/private.txt` through, and a short subject with it: both
#: are strings and both are short. The shape is the guarantee here as it is for
#: an ordering and an answer -- a value that matches this is an identifier and
#: cannot be a path, a sentence or a body.
#:
#: Provider and model are exempt on purpose. They carry names this repository
#: does not choose (`anthropic/claude-opus-5`), they come from a vendor and not
#: from a subject line, and constraining them would refuse a real model rather
#: than catch a leak.
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

#: And for an ordering, which is positions and not things. Long enough for the
#: longest list any caller reorders today (`sd_jev` caps its own input at 40)
#: with room to spare, and short enough that a body cannot hide in it.
MAX_ORDERING = 512

#: An ordering is whole numbers separated by commas, and nothing else. The
#: shape is the guarantee: a value that matches this cannot be content.
POSITIONS = re.compile(r"^\d+(,\d+)*$")


class JudgmentRefused(SdDbError):
    """A row the ledger will not write, naming the field and what it found."""


def _name(field: str, value: object, *, required: bool) -> str | None:
    if value is None or value == "":
        if required:
            raise JudgmentRefused(f"{field} is required and was {value!r}")
        return None
    if not isinstance(value, str):
        raise JudgmentRefused(f"{field} must be a string; got {value!r}")
    if len(value) > MAX_NAME:
        raise JudgmentRefused(
            f"{field} is {len(value)} characters and the ledger caps a name at "
            f"{MAX_NAME}; this table holds identifiers, never submitted content"
        )
    if "\n" in value or "\r" in value:
        raise JudgmentRefused(
            f"{field} carries a newline; this table holds identifiers, never "
            f"submitted content"
        )
    return value


def _identifier(field: str, value: object, *, required: bool) -> str | None:
    """A name the caller chose, held to the identifier grammar.

    `_name` first, so the length and the newline are reported the same way
    everywhere, and then the shape.
    """
    value = _name(field, value, required=required)
    if value is None:
        return None
    if not IDENTIFIER.match(value):
        raise JudgmentRefused(
            f"{field} must be an identifier -- letters, digits and `.`, `_`, "
            f"`:` or `-`, starting with a letter or a digit; got {value!r}. "
            f"A path, a subject line or a prompt is submitted content, and "
            f"this table never stores it"
        )
    return value


def _count(field: str, value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise JudgmentRefused(
            f"{field} must be a whole number of zero or more; got {value!r}"
        )
    return value


def _fraction(field: str, value: object) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not 0.0 <= value <= 1.0:
        raise JudgmentRefused(f"{field} must be a number from 0 to 1; got {value!r}")
    return float(value)


def _ordering(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise JudgmentRefused(f"ordering must be a string; got {value!r}")
    if len(value) > MAX_ORDERING:
        raise JudgmentRefused(
            f"ordering is {len(value)} characters and the ledger caps one at "
            f"{MAX_ORDERING}; an ordering is positions into the caller's own "
            f"input, never the things at those positions"
        )
    if not POSITIONS.match(value):
        raise JudgmentRefused(
            f"ordering must be whole numbers separated by commas, e.g. `3,1,2`; "
            f"got {value!r}. The shape is the guarantee: this table never "
            f"stores submitted content"
        )
    return value


def _answer(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise JudgmentRefused(f"answer must be a string; got {value!r}")
    if len(value) > MAX_ANSWER:
        raise JudgmentRefused(
            f"answer is {len(value)} characters and the ledger caps one at "
            f"{MAX_ANSWER}; a judgment is a probability, a score or a position, "
            f"and this table never stores submitted content"
        )
    if not NUMBER.match(value):
        raise JudgmentRefused(
            f"answer must be a number -- a probability, a score, or a position "
            f"into your own criteria; got {value!r}. A criterion key is text "
            f"you wrote, so it can be a path or a subject, and this table "
            f"never stores submitted content. Record which one won, not what "
            f"it was called."
        )
    return value


def record(
    connection: sqlite3.Connection,
    *,
    caller: str,
    stage: str,
    provider: str,
    primitive: str,
    outcome: str,
    arm: str = "jev",
    pair: str | None = None,
    shadow: bool = False,
    model: str | None = None,
    question_id: str | None = None,
    questions: int | None = None,
    cause: str | None = None,
    answer: str | None = None,
    confidence: float | None = None,
    ordering: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    duration_ms: int | None = None,
    usd: float | None = None,
    changed: str = "unknown",
    now: str | None = None,
) -> int:
    """Write one row and return its id.

    Every refusal comes before the transaction, so a refused call writes
    nothing. `now` is the moment the decision was made, in any aware ISO-8601
    shape; the clock when omitted.
    """
    caller = _identifier("caller", caller, required=True)
    stage = _identifier("stage", stage, required=True)
    provider = _name("provider", provider, required=True)
    primitive = _identifier("primitive", primitive, required=True)
    model = _name("model", model, required=False)
    question_id = _identifier("question_id", question_id, required=False)
    pair = _identifier("pair", pair, required=False)
    if arm not in ARMS:
        raise JudgmentRefused(f"arm must be one of {', '.join(ARMS)}; got {arm!r}")
    if outcome not in OUTCOMES:
        raise JudgmentRefused(
            f"outcome must be one of {', '.join(OUTCOMES)}; got {outcome!r}"
        )
    if cause is not None and cause not in CAUSES:
        raise JudgmentRefused(
            f"cause must be null or one of {', '.join(CAUSES)}; got {cause!r}"
        )
    if changed not in CHANGED:
        raise JudgmentRefused(
            f"changed must be one of {', '.join(CHANGED)}; got {changed!r}"
        )
    answer = _answer(answer)
    ordering = _ordering(ordering)
    confidence = _fraction("confidence", confidence)
    tokens_in = _count("tokens_in", tokens_in)
    tokens_out = _count("tokens_out", tokens_out)
    duration_ms = _count("duration_ms", duration_ms)
    questions = _count("questions", questions)
    if usd is not None and (type(usd) not in (int, float) or usd < 0):
        raise JudgmentRefused(f"usd must be a number of zero or more; got {usd!r}")
    moment = _now() if now is None else stamp(now)
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO judgment (timestamp, caller, stage, arm, pair, shadow, "
            "provider, model, primitive, question_id, questions, outcome, cause, "
            "answer, confidence, ordering, tokens_in, tokens_out, duration_ms, "
            "usd, changed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (moment, caller, stage, arm, pair, 1 if shadow else 0, provider,
             model, primitive, question_id, questions, outcome, cause, answer,
             confidence, ordering, tokens_in, tokens_out, duration_ms,
             None if usd is None else float(usd), changed),
        )
    return int(cursor.lastrowid)


#: The primitive a gate event carries: one caller asking, before any decision,
#: whether a judgment is available at all. It is not a decision, and every
#: query below that counts decisions excludes it.
#:
#: A caller that declines at the gate and then records what its own mechanism
#: did writes two rows for the one decision. Counting both made a single
#: decline read as two calls and two declines, with nothing tying them
#: together. `GATE_PRIMITIVE` is the join that was missing: the gate row says
#: the old path is about to run, the completed row says what it did, and only
#: the second is a decision. A stage whose callers only ever decline still has
#: a number, because `GATES` counts these on their own.
GATE_PRIMITIVE = "gate"

#: One row per stage and arm: how many decisions it made, how they ended, what
#: they cost, how long the caller waited, and how many are known to have
#: changed anything. Gate events are not decisions and are left out.
#:
#: `avg` and `max` rather than a percentile, because SQLite has no percentile
#: and a median computed in Python over a year of rows is a read this report
#: does not need to be.
BY_STAGE_ARM = """
SELECT stage, arm,
       COUNT(*)                                          AS calls,
       SUM(outcome = 'ok')                               AS ok,
       SUM(outcome = 'fallback')                         AS fallbacks,
       SUM(shadow)                                       AS shadow,
       SUM(changed = 'yes')                              AS changed_yes,
       SUM(changed = 'no')                               AS changed_no,
       SUM(changed = 'unknown')                          AS changed_unknown,
       SUM(override IS NOT NULL)                         AS overrides,
       COALESCE(SUM(tokens_in), 0)                       AS tokens_in,
       COALESCE(SUM(tokens_out), 0)                      AS tokens_out,
       SUM(usd)                                          AS usd,
       AVG(duration_ms)                                  AS avg_ms,
       MAX(duration_ms)                                  AS max_ms
FROM judgment
WHERE primitive <> :gate
  AND (:since IS NULL OR timestamp >= :since)
  AND (:until IS NULL OR timestamp < :until)
GROUP BY stage, arm
ORDER BY stage, arm
"""

#: Why the model was not used, per stage: the decline reasons of both arms
#: counted separately, so a stage's fallbacks name their cause.
DECLINES = """
SELECT stage, cause, COUNT(*) AS n
FROM judgment
WHERE cause IS NOT NULL
  AND primitive <> :gate
  AND (:since IS NULL OR timestamp >= :since)
  AND (:until IS NULL OR timestamp < :until)
GROUP BY stage, cause
ORDER BY stage, cause
"""

#: How often each stage never got as far as a decision, and why. These are the
#: gate events `BY_STAGE_ARM` and `DECLINES` leave out, counted here instead,
#: so a stage whose callers only decline is still visible and no decision is
#: counted twice.
GATES = """
SELECT stage, cause, COUNT(*) AS n
FROM judgment
WHERE primitive = :gate
  AND (:since IS NULL OR timestamp >= :since)
  AND (:until IS NULL OR timestamp < :until)
GROUP BY stage, cause
ORDER BY stage, cause
"""

#: A paired sample is one `pair` id carrying both arms. A stage with none has
#: no delta yet, whatever else its rows say, and the report names it.
PAIRS = """
SELECT stage, COUNT(*) AS paired FROM (
  SELECT stage, pair
  FROM judgment
  WHERE pair IS NOT NULL
    AND primitive <> :gate
    AND (:since IS NULL OR timestamp >= :since)
    AND (:until IS NULL OR timestamp < :until)
  GROUP BY stage, pair
  HAVING COUNT(DISTINCT arm) = 2
)
GROUP BY stage
"""


def present(connection: sqlite3.Connection) -> bool:
    """Whether migration 011 has run on the database behind this connection."""
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'judgment'"
    ).fetchone() is not None


def by_stage(
    connection: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """The per-stage comparison: both arms side by side, one entry per stage.

    `since` and `until` are compared as text, which is what the one timestamp
    shape buys: `--since 2026-09` selects a month and `--since 2026-09-20` a
    day, with no date arithmetic in the query. The oldest bound is inclusive
    and the newest exclusive.

    A reader opens a database it may not migrate, and one at schema 10 has no
    `judgment` table. The empty list is that database's honest answer -- no
    judgment has been recorded on it -- and it is the answer a read-only open
    promises. Raising `no such table` instead would make every reader carry
    this check.
    """
    if not present(connection):
        return []
    bounds = {"since": since, "until": until, "gate": GATE_PRIMITIVE}

    def entry_for(stage: str) -> dict:
        return stages.setdefault(
            stage,
            {"stage": stage, "arms": {}, "declines": {}, "gates": {},
             "paired": 0},
        )

    stages: dict[str, dict] = {}
    for row in connection.execute(BY_STAGE_ARM, bounds):
        arm = dict(row)
        entry = entry_for(arm.pop("stage"))
        entry["arms"][arm.pop("arm")] = arm
    for row in connection.execute(DECLINES, bounds):
        if row["stage"] in stages:
            stages[row["stage"]]["declines"][row["cause"]] = row["n"]
    # A gate event makes its stage appear even when no decision on it was ever
    # recorded, which is the common case: most callers decline and stop.
    for row in connection.execute(GATES, bounds):
        entry_for(row["stage"])["gates"][row["cause"]] = row["n"]
    for row in connection.execute(PAIRS, bounds):
        if row["stage"] in stages:
            stages[row["stage"]]["paired"] = row["paired"]
    return [stages[name] for name in sorted(stages)]


def unpaired(stages: list[dict]) -> list[str]:
    """The stages with no paired sample yet, named rather than left to be
    noticed. These are the stages where no delta can be computed: the arms
    have run, but never on the same decision."""
    return [entry["stage"] for entry in stages if not entry["paired"]]


def _rate(part, whole) -> str:
    return "—" if not whole else f"{100.0 * (part or 0) / whole:.0f}%"


def _ms(value) -> str:
    return "—" if value is None else f"{float(value):.0f}ms"


def _money(value) -> str:
    return "—" if value is None else f"${float(value):,.4f}"


def _arm_line(label: str, arm: dict | None) -> str:
    if arm is None:
        return f"    {label}: no runs"
    return (
        f"    {label}: {arm['calls']} run(s), {arm['ok']} ok, "
        f"{arm['fallbacks']} fallback ({_rate(arm['fallbacks'], arm['calls'])}), "
        f"latency {_ms(arm['avg_ms'])} avg / {_ms(arm['max_ms'])} max, "
        f"tokens {arm['tokens_in']} in / {arm['tokens_out']} out, "
        f"cost {_money(arm['usd'])}"
    )


def text(stages: list[dict]) -> str:
    """The comparison as a person reads it, one block per stage."""
    if not stages:
        return "judgments: no calls recorded\n"
    lines = ["judgments by stage:"]
    for entry in stages:
        jev = entry["arms"].get("jev")
        baseline = entry["arms"].get("baseline")
        paired = entry["paired"]
        lines.append(
            f"  {entry['stage']}: {paired} paired sample(s)"
            + ("" if paired else " — no delta yet")
        )
        lines.append(_arm_line("jev     ", jev))
        lines.append(_arm_line("baseline", baseline))
        if entry["declines"]:
            named = ", ".join(f"{n} {cause}"
                              for cause, n in sorted(entry["declines"].items()))
            lines.append(f"    declines: {named}")
        if entry["gates"]:
            named = ", ".join(f"{n} {cause}"
                              for cause, n in sorted(entry["gates"].items()))
            lines.append(f"    never reached a decision: {named}")
        counted = {key: sum(arm.get(key) or 0 for arm in entry["arms"].values())
                   for key in ("changed_yes", "changed_no", "changed_unknown",
                               "overrides", "shadow")}
        lines.append(
            f"    changed behaviour: {counted['changed_yes']} yes, "
            f"{counted['changed_no']} no, {counted['changed_unknown']} unknown; "
            f"{counted['overrides']} override(s); {counted['shadow']} shadow run(s)"
        )
    missing = unpaired(stages)
    if missing:
        lines.append(
            "no paired sample yet, so no delta: " + ", ".join(missing)
        )
    return "\n".join(lines) + "\n"


def document(stages: list[dict]) -> dict:
    """The comparison as JSON, the one serialisation a screen would read."""
    return {"stages": stages, "unpaired": unpaired(stages)}


def json_text(stages: list[dict]) -> str:
    return json.dumps(document(stages), indent=2, sort_keys=True) + "\n"
