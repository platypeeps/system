"""Where a finished call is written down, and why it can never cost a caller.

`jev.py` is standalone: stdlib only, no package to install, runnable from a
cron `JOB_COMMAND` on a machine that has nothing else. This module keeps that
true. It imports `sd_db` **inside the call**, catches everything, and returns
a word saying what it did. There is no path through it that raises, and no
path through it that runs before the answer has been printed.

That is the whole contract, and it is the one the item states: a missing,
unmigrated or unwritable database degrades to no recording, never to a failed
or slowed judgment. Six things go wrong here in practice -- `sd_db` is not
installed, the file does not exist, the schema is older than the library, the
disk is read-only, the row is refused, the table is locked -- and every one of
them is the same answer to the caller, which is nothing.

**Switching it off.** `JEV_METER=0` (or `off`, `false`, `no`, `disabled`)
records nothing. Unset means on, the same default the kill switch uses and for
the same reason: a switch that defaults to off makes every integration added
after it silently never run.

**Which database.** `JEV_METER_DB` names one; without it, `sd_db`'s own
default. The variable exists for the suite, which must not write to the real
store, and for a machine whose store is somewhere else.

**A busy ledger loses the row rather than the caller's time.** `sd_db` waits
five seconds for a lock, which is right for a write someone is waiting on and
wrong for every write made here: a subprocess caller waits for this process to
exit, so five seconds of contention is five seconds added to a judgment that
already finished. The connection is opened with `busy_timeout=0`, so a locked
table fails at once and the measurement is dropped. A measurement is worth
less than the thing it measures, and this is the one place in the stack where
that trade is already decided.
"""

from __future__ import annotations

import os
from sqlite3 import OperationalError

#: The words the kill switch accepts, so a reader who knows one knows both.
OFF = ("0", "off", "false", "no", "disabled")

#: What `record` returns. Only `written` means a row exists.
WRITTEN = "written"
SWITCHED_OFF = "switched off"
NO_LIBRARY = "sd_db is not installed here"
NO_STORE = "no database to record in"
REFUSED = "the ledger refused the row"
CONTENDED = "the ledger was busy; the measurement was dropped"


def switched_on(env) -> bool:
    return (env.get("JEV_METER") or "").strip().lower() not in OFF


def record(event: dict, env=None) -> str:
    """Write one `judgment` row for a finished call. Never raises.

    `event` is the provider-neutral shape `jev.py` builds: the keyword
    arguments of `sd_db.judgment.record`, and nothing else. The return value
    is for the suite and for a human reading a trace; no caller branches on it,
    because no caller may care whether the recording worked.
    """
    env = os.environ if env is None else env
    if not switched_on(env):
        return SWITCHED_OFF
    try:
        from sd_db.database import connect
        from sd_db.judgment import record as write_row
    except Exception:                        # pragma: no cover - import shape
        # Bare `Exception` on purpose, twice in this function. The promise is
        # that metering cannot break a caller, and a promise that holds for
        # the failures someone listed is not that promise. An `ImportError` is
        # the expected one; a half-installed package raising something else
        # must land in the same place.
        return NO_LIBRARY
    path = (env.get("JEV_METER_DB") or "").strip() or None
    try:
        # Zero, not the library's five seconds. See the module docstring: a
        # lock here is time added to a judgment that has already been printed.
        connection = connect(path, busy_timeout=0)
    except OperationalError:
        return CONTENDED
    except Exception:
        return NO_STORE
    try:
        write_row(connection, **event)
    except OperationalError:
        # `database is locked` from a zero timeout, which is the expected
        # answer under contention rather than a fault, and is worth its own
        # word so a trace does not read it as a refused row.
        return CONTENDED
    except Exception:
        return REFUSED
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return WRITTEN
