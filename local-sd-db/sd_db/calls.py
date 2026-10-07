"""One `url` call: reserve, claim, one request on the wire, settle or lose.

Every call the library makes for a `url` entry goes through `call`, and
nothing else in the system puts such a request on the wire. That is what
lets the ledger be a ledger: the reservation is taken before the request,
the claim is the only road to the wire, and the row is settled from the
response the same function read, so no caller -- not `sd-review`, not the
runner -- inserts a cost row of its own (clause 15.7), and a review pass is
charged once, in the bill's total and the assignment's alike (15.6).

The order is the prd's (requirement 6): `release_orphans`, then `reserve`
at the bound, then `claim`, then exactly one HTTP POST, then `settle` with
the usage the response carries, or `lose` when the response cannot cost the
call. The bound is the prompt's estimated tokens at the entry's input price
plus the entry's `max_tokens` at its output price, per million tokens. The
estimate is three bytes of UTF-8 to a token -- the pack has no estimator of
its own to port, so this is the assumption, stated here and in
`BYTES_PER_TOKEN`. It errs high on purpose: the bound is what the call may
cost, and the settlement corrects it from the vendor's own count.

What is `run` and what is `bound`. A response whose body carries `usage`
with integer `prompt_tokens` and `completion_tokens` settles the row to
`run` at the actual cost, whatever its HTTP status -- a 429 or a 500 after
the model answered is still a real count. Everything else settles to
`bound`: a timeout, a dropped connection, a body that is not JSON, an HTTP
error whose body carries no usage, and a cost the ledger cannot hold (two
finite prices and two capped counts can still multiply to `inf`).

**A redirect is `bound` whatever its body says.** It is refused and never
followed, so no model ran and no tokens were generated; a `usage` object in
a 3xx body is a number from a host nobody named, and billing it is the one
way "whatever its HTTP status" was wrong. The result reports no counts
either: they were never a model's. The ledger cannot tell from such an answer
whether the provider billed the attempt, and the prd's rule is that an
attempt that may have been billed is never counted as nothing; the usage
screen lists `bound` rows for the operator to correct against the invoice.

Zero retries. urllib makes none of its own, `_NoRedirect` turns a 3xx into
a reported answer rather than a second request to a host nobody named, and
this function sends once: a lost response is `lose`, and a caller that
tries again does so as a new call with a new id and a fresh reservation.
`tests/test_calls.py` counts the requests a local server receives to hold
that line.

Refused by name, before any reservation: a `start` entry, a cleartext
`http://` URL that is not loopback, an entry whose key variable is unset
-- unless it points at loopback and declares no variable, where the socket
already restricts the recipient to this user -- and, when the bill is
capped or the assignment carries a budget, an entry without `price.in`,
`price.out` or `max_tokens`, because a bound the library cannot compute is
a limit it cannot hold. On an uncapped bill with no budget a missing price
is a price of zero and the row records no money.

A refused reservation is the ledger's `LedgerRefused`, raised through with
its `scope`, `exposure`, `limit` and `bound`, so the runner can end its row
`blocked` with a `budget spent` note and the amount without parsing text.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping

from .errors import SdDbError
from .ledger import claim, lose, reserve, settle
from .registry import Provider, Registry, read as read_registry

#: The OpenAI-compatible endpoint every `url` entry answers on, as the pack's
#: `sd_registry.endpoint` builds it.
CHAT_COMPLETIONS = "/chat/completions"
AUTH_SCHEME = "Bearer"
#: A body past this is not read further and cannot cost the call.
MAX_RESPONSE_BYTES = 2_000_000
#: The pack's `sd-review` default per provider, in seconds; the registry
#: carries no per-entry timeout (measured: `Provider` has no such field).
DEFAULT_TIMEOUT = 1800
#: The estimate's ratio. An assumption, not a measurement of any tokenizer.
#: Three, not four, so the input side errs high: on 2026-10-07 three calls ran
#: up to 6% more input tokens than four bytes a token predicted (3.77 bytes a
#: token) and overshot their bounds (sd:1016, sd:1799, sd:1804).
BYTES_PER_TOKEN = 3
#: The largest token count a usage field is believed at.
MAX_TOKENS_FIELD = 10**12
#: The wire: `(request, timeout) -> (status, body)`, raising `OSError` when
#: no response came.
Transport = Callable[[urllib.request.Request, float], tuple[int, bytes]]


class CallRefused(SdDbError):
    """A call the library will not make, naming the entry. Nothing was
    reserved and nothing went on the wire."""

    def __init__(self, message: str, *, entry: str) -> None:
        super().__init__(message)
        self.entry = entry


@dataclass(frozen=True)
class CallResult:
    """What one attempt came to. `outcome` is the row's state, `run` or
    `bound`; `usd` is the settled cost for `run` and None for `bound`;
    `reason` says why a `bound` row is bound and is empty for `run`.

    `tokens_in` and `tokens_out` are the vendor's counts when the body
    carried readable ones, whatever the outcome. A `bound` row can therefore
    carry counts: the answer is what could not be priced, not what could not
    be read, and the operator correcting the row against the invoice wants
    the numbers the response gave."""

    call_id: str
    row: int
    outcome: str
    bound: float
    usd: float | None
    tokens_in: int | None
    tokens_out: int | None
    http_status: int | None
    body: str
    reason: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a second recipient nobody named, and urllib follows one
    by default, carrying `Authorization` to whichever host the answer points
    at. `None` turns the 3xx into a reported `HTTPError`."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

#: For a recipient on this machine. `build_opener` installs a `ProxyHandler`
#: that reads `HTTP_PROXY` at import, and urllib's bypass list does not
#: special-case loopback: on a box where `HTTP_PROXY` is set and `NO_PROXY`
#: omits `localhost`, a loopback request is forwarded to the proxy and the
#: prompt leaves the machine. An empty mapping installs no proxy at all, so
#: the socket goes where the URL says. Public hosts keep `_OPENER`, because
#: a proxy is how they are reachable at all on such a box.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def estimate_tokens(prompt: str) -> int:
    """The prompt's tokens, estimated at `BYTES_PER_TOKEN` bytes each."""
    return len(prompt.encode("utf-8")) // BYTES_PER_TOKEN


