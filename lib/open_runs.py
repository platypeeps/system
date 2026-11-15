"""Print the open pack runs that would write bulk storage, one per line.

Run with the pack's Python (`SD_DB_PYTHON`): `open_runs.py <pack root>`.
Prints the open build and draft runs, and the lane entries this host
runs; prints nothing when idle and fails when the database cannot be
read. machine-setup's bulkmove stage and `maintenance.sh move` both
check it before and after a move (sd:8242, sd:8797).
"""

import json
import sys

sys.path.insert(0, sys.argv[1] + "/lib")
import sd_db  # noqa: E402
from sd_db import build, draft  # noqa: E402
from sd_db.ship import this_host  # noqa: E402

c = sd_db.connect(write=False)
hosts = build.lane_hosts(c)
for r in build.open_runs(c, hosts) + draft.open_runs(c, hosts):
    print("run", r["id"])
here = this_host()
for row in c.execute("SELECT body FROM state WHERE id IN (SELECT MAX(id) FROM state"
                     " WHERE kind = ? AND key LIKE ? GROUP BY key)", ("checkpoint", "lane:v1:%")):
    e = json.loads(row["body"])
    if e.get("status") == "running" and (e.get("holder") or {}).get("host") == here:
        print("lane", e.get("item"), e.get("id"))
