"""Notice what arrives on shared mail aliases, and who is waiting on whom.

The Drive walker answers "what document landed". This answers the question that
actually costs something when it goes unanswered: who wrote, about what, and
whether the last word was theirs. Over the fourteen days before this was
written, the two aliases it was built for carried more than 25 messages across
at least 9 threads.

**It reads Gmail from a shell script, not from an agent session.** The repo's
notes said Gmail had no shell path and needed a Claude session; `local-notify`
disproves that every time it mails a digest. The Google workspace MCP server
listens on loopback and speaks JSON-RPC over HTTP, so `curl` reaches it and so
does this. That is not a detail: thirteen agent-driven cron jobs died on
2026-09-19 when the Claude session expired, and a plain command job is immune
to that failure entirely.

**It never sends, replies, files or labels.** The only tools it calls are
`search_gmail_messages` and `get_gmail_messages_content_batch`, and it asks for
headers rather than bodies. The rule it was built under is that reading mail is
allowed and anything leaving the machine is the owner's call.

**Nothing it writes goes in a repository.** Mail headers carry real names and
addresses, and those stay out of git. The conf lives in the config folder,
<config>/mail-intake/mail-intake.conf, and state lives in the state directory,
and nowhere else.

**One optional signal leaves the machine, and only the subject line does.**
`report` can ask Jev whether a thread's newest message asks for a decision or
an action, and use the answer to order the report. It runs unless
`jev enabled JEV_MAIL_INTAKE` declines, it never changes
`waiting_on` and never drops a thread, and the payload is a subject line and
the word inbound or outbound. Sending an address to a third party would be
worse than committing one, so nothing else is sent. See `state_for_jev`.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import system_tools_config  # noqa: E402

# The folder name minus `local-`: the conf is <config>/mail-intake/mail-intake.conf.
TOOL = "mail-intake"

EXIT_FOUND = 0
EXIT_NONE = 3
EXIT_ERROR = 1

THREAD_COLUMNS = ("thread_id", "last_message_id", "last_at", "seen_at")
ARRIVAL_COLUMNS = (
    "detected_at", "thread_id", "message_id", "alias", "subject",
    "who", "direction", "waiting_on", "sent_at", "reported_at",
)

DEFAULT_URL = "http://127.0.0.1:8083/mcp"

# The server fans a batch out as concurrent Gmail calls, and Gmail answers the
# overflow with 429 "Too many concurrent requests for user." At 25 that lost
# eight to fifteen of 104 messages per run, a different set each time.
BATCH = 10
RETRIES = 4     # attempts at a throttled id before this run gives up on it
BACKOFF = 2.0   # seconds before the first retry, doubled each attempt


@dataclass(frozen=True)
class Config:
    account: str
    me: set[str]
    aliases: dict[str, str]
    window_days: int


def parse_config(text: str) -> tuple[Config | None, list[str]]:
    """Read the conf. Pipe-separated, same shape as local-drive-intake's.

        account|<the Google account the MCP server is authorised for>
        me|<an address that counts as the owner, repeatable>
        alias|<label>|<address>
        window|<days of history a fetch looks back over>
    """
    account = ""
    me: set[str] = set()
    aliases: dict[str, str] = {}
    window = 21
    problems: list[str] = []

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        kind = parts[0]
        if kind == "account" and len(parts) == 2:
            account = parts[1]
        elif kind == "me" and len(parts) == 2:
            me.add(parts[1].lower())
        elif kind == "alias" and len(parts) == 3:
            aliases[parts[1]] = parts[2].lower()
        elif kind == "window" and len(parts) == 2:
            try:
                window = int(parts[1])
            except ValueError:
                problems.append(f"line {number}: window is not a number")
        else:
            problems.append(f"line {number}: cannot read {line!r}")

    if not account:
        problems.append("no account row: the MCP server needs to know whose mailbox")
    if not aliases:
        problems.append("no alias rows: nothing to watch")
    if not me:
        problems.append("no me rows: every message would look like it needs a reply")
    if problems:
        return None, problems
    # The account is always "me": a reply the owner sent is not waiting on them.
    me.add(account.lower())
    return Config(account, me, aliases, window), []


# --------------------------------------------------------------- mcp client


class McpError(RuntimeError):
    pass


class Mcp:
    """The smallest JSON-RPC client that reaches the workspace MCP server.

    Deliberately not a dependency. `local-notify` does the same three calls in
    fifteen lines of curl, and matching that keeps this module installable on a
    machine with nothing but Python and the server already running.
    """

    def __init__(self, url: str, timeout: int = 60):
        self.url = url
        self.timeout = timeout
        self.session: str | None = None
        self._id = 0

    def _post(self, body: dict, want_headers: bool = False):
        self._id += 1
        body.setdefault("jsonrpc", "2.0")
        if "method" in body and body.get("id") is None and "id" not in body:
            body["id"] = self._id
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            # The server speaks both; without this header it refuses with a 406.
            "Accept": "application/json, text/event-stream",
        }
        if self.session:
            headers["mcp-session-id"] = self.session
        request = urllib.request.Request(self.url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8", "replace")
                if want_headers:
                    return payload, dict(response.headers)
                return payload, {}
        except urllib.error.URLError as error:
            raise McpError(f"cannot reach {self.url}: {error}") from error

    def connect(self) -> None:
        payload, headers = self._post({
            "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "mail-intake", "version": "1.0"}},
        }, want_headers=True)
        self.session = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        if not self.session:
            raise McpError("workspace-mcp gave no session id")
        self._post({"method": "notifications/initialized"})

    def call(self, name: str, arguments: dict) -> str:
        """One tools/call. Returns the text payload the server produced."""
        payload, _ = self._post({
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        # The transport may answer as server-sent events; the JSON is the last
        # `data:` line either way.
        text = payload
        if "data:" in payload:
            lines = [l[5:].strip() for l in payload.splitlines() if l.startswith("data:")]
            text = lines[-1] if lines else payload
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise McpError(f"{name}: unparseable reply: {error}") from error
        if "error" in parsed:
            raise McpError(f"{name}: {parsed['error']}")
        content = parsed.get("result", {}).get("content", [])
        out = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if parsed.get("result", {}).get("isError"):
            raise McpError(f"{name}: {out[:300]}")
        return out


# ------------------------------------------------------------------ parsing


ID_RE = re.compile(r"Message ID:\s*(\S+)")
THREAD_RE = re.compile(r"Thread ID:\s*(\S+)")
ADDRESS_RE = re.compile(r"<([^>]+)>")


def parse_search(text: str) -> list[tuple[str, str]]:
    """(message_id, thread_id) pairs from a search result.

    The server returns prose with the ids embedded rather than JSON, so this
    reads the two labelled lines and ignores everything else. Pairs are taken
    positionally within each numbered block.
    """
    pairs: list[tuple[str, str]] = []
    current: str | None = None
    for line in text.splitlines():
        found = ID_RE.search(line)
        if found and "Thread" not in line:
            current = found.group(1)
            continue
        found = THREAD_RE.search(line)
        if found and current:
            pairs.append((current, found.group(1)))
            current = None
    return pairs


def parse_metadata(text: str) -> list[dict]:
    """Header blocks from a batch fetch, split on the server's `---` divider."""
    out: list[dict] = []
    for block in text.split("\n---\n"):
        if "Message ID:" not in block:
            continue
        fields: dict[str, str] = {}
        key = None
        for line in block.splitlines():
            match = re.match(r"^([A-Za-z-]+(?: ID)?):\s*(.*)$", line)
            if match and match.group(1) in (
                "Message ID", "Subject", "From", "Date", "To", "Cc", "In-Reply-To"
            ):
                key = match.group(1)
                fields[key] = match.group(2).strip()
            elif key and line.startswith(" "):
                fields[key] += " " + line.strip()
        if fields.get("Message ID"):
            out.append(fields)
    return out