def _price(entry: Provider, side: str) -> float | None:
    """The entry's price per million on `side`, or None when the file gives
    none the library can use: not a number, negative, or an int too large
    for a float (`registry.parse` does not validate prices)."""
    value = entry.price.get(side)
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        priced = float(value)
    except OverflowError:
        return None
    return priced if math.isfinite(priced) else None


def _usd(entry: Provider, tokens_in: int, tokens_out: int) -> float:
    """`tokens_in` and `tokens_out` at the entry's prices per million.

    Multiplied as the decimals the registry wrote, then made a float once:
    float arithmetic turns three tokens at $0.10/M into
    3.0000000000000004e-7, which the ledger's exact compare refuses against
    a limit of 3e-7 (sd:1176). A product past the float ceiling is `inf`,
    which `call` refuses by name.
    """
    total = Decimal(0)
    for tokens, side in ((tokens_in, "in"), (tokens_out, "out")):
        price = _price(entry, side)
        if tokens and price:
            total += tokens * Decimal(repr(price))
    return float(total / 1_000_000)


def bound_for(entry: Provider, prompt: str) -> float:
    """The most this call can cost: the estimate at the input price plus
    `max_tokens` at the output price, per million. Missing parts are zero;
    `call` refuses them by name where a limit depends on the bound."""
    return _usd(entry, estimate_tokens(prompt), _max_tokens(entry) or 0)


def _max_tokens(entry: Provider) -> int | None:
    """The entry's `max_tokens` when it is a positive int no larger than
    `MAX_TOKENS_FIELD`, else None: the one predicate the bound, the refusal
    and the request body all read.

    The ceiling is the same one `usage_of` believes a vendor's count at, and
    it is here because `registry.parse` validates no `max_tokens` at all. A
    value such as `10**1000` is an int and is positive, so it reached
    `bound_for`'s multiplication by a float and raised `OverflowError` --
    out of a function documented to answer with `CallRefused` or
    `LedgerRefused`, before anything was reserved and before the caller was
    told which entry was at fault. Above the ceiling the entry now has no
    usable `max_tokens`, which is the missing-part case `call` already
    refuses by name.
    """
    value = entry.max_tokens
    return value if type(value) is int and 0 < value <= MAX_TOKENS_FIELD else None


