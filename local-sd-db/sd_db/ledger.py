"""The reservation ledger: a capped bill is charged before the call is made.

Every call the library makes on a capped bill is four rows' worth of one
row. The client reserves a bound -- prompt tokens plus the entry's
`max_tokens`, at the entry's price -- as a `cost` row of `source: reserved`
before anything goes on the wire; claims it, `reserved` to `sending`, in the
transaction just before the request is sent; and settles it once, to `run`
with the actual cost when the response arrives, or to `bound` at the full
bound when the response is lost, because the provider may have finished and
billed it. A second settlement of the same call id changes nothing.

The reservation is what makes the cap a cap rather than an alert. A bill's
exposure is `run` and `bound` rows of the month asked plus every `reserved`
and `sending` row still open on it, whichever month it was made in; a
reservation that would take that past the cap is refused, and the check and
the insert are one `BEGIN IMMEDIATE` transaction, so two callers with room
for one cannot both pass. An assignment carrying `budget_usd` is bounded the
same way by the same sum -- `exposure` is the one function, and a grep of
this file for a second sum over `cost` rows returns nothing -- on every bill,
capped or not.

Three consequences of the row being the only record:

* A call id is one attempt on the wire. The client makes no retry of its
  own; a caller that tries again reserves again as a new call and is refused
  when the room is gone.
* A `reserved` row whose owner is dead has provably spent nothing, because
  the claim is the only road to the wire and it refuses a row that is not
  `reserved`. The next reservation on any bill deletes it. A `sending` row
  whose owner is dead may have been billed, and settles to `bound`.
* `budget_usd` is accepted on an assignment only when every call the
  assignment can make goes through this ledger: its author a `url` entry,
  and the reviewers it can resolve the same. A `start` entry's session calls
  the vendor itself, and a budget the library cannot stand in front of is a
  number, not a bound.

What this module does not do: the `run` row a `start` session is charged at
its exit is the runner's to write, and the operator's correction of a
`bound` row against the invoice is the usage screen's.
"""

from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from . import paths, reporting
from .database import transaction
from .errors import RegistryError, SdDbError
from .registry import Provider, Registry, read as read_registry
from .writes import now as _now, stamp, update_assignment

#: The report job a settlement above its bound is filed under, as an
#: attention report (`reporting.ingest`), which is the row that pushes.
OVERSHOOT_JOB = "sd-ledger"
#: The longest call id `reserve` takes: the overshoot report's run id is
#: the call id, and `reporting.ingest` takes a run id of at most this many
#: characters, so a settlement must never be the first to find out.
MAX_CALL_ID = 160