def parse_failures(text: str) -> list[str]:
    """Message ids the server could not fetch, from its warning blocks.

    A throttled message comes back as `⚠️ Message <id>: <HttpError 429 ...>`
    in place of its headers. Skipping those quietly is how a thread vanishes
    from one run and returns as new in the next, so they are collected and
    retried instead.
    """
    return re.findall(r"^\s*⚠️?\s*Message ([0-9a-fA-F]+):", text, re.MULTILINE)


def addresses(header: str) -> set[str]:
    """Every address in a To/Cc/From header, lowercased."""
    if not header or "not present" in header:
        return set()
    found = {a.lower() for a in ADDRESS_RE.findall(header)}
    if not found:
        found = {p.strip().lower() for p in header.split(",") if "@" in p}
    return found


def display_name(header: str) -> str:
    """The human part of a From header, falling back to the address."""
    if not header:
        return "unknown"
    name = ADDRESS_RE.sub("", header).strip().strip('"').strip()
    if name:
        return name
    return header.strip()


def when(header: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ------------------------------------------------------------------ threads


@dataclass(frozen=True)
class Message:
    message_id: str
    thread_id: str
    subject: str
    who: str
    sender: str
    sent_at: datetime | None
    alias: str


def alias_for(fields: dict, aliases: dict[str, str]) -> str:
    """Which alias carried this message. Empty when none did."""
    recipients = addresses(fields.get("To", "")) | addresses(fields.get("Cc", ""))
    for label, address in aliases.items():
        if address in recipients:
            return label
    # Mail sent *from* an alias still belongs to it.
    senders = addresses(fields.get("From", ""))
    for label, address in aliases.items():
        if address in senders:
            return label
    return ""


def to_messages(pairs: list[tuple[str, str]], blocks: list[dict], config: Config) -> list[Message]:
    thread_of = dict(pairs)
    out: list[Message] = []
    for fields in blocks:
        mid = fields["Message ID"]
        sender = next(iter(addresses(fields.get("From", ""))), "")
        out.append(Message(
            message_id=mid,
            thread_id=thread_of.get(mid, mid),
            subject=fields.get("Subject", "(no subject)"),
            who=display_name(fields.get("From", "")),
            sender=sender,
            sent_at=when(fields.get("Date", "")),
            alias=alias_for(fields, config.aliases),
        ))
    return out


def latest_per_thread(messages: list[Message]) -> dict[str, Message]:
    """The newest message in each thread. That is the one that decides who waits."""
    best: dict[str, Message] = {}
    for message in messages:
        current = best.get(message.thread_id)
        if current is None:
            best[message.thread_id] = message
            continue
        a = message.sent_at or datetime.min.replace(tzinfo=timezone.utc)
        b = current.sent_at or datetime.min.replace(tzinfo=timezone.utc)
        if a > b:
            best[message.thread_id] = message
    return best


def waiting_on(message: Message, config: Config) -> str:
    """Whose move it is.

    Decided by who spoke last and nothing else. No attempt is made to read the
    text and judge whether a question was asked: that is the kind of guess that
    is wrong quietly. "They replied last, so it is your move" is a rule a reader
    can check at a glance and correct in their head.
    """
    return "them" if message.sender in config.me else "you"


# ------------------------------------------------- does it ask for something

# A second, separate signal, and deliberately not part of `waiting_on`. Whose
# move it is stays decided by who spoke last, because that rule is checkable.
# This one answers a different question — does the newest message actually ask
# its recipients for a decision or an action — and is used for one thing only:
# floating those threads to the top of the group they were already in. It
# never drops a row, never hides one, and never moves one between groups.
#
# It runs unless `jev enabled JEV_MAIL_INTAKE` declines: an unkeyed machine,
# the fleet switch off, or that variable switching this stage off. Unset means
# on, and the variable only ever subtracts. Jev is experimental and
# the repo's rule is that the machine runs the same without it as with it, so
# every failure here falls back to today's ordering, says why on stderr, and
# leaves the exit code alone.
#
# WHAT LEAVES THE MACHINE: the subject line, and the single word "inbound" or
# "outbound". No address, no display name, no body, no snippet, no thread id
# and no message id is ever sent. `state_for_jev` builds the whole payload and
# nothing else contributes to it, which is what makes that sentence checkable.

JEV_STAGE = "JEV_MAIL_INTAKE"

#: Who this is in the judgment ledger. The folder name, which the ledger's
#: identifier grammar accepts as it stands; a name it refuses is filed under
#: `unknown`, and a ledger of `unknown` rows compares nothing.
JEV_CALLER = "local-mail-intake"
JEV_QUESTION = ("Does the newest message in this email thread ask its "
                "recipients for a decision or an action?")
JEV_GATE = "0.7"
JEV_TIMEOUT = 20        # seconds for one question; jev does its own retries
JEV_MAX_QUESTIONS = 25  # a bound, so `report --all` cannot turn into a bill


def jev_script() -> Path:
    """The sibling entrypoint, by path.

    Resolved from this file rather than looked up on `PATH`. `local-bin-links`
    puts `jev` on an interactive shell's PATH and neither cron nor CI has it,
    so a caller written against the name is a caller that quietly stops
    running exactly where this module actually runs.
    """
    return Path(__file__).resolve().parent.parent / "local-jev" / "jev.sh"


def state_for_jev(row: dict) -> str:
    """The entire payload: a subject line and a direction word.

    Read this against the README's privacy sentence. If anything else ever
    needs to go to Jev, it goes in here, in the open, and the test that greps
    this payload for an address fails first.
    """
    subject = " ".join((row.get("subject") or "").split())
    direction = "outbound" if (row.get("direction") or "") == "sent" else "inbound"
    return f"direction: {direction}\nsubject: {subject}\n"


def run_jev(args: list[str], state: str,
            environ: dict[str, str] | None = None) -> tuple[int, str]:
    """Invoke the sibling command. The one place a subprocess is spawned.

    `environ` is layered over `os.environ` rather than replacing it: the child
    still needs PATH and HOME, and this module's own `environ` is an injected
    dict, not the process environment.

    Passing it matters more since the flip. `jev enabled JEV_MAIL_INTAKE` reads
    the variable in the *child*, so a caller that sets it in the dict this
    module was handed -- which is the only way a test or an embedding caller
    can set it -- would otherwise find the kill switch does nothing. A switch
    that looks set and is not is the failure this whole shape exists to avoid.
    """
    completed = subprocess.run(args, input=state, capture_output=True,
                               text=True, timeout=JEV_TIMEOUT,
                               env={**os.environ, **(environ or {})})
    return completed.returncode, completed.stdout.strip()


def record_baseline(script: Path,
                    environ: dict[str, str] | None = None,
                    cause: str | None = None) -> None:
    """Say today's order -- the control arm -- is what the report carried.

    `--outcome ok` always: the old path completed, and that this row exists at
    all is the fact nothing else carries.

    `cause` is the word only when `jev` never wrote its own row: the caller
    killed it on a deadline, or it could not be started. `jev` flushes its
    measurement after the answer is printed and installs no signal handler, so
    a call ended by SIGTERM leaves nothing behind and this row is the only
    record there will be. When `jev` did return -- a non-zero exit, an answer
    this cannot read -- it has already written the cause, and repeating it
    here counts one decision as two: `judgment.py`'s DECLINES groups by stage
    and cause across both arms with no deduplication.

    Bookkeeping only: `jev record` sends nothing, needs no key, prints
    nothing and always exits 0. Every failure is swallowed, because a report
    must never fail over its own measurement.
    """
    try:
        run_jev([str(script), "record", "--caller", JEV_CALLER,
                 "--stage", JEV_STAGE, "--arm", "baseline",
                 "--outcome", "ok"]
                + (["--decline", cause] if cause else []), "", environ)
    except Exception:  # noqa: BLE001 - bookkeeping may never fail the caller
        pass


def judge_asks(rows: list[dict], environ: dict[str, str], err) -> None:
    """Annotate rows with `asks` (yes/no/unknown), in place.

    A row this cannot answer for keeps no `asks` key at all, or an explicit
    `unknown`, and `asks_first` leaves both where they were. Returning early
    is always safe: the report is complete either way.
    """
    script = jev_script()
    if not script.exists():
        print(f"jev: no {script}; ordering unchanged", file=err)
        return
    try:
        # Both halves in one call: Jev can answer here, and JEV_MAIL_INTAKE
        # has not been used to switch this stage off. Unset means on.
        # `--record` so the decline is counted: most reports end here, and
        # today's order running is the only fact the control arm has.
        code, _ = run_jev([str(script), "enabled", JEV_STAGE,
                           "--record", "--caller", JEV_CALLER], "", environ)
    except Exception as error:  # a missing interpreter, a timeout, anything
        print(f"jev: enabled check failed ({error}); ordering unchanged", file=err)
        return
    if code != 0:
        print(f"jev: not enabled here (try {script} enabled {JEV_STAGE} --why); "
              "ordering unchanged", file=err)
        # No second row: the gate above wrote one in the process this call
        # already started, and `judgment.py` counts gate events on their own.
        return

    asked = 0
    for row in rows:
        if asked >= JEV_MAX_QUESTIONS:
            print(f"jev: stopped after {JEV_MAX_QUESTIONS} questions; the rest "
                  "keep today's order", file=err)
            break
        try:
            code, answer = run_jev(
                [str(script), "noul", JEV_QUESTION, "--state", "-",
                 "--gate", JEV_GATE, "--caller", JEV_CALLER,
                 "--stage", JEV_STAGE, "--fallback", "unknown"],
                state_for_jev(row), environ)
        except Exception as error:
            print(f"jev: {error}; the rest keep today's order", file=err)
            # A call was made and did not answer, so the old path -- today's
            # order -- is what the report carries. That is the control arm,
            # and it is worth a row; the gate decline above is not, because
            # `jev enabled --record` already wrote one there.
            #
            # This branch is only ever reached when the process itself failed:
            # `run_jev` puts `JEV_TIMEOUT` on the call and the timeout kills
            # it, or it never started. Either way `jev` wrote nothing, so the
            # cause belongs here. A `jev` that returns unusably goes to the
            # `code != 0` branch below, where it has already written its own.
            record_baseline(
                script, environ,
                "timeout" if isinstance(error, subprocess.TimeoutExpired)
                else "unavailable")
            break
        asked += 1
        if code != 0 or answer not in ("yes", "no"):
            print(f"jev: no answer for one thread (rc={code}); it keeps "
                  "today's place", file=err)
            row["asks"] = "unknown"
            continue
        row["asks"] = answer


def asks_first(rows: list[dict]) -> list[dict]:
    """Threads that ask for something first. Stable, so nothing else moves."""
    return sorted(rows, key=lambda row: 0 if row.get("asks") == "yes" else 1)


# -------------------------------------------------------------------- state


def read_threads(path: Path) -> dict[str, str]:
    """thread_id -> the last message id we have already recorded."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {r["thread_id"]: r["last_message_id"]
                for r in csv.DictReader(handle) if r.get("thread_id")}


def read_thread_rows(path: Path) -> dict[str, list[str]]:
    """Every recorded thread row, whole, for carrying forward."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {r["thread_id"]: [r.get(c, "") for c in THREAD_COLUMNS]
                for r in csv.DictReader(handle) if r.get("thread_id")}


