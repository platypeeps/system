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

import os
from pathlib import Path

__all__ = ["__version__", "pack_library"]

__version__ = "0.1.0"


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
