"""Recurrence rules: a stdlib subset of RFC 5545 RRULE.

The subset is `FREQ` (`DAILY`, `WEEKLY`, `MONTHLY`, `YEARLY`), `INTERVAL`,
`BYMONTH` and `BYMONTHDAY` (operator decision, sd:1099 note 3728). There is
no `python-dateutil`: `sd_db` stays dependency-free. Every other part, and
every sub-day frequency, is refused with `RecurrenceError` naming it. A part
the engine ignored would produce dates the operator did not ask for.

Two rules follow RFC 5545 rather than intuition, on purpose, so a stored rule
means here what it means in TaskNotes:

- **Month end skips.** `BYMONTHDAY=31` produces nothing in a month without a
  31st. `BYMONTHDAY=-1` is the standard's spelling of the last day.
- **The start is `DTSTART`.** `INTERVAL` counts periods from it, and a
  `MONTHLY` or `YEARLY` rule without `BYMONTHDAY` takes its day from it; a
  `YEARLY` rule without `BYMONTH` takes its month from it, unless
  `BYMONTHDAY` is given, which expands over every month.

`docs/work/2026-09-19-tasks-that-recur/design.md` is the plan.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

from .errors import SdDbError

FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
PARTS = ("FREQ", "INTERVAL", "BYMONTH", "BYMONTHDAY")
ANCHORS = ("schedule", "completion")

#: How far `next_after` searches before it calls a rule empty. A rule with no
#: occurrence in a century, such as February 30th, has none at all.
HORIZON_YEARS = 100

_NUMBER = re.compile(r"^[+-]?[0-9]{1,9}$")


class RecurrenceError(SdDbError, ValueError):
    """A recurrence rule outside the subset, malformed, or without a next date."""


@dataclass(frozen=True)
class Rule:
    freq: str
    interval: int = 1
    bymonth: tuple[int, ...] = ()
    bymonthday: tuple[int, ...] = ()

    def text(self) -> str:
        """The canonical spelling: parts in `PARTS` order, `INTERVAL=1` dropped."""
        parts = [f"FREQ={self.freq}"]
        if self.interval != 1:
            parts.append(f"INTERVAL={self.interval}")
        if self.bymonth:
            parts.append("BYMONTH=" + ",".join(map(str, self.bymonth)))
        if self.bymonthday:
            parts.append("BYMONTHDAY=" + ",".join(map(str, self.bymonthday)))
        return ";".join(parts)


def _numbers(name: str, value: str, valid) -> tuple[int, ...]:
    found = set()
    for piece in value.split(","):
        piece = piece.strip()
        if not _NUMBER.match(piece) or not valid(int(piece)):
            raise RecurrenceError(f"{name}={value} is not valid; {name} takes {_RANGES[name]}")
        found.add(int(piece))
    return tuple(sorted(found))


_RANGES = {
    "INTERVAL": "one positive integer",
    "BYMONTH": "a comma-separated list of 1 to 12",
    "BYMONTHDAY": "a comma-separated list of 1 to 31 or -31 to -1",
}


def parse(text: object) -> Rule:
    """Validate one rule and return it; `Rule.text()` is what the store keeps."""
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise RecurrenceError("recurrence must be an RRULE string such as FREQ=WEEKLY")
    body = text.strip()
    if body.upper().startswith("RRULE:"):
        body = body[len("RRULE:"):]
    seen: dict[str, str] = {}
    for piece in body.split(";"):
        if not piece.strip():
            raise RecurrenceError(f"recurrence {text!r} has an empty part")
        name, sep, value = piece.partition("=")
        name, value = name.strip().upper(), value.strip().upper()
        if not sep or not name:
            raise RecurrenceError(f"recurrence part {piece!r} is not NAME=VALUE")
        if name not in PARTS:
            raise RecurrenceError(
                f"recurrence part {name} is not supported; the parts are "
                f"{', '.join(PARTS)}")
        if name in seen:
            raise RecurrenceError(f"recurrence part {name} is given twice")
        if not value:
            raise RecurrenceError(f"recurrence part {name} has no value")
        seen[name] = value
    if "FREQ" not in seen:
        raise RecurrenceError("recurrence needs FREQ")
    freq = seen["FREQ"]
    if freq not in FREQUENCIES:
        raise RecurrenceError(f"FREQ={freq} is not supported; FREQ is one of {', '.join(FREQUENCIES)}")
    interval = 1
    if "INTERVAL" in seen:
        value = seen["INTERVAL"]
        if not re.fullmatch(r"[0-9]{1,9}", value) or int(value) < 1:
            raise RecurrenceError(f"INTERVAL={value} is not valid; INTERVAL takes {_RANGES['INTERVAL']}")
        interval = int(value)
    bymonth = _numbers("BYMONTH", seen["BYMONTH"], lambda n: 1 <= n <= 12) if "BYMONTH" in seen else ()
    bymonthday = (_numbers("BYMONTHDAY", seen["BYMONTHDAY"], lambda n: 1 <= abs(n) <= 31)
                  if "BYMONTHDAY" in seen else ())
    if bymonthday and freq == "WEEKLY":
        raise RecurrenceError("BYMONTHDAY cannot be used with FREQ=WEEKLY; RFC 5545 forbids it")
    return Rule(freq, interval, bymonth, bymonthday)


def _days(year: int, month: int, wanted: tuple[int, ...]) -> list[int]:
    """The days of `wanted` that exist in this month, ascending. Month end skips."""
    last = calendar.monthrange(year, month)[1]
    days = set()
    for day in wanted:
        actual = day if day > 0 else last + 1 + day
        if 1 <= actual <= last:
            days.add(actual)
    return sorted(days)


def next_after(rule: Rule, start: date, after: date | None = None) -> date:
    """The first occurrence of the series that starts at `start`, strictly after
    `after` and after `start`.

    `start` is the series' `DTSTART`: `INTERVAL` counts periods from it, and
    the implied day and month come from it. `after` only moves the threshold,
    never the phase, so a threshold far from `start` still lands on the
    series. Starting each search from the date the previous one produced
    lands a chain of occurrences on the dates of one RFC 5545 series.
    """
    threshold = start if after is None or after < start else after
    limit = min(threshold.year + HORIZON_YEARS, date.max.year - 1)
    if rule.freq in ("DAILY", "WEEKLY"):
        step = rule.interval * (7 if rule.freq == "WEEKLY" else 1)
        # Jump straight to the first step past the threshold; the filter below
        # walks on from there.
        k = (threshold.toordinal() - start.toordinal()) // step + 1
        while True:
            ordinal = start.toordinal() + k * step
            if ordinal > date(limit, 12, 31).toordinal():
                break
            candidate = date.fromordinal(ordinal)
            if ((not rule.bymonth or candidate.month in rule.bymonth)
                    and (not rule.bymonthday
                         or candidate.day in _days(candidate.year, candidate.month, rule.bymonthday))):
                return candidate
            k += 1
    elif rule.freq == "MONTHLY":
        base = start.year * 12 + start.month - 1
        k = (threshold.year * 12 + threshold.month - 1 - base) // rule.interval
        while True:
            year, index = divmod(base + k * rule.interval, 12)
            if year > limit:
                break
            month = index + 1
            if not rule.bymonth or month in rule.bymonth:
                for day in _days(year, month, rule.bymonthday or (start.day,)):
                    candidate = date(year, month, day)
                    if candidate > threshold:
                        return candidate
            k += 1
    else:  # YEARLY
        if rule.bymonth:
            months: tuple[int, ...] = rule.bymonth
        elif rule.bymonthday:
            months = tuple(range(1, 13))
        else:
            months = (start.month,)
        k = (threshold.year - start.year) // rule.interval
        while True:
            year = start.year + k * rule.interval
            if year > limit:
                break
            for month in months:
                for day in _days(year, month, rule.bymonthday or (start.day,)):
                    candidate = date(year, month, day)
                    if candidate > threshold:
                        return candidate
            k += 1
    raise RecurrenceError(
        f"recurrence {rule.text()} has no occurrence after {threshold.isoformat()} "
        f"within {HORIZON_YEARS} years")


#: One full leap cycle. A completion date's effect on a rule is its month,
#: its day and its place in the leap cycle, and these four years hold every
#: combination of the three.
LEAP_CYCLE = (date(2028, 1, 1), date(2031, 12, 31))


def first_barren_start(rule: Rule) -> date | None:
    """The first start date in `LEAP_CYCLE` from which `rule` never occurs.

    A completion-anchored rule restarts at the completion date and takes
    every part it omits from it: the day, the month, and the phase that
    `INTERVAL` counts from. So the rule is sound for that anchor only when
    every possible start finds an occurrence. `None` means it does; the
    sweep stops at the first start that does not.
    """
    first, last = (d.toordinal() for d in LEAP_CYCLE)
    for ordinal in range(first, last + 1):
        start = date.fromordinal(ordinal)
        try:
            next_after(rule, start)
        except RecurrenceError:
            return start
    return None