def write_threads(path: Path, latest: dict[str, Message], seen_at: str,
                  carry: dict[str, list[str]] | None = None) -> None:
    """Write the thread state atomically, keeping threads this run did not see.

    A thread absent from `latest` is carried forward unchanged rather than
    dropped. Absence is not evidence: Gmail throttles a batch fetch under load,
    and a message lost to a 429 makes its thread look gone. Dropping the row
    would report that thread as brand new on the next run, which is exactly the
    false alarm the digest exists to avoid.

    A thread that really has aged out of the window keeps a stale row. That
    costs one CSV line and reports nothing.
    """
    rows = dict(carry or {})
    for thread_id, message in latest.items():
        stamp = message.sent_at.isoformat(timespec="seconds") if message.sent_at else ""
        rows[thread_id] = [thread_id, message.message_id, stamp, seen_at]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(THREAD_COLUMNS)
        for thread_id in sorted(rows):
            writer.writerow(rows[thread_id])
    temporary.replace(path)


def read_arrivals(path: Path, unreported_only: bool = True) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [r for r in csv.DictReader(handle) if r.get("thread_id")]
    if unreported_only:
        rows = [r for r in rows if not (r.get("reported_at") or "").strip()]
    return rows


def append_arrivals(path: Path, rows: list[dict]) -> None:
    """Append-only, for the same reason local-drive-intake's log is.

    A hand-run fetch must not consume the rows the morning digest has not
    mailed yet.
    """
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ARRIVAL_COLUMNS)
        if fresh:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def stamp_arrivals(path: Path, through: str, stamped_at: str) -> int:
    """Mark rows delivered up to a cutoff. Bounded for the same race reason."""
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    count = 0
    for row in rows:
        if (row.get("reported_at") or "").strip():
            continue
        if (row.get("detected_at") or "") <= through:
            row["reported_at"] = stamped_at
            count += 1
    if not count:
        return 0
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ARRIVAL_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in ARRIVAL_COLUMNS})
    temporary.replace(path)
    return count


