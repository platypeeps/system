"""`dependabot.sh holds` -- the watcher's mechanical pass, as `ROUTINE.md` states it.

The contract is the thing under review; this module is its executable
reference for the half that a test can pin: discovery through the two gates,
the writer check, the three probes, the lift sequence, resumption, and
supersession. Judgement -- writing a hold in the first place, wording it,
choosing which probe names the blocker -- stays with the agent in the daily
sweep and is not here.

Every external effect goes through two runners. `SD_HOLDS_GH` names the
command standing in for `gh` and `SD_HOLDS_NPM` the one standing in for
`npm`; the suite points both at doubles under `tests/doubles/` that answer
from recorded shapes and journal every write. Nothing here talks to GitHub or
the registry any other way, which is what makes the doubles exhaustive.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import system_tools_config  # noqa: E402

#: The owner gate, as `ROUTINE.md` writes it. The denied list is recited there
#: on purpose (authorization cannot be enumerated from a filesystem) and copied
#: here because a runtime parse of prose is a second thing to get wrong; the
#: suite asserts these two agree with the document, so they cannot drift
#: silently. Repositories allowed outside ALLOWED_OWNERS are the operator's
#: own consent, so they live in `<config>/dependabot/allowed-repos.conf`,
#: outside the checkout.
DENIED = (
    "platypeeps/Trellis",
    "platypeeps/google_workspace_mcp",
    "platypeeps/sd-github-review",
    "platypeeps/sd-github-review-pilot",
    "platypeeps/sd-review-test",
    "platypeeps/se-ai-command-pack",
)
ALLOWED_OWNERS = ("platypeeps",)


def _allowed_repos() -> tuple[str, ...]:
    """Repositories outside ALLOWED_OWNERS the owner consented to, read from
    `<config>/dependabot/allowed-repos.conf` (one `owner/name` per line, `#`
    comments) or the file DEPENDABOT_ALLOWED_REPOS_FILE names. Missing means
    none: the gate fails closed."""
    path = Path(os.environ.get("DEPENDABOT_ALLOWED_REPOS_FILE")
                or system_tools_config.config_dir("dependabot") / "allowed-repos.conf")
    if not path.is_file():
        return ()
    slugs = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and line not in slugs:
            slugs.append(line)
    return tuple(slugs)


ALLOWED_REPOS = _allowed_repos()

#: `gh repo view --json viewerPermission` answers a merge would succeed.
WRITE_VIEWER = {"ADMIN", "MAINTAIN", "WRITE"}
#: `collaborators/{login}/permission` answers whether a comment's author may
#: register a hold. `role_name` carries `maintain`; `permission` folds it into
#: `write`, so both are read.
WRITE_ROLES = {"admin", "maintain", "write"}

DEPENDABOT = "dependabot[bot]"
PAGE = 100

FENCE_RE = re.compile(r"```yaml[ \t]*\n(sd-hold:\n(?:[ \t]+[^\n]*\n?)*)```", re.MULTILINE)
SUPERSEDED_RE = re.compile(r"^\s*Superseded by #(\d+)\.?\s*$", re.MULTILINE)
ISSUE_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)$")
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
PACKAGE_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
OPERATORS = (">=", "<=", ">", "<", "=")
PRERELEASE_RE = re.compile(r"\d-[0-9A-Za-z]")


class CannotRun(Exception):
    """The job cannot run at all: no runner, no login. The one non-zero exit."""


class ReadFailed(Exception):
    """A probe's read did not answer; the probe is `unknown`, never false."""


class WriteFailed(Exception):
    """A write did not land. Never caught: the run fails, as it did before."""


# --- runners --------------------------------------------------------------


def _runner(variable: str, default: str) -> list[str]:
    return shlex.split(os.environ.get(variable) or default)


def _call(command: list[str], stdin: str | None = None) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, input=stdin)
    except OSError as error:
        # A missing or unrunnable `gh`/`npm` is a failed read like any other.
        raise ReadFailed(f"{command[0]}: {error.strerror or error}") from error
    if done.returncode != 0:
        message = (done.stderr or done.stdout).strip().splitlines()
        raise ReadFailed(message[0] if message else f"exit {done.returncode}")
    return done.stdout


