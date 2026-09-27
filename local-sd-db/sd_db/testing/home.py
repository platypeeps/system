"""A home directory the test owns, and the environment that points at it.

Everything the system reads or writes outside a repository is under `$HOME`:
the database and the registry in `~/.local/share/sd/`, the exported
variables in `~/.config/shell/env.sh`. The backups are the exception: their
default root is the USB disk, so `SD_DB_BACKUP_ROOT` points them at
`~/Documents/sd-backups/` in the fixture home. A test that let any of those resolve to the real
home would read the operator's data on a good day and overwrite it on a bad
one, so the harness builds a home and every door points inside it.

`FixtureHome` is also where the pieces are composed. It owns the stubs, and
`environment()` returns one mapping carrying all of it: `HOME`, the
directories, `PATH` with the stub directory first, and whatever the GitHub
shim and the `start` providers need. A caller under test is spawned with that
mapping and reaches nothing real.

`XDG_*` and `TMPDIR` are set as well. They are not read by the system today,
but a library that grows a cache would land in the real home without them,
and the failure would be silent.
"""

from __future__ import annotations

import os
from pathlib import Path

from .stubs import STUB_NAMES, Stubs

#: The directories the system expects to exist under `$HOME`, relative to it.
#: Enumerated rather than created on demand: a test asserting that a command
#: created one of them needs it absent, and one asserting a command wrote
#: into it needs it present. This is the second list.
HOME_DIRS = (
    ".local/share/sd",
    ".local/state/sd",
    ".config/shell",
    "Documents/sd-backups",
    "bin",
)

ENV_SH = """\
# A fixture `env.sh`. Written by `sd_db.testing.home`.
#
# The real file exports the operator's tokens. This one exports the fixture's,
# so a caller that sources it reaches the doubles and not a vendor.
export SD_HOME="$HOME"
export SD_STATE="$HOME/.local/share/sd"
"""


class FixtureHome:
    """A `$HOME` with the system's directories in it, plus the stubs.

    `path` is the home. `environment()` is what a spawned process gets.
    Additional pieces -- the `gh` shim, the `start` providers -- are merged
    in by passing their environments to `merge()`, so the composition stays
    the test's decision and this class stays a directory plus a mapping.
    """

    def __init__(self, root: Path, *, stub_names: tuple[str, ...] = STUB_NAMES) -> None:
        self.root = Path(root)
        self.path = self.root / "home"
        self.path.mkdir(parents=True, exist_ok=True)
        for relative in HOME_DIRS:
            (self.path / relative).mkdir(parents=True, exist_ok=True)
        self.env_sh = self.path / ".config/shell/env.sh"
        self.env_sh.write_text(ENV_SH, encoding="utf-8")
        self.tmp = self.root / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.stubs = Stubs(self.root / "stubs", names=stub_names)
        self._extra: dict[str, str] = {}
        self._prefixes: list[str] = [str(self.stubs.bin)]

    # ------------------------------------------------------------- places

    @property
    def state(self) -> Path:
        """`~/.local/share/sd/` -- the database and the registry."""
        return self.path / ".local/share/sd"

    @property
    def backups(self) -> Path:
        """`~/Documents/sd-backups/` -- the backup root, via `SD_DB_BACKUP_ROOT`."""
        return self.path / "Documents/sd-backups"

    @property
    def registry(self) -> Path:
        """`~/.local/share/sd/providers.yaml`."""
        return self.state / "providers.yaml"

    @property
    def bin(self) -> Path:
        """`~/bin` -- where a `start` provider or a shim is written."""
        return self.path / "bin"

    # -------------------------------------------------------- environment

    def merge(self, *environments: dict[str, str]) -> FixtureHome:
        """Fold another door's environment in. `PATH` entries accumulate.

        Every piece of the harness returns a `PATH` with its own directory
        prepended, so a plain `update()` would keep the last one and lose the
        rest. Here the prefixes are collected and the base `PATH` is taken
        from `environment()`.
        """
        for environment in environments:
            for key, value in environment.items():
                if key == "PATH":
                    head = value.split(os.pathsep)[0]
                    if head and head not in self._prefixes:
                        self._prefixes.append(head)
                    continue
                self._extra[key] = value
        return self

    def environment(self, base: os._Environ | dict | None = None) -> dict[str, str]:
        """One mapping. Nothing in it resolves outside this directory."""
        source = dict(base if base is not None else os.environ)
        # A caller's PATH is kept, because the stubs replace six commands and
        # not the shell: `git`, `python3` and `sh` still have to resolve.
        inherited = source.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        environment = {
            **source,
            **self._extra,
            "HOME": str(self.path),
            "TMPDIR": str(self.tmp),
            "XDG_DATA_HOME": str(self.path / ".local/share"),
            "XDG_STATE_HOME": str(self.path / ".local/state"),
            "XDG_CONFIG_HOME": str(self.path / ".config"),
            "XDG_CACHE_HOME": str(self.path / ".cache"),
            "SD_FIXTURE_STUBS": str(self.stubs.data),
            "SD_DB_BACKUP_ROOT": str(self.backups),
            "PATH": os.pathsep.join([*self._prefixes, inherited]),
        }
        return environment