def diff(latest: dict[str, Message], known: dict[str, str], config: Config,
         detected_at: str) -> list[dict]:
    """Threads whose newest message we have not recorded before.

    Keyed on the thread's newest message id, not on a count. A thread that goes
    quiet produces nothing; a thread that gets one more reply produces one row,
    however long it already was.
    """
    rows: list[dict] = []
    for thread_id, message in latest.items():
        if known.get(thread_id) == message.message_id:
            continue
        direction = "sent" if message.sender in config.me else "received"
        rows.append({
            "detected_at": detected_at,
            "thread_id": thread_id,
            "message_id": message.message_id,
            "alias": message.alias,
            "subject": message.subject,
            "who": message.who,
            "direction": direction,
            "waiting_on": waiting_on(message, config),
            "sent_at": message.sent_at.isoformat(timespec="seconds") if message.sent_at else "",
            "reported_at": "",
        })
    rows.sort(key=lambda r: r["sent_at"], reverse=True)
    return rows


# -------------------------------------------------------------------- verbs


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def gather(config: Config, url: str, out) -> tuple[list[Message], list[str]]:
    """Search both aliases, then fetch headers for what came back.

    Headers, not bodies. The digest reports who wrote and about what; it has no
    use for the text, and not fetching it keeps homeowner correspondence out of
    every buffer this program touches.

    Returns the messages and the ids Gmail throttled away even after retries.
    The caller needs that second list: a run that could not see every message
    has not proved a thread is unchanged, so it must not overwrite that
    thread's state.
    """
    client = Mcp(url)
    client.connect()

    terms = []
    for address in sorted(set(config.aliases.values())):
        terms.append(f"to:{address}")
        terms.append(f"from:{address}")
        terms.append(f"cc:{address}")
    query = f"({' OR '.join(terms)}) newer_than:{config.window_days}d"

    pairs: list[tuple[str, str]] = []
    token = None
    for _ in range(20):  # a hard ceiling, so a bad query cannot page forever
        arguments = {"query": query, "user_google_email": config.account, "page_size": 25}
        if token:
            arguments["page_token"] = token
        text = client.call("search_gmail_messages", arguments)
        pairs.extend(parse_search(text))
        match = re.search(r"page_token='([^']+)'", text)
        if not match:
            break
        token = match.group(1)

    # Same message can appear twice across pages; keep the first thread seen.
    unique: dict[str, str] = {}
    for message_id, thread_id in pairs:
        unique.setdefault(message_id, thread_id)
    ids = list(unique)
    if not ids:
        return [], []

    blocks: list[dict] = []
    pending = ids
    delay = BACKOFF
    for attempt in range(RETRIES):
        throttled: list[str] = []
        size = BATCH if attempt == 0 else max(3, BATCH // 2)
        for start in range(0, len(pending), size):
            chunk = pending[start:start + size]
            text = client.call("get_gmail_messages_content_batch", {
                "message_ids": chunk,
                "user_google_email": config.account,
                "format": "metadata",
            })
            blocks.extend(parse_metadata(text))
            throttled.extend(parse_failures(text))
        pending = throttled
        if not pending:
            break
        time.sleep(delay)
        delay *= 2

    messages = to_messages(list(unique.items()), blocks, config)
    # A message that reached neither alias is somebody else's mail.
    return [m for m in messages if m.alias], pending


def load_config(config_path: Path, out) -> Config | None:
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"error: {config_path} does not exist. Copy "
              f"local-mail-intake/mail-intake.conf.example there and fill it in, "
              f"or set MAIL_INTAKE_CONFIG.", file=out)
        return None
    except OSError as error:
        print(f"error: cannot read {config_path}: {error}", file=out)
        return None
    config, problems = parse_config(text)
    for problem in problems:
        print(f"config: {problem}", file=out)
    return config