class GitHub:
    """Every GitHub read and write, one method each, so a double is small."""

    def __init__(self, dry_run: bool = False):
        self.gh = _runner("SD_HOLDS_GH", "gh")
        self.dry_run = dry_run

    def api(self, path: str) -> object:
        return json.loads(_call(self.gh + ["api", path]) or "null")

    def paged(self, path: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            joiner = "&" if "?" in path else "?"
            batch = self.api(f"{path}{joiner}per_page={PAGE}&page={page}")
            items.extend(batch)
            if len(batch) < PAGE:
                return items
            page += 1

    def login(self) -> str:
        try:
            return self.api("user")["login"]
        except (ReadFailed, KeyError, TypeError) as error:
            raise CannotRun(f"cannot read the authenticated login: {error}") from error

    def viewer_permission(self, slug: str) -> str | None:
        try:
            out = _call(self.gh + ["repo", "view", slug, "--json", "viewerPermission"])
            return json.loads(out).get("viewerPermission")
        except (ReadFailed, ValueError):
            return None

    def dependabot_pulls(self, slug: str) -> list[dict]:
        # The issues listing rather than `pulls`: it carries the comment
        # count, which is what spares a comment read on every pull request
        # Dependabot ever opened. Its `creator` filter takes the bot's login.
        issues = self.paged(f"repos/{slug}/issues?state=all&creator=dependabot%5Bbot%5D")
        return [i for i in issues if i.get("pull_request") and i["user"]["login"] == DEPENDABOT]

    def comments(self, slug: str, number: int) -> list[dict]:
        return self.paged(f"repos/{slug}/issues/{number}/comments")

    def can_write(self, slug: str, login: str) -> bool:
        try:
            answer = self.api(f"repos/{slug}/collaborators/{login}/permission")
        except ReadFailed:
            return False
        role = (answer.get("role_name") or answer.get("permission") or "").lower()
        return role in WRITE_ROLES

    def issue_state(self, slug: str, number: int) -> tuple[str, str]:
        answer = self.api(f"repos/{slug}/issues/{number}")
        return answer["state"], answer.get("html_url") or f"https://github.com/{slug}/issues/{number}"

    def _write(self, method: str, path: str, body: str) -> dict:
        if self.dry_run:
            return {"id": 0, "body": body}
        try:
            out = _call(self.gh + ["api", "--method", method, path, "--input", "-"],
                        stdin=json.dumps({"body": body}))
        except ReadFailed as error:
            raise WriteFailed(f"{method} {path}: {error}") from error
        return json.loads(out or "{}")

    def edit_comment(self, slug: str, comment_id: int, body: str) -> dict:
        return self._write("PATCH", f"repos/{slug}/issues/comments/{comment_id}", body)

    def post_comment(self, slug: str, number: int, body: str) -> dict:
        return self._write("POST", f"repos/{slug}/issues/{number}/comments", body)


class Registry:
    def __init__(self):
        self.npm = _runner("SD_HOLDS_NPM", "npm")

    def latest(self, package: str) -> tuple[str, dict]:
        out = _call(self.npm + ["view", f"{package}@latest", "version", "dependencies", "--json"])
        answer = json.loads(out or "{}")
        # A package declaring no dependencies answers with the bare version
        # string (`npm view ms@latest version dependencies --json` -> "2.1.3").
        if isinstance(answer, str):
            return answer, {}
        if not isinstance(answer, dict) or "version" not in answer:
            raise ReadFailed(f"registry answered without a version for {package}")
        return answer["version"], answer.get("dependencies") or {}


# --- versions and ranges ----------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int, tuple]:
    match = VERSION_RE.match(text.strip())
    if not match:
        raise ValueError(f"not a version: {text!r}")
    major, minor, patch, pre = match.groups()
    # A prerelease sorts before its release: (1,0,0,(0,(...))) < (1,0,0,(1,)).
    # Its dot-separated identifiers compare one by one, numerics as numbers
    # and below alphanumerics, and a shorter prefix sorts first (semver 11).
    identifiers = tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                        for part in pre.split(".")) if pre else ()
    tag = (0, identifiers) if pre else (1,)
    return int(major), int(minor), int(patch), tag


def compare(left: str, operator: str, right: str) -> bool:
    a, b = parse_version(left), parse_version(right)
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b, "=": a == b}[operator]


