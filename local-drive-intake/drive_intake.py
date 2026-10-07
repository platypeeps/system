"""Notice files arriving in the mounted Google Drives, and say what they are.

Both watched Drives are mounted by Google Drive for Desktop and mirrored
nightly by `local-mirror-sync`. The mirror is `rsync -a --delete` and keeps no
change log, so a file can land in either Drive, be copied to the NAS, and never
produce a signal anybody sees. 488 files changed across the two mounts in the
thirty days before this module was written.

This walks the mounts and compares each file against a state file on three
cheap attributes -- relative path, size, modification time. Not a hash: the
mounts hold 3,281 files and the question is "what is new", not "what is
corrupt". The comparator is the plan's, section 5.7, recorded in the private
repository this module was written for.

**Nothing private goes in this repository.** The conf names the mounts, and a
mount path carries the account address, so it lives in the config folder at
<config>/drive-intake/drive-intake.conf.

**This module never writes to a Drive.** It opens a Drive path exactly once,
with `os.scandir`, and never with a mode that can modify. Everything it
produces lands in the state directory, which is outside both mounts. The test
suite asserts this empirically rather than by inspection: it snapshots a
fixture tree's paths, sizes, mtimes and hashes, runs a fetch over it, and
compares the snapshot byte for byte.
"""

from __future__ import annotations

import csv
import fnmatch
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import system_tools_config  # noqa: E402

# The folder name minus `local-`: the conf is <config>/drive-intake/drive-intake.conf.
TOOL = "drive-intake"

# Exit codes, the repo's convention: 0 something to act on, 3 nothing to do,
# 1 a real error. `local-cron-jobs` treats 1 as a failure and banners it, and
# treats 3 as a quiet success, so "no new files" must not be 1.
EXIT_FOUND = 0
EXIT_NONE = 3
EXIT_ERROR = 1

STATE_COLUMNS = ("root", "rel_path", "size", "mtime_ns", "seen_at")

# The arrivals log is append-only, and `reported_at` is why. A report that was
# overwritten by the next run meant a hand-run `fetch` at nine silently ate the
# arrivals the 06:45 digest would have mailed. Now a run only ever adds rows; a
# consumer marks the rows it has delivered and nothing else is consumed.
#
# It also makes "what arrived in August" answerable, which a replaced report
# never could.
ARRIVAL_COLUMNS = (
    "detected_at", "root", "rel_path", "route", "change",
    "size", "mtime", "from_path", "reported_at",
)

CHANGES = ("new", "modified", "moved")

# Four routes, from the plan. Anything unmatched is noise, which is a routing
# decision and not a failure: the digest shows the first three and counts the
# fourth, so a new kind of file is visible as a number before anybody writes a
# rule for it.
ROUTES = ("data-export", "document", "correspondence", "noise")

# Never walked. The first three are Finder's, the fourth is Google Drive's
# download staging directory -- it appears and vanishes as Drive syncs and its
# contents are regenerated each time, which `local-mirror-sync` documents as
# churn worth excluding. Walking it would report a dozen new files a day that
# are gone by the time anybody looks.
SKIP_DIRS = (".tmp.drivedownload", ".Trash", "#recycle")
SKIP_FILES = (".DS_Store", "Icon\r", ".localized")


@dataclass(frozen=True)
class Root:
    """One mounted Drive: a short label for reports, and where it is."""

    label: str
    path: Path


@dataclass(frozen=True)
class Rule:
    """A routing rule. First match in file order wins, so order is meaning."""

    route: str
    pattern: str


@dataclass(frozen=True)
class Entry:
    """One file as the walker found it."""

    root: str
    rel_path: str
    size: int
    mtime_ns: int


# ------------------------------------------------------------------ config


