"""Jev experiment, journal entry 2 (sd:2764): one verb per step of its plan.

The plan is `docs/work/2026-10-05-jev-experiment-entry-2/` and the binding
rules are `docs/experiments/2026-10-05-jev-entry-2-preregistration.md`. This
file holds the steps built so far:

* `reconcile` (step 0) counts entry 1's tier readings under each extraction,
  so the one behind its published numbers is named. No model call.
* `heuristic` (step 3) checks the frozen heuristic file against the hash the
  pre-registration names, and refuses any other.
* `faithful` (step 1) rebuilds 50 states from stored checkpoints and asks Jev
  once each, the way the pack asks it, writing to the experiment's own ledger.
* `population` (step 2) lists every reviewed head of C1 and E with its state,
  from the checkpoint or rebuilt with git and the pinned pack's router. It
  reads routing only: no finding, review count or status.
* `labels objective` (step 4) writes blocking@head per head of one
  population; it refuses C1 until its answer files are sealed, and C2.
* `replay --arm jev|kev|haiku` (step 5) asks one model arm once per head on
  the same redacted payload, under the stopping rule: registration guard,
  frozen heuristic, ledger isolation, public-pool scope, one retry, spend cap,
  Kev outage.
* `labels sheet --population C1` (step 8) draws the blinded hand-label sheet,
  its Markdown copy and its key from one population's sealed answer files;
  it reads no finding, and refuses C2 until step 15 seals it.

**It reads the workflow database read-only.** Every query opens it through
`sd_db`'s `connect(write=False)`, which is `mode=ro` and `query_only`;
nothing here writes a checkpoint or a live ledger row.

**It never writes the live ledger.** A Jev call runs `jev.sh` with
`JEV_METER_DB` pointed at the experiment database, both comparison switches
empty, shadow off and traces off, and refuses to start while `jev_meter.ready`
refuses that database.

**It sends no path but a public one.** A hosted arm is never asked about a
state holding a path sign, or a `/` in anything but a path a public managed
repository tracks; Kev, which runs here, is asked about every state (the
2026-10-11 amendment, decision #14288).

**It draws only public-repository states** (operator, 2026-10-11): those of a
public managed repository whose changed paths that repository still tracks.
The privacy check runs on every state of that pool before the draw, so the
sample leans to public-repository work and the results say so.

Stdlib and `sd_db`, the one opener of the workflow database. Raw output goes
to `--out`, outside the checkout.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import datetime
import hashlib
import ipaddress
import json
import math
import os
import random
import re
import secrets
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import jev_meter

FOLDER = Path(__file__).resolve().parent

#: The seed every draw uses (pre-registration, "Faithfulness gate").
SEED = 2764

#: The heuristic file and its registered SHA-256 (pre-registration, R3).
HEURISTIC = FOLDER / "jev_experiment_heuristic.py"
HEURISTIC_SHA256 = "b637100886386bc803d23e276df6fa85b4aef438691f748cca482b41ebdf58a7"

#: Entry 1's frozen window, from its research notes: checkpoints from the
#: first to the last reading it counted, in UTC.
ENTRY1_FROM = "2026-09-21T06:55:07"
ENTRY1_TO = "2026-09-29T20:10:00"

#: Population C1's first day in UTC (pre-registration, "Populations"); C1
#: runs to the day before E's first.
C1_FROM = "2026-09-09"

#: Population E's window in UTC: 2026-09-21 to 2026-10-05, both days whole.
E_FROM = "2026-09-21"
E_TO = "2026-10-06"

#: The pack's tier order when a policy declares none (`sd_route`), and each
#: tier's criterion text, copied from the pack's `bin/sd_jev.py`. The question
#: and these four did not change from 2026-09-20 to 2026-10-05 (design.md).
TIERS = ("skip", "cheap", "standard", "deep")
QUESTION = "How deeply should this code change be reviewed?"
TIER_CRITERIA = {
    "skip": "no review is warranted because the change cannot alter behaviour",
    "cheap": "one quick read is enough because the change is small and local",
    "standard": "an ordinary careful review of the changed logic",
    "deep": "the change is risky or wide-reaching or touches security"
            " and needs the most thorough review",
}
MAX_PATHS = 40

#: The pack commit whose router and default policy rebuild a state from git
#: (design.md, "From git"): the one the suite's `PACK_STATE_SHA256` was taken
#: at. The pack's checkout, under HOME, as `jev.sh` finds its interpreter.
PACK_SHA = "b6b58bb14aa5105eae7d62f2e70f0a38e1f4c61b"
PACK_HOME = "repos/platypeeps/sd-ai-command-pack"

#: What `sd-review` appends to the routing reason once Jev answered; the
#: state Jev saw holds the reason before it (design.md, "Rebuilding each state").
JEV_SUFFIX = "; Jev read the diff and chose tier "

#: The faithfulness gate: 50 states, 25 of them moved, 46 matches to pass.
FAITHFUL_COUNT = 50
FAITHFUL_PASS = 46

#: The stopping rule's one retry, after 30 s.
RETRY_WAIT = 30.0


class Refused(RuntimeError):
    """A rule of the pre-registration stopped this step."""


# --- the workflow database, read-only ----------------------------------------

def db_path(raw) -> Path:
    """The one spelling of a database path that every identity check and every
    open after it uses: stripped as `jev_meter` strips `JEV_METER_DB`, `~`
    expanded, links and `..` resolved (review round 3). Apply it once: a
    resolved name may end in space, which a second pass would strip."""
    return Path(os.path.realpath(os.path.expanduser(str(raw).strip())))


def open_ro(path: str | Path):
    # Through `sd_db`, because nothing outside it opens the database
    # (`local-sd-db/tests/test_one_store.py`). Its `connect` opens the
    # resolved path's `as_uri`, which escapes `?` and `#`, so the file opened
    # is the one named and `mode=ro` is the only parameter. No `db_path`
    # here: the caller's path is already the one its identity check judged,
    # and resolving it again strips nothing. The checkout's own `sd_db` is
    # the fallback, as in `jev_meter`'s tests.
    try:
        from sd_db.database import connect
    except ImportError:
        sys.path.insert(0, str(FOLDER.parent / "local-sd-db"))
        from sd_db.database import connect
    return connect(Path(path), write=False)


def checkpoints(db, since: str, until: str):
    """Each checkpoint row in [since, until) whose body parses, as
    (id, key, timestamp, body). A body that does not parse is skipped here and
    counted by `invalid_bodies`."""
    with contextlib.closing(open_ro(db)) as connection:
        rows = connection.execute(
            "SELECT id, key, timestamp, body FROM state WHERE kind = 'checkpoint'"
            " AND timestamp >= ? AND timestamp < ? ORDER BY timestamp, id",
            (since, until)).fetchall()
    for row in rows:
        try:
            body = json.loads(row["body"] or "")
        except ValueError:
            continue
        if isinstance(body, dict):
            yield row["id"], row["key"] or "", row["timestamp"], body


def invalid_bodies(db, since: str, until: str) -> int:
    with contextlib.closing(open_ro(db)) as connection:
        rows = connection.execute(
            "SELECT body FROM state WHERE kind = 'checkpoint'"
            " AND timestamp >= ? AND timestamp < ?", (since, until)).fetchall()
    count = 0
    for (body,) in rows:
        try:
            if not isinstance(json.loads(body or ""), dict):
                count += 1
        except ValueError:
            count += 1
    return count


def passes(body: dict):
    found = body.get("passes")
    return [p for p in found if isinstance(p, dict)] if isinstance(found, list) else []


def report(one_pass: dict) -> dict:
    found = one_pass.get("report")
    return found if isinstance(found, dict) else {}


def reading(one_pass: dict) -> dict | None:
    found = report(one_pass).get("jev")
    return found if isinstance(found, dict) and found.get("tier") else None


def raw_tier(jev_reading: dict) -> str:
    """What Jev answered: `below_routed` once the floor hid a lower answer."""
    return jev_reading.get("below_routed") or jev_reading["tier"]


# --- step 0: which extraction is entry 1 -------------------------------------

def tally(first: dict) -> dict:
    """Entry 1's counts over first readings: heads, agreed, moved, up, down."""
    counts = {"heads": 0, "agreed": 0, "moved": 0, "up": 0, "down": 0,
              "to_skip": 0, "to_cheap": 0, "to_deep": 0}
    for routed, tier in first.values():
        counts["heads"] += 1
        if tier == routed:
            counts["agreed"] += 1
            continue
        counts["moved"] += 1
        if tier in TIERS and routed in TIERS:
            counts["up" if TIERS.index(tier) > TIERS.index(routed) else "down"] += 1
        if f"to_{tier}" in counts:
            counts[f"to_{tier}"] += 1
    return counts


def extraction_entry1(rows) -> dict:
    """Entry 1's recount: every checkpoint key, every pass with a reading, the
    earliest pass per (repository, pass head)."""
    first = {}
    for _id, _key, _stamp, body in rows:
        for one in passes(body):
            found = reading(one)
            key = (body.get("repository"), one.get("head"))
            if found and key not in first:
                first[key] = (found.get("routed_tier"), found["tier"])
    return first


def extraction_a(rows) -> dict:
    """design.md's A: every pass with a reading, keyed by
    `report.subject.head`, the first reading per head."""
    first = {}
    for _id, _key, _stamp, body in rows:
        for one in passes(body):
            found = reading(one)
            subject = report(one).get("subject")
            head = subject.get("head") if isinstance(subject, dict) else None
            if found and head and head not in first:
                first[head] = (found.get("routed_tier"), found["tier"])
    return first


def extraction_b(rows) -> dict:
    """design.md's B: `passes[0]` only, keyed by `coalesce(reviewed_head, head)`
    of the checkpoint."""
    first = {}
    for _id, _key, _stamp, body in rows:
        head = body.get("reviewed_head") or body.get("head")
        listed = passes(body)
        found = reading(listed[0]) if listed else None
        if found and head and head not in first:
            first[head] = (found.get("routed_tier"), found["tier"])
    return first


EXTRACTIONS = {"entry1": extraction_entry1, "A": extraction_a, "B": extraction_b}


def cmd_reconcile(args, out, env) -> int:
    db = args.db or default_db(env)
    rows = list(checkpoints(db, args.since, args.until))
    result = {"window": [args.since, args.until],
              "invalid_bodies": invalid_bodies(db, args.since, args.until),
              "extractions": {name: tally(build(rows)) for name, build in EXTRACTIONS.items()}}
    json.dump(result, out, indent=2, sort_keys=True)
    out.write("\n")
    return 0


# --- step 3: the frozen heuristic ---------------------------------------------

def heuristic_sha256(path: Path = HEURISTIC) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_heuristic(path: Path = HEURISTIC):
    """The registered `heuristic` function, or Refused when the file differs."""
    found = heuristic_sha256(path)
    if found != HEURISTIC_SHA256:
        raise Refused(f"{path.name} has SHA-256 {found}, not the registered "
                      f"{HEURISTIC_SHA256}; a changed heuristic is a new arm")
    scope: dict = {}
    exec(compile(path.read_bytes(), str(path), "exec"), scope)  # noqa: S102 - the hashed file
    return scope["heuristic"]


def cmd_heuristic(args, out, env) -> int:
    frozen_heuristic()
    out.write(f"{HEURISTIC_SHA256}  {HEURISTIC.name}\n")
    return 0


# --- step 1: the faithfulness gate --------------------------------------------

def routing_said(reason: str) -> str:
    """The routing reason as Jev saw it: without the suffix `sd-review` adds."""
    cut = reason.find(JEV_SUFFIX)
    return reason if cut < 0 else reason[:cut]


def state_of(paths, lines: int, reason: str) -> str:
    """The pack's `_jev_state`, byte for byte: the first 40 paths as given."""
    shown = [str(path) for path in paths][:MAX_PATHS]
    return json.dumps({
        "changed_paths": shown,
        "changed_paths_omitted": len(paths) - len(shown),
        "path_count": len(paths),
        "lines_moved": lines,
        "deterministic_routing_said": reason,
    }, sort_keys=True)


def state_from(found: dict) -> str:
    """A head's state from what `stored` or git gave: the one way step 1's
    draw and step 2's population build one."""
    return state_of(found["paths"], found["lines"], found["reason"])


