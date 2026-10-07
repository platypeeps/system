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

**A label is a later answer, in the answer's own shape.** Reported confidence
is not accuracy, so a row can carry `override`: what an authoritative later
source says the answer should have been, a number like `answer`. A row is
right when the two are equal. `label` is the one write path for it, and it
holds the label to the same shapes a row is held to: a number, and a source
that is an identifier naming the rule that produced it (sd:2107).

**`location` is the caller's directory, and the one field a bad value does
not refuse.** It says where the call came from, beside `caller`, which says
who asked: the git toplevel of the caller's working directory, else that
directory. It is a repository key in the sense of `paths`, as
`skill_use.cwd` is: a path under `$HOME` is stored `~/`-relative, and any
other path stays absolute. A value over `MAX_LOCATION` characters, or one
with a character `str.isprintable` rejects, is stored as NULL and the row is
kept: a location is a convenience for the reader, not a fact the metering
depends on (sd:2950).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from decimal import Decimal

from . import paths
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
    "MAX_DISTRIBUTION",
    "MAX_LOCATION",
    "MAX_OPTIONS",
    "MAX_ORDERING",
    "OUTCOMES",
    "by_stage",
    "compare",
    "compare_json",
    "compare_text",
    "document",
    "json_text",
    "label",
    "record",
    "text",
    "unlabelled",
]

#: Which mechanism answered. `jev` is the judgment model's arm, whatever the
#: vendor; `baseline` is whatever the caller did before it, and does after it
#: when the model declines. `kev` and `haiku` are the comparison arms of
#: sd:2366: a local System One server and a frontier model behind a prompt
#: adapter, asked the same request beside every live call. They only record;
#: neither answer is ever used. They are roles, not vendors: the transport or
#: host is the row's `provider` and the checkpoint its `model`.
ARMS = ("jev", "baseline", "kev", "haiku")

#: The two arms the per-stage report compares. The comparison arms have
#: their own report, `compare`, so a stage's call counts and paired samples
#: keep the meaning they had before those arms existed.
DECISION_ARMS = ("jev", "baseline")

#: The arms `compare` reads: the model whose answer is used, and the two that
#: are asked beside it.
MODEL_ARMS = ("jev", "kev", "haiku")

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

#: A distribution: numbers in the caller's option order, separated by commas.
#: The keys are never stored, for the reason `answer` holds a position.
DISTRIBUTION = re.compile(r"^\d+(\.\d+)?(,\d+(\.\d+)?)*$")

#: The most options one question may have: Kev's API limit, and so the most
#: values a distribution carries. It is counted, not inferred from a length.
MAX_OPTIONS = 255

#: Room for `MAX_OPTIONS` values of up to 15 characters each, plus commas.
#: Sized from the option count; `MAX_ORDERING` is a different thing's bound.
MAX_DISTRIBUTION = MAX_OPTIONS * 16

#: The longest location `record` keeps. A longer one is stored as NULL.
MAX_LOCATION = 255


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


