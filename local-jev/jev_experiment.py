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

**It reads the workflow database read-only.** Every query opens it through
`sd_db`'s `connect(write=False)`, which is `mode=ro` and `query_only`;
nothing here writes a checkpoint or a live ledger row.

**It never writes the live ledger.** A Jev call runs `jev.sh` with
`JEV_METER_DB` pointed at the experiment database, both comparison switches
empty, shadow off and traces off, and refuses to start while `jev_meter.ready`
refuses that database.

**It sends no path but a public one.** A state holding a path sign, or a `/`
in anything but a path a public managed repository tracks, halts the run
before the call (the stopping rule's privacy halt).

Stdlib and `sd_db`, the one opener of the workflow database. Raw output goes
to `--out`, outside the checkout.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import unicodedata
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
    """Every path `git ls-files` lists in a managed repository whose last
    observed visibility is public, from its local checkout, read once per run.
    Public is what `sd shadow sync` stored (`sd_db.protection.alerts`), the
    same reading Health's Security area uses. `protection.rows` lends an
    unobserved checkout the row of another with the same stored remote, so
    the checkout's own `origin` must be that remote (codex pass on round 4).
    A repository not read as public, a checkout whose `origin` differs or is
    missing, or one git cannot list, adds nothing: unknown is not public."""
    from sd_db import protection
    from sd_db.paths import home_absolute
    with contextlib.closing(open_ro(db)) as connection:
        repos = protection.rows(connection)
    tracked = set()
    for repo in repos:
        part = (repo.get("alerts") or {}).get("secret_scanning")
        if not repo["managed"] or not isinstance(part, dict) or part.get("visibility") != "public":
            continue
        root = home_absolute(repo["repo"])
        origin = git(root, "config", "--get", "remote.origin.url")
        slug = protection.github_slug(origin.decode("utf-8", "replace").strip()) if origin else None
        if slug is None or not repo["slug"] or "/".join(slug).lower() != repo["slug"].lower():
            continue
        listed = git(root, "ls-files", "-z")
        if listed is None:
            continue
        for raw in listed.split(b"\0"):
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if name:
                tracked.add(name)
    return frozenset(tracked)


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
            rep = report(one)
            subject, route = rep.get("subject"), rep.get("route")
            head = one.get("head")
            ident = (body.get("repository") or "", head)
            if not got or not head or ident in found:
                continue
            if not isinstance(subject, dict) or not isinstance(route, dict):
                continue
            paths, lines, reason = subject.get("paths"), subject.get("lines"), route.get("reason")
            routed = got.get("routed_tier")
            if not isinstance(paths, list) or type(lines) is not int \
                    or not isinstance(reason, str) or routed not in TIERS \
                    or raw_tier(got) not in TIERS:
                continue
            found[ident] = {
                "repository": ident[0], "head": head, "checkpoint": key, "at": stamp,
                "routed": routed, "recorded": raw_tier(got),
                "moved": raw_tier(got) != routed,
                "state": state_of(paths, lines, routing_said(reason)),
            }
    return sorted(found.values(), key=lambda c: (c["repository"], c["head"]))


def draw(pool: list[dict], count: int = FAITHFUL_COUNT, seed: int = SEED,
         exclude=frozenset()) -> list[dict]:
    """Half from moved heads, half from the rest, seeded; `exclude` holds the
    heads an earlier attempt drew, so a second attempt uses new states."""
    rng = random.Random(seed)
    usable = [c for c in pool if (c["repository"], c["head"]) not in exclude]
    moved = [c for c in usable if c["moved"]]
    kept = [c for c in usable if not c["moved"]]
    half = count // 2
    if len(moved) < half or len(kept) < count - half:
        raise Refused(f"too few states to draw: {len(moved)} moved, {len(kept)} not")
    return rng.sample(moved, half) + rng.sample(kept, count - half)


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


def cmd_faithful(args, out, env) -> int:
    # By file identity, before the ledger is opened or any call is made: a
    # link or another spelling of the live or source database is that database.
    # Each path is normalised once, and the meter gets that exact string.
    # `--ledger` and `--db` are `db_path` results already; a second pass
    # would strip a resolved name that ends in space.
    ledger, db = args.ledger, args.db or default_db(env)
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
    chosen = draw(candidates(db), exclude=exclude)
    home = env.get("HOME")
    public = public_paths(db)
    for one in chosen:
        hit = private_path(one["state"], home, public)
        if hit is not None:
            raise Refused(f"privacy halt: a state for {one['head'][:12]} holds a path "
                          "sign or an untracked path; the operator reviews before any resume")
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
        row["state"] = state_of(found["paths"], found["lines"], found["reason"]) if found else None
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
    return parser


VERBS = {"reconcile": cmd_reconcile, "heuristic": cmd_heuristic, "faithful": cmd_faithful,
         "population": cmd_population}


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