def parse_config(text: str) -> tuple[list[Root], list[Rule], list[str]]:
    """Read the conf. Returns roots, rules, and problems worth printing.

    Two row types, pipe-separated, because a Drive path contains spaces and an
    `@` and reading it with `read -r name value` in the shell would split it.

        root|<label>|<path>
        route|<route>|<glob against the relative path>

    A problem is collected rather than raised so one bad line does not hide the
    rest of the file, and so `status` can report every one of them at once.
    """
    roots: list[Root] = []
    rules: list[Rule] = []
    problems: list[str] = []

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            problems.append(f"line {number}: expected three pipe-separated fields, got {len(parts)}")
            continue
        kind, first, second = parts
        if kind == "root":
            if not first:
                problems.append(f"line {number}: root has no label")
                continue
            roots.append(Root(first, Path(os.path.expanduser(second))))
        elif kind == "route":
            if first not in ROUTES:
                problems.append(f"line {number}: unknown route {first!r}, expected one of {', '.join(ROUTES)}")
                continue
            rules.append(Rule(first, second))
        else:
            problems.append(f"line {number}: unknown row type {kind!r}")

    if not roots:
        problems.append("no root rows: nothing to walk")
    return roots, rules, problems


def classify(rel_path: str, rules: list[Rule], environ: dict[str, str] | None = None, run=None) -> str:
    """Which route a file rides. First matching rule wins; default is noise.

    Matched case-insensitively against the whole relative path, so a rule can
    name a folder (`Operations/Telemetry Data/*`) or an extension (`*.pdf`), and
    the same syntax covers both. `fnmatch` rather than a regex because the conf
    is meant to be edited by whoever is on the board that year.

    A rule miss falls to `jev_route`, which is `noise` when JEV_DRIVE_INTAKE
    switches this stage off or Jev cannot answer. The rules are unchanged and
    run first either way.
    """
    lowered = rel_path.lower()
    # Matched against the path and against a leading-slash form of it, so a
    # rule written `*/meetings/*` catches `Meetings/x.pdf` sitting at the top
    # of a root as well as `Example Board/Meetings/x.pdf` below it. Without
    # this, `*/` silently means "at least one folder deep", and a rule that
    # reads as "anywhere" quietly misses the root. Found by a test, not by
    # inspection.
    rooted = "/" + lowered
    for rule in rules:
        pattern = rule.pattern.lower()
        if fnmatch.fnmatch(lowered, pattern) or fnmatch.fnmatch(rooted, pattern):
            return rule.route
    return jev_route(rel_path, rules, environ, run)


# -------------------------------------------------------- the optional Jev step

# This stage's own switch, and it only ever subtracts. Unset means on: set it
# to `0`, `off`, `false`, `no` or `disabled` (any case) and a rule miss is
# noise, exactly as it was before Jev existed. Anything else leaves the stage
# on, so a typo cannot silently stop the lane.
#
# This used to be an opt-in, on the reasoning that an integration which has to
# be switched off to be safe is the wrong way round. The opposite failure
# turned out to cost more: a per-caller switch that defaults to off makes every
# integration added after it silently never run, and a lane that quietly stops
# running is the defect this repository has already been bitten by. Nothing
# here depends on Jev either way -- `jev off`, an unkeyed machine and a failing
# call all fall back to the rules, which run first and are unchanged.
JEV_STAGE = "JEV_DRIVE_INTAKE"

#: Who this is in the judgment ledger. It is the folder name, which the
#: ledger's identifier grammar accepts as it stands; a name that grammar
#: refuses is filed under `unknown`, which is the reason it is written here
#: rather than left to a variable nothing sets.
JEV_CALLER = "local-drive-intake"

# Where the sibling entrypoint lives, and the override the tests inject. Not
# `jev` on PATH: cron and CI run this with a PATH that `local-bin-links` never
# touched, and a caller that only works from an interactive shell is a caller
# that quietly stops running at 06:45.
JEV_COMMAND_VAR = "DRIVE_INTAKE_JEV"

# The answer that means "none of these routes fits". It is offered alongside the
# real route names so that Jev has somewhere to put a path it cannot place,
# rather than being forced to pick the least wrong route. It can never collide
# with a configured route, because `parse_config` refuses any route outside
# ROUTES and this is not one of them.
JEV_NO_MATCH = "none-of-these"

#: What `--fallback` prints when the call degrades: no key, the switch off, a
#: malformed setting. Deliberately outside ROUTES, which `parse_config`
#: validates against, so no conf can make a degraded answer look like a
#: judged one. This used to be `noise`, which is a route: `answer in routes`
#: was then true whether Jev judged or shrugged, and the caller got the same
#: string either way. A test asserts it stays outside ROUTES.
JEV_DEGRADED = "not-judged"