def _probabilities(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise JudgmentRefused(f"probabilities must be a string; got {value!r}")
    if len(value) > MAX_DISTRIBUTION:
        raise JudgmentRefused(
            f"probabilities is {len(value)} characters and the ledger caps it at "
            f"{MAX_DISTRIBUTION}")
    if value.count(",") + 1 > MAX_OPTIONS:
        raise JudgmentRefused(
            f"probabilities has {value.count(',') + 1} values and a question has "
            f"at most {MAX_OPTIONS} options")
    if not DISTRIBUTION.match(value):
        raise JudgmentRefused(
            f"probabilities must be numbers separated by commas, in option "
            f"order, e.g. `0.47,0.28,0.25`; got {value!r}. The option keys are "
            f"text the caller wrote, and this table never stores them")
    # The shape admits a 4,000-digit integer, which `compare` would read as
    # infinity and `compare --json` would print as `Infinity`.
    if any(not 0.0 <= float(part) <= 1.0 for part in value.split(",")):
        raise JudgmentRefused(
            f"each probability must be a number from 0 to 1; got {value[:40]!r}")
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


def _location(value: object) -> str | None:
    """The caller's directory as a `paths` key, or None for a value the
    ledger does not keep: not a string, empty, over `MAX_LOCATION`, or
    carrying a control character. Never refuses; a bad location costs the
    row its location, not the row."""
    if not isinstance(value, str) or not value or len(value) > MAX_LOCATION \
            or not value.isprintable():
        return None
    try:
        value = paths.key(value)
    except paths.PathRefused:        # no usable $HOME: keep it as given
        return value
    except (OSError, RuntimeError):  # resolving failed: the row, not the path
        return None
    return value if len(value) <= MAX_LOCATION else None


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
    server_ms: int | None = None,
    probabilities: str | None = None,
    location: str | None = None,
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
    server_ms = _count("server_ms", server_ms)
    probabilities = _probabilities(probabilities)
    location = _location(location)
    if usd is not None and (type(usd) not in (int, float) or not math.isfinite(usd)
                            or usd < 0):
        raise JudgmentRefused(f"usd must be a finite number of zero or more; got {usd!r}")
    if usd is None:
        usd = registered_usd(connection, provider, model, tokens_in, tokens_out)
    moment = _now() if now is None else stamp(now)
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO judgment (timestamp, caller, stage, arm, pair, shadow, "
            "provider, model, primitive, question_id, questions, outcome, cause, "
            "answer, confidence, ordering, tokens_in, tokens_out, duration_ms, "
            "usd, changed, server_ms, probabilities, location) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (moment, caller, stage, arm, pair, 1 if shadow else 0, provider,
             model, primitive, question_id, questions, outcome, cause, answer,
             confidence, ordering, tokens_in, tokens_out, duration_ms,
             None if usd is None else float(usd), changed, server_ms,
             probabilities, location),
        )
    return int(cursor.lastrowid)


def registered_usd(
    connection: sqlite3.Connection,
    provider: str,
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
) -> float | None:
    """What a row's tokens cost at the price `providers.yaml` gives its
    provider, per million tokens; None when there is no such price.

    The registry is the file beside the connection's database, read alone:
    no seeding write, so a row needs no `provider` row. An entry that names a
    model prices that model only; a row for another model gets no cost, as a
    comparison arm's Haiku price is only Haiku's. A side with tokens and no
    usable price leaves the whole row unpriced rather than half-priced. A
    missing, unreadable or refused registry is no price, never a refused row:
    the tokens are the record either way.
    """
    if tokens_in is None and tokens_out is None:
        return None
    from .calls import _price
    from .registry import beside, read as read_registry
    try:
        entry = read_registry(beside(connection)).providers.get(provider)
    except Exception:          # a price list may not cost the ledger its row
        return None
    if entry is None or (entry.model is not None and entry.model != model):
        return None
    total = Decimal(0)
    for tokens, side in ((tokens_in, "in"), (tokens_out, "out")):
        if not tokens:
            continue
        price = _price(entry, side)
        if price is None:
            return None
        total += tokens * Decimal(repr(price))
    usd = float(total / 1_000_000)
    return usd if math.isfinite(usd) else None


def label(
    connection: sqlite3.Connection,
    row_id: int,
    override: object,
    source: object,
    *,
    replace: bool = False,
    now: str | None = None,
) -> bool:
    """Write the later, authoritative answer for one row, and say whether
    anything changed.

    The one write path for a label. `override` is held to the answer's shape
    and `source` to the identifier grammar, before anything is read. The same
    value again is a no-op that returns False and keeps the first moment; a
    different value is refused unless `replace` says to overwrite it, because
    two rules disagreeing about one row is a finding, not a last-writer-wins.
    """
    if type(row_id) is not int or row_id < 1:
        raise JudgmentRefused(f"row must be a whole number of one or more; got {row_id!r}")
    if override is None or override == "":
        raise JudgmentRefused("override is required: the answer the later source gave")
    override = _answer(override)
    source = _identifier("source", source, required=True)
    moment = _now() if now is None else stamp(now)
    with transaction(connection):
        row = connection.execute(
            "SELECT arm, primitive, answer, override FROM judgment WHERE id = ?", (row_id,)
        ).fetchone()
        if row is None:
            raise JudgmentRefused(f"no judgment row {row_id}")
        if row["arm"] not in DECISION_ARMS:
            raise JudgmentRefused(
                f"row {row_id} is a {row['arm']} comparison row; a pair's label "
                f"lives on its jev row, and compare reads it there")
        if row["primitive"] == GATE_PRIMITIVE:
            raise JudgmentRefused(
                f"row {row_id} is a gate event, not a decision; there is "
                f"nothing for a label to be right or wrong about")
        if row["answer"] is None:
            raise JudgmentRefused(
                f"row {row_id} recorded no answer (a failed call or a batch); "
                f"a label on it could only ever count as wrong")
        if row["primitive"] == "choice" and _position(override) is None:
            raise JudgmentRefused(
                f"a choice is labelled with the position that was right, a whole "
                f"number from 1; got {override!r}")
        if row["override"] is not None:
            if same_number(row["override"], override):
                return False
            if not replace:
                raise JudgmentRefused(
                    f"row {row_id} is already labelled {row['override']}; a "
                    f"different label replaces it only when asked to")
        connection.execute(
            "UPDATE judgment SET override = ?, override_source = ?, "
            "override_at = ? WHERE id = ?",
            (override, source, moment, row_id))
    return True


