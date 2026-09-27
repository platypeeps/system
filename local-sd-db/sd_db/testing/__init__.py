"""One harness, imported by both repositories' test suites.

`system` and the pack both drive the same outside world -- one GitHub, one
provider registry, one launchd domain, one home directory -- and until now
each suite doubled it separately. Two doubles of one thing drift, and the
drift shows up as a test that passes against a shape the other repository
stopped producing. This package is the single double, shipped inside the
library because the library is what both repositories install.

    from sd_db.testing import FixtureHome, FixtureRemote, GitHubDouble

    remote = FixtureRemote(tmp_path)
    with GitHubDouble(remote) as github:
        install_gh(github, home.bin)
        home.merge(gh_environment(github, home.bin))
        ...                       # spawn the thing under test
    assert [call.path for call in remote.calls] == [...]

What each module holds:

* `remote` -- the state. A real bare repository, a pull-request table, a
  protection state, a collaborator list, and the answers to the questions
  the system asks about them.
* `github` -- two doors onto that state, an HTTP one and a `gh` one, so a
  call arriving either way reads one truth and is recorded once.
* `providers` -- the two registry shapes: an OpenAI-compatible endpoint for
  `url` entries and a recording script for `start` entries.
* `stubs` -- `launchctl`, `tailscale`, `curl`, `caffeinate`, `lsof` and
  `local-notify`, on PATH rather than patched over.
* `home` -- a `$HOME` with the system's directories, and the one environment
  mapping that points every door inside it.
* `store` -- a real database, at today's shape or an older one, and the two
  shapes `restore` refuses. Not the outside world like the rest, but the one
  fixture only this library may build: nothing outside it opens the database.

Nothing here patches `subprocess`. A suite that patches it proves a call was
made; a suite that puts the command on PATH proves the call was made and
spelled correctly, and that is what the harness is for.
"""

from __future__ import annotations

from .github import GH_SHIM, GitHubDouble, gh_environment, install_gh
from .home import ENV_SH, HOME_DIRS, FixtureHome
from .providers import (
    START_PROVIDERS,
    URL_PROVIDERS,
    ProviderCall,
    ProviderDouble,
    Reply,
    StartProvider,
    install_start_command,
    provider_environment,
    write_registry,
)
from .remote import Call, FixtureRemote, PullRequest, RemoteRefusal
from .store import add_unknown_table, break_foreign_keys, make_store
from .stubs import STUB_NAMES, StubCall, Stubs

__all__ = [
    "Call",
    "ENV_SH",
    "FixtureHome",
    "FixtureRemote",
    "GH_SHIM",
    "GitHubDouble",
    "HOME_DIRS",
    "ProviderCall",
    "ProviderDouble",
    "PullRequest",
    "RemoteRefusal",
    "Reply",
    "STUB_NAMES",
    "START_PROVIDERS",
    "StartProvider",
    "StubCall",
    "Stubs",
    "URL_PROVIDERS",
    "add_unknown_table",
    "break_foreign_keys",
    "gh_environment",
    "install_gh",
    "install_start_command",
    "make_store",
    "provider_environment",
    "write_registry",
]
