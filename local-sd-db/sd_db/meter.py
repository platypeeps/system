"""The meter's row: provider-wide plan usage, sampled on a schedule.

A `meter` cost row carries a window and a percentage and no money. It is
what `local-agent-meter/agent-meter.py` writes every four hours from
`codexbar usage`: per provider, the vendor's own rate windows -- five hours
and a week for `claude` -- each with the share of it used. One row is
written **per provider per window**, so the later Usage-screen slice (8d)
reads two rows for its two gauges rather than two fields of one, and `usd`,
`tokens_in` and
`tokens_out` are NULL, which every sum over `run` and `bound` rows already
assumes by never reading a `meter` row at all.

The row names the provider and the bill the provider is on, and the bill is
the registry's answer, not the caller's: the sampler knows `claude` and
`codex` by the names `codexbar` uses, and the registry says what each is
billed to. So the registry is read through the connection, which also seeds
the `provider` and `bill` rows the row's foreign keys point at on a store
nothing has read yet -- inside the row's own transaction, after every
refusal, so a refused sample writes nothing at all. A provider the registry
does not name is refused; so is a percentage outside 0..100 and a window
that is not a positive number of minutes, because a row the reader cannot
draw as a gauge is worse than a gap in the series.

`latest` is the read the pack's meter step asks: the newest row for one
bill and one window, whatever month wrote it. `reads.usage_month` also
reads the latest row per provider and window, but month-scoped, because it
is the month's report; in a month's first five hours the freshest reading of
a five-hour window is last month's row, and a bill classified from the
month's report alone would read as unmetered until the next sample. So this
read is keyed by bill, the key the pack's cap map uses, and has no month.

What this module does not do: the JSONL beside it. `agent-meter.py` keeps
writing the blog piece's data file until item C decides otherwise, and this
row is the library's half of that dual write.
"""

from __future__ import annotations

import math
import sqlite3

from .database import transaction
from .errors import SdDbError
from .registry import Registry, beside, ensure_seeded, read as read_registry
from .writes import now as _now, stamp


class MeterRefused(SdDbError):
    """A sample the meter will not write, naming the provider."""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


def _is_window(window_minutes: object) -> bool:
    """A vendor's window: a positive `int`, and not a `bool`."""
    return type(window_minutes) is int and window_minutes > 0


def sample(
    connection: sqlite3.Connection,
    *,
    provider: str,
    window_minutes: int,
    used_percent: float,
    now: str | None = None,
    registry: Registry | None = None,
) -> int:
    """Write one `meter` row for `provider`'s window. Returns the row id.

    `window_minutes` is the vendor's window as `codexbar` reports it, a
    positive integer; `used_percent` the share used, 0 to 100 inclusive.
    `now` is the moment the reading was taken, in any aware ISO-8601 shape,
    and the sampler passes the stamp its JSONL line carries so the two
    records of one reading agree to the second; the clock when omitted.
    The registry is the file beside the connection's database unless the
    caller passes one; the `provider` and `bill` rows the foreign keys need
    are seeded from it inside the row's transaction, after the refusals.
    """
    if not isinstance(provider, str) or not provider:
        raise MeterRefused(
            f"a provider is a non-empty name from the registry; got {provider!r}",
            provider=provider if isinstance(provider, str) else None,
        )
    if not _is_window(window_minutes):
        raise MeterRefused(
            f"window_minutes must be a positive integer, the vendor's window in "
            f"minutes; provider {provider!r} was given {window_minutes!r}",
            provider=provider,
        )
    # The range first: `math.isfinite` converts an `int` to a double and
    # raises `OverflowError` on one too large for it, as `ledger._money`
    # already knows, and every refusal here is a `MeterRefused`.
    try:
        sound = (type(used_percent) in (int, float) and 0 <= used_percent <= 100
                 and math.isfinite(used_percent))
    except OverflowError:
        sound = False
    if not sound:
        raise MeterRefused(
            f"used_percent must be a number from 0 to 100; provider "
            f"{provider!r} was given {used_percent!r}",
            provider=provider,
        )
    moment = _now() if now is None else stamp(now)
    if registry is None:
        # The file alone, not `read(connection=...)`: that read seeds the
        # `provider` and `bill` tables, which is a write, and the refusal
        # below comes before any write. The bill is identity and lives in
        # the file; nothing the rows hold changes what a sample records.
        registry = read_registry(beside(connection))
    entry = registry.providers.get(provider)
    if entry is None:
        raise MeterRefused(
            f"provider {provider!r} is not in the registry at {registry.path}; "
            f"the meter samples only what the registry bills",
            provider=provider,
        )
    with transaction(connection):
        # The rows the foreign keys point at, in the row's own transaction
        # on a store nothing has read yet; a no-op once they are there.
        ensure_seeded(connection, registry)
        cursor = connection.execute(
            "INSERT INTO cost (timestamp, provider, bill, window_minutes, "
            "used_percent, source) VALUES (?, ?, ?, ?, ?, 'meter')",
            (moment, provider, entry.bill, window_minutes, float(used_percent)),
        )
    return int(cursor.lastrowid)


def latest(
    connection: sqlite3.Connection,
    *,
    bill: str,
    window_minutes: int,
) -> sqlite3.Row | None:
    """The newest `meter` row for `bill`'s window, or None when there is none.

    Newest by `timestamp` then `id`, the rule `reads.usage_month` applies,
    so two samples at one moment resolve to the later row. Not month-scoped:
    the row may have been written last month, which is the case the pack's
    meter step exists for. `window_minutes` is refused as `sample` refuses
    it; a bill is not checked, and a bill with no row reads as None.
    """
    if not _is_window(window_minutes):
        raise MeterRefused(
            f"window_minutes must be a positive integer, the vendor's window in "
            f"minutes; bill {bill!r} was given {window_minutes!r}",
        )
    return connection.execute(
        "SELECT id, timestamp, provider, bill, window_minutes, used_percent "
        "FROM cost WHERE source = 'meter' AND bill = ? AND window_minutes = ? "
        "ORDER BY timestamp DESC, id DESC LIMIT 1",
        (bill, window_minutes),
    ).fetchone()