def criteria(order=TIERS) -> str:
    return ",".join(f"{name}={TIER_CRITERIA[name]}" for name in order)


#: A path sign anywhere in a string (review round 4; rounds 1 to 3 each let
#: some character before a slash pass, and each round found one that hid a
#: root): any `\`, `~` or `file:`, any `$` or `%` with something other than
#: space after it, and any control character; `private_path` adds any Unicode
#: format character (a bidi override, a zero-width space). A `/` is judged per
#: token by `public_path`, the one exception the operator allowed.
SIGNS = re.compile(r"[\\~\x00-\x1f\x7f-\x9f]|[$%]\S|(?i:file):")

#: What may wrap a path in prose, stripped from a token's ends before it is
#: compared. Never `/`, so the compared token keeps every slash, and never a
#: leading `.`, so `./x` and `../x` stay themselves and `.github/x` matches.
LEADING = "\"'`*_([{<\u2018\u201c"
TRAILING = "\"'`*_)]}>.,;:!?\u2019\u201d"


def path_token(piece: str) -> str:
    """A whitespace-split piece without the punctuation that wraps it: the
    string `public_path` compares, and the only one."""
    return piece.lstrip(LEADING).rstrip(TRAILING)


def public_path(piece: str, public: frozenset) -> bool:
    """Whether a piece with a `/` in it is exactly a path a public managed
    repository tracks (`public_paths`). `git ls-files` never lists a name
    with a leading `/` or a `.` or `..` segment, so a root, `./` or `../`
    before a tracked path is not that path."""
    return path_token(piece) in public


def git(root: str, *argv: str) -> bytes | None:
    """`git -C root argv` output, or None when git cannot answer. Without any
    inherited `GIT_*` variable: `GIT_DIR` would answer for another repository."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        done = subprocess.run(["git", "-C", root, *argv], capture_output=True,
                              env=env, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def public_paths(db) -> frozenset:
    """Every path a public managed repository tracks (`public_repos`)."""
    return frozenset().union(*public_repos(db).values())


def public_repos(db, failed: set | None = None) -> dict[str, frozenset]:
    """Each public managed repository's `owner/name`, lower-cased, with every
    path `git ls-files` lists in its local checkouts, read once per run. A
    managed repository is public when its last observed visibility is.
    Public is what `sd shadow sync` stored (`sd_db.protection.alerts`), the
    same reading Health's Security area uses. `protection.rows` lends an
    unobserved checkout the row of another with the same stored remote, so
    the checkout's own `origin` must be that remote (codex pass on round 4).
    A repository not read as public, a checkout whose `origin` differs or is
    missing, or one git cannot list, adds nothing: unknown is not public.
    Checkouts of one repository add to one entry. `failed`, when given,
    gets each public repository with a checkout git could not answer for."""
    from sd_db import protection
    from sd_db.paths import home_absolute
    with contextlib.closing(open_ro(db)) as connection:
        repos = protection.rows(connection)
    tracked: dict[str, set] = {}
    for repo in repos:
        part = (repo.get("alerts") or {}).get("secret_scanning")
        if not repo["managed"] or not isinstance(part, dict) or part.get("visibility") != "public":
            continue
        root = home_absolute(repo["repo"])
        origin = git(root, "config", "--get", "remote.origin.url")
        slug = protection.github_slug(origin.decode("utf-8", "replace").strip()) if origin else None
        ours = bool(slug and repo["slug"] and "/".join(slug).lower() == repo["slug"].lower())
        listed = git(root, "ls-files", "-z") if ours else None
        if listed is None:
            # Another repository's checkout is not a failure; no answer is.
            if failed is not None and (origin is None or ours):
                failed.add(repo["slug"] or str(root))
            continue
        names = tracked.setdefault(repo["slug"].lower(), set())
        for raw in listed.split(b"\0"):
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if name:
                names.add(name)
    return {slug: frozenset(names) for slug, names in tracked.items()}


def private_path(state: str, home: str | None, public: frozenset = frozenset()) -> str | None:
    """The first string in the state that holds a path sign, or a `/` in a
    token that is not a tracked public path, else None. A changed path passes
    only as a whole tracked public path, `/` or not (codex pass on round 4)."""
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                yield str(key)
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
    found = json.loads(state)
    paths = found.get("changed_paths") if isinstance(found, dict) else None
    for path in paths if isinstance(paths, list) else ():
        if path not in public:
            return str(path)
    for text in strings(found):
        if SIGNS.search(text) or (home and home in text) \
                or any(unicodedata.category(char) == "Cf" for char in text):
            return text
        if any("/" in piece and not public_path(piece, public) for piece in text.split()):
            return text
    return None


def candidates(db) -> list[dict]:
    """E heads with a recorded reading and a stored subject: the first such pass
    per (repository, pass head), with the state rebuilt from that pass."""
    found = {}
    for _id, key, stamp, body in checkpoints(db, E_FROM, E_TO):
        for one in passes(body):
            got = reading(one)
            head = one.get("head")
            ident = (body.get("repository") or "", head)
            if not got or not head or ident in found:
                continue
            # The state the population gives a pass, from the same two functions.
            kept = stored(routing_view(one))
            routed = got.get("routed_tier")
            if kept is None or routed not in TIERS or raw_tier(got) not in TIERS:
                continue
            found[ident] = {
                "repository": ident[0], "head": head, "checkpoint": key, "at": stamp,
                "routed": routed, "recorded": raw_tier(got),
                "moved": raw_tier(got) != routed,
                "state": state_from(kept),
            }
    return sorted(found.values(), key=lambda c: (c["repository"], c["head"]))


def public_pool(found: list[dict], repos: dict, home: str | None,
                public: frozenset) -> tuple[list[dict], int]:
    """The states step 1 may draw (operator, 2026-10-11): those of a public
    managed repository whose changed paths it still tracks. Also how many of
    them the privacy check halts, which must be none before any call."""
    pool, halted = [], 0
    for one in found:
        tracked = repos.get(one["repository"].lower())
        if tracked is None or not all(
                path in tracked for path in json.loads(one["state"])["changed_paths"]):
            continue
        pool.append(one)
        halted += private_path(one["state"], home, public) is not None
    return pool, halted


def draw(pool: list[dict], count: int = FAITHFUL_COUNT, seed: int = SEED,
         exclude=frozenset()) -> list[dict]:
    """Half from moved heads, or every moved head when there are fewer, the
    rest from the others, seeded; `exclude` holds the heads an earlier attempt
    drew, so a second attempt uses new states."""
    rng = random.Random(seed)
    usable = [c for c in pool if (c["repository"], c["head"]) not in exclude]
    moved = [c for c in usable if c["moved"]]
    kept = [c for c in usable if not c["moved"]]
    take = min(count // 2, len(moved))
    if len(kept) < count - take:
        raise Refused(f"too few states to draw: {len(moved)} moved, {len(kept)} not")
    return rng.sample(moved, take) + rng.sample(kept, count - take)


def jev_env(env, ledger: Path, corpus: Path) -> dict:
    """The child's environment: the experiment ledger, no arm, no shadow."""
    child = dict(env)
    child.update({
        "JEV_METER": "1", "JEV_METER_DB": str(ledger), "JEV_CORPUS_DIR": str(corpus),
        # Set and empty: `jev.sh` keeps an exported empty switch over the
        # `.env`'s value, so no comparison arm starts.
        "JEV_COMPARE_STAGES": "", "JEV_COMPARE_HAIKU_VIA": "",
        "JEV_SHADOW": "off", "JEV_ENABLED": "on", "JEV_TRACES_URL": "off",
        "JEV_CALLER": "jev-experiment",
        # The meter writes with the interpreter that checked the ledger.
        "PYTHON": sys.executable,
    })
    return child


def ask_jev(state: str, env, subject: str | None = None, sleep=time.sleep) -> dict:
    """One `jev choice` as the pack asks it, without `--unsure-below`, so the
    full answer comes back. One retry after 30 s; then no answer."""
    argv = ["sh", str(FOLDER / "jev.sh"), "choice", QUESTION, "--criteria", criteria(),
            "--state", "-", "--state-format", "json", "--caller", "jev-experiment",
            "--id", "sd-review-tier", "--stage", "JEV_SD_REVIEW", "--json",
            *(["--subject", subject] if subject else [])]
    tries = []
    for attempt in range(2):
        if attempt:
            sleep(RETRY_WAIT)
        started = time.monotonic()
        try:
            done = subprocess.run(argv, input=state, env=env, capture_output=True,
                                  text=True, timeout=90, check=False)
            code, stdout, stderr = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired:
            code, stdout, stderr = None, "", "timed out after 90 s"
        wait_ms = int(round((time.monotonic() - started) * 1000))
        tries.append({"exit": code, "wait_ms": wait_ms,
                      "error": (stderr or "").strip().splitlines()[-1:] if code else []})
        if code == 0:
            try:
                answer = json.loads(stdout)
            except ValueError:
                answer = None
            if isinstance(answer, dict) and answer.get("choice") in TIERS:
                return {"answer": answer, "tries": tries}
    return {"answer": None, "tries": tries}


def same_file(one: Path, other: Path) -> bool:
    """Whether two `db_path` results name one file: equal, or one file by
    identity. Equal names count even with no file there yet, because on a
    satellite `sd_db` sends the default path to the hub. It normalises
    nothing again: a second pass would judge a path nobody opens."""
    if one == other:
        return True
    try:
        return os.path.samefile(one, other)
    except OSError:
        return False


def isolated(ledger: Path, db: Path, env) -> None:
    """Refuse an experiment ledger that is the live or the source database.
    By file identity, before the ledger is opened or any call is made: a link
    or another spelling of either is that database. Each path is normalised
    once, and the meter gets that exact string. `--ledger` and `--db` are
    `db_path` results already; a second pass would strip a resolved name that
    ends in space."""
    if str(ledger) != str(ledger).strip():
        raise Refused(f"--ledger resolves to {str(ledger)!r}, which ends in space; "
                      "the meter would strip it and open another file")
    if not db.is_file():
        raise Refused(f"--db {db} is not a file")
    for name, other in (("the live workflow database", default_db(env)),
                        ("the --db source database", db)):
        if same_file(ledger, other):
            raise Refused(f"--ledger {ledger} is {name}; experiment rows "
                          "never go to the live ledger")


def cmd_faithful(args, out, env) -> int:
    ledger, db = args.ledger, args.db or default_db(env)
    isolated(ledger, db, env)
    env = jev_env(env, ledger, args.out / "corpus")
    word = jev_meter.ready("jev", env)
    if word != "ready":
        raise Refused(f"the experiment ledger {ledger} refused: {word}")
    exclude = set()
    if args.exclude:
        for line in Path(args.exclude).read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                exclude.add((row["repository"], row["head"]))
    repos = public_repos(db)
    pool, halted = public_pool(candidates(db), repos, env.get("HOME"),
                               frozenset().union(*repos.values()))
    if halted:
        raise Refused(f"privacy halt: {halted} of {len(pool)} states in the public pool "
                      "hold a path sign or an untracked path; the operator reviews "
                      "before any resume")
    chosen = draw(pool, exclude=exclude)
    args.out.mkdir(parents=True, exist_ok=True)
    answers = args.out / "answers.jsonl"
    if answers.exists():
        raise Refused(f"{answers} exists; one pass per arm, and a second run is "
                      "reported as a second run in a folder of its own")
    matched = answered = 0
    with answers.open("w", encoding="utf-8") as fh:
        for one in chosen:
            owner_repo = one["repository"].lower().replace("/", ".")
            subject = f"sd-review-tier:{owner_repo}:{one['head'][:12]}"
            got = ask_jev(one["state"], env, subject if len(subject) <= 96 else None)
            tier = got["answer"]["choice"] if got["answer"] else None
            matched += tier == one["recorded"]
            answered += tier is not None
            fh.write(json.dumps(dict(one, replayed=tier, match=tier == one["recorded"],
                                     **got), sort_keys=True) + "\n")
            fh.flush()
    summary = {"asked": len(chosen), "matched": matched,
               "answered": answered,
               "moved_drawn": sum(1 for c in chosen if c["moved"]),
               "pool": len(pool), "pool_moved": sum(1 for c in pool if c["moved"]),
               "pool_halted": halted, "population": "public managed repositories only",
               "threshold": FAITHFUL_PASS, "passed": matched >= FAITHFUL_PASS,
               "seed": SEED, "finished": datetime.datetime.now(datetime.timezone.utc)
               .isoformat(timespec="seconds")}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                                           encoding="utf-8")
    json.dump(summary, out, indent=2, sort_keys=True)
    out.write("\n")
    return 0 if summary["passed"] else 1


