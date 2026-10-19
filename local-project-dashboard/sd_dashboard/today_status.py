"""Today's Status view (sd:5099): the sections `sd today` and `sd-status` print, read by the pack's `sd_db.today`.

The pack owns the sections (pack sd:5098): `sd_db.today.sections` reads each one, in `ORDER`, with the summary counts,
and `sd today` renders the same dict as text. This module hands that dict to the page unchanged, so the view and the
command cannot drift; it adds only `now`, the read time the page measures each row against.

The gate slots are files on this machine, not rows: `sd gate status --json` reads them, as `sd today` does. A read
that failed is its reason, which `sections` keeps as the gate's `error`, so the page says unknown, never an idle gate.
"""

from __future__ import annotations

from typing import Any, Callable


def _gate() -> Any:
    """`sd gate status --json`, or why it could not be read."""
    from .start_next import Unread, gate_status

    try:
        return gate_status()
    except Unread as error:
        return str(error)


def document(connection, *, now: str, gate: Callable[[], Any] | None = None) -> dict:
    """`sd_db.today.sections` as of `now`, with `now`; `gate` reads the slots, `_gate` unless a test gives one."""
    from sd_db import today

    return {"now": now, **today.sections(connection, now=now, gate=(gate or _gate)())}