def loopback_url(url: str) -> bool:
    """Whether this URL's host is the machine the library runs on."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def loopback(entry: Provider) -> bool:
    """Whether this entry's recipient is the machine the library runs on.

    Parse the host; never match a `127.` prefix. `127.evil.com` and
    `127.0.0.1.evil.com` are public DNS names, and an abbreviated `127.1` is
    refused too: curl accepts it, but a second address parser here is a
    second chance to disagree about what counts as this machine.

    One parse, read by all three rules that care where a `url` entry points:
    the cleartext rule below, the key rule in `call`, and the opener `_send`
    picks. The pair that disagrees is the pair that sends a prompt somewhere
    unintended.
    """
    return loopback_url(str(entry.url))


def cleartext(entry: Provider) -> str | None:
    """Why this URL is refused, or None. `https` and loopback hosts pass;
    anything else would carry the key and the prompt in the clear."""
    parts = urllib.parse.urlsplit(str(entry.url))
    if parts.scheme == "https" or (parts.scheme == "http" and loopback(entry)):
        return None
    return (
        f"{entry.name} points at {entry.url!r}, which reaches {parts.netloc!r} "
        f"in the clear; the library sends a key and a prompt only over "
        f"'https' or to a loopback host"
    )


def endpoint(entry: Provider) -> str:
    return f"{str(entry.url).rstrip('/')}{CHAT_COMPLETIONS}"


def _send(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """The urllib path: one request, no retry, no redirect. An HTTP error is
    an answer with a status and a body; a timeout or a dropped connection
    raises `OSError` for the caller to record as lost."""
    opener = _DIRECT_OPENER if loopback_url(request.full_url) else _OPENER
    try:
        with opener.open(request, timeout=timeout) as answer:
            status = getattr(answer, "status", None)
            body = answer.read(MAX_RESPONSE_BYTES + 1)
            return (status if type(status) is int else 0), body
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read(MAX_RESPONSE_BYTES + 1)


def usage_of(body: bytes) -> tuple[int, int] | None:
    """`(prompt_tokens, completion_tokens)` when the body is a JSON object
    whose `usage` carries both as sane integers, else None."""
    if len(body) > MAX_RESPONSE_BYTES:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = []
    for key in ("prompt_tokens", "completion_tokens"):
        value = usage.get(key)
        if type(value) is not int or not 0 <= value <= MAX_TOKENS_FIELD:
            return None
        counts.append(value)
    return counts[0], counts[1]


#: What strict mode accepts as `json_schema.name`.
SCHEMA_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: Keywords strict mode rejects. The operator's ruling on sd:1827 drops these
#: from the copy sent; the caller's own parser keeps checking them.
STRICT_DROPS = frozenset({"minLength", "maxItems"})


#: Keywords whose value maps names to schemas: a property named `maxItems`
#: is a name there, not a keyword, and stays.
NAMED_SCHEMAS = frozenset({"properties", "patternProperties", "$defs", "definitions"})


def strict_schema(schema: Any) -> Any:
    """A copy of `schema` without the keywords strict mode rejects."""
    if isinstance(schema, Mapping):
        return {key: ({name: strict_schema(sub) for name, sub in value.items()}
                      if key in NAMED_SCHEMAS and isinstance(value, Mapping) else strict_schema(value))
                for key, value in schema.items() if key not in STRICT_DROPS}
    if isinstance(schema, list):
        return [strict_schema(value) for value in schema]
    return schema


def call(
    connection: sqlite3.Connection,
    *,
    entry: Provider,
    prompt: str,
    environ: Mapping[str, str],
    assignment: int | None = None,
    pass_: str | None = None,
    role: str | None = None,
    repo: str | None = None,
    call_id: str | None = None,
    now: str | None = None,
    timeout: float | None = None,
    registry: Registry | None = None,
    transport: Transport | None = None,
    owner_pid: int | None = None,
    response_schema: Mapping[str, Any] | None = None,
    schema_name: str = "response",
) -> CallResult:
    """Make one call for `entry`, charged through the ledger. See the module.

    `transport` is the wire, `(request, timeout) -> (status, body)`, raising
    `OSError` for a response that never came; the default is urllib with no
    redirect and no retry. `registry` is the one beside the connection's
    database unless given; it answers whether the bill is capped. `call_id`
    is minted when not given, and is one attempt: a second call is a new id.
    Raises `CallRefused` for an entry it will not call and `LedgerRefused`
    for a reservation the ledger will not hold, both before the wire.

    `response_schema` is the JSON schema the answer must follow, named
    `schema_name`. It goes on the wire only for an entry whose
    `response_format` opts in (sd:1827); for any other entry the body is the
    same as without it.
    """
    if entry.kind != "url":
        raise CallRefused(
            f"{entry.name} is a 'start' entry; its session calls the vendor "
            f"itself and the library makes calls for 'url' entries only",
            entry=entry.name,
        )
    refusal = cleartext(entry)
    if refusal is not None:
        raise CallRefused(refusal, entry=entry.name)
    variable = entry.env[0] if entry.env else ""
    key = environ.get(variable, "") if variable else ""
    # A server on this machine is already restricted to processes running as
    # this user, so a key adds nothing the socket did not require. Demanding
    # one kept every such entry uncallable: the registry has no way to spell
    # "this recipient authenticates nobody", and a placeholder value exported
    # to satisfy the check would be a secret in name only. An entry that does
    # declare a variable still has to supply its value, loopback or not: a
    # server configured to check a key must not be called without one.
    if not key and not (loopback(entry) and not entry.env):
        raise CallRefused(
            f"{entry.name} has no value for {variable or 'any key variable'}; "
            f"the entry's 'env' names the variable the key is read from",
            entry=entry.name,
        )
    seconds = DEFAULT_TIMEOUT if timeout is None else timeout
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise CallRefused(
            f"timeout must be a finite number of seconds above zero; the call "
            f"for {entry.name} was given {timeout!r}",
            entry=entry.name,
        )
    if registry is None:
        registry = read_registry(connection=connection)
    bill = registry.bills.get(entry.bill)
    budgeted = False
    if assignment is not None:
        found = connection.execute(
            "SELECT budget_usd FROM assignment WHERE id = ?", (assignment,)
        ).fetchone()
        if found is None:
            raise SdDbError(f"no assignment {assignment}")
        budgeted = found["budget_usd"] is not None
    if (bill is not None and bill.capped) or budgeted:
        missing = [
            name for name, present in (
                ("price.in", _price(entry, "in") is not None),
                ("price.out", _price(entry, "out") is not None),
                ("max_tokens", _max_tokens(entry) is not None),
            ) if not present
        ]
        if missing:
            because = " and ".join(
                why for why, holds in ((f"its bill {entry.bill!r} is capped", bill is not None and bill.capped),
                                       (f"assignment {assignment} carries a budget", budgeted)) if holds)
            raise CallRefused(
                f"{entry.name} has no {', '.join(missing)}, and {because}; the "
                f"bound the ledger holds needs price.in, price.out and max_tokens",
                entry=entry.name,
            )
    call_id = uuid.uuid4().hex if call_id is None else call_id
    bound = bound_for(entry, prompt)
    pid = os.getpid() if owner_pid is None else owner_pid
    body_fields: dict[str, Any] = {
        "model": entry.model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if _max_tokens(entry) is not None:
        # Omitted when the entry sets none the bound could use: a JSON null,
        # a `-1` or a `"lots"` is not "no limit" to every OpenAI-compatible
        # endpoint, and what the bound treated as missing does not go on
        # the wire either.
        body_fields["max_tokens"] = entry.max_tokens
    if entry.thinking is not None:
        body_fields["thinking"] = {"type": entry.thinking}
    if entry.reasoning_effort is not None:
        body_fields["reasoning_effort"] = entry.reasoning_effort
    if entry.response_format == "json_schema" and response_schema is not None:
        if not isinstance(response_schema, Mapping) or not SCHEMA_NAME.fullmatch(str(schema_name)):
            raise CallRefused(
                f"{entry.name} needs a response_schema mapping and a schema_name of 1 to 64 "
                f"letters, digits, '_' or '-'; nothing was reserved and nothing went on the wire",
                entry=entry.name,
            )
        body_fields["response_format"] = {"type": "json_schema", "json_schema": {
            "name": schema_name, "strict": True, "schema": strict_schema(response_schema)}}
    # The request is built **before** the reservation, and that order is the
    # fix rather than the taste. `registry.parse` validates neither the
    # endpoint nor the body values, so `json.dumps` and `Request` can still
    # raise on an entry this far in -- a value no JSON encoder accepts, a url
    # with no host. Built after `reserve`, such a raise left a `reserved` row
    # holding this call's bound against the bill with nothing on the wire to
    # settle it, released only by a later caller's orphan sweep. Built here,
    # nothing is reserved and the refusal names the entry, which is this
    # function's documented way to decline a call it will not make.
    try:
        request = urllib.request.Request(
            endpoint(entry),
            data=json.dumps(body_fields).encode("utf-8"),
            headers={**({"Authorization": f"{AUTH_SCHEME} {key}"} if key else {}),
                     "Content-Type": "application/json"},
            method="POST",
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise CallRefused(
            f"{entry.name} does not make a request: {error}; nothing was reserved "
            f"and nothing went on the wire",
            entry=entry.name,
        ) from None
    # The sweep, then the reservation: `reserve` runs `release_orphans`
    # itself before its own transaction, so a refusal here rolls back no
    # release. What reaches the caller is the ledger's refusal, with its
    # scope, exposure, limit and bound on it.
    row = reserve(
        connection, bill=entry.bill, bound=bound, call_id=call_id, owner_pid=pid,
        assignment=assignment, provider=entry.name, role=role, repo=repo,
        pass_=pass_, now=now,
    )
    # The claim is the only road to the wire: it refuses a row a sweep
    # released, and nothing is sent when it does.
    claim(connection, call_id, now=now)
    send = _send if transport is None else transport
    try:
        status, body = send(request, seconds)
    except OSError as error:
        # A timeout and a dropped connection are both `OSError` (urllib's
        # `URLError` included); the provider may have finished and billed.
        lose(connection, call_id)
        return CallResult(call_id, row, "bound", bound, None, None, None, None, "",
                          f"response lost: {error}")
    except Exception:
        # Anything else the transport raises after the claim is a fault in
        # the transport, not an answer; the request may still have gone, so
        # the row is bound before the fault reaches the caller, never left
        # `sending` for a live owner that will not settle it.
        lose(connection, call_id)
        raise
    text = body.decode("utf-8", "replace")
    usage = usage_of(body)
    # The redirect test sits outside the usage test, and that is the whole of
    # it: a 3xx is the one status where nothing generated the tokens a body
    # might claim. The request was not followed, so no model ran, and a
    # `usage` object in a redirect body is a number from a host nobody named.
    # Inside the `usage is None` arm it settled such a body to `run` and
    # billed it. Every other status can carry a real count -- a 429 or a 500
    # after the model answered -- which is why "whatever its HTTP status"
    # still holds for them.
    if usage is None or 300 <= status < 400:
        lose(connection, call_id)
        if 300 <= status < 400:
            reason = f"HTTP {status}, a redirect the client does not follow"
        elif len(body) > MAX_RESPONSE_BYTES:
            reason = "response exceeds the byte limit and carries no readable usage"
        else:
            reason = f"HTTP {status} with no usage in the body"
        return CallResult(call_id, row, "bound", bound, None, None, None, status, text, reason)
    prompt_tokens, completion_tokens = usage
    usd = _usd(entry, prompt_tokens, completion_tokens)
    if not math.isfinite(usd):
        # Both prices are finite -- `_price` refuses anything else -- and the
        # counts are capped at `MAX_TOKENS_FIELD`, and the product still
        # overflows to `inf` at a price near the float ceiling. `ledger._money`
        # refuses a non-finite amount, so `settle` raised `LedgerRefused` out
        # of `call` with the row left `sending` and the response already in
        # hand: the one state this function never leaves a row in. The attempt
        # may have been billed and cannot be priced, which is exactly what
        # `bound` records.
        lose(connection, call_id)
        return CallResult(call_id, row, "bound", bound, None, prompt_tokens, completion_tokens, status, text,
                          f"usage at this entry's prices is not a finite cost; {entry.name} is priced too high "
                          f"to settle {prompt_tokens} in and {completion_tokens} out")
    settle(connection, call_id, usd=usd, tokens_in=prompt_tokens, tokens_out=completion_tokens)
    return CallResult(call_id, row, "run", bound, usd, prompt_tokens, completion_tokens, status, text, "")