Bound = tuple[tuple, bool]  # (version, inclusive)
LOWEST: Bound = ((0, 0, 0, (0, ())), True)
HIGHEST: Bound = ((10**9, 0, 0, (1,)), False)


def _partial(text: str) -> tuple[list[int], bool]:
    """`1`, `1.2`, `1.2.3`, `1.x`, `*` -> the numbers given and whether any were."""
    text = text.strip().lstrip("v")
    if text in ("", "*", "x", "X"):
        return [], False
    numbers = []
    for part in text.split("."):
        if part in ("x", "X", "*"):
            break
        if not part.isdigit():
            raise ValueError(f"not a version range: {text!r}")
        numbers.append(int(part))
    return numbers, True


def _interval(comparator: str) -> tuple[Bound, Bound]:
    """One comparator -> [low, high) over versions, following npm's rules."""
    comparator = comparator.strip()
    if comparator.startswith(("^", "~")):
        numbers, _ = _partial(comparator[1:])
        if not numbers:
            return LOWEST, HIGHEST
        major = numbers[0]
        minor = numbers[1] if len(numbers) > 1 else 0
        patch = numbers[2] if len(numbers) > 2 else 0
        low = ((major, minor, patch, (0, ())), True)
        if comparator[0] == "~":
            high = (major, minor + 1, 0) if len(numbers) > 1 else (major + 1, 0, 0)
        elif major > 0 or len(numbers) == 1:
            high = (major + 1, 0, 0)
        elif minor > 0 or len(numbers) == 2:
            high = (0, minor + 1, 0)
        else:
            high = (0, 0, patch + 1)
        return low, ((*high, (0, ())), False)
    for operator in OPERATORS:
        if comparator.startswith(operator):
            rest = comparator[len(operator):].strip()
            if VERSION_RE.match(rest):
                version = parse_version(rest)
                if operator == ">":
                    return (version, False), HIGHEST
                if operator == ">=":
                    return (version, True), HIGHEST
                if operator == "<":
                    return LOWEST, (version, False)
                if operator == "<=":
                    return LOWEST, (version, True)
                return (version, True), (version, True)
            # A partial after an operator follows npm: `>=9` is `>=9.0.0`,
            # `>9` is `>=10.0.0`, `<=9` is `<10.0.0`, `<11` is `<11.0.0`.
            low, high = _interval(rest)
            if operator == ">=":
                return low, HIGHEST
            if operator == ">":
                return (high[0], True), HIGHEST
            if operator == "<":
                return LOWEST, (low[0], False)
            if operator == "<=":
                return LOWEST, high
            return low, high
    numbers, given = _partial(comparator)
    if not given:
        return LOWEST, HIGHEST
    if len(numbers) == 3:
        version = (*numbers, (1,))
        return (version, True), (version, True)
    low = ((*numbers, *([0] * (3 - len(numbers))), (0, ())), True)
    high = (numbers[0] + 1, 0, 0) if len(numbers) == 1 else (numbers[0], numbers[1] + 1, 0)
    return low, ((*high, (0, ())), False)


def _intersect(a: tuple[Bound, Bound], b: tuple[Bound, Bound]) -> bool:
    """Whether a release lies in both: npm's ranges exclude every prerelease here.

    A partial bound sits at a prerelease (`>=10` starts at 10.0.0-0), so an
    overlap can hold prereleases only (`>=10` with `<10.0.0`); that is none.
    """
    low = max(a[0], b[0], key=lambda bound: (bound[0], not bound[1]))
    high = min(a[1], b[1], key=lambda bound: (bound[0], bound[1]))
    (major, minor, patch, tag), inclusive = low
    if tag != (1,):
        first = (major, minor, patch, (1,))
    elif inclusive:
        first = low[0]
    else:
        first = (major, minor, patch + 1, (1,))
    return first < high[0] or (first == high[0] and high[1])


