"""Service status readers for `agent-meter.py services` (system sd:9259).

The 'status services' list holds one service per line:

    <name> <kind> <url> [provider[,provider...]]

`kind` is one of KINDS. Each read becomes one reading:

    {"name", "kind", "url", "providers", "status", "indicator", "incidents",
     "components", "read_at", "error"}

`status` is `ok`, `degraded`, `outage` or `unknown`. A read that fails (no
answer, an HTTP error, an answer of another shape) is `unknown`, never `ok`:
the job cannot tell its own network from the service's. A collection of
another shape, or an entry in it that is not an object, is another shape
too. Every incident and component field is a string of at most TITLE
characters, or None; an incident url is a whole http(s) URL of at most
URL characters, or None.
"""

import urllib.error
import urllib.parse
import urllib.request

import json

from usage_sources import (NAME, TIMEOUT, USER_AGENT, ReadFailed, event_time, fetch_json, iso,
                           when, within)

KINDS = ("statuspage", "slack", "google-incidents", "http")
#: The longest incident title kept; Google's descriptions run to pages.
TITLE = 200
#: The google-incidents feeds are whole histories (about 290 KB each on
#: 2026-11-17, growing with each incident); other feeds take MAX_BODY.
GOOGLE_MAX_BODY = 16 << 20
#: The longest incident url kept; a longer one is dropped, never cut.
URL = 2048

#: The 'status services' list a database that never held the row starts
#: with (operator 2026-11-17, notes #59922 and #59926; feeds checked then).
#: LangSmith's summary.json answers with every field null, so it is `http`.
#: Suno has no status feed, so `http` checks suno.com (sd:9903).
STARTING = """\
claude statuspage https://status.claude.com/api/v2/summary.json claude
openai statuspage https://status.openai.com/api/v2/summary.json codex,openai
google-cloud google-incidents https://status.cloud.google.com/incidents.json gemini
github statuspage https://www.githubstatus.com/api/v2/summary.json copilot
tailscale statuspage https://status.tailscale.com/api/v2/summary.json
google-workspace google-incidents https://www.google.com/appsstatus/dashboard/incidents.json
slack slack https://slack-status.com/api/v2.0.0/current
notion statuspage https://www.notion-status.com/api/v2/summary.json
atlassian statuspage https://status.atlassian.com/api/v2/summary.json
docker-hub statuspage https://www.dockerstatus.com/api/v2/summary.json
cloudflare statuspage https://www.cloudflarestatus.com/api/v2/summary.json
strongdm statuspage https://status.strongdm.com/api/v2/summary.json
heygen statuspage https://status.heygen.com/api/v2/summary.json heygen
groq statuspage https://groqstatus.com/api/v2/summary.json groq
baseten statuspage https://status.baseten.co/api/v2/summary.json baseten
runway statuspage https://status.runwayml.com/api/v2/summary.json runway
langsmith http https://status.smith.langchain.com langsmith
minimax statuspage https://status.minimax.io/api/v2/summary.json minimax
youcom statuspage https://status.you.com/api/v2/summary.json youcom
deepinfra http https://status.deepinfra.com deepinfra
openrouter http https://status.openrouter.ai openrouter
huggingface http https://status.huggingface.co huggingface
firecrawl http https://status.firecrawl.dev firecrawl
moonshot http https://platform.moonshot.ai moonshot
typesafe http https://status.typesafe.ai typesafe
suno http https://suno.com
"""

#: Earlier STARTING lists: a database that still holds one unedited moves to
#: STARTING, so an added service reaches it; an edited list stays as the
#: operator wrote it (sd:9903 review). Add the old text here when STARTING
#: changes.
EARLIER = (STARTING.replace("suno http https://suno.com\n", ""),)

#: A statuspage indicator as a status; anything else is unknown.
INDICATOR = {"none": "ok", "minor": "degraded", "maintenance": "degraded",
             "major": "outage", "critical": "outage"}
#: The values each feed documents for a field that decides a status. Any
#: other value, or another type, reads unknown (sd:9256 review).
INCIDENT_OPEN = ("investigating", "identified", "monitoring")
INCIDENT_CLOSED = ("resolved", "postmortem")
COMPONENT = ("operational", "degraded_performance", "partial_outage", "major_outage",
             "under_maintenance")
