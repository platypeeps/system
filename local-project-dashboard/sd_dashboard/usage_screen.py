"""The Usage area's month: the four numbers per bill, the `bound` rows, the charts.

Requirement 6 says the four numbers, the item screen and `sd usage` cannot
disagree, so this renders `usage.read` -- `reads.usage_month` under the
registry's merged caps, so a legacy row cap on a `start` bill is no cap here
either (sd:234 slice 12h) -- the read `sd usage` prints, and nothing it
computes itself; `/api/usage` serves `usage.json_text` of the same read, the
bytes `sd usage --json` writes. The one difference is the
sweep: the dashboard's GET connection is read-only (`sd_db.connect` with
`write=False`: `mode=ro`, `query_only`), so the panel and the API show the
read as it stands, a dead owner's `reserved` or `sending` row still held
until the next reservation or `sd usage` sweeps it, after which the two
are the same bytes. Requirement 5 draws each
capped bill's burn and a `plan` bill's two windows as gauges from the meter,
and lists `bound` rows so the operator can correct one against the invoice.
The correction itself is a write this slice does not carry: criterion 15's
correction clause is the two-month row (`prd.md:1572-1578`), which waits on
a column the schema does not have (slice 8a's note in `implement.md`).

Everything here is markup the server renders; the month is a GET form, so
the screen reads without the page script, and nothing on it moves.
"""

from __future__ import annotations

import sqlite3

from sd_db import reads, usage
from sd_db.errors import SdDbError

from .charts import burn_svg, gauge_svg
from .listing import Column, Listing
from .markup import join, tag

__all__ = ["document", "usage_panel"]


def document(connection: sqlite3.Connection, *, now: str, month: str | None) -> str:
    """`/api/usage`: the read as `sd usage --json` prints it, without the
    verb's sweep (the module docstring: the connection cannot write)."""
    return usage.json_text(usage.read(connection, month=month, now=now))


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _percent(used: float, cap: float) -> float:
    """Of a cap. A zero cap is a limit with nothing left, so it reads full."""
    return 100.0 * used / cap if cap else 100.0


def _gauges(rows) -> list[object]:
    return [gauge_svg(f"{row['provider']} · {row['window_minutes']} min window", row["used_percent"],
                      name=f"{row['provider']}:{row['window_minutes']}") for row in rows]


def _bill(bill: reads.BillUsage, *, days: int, today: int | None, meter):
    """One bill's card. A `plan` bill draws its meter windows and no cap burn,
    whatever `cap_usd_month` says (requirement 5); any other bill with a cap,
    zero included, draws the cap gauge, the room and the burn."""
    numbers = tag("dl", join(tag("div", tag("dt", label), tag("dd", value, data_number=key))
                             for key, label, value in (
                                 ("spent", "spent", _money(bill.spent)),
                                 ("estimated", "estimated (bound)", _money(bill.estimated)),
                                 ("held", "held (reserved and sending)", _money(bill.held)),
                                 ("cap", "cap", _money(bill.cap)))),
                  class_="tile-inputs usage-numbers")
    parts = [tag("h3", f"{bill.name} ({bill.cost_basis})"), numbers]
    if bill.cost_basis == "plan":
        windows = [row for row in meter if row["bill"] == bill.name]
        parts.extend(_gauges(windows) or [tag("p", "No meter rows this month.", class_="hint")])
    elif bill.cap is not None:
        parts.append(gauge_svg("of cap", _percent(bill.spent + bill.held, bill.cap), name=bill.name))
        parts.append(tag("p", f"room {_money(bill.room)}", class_="hint"))
        parts.append(burn_svg(bill, days=days, today=today))
    return tag("article", join(parts), class_="operation-card usage-bill", data_bill=bill.name)


