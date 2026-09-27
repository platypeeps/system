"""Database ownership evidence for finite retained-clone maintenance."""

from __future__ import annotations

from . import runner

#: The name `runner.sh prune-apply` renames a retained clone to, beside it,
#: before it removes a byte (sd:770). A directory by this name is a removal
#: that stopped part way: the prune finishes it, and a restore or a repo or
#: item remove refuses it rather than reading it as a clone.
PRUNING = ".pruning-clone"


def evidence(connection, ident: str) -> dict:
    run = runner.run_state(connection, ident)
    assignment = runner.queue_state(connection, run["assignment"])
    leases = [dict(row) for row in connection.execute(
        "SELECT * FROM runner_lease WHERE repo=? AND released_at IS NULL ORDER BY run", (run["repo"],))]
    return {"run": run, "assignment_revision": assignment["revision"], "leases": leases}


def candidates(connection) -> list[dict]:
    return [evidence(connection, row[0]) for row in connection.execute(
        "SELECT id FROM runner_run WHERE released_at IS NOT NULL ORDER BY released_at,id")]