def summarise(rows: list[dict]) -> str:
    you = sum(1 for r in rows if r["waiting_on"] == "you")
    them = len(rows) - you
    parts = []
    if you:
        parts.append(f"{you} waiting on you")
    if them:
        parts.append(f"{them} waiting on them")
    return ", ".join(parts) or "no change"


def cmd_fetch(config_path: Path, state_dir: Path, url: str, out, dry_run: bool = False) -> int:
    config = load_config(config_path, out)
    if config is None:
        return EXIT_ERROR
    try:
        messages, throttled = gather(config, url, out)
    except McpError as error:
        print(f"error: {error}", file=out)
        return EXIT_ERROR
    if throttled:
        # Not an error. Gmail rate-limits under load and the state merge below
        # makes a partial run harmless, but say so rather than report silence.
        print(f"note: Gmail throttled {len(throttled)} message(s) after "
              f"{RETRIES} attempts; they will be picked up next run", file=out)

    threads_path = state_dir / "threads.csv"
    log_path = state_dir / "mail-arrivals.csv"
    first_run = not threads_path.exists()
    latest = latest_per_thread(messages)
    carry = read_thread_rows(threads_path)
    known = read_threads(threads_path)
    stamp = now_iso()
    rows = diff(latest, known, config, stamp)

    if dry_run:
        if first_run:
            print(f"no state yet: a fetch would record {len(latest)} threads as a baseline", file=out)
            return EXIT_NONE
        if not rows:
            print(f"no change: {len(latest)} threads in the last {config.window_days} days", file=out)
            return EXIT_NONE
        print(f"a fetch would record {len(rows)}: {summarise(rows)}", file=out)
        for row in rows:
            print(f"  [{row['alias']}] {row['subject']}  — {row['who']} ({row['waiting_on']})", file=out)
        return EXIT_FOUND

    write_threads(threads_path, latest, stamp, carry)

    if first_run:
        # Baseline. Every open thread is technically new and none of it is news.
        print(f"baseline established: {len(latest)} threads across {len(config.aliases)} aliases", file=out)
        return EXIT_NONE

    append_arrivals(log_path, rows)
    if not rows:
        print(f"no change: {len(latest)} threads in the last {config.window_days} days", file=out)
        return EXIT_NONE
    pending = len(read_arrivals(log_path))
    print(f"{len(rows)} changed: {summarise(rows)}", file=out)
    print(f"log: {log_path}  ({pending} undelivered)", file=out)
    return EXIT_FOUND


