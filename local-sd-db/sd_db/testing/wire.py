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

A test marked `hub_only` runs with its connections local (step 6, ruling Q2
= A). It exercises a path the proxy refuses by design: a file lock or a
directory beside the database. Over the wire it would meet `HubOnly`, and
criterion 1 needs the same count with zero skips. The run prints how many
ran local. `tests/test_hub_only.py` runs each marked test over the wire and
fails one that meets no hub-only refusal, so a mark cannot hide a path the
proxy should carry.
"""

from __future__ import annotations

import atexit
import sys
import unittest
from pathlib import Path

#: The one address `sd-db.sh serve --loopback` binds. `localhost` and `::1`
#: are refused: either can reach an address the server does not listen on.
LOOPBACK_HOSTS = ("127.0.0.1",)

_sessions = 0
_local = 0
_hub_only_runs = 0
#: True while a `hub_only` test runs: its opens stay local.
_hub_only = False


def hub_only(target):
    """Mark a test method, or a whole `TestCase`, as exercising a hub-only path."""
    target.sd_hub_only = True
    return target


def is_hub_only(test) -> bool:
    method = getattr(test, getattr(test, "_testMethodName", ""), None)
    return bool(getattr(method, "sd_hub_only", False) or getattr(type(test), "sd_hub_only", False))


def install(host: str, port: int, token: str, *, marks: bool = True) -> None:
    """Send every non-creating open to the server. `marks=False` ignores
    `hub_only`, which is how the guard test meets the refusal."""
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
        if marks and _hub_only:
            return database.open_local(target, write=write, create=create,
                                       busy_timeout=busy_timeout)
        _sessions += 1
        return remote.connect(host, port, target, token=token, write=write, create=create,
                              busy_timeout=busy_timeout)

    def served_by(target):
        return None if marks and _hub_only else f"{host}:{port}"

    opener.served_by = served_by
    database._opener = opener


def sessions() -> int:
    return _sessions


class _Result(unittest.TextTestResult):
    """Keeps a `hub_only` test's connections local from its start to its stop."""

    def startTest(self, test):
        global _hub_only, _hub_only_runs
        _hub_only = is_hub_only(test)
        _hub_only_runs += _hub_only
        super().startTest(test)

    def stopTest(self, test):
        global _hub_only
        super().stopTest(test)
        _hub_only = False


class _Runner(unittest.TextTestRunner):
    resultclass = _Result


def main(argv: list[str]) -> int:

    if len(argv) < 2 or ":" not in argv[0]:
        print("usage: sd-db.sh test --remote HOST:PORT TOKEN_FILE [unittest args...]", file=sys.stderr)
        return 1
    from ..serve import read_token

    host, _, port = argv[0].rpartition(":")
    install(host, int(port), read_token(Path(argv[1])))
    atexit.register(lambda: print(f"sd-db test --remote: {_sessions} sessions over the wire, {_local} local creates, "
                                  f"{_hub_only_runs} hub-only tests ran local", file=sys.stderr))
    here = Path(__file__).resolve().parents[2]
    program = unittest.main(
        module=None,
        argv=["sd_db.testing.wire", "discover", "-s", str(here / "tests"), "-t", str(here), *argv[2:]],
        exit=False,
        testRunner=_Runner,
    )
    return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