def jev_command(environ: dict[str, str]) -> list[str]:
    """The command that answers a question, as argv.

    Resolved from this file's own location, so the answer does not depend on
    the caller's PATH or working directory.
    """
    override = environ.get(JEV_COMMAND_VAR, "").strip()
    if override:
        return shlex.split(override)
    return [str(Path(__file__).resolve().parent.parent / "local-jev" / "jev.sh")]


def route_names(rules: list[Rule]) -> list[str]:
    """The routes this config actually defines, in file order, deduplicated.

    Read from the loaded rules rather than written down here. A hand-kept list
    of route names would be right on the day it was typed and wrong the first
    time somebody edits the conf, and the question Jev is asked would then
    quietly offer a route that no longer exists.
    """
    seen: list[str] = []
    for rule in rules:
        if rule.route not in seen:
            seen.append(rule.route)
    return seen


#: Set once the declined-probe reason has been printed. `jev_route` runs once
#: per unmatched path, so an unkeyed machine sweeping a Drive would otherwise
#: print the same line thousands of times -- which is how a true statement
#: becomes noise nobody reads. Said once, on the first path that asks.
_SAID: set[str] = set()


def _say_once(reason: str) -> None:
    if reason in _SAID:
        return
    _SAID.add(reason)
    print(f"jev: {reason}; unmatched paths stay noise", file=sys.stderr)