def cmd_report(state_dir: Path, out, show_all: bool = False,
               environ: dict[str, str] | None = None, err=None) -> int:
    err = sys.stderr if err is None else err
    environ = dict(os.environ if environ is None else environ)
    log_path = state_dir / "mail-arrivals.csv"
    if not log_path.exists():
        # An absent log is two different situations and only one is an error.
        # The thread state is what says a fetch has run: with it present, no
        # log means no thread has changed since the baseline, which is a quiet
        # day and must exit 3. Returning 1 here told a caller to run the
        # command it had just run, and broke this module's own rule that a
        # quiet day is 3 and not 1.
        if (state_dir / "threads.csv").exists():
            print("nothing undelivered", file=out)
            return EXIT_NONE
        print(f"no arrivals log at {log_path}; run fetch first", file=out)
        return EXIT_ERROR
    rows = read_arrivals(log_path, unreported_only=not show_all)
    if not rows:
        print("nothing undelivered" if not show_all else "the log is empty", file=out)
        return EXIT_NONE
    # Optional and additive. Switched off, unkeyed or failing, this is a no-op
    # and every row keeps the `asks` key it does not have, so the loop below
    # prints exactly what it printed before Jev existed.
    judge_asks(rows, environ, err)
    for label, heading in (("you", "WAITING ON YOU"), ("them", "waiting on them")):
        group = [r for r in rows if r["waiting_on"] == label]
        if not group:
            continue
        print(f"\n{heading} ({len(group)})", file=out)
        for row in asks_first(group):
            mark = "  (asks)" if row.get("asks") == "yes" else ""
            print(f"  [{row['alias']}] {row['subject']}{mark}", file=out)
            print(f"      {row['who']}, {row['sent_at'][:16]}", file=out)
    return EXIT_FOUND


