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
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

#: Seconds one HTTP read may take.
TIMEOUT = 20
#: The largest answer read; a bigger one is ReadFailed (sd:9256 review).
MAX_BODY = 1 << 20


def is_zero(value):
    """A success code of 0: the JSON integer, not False or 0.0."""
    return type(value) is int and value == 0
#: How many of the newest rollout files the Codex reader opens, and how
#: much of each one's tail, before it says unknown.
CODEX_FILES = 20
CODEX_TAIL = 4 * 1024 * 1024
#: How far past now a token_count's stamp may be and still read: clock
#: skew. A later stamp would outrank every real reading (sd:9499).
CODEX_SKEW = datetime.timedelta(minutes=5)
USER_AGENT = "agent-meter/1"
#: The longest rate window a reading takes: a year.
MAX_MINUTES = 366 * 24 * 60
#: The longest window label a reply's own name gives (sd:9256 round 8).
LABEL = 100
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
    """A finite JSON number, or ReadFailed naming the field (never its
    value). A numeric string is not a number (sd:9256 review)."""
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


def within(call, seconds):
    """call() within seconds in all, or ReadFailed. The socket timeout bounds
    each wait, not the read: a source that keeps sending (headers or body)
    held the whole run (sd:9256 review). A daemon thread is left behind on
    a timeout; it ends with its socket or with the process."""
    done = {}

    def run():
        try:
            done["value"] = call()
        except BaseException as e:  # handed to the caller below
            done["error"] = e
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise ReadFailed("no whole answer in %gs" % seconds)
    if "error" in done:
        raise done["error"]
    return done["value"]


def read_capped(response, cap=MAX_BODY):
    """The body, or ReadFailed past cap bytes."""
    chunks, size = [], 0
    while chunk := response.read1(65536):
        size += len(chunk)
        if size > cap:
            raise ReadFailed("answer over %d bytes" % cap)
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_json(url, headers=None, timeout=TIMEOUT, cap=MAX_BODY):
    """GET url as JSON, or ReadFailed with a message that holds no body.
    The whole read, headers and body, takes at most timeout seconds."""
    request = urllib.request.Request(url, headers={**(headers or {}), "User-Agent": USER_AGENT})

    def get():
        # Built per call: the proxy handler reads the environment when built.
        with urllib.request.build_opener(SameOrigin).open(request, timeout=timeout) as response:
            return read_capped(response, cap)
    try:
        body = within(get, timeout)
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


def drop_reset(fields, now):
    """Drops each window that reset at or before now: its share is not
    what is used now. Every reader's path applies it, so the live row and
    the meter row agree (sd:9256 round 8). Returns the stale count."""
    windows, kept = fields.get("windows") or [], []
    for entry in windows:
        reset = event_time(entry.get("resets_at"))
        if reset is None or reset > now:
            kept.append(entry)
    fields["windows"] = kept
    return len(windows) - len(kept)


def settled(provider, source, fields, now, **extra):
    """The reading for fields read at extra's read_at: ok, or unknown with
    the reason. A stale window is counted in `stale`. A month's spend
    resets at the next UTC month start (sd:9701)."""
    spend = fields.get("spend")
    if spend and spend.get("period") == "month":
        spend.setdefault("resets_at", iso(next_month(now)))
    stale = drop_reset(fields, now)
    if stale:
        extra["stale"] = stale
    why = unsettled(fields)
    if why and stale and not (fields.get("balance") or fields.get("spend")):
        why = "every window reset after its reading"
    if why:
        extra.pop("read_at", None)
        return reading(provider, "unknown", source, error=why, **extra)
    return reading(provider, "ok", source, **extra, **fields)


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
    # The key's limit caps the month's spend only when it resets monthly;
    # a daily, weekly or lifetime limit is another counter (sd:9701 review).
    if data.get("limit") is not None and data.get("limit_reset") == "monthly":
        out["spend"]["limit"] = number(data["limit"], "limit")
    if data.get("limit_remaining") is not None:
        out["balance"] = {"amount": number(data["limit_remaining"], "limit_remaining"),
                          "unit": "usd"}
    return out