# --- step 2: the population ---------------------------------------------------

def head_of(body: dict) -> str | None:
    """The head a checkpoint reviewed: `coalesce(reviewed_head, head)`."""
    return body.get("reviewed_head") or body.get("head")


def routing_view(one_pass: dict) -> dict:
    """What the population may read of a pass: its head, its base, and the
    report's subject, route and Jev reading. Never a finding, a review count
    or a status, so a C1 outcome stays unread (pre-registration, "Unseen rule")."""
    rep = report(one_pass)
    return {"head": one_pass.get("head"), "base": one_pass.get("base"),
            "subject": rep.get("subject"), "route": rep.get("route"), "jev": rep.get("jev")}


def stored(view: dict) -> dict | None:
    """The four inputs of `_jev_state` and the routed tier, when the pass
    stored them all, else None."""
    subject, route = view["subject"], view["route"]
    if not isinstance(subject, dict) or not isinstance(route, dict):
        return None
    paths, lines, reason = subject.get("paths"), subject.get("lines"), route.get("reason")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths) \
            or type(lines) is not int or not isinstance(reason, str):
        return None
    reading_of = view["jev"] if isinstance(view["jev"], dict) else {}
    return {"paths": paths, "lines": lines, "reason": routing_said(reason),
            "routed": reading_of.get("routed_tier") or route.get("tier"),
            "base": subject.get("base") or view["base"]}


def numstat(root: str, base: str, head: str) -> tuple[list[str], int] | None:
    """The pack's `changed`: sorted distinct paths and lines moved, or None."""
    found = git(root, "diff", "--numstat", "-z", "--no-renames", "--no-ext-diff",
                "--no-textconv", f"{base}..{head}")
    if found is None:
        return None
    paths, lines = set(), 0
    for row in filter(None, found.decode("utf-8", "replace").split("\0")):
        added, removed, name = row.split("\t", 2)
        paths.add(name)
        lines += sum(int(value) for value in (added, removed) if value.isdigit())
    return sorted(paths), lines


def pinned_router(pack: Path):
    """`route(paths, lines, policy_text) -> (tier, reason)` from the pack's
    `sd_route.py` and `sd-review`'s `DEFAULT_POLICY` at `PACK_SHA`, or None
    when the pack checkout cannot show them. A repository's own
    `.github/sd-review.json` at the head is laid over the default, as
    `validate_policy` does; a draft is never assumed."""
    import ast
    import types
    source = git(str(pack), "show", f"{PACK_SHA}:bin/sd_route.py")
    script = git(str(pack), "show", f"{PACK_SHA}:bin/sd-review")
    if source is None or script is None:
        return None
    module = types.ModuleType("sd_route_pinned")
    exec(compile(source, "sd_route.py", "exec"), module.__dict__)  # noqa: S102 - the pinned pack
    default = None
    for node in ast.parse(script).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == "DEFAULT_POLICY":
            default = ast.literal_eval(node.value)
    if not isinstance(default, dict):
        return None

    def route(paths, lines, policy_text):
        policy = dict(default)
        if policy_text is not None:
            policy.update({k: v for k, v in json.loads(policy_text).items() if k != "$schema"})
        plan = module.route(paths, lines, False, policy)
        return plan.tier, plan.reason
    return route


def clones(db) -> dict[str, list[str]]:
    """Each managed repository's `owner/name`, lower-cased, with its local
    checkouts whose `origin` is that remote."""
    from sd_db import protection
    from sd_db.paths import home_absolute
    with contextlib.closing(open_ro(db)) as connection:
        repos = protection.rows(connection)
    found: dict[str, list[str]] = {}
    for repo in repos:
        root = home_absolute(repo["repo"])
        if not repo["slug"] or not root:
            continue
        origin = git(root, "config", "--get", "remote.origin.url")
        slug = protection.github_slug(origin.decode("utf-8", "replace").strip()) if origin else None
        if slug and "/".join(slug).lower() == repo["slug"].lower():
            found.setdefault(repo["slug"].lower(), []).append(root)
    return found


def holding(roots: list[str], head: str) -> str | None:
    """The first checkout that still holds the head's objects."""
    return next((root for root in roots
                 if git(root, "cat-file", "-e", f"{head}^{{commit}}") is not None), None)


def from_git(roots: list[str], head: str, bases: list, route) -> tuple[dict | None, str]:
    """A state rebuilt with git and the pinned router, as (inputs, "") or
    (None, why not). A base is a pass's commit, else the merge base with the
    checkpoint's branch."""
    if not roots:
        return None, "no-clone"
    root = holding(roots, head)
    if root is None:
        return None, "objects-gone"
    if route is None:
        return None, "no-router"
    base = None
    for candidate in filter(None, bases):
        if re.fullmatch(r"[0-9a-f]{40}", candidate):
            if git(root, "cat-file", "-e", f"{candidate}^{{commit}}") is not None:
                base = candidate
        else:
            for ref in (f"origin/{candidate}", candidate):
                merged = git(root, "merge-base", head, ref)
                if merged:
                    base = merged.decode().strip()
                    break
        if base:
            break
    if not base:
        return None, "no-base"
    changed = numstat(root, base, head)
    if changed is None:
        return None, "objects-gone"
    policy = git(root, "show", f"{head}:.github/sd-review.json")
    try:
        tier, reason = route(changed[0], changed[1],
                             policy.decode("utf-8") if policy is not None else None)
    except Exception:                        # a policy the pinned router cannot read
        return None, "policy-invalid"
    return {"paths": changed[0], "lines": changed[1], "reason": reason, "routed": tier,
            "base": base}, ""


def population(db, route=None) -> tuple[list[dict], dict]:
    """Every distinct reviewed head from C1's first day to E's last, keyed
    `(repository, coalesce(reviewed_head, head))` of a checkpoint that lists
    review passes, and tagged C1 or E by its first such checkpoint, with its state or the reason it has none, and a summary.
    A state comes from the first pass at that head that stored one, else from
    git; where git still holds a stored state's commits, it checks them."""
    heads: dict[tuple, dict] = {}
    views: dict[tuple, list] = {}
    no_head = unreviewed = 0
    for _id, key, stamp, body in checkpoints(db, C1_FROM, E_TO):
        repository = body.get("repository") or ""
        head = head_of(body)
        if not passes(body):
            # A gate receipt or a contribution names a head it did not review.
            unreviewed += 1
            continue
        if not head:
            no_head += 1
            continue
        heads.setdefault((repository, head), {"first": stamp, "checkpoint": key,
                                              "branch_base": body.get("base")})
        for one in passes(body):
            view = routing_view(one)
            if view["head"]:
                views.setdefault((repository, view["head"]), []).append(view)
    paper = set(extraction_entry1(checkpoints(db, ENTRY1_FROM, ENTRY1_TO)))
    repos = clones(db)
    rows = []
    for (repository, head), seen in sorted(heads.items()):
        at_head = views.get((repository, head), [])
        found = next(filter(None, map(stored, at_head)), None)
        first = next((v["jev"] for v in at_head
                      if isinstance(v["jev"], dict) and v["jev"].get("tier")), None)
        roots = repos.get(repository.lower(), [])
        row = {"repository": repository, "head": head, "first_at": seen["first"],
               "checkpoint": seen["checkpoint"],
               "population": "C1" if seen["first"] < E_FROM else "E",
               "recorded": raw_tier(first) if first else None,
               "paper": (repository, head) in paper,
               "source": "checkpoint", "dropped": None, "git_check": "unverified"}
        row["e0"] = row["population"] == "E" and first is None
        if found is None:
            found, why = from_git(roots, head, [v["base"] for v in at_head]
                                  + [seen["branch_base"]], route)
            row.update(source="rebuilt-from-git" if found else None, dropped=why or None)
        elif found["base"] and roots:
            root = holding(roots, head)
            again = numstat(root, found["base"], head) if root else None
            if again is not None:
                row["git_check"] = ("match" if again == (sorted(found["paths"]), found["lines"])
                                    else "mismatch")
        row["routed"] = found["routed"] if found else None
        row["state"] = state_from(found) if found else None
        rows.append(row)
    aliases = {name: f"R{n:02d}" for n, name in
               enumerate(sorted({r["repository"] for r in rows}), 1)}
    for row in rows:
        row["alias"] = aliases[row["repository"]]
    summary = {"no_head": no_head, "no_passes": unreviewed, "invalid_bodies": invalid_bodies(db, C1_FROM, E_TO),
               "paper_heads": len(paper),
               "paper_in_population": sum(1 for r in rows if r["paper"])}
    for name in ("C1", "E"):
        part = [r for r in rows if r["population"] == name]
        dropped: dict[str, int] = {}
        for r in part:
            if r["dropped"]:
                dropped[r["dropped"]] = dropped.get(r["dropped"], 0) + 1
        summary[name] = {
            "heads": len(part), "with_state": sum(1 for r in part if r["state"]),
            "from_checkpoint": sum(1 for r in part if r["source"] == "checkpoint"),
            "rebuilt_from_git": sum(1 for r in part if r["source"] == "rebuilt-from-git"),
            "dropped": dropped, "e0": sum(1 for r in part if r["e0"]),
            "git_check": {word: sum(1 for r in part if r["git_check"] == word)
                          for word in ("match", "mismatch", "unverified")}}
    return rows, {"summary": summary, "aliases": aliases}


