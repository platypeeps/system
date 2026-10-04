"""The satellite's selector: `~/.config/sd/hub.json` (step 4).

Step 4 of `docs/work/2026-09-22-run-the-framework-from-a-second-machine/`.
A machine whose home holds this file is a satellite: `sd_db.connect` sends
every open of the default database to the hub the file names, and
`sd_db.default_path` answers with a `HubPath`, whose `exists()` asks the hub.

    {"hub": "sol", "port": 8769}

The selector is a file and not an environment variable. Cron and launchd
read no shell profile, and a satellite must refuse the same way from every
caller.

Three rules, each one a test in `tests/test_hub.py`:

* No file: `connect` opens the local database, as it always did.
* The file and no reachable hub: `HubUnreachable`, naming the hub. Nothing
  is created on this machine: no database, no folder for one.
* The file beside a local database: `HubConflict`. A machine is a hub or a
  satellite, not both, and the refusal is what keeps the satellite free of
  a second writer.

An explicit path other than the default is opened locally, as before: the
Jev meter's own file, a backup target and a test's fixture are not the
record, and the hub does not hold them.

`token_file` is for loopback only, until step 7 authenticates the peer by
its tailnet identity: `sd-db.sh serve --loopback` refuses a session without
the token it wrote beside its database.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import remote
from .errors import SchemaTooNew

#: Beside `runner.json` and `dashboard.json`, under the same home as the
#: database path it replaces.
CONFIG_RELATIVE = Path(".config/sd/hub.json")
#: The port the design names for `sd-db.sh serve`.
DEFAULT_PORT = 8769
#: Seconds to wait for the hub to answer a connect. A satellite with
#: Tailscale down must fail by name in seconds, not hang for a TCP timeout.
CONNECT_TIMEOUT = 5.0


class HubConfigError(remote.RemoteError):
    """`hub.json` exists and cannot be read as a hub. Never a local fallback."""


class HubConflict(remote.RemoteError):
    """This machine has both `hub.json` and a local database."""

    def __init__(self, config: Path, local: Path, host: str) -> None:
        self.config = str(config)
        self.local = str(local)
        self.host = host
        super().__init__(
            f"this machine has both {config}, which names the sd hub {host}, and a "
            f"local database at {local}; a machine is a hub or a satellite, not both. "
            f"On a satellite, move the local database away; on the hub, remove {config}"
        )


def _base(home: Path | str | None) -> Path:
    # As `database.local_path` reads it: `$HOME` at call time.
    return Path(home) if home is not None else Path(os.environ.get("HOME", "~")).expanduser()


def config_path(home: Path | str | None = None) -> Path:
    return _base(home) / CONFIG_RELATIVE


def configured(home: Path | str | None = None) -> bool:
    """Whether this home names a hub. Cheap: one `stat`, no parse."""
    return config_path(home).exists()


@dataclass(frozen=True)
class Hub:
    """The hub one `hub.json` names."""

    host: str
    port: int
    config: Path
    token_file: Path | None = None

    def token(self) -> str:
        """The loopback server's token, read when the socket is up."""
        if self.token_file is None:
            return ""
        try:
            return self.token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise HubConfigError(
                f"cannot read the token file {self.token_file} that {self.config} names: "
                f"{error.strerror or error}"
            ) from error

    def open(self, local: Path, *, write: bool, create: bool, busy_timeout: int) -> remote.Connection:
        """A session on the hub's own database, in place of `local`."""
        if local.exists():
            raise HubConflict(self.config, local, self.host)
        if create:
            # `init` and `migrate` are hub verbs, and a satellite holds no
            # database to create (R4).
            raise remote.HubOnly("init and migrate", f"{self.host}:{self.port}")
        try:
            return remote.connect(self.host, self.port, None, token=self.token, write=write,
                                  busy_timeout=busy_timeout, timeout=CONNECT_TIMEOUT)
        except remote.HubUnreachable as error:
            raise remote.HubUnreachable(
                self.host, self.port, f"{error.reason} (named in {self.config})"
            ) from error


def read(home: Path | str | None = None) -> Hub | None:
    """The hub this home names, or `None` when it names none.

    A file that exists and does not parse is refused by name. Falling back
    to a local open would put a second database on a satellite.
    """
    path = config_path(home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise HubConfigError(f"cannot read {path}: {error.strerror or error}") from error
    expected = f'expected {{"hub": "<host>", "port": {DEFAULT_PORT}}}'
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        host = data["hub"]
        if not isinstance(host, str) or not host.strip():
            raise ValueError('"hub" is not a host name')
        port = data.get("port", DEFAULT_PORT)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError(f'"port" {port!r} is not a TCP port')
        token_file = data.get("token_file")
        if token_file is not None and (not isinstance(token_file, str) or not token_file):
            raise ValueError('"token_file" is not a path')
    except KeyError as missing:
        raise HubConfigError(f"{path} names no {missing.args[0]!r}; {expected}") from None
    except ValueError as problem:
        raise HubConfigError(f"{path} is not a hub config: {problem}; {expected}") from None
    return Hub(host=host.strip(), port=port, config=path,
               token_file=Path(token_file).expanduser() if token_file else None)


class HubPath(type(Path())):
    """`default_path()` on a satellite: the same path, with `exists()` asked of the hub.

    Gap x1 of the design. Pack verbs test `sd_db.default_path().exists()`
    before they connect, or pass the path to `connect`. On a satellite the
    local file does not exist, so a plain path would read "no database". This
    one compares, prints and hashes as the plain path does. `connect` treats
    it, and any path equal to it, as the default. Paths derived from it
    (`parent`, `with_name`, `/`) are plain paths.
    """

    def __init__(self, *segments, home: Path | str | None = None) -> None:
        super().__init__(*segments)
        self.sd_home = None if home is None else Path(home)

    def with_segments(self, *segments) -> Path:
        return Path(*segments)

    def exists(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the hub has its database. Raises `HubUnreachable`, never guesses.

        A read session proves it. A hub whose file is newer than this
        library still has one, so `SchemaTooNew` answers `True` here and
        refuses at `connect`, where it belongs.
        """
        from .database import connect

        try:
            connection = connect(self, write=False, home=self.sd_home)
        except FileNotFoundError:
            return False
        except SchemaTooNew:
            return True
        connection.close()
        return True
