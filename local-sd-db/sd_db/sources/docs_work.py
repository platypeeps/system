"""What is left of the `docs/work` source: its row key and its git helpers.

The importer that read `docs/work/*/prd.md` into `item` rows, and the retire
that handed status to the rows, are gone (sd:3231): the pack reads every
item's status from its row (sd:3015), and `sd work register` makes the row
for a new folder. `SOURCE` is the `item.source` those rows carry. `_git`,
`_read` and `default_branch` serve `register`, `recovery`, `sd work register`
and the pack's `sd_work.REGISTER_NEEDS`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import MigrationRefused

#: `source` on every `docs/work` row. The pair `(source, external_id)` is the
#: schema's unique index, which is what makes a second registration an update
#: rather than a second row.
SOURCE = "docs/work"

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
