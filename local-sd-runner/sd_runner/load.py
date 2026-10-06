"""The runner's load limit, with no sd_db import (sd:2812).

`restart` refuses a 1-minute load at or above `max_load` (default: the core
count), because a cold start under load stalled on diskutil (sd:1950).
`deploy.sh upgrade` asks the same question before it replaces sd_db, so this
module imports nothing from it: run it as a script under the runner's
interpreter, and it exits 1 with the refusal as JSON.
"""

import json
import os
import sys


def refusal(max_load=None):
    """The refusal for the current load, or None when the load is below the limit."""
    limit = float(max_load if max_load is not None else os.cpu_count() or 1)
    load = os.getloadavg()[0]
    if load < limit:
        return None
    return {"ok": False, "reason": f"the 1-minute load average {load:.1f} is at or above {limit:g}; "
            "a cold start under load can stall on diskutil, so restart when the machine is quieter",
            "load": load, "max_load": limit}


if __name__ == "__main__":
    found = refusal()
    print(json.dumps(found or {"ok": True}))
    sys.exit(1 if found else 0)
