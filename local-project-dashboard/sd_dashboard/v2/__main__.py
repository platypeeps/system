"""`python3 -m sd_dashboard.v2`: the rail and palette map, which `dashboard.sh pages` prints (sd:2418)."""

from __future__ import annotations

from . import listing

for line in listing():
    print(line)
