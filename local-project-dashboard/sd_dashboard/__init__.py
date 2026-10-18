"""The dashboard: the front door, rebuilt on the one database.

Three screens land here read-only -- Today, Backlog and Item -- and every row
on them comes through `sd_db`. Nothing in this package opens a database of its
own; the only `sqlite3` connection opened anywhere is `sd_db.database`'s, which is
requirement 2 and criterion 2's grep.

There is no frontend build step and there will not be one: no `package.json`,
no `node_modules`, no bundler config under this folder. One stylesheet and one
script, both hand-written, both served from `static/`. The `pyproject.toml`
beside this package builds a wheel of the Python, which is a different thing:
it adds no asset pipeline and changes nothing about how `static/` is served.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

__all__ = ["PACK_MODULES", "__version__", "pack_bin", "pack_library", "pack_module"]

__version__ = "0.1.0"

#: The pack `bin/` modules this process imports: `sd_lane`, for the Queue's read, and what it imports in turn; `sd_research_publish`,
#: for the mirror queue and the research.conf.py trust record (sd:5102).
#: `runtime.build_digests` hashes each, so a pack update to one restarts the server like a library update.
PACK_MODULES = ("sd_lane", "sd_lib", "sd_ship_workflow", "sd_research_publish")


def pack_library():
    """The command pack checkout's `lib/`, which holds `sd_db` (sd:2997).

    `SD_PACK_ROOT` names the checkout, as for `sd-db`; unset, it is the
    home clone. `bootstrap.py` and the restart probe put this folder first on
    `sys.path`, so no copy installed in the interpreter answers in its place.
    Here and not in `runtime`, so the bootstrap reads it before it runs that
    module as `__main__`.
    """
    root = os.environ.get("SD_PACK_ROOT") or Path.home() / "repos/platypeeps/sd-ai-command-pack"
    return Path(root).expanduser().resolve() / "lib"


def pack_bin(library_root=None) -> Path:
    """The pack checkout's `bin/`, beside the `lib/` whose `sd_db` this process loaded, so the two match."""
    if library_root is None:
        import sd_db

        library_root = Path(sd_db.__file__).resolve().parent
    return Path(library_root).parent.parent / "bin"


def pack_module(name: str):
    """One of `PACK_MODULES`, imported from `pack_bin()` (sd:TBD): the read a pack command makes, without its process.

    The folder goes last on `sys.path`, so it shadows neither this package nor `sd_db`. `ImportError` when the folder
    or the module is missing, as in an installed copy outside a checkout; the caller runs the command instead.
    """
    if name not in PACK_MODULES:
        raise ImportError(f"{name} is not one of the pack modules the build digest covers")
    folder = str(pack_bin())
    if folder not in sys.path:
        sys.path.append(folder)
    return importlib.import_module(name)
