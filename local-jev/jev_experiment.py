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

**It reads the workflow database read-only.** Every query opens it through
`sd_db`'s `connect(write=False)`, which is `mode=ro` and `query_only`;
nothing here writes a checkpoint or a live ledger row.

**It never writes the live ledger.** A Jev call runs `jev.sh` with
`JEV_METER_DB` pointed at the experiment database, both comparison switches
empty, shadow off and traces off, and refuses to start while `jev_meter.ready`
refuses that database.

**It sends no absolute path.** A state holding one halts the run before the
call (the stopping rule's privacy halt).

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


#: A path sign anywhere in a string, with no exception for what comes before
#: it (review round 4; rounds 1 to 3 each let some character glue a slash
#: into a relative path, and each round found one that hid a root): any `/`,
#: `\`, `~` or `file:`, any `$` or `%` with something other than space after
#: it, and any control character. So every changed path that names a folder
#: halts, as do `1/2`, `and/or` and `profile:`; `a.py`, `25% off` and plain
#: words do not. A false halt costs a resume the operator reviews; a miss
#: sends a path off the machine.
ABSOLUTE = re.compile(r"[/\\~\x00-\x1f\x7f-\x9f]|[$%]\S|(?i:file):")


def private_path(state: str, home: str | None) -> str | None:
    """The first string in the state that holds an absolute or home path
    anywhere in it, else None."""
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
    for text in strings(json.loads(state)):
        if ABSOLUTE.search(text) or (home and home in text):
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
    for one in chosen:
        hit = private_path(one["state"], home)
        if hit is not None:
            raise Refused(f"privacy halt: a state for {one['head'][:12]} holds an "
                          "absolute or home path; the operator reviews before any resume")
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
    return parser


VERBS = {"reconcile": cmd_reconcile, "heuristic": cmd_heuristic, "faithful": cmd_faithful}


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