def _range_set(text: str) -> list[tuple[Bound, Bound]]:
    """`^9.0.1 || >=10.0.0 <11` -> a union of intervals; `-` hyphen ranges too."""
    # npm admits a prerelease only through a comparator naming its own triple,
    # which intervals cannot say; a range naming one is refused, never guessed.
    if PRERELEASE_RE.search(text):
        raise ValueError(f"a prerelease in a range is not read: {text!r}")
    result = []
    for alternative in text.split("||"):
        alternative = alternative.strip()
        if " - " in alternative:
            low, high = (part.strip() for part in alternative.split(" - ", 1))
            interval = (_interval(">=" + low)[0], _interval(high)[1])
        else:
            # npm lets whitespace sit between an operator and its version:
            # `>= 9.0.0` is `>=9.0.0`, not `>=` followed by an exact `9.0.0`.
            alternative = re.sub(r"(<=|>=|<|>|=|\^|~)\s+", r"\1", alternative)
            interval = (LOWEST, HIGHEST)
            for comparator in alternative.split() or ["*"]:
                piece = _interval(comparator)
                interval = (max(interval[0], piece[0], key=lambda b: (b[0], not b[1])),
                            min(interval[1], piece[1], key=lambda b: (b[0], b[1])))
        result.append(interval)
    return result


def ranges_overlap(declared: str, wanted: str) -> bool:
    """Whether some version satisfies both `declared` and `wanted`."""
    return any(_intersect(a, b) for a in _range_set(declared) for b in _range_set(wanted))


# --- records ----------------------------------------------------------------


@dataclass
class Probe:
    line: str
    kind: str | None = None
    value: str | None = None
    parts: tuple = ()

    @property
    def malformed(self) -> bool:
        return self.kind is None

    def __str__(self) -> str:
        return f"{self.kind} {self.value}" if self.kind else self.line


def parse_probe(line: str) -> Probe:
    match = re.match(r"^\s*-\s+([a-z-]+):\s*(.+?)\s*$", line)
    if not match:
        return Probe(line.strip())
    kind, raw = match.groups()
    value = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw
    words = value.split()
    try:
        if kind == "issue-closed":
            owner, repo, number = ISSUE_RE.match(value).groups()  # type: ignore[union-attr]
            return Probe(line.strip(), kind, value, (f"{owner}/{repo}", int(number)))
        if kind == "npm-version" and len(words) == 3 and words[1] in OPERATORS \
                and PACKAGE_RE.match(words[0]):
            parse_version(words[2])
            return Probe(line.strip(), kind, value, tuple(words))
        if kind == "npm-dep" and len(words) >= 3 and PACKAGE_RE.match(words[0]) \
                and PACKAGE_RE.match(words[1]):
            # The range is the rest of the value: `^9.0.1 || ^10.0.0` has spaces.
            wanted = " ".join(words[2:])
            _range_set(wanted)
            return Probe(line.strip(), kind, value, (words[0], words[1], wanted))
    except (AttributeError, ValueError):
        pass
    return Probe(line.strip())


@dataclass
class Record:
    slug: str
    number: int
    pull_state: str
    comment_id: int
    author: str
    body: str
    fields: dict = field(default_factory=dict)
    probes: list[Probe] = field(default_factory=list)
    lists_probes: bool = False

    @property
    def problems(self) -> list[str]:
        """What the block lacks of the three fields ROUTINE.md requires."""
        missing = [f"missing {key}" for key in ("since", "reason") if not self.fields.get(key)]
        if not self.lists_probes:
            missing.append("missing lifts-when")
        elif not self.probes:
            missing.append("lifts-when names no probe")
        return missing

    @property
    def block(self) -> str:
        return FENCE_RE.search(self.body).group(1)  # type: ignore[union-attr]

    def with_field(self, key: str, value: str) -> str:
        """The comment body with `key: value` set on the block, in place or appended."""
        lines = self.block.rstrip("\n").split("\n")
        replaced = False
        for index, line in enumerate(lines):
            if re.match(rf"^  {re.escape(key)}:", line):
                lines[index] = f"  {key}: {value}"
                replaced = True
        if not replaced:
            lines.append(f"  {key}: {value}")
        return self.body.replace(self.block, "\n".join(lines) + "\n", 1)

    def moved_field(self, old: str, key: str, value: str) -> str:
        """The body with `old` dropped and `key: value` set: `lifting` becoming `lifted`."""
        lines = [line for line in self.block.rstrip("\n").split("\n")
                 if not re.match(rf"^  {re.escape(old)}:", line)]
        trimmed = Record(**{**self.__dict__, "body": self.body.replace(self.block, "\n".join(lines) + "\n", 1)})
        return trimmed.with_field(key, value)


