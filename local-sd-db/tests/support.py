"""Fixtures the migration tests build on: a git repository and a vault.

`sd_db.testing` is the harness both repositories import and it doubles the
*outside world* -- GitHub, launchd, a home directory. These are neither: they
are small sources this repository's own migrations read, built here because
nothing outside this suite has a use for them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PRD = """\
---
title: {title}
status: {status}
created: {created}
---

# PRD — {title}

## Problem

{body}
"""


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


def repository(root: Path, *, bare: Path | None = None) -> Path:
    """A git checkout with an identity, and an origin when one is asked for."""
    root.mkdir(parents=True, exist_ok=True)
    git(root.parent, "init", "-q", "-b", "main", root.name)
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-qm", "first")
    if bare is not None:
        subprocess.run(
            ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True
        )
        git(root, "remote", "add", "origin", str(bare))
        git(root, "push", "-q", "-u", "origin", "main")
    return root


def write_item(
    root: Path,
    slug: str,
    *,
    title: str = "an item",
    status: str = "planning",
    created: str = "2026-07-01",
    body: str = "the problem.",
) -> Path:
    path = root / "docs" / "work" / slug / "prd.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        PRD.format(title=title, status=status, created=created, body=body),
        encoding="utf-8",
    )
    return path


def write_archived_item(
    root: Path, month: str, slug: str, *, status: str = "done", **kwargs
) -> Path:
    """A prd two levels deeper, which is closed history and stays that way.

    `docs/work/archive/<month>/<slug>/prd.md`. The one-star glob the
    migration enumerates with does not reach it, and neither does the retire:
    an archived `status:` line is a record of what an item's status *was*.
    """
    path = root / "docs" / "work" / "archive" / month / slug / "prd.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = {"title": "an archived item", "created": "2026-05-01",
              "body": "the problem.", **kwargs, "status": status}
    path.write_text(PRD.format(**fields), encoding="utf-8")
    return path


#: Both lines matter, and the `__future__` one is the subtle half. A module
#: executed with no `sys.modules` entry cannot define a dataclass whose field
#: annotations are *strings*: `dataclasses._is_type` reads
#: `sys.modules.get(cls.__module__).__dict__` unguarded, and it is reached
#: only when an annotation needs resolving against the module. `sd_lib.py`
#: has `from __future__ import annotations`, so every annotation is a string.
#: A stub with a dataclass but no `__future__` import passes under a probe
#: that the real file kills -- this fixture was that stub, and the mutation
#: that removes the registration escaped until the import was added.
PACK_LIBRARY = '''\
"""A stand-in for the pack's `bin/sd_lib.py`, carrying both names checked."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Shaped:
    """As `sd_lib.py:254` is. The probe must survive executing this."""

    name: str = ""


def status_marker(root, work_dir="docs/work"):
    return "file", ""


def delivered(root, item):
    return "yes"
'''

#: A pack from `eb7695c7`, which is a real version of the real pack: it
#: answers `delivered` and knows nothing of the marker or the rows. It is the
#: shape a `delivered`-only version check waves through.
PACK_BEFORE_THE_ROW_READERS = '''\
"""The pack as of `eb7695c7`: `delivered` landed, the row readers had not."""


def delivered(root, item):
    return "yes"
'''


def pack(
    home: Path,
    *,
    checkout: Path | None = None,
    commit: str = "47d41245a4701dea7a1e3623978c4af2d065c03f",
    library: str | None = PACK_LIBRARY,
    receipt: bool = True,
) -> Path:
    """An installed pack, as the pack's own installer leaves one.

    The receipt under the state home naming a checkout and a commit, and that
    checkout carrying `bin/sd_lib.py`. Built this way round because the
    retire's version check is about what is *installed*: a checkout sitting
    on disk that no receipt names is a checkout, not an installation.

    `library=None` leaves the checkout with no `bin/sd_lib.py`;
    `receipt=False` leaves the machine with no installed pack at all.
    """
    target = Path(checkout) if checkout else home / "pack"
    if library is not None:
        (target / "bin").mkdir(parents=True, exist_ok=True)
        (target / "bin" / "sd_lib.py").write_text(library, encoding="utf-8")
    path = home / ".local/state/sd-ai-command-pack/installed.json"
    if receipt:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "checkout": str(target),
                    "commit": commit,
                    "branch": "main",
                    "dirty": False,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return target


def commit(root: Path, message: str = "an item") -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def push(root: Path, branch: str = "main") -> None:
    git(root, "push", "-q", "origin", branch)


NOTE = """\
---
contexts:
  - Personal
content-type: {kind}
status: {stage}
dateCreated: {created}
description: "{description}"
tags:
  - {kind}
---

# {title}

{body}
"""


def vault(root: Path, *, ideas: dict[str, str], topics: dict[str, str]) -> Path:
    """A vault with the two bases requirement 2 migrates. `{name: stage}`."""
    for base, kind, entries in (
        ("System/Databases/Blog Ideas", "blog-idea", ideas),
        ("System/Databases/Topics", "topic", topics),
    ):
        directory = root / base
        directory.mkdir(parents=True, exist_ok=True)
        for name, stage in entries.items():
            (directory / f"{name}.md").write_text(
                NOTE.format(
                    kind=kind,
                    stage=stage,
                    created="2026-06-15",
                    description=f"{name}, at {stage}",
                    title=name,
                    body=f"the {kind} body of {name}.",
                ),
                encoding="utf-8",
            )
    return root