def same_number(left: str, right: str) -> bool:
    """Whether two recorded numbers are equal, exactly: `2` is `2.0`, and
    2**53 is not 2**53 + 1, which a float comparison would call equal."""
    return Decimal(left) == Decimal(right)


def unlabelled(
    connection: sqlite3.Connection,
    stage: str,
    *,
    prefix: str | None = None,
) -> list[dict]:
    """The decisions of one stage that carry no label yet, oldest first.

    `prefix` narrows them to the rows whose `question_id` starts with it,
    which is how a labeller finds the rows that name a subject it can look
    up. Gate events are not decisions, and a row with no answer cannot be
    labelled, so neither is returned. Nor is a comparison arm's row: a pair's
    label lives on its Jev row, and `compare` reads it from there.
    """
    stage = _identifier("stage", stage, required=True)
    if not present(connection):
        return []
    rows = connection.execute(
        "SELECT id, timestamp, caller, stage, arm, primitive, question_id, "
        "outcome, answer, confidence FROM judgment "
        "WHERE stage = ? AND override IS NULL AND answer IS NOT NULL "
        "AND primitive <> ? AND arm IN (?, ?) "
        "AND (? IS NULL OR substr(question_id, 1, length(?)) = ?) "
        "ORDER BY timestamp, id",
        (stage, GATE_PRIMITIVE, *DECISION_ARMS, prefix, prefix, prefix))
    return [dict(row) for row in rows]


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
  AND arm IN ('jev', 'baseline')
  AND (:since IS NULL OR timestamp >= :since)
  AND (:until IS NULL OR timestamp < :until)
GROUP BY stage, arm
ORDER BY stage, arm
"""

#: Rows that used tokens and carry no cost, per stage, arm and provider: no
#: price was registered for that provider (or model) when they were recorded.
UNPRICED = """
SELECT stage, arm, provider, COUNT(*) AS n
FROM judgment
WHERE usd IS NULL
  AND COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0) > 0
  AND primitive <> :gate
  AND arm IN ('jev', 'baseline')
  AND (:since IS NULL OR timestamp >= :since)
  AND (:until IS NULL OR timestamp < :until)
GROUP BY stage, arm, provider
ORDER BY stage, arm, provider
"""

#: Why the model was not used, per stage: the decline reasons of both arms
#: counted separately, so a stage's fallbacks name their cause.
DECLINES = """
SELECT stage, cause, COUNT(*) AS n
FROM judgment
WHERE cause IS NOT NULL
  AND primitive <> :gate
  AND arm IN ('jev', 'baseline')
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
    AND arm IN ('jev', 'baseline')
    AND (:since IS NULL OR timestamp >= :since)
    AND (:until IS NULL OR timestamp < :until)
  GROUP BY stage, pair
  HAVING COUNT(DISTINCT arm) = 2
)
GROUP BY stage
"""


#: Every labelled decision, for the correctness counts. Rightness is decided
#: in Python with `same_number`: SQLite has no exact decimal, and a REAL cast
#: calls 2**53 and 2**53 + 1 equal. Labels are few, so this is a small read.
LABELLED = """
SELECT stage, arm, confidence, answer, override
FROM judgment
WHERE override IS NOT NULL
  AND answer IS NOT NULL
  AND primitive <> :gate
  AND arm IN ('jev', 'baseline')
  AND (:since IS NULL OR timestamp >= :since)
  AND (:until IS NULL OR timestamp < :until)