def read_moonshot(env, fetch, now):
    data = fetch("https://api.moonshot.ai/v1/users/me/balance",
                 {"Authorization": "Bearer " + key(env, "MOONSHOT_API_KEY")})
    if (not isinstance(data, dict) or not is_zero(data.get("code"))
            or not isinstance(data.get("data"), dict)):
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
    if (not isinstance(base, dict) or not is_zero(base.get("status_code"))
            or not isinstance(models, list)):
        raise ReadFailed("refused or another shape")
    windows = []
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("model_name"), str):
            raise ReadFailed("a model entry of another shape")
        # The name labels a window and no error: it is the reply's text.
        name = model["model_name"][:LABEL - len(" weekly")]
        # Times are milliseconds; the remaining share is a whole percent.
        for label, start, end, left in (
                (name, "start_time", "end_time", "current_interval_remaining_percent"),
                (name + " weekly", "weekly_start_time", "weekly_end_time",
                 "current_weekly_remaining_percent")):
            begin, finish = number(model.get(start), start), number(model.get(end), end)
            if not begin < finish or not 0 <= number(model.get(left), left) <= 100:
                raise ReadFailed("%s is not a window" % end)
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
    # /v2/user/remaining_quota answers 410 since 2026-11-18; its successor holds the wallet (sd:9415).
    data = fetch("https://api.heygen.com/v3/users/me", {"X-Api-Key": key(env, "HEYGEN_API_KEY")})
    data = data.get("data") if isinstance(data, dict) else None
    if not isinstance(data, dict):
        raise ReadFailed("no data object")
    # A plan account has no wallet, or one in another currency: the
    # meter has no usd balance for it, one plain unknown each read.
    wallet = data.get("wallet")
    if not isinstance(wallet, dict) or wallet.get("currency") != "usd":
        raise ReadFailed("no usd wallet")
    return {"balance": {"amount": number(wallet.get("remaining_balance"), "remaining_balance"),
                        "unit": "usd"}}


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


#: A money amount Baseten sends as a decimal string: "242.95".
DECIMAL = re.compile(r"\d{1,15}(\.\d{1,6})?")
#: A providers list line's optional cap (sd:9701).
CAP = re.compile(r"cap=(%s)" % DECIMAL.pattern)


