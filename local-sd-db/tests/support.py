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


#: A stand-in for the pack's `bin/sd_lib.py`; nothing reads its contents.
PACK_LIBRARY = '''"""A stand-in for the pack's `bin/sd_lib.py`."""
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
    checkout carrying `bin/sd_lib.py`.

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


#: The writing pack's `workflow` maps, as its manifest declares them (sd:1425).
BLOG_IDEA = {"inbox": "planning", "accepted": "ready", "drafting": "in_progress",
             "published": "done", "declined": "done"}
TOPIC = {"candidate": "planning", "active": "in_progress", "parked": "blocked",
         "retired": "done"}


def plugin_entry(prefix="sdw", root="$OBSIDIAN_VAULT", workflow=None, **extra):
    """One `sd plugin list --json` entry, as the pack's `describe` builds it.

    `workflow=None` declares both writing kinds; `{}` declares none.
    """
    found = {
        "root": f"/plugins/{prefix}",
        "readable": True,
        "prefix": prefix,
        "store": {"driver": "vault", "root": root, "bases": {
            "blog-idea": "System/Databases/Blog Ideas",
            "topic": "System/Databases/Topics",
        }},
        **extra,
    }
    if workflow is None:
        workflow = {"blog-idea": {"status": dict(BLOG_IDEA)}, "topic": {"status": dict(TOPIC)}}
    if workflow:
        found["workflow"] = workflow
    return found


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