ORDER BY stage, arm
"""


def band_of(confidence) -> str:
    """Reported confidence in tenths: `0.9` holds 0.9 up to and including 1.0,
    and a row with no confidence is `none`."""
    if confidence is None:
        return "none"
    return f"{min(int(confidence * 10), 9) / 10:.1f}"


#: Which rules labelled each stage, so the report can print what each rule
#: cannot see next to the numbers it produced.
SOURCES = """
SELECT DISTINCT stage, override_source AS source
FROM judgment
WHERE override IS NOT NULL
  AND override_source IS NOT NULL
  AND primitive <> :gate
  AND (:since IS NULL OR timestamp >= :since)
  AND (:until IS NULL OR timestamp < :until)
ORDER BY stage, source
"""

#: What a labelling rule cannot see, printed next to the stage it labelled.
#: A rule that sees only one direction of error makes `right` an upper bound,
#: and a reader choosing a floor from it has to know that.
#: Empty until a rule ships: the first sd-review rule was withdrawn in review,
#: because a later fix in a shared file is not evidence of the right tier.
LABEL_LIMITS: dict[str, str] = {}


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
             "paired": 0, "bands": [], "sources": []},
        )

    stages: dict[str, dict] = {}
    for row in connection.execute(BY_STAGE_ARM, bounds):
        arm = dict(row, unpriced=0, unpriced_providers=[])
        entry = entry_for(arm.pop("stage"))
        entry["arms"][arm.pop("arm")] = arm
    for row in connection.execute(UNPRICED, bounds):
        arm = stages[row["stage"]]["arms"][row["arm"]]
        arm["unpriced"] += row["n"]
        arm["unpriced_providers"].append(row["provider"])
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
    for arm in (a for entry in stages.values() for a in entry["arms"].values()):
        arm["labelled"] = arm["right"] = 0
    # Bands are per arm: a baseline row has no confidence, and pooling it with
    # the model's `none` band would mix two arms in one number.
    bands: dict[tuple[str, str, str], dict] = {}
    for row in connection.execute(LABELLED, bounds):
        if row["stage"] not in stages:
            continue
        right = int(same_number(row["answer"], row["override"]))
        arm = stages[row["stage"]]["arms"][row["arm"]]
        arm["labelled"] += 1
        arm["right"] += right
        band = bands.setdefault(
            (row["stage"], row["arm"], band_of(row["confidence"])),
            {"arm": row["arm"], "band": band_of(row["confidence"]),
             "labelled": 0, "right": 0})
        band["labelled"] += 1
        band["right"] += right
    for (stage, _, _), band in sorted(
            bands.items(), key=lambda item: (item[0][0], item[0][1],
                                             item[0][2] == "none", item[0][2])):
        stages[stage]["bands"].append(band)
    for row in connection.execute(SOURCES, bounds):
        if row["stage"] in stages:
            stages[row["stage"]]["sources"].append(row["source"])
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
        + (f" ({arm['unpriced']} unpriced: no providers.yaml price for "
           f"{', '.join(arm['unpriced_providers'])})" if arm.get("unpriced") else "")
    )


def _correctness_lines(entry: dict) -> list[str]:
    """How many of a stage's rows carry a label, how many of those were
    right, and the same by reported confidence, with each rule's limit."""
    if not any(arm.get("labelled") for arm in entry["arms"].values()):
        return ["    correctness: no labelled rows"]
    lines = []
    for name in ("jev", "baseline"):
        arm = entry["arms"].get(name)
        if not arm or not arm.get("labelled"):
            continue
        lines.append(f"    correctness {name}: {arm['labelled']} labelled, "
                     f"{arm['right']} right ({_rate(arm['right'], arm['labelled'])})")
        bands = [band for band in entry.get("bands", ()) if band["arm"] == name]
        if bands:
            lines.append(f"    by confidence {name}: " + ", ".join(
                f"{band['band']} {band['right']}/{band['labelled']}" for band in bands))
    for source in entry.get("sources", ()):
        if source in LABEL_LIMITS:
            lines.append(f"    {source}: {LABEL_LIMITS[source]}")
    return lines


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
        lines.extend(_correctness_lines(entry))
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