def parse_record(slug: str, number: int, pull_state: str, comment: dict) -> Record | None:
    # A comment written in the web UI carries CRLF; the block is read either way.
    body = (comment.get("body") or "").replace("\r\n", "\n")
    match = FENCE_RE.search(body)
    if not match:
        return None
    record = Record(slug, number, pull_state, comment["id"], comment["user"]["login"], body)
    in_list = False
    for line in match.group(1).split("\n")[1:]:
        if not line.strip():
            continue
        if re.match(r"^  lifts-when:\s*$", line):
            in_list = True
            record.lists_probes = True
            continue
        if in_list and re.match(r"^\s+-\s", line):
            record.probes.append(parse_probe(line))
            continue
        in_list = False
        pair = re.match(r"^  ([a-z-]+):\s*(.*?)\s*$", line)
        if pair:
            record.fields[pair.group(1)] = pair.group(2)
    return record


def render_block(since: str, reason: str, probes: list[Probe], carried_from: int) -> str:
    lines = ["sd-hold:", f"  since: {since}", f"  reason: {reason}", "  lifts-when:"]
    lines.extend(f"    {probe.line}" for probe in probes)
    lines.append(f"  carried-from: #{carried_from}")
    return "```yaml\n" + "\n".join(lines) + "\n```"


# --- the pass ---------------------------------------------------------------


def owner_gate(slug: str) -> str | None:
    """Why the owner gate refuses `slug`, or None when it admits it."""
    if slug in DENIED:
        return "denied by name"
    if slug.split("/")[0] in ALLOWED_OWNERS or slug in ALLOWED_REPOS:
        return None
    return "owner not authorized"


def fleet(root: Path) -> list[str]:
    """Every checkout one or two levels under `root`, as its origin's slug.

    The same corpus as ROUTINE.md's `find "$HOME/repos" -mindepth 2 -maxdepth 3 -name .git`.
    """
    slugs: list[str] = []
    for git_dir in sorted(list(root.glob("*/.git")) + list(root.glob("*/*/.git"))):
        try:
            url = subprocess.run(["git", "-C", str(git_dir.parent), "remote", "get-url", "origin"],
                                 capture_output=True, text=True, check=True).stdout.strip()
        except (subprocess.CalledProcessError, OSError):
            continue
        match = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", url)
        if match:
            slug = f"{match.group(1)}/{match.group(2)}"
            if slug not in slugs:
                slugs.append(slug)
    return slugs


