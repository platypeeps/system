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

**A busy ledger costs the caller a quarter of a second at most.** `sd_db`
waits five seconds for a lock, which is right for a write someone is waiting on
and wrong for every write made here: a subprocess caller waits for this process
to exit, so five seconds of contention is five seconds added to a judgment that
already finished. The connection waits `BUSY_MS`, then fails and drops the
measurement. It waited zero at first, and that dropped rows under the ordinary
contention of a busy machine (sd:2087); 250 ms kept all of the first 112 live
rows. `JEV_METER_BUSY_MS` sets another bound, and 0 restores the old one.
"""

from __future__ import annotations

import os
from sqlite3 import OperationalError

#: The words the kill switch accepts, so a reader who knows one knows both.
OFF = ("0", "off", "false", "no", "disabled")

#: How long a write waits for a lock, in milliseconds, as `sd_db` counts it.
BUSY_MS = 250

#: What `record` returns. Only `written` means a row exists.
WRITTEN = "written"
SWITCHED_OFF = "switched off"
NO_LIBRARY = "sd_db is not installed here"
NO_STORE = "no database to record in"
REFUSED = "the ledger refused the row"
CONTENDED = "the ledger was busy; the measurement was dropped"


def switched_on(env) -> bool:
    return (env.get("JEV_METER") or "").strip().lower() not in OFF


#: What `ready` returns when a row for that arm would be accepted.
READY = "ready"
NO_ARM = "this sd_db does not know the arm"


def ready(arm: str, env=None) -> str:
    """Whether a row for `arm` has somewhere to go, asked before it costs
    anything. Never raises.

    A comparison arm makes a call that may be billed and exists only to write
    its row, so the ledger is checked first: the meter is on, `sd_db` is
    installed and names the arm, and the database opens for writing at the
    library's schema. `connect` refuses an older or newer schema, which is
    the window between a library upgrade and its migration.
    """
    env = os.environ if env is None else env
    if not switched_on(env):
        return SWITCHED_OFF
    try:
        from sd_db.database import connect
        from sd_db.judgment import ARMS
    except Exception:
        return NO_LIBRARY
    if arm not in ARMS:
        return NO_ARM
    path = (env.get("JEV_METER_DB") or "").strip() or None
    try:
        connection = connect(path, busy_timeout=busy_ms(env))
    except OperationalError:
        return CONTENDED
    except Exception:
        return NO_STORE
    try:
        connection.close()
    except Exception:
        pass
    return READY


def busy_ms(env) -> int:
    """`BUSY_MS`, or what `JEV_METER_BUSY_MS` says; a value that does not
    parse is the default, because a typo here may not cost the caller."""
    try:
        return max(0, int((env.get("JEV_METER_BUSY_MS") or "").strip() or BUSY_MS))
    except ValueError:
        return BUSY_MS


def accepted(write_row, event: dict) -> dict:
    """The event without the fields this `sd_db` does not take.

    `jev` and the library are installed separately, and the library `jev`
    reads is often the command pack's copy. A field a newer `jev` measures
    (`server_ms`, `probabilities`) would make an older library refuse the
    whole row over a keyword it has never heard of. Dropping the field keeps
    the row, which is the trade every other refusal here makes.
    """
    import inspect
    try:
        known = inspect.signature(write_row).parameters
    except (TypeError, ValueError):          # pragma: no cover - a C callable
        return event
    if any(p.kind is p.VAR_KEYWORD for p in known.values()):
        return event
    return {key: value for key, value in event.items() if key in known}


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
        # A bounded wait, not the library's five seconds. See the module
        # docstring: a lock here is time added to a judgment already printed.
        connection = connect(path, busy_timeout=busy_ms(env))
    except OperationalError:
        return CONTENDED
    except Exception:
        return NO_STORE
    try:
        write_row(connection, **accepted(write_row, event))
    except OperationalError:
        # `database is locked` once the wait ran out, which is the expected
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
