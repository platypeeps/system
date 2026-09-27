"""Run the library's suite with every open going over the wire.

    python -m sd_db.testing.wire HOST:PORT TOKEN_FILE [unittest args...]

`sd-db.sh test --remote HOST:PORT TOKEN_FILE` runs this against a server started with
`sd-db.sh serve --loopback`. Every `sd_db.connect` in this process then
returns an `sd_db.remote.Connection` to that server, naming the file the
test built; the server opens it with the library's local open. Criterion 1
of the second-machine plan: the summary line equals the local run's.

In-process only. A test that runs `sd-db.sh` or `python -m sd_db...` in a
child process opens its database locally in that child, because the seam is
a module attribute and not an environment variable (see `database._opener`).
The run prints how many sessions went over the wire so the share is visible.
"""

from __future__ import annotations

import atexit
import sys
from pathlib import Path

#: The one address `sd-db.sh serve --loopback` binds. `localhost` and `::1`
#: are refused: either can reach an address the server does not listen on.
LOOPBACK_HOSTS = ("127.0.0.1",)

_sessions = 0
_local = 0


def install(host: str, port: int, token: str) -> None:
    from .. import database, remote

    if host not in LOOPBACK_HOSTS:
        raise SystemExit(f"sd-db test --remote: {host} is not 127.0.0.1, the only address "
                         f"`serve --loopback` binds")

    def opener(target, *, write, create, busy_timeout):
        global _sessions, _local
        if create:
            # `init` and `migrate`, the only callers of `create=True`, are hub
            # verbs, and the server refuses a session that would create a
            # file (gap (h)). The test that builds a database builds it here.
            _local += 1
            return database.open_local(target, write=write, create=create,
                                       busy_timeout=busy_timeout)
        _sessions += 1
        return remote.connect(host, port, target, token=token, write=write, create=create,
                              busy_timeout=busy_timeout)

    database._opener = opener


def sessions() -> int:
    return _sessions


def main(argv: list[str]) -> int:
    import unittest

    if len(argv) < 2 or ":" not in argv[0]:
        print("usage: sd-db.sh test --remote HOST:PORT TOKEN_FILE [unittest args...]", file=sys.stderr)
        return 1
    from ..serve import read_token

    host, _, port = argv[0].rpartition(":")
    install(host, int(port), read_token(Path(argv[1])))
    atexit.register(lambda: print(f"sd-db test --remote: {_sessions} sessions over the wire, {_local} local creates",
                                  file=sys.stderr))
    here = Path(__file__).resolve().parents[2]
    program = unittest.main(
        module=None,
        argv=["sd_db.testing.wire", "discover", "-s", str(here / "tests"), "-t", str(here), *argv[2:]],
        exit=False,
    )
    return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