def cmd_stamp(state_dir: Path, through: str | None, out) -> int:
    log_path = state_dir / "mail-arrivals.csv"
    if not log_path.exists():
        if (state_dir / "threads.csv").exists():
            print("nothing to stamp", file=out)
            return EXIT_NONE
        print(f"no arrivals log at {log_path}; run fetch first", file=out)
        return EXIT_ERROR
    cutoff = through or now_iso()
    count = stamp_arrivals(log_path, cutoff, now_iso())
    if not count:
        print("nothing to stamp", file=out)
        return EXIT_NONE
    print(f"stamped {count} arrival(s) delivered, through {cutoff}", file=out)
    return EXIT_FOUND


def cmd_status(config_path: Path, state_dir: Path, url: str, out) -> int:
    threads_path = state_dir / "threads.csv"
    log_path = state_dir / "mail-arrivals.csv"
    print(f"config : {config_path}", file=out)
    print(f"state  : {threads_path}", file=out)

    config = load_config(config_path, out)
    if config is None:
        return EXIT_ERROR
    print(f"account: {config.account}", file=out)
    for label, address in sorted(config.aliases.items()):
        print(f"alias  : {label}  {address}", file=out)
    print(f"window : {config.window_days} days", file=out)

    client = Mcp(url, timeout=10)
    try:
        client.connect()
        print(f"server : ok      {url}", file=out)
    except McpError as error:
        print(f"server : ERROR   {error}", file=out)
        return EXIT_ERROR

    total = len(read_arrivals(log_path, unreported_only=False))
    pending = len(read_arrivals(log_path))
    print(f"log    : {total} arrivals recorded, {pending} undelivered", file=out)
    if not threads_path.exists():
        print("state  : MISSING, no fetch has run", file=out)
        return EXIT_NONE
    print(f"state  : {len(read_threads(threads_path))} threads tracked", file=out)
    return EXIT_FOUND


