"""A checkout fleet in a temp directory, for the Repos and Sessions areas.

Real `git init` checkouts under a root of their own, never the operator's
`~/repos`: the child `fleet.py` reads `REPO_ROOT` from its environment, so
the fixture patches that variable for the page render. A command the fleet
runs -- `git`, `ps` -- can be replaced by a shim placed first on `PATH`,
which the child inherits too; that is how a checkout that hangs is staged.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from support import ScreenCase
from stubs import executable

GIT = "/usr/bin/git"


class FleetCase(ScreenCase):
    """`ScreenCase`, plus a checkout root and the environment that names it."""

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.tmp.name) / "repos"
        self.root.mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        environment = patch.dict(os.environ, {
            "REPO_ROOT": str(self.root),
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
        })
        environment.start()
        self.addCleanup(environment.stop)

    # -- checkouts -----------------------------------------------------------

    def git(self, path: Path, *argv: str, when: str | None = None) -> str:
        """Real git, by absolute path, so a `git` shim on `PATH` never reaches the fixture."""
        environment = dict(os.environ)
        if when:
            environment.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        done = subprocess.run([GIT, "-C", str(path), *argv], capture_output=True, text=True,
                              check=True, timeout=30, env=environment)
        return done.stdout.strip()

    def configure(self, path: Path) -> None:
        self.git(path, "config", "user.email", "fleet@example.invalid")
        self.git(path, "config", "user.name", "Fleet Fixture")
        self.git(path, "config", "commit.gpgsign", "false")
        # No auto maintenance: git 2.54's prunes the registration `abandoned`
        # writes, and run detached after a commit it raced the read (sd:1272).
        self.git(path, "config", "maintenance.auto", "false")

    def checkout(self, name: str, group: str | None = None, *, branch: str = "main",
                 when: str = "2026-09-01T10:00:00+00:00") -> Path:
        """A checkout with one commit on `branch` at `when`; grouped one level deep when asked.

        The commit time is fixed so the newest-first order the page keeps is
        the fixture's and not the clock's.
        """
        path = self.root / group / name if group else self.root / name
        path.mkdir(parents=True)
        self.git(path, "init", "-q", "-b", branch)
        self.configure(path)
        (path / "README").write_text(f"{name}\n")
        self.git(path, "add", "README")
        self.git(path, "commit", "-q", "-m", f"first commit in {name}", when=when)
        return path

    def worktree(self, checkout: Path, name: str, branch: str) -> Path:
        """A live worktree, registered by git itself."""
        where = Path(self.tmp.name) / "worktrees" / name
        where.parent.mkdir(exist_ok=True)
        self.git(checkout, "worktree", "add", "-q", "-b", branch, str(where))
        return where

    def abandoned(self, checkout: Path, name: str, branch: str) -> Path:
        """A registration whose directory is gone: what a cleared scratchpad leaves."""
        where = Path(self.tmp.name) / "gone" / name
        entry = checkout / ".git" / "worktrees" / name
        entry.mkdir(parents=True)
        (entry / "gitdir").write_text(f"{where / '.git'}\n")
        (entry / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        return where

    # -- shims -----------------------------------------------------------------

    def shim(self, name: str, body: str) -> Path:
        """A `name` on `PATH` ahead of the real one. `body` is the shell after `#!/bin/sh`."""
        script = self.bin / name
        executable(script, f"#!/bin/sh\n{body}\n")
        return script

    def hanging(self, name: str, *, when: str = "", quiet: bool = False) -> Path:
        """A `name` that records its pid and sleeps forever, for the arguments `when` matches.

        Every other invocation is handed to the real command, so a fixture can
        hang one checkout's git and leave the rest answering. `quiet` closes
        stdout before the sleep: the shape of a command that has said all it
        will say and then does not exit.
        """
        pids = Path(self.tmp.name) / f"{name}.pids"
        real = "/bin/ps" if name == "ps" else GIT
        condition = f'case "$*" in {when}) ' if when else ""
        close = "exec 1>&-; " if quiet else ""
        tail = ";; *) exec " + real + ' "$@" ;; esac' if when else ""
        return self.shim(name, f'{condition}echo $$ >> "{pids}"; {close}exec sleep 60{tail}')

    def assertGone(self, name: str) -> None:
        """No process a hanging shim recorded is still alive."""
        pids = Path(self.tmp.name) / f"{name}.pids"
        self.assertTrue(pids.exists(), f"the {name} shim never ran")
        deadline = time.monotonic() + 2
        for pid in [int(line) for line in pids.read_text().split()]:
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() > deadline:
                    self.fail(f"process {pid} outlived the refused collection")
                time.sleep(0.01)
