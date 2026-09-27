"""`docs/work/*/prd.md` in every registered repository, as `item` rows.

Sixty-four of them across six repositories on 2026-09-05, and every one of
the four rules below was written because the obvious reading of "import the
prds" gets it wrong.

**The `repo` table is the bound, not the disk.** Item D's runner clones a
registered repository's whole working tree, so a clone carries its own
`docs/work/*/prd.md` files. On "every prd on the machine" those files have no
`item` row and fail criterion 6 for as long as the retention holds the clone.
Enumeration reads the table; a clone was never added to it.

**Committed trees, never the working copy.** A row read from an editor buffer
is a row nobody can get back to: `source_commit` is what `sd restore
reimport` follows, and a dirty file has no commit. So the sitting refuses
while `git status --porcelain` names anything under `docs/work`, and reads
every file with `git show`.

**Every branch of the remote, not `HEAD` alone.** An item lives on its branch
until its merge, so a branch can carry an item the default branch has never
seen -- this very item did, for the whole of its planning. Two branches whose
`status:` lines disagree refuse the sitting, naming the item and both
branches: picking one would be picking which of two people was right, which
is a decision and not an import.

**The idle clock is seeded from the source.** Thirty-eight of the
forty-eight prds in one shared benchmark repository predate the forty-five
day threshold. A row whose `created_at` came from the import is a row the first sweep offers
as fresh work, thirty-eight at once, all of them touched by nothing but the
migration.

**The import writes no file; the retire writes exactly one commit.** The
`status:` line stays exactly where it is through every import -- criterion
7's import clause is that a `done` item lands as a `done` row *with its line
untouched*. `Reader.retire` at the bottom of this file is the step that
removes them, once, in one commit, after the writer that stops producing them
landed in item A's slice 2. It removes the line of every active item and
leaves the archive's alone: an archived item's line is a record of what its
status *was*, and the database holds no opinion about an item nobody will ask
about again. On 2026-09-06 the pack's default branch carried five active
`prd.md` files and 491 archived ones, and the one-star glob above is what
tells them apart.

**The marker is what a checkout with no database reads.** The same commit
adds `docs/work/.status-source` holding the single word `row`. A clone with
no database and no pack state cannot ask the `repo` table anything; it can
read a tracked file, and the presence of that file is how it knows the line's
absence is deliberate rather than a prd somebody wrote wrong.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..repos import registered
from .. import paths as sdpaths
from ..writes import STATUSES, upsert_item, upsert_repo
from . import Counts, Frozen, MigrationRefused, Record, Retired
from .frontmatter import FENCE
from .frontmatter import read as read_frontmatter

#: The directory this migration reads, relative to a repository root.
DIRECTORY = "docs/work"

#: The file inside an item's directory that carries its frontmatter.
FILENAME = "prd.md"

#: `source` on every row this migration writes. The pair `(source,
#: external_id)` is the schema's unique index, which is what makes a second
#: run an update rather than a second row.
SOURCE = "docs/work"

#: Who the status change is attributed to when a line moved since last time.
WHO = "docs/work migration"

#: The pack's review lane refuses a commit that does not say who wrote it, and
#: this migration writes commits *into* pack checkouts. Without the trailer
#: every retire lands a commit that fails the receiving repository's `route`
#: check, which is what happened on 2026-09-07 and had to be repaired by hand
#: with `sd attribute`. `human` is the honest entry: the operator ran the
#: sitting and no model composed the diff.
TRAILER = "Authored-with: human"

#: The tracked marker the retire commits, and the one word it holds. A
#: checkout with no database reads this file and nothing else to learn that
#: the missing `status:` line is the answer rather than an omission.
MARKER = f"{DIRECTORY}/.status-source"
MARKER_BODY = "row\n"

#: What the `repo` row says once the marker is committed.
ROW = "row"

GIT_TIMEOUT_SECONDS = 30


def _git(path: Path, *args: str) -> tuple[int, str, str]:
    try:
        done = subprocess.run(  # nosec B603 - fixed argv, shell=False
            ["git", "--no-optional-locks", "-C", str(path), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"git -C {path} {' '.join(args)} timed out"
    except OSError as error:
        return 127, "", f"cannot run git: {error}"
    return done.returncode, done.stdout, done.stderr


def _read(path: Path, *args: str) -> str:
    code, out, err = _git(path, *args)
    if code != 0:
        raise MigrationRefused(
            f"git -C {path} {' '.join(args)} exited {code}: "
            f"{(err or out).strip().splitlines()[0] if (err or out).strip() else 'no output'}"
        )
    return out


def default_branch(path: Path) -> str:
    """The branch a merge lands on: `origin/HEAD`'s target, else `origin/main`.

    Read rather than assumed. Two of the fleet's repositories still call it
    `master`, and a hard-coded `main` would treat their default as one more
    competing branch.
    """
    out = _read(
        path, "for-each-ref", "--format=%(symref:short)", "refs/remotes/origin/HEAD"
    ).strip()
    return out or "origin/main"


def branches(path: Path) -> list[str]:
    """Every unmerged branch of the remote, plus the default, newest last.

    Three things are filtered out and each one earns its line.

    `refs/remotes/origin/HEAD` is a symbolic ref onto another entry in the
    same list -- it prints as the bare `origin` -- and would double every file
    it names.

    **A branch already merged into the default is history, not a claim.** An
    item lives on its branch until its merge; afterwards the default carries
    it and the old branch carries whatever it said the day it landed. Left in,
    a stale merged branch disagrees with the default forever and refuses the
    sitting every night until somebody prunes it -- which was the first thing
    this migration did against the real fleet, on a branch merged three days
    earlier. Measured 2026-09-06.

    A repository with no remote falls back to its own branches rather than
    enumerating nothing: an empty list here reads as "this repository has no
    items", and being silently wrong about that is what this file is written
    against.
    """
    out = _read(
        path, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/**"
    )
    found = [
        line.strip() for line in out.splitlines()
        if line.strip() and line.strip() not in ("origin", "origin/HEAD")
    ]
    if not found:
        out = _read(path, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        return [line.strip() for line in out.splitlines() if line.strip()]
    default = default_branch(path)
    kept = [ref for ref in found if ref == default or not merged(path, ref, default)]
    return kept or found


def _branch_tips(path: Path, output: str, prefix: str) -> dict[str, str]:
    """Normalize full refs without treating the cached origin/HEAD alias as a branch."""
    tips: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split()
        if (len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0])
                or not fields[1].startswith(prefix) or fields[1] == prefix):
            raise MigrationRefused(f"{path}: cannot verify malformed origin branch evidence")
        commit, ref = fields
        if ref == "refs/remotes/origin/HEAD":
            continue
        name = ref.removeprefix(prefix)
        if name in tips:
            raise MigrationRefused(f"{path}: cannot verify duplicate origin branch {name}")
        tips[name] = commit
    return tips


def fresh_origin(path: Path) -> None:
    """Verify the cached branch set against the remote without fetching or pruning."""
    cached = _branch_tips(path, _read(
        path, "for-each-ref", "--format=%(objectname)%09%(refname)", "refs/remotes/origin/"
    ), "refs/remotes/origin/")
    if "origin" not in _read(path, "remote").splitlines():
        if cached:
            raise MigrationRefused(f"{path}: cached origin branches have no origin remote; restore origin before migrating")
        return  # A repository that has no origin still reads its local branches.
    code, output, _ = _git(path, "ls-remote", "--heads", "origin")
    if code:
        raise MigrationRefused(
            f"{path}: cannot verify live origin branches (git ls-remote exited {code}). "
            "Restore remote access, run git fetch --prune origin, and retry; the reader did not fetch."
        )
    advertised = _branch_tips(path, output, "refs/heads/")
    differences = []
    for name in sorted(cached.keys() | advertised.keys()):
        if name not in cached:
            differences.append(f"origin/{name} is missing from cached refs (remote {advertised[name]})")
        elif name not in advertised:
            differences.append(f"origin/{name} is stale: deleted on the remote (cached {cached[name]})")
        elif cached[name] != advertised[name]:
            differences.append(f"origin/{name} is stale: cached {cached[name]}, remote {advertised[name]}")
    if differences:
        raise MigrationRefused(
            f"{path}: origin branch cache differs from the live remote: {'; '.join(differences)}. "
            "Run git fetch --prune origin in this checkout and retry; the reader did not fetch."
        )


def live(path: Path, candidates: list["Candidate"], default: str) -> list["Candidate"]:
    """The candidates still making a claim about this file.

    A branch competes for a file only until its change to that file is in the
    default. Afterwards the branch still *carries* the file -- at whatever it
    said the day the branch was cut -- and reading that as a competing claim
    refuses the sitting over a disagreement that was settled by a merge.

    So a candidate whose last commit touching the file is already an ancestor
    of the default is superseded, and the default's own candidate never is.
    Measured against the real fleet on 2026-09-06: three files disagreed
    across ten branches, and every one of the disagreements was an old copy
    on a branch that had never touched the file.

    If every candidate is superseded -- a file deleted from the default and
    surviving only on branches -- none is dropped. Returning nothing here
    would lose the item entirely, which is worse than reporting a
    disagreement.
    """
    kept = [
        candidate for candidate in candidates
        if candidate.branch == default or not merged(path, candidate.commit, default)
    ]
    return kept or candidates


def merged(path: Path, ref: str, into: str) -> bool:
    """Whether `ref`'s tip is already an ancestor of `into`'s.

    `git merge-base --is-ancestor` exits 0 for yes and 1 for no, and anything
    else -- an unknown ref, a broken repository -- is neither. An error is
    reported as "not merged", which keeps the branch in the enumeration: the
    failure of a filter must never be a silent drop of a branch that carries
    an item.
    """
    code, _, _ = _git(path, "merge-base", "--is-ancestor", ref, into)
    return code == 0


def dirty(path: Path) -> list[str]:
    """Anything uncommitted under `docs/work`, in `git status` order."""
    code, out, err = _git(path, "status", "--porcelain", "--", DIRECTORY)
    if code != 0:
        raise MigrationRefused(f"git -C {path} status failed: {(err or out).strip()}")
    return [line.rstrip() for line in out.splitlines() if line.strip()]


def files(path: Path, ref: str) -> list[str]:
    """Every active `docs/work/<slug>/prd.md` in one branch's tree.

    The glob in the requirement is `docs/work/*/prd.md` with one star, and
    the star matters: `docs/work/archive/2026-06/<slug>/prd.md` is two levels
    deeper and is closed history. Criterion 7 says the same thing from the
    other side -- `sd-docs-lint` fails on a `status:` line in any `prd.md`
    under `docs/work/` **outside the archive**.

    Measured 2026-09-06: matching every `prd.md` under the directory found
    1,258 across eleven repositories, of which 486 were the pack's archive
    alone. The requirement counted sixty-four across six, and that is the set
    this returns.
    """
    out = _read(path, "ls-tree", "-r", "--name-only", ref, "--", DIRECTORY)
    wanted = []
    for line in out.splitlines():
        relative = line.strip()
        parts = relative.split("/")
        if len(parts) == 4 and parts[:2] == DIRECTORY.split("/") and parts[3] == FILENAME:
            wanted.append(relative)
    return sorted(wanted)


def archived(path: Path, ref: str) -> list[str]:
    """Every `prd.md` under `docs/work/archive/` in one branch's tree.

    The complement of `files` under the same directory, counted rather than
    edited. The retire reports both halves because "the lines are gone" is
    only half an answer: the other half is that 491 archived lines are still
    there, deliberately, and a run that quietly swept them would report the
    same success.
    """
    out = _read(path, "ls-tree", "-r", "--name-only", ref, "--", DIRECTORY)
    found = []
    for line in out.splitlines():
        relative = line.strip()
        parts = relative.split("/")
        if (
            len(parts) > 4
            and parts[:3] == [*DIRECTORY.split("/"), "archive"]
            and parts[-1] == FILENAME
        ):
            found.append(relative)
    return sorted(found)


def marker(path: Path, ref: str = "HEAD") -> str | None:
    """What the committed marker says in this tree, or `None` when absent.

    Read from the tree and not from the working copy: an untracked file of
    that name is somebody's note to themselves, and the whole point of the
    marker is that it arrives with the commit that removed the lines.
    """
    code, out, _ = _git(path, "show", f"{ref}:{MARKER}")
    return out.strip() if code == 0 else None


def retired(path: Path, ref: str = "HEAD") -> bool:
    """Whether this checkout has already been retired."""
    return marker(path, ref) == ROW


def without_status(text: str) -> tuple[str, bool]:
    """The file with its frontmatter `status:` line removed, and whether one was.

    A line-level edit inside the fenced block, not a parse and a re-emit.
    Re-emitting would normalise every other line on the way past -- quoting,
    spacing, the order keys were written in -- and turn a one-line removal
    into a diff nobody can review. It also leaves a `status:` in the body
    alone, which in a prd is prose about status and not a field.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FENCE:
        return text, False
    end = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == FENCE),
        None,
    )
    if end is None:
        return text, False
    kept = [
        line
        for index, line in enumerate(lines)
        if not (
            0 < index < end
            and not line[:1].isspace()
            and ":" in line
            and line.split(":", 1)[0].strip() == "status"
        )
    ]
    return "".join(kept), len(kept) != len(lines)


