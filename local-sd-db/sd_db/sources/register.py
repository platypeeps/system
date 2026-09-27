"""A research register's open entries, as `item` rows.

Three of them on 2026-09-05 -- O27, O28 and O29 -- in
`00-overview/open-questions.md`. The other twenty-six entries in that file
are decisions with follow-through, not open work, and the file says which is
which in its own header.

**The open set is read from the register, not from this file.** An earlier
draft of the plan named O27 to O29 in prose, and a list of ids compiled into
a migration is a list that is wrong the first time somebody adds O30. The
register's `Status:` header states the range; this reads that sentence and
takes the entries it names. A header this cannot parse is a refusal, because
the alternative is importing twenty-nine decisions as open work.

**The status is `planning` and is not inferred from the prose.** The register
writes each entry's state as English -- "O28 part-executed 2026-09-04, O27
not started, O29 parked by owner decision" -- and a migration that turned
that sentence into `in_progress`, `planning` and `blocked` would be guessing,
then presenting the guess as a row. The sentence is kept verbatim in
`fields`, where a reader can see what it actually says, and the operator
moves the three rows with `transition` once they are on the board.

**The repository must be registered.** `item.repo` references `repo(path)`,
so a register in a repository the table does not hold cannot land at all.
That is the right failure: criterion 6 enumerates `docs/work` from the same
table, and a repository worth importing a register from is one worth
enumerating.

**The committed tree on the default remote branch, never the working copy.**
Until 2026-09-11 this read the file off disk, which is whatever branch the
checkout happened to be on. On 2026-09-10 the simulator sat on
`feat/worldgen-gate1000` at `9452fce`, and O30 landed as row 366 from a
header sentence that existed only on that branch -- before it reached
`main`, and with no commit on the row to say where it had been read from.
`docs_work.Reader` had already settled the committed-tree half for the
prds: `git show` against a remote ref, never the disk, and the commit
recorded as `source_commit`. It reads every remote branch, because a prd
lives on its branch until the merge; a register has one answer, so this
reads only the default branch, resolved by the same `default_branch`
helper. A dirty file or a feature-branch checkout changes nothing about
what lands, and every row names the commit a reader can `git show` to see
the sentence it came from. The reader does not fetch: what `origin/main` says is what the
checkout last fetched, and the note the sitting prints names the ref and
the commit. The ref goes in the note and not in `item.branch`: that column
is the branch the runner works on, and a remote-tracking name there is the
defect sd:462 is clearing out of the `docs/work` rows.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths as sdpaths
from ..repos import row_for
from ..writes import upsert_item
from . import Counts, Frozen, MigrationRefused, Record
from .docs_work import _git, _read, default_branch

SOURCE = "register"

WHO = "register migration"

#: Where the register lives, relative to the checkout root.
REGISTER_RELATIVE = Path("research/world-simulator/00-overview/open-questions.md")

#: The header sentence that says which entries are open work rather than
#: decided. Both dash spellings, because the file uses an en dash and a
#: keyboard produces a hyphen.
OPEN_RANGE = re.compile(r"\bO(\d+)\s*[–—-]\s*O(\d+)\s+are\s+open\s+work\b")

#: One entry's heading: `**O27 — the title.**`, bold, at the start of a line.
#: The title may wrap: the register hard-wraps its prose at eighty columns and
#: two of the three open entries carry a heading that spans two lines. A
#: line-anchored `.` would have found O27 and silently missed O28 and O29 --
#: measured against the real register on 2026-09-06, which is why the missing
#: entry is a refusal rather than a shorter list.
ENTRY = re.compile(r"^\*\*(O\d+)\s*[–—-]\s*(.+?)\*\*", re.MULTILINE | re.DOTALL)

#: The header block, up to the first blank line after `Status:`.
STATUS_LINE = re.compile(r"^Status:\s*(.+?)(?=\n\n)", re.MULTILINE | re.DOTALL)


def register_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    named = env.get("SD_REGISTER")
    if named:
        return Path(os.path.expanduser(named))
    root = Path(os.path.expanduser(env.get("SD_REPO_ROOT") or "~/repos"))
    return root / REGISTER_RELATIVE


def open_ids(text: str) -> tuple[list[str], str]:
    """`(the open ids, the header sentence they came from)`.

    The sentence is returned as well as parsed, because it is what the rows
    carry: the reader of a row should be able to see the words the migration
    read rather than trust that it read them right.
    """
    header = STATUS_LINE.search(text)
    if header is None:
        raise MigrationRefused(
            "the register has no `Status:` header ending in a blank line; that "
            "sentence is what says which entries are open work, and without it "
            "this migration would import every decision as open work"
        )
    sentence = " ".join(header.group(1).split())
    found = OPEN_RANGE.search(sentence)
    if found is None:
        raise MigrationRefused(
            f"the register's Status header does not name an open range: "
            f"{sentence!r}. Expected something like `O27-O29 are open work`."
        )
    first, last = int(found.group(1)), int(found.group(2))
    if last < first:
        raise MigrationRefused(
            f"the register's open range runs backwards: O{first} to O{last}"
        )
    return [f"O{number}" for number in range(first, last + 1)], sentence


@dataclass
class Reader:
    """The register migration. Constructed with the file and its repository."""

    path: Path
    repo: str
    name: str = SOURCE
    notes: list[str] = field(default_factory=list)

    @classmethod
    def at(
        cls,
        path: Path | str | None = None,
        *,
        repo: str | None = None,
        environ: dict[str, str] | None = None,
    ) -> "Reader":
        target = Path(path) if path is not None else register_path(environ)
        # The repository is the checkout the register sits in: two directories
        # up from `00-overview/open-questions.md`.
        owner = repo if repo is not None else str(target.resolve().parents[1])
        return cls(path=target, repo=owner)

    # ------------------------------------------------------------ freeze

    def relative(self) -> str:
        """The register's path inside its repository, as `git show` wants it."""
        root = sdpaths.expand(self.repo).resolve()
        try:
            return Path(self.path).resolve().relative_to(root).as_posix()
        except ValueError:
            raise MigrationRefused(
                f"{self.path} is not inside {self.repo}, so there is no "
                f"committed tree to read it from"
            ) from None

    def source(self) -> tuple[str, str]:
        """`(ref, commit)`: the branch this reads and the commit it resolves to.

        The default remote branch, resolved exactly as the `docs/work`
        migration resolves it -- `origin/HEAD`'s target, else `origin/main`
        -- so the two migrations cannot disagree about which branch a merge
        lands on. A checkout with no `origin` at all has no remote to defer
        to and reads its own `HEAD`, still the committed tree and never the
        working copy; the note the sitting prints names whichever it was.
        """
        root = sdpaths.expand(self.repo)
        if not (root / ".git").exists():
            raise MigrationRefused(
                f"{root} is not a git checkout, and the register is read from "
                f"a committed tree; restore the checkout"
            )
        ref = default_branch(root)
        remote = "origin" in _read(root, "remote").splitlines()
        if not remote:
            ref = "HEAD"
        code, out, err = _git(
            root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"
        )
        if code != 0:
            # With no `origin` there is nothing to fetch; the missing thing is
            # a commit, and a remedy naming a remote the checkout lacks is one
            # the operator cannot run.
            remedy = (
                "Run `git fetch origin` in that checkout and retry; the reader "
                "did not fetch."
                if remote
                else "It has no `origin`, so commit the register in that "
                "checkout and retry."
            )
            raise MigrationRefused(
                f"{root} has no {ref} to read the register from: "
                f"{(err or out).strip() or 'the ref does not resolve'}. {remedy}"
            )
        return ref, out.strip()

    def freeze(self) -> Frozen:
        relative = self.relative()
        ref, commit = self.source()
        code, text, err = _git(sdpaths.expand(self.repo), "show", f"{ref}:{relative}")
        if code != 0:
            reason = err.strip().splitlines()[0] if err.strip() else "git show found nothing"
            raise MigrationRefused(
                f"no register at {ref}:{relative} in {self.repo} "
                f"({commit[:12]}); it lives in another repository, and an "
                f"absent one would import as zero entries rather than as an "
                f"error. The working copy is not consulted: {reason}"
            )
        wanted, sentence = open_ids(text)
        titles = {
            match.group(1): " ".join(match.group(2).split()).rstrip(".")
            for match in ENTRY.finditer(text)
        }
        missing = [identifier for identifier in wanted if identifier not in titles]
        if missing:
            raise MigrationRefused(
                f"the register's header names {', '.join(missing)} as open work "
                f"but the file has no `**{missing[0]} — ...**` entry for them"
            )
        records = {
            identifier: Record(
                identity=identifier,
                payload={
                    "title": titles[identifier],
                    # The key, so the verify compares like with like and a
                    # second machine lands the same row (sd:1439).
                    "repo": sdpaths.key(self.repo),
                    # Compared too: `land` writes it, and a column the verify
                    # does not read is one it cannot find wrong (sd:1226).
                    "path": relative,
                    "status": "planning",
                    "register_status": sentence,
                    "source_commit": commit,
                },
            )
            for identifier in wanted
        }
        return Frozen(
            source=self.name,
            records=records,
            commits={identifier: commit for identifier in wanted},
            notes=[
                f"{SOURCE}: read {relative} from {ref} at {commit[:12]}",
                f"{SOURCE}: {len(records)} open entr(ies) per the register's own "
                f"header: {', '.join(wanted)}",
            ],
        )

    # ------------------------------------------------------------ import

    def land(self, connection: sqlite3.Connection, frozen: Frozen) -> Counts:
        if row_for(connection, self.repo) is None:
            raise MigrationRefused(
                f"{self.repo} is not in the `repo` table, so its register's rows "
                f"cannot reference it. Run `sd-db.sh repo add {self.repo}` first; "
                f"criterion 6 enumerates `docs/work` from the same table."
            )
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
                repo=payload["repo"],
                # Relative to the repository, as the `docs/work` rows are, so
                # `git -C repo show source_commit:path` reads the register the
                # row came from. An absolute path was landed until 2026-09-11
                # and is one `git show` cannot follow.
                path=payload["path"],
                # `branch` is deliberately not written. `item.branch` is the
                # branch the runner does the work on, and `origin/main` is a
                # remote-tracking name and not one -- the defect sd:462 is
                # clearing out of the docs/work rows. The ref this was read
                # from is in the sitting's note; the commit is the column.
                source_commit=payload["source_commit"],
                fields={"register_status": payload["register_status"]},
            )
            counts.record(what)
        return counts

    # ------------------------------------------------------------ verify

    def rows(self, connection: sqlite3.Connection) -> dict[str, Record]:
        held: dict[str, Record] = {}
        for row in connection.execute(
            "SELECT * FROM item WHERE source = ? ORDER BY external_id", (SOURCE,)
        ):
            fields = json.loads(row["fields"] or "{}")
            held[row["external_id"]] = Record(
                identity=row["external_id"],
                payload={
                    "title": row["title"],
                    "repo": row["repo"],
                    "path": row["path"],
                    "status": row["status"],
                    "register_status": fields.get("register_status"),
                    "source_commit": row["source_commit"],
                },
            )
        return held