def main(argv: list[str], environ: dict[str, str] | None = None, out=None) -> int:
    out = sys.stdout if out is None else out
    environ = dict(os.environ if environ is None else environ)

    config_path = Path(environ.get("MAIL_INTAKE_CONFIG")
                       or system_tools_config.config_dir(TOOL, environ) / "mail-intake.conf")
    state_dir = Path(environ.get("MAIL_INTAKE_STATE", Path.home() / ".local/share/mail-intake"))
    url = environ.get("WORKSPACE_MCP_URL", DEFAULT_URL)

    verb = argv[0] if argv else ""
    rest = argv[1:]
    if verb == "fetch":
        return cmd_fetch(config_path, state_dir, url, out)
    if verb == "peek":
        return cmd_fetch(config_path, state_dir, url, out, dry_run=True)
    if verb == "report":
        return cmd_report(state_dir, out, show_all="--all" in rest,
                          environ=environ)
    if verb == "stamp":
        through = None
        if "--through" in rest:
            index = rest.index("--through")
            if index + 1 < len(rest):
                through = rest[index + 1]
            else:
                print("error: --through needs a timestamp", file=out)
                return EXIT_ERROR
        return cmd_stamp(state_dir, through, out)
    if verb == "status":
        return cmd_status(config_path, state_dir, url, out)
    print("usage: mail-intake.sh fetch|peek|report [--all]|stamp [--through TS]|status|test", file=out)
    return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - exercised via mail-intake.sh
    raise SystemExit(main(sys.argv[1:]))