def month_start(now):
    """The first of now's month in UTC: the providers bill by UTC days."""
    return now.astimezone(datetime.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month(now):
    start = month_start(now)
    return (start + datetime.timedelta(days=32)).replace(day=1)


def read_baseten(env, fetch, now):
    """The month's cost: the sum of each usage kind's `total` (dedicated,
    training, model APIs, ...), a decimal string in usd. Baseten takes
    about 23 s to answer (checked 2026-11-19), past TIMEOUT. Dates are
    UTC days and the end date is exclusive: the next day takes in today."""
    today = now.astimezone(datetime.timezone.utc)
    data = fetch("https://api.baseten.co/v1/billing/usage_summary?start_date=%s&end_date=%s"
                 % (month_start(today).strftime("%Y-%m-%d"),
                    (today + datetime.timedelta(days=1)).strftime("%Y-%m-%d")),
                 {"Authorization": "Api-Key " + key(env, "BASETEN_API_KEY")}, timeout=60)
    # Every top-level value is a usage section, under any name, and counts:
    # one of another shape is no reading, never a total of the rest
    # (sd:9590 lane review).
    kinds = list(data.values()) if isinstance(data, dict) else []
    if not kinds:
        raise ReadFailed("no usage kinds")
    total = 0.0
    for kind in kinds:
        value = kind.get("total") if isinstance(kind, dict) else None
        if not isinstance(value, str) or not DECIMAL.fullmatch(value):
            raise ReadFailed("total is not a decimal string")
        total += float(value)
    return {"spend": {"amount": total, "unit": "usd", "period": "month"}}


def read_langsmith(env, fetch, now):
    """The month's LangChain Standard Units: the sum over billable metrics;
    a metric with no use this month has a null value and no groups."""
    data = fetch("https://api.smith.langchain.com/api/v1/orgs/current/billing/usage"
                 "?starting_on=%s&ending_before=%s" % (iso(month_start(now)), iso(next_month(now))),
                 {"x-api-key": key(env, "LANGSMITH_API_KEY")})
    if not isinstance(data, list) or not data:
        raise ReadFailed("no metrics list")
    total = 0.0
    for metric in data:
        if not isinstance(metric, dict) or "value" not in metric:
            raise ReadFailed("a metric of another shape")
        if metric["value"] is not None:
            total += number(metric["value"], "value")
        elif metric.get("groups"):
            # Use under groups with no value is not a month with no use.
            raise ReadFailed("a null value with groups")
    return {"spend": {"amount": total, "unit": "units", "period": "month"}}


def read_youcom(env, fetch, now):
    """The account's credit, sent in cents (You.com's API reference,
    get-account-balance)."""
    data = fetch("https://api.you.com/v1/billing/account_balance",
                 {"X-API-Key": key(env, "YDC_API_KEY")})
    data = data.get("data") if isinstance(data, dict) else None
    attributes = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(attributes, dict):
        raise ReadFailed("no data.attributes object")
    return {"balance": {"amount": number(attributes.get("balance"), "balance") / 100,
                        "unit": "usd"}}


def read_openwhispr(env, fetch, now):
    """Words used this billing period; a limit of -1 is unlimited, so the
    share and the words left show only on a capped plan."""
    data = fetch("https://api.openwhispr.com/api/v1/usage",
                 {"Authorization": "Bearer " + key(env, "OPENWHISPR_API_KEY")})
    data = data.get("data") if isinstance(data, dict) else None
    if not isinstance(data, dict):
        raise ReadFailed("no data object")
    used = number(data.get("words_used"), "words_used")
    limit = number(data.get("limit"), "limit")
    if used < 0 or (limit < 0 and limit != -1):
        raise ReadFailed("words_used or limit is not a count")
    end = when(data.get("current_period_end"), "current_period_end")
    out = {"spend": {"amount": used, "unit": "words", "period": "billing period"}}
    if end is not None:
        out["spend"]["resets_at"] = end
    if limit > 0:
        # The plan's own limit; a list cap fills in only when it has none.
        out["spend"]["limit"] = limit
        out["windows"] = [window("words", min(100.0, used / limit * 100), None, end)]
        out["balance"] = {"amount": max(0.0, limit - used), "unit": "words"}
    return out
#: A cost report amount: a decimal string of cents, no sign or exponent.
CENTS = re.compile(r"[0-9]{1,15}(?:\.[0-9]{1,12})?")


def read_anthropic_api(env, fetch, now):
    """The month's API cost from the Admin API cost report: one daily
    bucket per day, so a month fits one page of 31 (sd:9594)."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    data = fetch("https://api.anthropic.com/v1/organizations/cost_report?starting_at=%s"
                 "&bucket_width=1d&limit=31" % iso(start),
                 {"x-api-key": key(env, "ANTHROPIC_ADMIN_KEY"), "anthropic-version": "2023-06-01"})
    buckets = data.get("data") if isinstance(data, dict) else None
    if not isinstance(buckets, list) or data.get("has_more") is not False:
        raise ReadFailed("no data list on one page")
    cents = 0.0
    for bucket in buckets:
        results = bucket.get("results") if isinstance(bucket, dict) else None
        if not isinstance(results, list):
            raise ReadFailed("a bucket of another shape")
        for result in results:
            if not isinstance(result, dict) or result.get("currency") != "USD":
                raise ReadFailed("a result of another shape")
            amount = result.get("amount")
            if not isinstance(amount, str) or not CENTS.fullmatch(amount):
                raise ReadFailed("amount is not decimal cents")
            cents += float(amount)
    return {"spend": {"amount": cents / 100, "unit": "usd", "period": "month"}}


#: A GitHub login: what /user may name before it goes into a path.
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


def github_report(env, fetch, now):
    """A reader of the month's usageItems from a per-user billing report.
    They need a token with the `user` scope; /user names the login
    (sd:9592)."""
    headers = {"Authorization": "Bearer " + key(env, "GH_TOKEN"),
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    user = fetch("https://api.github.com/user", headers)
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str) or not GITHUB_LOGIN.fullmatch(login):
        raise ReadFailed("no login")

    def items(path):
        data = fetch("https://api.github.com/users/%s/settings/billing/%s?year=%d&month=%d"
                     % (login, path, now.year, now.month), headers)
        found = data.get("usageItems") if isinstance(data, dict) else None
        if not isinstance(found, list) or not all(isinstance(item, dict) for item in found):
            raise ReadFailed("no usageItems list")
        return found
    return items


def month_spend(items):
    return {"amount": sum(number(item.get("netAmount"), "netAmount") for item in items),
            "unit": "usd", "period": "month"}


def gross(items):
    return sum(number(item.get("grossQuantity"), "grossQuantity") for item in items)


def unless_absent(read, path):
    """read(path), or None when the report answers 404: it does not apply
    to this account. Any other failure stays a failure."""
    try:
        return read(path)
    except ReadFailed as e:
        if str(e) != "HTTP 404":
            raise
        return None


def read_github(env, fetch, now):
    """The month's GitHub bill after discounts: Actions, storage, Copilot."""
    return {"spend": month_spend(github_report(env, fetch, now)("usage"))}


def read_copilot(env, fetch, now):
    """The month's Copilot use and what it cost past the plan's allowance.

    Personal plans on AI credits bill in the AI-credit report, in
    `credits`; the premium-request report misses their use and overage
    (sd:9592 review). An older plan bills premium requests, in
    `requests`. No answer names the plan, so the AI-credit report is read
    first and the premium-request one only when it is empty or absent;
    the fallback must read."""
    read = github_report(env, fetch, now)
    ai = unless_absent(read, "ai_credit/usage")
    if ai:
        if any(item.get("unitType") != "ai-credits" for item in ai):
            raise ReadFailed("an AI-credit item in another unit")
        return {"spend": month_spend(ai), "credits": gross(ai)}
    # An empty AI-credit report does not establish the plan: a fallback
    # that cannot be read, a 404 too, is unknown, never zero (sd:9592).
    premium = read("premium_request/usage")
    if premium:
        return {"spend": month_spend(premium), "requests": gross(premium)}
    if ai is None:
        return {"spend": month_spend([]), "requests": 0.0}
    return {"spend": month_spend([]), "credits": 0.0}


READERS = {
    "openrouter": read_openrouter,
    "moonshot": read_moonshot,
    "deepinfra": read_deepinfra,
    "minimax": read_minimax,
    "firecrawl": read_firecrawl,
    "heygen": read_heygen,
    "runway": read_runway,
    "openai": read_openai,
    "baseten": read_baseten,
    "langsmith": read_langsmith,
    "youcom": read_youcom,
    "openwhispr": read_openwhispr,
    "github": read_github,
    "copilot": read_copilot,
    "anthropic-api": read_anthropic_api,
}

#: Providers with a key in the environment and no usage API a key can call
#: (checked 2026-11-17): Gemini and Groq bill in their consoles only,
#: TypeSafe shows usage in its console only.
NO_SOURCE = ("gemini", "groq", "typesafe")

#: Providers whose reading a caller passes in, as `usage --huggingface-error`:
#: the Hugging Face token has no quota endpoint, so the ZeroGPU error text
#: is the source, and a live run leaves the newest row standing (sd:9586).
PASSED = ("huggingface",)

#: The 'usage providers' list a database that never held the row starts with.
STARTING = "\n".join(["claude", "codex", *READERS, *NO_SOURCE, *PASSED]) + "\n"

#: ZeroGPU's quota error: "... GPU quota (60s requested vs. 0s left)".
ZEROGPU = re.compile(r"\((\d{1,9})s requested vs\. (\d{1,9})s left\)")


def huggingface_error(text, now):
    """The huggingface reading in a ZeroGPU quota error: the seconds left,
    read now; None for a text that holds no such error (sd:9586)."""
    found = ZEROGPU.search(text or "")
    if not found:
        return None
    return settled("huggingface", "error",
                   {"balance": {"amount": float(found.group(2)), "unit": "seconds"}},
                   now, read_at=iso(now))


def read_provider(name, env, fetch, now, cap=None):
    """One keyed provider's reading; claude and codex have their own. cap,
    the list line's cap=N, is the spend's limit when the answer has none."""
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
    if cap is not None and fields.get("spend"):
        fields["spend"].setdefault("limit", cap)
    return settled(name, "api", fields, now, read_at=iso(now))


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
              if account.get("active") is True and is_number(account.get("pct"))][:1]
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
    return settled("claude", "cswap", fields, now, read_at=account["fetchedAt"],
                   accounts=accounts)


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


