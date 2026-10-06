"""Keep the real `jev` out of the operator's ledger, corpus and collector (sd:2799).

`health-check.sh check` calls the checkout's `local-jev/jev.sh`, and its
`enabled --record` writes a ledger row even with the stage switched off.
`JEV_TRACES_URL` is pinned to an off word, not dropped: `jev.sh` lets
`<config>/jev/.env` fill an unset or empty one.
"""

import os
import unittest.mock

PINNED = {"JEV_METER": "0", "JEV_CORPUS": "0", "JEV_TRACES_URL": "0"}
DROPPED = ("JEV_METER_DB", "JEV_CORPUS_DIR")


def isolate():
    """Pin the environment for one module; returns the undo."""
    patcher = unittest.mock.patch.dict(os.environ, PINNED)
    patcher.start()
    for name in DROPPED:
        os.environ.pop(name, None)
    return patcher.stop
