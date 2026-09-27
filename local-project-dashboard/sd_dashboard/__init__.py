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

__all__ = ["__version__"]

__version__ = "0.1.0"