#: What `codex app-server` is sent: the handshake, then one usage read.
CODEX_ASK = b"".join(json.dumps(m).encode() + b"\n" for m in (
    {"id": 1, "method": "initialize",
     "params": {"clientInfo": {"name": "agent-meter", "version": "1"}}},
    {"method": "initialized"},
    {"id": 2, "method": "account/rateLimits/read",
     "params": {"excludeResetCreditDetails": True}}))


def codex_app_server(binary):
    """The result of `account/rateLimits/read` from `binary app-server`,
    within TIMEOUT seconds, or ReadFailed. The server asks the account
    live, so an idle Codex reads current (sd:9655). No reply text reaches
    the error: an error reply gives its code only."""
    try:
        proc = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise ReadFailed(type(e).__name__) from None

    def talk():
        proc.stdin.write(CODEX_ASK)
        proc.stdin.flush()
        while line := proc.stdout.readline(MAX_BODY + 1):
            if len(line) > MAX_BODY:
                raise ReadFailed("answer over %d bytes" % MAX_BODY)
            message = decode(line)
            if isinstance(message, dict) and message.get("id") == 2:
                return message
        raise ReadFailed("app-server closed with no answer")
    try:
        message = within(talk, TIMEOUT)
    except OSError as e:
        raise ReadFailed(type(e).__name__) from None
    finally:
        proc.kill()
        proc.wait()
    if "error" in message:
        code = (message["error"] or {}).get("code") if isinstance(message["error"], dict) else None
        raise ReadFailed("app-server error %s" % code if is_number(code) else "app-server error")
    result = message.get("result")
    if not isinstance(result, dict):
        raise ReadFailed("app-server result of another shape")
    return result