def _bound_listing(month: reads.Usage, parameters) -> Listing:
    """The `bound` rows through the one list component: they grow all month,
    so the filter and the pager are the component's, and the month rides in
    every link. The roles table stays a plain table: it is an aggregate
    bounded by bills × providers × roles, never a page's worth."""
    query, number, selected = Listing.read_query(parameters)
    return Listing("usage-bound", (
        Column("timestamp", "When", lambda row: row["timestamp"]),
        Column("call_id", "Call", lambda row: row["call_id"] or "—"),
        Column("bill", "Bill", lambda row: row["bill"] or "—"),
        Column("provider", "Provider", lambda row: row["provider"] or "—"),
        Column("role", "Role", lambda row: row["role"] or "—"),
        Column("assignment", "Assignment",
               lambda row: f"assignment {row['assignment']}" if row["assignment"] is not None else "—"),
        Column("usd", "Bound", lambda row: tag("span", _money(row["usd"]), data_bound=row["call_id"] or ""),
               text=lambda row: _money(row["usd"])),
    ), month.bound, path="/operations", query=query, page_number=number, selected=selected,
        extra={"area": "usage", "month": month.month}, empty="No bound rows this month.")


def read_month(connection: sqlite3.Connection, *, now: str, parameters) -> tuple:
    """`(month, error)`: the month the picker asks for, as one store read.

    Split out of the panel so a caller can hold this read inside a
    transaction and render outside it (Codex review of PR #501). The render
    below runs git across every registered repository, and a read snapshot
    held across that pins the WAL for as long as the slowest checkout takes.
    """
    asked = (parameters.get("month") or [None])[0] or None
    try:
        return usage.read(connection, month=asked, now=now), ""
    except SdDbError as error:
        return None, str(error)


def usage_panel(connection: sqlite3.Connection, *, now: str, parameters, month=None, error: str = "") -> object:
    """The section the Usage area shows below its cost tile.

    `month` is the already-read month, for a caller that read it under a
    transaction of its own; without one this reads it here.
    """
    if month is None and not error:
        month, error = read_month(connection, now=now, parameters=parameters)
    if error:
        return tag("section", tag("h2", "The month"), tag("p", error, class_="notice"))
    picker = tag("form", tag("input", type="hidden", name="area", value="usage"),
                 tag("label", "Month", for_="usage-month"),
                 tag("input", type="month", name="month", id="usage-month", value=month.month),
                 tag("button", "Show", type="submit", class_="listing-go"),
                 method="get", action="/operations", class_="listing-controls usage-month")
    bills = join(_bill(bill, days=month.days, today=month.today, meter=month.meter) for bill in month.bills) \
        or tag("p", "No bills yet.", class_="hint")
    # Meter rows whose bill is not a `plan` card still show, below the cards.
    plans = {bill.name for bill in month.bills if bill.cost_basis == "plan"}
    gauges = _gauges(row for row in month.meter if row["bill"] not in plans)
    roles = tag("table", tag("thead", tag("tr", join(tag("th", label, scope="col") for label in
        ("Bill", "Provider", "Role", "Calls", "Spent", "Held", "Tokens in", "Tokens out")))),
        tag("tbody", join(tag("tr", tag("td", row["bill"] or "—"), tag("td", row["provider"] or "—"),
            tag("td", row["role"] or "—"), tag("td", row["calls"]), tag("td", _money(row["spent"])),
            tag("td", _money(row["held"])), tag("td", row["tokens_in"]), tag("td", row["tokens_out"]))
            for row in month.roles)), class_="listing-table")
    bound = _bound_listing(month, parameters).render()
    return tag("section", tag("h2", "The month"), picker,
        # A month with no row is not a measured zero (the weekly tile's rule).
        tag("p", f"{month.month}: {_money(month.spent)} spent, {_money(month.held)} held, "
                 f"against the invoice line." if month.roles else f"{month.month}: no cost rows recorded.",
            class_="cost-line usage-total"),
        tag("div", bills, class_="operations-grid"),
        join((tag("h3", "Other meter windows"), join(gauges))) if gauges else "",
        tag("h3", "By bill, provider and role"), roles,
        tag("h3", "Bound rows"),
        tag("p", "Money the provider may have billed for a response that was lost; counted at the bound, "
                 "labelled estimated, until corrected against the invoice.", class_="hint"),
        bound, tag("span", hidden=True, data_cli=f"sd-db.sh usage --month {month.month}"),
        id="usage", class_="usage-panel")