class LedgerRefused(SdDbError):
    """A reservation, claim or settlement the ledger will not make.

    `scope` names what refused it -- `bill`, `assignment`, or `call` -- and
    the numbers travel with the message, so the runner can write its
    `budget spent` note with the amount rather than parsing the text.
    """

    def __init__(
        self,
        message: str,
        *,
        scope: str,
        name: str | int | None = None,
        exposure: float | None = None,
        limit: float | None = None,
        bound: float | None = None,
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.name = name
        self.exposure = exposure
        self.limit = limit
        self.bound = bound


@dataclass(frozen=True)
class Released:
    """What one sweep did: the call ids it deleted, and the ones it bound.

    A `None` in either tuple is a legacy row `record_cost` wrote with no
    call id (`cost.call_id` is nullable; `reserve` refuses one): the sweep
    releases it by its row id and reports it as `None`, since it has no
    other name.
    """

    deleted: tuple[str | None, ...] = ()
    bound: tuple[str | None, ...] = ()


def alive(pid: int) -> bool:
    """Whether `pid` is a live process. Signal 0 delivers nothing and asks."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Another user's process: alive, and not ours to signal.
        return True
    return True


def _money(value, what: str, *, scope: str, name, where: str | None = None) -> float:
    """`value` as a finite, non-negative number, or the refusal.

    SQLite stores `nan` as NULL, and a NULL `usd` is nothing to the sum: a
    reservation or a budget of `nan` would pass every limit unseen. A
    negative amount reopens room the call already spent. `bool` is an int
    to Python and never an amount. An `int` too large for a float is not
    finite either; `math.isfinite` raises on it rather than answering.
    `where` names the holder in the message when `scope` and `name` cannot,
    as for a row not yet written.
    """
    try:
        sound = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        sound = False
    if not sound:
        raise LedgerRefused(
            f"{what} must be a finite, non-negative number; "
            f"{where or f'{scope} {name!r}'} was given {value!r}",
            scope=scope, name=name,
        )
    return float(value)


def _tokens(value, what: str, *, call_id: str) -> int | None:
    if value is not None and (type(value) is not int or value < 0):
        raise LedgerRefused(
            f"{what} must be a non-negative integer or None; call {call_id!r} "
            f"was given {value!r}",
            scope="call", name=call_id,
        )
    return value


# ------------------------------------------------------------------ exposure


def exposure(
    connection: sqlite3.Connection,
    *,
    bill: str | None = None,
    assignment: int | None = None,
    month: str | None = None,
) -> float:
    """The money a bill or an assignment has spent or holds, as one sum.

    `run` and `bound` rows of `month` -- every month when `month` is None,
    which is what a budget asks, since an assignment is bounded for its whole
    life and not per calendar month -- plus every `reserved` and `sending`
    row still open, whichever month it was made in. An open reservation
    counts against whichever month asks: a call reserved a minute before the
    month ends and dispatched a minute after is money the new month owes,
    and the old month's bucket cannot hold it from the new month's callers.

    Exactly one of `bill` and `assignment` scopes the sum. This is the one
    place that sums `cost` rows for a limit, for a cap and a budget alike,
    so a call on the wire counts against the budget as it counts against
    the cap.
    """
    if (bill is None) == (assignment is None):
        raise ValueError("exposure takes exactly one of bill= or assignment=")
    return float(_exposure_exact(connection, bill=bill, assignment=assignment, month=month))


def _exposure_exact(
    connection: sqlite3.Connection,
    *,
    bill: str | None,
    assignment: int | None,
    month: str | None,
) -> Decimal:
    """`exposure` as an exact decimal sum of the rows' amounts, for `_over`."""
    column, key = ("bill", bill) if assignment is None else ("assignment", assignment)
    rows = connection.execute(
        f"""
        SELECT usd
          FROM cost
         WHERE {column} = ?
           AND (source IN ('reserved', 'sending')
                OR (source IN ('run', 'bound')
                    AND (? IS NULL OR substr(timestamp, 1, 7) = ?)))
           AND usd IS NOT NULL
        """,
        (key, month, month),
    ).fetchall()
    return sum((_decimal(row[0]) for row in rows), Decimal(0))


# --------------------------------------------------------------- the sweep


def release_orphans(
    connection: sqlite3.Connection,
    *,
    is_alive=alive,
) -> Released:
    """Release what a dead owner left open, on every bill.

    A `reserved` row whose owner pid is dead is deleted: the claim is the
    only road to the wire and it refuses a row that is not `reserved`, so a
    row the claim never moved has spent nothing, and holding the cap for it,
    or turning it into `bound` money the operator reconciles against an
    invoice that never carried it, would both be wrong. A `sending` row
    whose owner is dead settles to `bound` at its full bound: the request may
    have reached the provider, and an attempt that may have been billed is
    never counted as nothing. A row whose owner lives is the owner's to
    settle, however long.

    Run by every reservation, before its own transaction so a refused
    reservation does not roll the sweep back, and exported for `sd usage`. `is_alive` is the process check, replaceable by a test that
    wants a pid to be dead without killing anything.
    """
    deleted: list[str] = []
    bound: list[str] = []
    with transaction(connection):
        rows = connection.execute(
            "SELECT id, call_id, owner_pid, source FROM cost "
            "WHERE source IN ('reserved', 'sending') AND owner_pid IS NOT NULL"
        ).fetchall()
        # By row id and not by call id: a legacy row with a NULL call id
        # matches `call_id = ?` never (NULL equals nothing), and was
        # reported released while left open (sd:965, R11).
        for row in rows:
            if is_alive(int(row["owner_pid"])):
                continue
            if row["source"] == "reserved":
                connection.execute(
                    "DELETE FROM cost WHERE id = ? AND source = 'reserved'",
                    (row["id"],),
                )
                deleted.append(row["call_id"])
            else:
                connection.execute(
                    "UPDATE cost SET source = 'bound' "
                    "WHERE id = ? AND source = 'sending'",
                    (row["id"],),
                )
                bound.append(row["call_id"])
    return Released(deleted=tuple(deleted), bound=tuple(bound))


# ------------------------------------------------------------- the budget


def set_budget(
    connection: sqlite3.Connection,
    assignment: int,
    budget_usd: float | None,
    *,
    registry: Registry | None = None,
) -> None:
    """Put `budget_usd` on an assignment, if the ledger can enforce it.

    Accepted only when every call the assignment can make goes through
    `reserve`: its author a `url` entry, and every reviewer the registry can
    resolve for it a `url` entry too. A `start` entry's session calls the
    vendor itself, so a budget on it would be a number nothing enforces; the
    refusal names the entry. `None` clears the budget and needs no check.

    The registry is the one beside the connection's database unless the
    caller passes one.
    """
    if budget_usd is None:
        update_assignment(connection, assignment, budget_usd=None)
        return
    budget_usd = _money(budget_usd, "a budget", scope="assignment", name=assignment)
    # The check and the write share one write lock: the runner moves an
    # assignment's provider (`update_run`), and a budget accepted against
    # one author must not land on another.
    with transaction(connection):
        row = connection.execute(
            "SELECT provider FROM assignment WHERE id = ?", (assignment,)
        ).fetchone()
        if row is None:
            raise SdDbError(f"no assignment {assignment}")
        if row["provider"] is None:
            raise LedgerRefused(
                f"assignment {assignment} has no provider yet, so the ledger "
                f"cannot tell whether it can stand in front of a budget; set "
                f"the author first",
                scope="assignment", name=assignment, limit=budget_usd,
            )
        if registry is None:
            registry = read_registry(connection=connection)
        author = registry.providers.get(row["provider"])
        if author is None:
            raise LedgerRefused(
                f"assignment {assignment} names provider {row['provider']!r}, "
                f"which the registry at {registry.path} does not name",
                scope="assignment", name=assignment, limit=budget_usd,
            )
        _refuse_spawned(author, registry, role="author", where=f"assignment {assignment}",
                        scope="assignment", name=assignment, limit=budget_usd)
        update_assignment(connection, assignment, budget_usd=budget_usd)


def _refuse_spawned(
    chosen: Provider,
    registry: Registry,
    *,
    role: str,
    where: str,
    scope: str,
    name,
    limit: float,
) -> None:
    """Refuse a budget the ledger cannot stand in front of, naming the entry.

    `chosen` is the entry the row calls in `role`; the reviewers the registry
    orders are the entries it can resolve. The first `start` entry among them
    is the one named: its session calls the vendor itself, so a budget on the
    row would be a number nothing enforces. Every `url` entry means nothing
    to refuse. `where` names the row in the message, an assignment id for one
    written, the items for one about to be.
    """
    spawned = [(chosen, role)] if chosen.kind == "start" else []
    spawned += [(entry, "reviewer") for entry in registry.order("reviewer") if entry.kind == "start"]
    if not spawned:
        return
    entry, role = spawned[0]
    raise LedgerRefused(
        f"budget_usd refused on {where}: its {role} "
        f"{entry.name!r} is a 'start' entry, whose session calls the "
        f"vendor itself. The ledger reserves only for 'url' entries, "
        f"so a budget on it would be a number nothing enforces. Give "
        f"{entry.name!r} a 'url', or leave the budget off.",
        scope=scope, name=name, limit=limit,
    )


def budget_for_selection(
    connection: sqlite3.Connection,
    budget_usd: float | None,
    *,
    role: str,
    items: list[int],
    registry: Registry | None = None,
) -> float | None:
    """`budget_usd` as `enqueue` may write it on the rows it is about to
    create, or the refusal; `None` is no budget and needs no check.

    Only an `author` or a `reviewer` row calls a provider; an `exec` row
    runs a registered command and a `merge` row ships, so a budget on either
    is refused as a number nothing could consume.

    The rows have no provider yet: the runner resolves one when it claims,
    from the registry's order for the role, and takes the first it can
    execute -- today the first `start` entry, whatever `url` entries stand
    before it. So every entry the registry orders for the role is checked,
    not the first alone: a `start` entry anywhere in the order can be the
    one that runs, and the first such is named. A registry that orders none
    is refused too, naming the role and the file: a budget with no entry to
    stand in front of is as unenforced as one on a `start` entry. The
    reviewers the registry orders are checked as `set_budget` checks them.
    The registry is the one beside the connection's database unless the
    caller passes one.

    What this cannot promise: the registry the runner reads at claim time is
    the file then, and an entry that has been given a `start` since is the
    runner's to refuse at dispatch.
    """
    if budget_usd is None:
        return None
    where = "the selection of item" + ("s " if len(items) > 1 else " ") + ", ".join(str(item) for item in items)
    if role not in ("author", "reviewer"):
        raise LedgerRefused(
            f"budget_usd refused on {where}: a {role!r} row calls no provider, "
            f"so there is nothing for a budget to bound",
            scope="assignment", name=None,
        )
    budget_usd = _money(budget_usd, "a budget", scope="assignment", name=None, where=where)
    if registry is None:
        registry = read_registry(connection=connection)
    order = registry.order(role)
    if not order:
        raise LedgerRefused(
            f"budget_usd refused on {where}: the registry at {registry.path} "
            f"orders no {role}, so there is no entry for the ledger to "
            f"stand in front of",
            scope="assignment", name=None, limit=budget_usd,
        )
    # The runner takes the first entry it can execute, so the first `start`
    # entry in the order is the one it would run, not `order[0]`.
    chosen = next((entry for entry in order if entry.kind == "start"), order[0])
    _refuse_spawned(chosen, registry, role=role, where=where,
                    scope="assignment", name=None, limit=budget_usd)
    return budget_usd


# --------------------------------------------------------------- reserve


#: Kept for a reader outside this package: the pack's `sd-review` reads
#: `ledger.MONEY_NOISE` in `capped_bills`. `_over` no longer uses it (sd:1176),
#: and removing the name broke every review on a machine that installed the
#: library. Remove it only after that reader stops using it.
MONEY_NOISE = 1e-9


def _decimal(amount: float) -> Decimal:
    """`amount` as the decimal its shortest `repr` spells.

    That is the amount the caller wrote: `0.1` is stored as the float
    nearest one tenth, and `repr` gives back `0.1`, not the binary
    expansion `Decimal(0.1)` would.
    """
    return Decimal(repr(float(amount)))


def _over(spent: float | Decimal, bound: float, limit: float) -> bool:
    """Whether `spent + bound` exceeds `limit`, compared as money.

    Money is REAL in `cost.usd`, `bill.cap_usd_month` and
    `assignment.budget_usd`, so a float sum is a binary float: `0.1 + 0.2 >
    0.3` is True, and a bound that exactly fills the room was refused
    (sd:965, #406's R4/R5). Each amount is compared as the decimal the
    caller wrote instead, so the sum is exact: a bound that fills the room
    is admitted, and any overage at all is refused. A tolerance admitted
    what sat under it -- rounding to six places admitted `0.2000004` (#423's
    review), and a 1e-9 noise floor admitted `0.2000000005` (sd:1176) --
    and `_money` accepts any finite non-negative float, so the limit holds
    for every amount the API accepts. `spent` is `_exposure_exact`'s sum,
    or a float read from one row.
    """
    held = spent if isinstance(spent, Decimal) else _decimal(spent)
    return held + _decimal(bound) > _decimal(limit)


def _limit(
    connection: sqlite3.Connection,
    *,
    bill: str,
    assignment: int | None,
) -> tuple[float | None, float | None]:
    """The bill's cap and the assignment's budget, each None when unset.

    A store `init` made has no `bill` rows until something reads the
    registry through a writable connection, so a bill with no row is read
    once from the file beside the database, which seeds the rows (sd:965,
    R10); only a bill the file does not name either is `no bill`.
    """
    found = connection.execute(
        "SELECT cap_usd_month FROM bill WHERE name = ?", (bill,)
    ).fetchone()
    if found is None:
        try:
            read_registry(connection=connection)
        except RegistryError as error:
            raise LedgerRefused(
                f"no bill {bill!r}, and the registry could not seed it: {error}",
                scope="bill", name=bill,
            ) from None
        found = connection.execute(
            "SELECT cap_usd_month FROM bill WHERE name = ?", (bill,)
        ).fetchone()
    if found is None:
        raise LedgerRefused(
            f"no bill {bill!r}; the registry beside the database does not name it",
            scope="bill", name=bill,
        )
    budget = None
    if assignment is not None:
        row = connection.execute(
            "SELECT budget_usd FROM assignment WHERE id = ?", (assignment,)
        ).fetchone()
        if row is None:
            raise SdDbError(f"no assignment {assignment}")
        budget = row["budget_usd"]
    return found["cap_usd_month"], budget


def reserve(
    connection: sqlite3.Connection,
    *,
    bill: str,
    bound: float,
    call_id: str,
    owner_pid: int,
    assignment: int | None = None,
    provider: str | None = None,
    role: str | None = None,
    repo: str | None = None,
    pass_: str | None = None,
    now: str | None = None,
    is_alive=alive,
) -> int:
    """Hold `bound` on `bill` for one call, or refuse. Returns the row id.

    First the sweep releases what dead owners left open, on any bill. Then,
    in one `BEGIN IMMEDIATE` transaction: the bill's exposure for the month
    plus this bound must stay within its cap, when it has one; the
    assignment's exposure plus this bound must stay within its `budget_usd`,
    when it has one, on every bill; then one `reserved` row is written with
    the bound, the month, and the owner's pid. Both checks read `exposure`,
    under the write lock, so two callers with room for one cannot both pass.
    The refusal names the bill or the assignment, the money held and the
    limit, and a budget refusal says `budget spent`, which is the note the
    runner writes.

    `call_id` is one attempt: a second reservation under a call id the
    ledger already holds is refused, whatever state that row is in.

    Refused inside a caller's open transaction: `transaction` nests as a
    savepoint there and takes no lock of its own, so the check would read
    under whatever lock the caller holds, which may be none.
    """
    if not isinstance(call_id, str) or not 1 <= len(call_id) <= MAX_CALL_ID:
        raise LedgerRefused(
            f"a call id is a non-empty string of at most {MAX_CALL_ID} "
            f"characters, the row's identity for the claim, the settlement "
            f"and the overshoot report; got "
            + (f"{len(call_id)} characters" if isinstance(call_id, str) else repr(call_id)),
            scope="call", name=call_id,
        )
    if connection.in_transaction:
        raise LedgerRefused(
            f"reserve takes the write lock itself and cannot run inside an "
            f"open transaction; call {call_id!r}",
            scope="call", name=call_id,
        )
    bound = _money(bound, "a bound", scope="call", name=call_id)
    repo = paths.key(repo)
    if type(owner_pid) is not int or owner_pid <= 0:
        raise LedgerRefused(
            f"owner_pid must be a positive process id, the caller's own; call "
            f"{call_id!r} gave {owner_pid!r}",
            scope="call", name=call_id, bound=bound,
        )
    # The sweep commits on its own, before the reservation's transaction: a
    # refusal below rolls that transaction back, and what a dead owner left
    # open must not come back with it.
    release_orphans(connection, is_alive=is_alive)
    with transaction(connection):
        # The moment is taken under the lock: a wait for it that crosses
        # month end checks and records the row in the month it lands in.
        moment = _now() if now is None else stamp(now)
        month = moment[:7]
        held = connection.execute(
            "SELECT source FROM cost WHERE call_id = ?", (call_id,)
        ).fetchone()
        if held is not None:
            raise LedgerRefused(
                f"call {call_id!r} is already in the ledger as {held['source']!r}; "
                f"a call id is one attempt, and a retry is a new call",
                scope="call", name=call_id, bound=bound,
            )
        cap, budget = _limit(connection, bill=bill, assignment=assignment)
        if cap is not None:
            exact = _exposure_exact(connection, bill=bill, assignment=None, month=month)
            spent = float(exact)
            if _over(exact, bound, cap):
                raise LedgerRefused(
                    f"bill {bill!r} has {cap - spent:.2f} of its {cap:.2f} cap "
                    f"left for {month} ({spent:.2f} spent or held) and the "
                    f"call's bound is {bound:.2f}; refused",
                    scope="bill", name=bill, exposure=spent, limit=cap, bound=bound,
                )
        if budget is not None:
            exact = _exposure_exact(connection, bill=None, assignment=assignment, month=None)
            spent = float(exact)
            if _over(exact, bound, budget):
                raise LedgerRefused(
                    f"budget spent: assignment {assignment} has "
                    f"{budget - spent:.2f} of its {budget:.2f} budget left "
                    f"({spent:.2f} spent or held) and the call's bound is "
                    f"{bound:.2f}; refused",
                    scope="assignment", name=assignment, exposure=spent,
                    limit=budget, bound=bound,
                )
        cursor = connection.execute(
            "INSERT INTO cost (call_id, timestamp, provider, bill, role, repo, "
            "assignment, pass, owner_pid, usd, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved')",
            (call_id, moment, provider, bill, role, repo, assignment, pass_,
             owner_pid, bound),
        )
    return int(cursor.lastrowid)


# ------------------------------------------------- claim, settle and lose


def _outside(what: str, connection: sqlite3.Connection, call_id: str) -> None:
    """Refuse `what` inside a caller's open transaction: the ledger's moves
    are each one committed transaction, and a savepoint under a caller's
    rollback would undo a claim after the wire or a settlement after the
    response, leaving the row a state the wire has passed."""
    if connection.in_transaction:
        raise LedgerRefused(
            f"{what} commits its own transaction and cannot run inside an "
            f"open one; call {call_id!r}",
            scope="call", name=call_id,
        )


def _state(connection: sqlite3.Connection, call_id: str) -> str | None:
    row = connection.execute(
        "SELECT source FROM cost WHERE call_id = ?", (call_id,)
    ).fetchone()
    return None if row is None else row["source"]


def claim(connection: sqlite3.Connection, call_id: str, *, now: str | None = None) -> None:
    """`reserved` to `sending`, just before the request goes on the wire.

    One transaction, and it moves only a row that is still `reserved`: a
    caller that paused between reserving and sending, past a sweep that
    released its row, is refused here with no request on the wire, and
    reserves again as a new call. The row takes the month of the attempt,
    which is the month the settlement will carry.

    Refused inside a caller's open transaction, as `reserve` is: nested,
    `transaction` is a savepoint, and a caller that rolls its outer
    transaction back after the request went out would leave the row
    `reserved` for the sweep to delete as money never spent (sd:965, R8).
    """
    _outside("claim", connection, call_id)
    with transaction(connection):
        moment = _now() if now is None else stamp(now)
        moved = connection.execute(
            "UPDATE cost SET source = 'sending', timestamp = ? "
            "WHERE call_id = ? AND source = 'reserved'",
            (moment, call_id),
        ).rowcount
        if moved != 1:
            state = _state(connection, call_id)
            raise LedgerRefused(
                f"call {call_id!r} cannot be claimed: "
                + ("the ledger holds no row for it; its reservation was "
                   "released, and it reserves again as a new call"
                   if state is None else
                   f"its row is {state!r}, not 'reserved'"),
                scope="call", name=call_id,
            )


def settle(
    connection: sqlite3.Connection,
    call_id: str,
    *,
    usd: float,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> bool:
    """`sending` to `run` with the actual cost. Once; a second time is a no-op.

    Returns True when the row moved and False when it was already settled,
    to `run` or to `bound`. A row that is `reserved` was never claimed and
    cannot have cost anything, so settling it is refused rather than
    written; so is a call id the ledger does not hold.

    An actual cost above the row's bound still settles at the actual cost
    -- the vendor's count is the fact -- and files the `cap overshot`
    attention row of the prd's C-59, an attention report under
    `OVERSHOOT_JOB` naming the call, the bound and the actual, in the
    settlement's own transaction: the bound was what the cap admitted, so
    an overshoot is money the cap did not stand in front of, and the
    report is the row that pushes (sd:965, R2). Refused inside a caller's
    open transaction, as `claim` is.
    """
    return _settle(
        connection, call_id, target="run",
        usd=_money(usd, "an actual cost", scope="call", name=call_id),
        tokens_in=_tokens(tokens_in, "tokens_in", call_id=call_id),
        tokens_out=_tokens(tokens_out, "tokens_out", call_id=call_id),
    )


def lose(connection: sqlite3.Connection, call_id: str) -> bool:
    """`sending` to `bound` at the full bound: the response was lost.

    A timeout or a dropped connection after the request went out; the
    provider may have finished and billed it, so the bound stands until the
    operator corrects it against the invoice. Idempotent as `settle` is,
    and refused inside a caller's open transaction as `settle` is.
    """
    return _settle(connection, call_id, target="bound")


def _settle(
    connection: sqlite3.Connection,
    call_id: str,
    *,
    target: str,
    usd: float | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> bool:
    _outside("settle" if target == "run" else "lose", connection, call_id)
    with transaction(connection):
        if target == "run":
            held = connection.execute(
                "SELECT usd FROM cost WHERE call_id = ? AND source = 'sending'",
                (call_id,),
            ).fetchone()
            moved = connection.execute(
                "UPDATE cost SET source = 'run', usd = ?, tokens_in = ?, "
                "tokens_out = ? WHERE call_id = ? AND source = 'sending'",
                (usd, tokens_in, tokens_out, call_id),
            ).rowcount
            if moved == 1 and held is not None and held["usd"] is not None and _over(0.0, usd, float(held["usd"])):
                _overshot(connection, call_id, bound=float(held["usd"]), actual=usd)
        else:
            moved = connection.execute(
                "UPDATE cost SET source = 'bound' "
                "WHERE call_id = ? AND source = 'sending'",
                (call_id,),
            ).rowcount
        if moved == 1:
            return True
        state = _state(connection, call_id)
        if state in ("run", "bound"):
            return False
        raise LedgerRefused(
            f"call {call_id!r} cannot be settled: "
            + ("the ledger holds no row for it" if state is None
               else f"its row is {state!r} and was never claimed, so it "
                    f"cost nothing"),
            scope="call", name=call_id,
        )


def _overshot(connection: sqlite3.Connection, call_id: str, *, bound: float, actual: float) -> None:
    """File the `cap overshot` attention report for one settlement.

    One report per call, its identity `OVERSHOOT_JOB` and the call id, so
    a settlement -- which happens once -- files once; the basis names the
    call too, so two overshoots are two reports with their own numbers
    rather than one folded into the other with the second's amounts lost.
    """
    moment = _now()
    reporting.ingest(
        connection, job=OVERSHOOT_JOB, run_id=call_id, started=moment, ended=moment,
        exit_code=1, attention=True, attention_basis=f"cap overshot on call {call_id}",
        source_path="",
        text=(f"cap overshot: call {call_id} settled at {actual:.6f} USD against a "
              f"bound of {bound:.6f} USD, {actual - bound:.6f} over what the cap "
              f"admitted. The row is `run` at the actual cost; the bound the "
              f"ledger held was the estimate, and the vendor's count is the "
              f"fact.\n"),
    )