def codex_live_limits(result):
    """The account's RateLimitSnapshot of a rateLimits/read result, in the
    rollout's snake_case so codex_fields reads both. The `codex` bucket of
    rateLimitsByLimitId first, else rateLimits; a model's bucket is never
    the account's."""
    buckets = result.get("rateLimitsByLimitId")
    snap = buckets.get("codex") if isinstance(buckets, dict) else None
    snap = snap if snap is not None else result.get("rateLimits")
    if not isinstance(snap, dict):
        raise ReadFailed("rateLimits is not an object")
    limits = {"limit_id": snap.get("limitId")}
    if not account_wide(limits):
        raise ReadFailed("no account-wide bucket")
    for name in ("primary", "secondary"):
        entry = snap.get(name)
        limits[name] = {"used_percent": entry.get("usedPercent"),
                        "window_minutes": entry.get("windowDurationMins"),
                        "resets_at": entry.get("resetsAt")} if isinstance(entry, dict) else entry
    credits = snap.get("credits")
    limits["credits"] = {"has_credits": credits.get("hasCredits"),
                         "unlimited": credits.get("unlimited"),
                         "balance": credits.get("balance")} if isinstance(credits, dict) else credits
    return limits


def codex_reading(home=None, now=None, binary=None):
    """codex live from `binary app-server`, read at `now` (sd:9655); with
    no binary or no live answer, from the session rollouts, the live
    failure in `live_error`."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if not binary:
        live_error = "no codex binary"
    else:
        try:
            fields = codex_fields(codex_live_limits(codex_app_server(binary)))
            return settled("codex", "codex-app-server", fields, now, read_at=iso(now))
        except ReadFailed as e:
            live_error = str(e)
        except (AttributeError, TypeError, ValueError, OverflowError) as e:
            live_error = "another shape: %s" % type(e).__name__
    got = codex_rollout_reading(home, now)
    got["live_error"] = live_error
    return got


def codex_rollout_reading(home=None, now=None):
    """codex from the newest account-wide `token_count` with `rate_limits`,
    by the event's own time, across the newest rollouts. A model's own
    bucket (another `limit_id`) is never the account's reading.

    `read_at` is that event's time, so a Codex left idle reads old and the
    view can call it stale rather than current. A file's mtime picks the
    rollouts to read, never the reading: a copied rollout keeps an old
    mtime, so every picked file's tail is read and event time alone
    decides (sd:9256 lane review). A window that reset before `now`, the
    clock when omitted, is dropped. An event stamped more than CODEX_SKEW
    after `now` is skipped and counted in `future` (sd:9499)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    root = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    try:
        files = sorted(root.glob("*/*/*/rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:CODEX_FILES]
    except OSError as e:
        return reading("codex", "unknown", "codex-sessions", error=type(e).__name__)
    best = None  # (time, stamp, rate_limits) of the newest account-wide event
    future = 0
    for path in files:
        try:
            with open(path, "rb") as f:
                f.seek(max(0, path.stat().st_size - CODEX_TAIL))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in lines:
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
                if at > now + CODEX_SKEW:
                    future += 1
                    continue
            except (ValueError, KeyError, TypeError, AttributeError, ReadFailed):
                continue
            if best is None or at > best[0]:
                best = (at, event["timestamp"], limits)
    counted = {"future": future} if future else {}
    if best is None and future:
        return reading("codex", "unknown", "codex-sessions", **counted,
                       error="every account-wide token_count is stamped after now")
    if best is None:
        return reading("codex", "unknown", "codex-sessions",
                       error="no account-wide token_count in the newest %d rollouts" % CODEX_FILES)
    # The newest snapshot decides: one that does not read is unknown, never
    # an older one published as current (sd:9256 review).
    try:
        fields = codex_fields(best[2])
    except ReadFailed as e:
        return reading("codex", "unknown", "codex-sessions", error=str(e), **counted)
    except (AttributeError, TypeError, ValueError, OverflowError) as e:
        return reading("codex", "unknown", "codex-sessions", **counted,
                       error="another shape: %s" % type(e).__name__)
    return settled("codex", "codex-sessions", fields, now, read_at=best[1], **counted)


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
            # The one numeric string: Codex's balance is Option<String>.
            if not isinstance(balance, str):
                raise ReadFailed("credits.balance is not a string")
            try:
                amount = float(balance)
            except ValueError:
                raise ReadFailed("credits.balance is not a number") from None
            out["balance"] = {"amount": number(amount, "credits.balance"), "unit": "credits"}
    return out


def parse_providers(text):
    """The list's (name, cap) entries in order, the first of a name only,
    and one problem per line that is not one. A line is a name and an
    optional `cap=N`, N a positive decimal: the spend's limit where the
    provider has no read API for it (sd:9701)."""
    entries, problems = [], []
    for number_, line in enumerate((text or "").splitlines(), 1):
        words = line.split("#", 1)[0].split(None, 1)
        if not words:
            continue
        name, rest = words[0], words[1].strip() if len(words) > 1 else ""
        cap = CAP.fullmatch(rest)
        if not NAME.fullmatch(name) or rest and not rest.startswith("cap="):
            problems.append("line %d: not a provider name" % number_)
        elif rest and not (cap and float(cap.group(1)) > 0):
            problems.append("line %d: cap=N wants N a positive decimal" % number_)
        elif name not in [known for known, _ in entries]:
            entries.append((name, float(cap.group(1)) if rest else None))
    return entries, problems