@dataclass
class Candidate:
    """One item's file as one branch carries it."""

    branch: str
    commit: str
    when: str
    status: str
    title: str
    created: str


@dataclass
class Reader:
    """The `docs/work` migration. Constructed with the repositories to read.

    Held as an object rather than five functions because the freeze and the
    verify must read the same set of repositories, and passing that set to
    both is how they stop agreeing when a caller forgets.
    """

    paths: list[str] = field(default_factory=list)
    name: str = SOURCE
    #: Registered repositories this reader deliberately left out because they
    #: have already been retired. Reported, never read.
    already: list[str] = field(default_factory=list)
    #: Registered repositories whose `repo` row already says `row`. The
    #: import narrows on this as well as on the marker: a row switched by a
    #: retire that was killed before its commit is a row that answers, and
    #: an import that read the lines it left behind would write them back
    #: over it.
    switched: list[str] = field(default_factory=list)

    @classmethod
    def from_table(cls, connection: sqlite3.Connection) -> "Reader":
        """The repositories the `repo` table holds, which is the bound."""
        rows = registered(connection)
        return cls(
            paths=[row["path"] for row in rows],
            switched=[row["path"] for row in rows if row["status_source"] == ROW],
        )

    def for_import(self) -> "Reader":
        """This reader, narrowed to the repositories the file is still the source for.

        The import and the verify ask this first. A retired repository has no
        line to read and a row that already answers, and reading it anyway
        refused the whole run on the first one met -- which, once every
        registered repository had retired, was every run of both verbs.
        Retired is the intended end state and not a fault, so those
        repositories go into `already` to be reported by name, and the run
        goes on to whatever is still on `file`.

        Not `for_retire`: that one narrows on the committed marker alone, so
        a retire killed between its switch and its commit reruns to the
        commit. The import has no commit to finish and narrows on the row as
        well.
        """
        done = [
            path for path in self.paths
            if path in self.switched or retired(sdpaths.expand(path))
        ]
        return replace(
            self,
            paths=[path for path in self.paths if path not in done],
            already=[*self.already, *done],
        )

    def for_retire(self) -> "Reader":
        """This reader, narrowed to the repositories not yet retired.

        A repository whose committed tree already holds the marker has no
        line left for the freeze to read, and `_candidate` refuses a status
        word it was not given -- correctly, since an empty status is exactly
        the shape a mangled prd has. So the retire narrows here rather than
        teaching the freeze about a second source of truth: the freeze stays
        "read this source", and this decides which repositories are still
        that source.

        The retire verb asks every source for this and every source that does
        not answer is used as it is, so a source with no notion of "partly
        retired" needs no method and gets no special case in the verb.
        """
        done = [path for path in self.paths if retired(sdpaths.expand(path))]
        # `replace` and not `Reader(...)`, so narrowing keeps whatever class
        # the caller handed in. Naming the class here would quietly discard a
        # subclass halfway through a sitting.
        return replace(
            self,
            paths=[path for path in self.paths if path not in done],
            already=[*self.already, *done],
        )

    # ------------------------------------------------------------ freeze

    def _candidate(self, root: Path, ref: str, relative: str) -> Candidate:
        text = _read(root, "show", f"{ref}:{relative}")
        matter, _ = read_frontmatter(text)
        status = (matter.get("status") or "").strip()
        if status not in STATUSES:
            # A retired tree has no line at all, and "'' is not one of
            # planning, ready, ..." sends the reader looking for a mangled
            # prd. Name the retire instead: the answer moved, it did not go.
            if not status and marker(root, ref) == ROW:
                raise MigrationRefused(
                    f"{root}/{relative} on {ref} carries no `status:` line and "
                    f"{root}/{MARKER} says {ROW}: this repository was retired "
                    f"and its status lives in the `item` row now. Read the row, "
                    f"not the file."
                )
            raise MigrationRefused(
                f"{root}/{relative} on {ref} has status {status!r}, which is not "
                f"one of {', '.join(STATUSES)}; the migration maps no word it was "
                f"not given"
            )
        newest = _read(
            root, "log", "-1", "--format=%H%x1f%aI", ref, "--", relative
        ).strip()
        commit, _, when = newest.partition("\x1f")
        # The frontmatter's own date first, the file's first commit second.
        # Neither is a fallback to `now()`: a row whose idle clock started at
        # the import is a row the first sweep offers as fresh work, and there
        # were thirty-eight of those waiting on 2026-09-05.
        #
        # The history walk is second because it is the expensive one -- a full
        # `git log` per file per branch, over sixty-four files and six
        # repositories -- and every prd written by `sd-plan` carries the line.
        created = str(matter.get("created") or "").strip()
        if not created:
            history = _read(
                root, "log", "--format=%aI", ref, "--", relative
            ).splitlines()
            created = history[-1].strip() if history else ""
        if not created:
            raise MigrationRefused(
                f"{root}/{relative} on {ref} has no `created:` line and no commit "
                f"history to take one from; the row's idle clock would start at "
                f"the import and the first sweep would offer it as fresh work"
            )
        return Candidate(
            branch=ref,
            commit=commit,
            when=when,
            status=status,
            title=str(matter.get("title") or relative).strip(),
            created=created,
        )

    def readable(self) -> None:
        """Refuse once, naming every repository that is not ready to be read.

        The three conditions below are unchanged, and so is the outcome: a
        sitting that meets any of them reads nothing. What changed is *when*
        the refusal is raised. Each one used to stop the freeze at the first
        repository that met it, so a fleet of sixty checkouts reported its
        stale caches one per run -- fetch, rerun, meet the next one -- and the
        enumeration never reached the repositories behind it. The first real
        run of this found `agntcy/coffeeAgntcy` and stopped, and nothing after
        it in the table was ever looked at (sd:1284).

        Probing all of them first costs nothing extra: `fresh_origin` was
        already going to run `ls-remote` against every repository on a clean
        sitting, so the work is the same and only the reporting is whole.
        """
        problems: list[str] = []
        for path in self.paths:
            root = sdpaths.expand(path)
            if not (root / ".git").exists():
                problems.append(
                    f"{root} is registered but is not a git checkout; remove it "
                    f"from the `repo` table or restore the checkout"
                )
                continue
            unclean = dirty(root)
            if unclean:
                problems.append(
                    f"{root} has uncommitted changes under {DIRECTORY}: "
                    f"{'; '.join(unclean)}. The migration reads committed trees, "
                    f"so a row would point at a commit that does not carry it. "
                    f"Commit or stash them and run the sitting again."
                )
                continue
            try:
                fresh_origin(root)
            except MigrationRefused as refusal:
                problems.append(str(refusal))
        if problems:
            raise MigrationRefused(
                f"{len(problems)} of {len(self.paths)} registered repositories are not "
                f"ready to be read, and the migration reads all or none:\n  - "
                + "\n  - ".join(problems)
            )

    def freeze(self) -> Frozen:
        records: dict[str, Record] = {}
        commits: dict[str, str] = {}
        notes: list[str] = []
        self.readable()
        for path in self.paths:
            root = sdpaths.expand(path)
            found: dict[str, list[Candidate]] = {}
            for ref in branches(root):
                for relative in files(root, ref):
                    found.setdefault(relative, []).append(
                        self._candidate(root, ref, relative)
                    )
            default = default_branch(root)
            for relative, all_candidates in sorted(found.items()):
                identity = f"{path}::{relative}"
                candidates = live(root, all_candidates, default)
                statuses = {candidate.status for candidate in candidates}
                if len(statuses) > 1:
                    disagreeing = ", ".join(
                        f"{candidate.branch} says {candidate.status}"
                        for candidate in sorted(candidates, key=lambda one: one.branch)
                    )
                    raise MigrationRefused(
                        f"{identity} disagrees across branches: {disagreeing}. "
                        f"The migration will not choose between them; make the "
                        f"lines agree and run the sitting again."
                    )
                # The newest commit carries the content, and its branch is the
                # one the row records: an item on a branch alone is landed from
                # that branch, which is the whole reason every branch is read.
                chosen = max(candidates, key=lambda one: (one.when, one.branch))
                on_default = [one for one in candidates if one.branch.endswith("/main")]
                if not on_default:
                    notes.append(
                        f"{SOURCE}: {relative} in {path} lives only on "
                        f"{chosen.branch}, landed at {chosen.commit[:12]}"
                    )
                records[identity] = Record(
                    identity=identity,
                    payload={
                        "repo": path,
                        "path": relative,
                        "title": chosen.title,
                        "status": chosen.status,
                        "branch": chosen.branch,
                        "source_commit": chosen.commit,
                        "created": chosen.created,
                    },
                )
                commits[identity] = chosen.commit
        return Frozen(source=self.name, records=records, commits=commits, notes=notes)

    # ------------------------------------------------------------ import

    def land(self, connection: sqlite3.Connection, frozen: Frozen) -> Counts:
        counts = Counts()
        for identity in sorted(frozen.records):
            payload = frozen.records[identity].payload
            _, what = upsert_item(
                connection,
                source=SOURCE,
                external_id=identity,
                kind="work",
                title=payload["title"],
                status=payload["status"],
                who=WHO,
                created_at=payload["created"] or None,
                repo=payload["repo"],
                path=payload["path"],
                branch=payload["branch"],
                source_commit=payload["source_commit"],
            )
            counts.record(what)
        return counts

    # ------------------------------------------------------------ retire

    def nothing_left(self) -> bool:
        """Whether a retire sitting has anything to do at all.

        The counterpart of `for_retire`, and asked before the freeze. A
        reader narrowed to nothing would otherwise freeze an empty source and
        the verify would report every row that exists as "no longer in the
        source" -- true of the empty set it just read, and a refusal in place
        of the plain answer, which is that this was done already.
        """
        return not self.paths

    def switch(self, connection: sqlite3.Connection) -> list[str]:
        """Point every repository this reader covers at its rows. Idempotent.

        Separate from the commit, and before it, because those are the two
        halves a killed sitting can land between. Before: the lines are still
        the answer and still say what the rows say, so a rerun repeats the
        whole sitting. After: the rows are the answer and the lines are a
        stale copy of the same words, so a rerun goes on to the commit. The
        other order has a window where the lines are gone and the rows are
        not yet authoritative, and nothing can answer at all.

        `upsert_repo` writes only the fields it is given, so this touches
        `status_source` and the timestamp and nothing else on the row.
        """
        return [
            upsert_repo(connection, path, status_source=ROW) for path in self.paths
        ]

    def _remove(self, root: Path, relative: str) -> bool:
        path = root / relative
        text = path.read_text(encoding="utf-8")
        rewritten, changed = without_status(text)
        if changed:
            path.write_text(rewritten, encoding="utf-8")
        return changed

    def retire(self, connection: sqlite3.Connection, frozen: Frozen) -> Retired:
        """Switch the rows, then remove the lines, in one commit per repository.

        `frozen` is the sitting's own freeze and is what says this ran after a
        verify rather than on its own. Nothing is re-read from it: the removal
        enumerates `HEAD`'s tree, because the freeze has already refused
        anything uncommitted under the directory and `HEAD` is therefore the
        same bytes the verify agreed with.

        One commit, and it carries both halves. A commit that removed the
        lines without the marker would leave a checkout with no database
        unable to tell a retired prd from a broken one; a marker without the
        removal would claim a migration that had not happened.
        """
        if frozen is not None and frozen.source != self.name:
            raise MigrationRefused(
                f"this is the {self.name} retire and it was handed a freeze of "
                f"{frozen.source!r}; one sitting retires one source"
            )
        result = Retired(already=list(self.already))
        result.switched = self.switch(connection)
        for path in self.paths:
            root = sdpaths.expand(path)
            if retired(root):
                # Killed after the commit and rerun: the marker is the record
                # that this repository is done, and doing it twice would make
                # an empty commit and a second entry in the report.
                result.already.append(path)
                continue
            active = files(root, "HEAD")
            removed = [one for one in active if self._remove(root, one)]
            (root / MARKER).write_text(MARKER_BODY, encoding="utf-8")
            result.removed += len(removed)
            kept = len(archived(root, "HEAD"))
            result.kept += kept
            _read(root, "add", "--", DIRECTORY)
            _read(
                root, "commit", "-q", "-m",
                f"chore(docs/work): the row is the status, not the line\n\n"
                f"{len(removed)} active `status:` line(s) removed; the archive "
                f"keeps its own, which are records of what was. "
                f"{MARKER} says {ROW}.\n\n{TRAILER}",
                "--", DIRECTORY,
            )
            result.commits[path] = _read(root, "rev-parse", "HEAD").strip()
            result.notes.append(
                f"{SOURCE}: {path}: {len(removed)} of {len(active)} active "
                f"line(s) removed, {kept} archived kept"
            )
        return result

    # ------------------------------------------------------------ verify

    def rows(self, connection: sqlite3.Connection) -> dict[str, Record]:
        # A repository this reader left out as retired is one whose rows it
        # did not read the source of, and a verify that still held them
        # against an empty freeze would call every one "no longer in the
        # source". Left out of both sides, so the comparison is of like
        # with like.
        held: dict[str, Record] = {}
        for row in connection.execute(
            "SELECT * FROM item WHERE source = ? ORDER BY external_id", (SOURCE,)
        ):
            if row["repo"] in self.already:
                continue
            held[row["external_id"]] = Record(
                identity=row["external_id"],
                payload={
                    "repo": row["repo"],
                    "path": row["path"],
                    "title": row["title"],
                    "status": row["status"],
                    "branch": row["branch"],
                    "source_commit": row["source_commit"],
                    "created": row["created_at"],
                },
            )
        return held