SLACK_TYPE = ("incident", "notice", "outage")
#: Slack's incident status: `current` can still list a resolved one, and
#: planned maintenance is scheduled, completed or cancelled (sd:9256 round 8).
SLACK_OPEN = ("active",)
SLACK_DONE = ("resolved", "completed", "cancelled", "scheduled")
GOOGLE_IMPACT = ("SERVICE_INFORMATION", "SERVICE_DISRUPTION", "SERVICE_OUTAGE")


def one_of(value, allowed, what):
    if not isinstance(value, str) or value not in allowed:
        raise ReadFailed("%s is not one the feed documents" % what)
    return value


def shown_time(value):
    """A time shown and never decided on: kept if it parses, else None."""
    return value if isinstance(value, str) and event_time(value) is not None else None


def web_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # a bad port raises here, not at the read
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def parse_services(text):
    """[(name, kind, url, providers)] in order, and one problem per refused line."""
    services, problems, seen = [], [], set()
    for number, line in enumerate((text or "").splitlines(), 1):
        fields = line.split("#", 1)[0].split()
        if not fields:
            continue
        if len(fields) not in (3, 4):
            problems.append("line %d: want <name> <kind> <url> [provider]" % number)
            continue
        name, kind, url = fields[:3]
        providers = fields[3].split(",") if len(fields) == 4 else []
        if not NAME.fullmatch(name) or name in seen:
            problems.append("line %d: name is not a new lower-case name" % number)
        elif kind not in KINDS:
            problems.append("line %d: kind is not one of %s" % (number, ", ".join(KINDS)))
        elif not web_url(url):
            problems.append("line %d: url is not http(s)" % number)
        elif not all(NAME.fullmatch(p) for p in providers):
            problems.append("line %d: a provider is not a lower-case name" % number)
        else:
            seen.add(name)
            services.append((name, kind, url, providers))
    return services, problems


def title(text):
    return " ".join(text.replace("*", "").split())[:TITLE] if isinstance(text, str) else ""


def short(value):
    """A stored field: a string cut to TITLE, or None."""
    return value[:TITLE] if isinstance(value, str) else None


def whole_url(value):
    """A link shown as is: a whole http(s) URL of at most URL characters,
    or None. A cut URL is a broken link (sd:9256 review)."""
    if (not isinstance(value, str) or len(value) > URL
            or any(c <= " " or c == "\x7f" for c in value)):
        return None
    return value if web_url(value) else None


def link(base, uri):
    try:
        return whole_url(urllib.parse.urljoin(base, uri)) if isinstance(uri, str) else None
    except ValueError:
        return None


def entries(value, what):
    """A list of objects, or ReadFailed: a strange entry is not no incident."""
    if not isinstance(value, list) or not all(isinstance(entry, dict) for entry in value):
        raise ReadFailed("%s of another shape" % what)
    return value


def read_statuspage(url, fetch):
    data = fetch(url)
    status = data.get("status") if isinstance(data, dict) else None
    indicator = status.get("indicator") if isinstance(status, dict) else None
    if indicator not in INDICATOR:
        raise ReadFailed("no status indicator")
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    base = page.get("url") if isinstance(page.get("url"), str) else ""
    incidents = []
    # Some pages (OpenAI, Tailscale, ...) leave `incidents` out when none is
    # open; a key that is there must still be a list of objects.
    for incident in entries(data["incidents"] if "incidents" in data else [], "incidents"):
        if one_of(incident.get("status"), INCIDENT_OPEN + INCIDENT_CLOSED,
                  "an incident status") in INCIDENT_CLOSED:
            continue
        url = incident.get("shortlink")
        if not isinstance(url, str) and base and isinstance(incident.get("id"), str):
            url = base.rstrip("/") + "/incidents/" + incident["id"]
        incidents.append({"title": title(incident.get("name")), "url": whole_url(url),
                          "impact": short(incident.get("impact")),
                          "started_at": shown_time(incident.get("created_at"))})
    components = [{"name": title(c.get("name")), "status": c["status"]}
                  for c in entries(data.get("components"), "components")
                  if one_of(c.get("status"), COMPONENT, "a component status") != "operational"]
    return {"status": INDICATOR[indicator], "indicator": indicator,
            "incidents": incidents, "components": components}