# --- the comparison arms (sd:2366) -------------------------------------------
#
# Every live Jev call can be asked of a `kev` and a `haiku` arm too, with the
# Jev row's pair id. Agreement and accuracy are computed here, at read time,
# from rows that hold numbers only: no arm knew another's answer when it
# recorded its own.

#: Every row the comparison reads. One stage at a time is a small read, and
#: percentiles and exact agreement are decided in Python: SQLite has neither.
COMPARE_ROWS = """
SELECT stage, arm, provider, pair, primitive, outcome, cause, answer,
       probabilities, duration_ms, server_ms, tokens_in, tokens_out, usd,
       override
FROM judgment
WHERE arm IN ('jev', 'kev', 'haiku')
  AND primitive <> :gate
  AND (:stage IS NULL OR stage = :stage)
  AND (:since IS NULL OR timestamp >= :since)
  AND (:until IS NULL OR timestamp < :until)
ORDER BY stage, timestamp, id
"""


#: The caller's own answer of each pair: the baseline row a shadow call
#: (`--shadow`, or `jev shadow on`) writes beside the judgment (sd:2761).
OLD_ANSWERS = """
SELECT stage, pair, answer
FROM judgment
WHERE arm = 'baseline'
  AND pair IS NOT NULL
  AND primitive <> :gate
  AND (:stage IS NULL OR stage = :stage)
  AND (:since IS NULL OR timestamp >= :since)
  AND (:until IS NULL OR timestamp < :until)
"""

#: How many disagreement pairs of values the report lists per arm.
SPLIT_TOP = 5


def _has_arms(connection: sqlite3.Connection) -> bool:
    """Whether migration 017 has run. A reader may open a schema 11-16
    database it does not migrate; that table holds no kev or haiku row and
    has neither column the comparison reads, so it has nothing to compare."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(judgment)")}
    return {"server_ms", "probabilities"} <= columns


def percentile(values: list, share: float):
    """The nearest-rank percentile, or None for no values. Nearest rank and
    not interpolation: the number printed is one a call actually took."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-int(share * 100) * len(ordered) // 100))
    return ordered[min(rank, len(ordered)) - 1]


def _distribution(row) -> list[float] | None:
    if not row["probabilities"]:
        return None
    return [float(value) for value in row["probabilities"].split(",")]


def top(row) -> int | None:
    """The answer a row would act on, as one comparable number.

    A noul is yes when its probability is at least 0.5. A choice is the
    position that won, read as a label is: a fractional one is no position.
    A score is its recorded score, rounded as `_truth` rounds a label; its
    distribution feeds the Brier score only, so two rows with the same score
    always agree.
    """
    if row["answer"] is None:
        return None
    value = float(row["answer"])
    if row["primitive"] == "noul":
        return 1 if value >= 0.5 else 0
    if row["primitive"] == "score":
        return int(round(value))
    return _position(row["answer"])


def _position(value: str) -> int | None:
    """A choice's position, a whole number from 1, or None."""
    number = float(value)
    return int(number) if number.is_integer() and number >= 1 else None


def _truth(primitive: str, override: str) -> int | None:
    """The label as `top` reads an answer. Only a score rounds: a choice
    label is a position, and one that is not whole (written before `label`
    checked) is no label rather than the nearest position."""
    value = float(override)
    if primitive == "noul":
        return 1 if value >= 0.5 else 0
    if primitive == "choice":
        return _position(override)
    return int(round(value))


def brier(row, truth: int) -> float | None:
    """The Brier score of one row against its label, or None without the
    distribution. A noul's answer is its probability; a choice's distribution
    counts from position 1 and a score's from level 0."""
    if row["primitive"] == "noul" and row["answer"] is not None:
        return (float(row["answer"]) - truth) ** 2
    dist = _distribution(row)
    if not dist:
        return None
    index = truth - 1 if row["primitive"] == "choice" else truth
    if not 0 <= index < len(dist):
        return None              # a label no option matches has no Brier score
    return sum((p - (1.0 if i == index else 0.0)) ** 2 for i, p in enumerate(dist))