def jev_route(
    rel_path: str,
    rules: list[Rule],
    environ: dict[str, str] | None = None,
    run=None,
) -> str:
    """Ask Jev which route an unmatched path belongs to. Never raises.

    Returns `noise` -- the answer today -- when this stage is switched off, when
    Jev is switched off or unkeyed, when the call fails, and when the answer is
    not a route this config defines. Every one of those degradations says so on
    stderr: a lane that silently stops running is the defect this shape exists
    to avoid.

    Only the relative path is sent. Never the file's contents: every call leaves
    the machine, and this module reads two Drives full of governance paper.
    """
    environ = os.environ if environ is None else environ
    runner = subprocess.run if run is None else run
    # Both halves in one call, and it costs nothing: Jev can answer on this
    # machine, and JEV_DRIVE_INTAKE has not been used to switch this stage
    # off. Unset means on -- a per-caller switch defaulting to off makes every
    # integration added after it silently never run.
    try:
        # 30s is a guard and not a budget: `enabled` makes no network call.
        # `input=""` closes stdin. `enabled` reads none, but a command that
        # inherits an open stdin and happens to read it waits for the timeout
        # instead of answering, and this runs inside other people's pipelines.
        # `--record` so the decline is counted. It costs nothing extra: the
        # row is written by the process this call already starts, which is
        # why the decline is recorded here and nowhere else. `jev_route` runs
        # once per unmatched path, and a second subprocess per path is the
        # cost `_SAID` exists to avoid.
        probe = runner([*jev_command(environ), "enabled", JEV_STAGE, "--why",
                        "--record", "--caller", JEV_CALLER],
                       input="", capture_output=True, text=True, timeout=30,
                       env={**os.environ, **environ})
    except Exception as failure:  # a missing interpreter, a timeout, anything
        print(f"jev: could not be asked whether it is enabled ({failure}); "
              f"{rel_path} stays noise", file=sys.stderr)
        return "noise"
    if probe.returncode != 0:
        # Said out loud, which the flip is the reason for. While this was an
        # opt-in, a silent return here meant "nobody asked" and saying so would
        # have been noise. It now also covers an unkeyed machine, `jev off` and
        # a probe that failed -- exactly the degradations the docstring above
        # promises are never silent.
        #
        # `--why` carries the reason on stdout, and `capture_output` means
        # nothing of jev's reaches a terminal unless this prints it.
        _say_once(probe.stdout.strip() or f"jev enabled exited {probe.returncode}")
        return "noise"

    routes = route_names(rules)
    if not routes:
        return "noise"

    criteria = ",".join([*routes, f"{JEV_NO_MATCH}=no route here fits this path"])
    command = [
        *jev_command(environ),
        "choice",
        "Which intake route does this file path belong to?",
        "--criteria",
        criteria,
        "--caller",
        JEV_CALLER,
        "--stage",
        JEV_STAGE,
        "--fallback",
        JEV_DEGRADED,
    ]

    try:
        done = runner(
            command,
            input=rel_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as problem:  # noqa: BLE001 - any failure is the same answer
        print(f"jev: {rel_path}: call failed ({problem}); routed noise", file=sys.stderr)
        return "noise"

    # `--fallback` says why it degraded on stderr, and capturing it here would
    # be exactly the silence that shape exists to prevent. Pass it through.
    reason = (done.stderr or "").strip()
    if reason:
        print(reason, file=sys.stderr)

    if done.returncode != 0:
        print(
            f"jev: {rel_path}: exit {done.returncode}; routed noise",
            file=sys.stderr,
        )
        return "noise"

    answer = (done.stdout or "").strip()
    if answer == JEV_DEGRADED:
        # It degraded. The reason is already on stderr above; this is only
        # here so the marker can never fall through to the route check.
        return "noise"
    if answer in routes:
        return answer
    if answer and answer != JEV_NO_MATCH:
        print(f"jev: {rel_path}: answered {answer!r}, not a route here; routed noise", file=sys.stderr)
    return "noise"


# ------------------------------------------------------------------- walk


def walk(root: Root) -> list[Entry]:
    """Every file under one root, as Entry rows.

    `os.scandir` rather than `Path.rglob` because it returns the stat with the
    directory entry on macOS, which halves the syscalls over 3,281 files, and
    because a mounted Drive can raise on an entry mid-walk when the daemon is
    evicting a cached file. Such an entry is skipped, not fatal: a transient
    read error on one file must not stop the other 3,280 being reported.
    """
    found: list[Entry] = []
    base = str(root.path)
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except (PermissionError, FileNotFoundError, OSError):
            continue
        for item in entries:
            name = item.name
            try:
                if item.is_dir(follow_symlinks=False):
                    if name in SKIP_DIRS or name.startswith("."):
                        continue
                    stack.append(item.path)
                    continue
                if not item.is_file(follow_symlinks=False):
                    continue
                if name in SKIP_FILES or name.startswith("._") or name.startswith("."):
                    continue
                info = item.stat(follow_symlinks=False)
            except OSError:
                continue
            rel = os.path.relpath(item.path, base)
            found.append(Entry(root.label, rel, info.st_size, info.st_mtime_ns))
    found.sort(key=lambda e: (e.root, e.rel_path))
    return found


# ------------------------------------------------------------------ state


def read_state(path: Path) -> dict[tuple[str, str], tuple[int, int]]:
    """The last walk, keyed by root and relative path.

    A missing file is an empty dict and not an error, which is what makes the
    first run a baseline rather than a report of 3,281 new files.
    """
    if not path.exists():
        return {}
    state: dict[tuple[str, str], tuple[int, int]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                state[(row["root"], row["rel_path"])] = (int(row["size"]), int(row["mtime_ns"]))
            except (KeyError, ValueError, TypeError):
                continue
    return state


def write_state(path: Path, entries: list[Entry], seen_at: str) -> None:
    """Replace the state file, atomically.

    Written to a sibling and renamed, because a fetch interrupted halfway
    through a direct write would leave a truncated state, and a truncated state
    reports every missing row as new on the next run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(STATE_COLUMNS)
        for entry in entries:
            writer.writerow([entry.root, entry.rel_path, entry.size, entry.mtime_ns, seen_at])
    temporary.replace(path)


def diff(
    entries: list[Entry],
    state: dict[tuple[str, str], tuple[int, int]],
) -> list[tuple[Entry, str, str]]:
    """What changed since the last walk, as (entry, change, from_path).

    Additions, modifications and moves. A plain deletion is still not reported:
    nothing downstream acts on one, and Drive produces them constantly.
    """
    appeared: list[Entry] = []
    changes: list[tuple[Entry, str, str]] = []
    seen: set[tuple[str, str]] = set()

    for entry in entries:
        seen.add((entry.root, entry.rel_path))
        previous = state.get((entry.root, entry.rel_path))
        if previous is None:
            appeared.append(entry)
        elif previous != (entry.size, entry.mtime_ns):
            changes.append((entry, "modified", ""))

    disappeared = [(root, rel, size, mtime) for (root, rel), (size, mtime) in state.items()
                   if (root, rel) not in seen]

    moves = detect_moves(appeared, disappeared)
    for entry in appeared:
        origin = moves.get((entry.root, entry.rel_path))
        changes.append((entry, "moved", origin) if origin else (entry, "new", ""))
    return changes


def detect_moves(
    appeared: list[Entry],
    disappeared: list[tuple[str, str, int, int]],
) -> dict[tuple[str, str], str]:
    """Pair a vanished file with an appeared one. Returns new key -> old path.

    Google removes and re-creates a file on a move, so reorganising one folder
    would otherwise report every file in it as a new document and bury the two
    that genuinely arrived.

    **Matched on attributes, not content, and the reason is that the other side
    no longer exists.** Hashing was the obvious design and it cannot work here:
    by the time a move is visible the source file is gone, so there is nothing
    left to hash. Storing a hash for all 2,892 files at every walk would make
    it exact, at 19 GB of reading on the first run and permanent bookkeeping
    afterwards, to solve a problem that a filename and a timestamp already
    solve.

    The key is basename, size and modification time, because a move preserves
    all three while a rename or an edit changes one.

    **An ambiguous match is refused rather than guessed.** Across the whole
    corpus that key collides for 34% of files -- the Archive folder duplicates
    material that also lives elsewhere -- so a nearest-match rule would invent
    moves that never happened. Ambiguity is only judged inside one run's own
    candidate sets, which are small, and a pair that is not unique on both
    sides is simply left as a new file and a silent deletion. Under-reporting a
    move costs a line in the digest; over-reporting one hides a real document.
    """
    if not appeared or not disappeared:
        return {}

    def key(name: str, size: int, mtime: int) -> tuple[str, int, int]:
        return (os.path.basename(name), size, mtime)

    old_by_key: dict[tuple, list[str]] = {}
    for root, rel, size, mtime in disappeared:
        old_by_key.setdefault(key(rel, size, mtime), []).append(rel)

    new_by_key: dict[tuple, list[Entry]] = {}
    for entry in appeared:
        new_by_key.setdefault(key(entry.rel_path, entry.size, entry.mtime_ns), []).append(entry)

    moves: dict[tuple[str, str], str] = {}
    for k, olds in old_by_key.items():
        news = new_by_key.get(k)
        # Unique on both sides or not claimed at all.
        if not news or len(olds) != 1 or len(news) != 1:
            continue
        if olds[0] == news[0].rel_path:
            continue
        moves[(news[0].root, news[0].rel_path)] = olds[0]
    return moves


def arrival_rows(
    changes: list[tuple[Entry, str, str]],
    rules: list[Rule],
    detected_at: str,
    environ: dict[str, str] | None = None,
) -> list[dict]:
    """Turn a diff into arrival rows, route included."""
    rows = []
    for entry, change, origin in changes:
        mtime = datetime.fromtimestamp(entry.mtime_ns / 1e9, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
        rows.append({
            "detected_at": detected_at,
            "root": entry.root,
            "rel_path": entry.rel_path,
            "route": classify(entry.rel_path, rules, environ),
            "change": change,
            "size": entry.size,
            "mtime": mtime,
            "from_path": origin,
            "reported_at": "",
        })
    rows.sort(key=lambda r: (r["root"], r["rel_path"]))
    return rows


def append_arrivals(path: Path, rows: list[dict]) -> None:
    """Add rows to the log. Never rewrites what is already there.

    Appended rather than rewritten so a crash mid-write can lose at most the
    rows of the run in progress, and so no run can erase another run's
    undelivered arrivals.
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


def read_arrivals(path: Path, unreported_only: bool = True) -> list[dict]:
    """The arrivals log, optionally only the rows nobody has delivered yet."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [r for r in csv.DictReader(handle) if r.get("rel_path")]
    if unreported_only:
        rows = [r for r in rows if not (r.get("reported_at") or "").strip()]
    return rows


def stamp_arrivals(path: Path, through: str, stamped_at: str) -> int:
    """Mark every unreported row detected at or before `through`. Returns how many.

    Bounded by a cutoff rather than stamping everything unreported, because a
    `fetch` can append between the moment a consumer reads the log and the
    moment it stamps. Stamping the lot would mark rows that were never
    delivered, and those rows are then invisible forever.

    Rewritten through a temporary file so an interrupted stamp cannot leave the
    log truncated.
    """
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


# Above this many rows in one route, the report names folders instead of files.
# A reorganisation that move detection could not pair unambiguously still
# arrives as hundreds of rows, and a digest that lists them all is a digest
# nobody reads to the bottom.
COLLAPSE_AT = 40


def collapse_repeats(rows: list[dict]) -> list[dict]:
    """One row per file (root plus rel_path), carrying how many arrivals it had.

    A Drive-hosted Doc syncs as a stub whose content never changes while every
    sync touches its mtime, so one notice arrived sixteen times on 2026-09-19.
    The log keeps every row, because `stamp` marks rows; this is presentation.
    `new` wins over `modified`, as in the site morning digest; otherwise the
    latest row speaks for the file. See docs/conventions/file-intake.md.
    """
    by_file: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        by_file.setdefault((row["root"], row["rel_path"]), []).append(row)
    return [{**next((r for r in group if r["change"] == "new"), group[-1]), "arrivals": len(group)}
            for group in by_file.values()]


def group_for_report(rows: list[dict]) -> list[str]:
    """Lines for one route: files, or folders once there are too many."""
    if len(rows) <= COLLAPSE_AT:
        out = []
        for row in rows:
            origin = f"  <- {row['from_path']}" if row.get("from_path") else ""
            count = f", {row['arrivals']} arrivals" if row.get("arrivals", 1) > 1 else ""
            out.append(f"    [{row['root']}] {row['rel_path']}  ({row['change']}{count}){origin}")
        return out
    folders: dict[str, int] = {}
    for row in rows:
        folder = os.path.dirname(row["rel_path"]) or "."
        folders[f"{row['root']}:{folder}"] = folders.get(f"{row['root']}:{folder}", 0) + 1
    out = [f"    {len(rows)} files, collapsed to folders:"]
    for folder, count in sorted(folders.items(), key=lambda kv: -kv[1])[:20]:
        root, _, path = folder.partition(":")
        out.append(f"    [{root}] {path}/  ({count} files)")
    if len(folders) > 20:
        out.append(f"    ... and {len(folders) - 20} more folders")
    return out


# ----------------------------------------------------------------- verbs


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def missing_config(config_path: Path, out, prefix: str) -> None:
    """The standard remedy for an absent conf: where it goes, and what to copy."""
    print(f"{prefix}{config_path} does not exist. Copy "
          f"local-drive-intake/drive-intake.conf.example there and fill it in, "
          f"or set DRIVE_INTAKE_CONFIG.", file=out)


def load_roots(config_path: Path, out) -> tuple[list[Root], list[Rule]] | None:
    """Config, mounts checked. None means the caller should return EXIT_ERROR."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        missing_config(config_path, out, "error: ")
        return None
    except OSError as error:
        print(f"error: cannot read {config_path}: {error}", file=out)
        return None

    roots, rules, problems = parse_config(text)
    for problem in problems:
        print(f"config: {problem}", file=out)
    if not roots:
        return None

    missing = [r for r in roots if not r.path.is_dir()]
    if missing:
        # A mount that is not there is an error, not an empty walk. Google
        # Drive for Desktop being signed out looks exactly like every file
        # having been deleted, and reporting that as "nothing new" would be a
        # silent failure on the one day it matters.
        for root in missing:
            print(f"error: root {root.label} is not a directory: {root.path}", file=out)
        return None
    return roots, rules


def survey(roots: list[Root], state_dir: Path) -> tuple[list[Entry], list[tuple[Entry, str, str]], bool]:
    """Walk, then diff against the state. Shared by fetch and peek."""
    entries: list[Entry] = []
    for root in roots:
        entries.extend(walk(root))
    state_path = state_dir / "intake-state.csv"
    first_run = not state_path.exists()
    return entries, diff(entries, read_state(state_path)), first_run


def summarise(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["route"]] = counts.get(row["route"], 0) + 1
    return ", ".join(f"{counts[r]} {r}" for r in ROUTES if r in counts)


def cmd_fetch(config_path: Path, state_dir: Path, out, environ: dict[str, str] | None = None) -> int:
    """Walk both mounts, append what is new to the log, advance the state."""
    loaded = load_roots(config_path, out)
    if loaded is None:
        return EXIT_ERROR
    roots, rules = loaded

    entries, changes, first_run = survey(roots, state_dir)
    stamp = now_iso()
    write_state(state_dir / "intake-state.csv", entries, stamp)

    if first_run:
        # Baseline. Every one of the 2,892 files is technically new and none of
        # it is news, so the state is recorded and the log stays empty.
        print(f"baseline established: {len(entries)} files across {len(roots)} roots", file=out)
        print(f"state: {state_dir / 'intake-state.csv'}", file=out)
        return EXIT_NONE

    rows = arrival_rows(changes, rules, stamp, environ)
    append_arrivals(state_dir / "arrivals.csv", rows)

    if not rows:
        print(f"no change: {len(entries)} files across {len(roots)} roots", file=out)
        return EXIT_NONE

    moved = sum(1 for r in rows if r["change"] == "moved")
    tail = f" ({moved} of them moved, not new)" if moved else ""
    print(f"{len(rows)} changed: {summarise(rows)}{tail}", file=out)
    pending = len(read_arrivals(state_dir / "arrivals.csv"))
    print(f"log: {state_dir / 'arrivals.csv'}  ({pending} undelivered)", file=out)
    return EXIT_FOUND


def cmd_peek(config_path: Path, state_dir: Path, out, environ: dict[str, str] | None = None) -> int:
    """Say what a fetch would find, and change nothing.

    The safe verb. `fetch` advances the state, so running it by hand used to
    mean the next digest saw nothing; the append-only log fixed the losing-it
    half of that, and this fixes the wanting-to-look half.
    """
    loaded = load_roots(config_path, out)
    if loaded is None:
        return EXIT_ERROR
    roots, rules = loaded

    entries, changes, first_run = survey(roots, state_dir)
    if first_run:
        print(f"no state yet: a fetch would record {len(entries)} files as a baseline", file=out)
        return EXIT_NONE

    rows = arrival_rows(changes, rules, now_iso(), environ)
    if not rows:
        print(f"no change: {len(entries)} files across {len(roots)} roots", file=out)
        return EXIT_NONE
    print(f"a fetch would record {len(rows)}: {summarise(rows)}", file=out)
    for route in ROUTES:
        # One walk diffs each file once, so this collapses nothing today. It
        # keeps `peek` on the same one-line-per-file rule as `report`.
        group = collapse_repeats([r for r in rows if r["route"] == route])
        if not group:
            continue
        if route == "noise":
            print(f"\n  noise: {len(group)} files, not listed", file=out)
            continue
        print(f"\n  {route} ({len(group)})", file=out)
        for line in group_for_report(group):
            print(line, file=out)
    return EXIT_FOUND


def cmd_report(config_path: Path, state_dir: Path, out, show_all: bool = False) -> int:
    """Print undelivered arrivals grouped by route, for a human or a digest.

    Reading never stamps. A consumer that has actually delivered the rows says
    so with `stamp`, so looking at the log costs the digest nothing.
    """
    log = state_dir / "arrivals.csv"
    if not log.exists():
        # See `local-mail-intake`: the walk state is what says a fetch has run.
        # With it present, an absent log means nothing new was routed, which is
        # a quiet day and exits 3, not an error telling the caller to re-run.
        if (state_dir / "intake-state.csv").exists():
            print("nothing undelivered", file=out)
            return EXIT_NONE
        print(f"no arrivals log at {log}; run fetch first", file=out)
        return EXIT_ERROR

    rows = read_arrivals(log, unreported_only=not show_all)
    if not rows:
        print("nothing undelivered" if not show_all else "the log is empty", file=out)
        return EXIT_NONE

    shown = 0
    for route in ROUTES:
        group = collapse_repeats([r for r in rows if r.get("route") == route])
        if not group:
            continue
        if route == "noise":
            print(f"\nnoise: {len(group)} files, not listed", file=out)
            continue
        shown += len(group)
        print(f"\n{route} ({len(group)})", file=out)
        for line in group_for_report(group):
            print(line, file=out)
    return EXIT_FOUND if shown else EXIT_NONE


def cmd_stamp(state_dir: Path, through: str | None, out) -> int:
    """Mark arrivals delivered, up to and including a cutoff.

    The cutoff is the `detected_at` of the newest row the consumer actually
    sent. Without it a fetch racing the digest would have its rows marked
    delivered without ever being mailed.
    """
    log = state_dir / "arrivals.csv"
    if not log.exists():
        if (state_dir / "intake-state.csv").exists():
            print("nothing to stamp", file=out)
            return EXIT_NONE
        print(f"no arrivals log at {log}; run fetch first", file=out)
        return EXIT_ERROR
    cutoff = through or now_iso()
    count = stamp_arrivals(log, cutoff, now_iso())
    if not count:
        print("nothing to stamp", file=out)
        return EXIT_NONE
    print(f"stamped {count} arrival(s) delivered, through {cutoff}", file=out)
    return EXIT_FOUND


def cmd_status(config_path: Path, state_dir: Path, stale_hours: float, out) -> int:
    """Is the state fresh, is the config sound, and what is undelivered.

    0 fresh, 3 stale, 1 unusable. Reports everything wrong in one pass rather
    than returning on the first problem.
    """
    state_path = state_dir / "intake-state.csv"
    log = state_dir / "arrivals.csv"
    print(f"config : {config_path}", file=out)
    print(f"state  : {state_path}", file=out)

    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        missing_config(config_path, out, "config : ERROR ")
        return EXIT_ERROR
    except OSError as error:
        print(f"config : ERROR cannot read: {error}", file=out)
        return EXIT_ERROR

    roots, rules, problems = parse_config(text)
    for problem in problems:
        print(f"config : ERROR {problem}", file=out)
    print(f"routes : {len(rules)} rules", file=out)

    bad = bool(problems)
    for root in roots:
        mark = "ok     " if root.path.is_dir() else "MISSING"
        print(f"root   : {mark} {root.label}  {root.path}", file=out)
        if not root.path.is_dir():
            bad = True
    if bad:
        return EXIT_ERROR

    total = len(read_arrivals(log, unreported_only=False))
    pending = len(read_arrivals(log))
    print(f"log    : {total} arrivals recorded, {pending} undelivered", file=out)

    if not state_path.exists():
        print("state  : MISSING, no fetch has run", file=out)
        return EXIT_NONE

    age_hours = (time.time() - state_path.stat().st_mtime) / 3600.0
    with state_path.open(encoding="utf-8") as handle:
        rows = max(0, sum(1 for _ in handle) - 1)
    print(f"state  : {rows} files, last walk {age_hours:.1f}h ago", file=out)
    if age_hours > stale_hours:
        print(f"state  : STALE, older than {stale_hours:.0f}h", file=out)
        return EXIT_NONE
    return EXIT_FOUND


def main(argv: list[str], environ: dict[str, str] | None = None, out=None) -> int:
    out = sys.stdout if out is None else out
    environ = dict(os.environ if environ is None else environ)

    config_path = Path(environ.get("DRIVE_INTAKE_CONFIG")
                       or system_tools_config.config_dir(TOOL, environ) / "drive-intake.conf")
    state_dir = Path(environ.get("DRIVE_INTAKE_STATE", Path.home() / ".local/share/drive-intake"))
    try:
        stale_hours = float(environ.get("DRIVE_INTAKE_STALE_HOURS", "36"))
    except ValueError:
        print("error: DRIVE_INTAKE_STALE_HOURS is not a number", file=out)
        return EXIT_ERROR

    verb = argv[0] if argv else ""
    rest = argv[1:]
    if verb == "fetch":
        return cmd_fetch(config_path, state_dir, out, environ)
    if verb == "peek":
        return cmd_peek(config_path, state_dir, out, environ)
    if verb == "report":
        return cmd_report(config_path, state_dir, out, show_all="--all" in rest)
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
        return cmd_status(config_path, state_dir, stale_hours, out)
    print("usage: drive-intake.sh fetch|peek|report [--all]|stamp [--through TS]|status|test", file=out)
    return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - exercised via drive-intake.sh
    raise SystemExit(main(sys.argv[1:]))