def read_slack(url, fetch):
    data = fetch(url)
    state = data.get("status") if isinstance(data, dict) else None
    active = data.get("active_incidents") if isinstance(data, dict) else None
    if state not in ("ok", "active") or not isinstance(active, list):
        raise ReadFailed("no status")
    incidents = [{"title": title(i.get("title")), "url": whole_url(i.get("url")),
                  "impact": one_of(i.get("type"), SLACK_TYPE, "an incident type"),
                  "started_at": shown_time(i.get("date_created"))}
                 for i in entries(active, "active_incidents")
                 if one_of(i.get("status"), SLACK_OPEN + SLACK_DONE,
                           "an incident status") in SLACK_OPEN]
    status = "ok"
    if incidents or (state == "active" and not active):
        status = "outage" if any(i["impact"] == "outage" for i in incidents) else "degraded"
    return {"status": status, "indicator": state, "incidents": incidents, "components": []}


def read_google(url, fetch):
    data = fetch(url, cap=GOOGLE_MAX_BODY)
    incidents = []
    for incident in entries(data, "incident list"):
        # The fields the open-or-closed call reads are checked first: only
        # a time closes an incident; any other end is unknown (sd:9256).
        if when(incident.get("end"), "an incident end") is not None:
            continue
        if when(incident.get("begin"), "an incident begin") is None:
            raise ReadFailed("an open incident with no begin")
        desc = incident.get("external_desc")
        desc = desc.strip().splitlines() if isinstance(desc, str) else []
        # The summary's first line is a heading ("**Summary:**"); take the line after.
        lines = [line for line in desc if line.strip() and not line.strip().startswith("**Summary")]
        service = title(incident.get("service_name"))
        if not (service or lines):
            raise ReadFailed("an open incident with no title")
        incidents.append({"title": title("%s: %s" % (service, lines[0] if lines else "")),
                          "url": link(url, incident.get("uri")),
                          "impact": one_of(incident.get("status_impact"), GOOGLE_IMPACT,
                                           "an incident impact"),
                          "started_at": incident["begin"]})
    status = "ok"
    if incidents:
        status = "outage" if any(i["impact"] == "SERVICE_OUTAGE" for i in incidents) else "degraded"
    return {"status": status, "indicator": None, "incidents": incidents, "components": []}


def read_http(url, fetch=None, timeout=TIMEOUT):
    """The page answers: 2xx and 3xx are ok, 5xx an outage, the rest unknown."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    def status():
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    try:
        code = within(status, timeout)
    except ReadFailed:
        raise
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
    except Exception as e:
        raise ReadFailed(type(getattr(e, "reason", None) or e).__name__) from None
    if code >= 500:
        return {"status": "outage", "indicator": "HTTP %d" % code, "incidents": [], "components": []}
    if code >= 400:
        raise ReadFailed("HTTP %d" % code)
    return {"status": "ok", "indicator": "HTTP %d" % code, "incidents": [], "components": []}


READERS = {"statuspage": read_statuspage, "slack": read_slack,
           "google-incidents": read_google, "http": read_http}


def read_service(service, now, fetch=fetch_json, http=read_http):
    name, kind, url, providers = service
    out = {"name": name, "kind": kind, "url": url, "providers": providers,
           "status": "unknown", "indicator": None, "incidents": [], "components": [],
           "read_at": iso(now)}
    try:
        got = http(url) if kind == "http" else READERS[kind](url, fetch)
        # The one rule for a status: one of the three, in finite JSON.
        if got.get("status") not in ("ok", "degraded", "outage"):
            raise ReadFailed("no status in the answer")
        json.dumps(got, allow_nan=False)
        out.update(got)
    except ReadFailed as e:
        out["error"] = str(e)
    except (AttributeError, TypeError, ValueError, OverflowError, RecursionError) as e:
        out["error"] = "another shape: %s" % type(e).__name__
    return out
