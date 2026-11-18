"""Usage readers for `agent-meter.py usage` and the ledger (system sd:9256).

One reader per provider, each turning the provider's own usage or balance
answer into one reading:

    {"provider", "status", "source", "read_at", "windows", "balance",
     "spend", "error"}

`status` is `ok`, `unknown` (the read failed: no key, an HTTP error, a
timeout, an answer of another shape) or `no-source` (the provider offers
no usage API this job can call). A failed read is never `ok` and never 0.

Keys come from the environment by name only. No key, response body or
account field reaches a reading or an error: an error is the HTTP status,
the exception's class or the missing variable's name.
"""

import datetime
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

#: Seconds one HTTP read may take.
TIMEOUT = 20
#: How many of the newest rollout files the Codex reader opens, and how
#: much of each one's tail, before it says unknown.
CODEX_FILES = 20
CODEX_TAIL = 4 * 1024 * 1024
USER_AGENT = "agent-meter/1"
#: The longest rate window a reading takes: a year.
MAX_MINUTES = 366 * 24 * 60
#: A provider or service name in a Settings list.
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class ReadFailed(Exception):
    """A read that gives no answer: the message is safe to store."""


def is_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def number(value, what):
    """A finite number, or ReadFailed naming the field (never its value)."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise ReadFailed("%s is not a number" % what) from None
    if not is_number(value):
        raise ReadFailed("%s is not a number" % what)
    return float(value)


def decode(text):
    """JSON text as a value, or ReadFailed. The one decoder for every answer,
    feed, rollout line and cswap reply: a body nested past the parser's depth
    raises RecursionError, which stopped the whole run (sd:9256 review)."""
    try:
        return json.loads(text)
    except (RecursionError, ValueError, MemoryError):
        raise ReadFailed("unparseable answer") from None


def unsettled(fields):
    """Why fields are not an ok reading, or None. The one rule for ok: it
    carries a window, a balance or a spend, and finite JSON only, so no sum
    or share that overflowed to inf is ever stored (sd:9256 review)."""
    if not (fields.get("windows") or fields.get("balance") or fields.get("spend")):
        return "no usage in the answer"
    try:
        json.dumps(fields, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return "a value that is not finite JSON"
    return None


def iso(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_epoch(seconds, what):
    try:
        return iso(datetime.datetime.fromtimestamp(number(seconds, what), datetime.timezone.utc))
    except (OverflowError, OSError, ValueError):
        raise ReadFailed("%s is not a time" % what) from None


def origin(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or {"http": 80, "https": 443}.get(parts.scheme)


class SameOrigin(urllib.request.HTTPRedirectHandler):
    """Follows a redirect within its origin only: urllib sends every header,
    the key too, on to wherever a redirect points (sd:9256 review)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(newurl) != origin(req.full_url):
            fp.close()
            raise ReadFailed("redirect to another origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def when(value, what):
    """A time field a source may leave null: None, or an aware ISO-8601
    string kept as sent. Anything else is ReadFailed: a time that does not
    parse never resets or closes anything (sd:9256 review)."""
    if value is None:
        return None
    if not isinstance(value, str) or event_time(value) is None:
        raise ReadFailed("%s is not a time" % what)
    return value


def fetch_json(url, headers=None, timeout=TIMEOUT):
    """GET url as JSON, or ReadFailed with a message that holds no body."""
    request = urllib.request.Request(url, headers={**(headers or {}), "User-Agent": USER_AGENT})
    try:
        # Built per call: the proxy handler reads the environment when built.
        with urllib.request.build_opener(SameOrigin).open(request, timeout=timeout) as response:
            body = response.read()
    except ReadFailed:
        raise
    except urllib.error.HTTPError as e:
        e.close()
        raise ReadFailed("HTTP %d" % e.code) from None
    except Exception as e:  # URLError, timeout, TLS: the class says enough
        raise ReadFailed(type(getattr(e, "reason", None) or e).__name__) from None
    return decode(body)


def key(env, name):
    value = env.get(name)
    if not value:
        raise ReadFailed("%s not set" % name)
    return value


def reading(provider, status, source, **fields):
    out = {"provider": provider, "status": status, "source": source, "read_at": None,
           "windows": [], "balance": None, "spend": None}
    out.update(fields)
    return out


def window(name, used, minutes, resets_at):
    return {"name": name, "used_percent": used, "window_minutes": minutes, "resets_at": resets_at}


# ---------------------------------------------------------------- readers
# Each takes (env, fetch, now) and returns the reading's fields, or raises
# ReadFailed. `fetch` is fetch_json, swapped in tests.


def read_openrouter(env, fetch, now):
    data = fetch("https://openrouter.ai/api/v1/key",
                 {"Authorization": "Bearer " + key(env, "OPENROUTER_API_KEY")})
    data = data.get("data") if isinstance(data, dict) else None
    if not isinstance(data, dict):
        raise ReadFailed("no data object")
    out = {"spend": {"amount": number(data.get("usage_monthly"), "usage_monthly"),
                     "unit": "usd", "period": "month"}}
    if data.get("limit_remaining") is not None:
        out["balance"] = {"amount": number(data["limit_remaining"], "limit_remaining"),
                          "unit": "usd"}
    return out


def read_moonshot(env, fetch, now):
    data = fetch("https://api.moonshot.ai/v1/users/me/balance",
                 {"Authorization": "Bearer " + key(env, "MOONSHOT_API_KEY")})
    if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(data.get("data"), dict):
        raise ReadFailed("refused or another shape")
    return {"balance": {"amount": number(data["data"].get("available_balance"),
                                         "available_balance"), "unit": "usd"}}


def read_deepinfra(env, fetch, now):
    month = now.strftime("%Y.%m")
    data = fetch("https://api.deepinfra.com/payment/usage?from=" + month,
                 {"Authorization": "Bearer " + key(env, "DEEPINFRA_API_KEY")})
    months = data.get("months") if isinstance(data, dict) else None
    if not isinstance(months, list):
        raise ReadFailed("no months list")
    found = [m for m in months if isinstance(m, dict) and m.get("period") == month]
    if not found:
        raise ReadFailed("no row for this month")
    # `total_cost` is in cents (DeepInfra's openapi.json, UsageMonth).
    return {"spend": {"amount": number(found[0].get("total_cost"), "total_cost") / 100,
                      "unit": "usd", "period": "month"}}


def read_minimax(env, fetch, now):
    data = fetch("https://api.minimax.io/v1/api/openplatform/coding_plan/remains",
                 {"Authorization": "Bearer " + key(env, "MINIMAX_API_KEY")})
    base = data.get("base_resp") if isinstance(data, dict) else None
    models = data.get("model_remains") if isinstance(data, dict) else None
    if not isinstance(base, dict) or base.get("status_code") != 0 or not isinstance(models, list):
        raise ReadFailed("refused or another shape")
    windows = []
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("model_name"), str):
            raise ReadFailed("a model entry of another shape")
        name = model["model_name"]
        # Times are milliseconds; the remaining share is a whole percent.
        for label, start, end, left in (
                (name, "start_time", "end_time", "current_interval_remaining_percent"),
                (name + " weekly", "weekly_start_time", "weekly_end_time",
                 "current_weekly_remaining_percent")):
            begin, finish = number(model.get(start), start), number(model.get(end), end)
            if not begin < finish or not 0 <= number(model.get(left), left) <= 100:
                raise ReadFailed("%s is not a window" % label)
            windows.append(window(label, 100 - number(model.get(left), left),
                                  int((finish - begin) // 60000),
                                  from_epoch(finish / 1000, end)))
    return {"windows": windows}


def read_firecrawl(env, fetch, now):
    data = fetch("https://api.firecrawl.dev/v2/team/credit-usage",
                 {"Authorization": "Bearer " + key(env, "FIRECRAWL_API_KEY")})
    data = data.get("data") if isinstance(data, dict) else None
    if not isinstance(data, dict):
        raise ReadFailed("no data object")
    left = number(data.get("remainingCredits"), "remainingCredits")
    plan = number(data.get("planCredits"), "planCredits")
    out = {"balance": {"amount": left, "unit": "credits"}}
    if plan > 0:
        out["windows"] = [window("credits", max(0.0, (plan - left) / plan * 100), None,
                                 when(data.get("billingPeriodEnd"), "billingPeriodEnd"))]
    return out


def read_heygen(env, fetch, now):
    data = fetch("https://api.heygen.com/v2/user/remaining_quota",
                 {"X-Api-Key": key(env, "HEYGEN_API_KEY")})
    data = data.get("data") if isinstance(data, dict) else None
    if not isinstance(data, dict):
        raise ReadFailed("no data object")
    return {"balance": {"amount": number(data.get("remaining_quota"), "remaining_quota"),
                        "unit": "quota"}}


def read_runway(env, fetch, now):
    data = fetch("https://api.dev.runwayml.com/v1/organization",
                 {"Authorization": "Bearer " + key(env, "RUNWAYML_API_SECRET"),
                  "X-Runway-Version": "2024-11-06"})
    if not isinstance(data, dict):
        raise ReadFailed("another shape")
    return {"balance": {"amount": number(data.get("creditBalance"), "creditBalance"),
                        "unit": "credits"}}


def read_openai(env, fetch, now):
    """The month's API cost. The costs endpoint takes an admin key only."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    data = fetch("https://api.openai.com/v1/organization/costs?limit=31&start_time=%d"
                 % int(start.timestamp()),
                 {"Authorization": "Bearer " + key(env, "OPENAI_ADMIN_KEY")})
    buckets = data.get("data") if isinstance(data, dict) else None
    if not isinstance(buckets, list):
        raise ReadFailed("no data list")
    total = 0.0
    for bucket in buckets:
        # A bucket with no results list is not a day with no spend.
        results = bucket.get("results") if isinstance(bucket, dict) else None
        if not isinstance(results, list):
            raise ReadFailed("a bucket of another shape")
        for result in results:
            amount = result.get("amount") if isinstance(result, dict) else None
            if not isinstance(amount, dict):
                raise ReadFailed("a result of another shape")
            total += number(amount.get("value"), "amount.value")
    return {"spend": {"amount": total, "unit": "usd", "period": "month"}}


READERS = {
    "openrouter": read_openrouter,
    "moonshot": read_moonshot,
    "deepinfra": read_deepinfra,
    "minimax": read_minimax,
    "firecrawl": read_firecrawl,
    "heygen": read_heygen,
    "runway": read_runway,
    "openai": read_openai,
}

#: Providers with a key in the environment and no usage API a key can call
#: (checked 2026-11-17): Gemini and Groq bill in their consoles only, the
#: Hugging Face token has no billing endpoint, TypeSafe and You.com show
#: usage in their consoles only. Baseten (`/v1/billing/usage_summary`) and
#: LangSmith (`/api/v1/orgs/current/billing/usage`) answer, but in units
#: the Usage view does not draw yet.
NO_SOURCE = ("gemini", "groq", "huggingface", "typesafe", "youcom", "baseten", "langsmith")

#: The 'usage providers' list a database that never held the row starts with.
STARTING = "\n".join(["claude", "codex", *READERS, *NO_SOURCE]) + "\n"


def read_provider(name, env, fetch, now):
    """One keyed provider's reading; claude and codex have their own."""
    if name in NO_SOURCE:
        return reading(name, "no-source", "none")
    reader = READERS.get(name)
    if reader is None:
        return reading(name, "unknown", "none", error="no reader for this name")
    try:
        fields = reader(env, fetch, now)
    except ReadFailed as e:
        return reading(name, "unknown", "api", error=str(e))
    except (AttributeError, TypeError, ValueError, OverflowError) as e:
        # One provider's answer never costs the run (sd:9256 review).
        return reading(name, "unknown", "api", error="another shape: %s" % type(e).__name__)
    why = unsettled(fields)
    if why:
        return reading(name, "unknown", "api", error=why)
    return reading(name, "ok", "api", read_at=iso(now), **fields)


def claude_reading(accounts, error, now):
    """claude from `cswap list --json`'s account entries (agent-meter's).

    `read_at` is when cswap fetched the active account's usage, not this
    run: cswap answers from its cache (sd:9256 review). An active window
    with no fetch time is unknown; no active window gives no read_at."""
    if error is not None:
        return reading("claude", "unknown", "cswap", error=error)
    # The active account's seven days is the provider's window, as the
    # meter row has always had it; every account stays in `accounts`.
    active = [account for account in accounts
              if account.get("active") and is_number(account.get("pct"))][:1]
    if not active:
        # An empty list, or an active account with no usage yet, is no
        # reading: ok needs data (sd:9256 review).
        return reading("claude", "unknown", "cswap", accounts=accounts,
                       error="no active account with a seven-day share")
    account = active[0]
    if event_time(account.get("fetchedAt")) is None:
        return reading("claude", "unknown", "cswap", accounts=accounts,
                       error="the active account has no fetch time")
    try:
        # cswap leaves resetsAt out when Anthropic sends none; a value
        # that is not a time is not a reset (sd:9256 review).
        resets = when(account.get("resetsAt"), "resetsAt")
    except ReadFailed as e:
        return reading("claude", "unknown", "cswap", accounts=accounts, error=str(e))
    fields = {"windows": [window("seven_day", account["pct"], 10080, resets)]}
    why = unsettled(fields)
    if why:
        return reading("claude", "unknown", "cswap", accounts=accounts, error=why)
    return reading("claude", "ok", "cswap", read_at=account["fetchedAt"], accounts=accounts,
                   **fields)


def event_time(stamp):
    """An event's aware time, or None."""
    try:
        moment = datetime.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def account_wide(limits):
    """The account's own bucket: Codex reads a missing `limit_id` as
    "codex" and shows that one first (app-server account_processor.rs)."""
    return limits.get("limit_id") in (None, "codex")


def codex_reading(home=None):
    """codex from the newest account-wide `token_count` with `rate_limits`,
    by the event's own time, across the newest rollouts. A model's own
    bucket (another `limit_id`) is never the account's reading.

    `read_at` is that event's time, so a Codex left idle reads old and the
    view can call it stale rather than current. A file's mtime orders the
    reads only: a rollout is not newer than its last write, so the scan
    stops at a file last written before the newest event found."""
    root = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    try:
        files = sorted(root.glob("*/*/*/rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:CODEX_FILES]
    except OSError as e:
        return reading("codex", "unknown", "codex-sessions", error=type(e).__name__)
    best = None  # (time, stamp, rate_limits) of the newest account-wide event
    for path in files:
        try:
            if best is not None and path.stat().st_mtime < best[0].timestamp():
                break
            with open(path, "rb") as f:
                f.seek(max(0, path.stat().st_size - CODEX_TAIL))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"token_count"' not in line or '"rate_limits"' not in line:
                continue
            try:
                event = decode(line)
                payload = event["payload"]
                limits = payload.get("rate_limits")
                if payload.get("type") != "token_count" or not isinstance(limits, dict):
                    continue
                if not account_wide(limits):
                    continue
                at = event_time(event.get("timestamp"))
                if at is None:
                    continue  # an event with no time has no place in the order
            except (ValueError, KeyError, TypeError, AttributeError, ReadFailed):
                continue
            # The file's last account-wide event is its newest one.
            if best is None or at > best[0]:
                best = (at, event["timestamp"], limits)
            break
    if best is None:
        return reading("codex", "unknown", "codex-sessions",
                       error="no account-wide token_count in the newest %d rollouts" % CODEX_FILES)
    # The newest snapshot decides: one that does not read is unknown, never
    # an older one published as current (sd:9256 review).
    try:
        fields = codex_fields(best[2])
    except ReadFailed as e:
        return reading("codex", "unknown", "codex-sessions", error=str(e))
    except (AttributeError, TypeError, ValueError, OverflowError) as e:
        return reading("codex", "unknown", "codex-sessions",
                       error="another shape: %s" % type(e).__name__)
    why = unsettled(fields)
    if why:
        return reading("codex", "unknown", "codex-sessions", error=why)
    return reading("codex", "ok", "codex-sessions", read_at=best[1], **fields)


def codex_fields(limits):
    """Codex's RateLimitSnapshot (codex-rs protocol.rs). primary, secondary
    and credits may be null, and so may a window's window_minutes and
    resets_at and the credits' balance: a null drops only what needs it.
    used_percent is a number (it can pass 100), has_credits and unlimited
    are booleans. Any other type or value is ReadFailed."""
    windows = []
    for name in ("primary", "secondary"):
        entry = limits.get(name)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise ReadFailed("%s is not an object" % name)
        minutes = entry.get("window_minutes")
        if minutes is not None and (not isinstance(minutes, int) or isinstance(minutes, bool)
                                    or not 0 < minutes <= MAX_MINUTES):
            raise ReadFailed("%s.window_minutes is not minutes" % name)
        resets = entry.get("resets_at")
        windows.append(window(name, number(entry.get("used_percent"), name + ".used_percent"),
                              minutes,
                              None if resets is None else from_epoch(resets, name + ".resets_at")))
    out = {"windows": windows}
    credits = limits.get("credits")
    if credits is not None:
        if not (isinstance(credits, dict) and isinstance(credits.get("has_credits"), bool)
                and isinstance(credits.get("unlimited"), bool)):
            raise ReadFailed("credits of another shape")
        balance = credits.get("balance")
        if credits["has_credits"] and not credits["unlimited"] and balance is not None:
            out["balance"] = {"amount": number(balance, "credits.balance"), "unit": "credits"}
    return out


def parse_providers(text):
    """The list's names in order, and one problem per line that is not one."""
    names, problems = [], []
    for number_, line in enumerate((text or "").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not NAME.fullmatch(line):
            problems.append("line %d: not a provider name" % number_)
        elif line not in names:
            names.append(line)
    return names, problems