def cmd_population(args, out, env) -> int:
    db = args.db or default_db(env)
    target = args.out / "population.jsonl"
    if target.exists():
        raise Refused(f"{target} exists; the replay reads one population, by its hash")
    pack = args.pack or Path(env.get("HOME", "~")).expanduser() / PACK_HOME
    rows, found = population(db, route=pinned_router(pack))
    args.out.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    target.write_text(text, encoding="utf-8")
    (args.out / "aliases.json").write_text(
        json.dumps(found["aliases"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = dict(found["summary"], pack=PACK_SHA,
                   sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    (args.out / "population-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    json.dump(summary, out, indent=2, sort_keys=True)
    out.write("\n")
    return 0


# --- step 4: objective labels ------------------------------------------------

#: The model arms whose answer files seal a population (implement.md, step 7).
MODEL_ARMS = ("kev", "jev", "haiku")

#: The work item whose notes carry each seal.
ITEM = 2764


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sealed(db, folder: Path, home: str | None) -> list[str]:
    """What keeps C1 unsealed; empty when it is sealed. Each model arm's
    primary replay finished, unhalted, on the `population.jsonl` beside it and
    on the scope that file gives it now; its answer file holds one line for
    each C1 head in that scope and none for a head outside it; and a note on
    sd:2764 carries that file's SHA-256. Every line must be a final record of
    that arm's run (`record_faults`): an identity alone is not an answer.
    The faithfulness gate must pass again on the registration and result
    the manifest names, and Jev's reused lines must be step 1's answers
    (`reuse_faults`)."""
    with contextlib.closing(open_ro(db)) as connection:
        notes = [body for (body,) in connection.execute(
            "SELECT body FROM note WHERE item = ?", (ITEM,))]
    population, manifest_path = folder / "population.jsonl", folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    if manifest.get("population", {}).get("sha256") != file_sha256(population):
        return [f"no replay manifest names {population.name} as it is now"]
    # The gate again, on what the manifest says the replay ran under: the
    # registered text, bound by the operator's note to step 1's result.
    bound = manifest.get("registration") or {}
    try:
        registered(REGISTRATION)
        if (bound.get("commit"), bound.get("sha256")) != (REGISTRATION_COMMIT, REGISTERED_SHA256) \
                or not bound.get("document"):
            raise Refused("the manifest names no registration")
        faithful, reuse = faithful_gate(Path(manifest.get("faithful_folder") or "."), db, bound,
                                        manifest["population"]["sha256"])
        rows = read_jsonl(population)
        for row in rows:
            step_one(reuse, row)
    except Refused as why:
        return [f"the faithfulness gate does not pass: {why}"]
    if manifest.get("faithful") != faithful:
        return ["the faithfulness result is not the one the replay ran on"]
    missing = []
    for arm in MODEL_ARMS:
        path = folder / "replay" / f"{arm}.jsonl"
        run = manifest.get("arms", {}).get(arm, {})
        heads = scope(arm, rows, db, home)
        wanted = {(r["repository"], r["head"]) for r in heads}
        c1 = {(r["repository"], r["head"]) for r in heads if r["population"] == "C1"}
        if not path.is_file() or not run.get("finished") or run.get("halted"):
            missing.append(f"{arm}'s replay has not finished")
            continue
        if run.get("scope") != scope_of(heads):
            missing.append(f"{arm}'s scope moved since its replay")
            continue
        written = read_jsonl(path)
        faults = record_faults(written, arm, run.get("run_id")) or reuse_faults(
            written, reuse if arm == "jev" else {}, heads)
        lines = collections.Counter((r.get("repository"), r.get("head")) for r in written)
        if faults:
            missing.append(f"{path.name} holds {len(faults)} line(s) that are not final "
                           f"answers, such as {faults[0]}")
        elif any(lines[ident] != 1 for ident in c1) or set(lines) - wanted:
            missing.append(f"{path.name} does not hold one answer for each of its {len(c1)} C1 heads")
        elif not any(file_sha256(path) in body for body in notes):
            missing.append(f"no note on sd:{ITEM} carries the SHA-256 of {path.name}")
    return missing


def blocking_at_head(db, wanted: set) -> dict:
    """For each (repository, head): the first pass at that head with a
    completed review, as (blocking, the blocking findings' severities).
    A finding at another head of the branch is never read for this one."""
    found = {}
    for _id, _key, _stamp, body in checkpoints(db, C1_FROM, "9999"):
        repository = body.get("repository") or ""
        for one in passes(body):
            ident = (repository, one.get("head"))
            if ident not in wanted or ident in found:
                continue
            rep = report(one)
            done = rep.get("completed_reviews")
            if type(done) is not int or done <= 0:
                continue
            findings = rep.get("findings") if isinstance(rep.get("findings"), list) else []
            blocking = [f for f in findings
                        if isinstance(f, dict) and f.get("disposition") == "blocking"]
            found[ident] = (bool(blocking), sorted(str(f.get("severity")) for f in blocking))
    return found


def cmd_labels(args, out, env) -> int:
    if args.kind == "sheet":
        return cmd_sheet(args, out, env)
    db = args.db or default_db(env)
    name = args.population
    if name == "C2":
        raise Refused("C2 is sealed by step 15 after its 14 days end (2026-10-20 09:01 MDT), "
                      "and step 15 is not built; nothing was read")
    if name == "C1":
        # Before any checkpoint is opened: an unsealed C1 outcome stays unread.
        unsealed = sealed(db, args.out, env.get("HOME"))
        if unsealed:
            raise Refused("C1 is not sealed: " + "; ".join(unsealed) + "; nothing was read")
    target = args.out / "labels" / f"{name}.jsonl"
    if target.exists():
        raise Refused(f"{target} exists; labels are written once")
    rows = [r for r in read_jsonl(args.out / "population.jsonl") if r["population"] == name]
    found = blocking_at_head(db, {(r["repository"], r["head"]) for r in rows})
    labels = []
    for row in rows:
        blocking, severities = found.get((row["repository"], row["head"]), (None, []))
        labels.append({"repository": row["repository"], "head": row["head"],
                       "population": name, "paper": row["paper"],
                       "blocking_at_head": blocking, "severities": severities,
                       "source": "review-blocking-at-head",
                       "unknown": None if blocking is not None else "no-completed-review"})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in labels),
                      encoding="utf-8")

    def rate(part):
        known = [r for r in part if r["blocking_at_head"] is not None]
        return {"heads": len(part), "known": len(known),
                "blocking": sum(1 for r in known if r["blocking_at_head"]),
                "rate": round(sum(1 for r in known if r["blocking_at_head"]) / len(known), 4)
                if known else None}
    summary = {"population": name, "all": rate(labels),
               "paper": rate([r for r in labels if r["paper"]]),
               "sha256": file_sha256(target)}
    json.dump(summary, out, indent=2, sort_keys=True)
    out.write("\n")
    return 0


# --- step 5: the replay harness -----------------------------------------------

#: The pre-registration, and the commit that registered it (step 6, #178).
REGISTRATION = FOLDER.parent / "docs/experiments/2026-10-05-jev-entry-2-preregistration.md"
REGISTRATION_COMMIT = "fe5b904d5fa1ee2e95c2ecce62ff113f5ca4c854"
#: The file's length and SHA-256 at that commit. An amendment is appended
#: below the registered text, so the document must begin with these bytes.
REGISTERED_BYTES = 33408
REGISTERED_SHA256 = "c6a9d61eacc2986407de3c670b594fd3ba48a0adb06c3e5454ea915e20aea297"
#: The same through the 2026-10-11 amendment (decision #14288), which sets
#: the hosted arms' scope this replay runs on; a later one goes below it.
AMENDED_BYTES = 42701
AMENDED_SHA256 = "bd94faff6e16c917c8c1ecf53398b1dec5d57b2364a6fe17c7fb780c5927363a"
#: Step 6's check: a blank operator prediction, or a drafted one not confirmed.
UNREGISTERED = re.compile(r"^PREDICTION \(operator[^)]*\):[ \t_]*$|^PREDICTION .*UNCONFIRMED",
                          re.M)

#: The ledger's run id for the replay's rows; step 1's rows carry none.
RUN_ID = "jev-entry-2-replay"
#: The arms that leave the machine, the stopping rule's spend cap over them,
#: and Jev's list price per million input tokens (output is free).
HOSTED = ("jev", "haiku")
SPEND_CAP = 10.0
JEV_USD_IN = 0.042
#: A token is at least one byte, so a payload's UTF-8 length bounds its input
#: tokens; this margin covers the provider's own prompt around it.
TEMPLATE_TOKENS = 8192
#: The stopping rule's Kev outage: this many failed heads in a row pause the
#: arm; it resumes once after `kev.sh status` exits 0, and a second ends it.
KEV_OUTAGE = 10
#: Where the spans go, and the Phoenix project they file under (design.md, "Traces").
TRACES_URL = "http://127.0.0.1:4338/v1/traces"
PROJECT = "jev-experiment-entry-2"
QID = "sd-review-tier"


def registered(path: Path) -> dict:
    """The registration the manifest freezes: the commit, the registered
    text's hash, and the whole document's. Refused unless the document begins
    with the text registered at `REGISTRATION_COMMIT`, which holds every
    required prediction, then the 2026-10-11 amendment the replay's scope
    follows, and while any `PREDICTION` line in it is blank,
    whitespace or `UNCONFIRMED`, an amendment's included."""
    data = path.read_bytes()
    if hashlib.sha256(data[:REGISTERED_BYTES]).hexdigest() != REGISTERED_SHA256:
        raise Refused(f"{path.name} does not begin with the text registered at "
                      f"{REGISTRATION_COMMIT[:12]}; an amendment goes below it, never in it")
    if hashlib.sha256(data[:AMENDED_BYTES]).hexdigest() != AMENDED_SHA256:
        raise Refused(f"{path.name} does not carry the 2026-10-11 amendment as written, which "
                      "sets the hosted arms' scope; a later amendment goes below it")
    open_lines = UNREGISTERED.findall(data.decode("utf-8"))
    if open_lines:
        raise Refused(f"{len(open_lines)} PREDICTION line(s) in {path.name} are blank or "
                      "UNCONFIRMED; the replay starts after the registration (step 6)")
    return {"commit": REGISTRATION_COMMIT, "sha256": REGISTERED_SHA256,
            "document": hashlib.sha256(data).hexdigest()}


def unit(value) -> bool:
    """A number from 0 to 1, as a probability or a confidence is."""
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def faithful_fault(one, source: dict | None) -> str | None:
    """Why a step 1 record is not one `ask_jev` result about its registered
    faithfulness head, else None. Every field the replay reuses is read:
    the state, the answer and its distribution, and the attempts, which end
    in the answer or, with no answer, hold the one retry."""
    if not isinstance(one, dict) or source is None:
        return "a head the faithfulness draw cannot give"
    if one.get("state") != source["state"]:
        return "a state that is not its head's"
    answer, tries = one.get("answer"), one.get("tries")
    if not isinstance(tries, list) or not 1 <= len(tries) <= 2 or not all(
            isinstance(t, dict) and (t.get("exit") is None or type(t.get("exit")) is int)
            and type(t.get("wait_ms")) is int and t["wait_ms"] >= 0 for t in tries):
        return "attempts that are not one or two `ask_jev` attempts"
    if answer is None:
        return None if len(tries) == 2 else "no answer before the one retry"
    if not isinstance(answer, dict) or answer.get("choice") not in TIERS:
        return "an answer that is not a tier"
    if tries[-1]["exit"] != 0:
        return "an answer from an attempt that failed"
    probabilities, confidence = answer.get("probabilities"), answer.get("confidence")
    if not (probabilities is None or isinstance(probabilities, dict)
            and set(probabilities) == set(TIERS) and all(map(unit, probabilities.values()))) \
            or not (confidence is None or unit(confidence)):
        return "a distribution or a confidence that is not numbers from 0 to 1"
    return None


def unanswered(tries: list) -> str:
    """A step 1 head's outcome when it got no answer, as its last attempt
    ended: no exit is a timeout, exit 0 an answer that was not a tier."""
    last = tries[-1]["exit"]
    return "timeout" if last is None else "invalid" if last == 0 else "unavailable"


def reused_line(found: dict) -> dict:
    """The answer line's fields that a step 1 record gives its head: a
    registered failure stays one, counted and not asked again."""
    answer = found["answer"] or {}
    return dict(tier=answer.get("choice"), probabilities=answer.get("probabilities"),
                confidence=answer.get("confidence"),
                outcome="ok" if answer else unanswered(found["tries"]),
                attempts=found["tries"], usd=None)


def step_one(reuse: dict, row: dict) -> dict | None:
    """The one map from a population head to what step 1 recorded for it:
    its answer line's fields, or None when step 1 did not ask it. Step 1
    asked about one state, and a head it asked is never asked again, so a
    population state that differs refuses: asking would grant another
    retry pair. The gate, the replay, a resume and the seal all read step 1
    through here."""
    found = reuse.get((row["repository"], row["head"]))
    if found is None:
        return None
    if found["state"] != row.get("state"):
        raise Refused(f"step 1 asked about head {row['head'][:12]} of {row['repository']} in "
                      "another state than the population gives it, and a head step 1 asked "
                      "is never asked again; nothing was asked")
    return reused_line(found)


def reuse_faults(lines: list, reuse: dict, rows: list[dict]) -> list[str]:
    """Why answer lines disagree with step 1's records (`step_one`), one
    reason each: a head step 1 asked must carry that record's answer,
    reused; no other line may claim reuse."""
    by_head = {(r["repository"], r["head"]): r for r in rows}
    faults = []
    for line in lines:
        row = by_head.get((line.get("repository"), line.get("head")))
        try:
            want = step_one(reuse, row) if row else None
        except Refused:
            faults.append("a line for a head step 1 asked about another state")
            continue
        if want is not None:
            if not line.get("reused") or any(line.get(k) != v for k, v in want.items()):
                faults.append("a line that is not step 1's answer for its head")
        elif line.get("reused"):
            faults.append("a reused line with no step 1 answer for its head")
    return faults


def faithful_gate(folder: Path, db, registration: dict, population_sha: str) -> tuple[dict, dict]:
    """Step 1's passing result, bound to this replay: its answers' hash, and
    every record by head for Jev to reuse, a no-answer included. Nothing it
    stores is taken as proof: it must hold exactly FAITHFUL_COUNT records,
    each a `faithful_fault`-free answer about a distinct registered
    faithfulness head (an E head with a recorded reading) with that head's
    rebuilt state. The draw must be the registered stratification, read from
    the checkpoints: half from moved heads, or every moved head of the public
    pool when it holds fewer. The answers whose choice equals the recorded
    tier, recounted here, must reach FAITHFUL_PASS. A note on sd:2764 must
    carry its hash with this registration's and population's."""
    answers = folder / "answers.jsonl"
    if not answers.is_file():
        raise Refused(f"no faithfulness result in {folder}; the full replay starts after "
                      "step 1 passes; nothing was asked")
    try:
        lines = read_jsonl(answers)
    except ValueError:
        raise Refused(f"the faithfulness result in {folder} holds a line that is not JSON; "
                      "nothing was asked") from None
    found = {(c["repository"], c["head"]): c for c in candidates(db)}
    seen, matched = set(), 0
    for one in lines:
        ident = (one.get("repository"), one.get("head")) if isinstance(one, dict) else None
        if ident and not all(isinstance(part, str) for part in ident):
            ident = None
        fault = "a head twice" if ident in seen else faithful_fault(one, found.get(ident))
        if fault:
            raise Refused(f"the faithfulness result in {folder} holds a record that is not one "
                          f"answer about a registered faithfulness head ({fault}); "
                          "nothing was asked")
        seen.add(ident)
        matched += one["answer"] is not None and one["answer"]["choice"] == found[ident]["recorded"]
    if len(lines) != FAITHFUL_COUNT or matched < FAITHFUL_PASS:
        raise Refused(f"the faithfulness result in {folder} did not pass: {matched} of "
                      f"{len(lines)} answers match, {FAITHFUL_PASS} of {FAITHFUL_COUNT} needed; "
                      "nothing was asked")
    moved, half = {ident for ident in seen if found[ident]["moved"]}, FAITHFUL_COUNT // 2
    # Step 1 draws from public managed repositories only. Which paths they
    # track moves with every merge, so the seeded sample is not drawn again:
    # the repositories and the moved cohort are what the gate can re-derive.
    failed: set = set()
    repos = public_repos(db, failed)
    unlisted = sorted({repository.lower() for repository, _head in seen} - set(repos))
    if failed or unlisted:
        raise Refused(f"the faithfulness result in {folder} cannot be checked against the "
                      f"public pool: git could not list {len(failed)} public checkout(s), and "
                      f"{len(unlisted)} drawn repositories are not public managed ones; "
                      "nothing was asked")
    left = set()
    if len(moved) < half:
        pool, _halted = public_pool(list(found.values()), repos, None, frozenset())
        left = {(c["repository"], c["head"]) for c in pool if c["moved"]} - moved
    if len(moved) > half or left:
        raise Refused(f"the faithfulness result in {folder} is not the registered draw: "
                      f"{len(moved)} of its heads moved, and the draw takes {half}, or every "
                      "moved head of the public pool when it holds fewer; nothing was asked")
    bound = {"answers": file_sha256(answers)}
    wanted = (bound["answers"], registration["document"], population_sha)
    with contextlib.closing(open_ro(db)) as connection:
        notes = [body for (body,) in connection.execute(
            "SELECT body FROM note WHERE item = ?", (ITEM,))]
    if not any(all(value in body for value in wanted) for body in notes):
        raise Refused(f"no note on sd:{ITEM} binds the faithfulness result to this registration "
                      f"and population; after review, note: faithful {wanted[0]} "
                      f"registration {wanted[1]} population {wanted[2]}; nothing was asked")
    return bound, {(one["repository"], one["head"]): one for one in lines}


def scope(arm: str, rows: list[dict], db, home: str | None) -> list[dict]:
    """The heads an arm is asked about: every head with a state for Kev; for a
    hosted arm, only a state whose every path a public managed repository
    tracks (decision #14288 on sd:2764). No other state leaves the machine."""
    rows = [r for r in rows if r.get("state")]
    if arm not in HOSTED:
        return rows
    public = public_paths(db)
    return [r for r in rows if private_path(r["state"], home, public) is None]


def scope_of(rows: list[dict]) -> dict:
    """What fixes a scope across a resume and a seal: its heads, hashed."""
    heads = sorted([r["repository"], r["head"]] for r in rows)
    return {"heads": len(heads), "sha256": hashlib.sha256(json.dumps(heads).encode()).hexdigest()}


def reserve(arm: str, payload: dict, definition: dict, env) -> float:
    """The most one attempt can cost, held against the cap before it is made;
    0 for Kev, which runs here."""
    if arm not in HOSTED:
        return 0.0
    import jev_compare
    tokens = len(json.dumps(payload).encode("utf-8")) + TEMPLATE_TOKENS
    if arm == "jev":
        return tokens * JEV_USD_IN / 1e6
    usd_in = jev_compare.price(env, "JEV_COMPARE_HAIKU_USD_IN", jev_compare.DEFAULT_USD_IN)
    usd_out = jev_compare.price(env, "JEV_COMPARE_HAIKU_USD_OUT", jev_compare.DEFAULT_USD_OUT)
    return (tokens * usd_in + jev_compare.output_cap(definition) * usd_out) / 1e6


def dollars(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite amount of 0 or more")
    return value


def replay_env(env, ledger: Path, traces: str) -> dict:
    """The arms' environment: `<config>/jev/.env` and the Kev service's
    settings as defaults below the caller's, as `jev.sh` reads them, then the
    experiment ledger, no comparison child, no shadow, and the run id."""
    import jev  # noqa: F401 - puts lib/ on the path
    import system_tools_config
    child = dict(system_tools_config.read_env("jev", env))
    child.update(env)
    kev = system_tools_config.read_env("kev", env)
    for name in ("KEV_API_KEY", "KEV_MODEL"):
        if name not in child and kev.get(name, "") not in ("", "change-me", "changeme"):
            child[name] = kev[name]
    if not child.get("JEV_COMPARE_KEV_URL") and kev.get("KEV_PORT"):
        child["JEV_COMPARE_KEV_URL"] = f"http://127.0.0.1:{kev['KEV_PORT']}/v1/systemone"
    child.update({"JEV_METER": "1", "JEV_METER_DB": str(ledger), "JEV_COMPARE_STAGES": "",
                  "JEV_SHADOW": "off", "JEV_ENABLED": "on", "JEV_TRACES_URL": traces})
    return child


def ask_once(arm: str, payload: dict, definition: dict, conf: dict, env) -> dict:
    """One call to one arm, as the ledger row it makes: never raises."""
    import jev
    import jev_compare
    event: dict = {}
    try:
        if arm == "jev":
            event.update(provider=jev.PROVIDER, model=None)
            started = time.monotonic()
            try:
                response = jev.post(conf, payload)
            finally:
                event["duration_ms"] = int(round((time.monotonic() - started) * 1000))
            event.update(jev.usage_of(response), model=response.get("model"))
            # No count is an unknown cost, never a free call.
            event["usd"] = (None if event.get("tokens_in") is None
                            else event["tokens_in"] * JEV_USD_IN / 1e6)
            answer = jev.answer_of(response, QID)
            event.update(answer=jev.position_of(answer.get("choice"), definition["criteria"]),
                         confidence=jev.confidence_of(answer),
                         probabilities=jev.distribution_of(answer, definition))
        else:
            job = {"payload": payload}
            got = (jev_compare.kev_arm(job, env, LOCAL_ONLY) if arm == "kev"
                   else jev_compare.haiku_arm(job, env, "anthropic"))
            event.update({k: v for k, v in got.items() if not k.startswith("_")})
        if event.get("answer") is None:
            raise jev_compare.Declined("invalid", "invalid", "no tier in the answer")
        event.update(outcome="ok", cause=None)
    except jev_compare.Declined as exc:
        got = getattr(exc, "event", None) or {}
        event.update({k: v for k, v in got.items() if not k.startswith("_")})
        event.update(exc.error, outcome=exc.outcome, cause=exc.cause)
    except Exception as exc:                 # a failed call is a row, never a traceback
        word = "timeout" if isinstance(exc, TimeoutError) or "timed out" in str(exc) \
            else "unavailable"
        event.update(jev.error_of(exc), outcome=word, cause=word)
    event.setdefault("provider", arm)
    return event


def ask_arm(arm, payload, definition, conf, env, check=lambda tried, more: None):
    """The stopping rule's one retry after 30 s. `check` is asked after every
    attempt, before any retry, with whether a retry would follow. Returns the
    final attempt's row, every attempt's outcome, wait, cost and counts, and
    the halt `check` gave, if any."""
    attempts = []
    for attempt in range(2):
        if attempt:
            time.sleep(RETRY_WAIT)
        event = ask_once(arm, payload, definition, conf, env)
        attempts.append({"outcome": event["outcome"], "wait_ms": event.get("duration_ms"),
                         "usd": event.get("usd"), "tokens_in": event.get("tokens_in"),
                         "tokens_out": event.get("tokens_out")})
        more = event["outcome"] != "ok" and attempt == 0
        stop = check(attempts, more)
        if stop or not more:
            return event, attempts, stop
    return event, attempts, None


#: Attempt outcomes where a response came back; one with no cost is an
#: unknown cost. A call that got no response is charged its whole reservation.
RESPONDED = ("ok", "invalid")


def charge(attempt: dict, bound: float) -> float:
    """What an attempt counts against the cap: its cost, else its reservation."""
    return bound if attempt["usd"] is None else attempt["usd"]


def summed(attempts: list, name: str) -> int | None:
    counts = [a.get(name) for a in attempts if a.get(name) is not None]
    return sum(counts) if counts else None


def stopping(arm: str, entry: dict, *, total=0.0, cap=math.inf, bound=0.0, attempts=(),
             need=0.0, ledger=None) -> str | None:
    """Why the run must stop, else None: the stopping rule's halts in one
    place. Asked before every call, after every attempt and for every
    answer, recovered ones included, before its marker goes: a halt already
    saved, a ledger that did not take the row, an attempt that answered with
    no cost or cost more than its reservation, and spend plus the next call's
    reservation (`need`) above the cap."""
    if entry.get("halted"):
        return entry["halted"]
    if ledger not in (None, "written"):
        return f"the ledger did not take a row ({ledger}); its answer waits in the marker"
    if arm not in HOSTED:
        return None
    for one in attempts:
        if one["usd"] is None and one["outcome"] in RESPONDED:
            return f"an attempt answered with no cost; it is charged its reservation, ${bound:.4f}"
        if one["usd"] is not None and one["usd"] > bound:
            return f"an attempt cost ${one['usd']:.4f}, above its reservation of ${bound:.4f}"
    so_far = total + sum(charge(one, bound) for one in attempts)
    if so_far + need > cap:
        return (f"spend cap: ${so_far:.4f} spent, and the next call reserves ${need:.4f} "
                f"of ${cap:.2f}")
    return None


#: A head's final outcome when it got no answer; each is counted, not asked again.
NO_ANSWER = ("timeout", "unavailable", "invalid", "fallback", "interrupted", "ended")
#: The fields every answer line carries.
LINE_FIELDS = ("repository", "head", "arm", "run_id", "outcome", "tier", "usd", "at")


def record_faults(lines: list, arm: str, run_id: str) -> list[str]:
    """Why lines are not this run's final records, one reason each: another
    arm or run, a missing field, an outcome that is neither an answer with a
    tier nor an explicit no-answer, a reused answer on an arm step 1 did not
    ask, a cost that is not a number (a reused answer excepted), or a head
    twice."""
    faults, seen = [], set()
    for line in lines:
        missing = [k for k in LINE_FIELDS if k not in line]
        usd = line.get("usd")
        if missing:
            faults.append(f"a line without {', '.join(missing)}")
        elif (line["arm"], line["run_id"]) != (arm, run_id):
            faults.append(f"a line of {line['arm']} run {line['run_id']}")
        elif not ((line["outcome"] == "ok" and line["tier"] in TIERS)
                  or (line["outcome"] in NO_ANSWER and line["tier"] is None)):
            faults.append(f"outcome {line['outcome']!r} with tier {line['tier']!r}")
        elif line.get("reused") and arm != "jev":
            faults.append(f"a reused answer on the {arm} arm; only Jev was asked in step 1")
        elif not (usd is None and line.get("reused")) and (
                isinstance(usd, bool) or not isinstance(usd, (int, float))
                or not math.isfinite(usd) or usd < 0):
            faults.append(f"a line with cost {usd!r}")
        elif (line["repository"], line["head"]) in seen:
            faults.append("a head answered twice")
        seen.add((line.get("repository"), line.get("head")))
    # `ended` is earned, not claimed: only Kev's heads after two outages the
    # lines themselves show, recounted as the run counts them.
    ended = [n for n, line in enumerate(lines) if line.get("outcome") == "ended"]
    if ended:
        streak = outages = 0
        for line in lines[:ended[0]]:
            streak = 0 if line.get("outcome") == "ok" else streak + 1
            if streak >= KEV_OUTAGE:
                outages, streak = outages + 1, 0
        if arm != "kev" or outages < 2 or ended != list(range(ended[0], len(lines))):
            faults.append("an ended line without two Kev outages before it")
    return faults


def write_json(path: Path, value) -> None:
    """Whole or not at all: a reader never sees half a manifest."""
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(part, path)


def answer_file(arm: str, rerun: str | None, manifest: dict) -> tuple[str, str]:
    """(manifest key, run id) of the run to write: the primary, or the last
    unfinished rerun, or a new numbered one. Refused when the primary has
    finished and no rerun was asked for."""
    runs = manifest.get("arms", {})
    if rerun is None:
        if runs.get(arm, {}).get("finished"):
            raise Refused(f"{arm}'s replay finished; a second run needs --rerun REASON "
                          "and is reported as a second run")
        return arm, RUN_ID
    number = 2
    while f"{arm}.run{number}" in runs:
        if not runs[f"{arm}.run{number}"].get("finished"):
            return f"{arm}.run{number}", f"{RUN_ID}-{number}"
        number += 1
    return f"{arm}.run{number}", f"{RUN_ID}-{number}"


def spent(replay: Path, apart: Path | None = None) -> float:
    """Hosted spend so far, over every run's answers, plus each call still
    waiting in a marker other than `apart`: its reservation, or its held
    answer's cost when that is higher. A marker without a reservation counts
    as unbounded."""
    total = 0.0
    for path in replay.glob("*.jsonl"):
        if path.name.split(".")[0] in HOSTED:
            total += sum(row.get("usd") or 0.0 for row in read_jsonl(path))
    for path in replay.glob("*.asking"):
        if path.name.split(".")[0] in HOSTED and path != apart:
            held = json.loads(path.read_text(encoding="utf-8"))
            total += max(held.get("reserved", math.inf), held.get("line", {}).get("usd") or 0.0)
    return total


#: What tells this head's ledger row from another folder's under the same
#: run id, pair and arm: primary run ids repeat when folders share a ledger.
MATCHED = ("outcome", "cause", "answer", "usd", "tokens_in", "tokens_out", "duration_ms",
           "question_id", "prompt_hash")
#: `recorded` when the ledger holds another row under this run id, pair and arm.
DIFFERS = "another row under this run id, pair and arm"


def facts(row: dict) -> tuple:
    return tuple(None if row.get(k) is None else round(float(row[k]), 9) if k == "usd"
                 else str(row[k]) for k in MATCHED)


def recorded(ledger: Path, row: dict) -> str:
    """"written" when the ledger holds this head's row, "absent" when it holds
    none, DIFFERS when its rows under this run id, pair and arm are other
    answers, and "unreadable" when it cannot be read."""
    try:
        with contextlib.closing(open_ro(ledger)) as connection:
            found = connection.execute(
                f"SELECT {', '.join(MATCHED)} FROM judgment WHERE run_id = ? AND pair = ?"
                " AND arm = ?", (row["run_id"], row["pair"], row["arm"])).fetchall()
    except Exception:                        # unknown, never "not there"
        return "unreadable"
    if not found:
        return "absent"
    return "written" if facts(row) in {facts(dict(zip(MATCHED, r))) for r in found} else DIFFERS


def settle(ledger: Path, row: dict, env) -> str:
    """Write an answered head's ledger row once, with one retry: "written"
    once it is there, else the reason. The model is never asked again here,
    a row already written is not written twice, and another answer under the
    same run id, pair and arm is not taken for this one."""
    said = "unreadable"
    for _attempt in range(2):
        found = recorded(ledger, row)
        if found != "absent":
            return found
        said, _id = jev_meter.write(row, env)
        if said == "written":
            return said
    return said


class Unredirected(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: the Kev arm's request goes to its own address only."""

    def redirect_request(self, *args):
        return None


#: Kev's opener in the replay: no proxy, no redirect, so the request reaches
#: the address `on_this_machine` checked and nothing else.
LOCAL_ONLY = urllib.request.build_opener(urllib.request.ProxyHandler({}), Unredirected)


def on_this_machine(url: str) -> str | None:
    """Why `url` may not be on this machine, else None: Kev is asked about
    every state, private ones included, so every address its host resolves to
    must be loopback. A name that does not resolve is not loopback."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"{url} is not an http address"
    try:
        found = socket.getaddrinfo(parts.hostname, parts.port or 80, proto=socket.IPPROTO_TCP)
        addresses = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in found}
    except (OSError, ValueError) as exc:
        return f"{parts.hostname} does not resolve here ({exc})"
    if not addresses or not all(a.is_loopback for a in addresses):
        return f"{parts.hostname} resolves to an address off this machine"
    return None


def kev_up() -> bool:
    """`kev.sh status` exits 0. A probe that fails or hangs is not up."""
    try:
        return subprocess.run(["sh", str(FOLDER.parent / "local-kev" / "kev.sh"), "status"],
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def git_head(root: Path) -> str | None:
    found = git(str(root), "rev-parse", "HEAD")
    return found.decode().strip() if found else None


def cmd_replay(args, out, env) -> int:
    import jev
    import jev_compare
    import jev_trace
    arm = args.arm
    # Every refusal comes before the first call, and before any file is written.
    registration = registered(args.preregistration)
    frozen_heuristic()
    ledger, db = args.ledger, args.db or default_db(env)
    isolated(ledger, db, env)
    env = replay_env(env, ledger, args.traces)
    word = jev_meter.ready(arm, env)
    if word != "ready":
        raise Refused(f"the experiment ledger {ledger} refused the {arm} arm: {word}")
    conf = None
    if arm == "jev":
        conf = jev.settings(env)
        _why, reason = jev.why_unusable(conf, env)
        if reason:
            raise Refused(f"Jev cannot answer here: {reason}")
        # The stopping rule's one retry is the only one.
        conf["retries"] = 0
    elif arm == "haiku":
        key = jev_compare.haiku_conf("anthropic", env)["key"]
        if not key or key.lower() in jev.PLACEHOLDER_KEYS:
            raise Refused("no JEV_COMPARE_ANTHROPIC_KEY; the Haiku arm runs on the anthropic "
                          "transport only")
    else:
        fault = on_this_machine((env.get("JEV_COMPARE_KEV_URL") or jev_compare.DEFAULT_KEV_URL)
                                .strip())
        if fault:
            raise Refused(f"Kev is asked about private states, so it must run here: {fault}; "
                          "nothing was sent")
    population_text = args.population.read_bytes()
    population_sha = hashlib.sha256(population_text).hexdigest()
    everyone = read_jsonl(args.population)
    rows = scope(arm, everyone, db, env.get("HOME"))
    folder = args.out
    replay = folder / "replay"
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    if manifest.get("population", {}).get("sha256", population_sha) != population_sha:
        raise Refused(f"{args.population} is not the population this replay started on")
    if manifest.get("registration", registration) != registration:
        raise Refused(f"{args.preregistration.name} changed since this replay started; "
                      "every arm and resume runs under one registration")
    faithful, reuse = faithful_gate(args.faithful, db, registration, population_sha)
    if manifest.get("faithful", faithful) != faithful:
        raise Refused(f"the faithfulness result in {args.faithful} is not the one this replay "
                      "started on; nothing was asked")
    name, run_id = answer_file(arm, args.rerun, manifest)
    if name not in manifest.get("arms", {}) and any(
            (folder / "replay" / f"{name}{suffix}").exists() for suffix in (".jsonl", ".asking")):
        raise Refused(f"replay/{name}.jsonl or its marker exists, and no manifest names it; "
                      "its registration, population and scope are unknown")
    paused = manifest.get("arms", {}).get(name, {})
    # A halt holds until the operator resumes it by name: no run clears one alone.
    if paused.get("halted") and not args.resume:
        raise Refused(f"{name} halted: {paused['halted']}; after review, run again with "
                      "--resume REASON; nothing was asked")
    if arm == "kev" and paused.get("outages") == 1 and not paused.get("resumed") and not kev_up():
        raise Refused(f"{name} is paused after a Kev outage; it resumes once "
                      "`kev.sh status` exits 0; nothing was asked")
    if paused.get("scope", scope_of(rows)) != scope_of(rows):
        raise Refused(f"{name}'s heads differ from those it started on: the public pool moved; "
                      "nothing was sent")
    # Only Jev was asked in step 1; its answers there are reused, never asked again.
    # Every arm, before any call: the population and step 1 agree on each
    # head step 1 asked, a head with no population state included.
    for row in everyone:
        step_one(reuse, row)
    reuse = reuse if arm == "jev" else {}
    replay.mkdir(parents=True, exist_ok=True)
    answers = replay / f"{name}.jsonl"
    marker = replay / f"{name}.asking"
    if answers.exists():
        # A line cut off by a stop belongs to the head the marker names. Only
        # that suffix goes, in place: a failure here never loses a whole line.
        with answers.open("r+b") as fh:
            data = fh.read()
            if data and not data.endswith(b"\n"):
                fh.truncate(data.rfind(b"\n") + 1)
                fh.flush()
                os.fsync(fh.fileno())
    kept = read_jsonl(answers) if answers.exists() else []
    faults = record_faults(kept, arm, run_id) or reuse_faults(kept, reuse, rows)
    if faults:
        raise Refused(f"{answers.name} holds lines that are not final answers of this run: "
                      f"{faults[0]}; nothing was asked")
    done = {(r["repository"], r["head"]) for r in kept}
    cut = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None
    if cut is not None and not {"reserved", "ledger_row"} <= cut.keys():
        raise Refused(f"{marker} predates cost reservations and ledger rows; its call's cost "
                      "is unknown, so settle it by hand before a resume")
    entry = manifest.setdefault("arms", {}).setdefault(name, {})
    entry.update(file=f"replay/{answers.name}", run_id=run_id, reason=args.rerun,
                 scope=scope_of(rows), started=entry.get("started") or now())
    if args.resume:
        entry.setdefault("resumes", []).append(
            {"at": now(), "reason": args.resume, "halted": entry.get("halted")})
        entry["halted"] = None
        if entry.get("outages") == 1:
            entry["resumed"] = now()
    # The ledger's pairs name this folder's experiment, so folders that
    # share a ledger and a run id never take each other's rows.
    manifest.setdefault("instance", secrets.token_hex(8))
    manifest.update(
        registration=registration, faithful=faithful,
        faithful_folder=str(args.faithful.resolve()),
        heuristic_sha256=HEURISTIC_SHA256, pack=PACK_SHA, system=git_head(FOLDER.parent),
        population={"file": args.population.name, "sha256": population_sha})
    write_json(manifest_path, manifest)

    def halt(why):
        entry.update(halted=why)
        write_json(manifest_path, manifest)
        sys.stderr.write(f"jev_experiment: {why}; the run stops here\n")
        return 1

    # Kev's failure streak, rebuilt from the answer file: a stop does not
    # reset it. The head that completed an outage starts a new streak.
    outaged = {tuple(key) for key in entry.get("outage_heads", [])}
    failures = 0
    for one in kept:
        failures = 0 if one["outcome"] == "ok" or (one["repository"], one["head"]) in outaged \
            else failures + 1

    def record(line, stop):
        """The head's line goes in and its marker goes; a halt, and Kev's
        outage, are saved first. A head counts toward one outage once."""
        nonlocal failures
        failures = 0 if line["outcome"] == "ok" else failures + 1
        moved = arm == "kev" and failures >= KEV_OUTAGE
        if moved:
            failures = 0
            key = [line["repository"], line["head"]]
            if key not in entry.setdefault("outage_heads", []):
                entry["outage_heads"].append(key)
            entry["outages"] = len(entry["outage_heads"])
            if entry["outages"] == 1 and not entry.get("resumed"):
                stop = stop or (f"Kev outage: {KEV_OUTAGE} heads failed in a row; the arm "
                                "resumes once `kev.sh status` exits 0")
            elif entry["outages"] > 1:
                # A second outage ends the arm: the heads left get `ended` lines.
                entry["ended"] = "a second Kev outage"
        code = halt(stop) if stop else 0
        if moved and not stop:
            write_json(manifest_path, manifest)
        with answers.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
        marker.unlink(missing_ok=True)
        done.add((line["repository"], line["head"]))
        return code

    # Spend before the cut head, whose own marker is its charge.
    total = spent(replay, apart=marker)
    if cut is not None and (cut["repository"], cut["head"]) in done:
        marker.unlink()
    elif cut is not None:
        # A stop before the head's line: its ledger row, then the stopping
        # rule, then its line; the model is not asked again. Stopped between
        # a call and its answer, the call may have been made, so the head
        # keeps its reservation and the marker's interrupted row.
        if "line" in cut:
            line, bound, attempts = cut["line"], cut["reserved"] / 2, cut["line"]["attempts"]
        else:
            row = next((r for r in rows if (r["repository"], r["head"]) == (cut["repository"],
                                                                            cut["head"])), {})
            line = dict(repository=cut["repository"], head=cut["head"], alias=row.get("alias"),
                        population=row.get("population"), arm=arm, run_id=run_id, tier=None,
                        outcome="interrupted", usd=cut["reserved"], reserved=cut["reserved"],
                        at=now())
            bound, attempts = cut["reserved"], [{"outcome": "interrupted", "usd": cut["reserved"]}]
        said = settle(ledger, cut["ledger_row"], env)
        stop = stopping(arm, entry, total=total, cap=args.cap, bound=bound, attempts=attempts,
                        ledger=said)
        if said != "written":
            return halt(stop)
        if record(dict(line, ledger=said), stop):
            return 1
    definition = {"type": "choice", "instructions": QUESTION,
                  "criteria": jev.parse_mapping(criteria())}
    questions = {QID: definition}
    prompt_hash = jev.sha256([questions, {QID: list(definition["criteria"])}])[:16]
    patterns = jev.settings(env)["privacy"]
    model = env.get("JEV_MODEL") or jev.DEFAULT_MODEL
    total = spent(replay)
    sent = 0
    for row in rows:
        ident = (row["repository"], row["head"])
        if ident in done:
            continue
        if entry.get("ended"):
            break
        line = {"repository": row["repository"], "head": row["head"], "alias": row["alias"],
                "population": row["population"], "arm": arm, "run_id": run_id}
        fields = step_one(reuse, row)
        if fields is not None:
            line.update(fields, reused=args.faithful.name, at=now())
            record(line, None)
            continue
        payload, taken = jev.redacted_payload(
            jev.build_payload(json.loads(row["state"]), questions, model), patterns)
        # Both attempts are reserved before the first, and the marker keeps the
        # reservation: a stop before the answer line charges it, never $0.
        bound = reserve(arm, payload, definition, env)
        stop = stopping(arm, entry, total=total, cap=args.cap, need=2 * bound)
        if stop:
            return halt(stop)
        owner_repo = row["repository"].lower().replace("/", ".")
        pair = hashlib.sha256(f"{manifest['instance']}:{run_id}:{row['repository']}:"
                              f"{row['head']}".encode()).hexdigest()[:16]
        held = dict(caller="jev-experiment", stage="JEV_SD_REVIEW", arm=arm, pair=pair,
                    question_id=jev.named(f"{QID}:{owner_repo}:{row['head'][:12]}"),
                    primitive="choice", questions=1, prompt_hash=prompt_hash, run_id=run_id,
                    changed="no")
        # Until the answer replaces it, the marker holds the row a stop
        # leaves: the call may have been made, so it is charged its reservation.
        provider = {"jev": jev.PROVIDER, "kev": jev.BASELINE_PROVIDER}.get(arm, "anthropic")
        asking = {"repository": row["repository"], "head": row["head"], "reserved": 2 * bound,
                  "ledger_row": dict(held, provider=provider, outcome="unavailable",
                                     cause="unavailable", error_class="Interrupted",
                                     usd=2 * bound)}
        write_json(marker, asking)
        event, attempts, stop = ask_arm(arm, payload, definition, conf, env, check=lambda tried,
                                        more: stopping(arm, entry, total=total, cap=args.cap,
                                                       bound=bound, attempts=tried,
                                                       need=bound if more else 0.0))
        usd = sum(charge(a, bound) for a in attempts)
        # The final attempt's answer, with the cost and counts of every attempt.
        ledger_row = dict(event, **held, load_avg=jev.load_avg(), usd=usd,
                          tokens_in=summed(attempts, "tokens_in"),
                          tokens_out=summed(attempts, "tokens_out"))
        tier = TIERS[int(event["answer"]) - 1] if event.get("answer") else None
        span = {k: ledger_row.get(k) for k in ("caller", "stage", "arm", "pair", "primitive",
                                               "provider", "model", "outcome", "cause", "answer",
                                               "confidence", "tokens_in", "tokens_out",
                                               "duration_ms")}
        values = (event.get("probabilities") or "").split(",")
        line.update(tier=tier, confidence=event.get("confidence"),
                    probabilities=dict(zip(TIERS, map(float, values))) if len(values) == 4 else None,
                    outcome=event["outcome"], cause=event.get("cause"),
                    provider=event.get("provider"), model=event.get("model"),
                    tokens_in=event.get("tokens_in"), tokens_out=event.get("tokens_out"),
                    server_ms=event.get("server_ms"), usd=usd, attempts=attempts,
                    redacted=taken, reused=None, at=now())
        # The answer waits in the marker until the ledger takes its row; a
        # ledger that will not halts the run, and a resume records it with no call.
        write_json(marker, dict(asking, ledger_row=ledger_row, line=line))
        sent += jev_trace.export(span, env, project=PROJECT, extra=(
            ("jev.repo_alias", row["alias"]), ("jev.tier", tier), ("jev.usd", usd),
            ("jev.population", row["population"]))) == jev_trace.SENT
        said = settle(ledger, ledger_row, env)
        stop = stopping(arm, entry, bound=bound, attempts=attempts, ledger=said) or stop
        if said != "written":
            return halt(stop)
        total += usd
        if record(dict(line, ledger=said), stop):
            return 1
    if entry.get("ended"):
        # Each head left is counted, not asked; a stop here resumes here.
        with answers.open("a", encoding="utf-8") as fh:
            for other in rows:
                if (other["repository"], other["head"]) not in done:
                    fh.write(json.dumps({
                        "repository": other["repository"], "head": other["head"],
                        "alias": other["alias"], "population": other["population"],
                        "arm": arm, "run_id": run_id, "tier": None, "outcome": "ended",
                        "cause": entry["ended"], "usd": 0.0, "at": now()},
                        sort_keys=True) + "\n")
    written = read_jsonl(answers) if answers.exists() else []
    entry.update(finished=now(), answers=len(written),
                 no_answer=sum(1 for r in written if r.get("tier") is None),
                 reused=sum(1 for r in written if r.get("reused")),
                 spans_sent=entry.get("spans_sent", 0) + sent, hosted_usd=round(total, 6))
    write_json(manifest_path, manifest)
    json.dump({"arm": name, **entry}, out, indent=2, sort_keys=True)
    out.write("\n")
    return 0


# --- step 8: the blinded label kit ---------------------------------------------

#: The five arms, in the key's column order and the pattern's letter order.
ARMS = ("rules", "heuristic", "jev", "kev", "haiku")
#: Production's cutoff, read on the answer's top probability as the
#: pre-registration's "Production answer" says (operator, on #14765).
UNSURE_BELOW = 0.6
#: The draws' caps, and the arms whose every model skip is drawn; Haiku's are
#: labelled only where they fall in the disagreement draw (H1).
DISAGREEMENT_CAP = 150
SKIP_CAP = 300
SKIP_ARMS = ("jev", "kev")
#: The paths a sheet row shows before `+n more` (design.md, "Sheet").
SHEET_PATHS = 10
SHEET_COLUMNS = ("row", "repo", "commit", "paths", "path_count", "lines_moved",
                 "routing_said", "candidates", "label", "sure", "note")
KEY_COLUMNS = ("row", "repo", "commit", "population", "hosted", *ARMS,
               "jev_raw", "kev_raw", "haiku_raw",
               "jev_confidence", "kev_confidence", "haiku_confidence",
               "pattern", "disagreement_draw", "stratum_heads", "stratum_drawn",
               "disagreement_weight", "jev_skip_draw", "jev_skip_weight",
               "kev_skip_draw", "kev_skip_weight")


def production(line: dict | None, routed: str) -> str | None:
    """An arm's production answer: its tier when its top probability is at
    or above the cutoff; the rules' tier below it, with no distribution, or
    when the call failed; None when the arm was not asked about the head."""
    if line is None:
        return None
    found = line.get("probabilities")
    top = max(found.values()) if isinstance(found, dict) and found \
        and all(map(unit, found.values())) else None
    if line.get("outcome") == "ok" and line.get("tier") in TIERS and top is not None \
            and top >= UNSURE_BELOW:
        return line["tier"]
    return routed


def allocate(sizes: dict, cap: int) -> dict:
    """Rows per stratum: all of them up to `cap`; above it, one per stratum
    first, then each row to the stratum furthest below its share."""
    total = sum(sizes.values())
    if total <= cap:
        return dict(sizes)
    taken = dict.fromkeys(sizes, 1 if len(sizes) <= cap else 0)
    while sum(taken.values()) < cap:
        pick = max((s for s in sorted(sizes) if taken[s] < sizes[s]),
                   key=lambda s: sizes[s] * cap / total - taken[s])
        taken[pick] += 1
    return taken


def label_kit(rows: list[dict], answers: dict, heuristic, seed: int = SEED):
    """The sheet's rows, the key's rows and a summary, from population rows
    and each model arm's answer lines by (repository, head).

    The arms vote with production answers. A model arm with no line for a
    head was not asked about it: a hosted arm outside the public pool
    (decision #14288) has no vote, rather than the rules' tier it never gave,
    and the pattern shows `-` for it. A line without an answer votes the
    rules' tier, as production falls back. So a head is a disagreement when
    the arms asked about it split on review or `skip`, and an arm's skip
    draw holds the model skips of the heads it was asked about.

    The disagreement draw keeps every disagreement up to `DISAGREEMENT_CAP`;
    above it, a seeded draw within each pattern, each row weighted by its
    pattern's heads over its pattern's drawn rows. Each skip draw keeps
    every model skip up to `SKIP_CAP`, else a seeded simple random draw.
    Each draw has its own stream, seeded with `seed`, the population and the
    draw's name, so one draw's size never moves another's rows and two
    populations' draws are independent. The order and each row's candidates
    use a fourth stream, which selects nothing.

    One population per sheet: C2 gets its own draws, as C3 does (operator,
    on #14764), so rows of two populations refuse."""
    if len({r["population"] for r in rows}) > 1:
        raise Refused("a sheet draws over one population; C2 and C3 get sheets of their own")
    population = rows[0]["population"] if rows else ""
    heads, left_out = [], collections.Counter()
    for row in sorted(rows, key=lambda r: (r["population"], r["repository"], r["head"])):
        if not row.get("state"):
            left_out["no-state"] += 1
            continue
        if row.get("routed") not in TIERS:
            left_out["no-routed-tier"] += 1
            continue
        state, routed = json.loads(row["state"]), row["routed"]
        ident = (row["repository"], row["head"])
        lines = {arm: answers.get(arm, {}).get(ident) for arm in MODEL_ARMS}
        votes = {"rules": routed, "heuristic": heuristic(state, routed),
                 **{arm: production(lines[arm], routed) for arm in MODEL_ARMS}}
        heads.append({"row": row, "state": state, "lines": lines, "votes": votes,
                      "pattern": "".join("-" if votes[a] is None else "S" if votes[a] == "skip"
                                         else "R" for a in ARMS)})
    key: dict[tuple, dict] = {}

    def keyed(head):
        return key.setdefault((head["row"]["repository"], head["row"]["head"]), {"head": head})

    strata: dict[str, list] = {}
    for head in heads:
        if "S" in head["pattern"] and "R" in head["pattern"]:
            strata.setdefault(head["pattern"], []).append(head)
    taken = allocate({p: len(m) for p, m in strata.items()}, DISAGREEMENT_CAP)
    rng = random.Random(f"{seed}:{population}:disagreement")
    for pattern in sorted(strata):
        for head in rng.sample(strata[pattern], taken[pattern]):
            keyed(head).update(disagreement_draw=1, stratum_heads=len(strata[pattern]),
                               stratum_drawn=taken[pattern],
                               disagreement_weight=len(strata[pattern]) / taken[pattern])
    skips = {}
    for arm in SKIP_ARMS:
        pool = [h for h in heads if h["votes"][arm] == "skip" and h["votes"]["rules"] != "skip"]
        drawn = random.Random(f"{seed}:{population}:skip:{arm}").sample(
            pool, min(SKIP_CAP, len(pool)))
        for head in drawn:
            keyed(head).update({f"{arm}_skip_draw": 1,
                                f"{arm}_skip_weight": len(pool) / len(drawn)})
        # The sampling rate a pooled H1 look thins every population to the
        # lowest of (design.md, review round 1); none without a model skip.
        skips[arm] = {"pool": len(pool), "drawn": len(drawn),
                      "rate": len(drawn) / len(pool) if pool else None}
    order = random.Random(f"{seed}:order")
    entries = [key[ident] for ident in sorted(key)]
    order.shuffle(entries)
    sheet, keys = [], []
    for number, entry in enumerate(entries, 1):
        head = entry["head"]
        row, state, votes, lines = head["row"], head["state"], head["votes"], head["lines"]
        shown = state["changed_paths"][:SHEET_PATHS]
        more = state["path_count"] - len(shown)
        candidates = sorted({v for v in votes.values() if v is not None})
        order.shuffle(candidates)
        sheet.append({"row": number, "repo": row["alias"], "commit": row["head"],
                      "paths": "; ".join(shown) + (f" +{more} more" if more else ""),
                      "path_count": state["path_count"], "lines_moved": state["lines_moved"],
                      "routing_said": state["deterministic_routing_said"],
                      "candidates": ", ".join(candidates), "label": "", "sure": "", "note": ""})
        keys.append({
            "row": number, "repo": row["alias"], "commit": row["head"],
            "population": row["population"], "hosted": int(lines["jev"] is not None),
            **{arm: votes[arm] or "" for arm in ARMS},
            **{f"{arm}_raw": "" if lines[arm] is None else lines[arm].get("tier")
               or lines[arm].get("outcome") for arm in MODEL_ARMS},
            **{f"{arm}_confidence": "" if lines[arm] is None or lines[arm].get("confidence")
               is None else lines[arm]["confidence"] for arm in MODEL_ARMS},
            "pattern": head["pattern"], "disagreement_draw": entry.get("disagreement_draw", 0),
            "stratum_heads": entry.get("stratum_heads", ""),
            "stratum_drawn": entry.get("stratum_drawn", ""),
            "disagreement_weight": entry.get("disagreement_weight", ""),
            **{f"{arm}_skip{part}": entry.get(f"{arm}_skip{part}", 0 if part == "_draw" else "")
               for arm in SKIP_ARMS for part in ("_draw", "_weight")}})
    summary = {"heads": len(heads), "left_out": dict(left_out), "rows": len(sheet),
               "rows_by_population": dict(collections.Counter(k["population"] for k in keys)),
               "disagreement": {"pool": sum(map(len, strata.values())),
                                "drawn": sum(taken.values()), "strata": len(strata)},
               "skip": skips}
    return [as_cells(r) for r in sheet], [as_cells(k) for k in keys], summary


def thinned(rows: list[dict], arm: str, population: str, common: float, own,
            seed: int = SEED) -> list[dict]:
    """One population's drawn skips of one arm, thinned for a pooled H1
    look (design.md, "Pooled H1"): one generator per arm and population,
    advanced once per row in `(repo, commit)` order, keeps a row when its
    number falls below `common / own`. A row was drawn at `own`, so it is
    kept at `common`, the lowest rate across the looked-at populations,
    whatever its population: the pooled sample has one inclusion chance."""
    if own is None or not 0 < common <= own <= 1:
        raise Refused(f"no thinning from {own!r} to {common!r}: the common rate is the lowest "
                      "rate of the populations pooled, each above 0")
    rng = random.Random(f"{seed}:thin:{arm}:{population}")
    return [row for row in sorted(rows, key=lambda r: (r["repo"], r["commit"]))
            if rng.random() < common / own]


def as_cells(row: dict) -> dict:
    """A row as the CSV holds it, so the files and the tests read one form."""
    return {name: str(value) for name, value in row.items()}


def csv_bytes(columns, rows) -> bytes:
    import csv
    import io
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def sheet_markdown(name: str, sheet: list[dict]) -> bytes:
    """The sheet's reading copy: one section per row, in the sheet's order."""
    out = [f"# Hand-label sheet {name}", "",
           f"Fill `label` (skip, cheap, standard or deep) and `sure` (1 guess, 2 fairly sure,"
           f" 3 sure) in `sheet-{name}.csv`; this copy is for reading.",
           "Read each commit's diff in the local clone. The candidates are the tiers given"
           " for it, in no order.", ""]
    for row in sheet:
        out += [f"## Row {row['row']}", "",
                f"- Repository: {row['repo']}", f"- Commit: `{row['commit']}`",
                f"- Paths ({row['path_count']}, {row['lines_moved']} lines moved): {row['paths']}",
                f"- Routing said: {row['routing_said']}",
                f"- Candidates: {row['candidates']}", ""]
    return "\n".join(out).encode("utf-8")


def cmd_sheet(args, out, env) -> int:
    db = args.db or default_db(env)
    if args.population == "C2":
        raise Refused("C2 is sealed by step 15 after its 14 days end (2026-10-20 09:01 MDT), "
                      "and step 15 is not built; nothing was read")
    # Before any answer is read: the draws come from sealed answer files only.
    unsealed = sealed(db, args.out, env.get("HOME"))
    if unsealed:
        raise Refused("C1 is not sealed: " + "; ".join(unsealed) + "; nothing was read")
    heuristic = frozen_heuristic()
    inputs, answers = {}, {}
    for name in ("population.jsonl", *(f"replay/{arm}.jsonl" for arm in MODEL_ARMS)):
        data = (args.out / name).read_bytes()
        inputs[name] = hashlib.sha256(data).hexdigest()
        lines = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
        if name == "population.jsonl":
            rows = [r for r in lines if r["population"] == args.population]
        else:
            answers[name[len("replay/"):-len(".jsonl")]] = {
                (line["repository"], line["head"]): line for line in lines}
    sheet, key, summary = label_kit(rows, answers, heuristic)
    tag = args.population
    files = {f"sheet-{tag}.csv": csv_bytes(SHEET_COLUMNS, sheet),
             f"sheet-{tag}.md": sheet_markdown(tag, sheet),
             f"key-{tag}.csv": csv_bytes(KEY_COLUMNS, key)}
    summary.update(population=tag, seed=SEED, inputs=inputs,
                   files={name: hashlib.sha256(data).hexdigest() for name, data in files.items()})
    files[f"sheet-{tag}.json"] = (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode()
    # Written once: a file that holds other bytes, a filled sheet among them,
    # refuses the run before anything is written. The same bytes are kept, so
    # a run stopped between two files finishes on the next.
    labels = args.out / "labels"
    other = [name for name, data in files.items()
             if (labels / name).exists() and (labels / name).read_bytes() != data]
    if other:
        raise Refused(f"labels/{other[0]} exists with other contents; a sheet is written once")
    labels.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        target = labels / name
        if not target.exists():
            part = target.with_name(name + ".part")
            part.write_bytes(data)
            os.replace(part, target)
    json.dump(summary, out, indent=2, sort_keys=True)
    out.write("\n")
    return 0


# --- entry --------------------------------------------------------------------

def default_db(env=None) -> Path:
    env = os.environ if env is None else env
    return db_path(Path(env.get("HOME", "~")).expanduser() / ".local/share/sd/sd.db")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev_experiment.py", description=__doc__.splitlines()[0])
    verbs = parser.add_subparsers(dest="verb", required=True)
    rec = verbs.add_parser("reconcile", help="step 0: entry 1's counts under each extraction")
    rec.add_argument("--db", type=db_path, help="default: the live workflow database")
    rec.add_argument("--since", default=ENTRY1_FROM)
    rec.add_argument("--until", default=ENTRY1_TO)
    verbs.add_parser("heuristic", help="step 3: check the frozen heuristic's SHA-256")
    fai = verbs.add_parser("faithful", help="step 1: re-ask Jev on 50 rebuilt states")
    fai.add_argument("--db", type=db_path, help="default: the live workflow database")
    fai.add_argument("--ledger", type=db_path, required=True, help="the experiment database")
    fai.add_argument("--out", type=Path, required=True, help="a new folder for the answers")
    fai.add_argument("--exclude", help="an earlier attempt's answers.jsonl; its heads are not drawn")
    pop = verbs.add_parser("population", help="step 2: every reviewed head of C1 and E, with its state")
    pop.add_argument("--db", type=db_path, help="default: the live workflow database")
    pop.add_argument("--out", type=Path, required=True, help="the experiment data folder")
    pop.add_argument("--pack", type=Path, help=f"the pack checkout; default ~/{PACK_HOME}")
    lab = verbs.add_parser("labels", help="step 4: labels from the review at each head")
    kinds = lab.add_subparsers(dest="kind", required=True)
    obj = kinds.add_parser("objective", help="blocking@head from the checkpoints")
    obj.add_argument("--population", choices=("E", "C1", "C2"), required=True)
    obj.add_argument("--db", type=db_path, help="default: the live workflow database")
    obj.add_argument("--out", type=Path, required=True,
                     help="the experiment data folder, holding population.jsonl")
    sht = kinds.add_parser("sheet", help="step 8: the blinded hand-label sheet and its key")
    sht.add_argument("--population", choices=("C1", "C2"), required=True,
                     help="the sealed population to draw from; each gets its own sheet")
    sht.add_argument("--db", type=db_path, help="default: the live workflow database")
    sht.add_argument("--out", type=Path, required=True,
                     help="the experiment data folder, holding population.jsonl and replay/")
    rep =verbs.add_parser("replay", help="step 5: ask one model arm about every head")
    rep.add_argument("--arm", choices=MODEL_ARMS, required=True)
    rep.add_argument("--population", type=Path, required=True, help="population.jsonl")
    rep.add_argument("--out", type=Path, required=True, help="the experiment data folder")
    rep.add_argument("--ledger", type=db_path, required=True, help="the experiment database")
    rep.add_argument("--db", type=db_path, help="default: the live workflow database")
    rep.add_argument("--faithful", type=Path, required=True,
                     help="step 1's passing result folder; Jev reuses its answers")
    rep.add_argument("--cap", type=dollars, default=SPEND_CAP, help="hosted spend cap in US dollars")
    rep.add_argument("--traces", default=TRACES_URL, help="the OTLP traces endpoint")
    rep.add_argument("--rerun", metavar="REASON", help="a second run, reported as one")
    rep.add_argument("--resume", metavar="REASON",
                     help="go on after a halt, once the operator has reviewed it")
    rep.add_argument("--preregistration", type=Path, default=REGISTRATION, help=argparse.SUPPRESS)
    return parser


VERBS = {"reconcile": cmd_reconcile, "heuristic": cmd_heuristic, "faithful": cmd_faithful,
         "population": cmd_population, "labels": cmd_labels, "replay": cmd_replay}


def main(argv=None, out=None, env=None) -> int:
    out = sys.stdout if out is None else out
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    try:
        return VERBS[args.verb](args, out, env)
    except Refused as refusal:
        sys.stderr.write(f"jev_experiment: {refusal}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