class Watcher:
    def __init__(self, github: GitHub, registry: Registry, today: str, out=sys.stdout):
        self.github = github
        self.registry = registry
        self.today = today
        self.out = out
        self.login = github.login()
        self.totals: dict[str, int] = {}

    def log(self, word: str, rest: str) -> None:
        self.totals[word] = self.totals.get(word, 0) + 1
        print(f"{word} {rest}", file=self.out)

    # -- discovery --

    def run(self, slugs: list[str]) -> None:
        for slug in slugs:
            refusal = owner_gate(slug)
            if refusal:
                self.log("SKIP", f"{slug} {refusal}")
                continue
            permission = self.github.viewer_permission(slug)
            if permission not in WRITE_VIEWER:
                self.log("SKIP", f"{slug} no write access")
                continue
            # One repository's failed read is a named gap, not the end of the
            # pass: a rate limit partway through the fleet is ordinary.
            try:
                self.repository(slug)
            except ReadFailed as error:
                self.log("UNREADABLE", f"{slug}: {error}")
        summary = ", ".join(f"{count} {word}" for word, count in sorted(self.totals.items()))
        print(f"TOTAL {summary or 'nothing watched'}", file=self.out)

    def repository(self, slug: str) -> None:
        pulls = {p["number"]: p for p in self.github.dependabot_pulls(slug)}
        records: dict[int, list[Record]] = {}
        comments: dict[int, list[dict]] = {}
        writers: dict[str, bool] = {}
        ignored: list[str] = []
        for number in sorted(pulls):
            if not pulls[number].get("comments"):
                continue
            comments[number] = self.github.comments(slug, number)
            for comment in comments[number]:
                record = parse_record(slug, number, pulls[number]["state"], comment)
                if record is None:
                    continue
                author = record.author
                if author not in writers:
                    writers[author] = self.github.can_write(slug, author)
                if not writers[author]:
                    ignored.append(f"#{number} {slug} {author} no write access")
                    continue
                records.setdefault(number, []).append(record)
        print(f"SCANNED {slug} {len(pulls)} Dependabot pull requests, "
              f"{sum(len(r) for r in records.values())} records", file=self.out)
        for line in ignored:
            self.log("IGNORED", line)
        # Closed records first: a carry adds an open record to evaluate below.
        for number in sorted(records):
            for record in list(records[number]):
                if record.pull_state != "open":
                    carried = self.supersession(record, pulls, records, comments.get(number, []))
                    if carried is not None:
                        records.setdefault(carried.number, []).append(carried)
        for number in sorted(records):
            for record in records[number]:
                if record.pull_state == "open":
                    self.evaluate(record, comments.get(number, []))

    # -- supersession --

    def supersession(self, record: Record, pulls: dict, records: dict, comments: list[dict]) -> Record | None:
        where = f"#{record.number} {record.slug}"
        if "lifted" in record.fields:
            self.log("SKIP", f"{where} closed, lifted {record.fields['lifted'].split()[0]}")
            return None
        if "carried-to" in record.fields:
            self.log("SKIP", f"{where} closed, already carried to {record.fields['carried-to']}")
            return None
        successor = None
        for comment in comments:
            if comment["user"]["login"] != DEPENDABOT:
                continue
            match = SUPERSEDED_RE.search(comment.get("body") or "")
            if match:
                successor = int(match.group(1))
        if successor is None:
            self.log("SKIP", f"{where} closed, no `Superseded by` comment from Dependabot")
            return None
        target = pulls.get(successor)
        if target is None or target["state"] != "open":
            self.log("SKIP", f"{where} closed, superseded by #{successor} which is not open")
            return None
        if successor in records:
            self.log("SKIP", f"{where} closed, superseded by #{successor} which already carries a record")
            return None
        if record.problems:
            for problem in record.problems:
                self.log("MALFORMED", f"{where} {problem}")
            return None
        malformed = [probe for probe in record.probes if probe.malformed]
        if malformed:
            # Decided without a read, so it can block a carry the way it
            # blocks a lift; `unknown` needs the read the carried record gets.
            for probe in malformed:
                self.log("MALFORMED", f"{where} {probe.line}")
            return None
        block = render_block(record.fields.get("since", self.today), record.fields.get("reason", ""),
                             record.probes, record.number)
        body = (f"Hold carried from #{record.number}, which Dependabot superseded with this pull "
                f"request. The blocker and the probes are unchanged.\n\n{block}\n")
        if self.github.dry_run:
            self.log("WOULD-CARRY", f"#{record.number} -> #{successor} {record.slug}")
            # Evaluate the record it would post, so the dry run names the lift too.
            return parse_record(record.slug, successor, "open",
                                {"id": 0, "user": {"login": self.login}, "body": body})
        posted = self.github.post_comment(record.slug, successor, body)
        self.github.edit_comment(record.slug, record.comment_id, record.with_field("carried-to", f"#{successor}"))
        self.log("CARRIED", f"#{record.number} -> #{successor} {record.slug}")
        posted.setdefault("user", {"login": self.login})
        posted.setdefault("body", body)
        return parse_record(record.slug, successor, "open", posted)

    # -- evaluation --

    def evaluate(self, record: Record, comments: list[dict]) -> None:
        where = f"#{record.number} {record.slug}"
        if "lifted" in record.fields:
            self.log("LIFTED-EARLIER", f"{where} {record.fields['lifted'].split()[0]}")
            return
        if "lifting" in record.fields:
            self.resume(record, comments)
            return
        if record.problems:
            # The record is wrong, not the world: no probe is read.
            for problem in record.problems:
                self.log("MALFORMED", f"{where} {problem}")
            return
        lifted_by: tuple[Probe, str] | None = None
        blocked = False
        for probe in record.probes:
            if probe.malformed:
                self.log("MALFORMED", f"{where} {probe.line}")
                blocked = True
                continue
            try:
                true, evidence = self.probe(probe)
            except ReadFailed as error:
                self.log("UNKNOWN", f"{where} {probe}: {error}")
                blocked = True
                continue
            if true and lifted_by is None:
                lifted_by = (probe, evidence)
        if lifted_by is not None:
            self.lift(record, *lifted_by)
        elif not blocked:
            self.log("HELD", f"{where} {len(record.probes)} probes, none lifted")

    def probe(self, probe: Probe) -> tuple[bool, str]:
        if probe.kind == "issue-closed":
            slug, number = probe.parts
            state, url = self.github.issue_state(slug, number)
            return state == "closed", f"{url} {state}"
        if probe.kind == "npm-version":
            package, operator, bound = probe.parts
            latest, _ = self.registry.latest(package)
            return compare(latest, operator, bound), f"latest {latest}"
        package, dependency, wanted = probe.parts
        latest, dependencies = self.registry.latest(package)
        declared = dependencies.get(dependency)
        if declared is None:
            return False, f"{latest} does not declare {dependency}"
        try:
            admits = ranges_overlap(declared, wanted)
        except ValueError:
            raise ReadFailed(f"{latest} declares {dependency} {declared!r}, which is not a range this reads")
        return admits, f"{'satisfied' if admits else 'not satisfied'} by {latest} ({dependency} {declared})"

    # -- the lift sequence --

    def lift(self, record: Record, probe: Probe, evidence: str) -> None:
        where = f"#{record.number} {record.slug}"
        detail = f"{probe} {evidence}"
        if self.github.dry_run:
            self.log("WOULD-LIFT", f"{where} {detail}")
            return
        self.github.edit_comment(record.slug, record.comment_id,
                                 record.with_field("lifting", f"{self.today} {detail}"))
        self.github.post_comment(record.slug, record.number, self.lifted_comment(record, probe, evidence))
        self.github.post_comment(record.slug, record.number, "@dependabot rebase")
        self.github.edit_comment(record.slug, record.comment_id,
                                 record.moved_field("lifting", "lifted", f"{self.today} {detail}"))
        self.log("LIFTED", f"{where} {detail}")

    def resume(self, record: Record, comments: list[dict]) -> None:
        where = f"#{record.number} {record.slug}"
        detail = record.fields["lifting"]
        if self.github.dry_run:
            self.log("WOULD-LIFT", f"{where} (resumed) {detail}")
            return
        mine = [c for c in comments if c["user"]["login"] == self.login and c["id"] > record.comment_id]
        if not any(f"sd-hold-lifted #{record.number}" in (c.get("body") or "") for c in mine):
            self.github.post_comment(record.slug, record.number,
                                     self.lifted_comment(record, None, detail))
        if not any((c.get("body") or "").strip() == "@dependabot rebase" for c in mine):
            self.github.post_comment(record.slug, record.number, "@dependabot rebase")
        self.github.edit_comment(record.slug, record.comment_id,
                                 record.moved_field("lifting", "lifted", detail))
        self.log("LIFTED (resumed)", f"{where} {detail}")

    def lifted_comment(self, record: Record, probe: Probe | None, evidence: str) -> str:
        named = f"`{probe}` -- {evidence}" if probe else evidence
        return (f"sd-hold-lifted #{record.number}: {named}\n\n"
                f"The hold recorded on {record.fields.get('since', 'an earlier date')} has lifted, "
                f"so this asks Dependabot to rebase. Nothing merges here: a lifted major is still a "
                f"major, and the daily sweep's four conditions and the owner decide the rest.")


# --- entrypoint -------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dependabot.sh holds")
    parser.add_argument("--dry-run", action="store_true", help="read and report; write nothing")
    parser.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                        help="watch this repository instead of enumerating the fleet")
    parser.add_argument("--fleet-root", default=os.environ.get("SD_HOLDS_FLEET_ROOT")
                        or os.path.join(os.path.expanduser("~"), "repos"))
    args = parser.parse_args(argv)
    today = os.environ.get("SD_HOLDS_TODAY") or dt.date.today().isoformat()
    try:
        watcher = Watcher(GitHub(dry_run=args.dry_run), Registry(), today)
        slugs = args.repo or fleet(Path(args.fleet_root))
        if not slugs:
            raise CannotRun(f"no checkouts under {args.fleet_root}")
        watcher.run(slugs)
    except CannotRun as error:
        print(f"dependabot holds: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