def compare(
    connection: sqlite3.Connection,
    *,
    stage: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Per stage, one entry per arm and provider: calls, latency, cost, how
    often the arm agreed with the Jev row of the same pair, and how often it
    agreed with the old mechanism's answer of that pair (sd:2761).

    A pair is one decision, and its label is read from its Jev row only.
    """
    if stage is not None:
        stage = _identifier("stage", stage, required=True)
    if not present(connection) or not _has_arms(connection):
        return []
    bounds = {"since": since, "until": until, "stage": stage, "gate": GATE_PRIMITIVE}
    rows = [dict(row) for row in connection.execute(COMPARE_ROWS, bounds)]
    old = {(row["stage"], row["pair"]): row["answer"]
           for row in connection.execute(OLD_ANSWERS, bounds)}
    reference: dict[tuple[str, str], dict] = {}
    labels: dict[tuple[str, str], str] = {}
    for row in rows:
        if row["pair"] is None:
            continue
        key = (row["stage"], row["pair"])
        if row["arm"] == "jev":
            reference[key] = row
            # The pair's label lives on its Jev row; `label` refuses an arm
            # row, and one that reached an arm row some other way is ignored.
            if row["override"] is not None:
                labels[key] = row["override"]
    stages: dict[str, dict[tuple[str, str], dict]] = {}
    for row in rows:
        groups = stages.setdefault(row["stage"], {})
        entry = groups.setdefault((row["arm"], row["provider"]), {
            "arm": row["arm"], "provider": row["provider"], "calls": 0, "ok": 0,
            "declines": {}, "tokens_in": 0, "tokens_out": 0, "usd": None,
            "_ms": [], "_server": [], "paired": 0, "agree": None, "_dp": [],
            "labelled": 0, "right": 0, "_brier": [],
            "old_pairs": 0, "old_compared": 0, "old_agree": 0, "_split": {}})
        entry["calls"] += 1
        entry["ok"] += row["outcome"] == "ok"
        if row["cause"]:
            entry["declines"][row["cause"]] = entry["declines"].get(row["cause"], 0) + 1
        entry["tokens_in"] += row["tokens_in"] or 0
        entry["tokens_out"] += row["tokens_out"] or 0
        if row["usd"] is not None:
            entry["usd"] = (entry["usd"] or 0.0) + row["usd"]
        if row["duration_ms"] is not None:
            entry["_ms"].append(row["duration_ms"])
        if row["server_ms"] is not None:
            entry["_server"].append(row["server_ms"])
        mine = top(row)
        if (row["stage"], row["pair"]) in old:
            # The old answer is read in this row's primitive: a baseline row
            # is `primitive=baseline`, and its number means what the
            # judgment's would.
            entry["old_pairs"] += 1
            theirs = top({"answer": old[(row["stage"], row["pair"])],
                          "primitive": row["primitive"]})
            if theirs is not None and mine is not None:
                entry["old_compared"] += 1
                if theirs == mine:
                    entry["old_agree"] += 1
                else:
                    split = entry["_split"]
                    split[(theirs, mine)] = split.get((theirs, mine), 0) + 1
        if mine is None or row["pair"] is None:
            continue
        key = (row["stage"], row["pair"])
        jev = reference.get(key)
        if row["arm"] != "jev" and jev is not None and top(jev) is not None \
                and jev["primitive"] == row["primitive"]:
            entry["paired"] += 1
            entry["agree"] = (entry["agree"] or 0) + (mine == top(jev))
            if row["primitive"] == "noul":
                entry["_dp"].append(abs(float(row["answer"]) - float(jev["answer"])))
        truth = _truth(row["primitive"], labels[key]) if key in labels else None
        if truth is not None:
            entry["labelled"] += 1
            entry["right"] += mine == truth
            score = brier(row, truth)
            if score is not None:
                entry["_brier"].append(score)
    report = []
    for name in sorted(stages):
        arms = []
        for (arm, provider), entry in sorted(
                stages[name].items(),
                key=lambda item: (MODEL_ARMS.index(item[0][0]), item[0][1])):
            ms, server, dp, scores = (entry.pop("_ms"), entry.pop("_server"),
                                      entry.pop("_dp"), entry.pop("_brier"))
            entry["p50_ms"] = percentile(ms, 0.50)
            entry["p95_ms"] = percentile(ms, 0.95)
            entry["server_p50_ms"] = percentile(server, 0.50)
            entry["mean_abs_dp"] = sum(dp) / len(dp) if dp else None
            entry["agreement"] = (entry["agree"] / entry["paired"]
                                  if entry["paired"] else None)
            entry["accuracy"] = (entry["right"] / entry["labelled"]
                                 if entry["labelled"] else None)
            entry["brier"] = sum(scores) / len(scores) if scores else None
            entry["old_agreement"] = (entry["old_agree"] / entry["old_compared"]
                                      if entry["old_compared"] else None)
            entry["old_disagreements"] = [
                {"old": was, "arm": said, "n": n} for (was, said), n in sorted(
                    entry.pop("_split").items(),
                    key=lambda item: (-item[1], item[0]))[:SPLIT_TOP]]
            arms.append(entry)
        report.append({"stage": name, "arms": arms})
    return report


def _compare_line(entry: dict) -> str:
    who = f"{entry['arm']} ({entry['provider']})"
    declines = ", ".join(f"{n} {cause}" for cause, n in sorted(entry["declines"].items()))
    parts = [
        f"    {who}: {entry['calls']} call(s), {entry['ok']} ok"
        + (f" ({declines})" if declines else ""),
        f"latency p50 {_ms(entry['p50_ms'])} / p95 {_ms(entry['p95_ms'])}"
        + (f", server p50 {_ms(entry['server_p50_ms'])}"
           if entry["server_p50_ms"] is not None else ""),
        f"tokens {entry['tokens_in']} in / {entry['tokens_out']} out",
        f"cost {_money(entry['usd'])}",
    ]
    if entry["arm"] != "jev":
        agreement = (f"agree with jev {entry['agree'] or 0}/{entry['paired']}"
                     f" ({_rate(entry['agree'], entry['paired'])})")
        if entry["mean_abs_dp"] is not None:
            agreement += f", mean |Δp| {entry['mean_abs_dp']:.3f}"
        parts.append(agreement)
    if entry["old_pairs"] and not entry["old_compared"]:
        parts.append(f"vs old: no comparable old answer in {entry['old_pairs']} pair(s)")
    elif entry["old_pairs"]:
        against = (f"vs old {entry['old_agree']}/{entry['old_compared']} agree"
                   f" ({_rate(entry['old_agree'], entry['old_compared'])})")
        if entry["old_pairs"] > entry["old_compared"]:
            against += (f", {entry['old_pairs'] - entry['old_compared']} pair(s)"
                        f" not comparable")
        if entry["old_disagreements"]:
            against += ", old→arm " + " ".join(
                f"{d['old']}→{d['arm']} ×{d['n']}" for d in entry["old_disagreements"])
        parts.append(against)
    if entry["labelled"]:
        parts.append(f"labelled {entry['right']}/{entry['labelled']} right"
                     + (f", Brier {entry['brier']:.3f}" if entry["brier"] is not None else ""))
    return ", ".join(parts)


def compare_text(report: list[dict]) -> str:
    """The comparison as a person reads it, one block per stage."""
    if not report:
        return "judgments compare: no calls recorded on the jev, kev or haiku arms\n"
    lines = ["judgments compare: each arm against the jev row and the old "
             "mechanism of the same pair"]
    for entry in report:
        paired = any(arm["old_pairs"] for arm in entry["arms"])
        lines.append(f"  {entry['stage']}:" + (
            "" if paired else " no paired samples with the old mechanism"))
        lines.extend(_compare_line(arm) for arm in entry["arms"])
    return "\n".join(lines) + "\n"


def compare_json(report: list[dict]) -> str:
    """The comparison as JSON, for the analysis scripts."""
    return json.dumps({"stages": report}, indent=2, sort_keys=True) + "\n"
