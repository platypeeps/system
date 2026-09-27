"""Which pack is installed, and whether it is new enough to retire a source.

A retire step hands the answer to a question over to the database. Every
reader that asks the question has to already know how to ask the new way
before the old way stops existing, and the readers are not in this
repository -- they are the pack's `sd_lib`, `sd-status` and `sd-docs-lint`.
So the retire refuses under a pack that cannot answer, and names the version
it found, because "upgrade the pack" without a version is a sentence the
operator cannot act on.

**The receipt says which checkout. It does not say which bytes.** The
installer copies agents, hooks, skills and companions into the platform
homes and records each one in `installed.json`; nothing under `bin/` is
among them. This machine's receipt lists 500 owned paths across
`agent:claude`, `hook`, `skill:claude`, `companion:claude`, `skill:codex`,
`companion:codex` and `skill:opencode`, and not one is a `bin/` path. The
installed `sd-status` skill reaches its program as the *relative* path
`bin/sd-status`, resolved against whatever repository the skill is running
in -- the checkout. So `bin/sd_lib.py` is never copied, and the file a
reader loads is the checkout's live one.

That is why `installed()` reads the receipt for the checkout path and then
opens `bin/sd_lib.py` **on disk now**. A checkout on its own still proves
nothing -- an unrelated clone under some other directory is not what the
readers load -- so the receipt is still what picks the directory. But the
receipt's `commit` is a record of the install, not of the file being
probed; do not "fix" this toward reading `bin/sd_lib.py` out of that commit.
The checkout may have moved since, and the moved-to state is what actually
runs. See `Pack.refusal`, which has to keep those two apart in what it says
to the operator.

**What is probed is the reader contract, and it is two names, not one.** The
retire commit changes the answer for two populations of reader at the same
moment, and each has its own entry point in `sd_lib`:

* `status_marker` -- a checkout **with** a database. It reads
  `docs/work/.status-source`, the file the retire commit adds, and returns
  `file` or `row`; `Rows` beside it then takes the status from the `item`
  row. This is the half that reads what the removed `status:` line used to
  say.
* `delivered` -- a checkout **without** one, CI among them. Item A's
  criterion 13: a database-free clone reads "the line while the marker is
  absent and git alone once it is present", and `delivered` is how it asks
  git. The retire's commit is what makes the marker present, so it switches
  that path on too.

`Rows` is deliberately **not** probed, and that is a decision rather than an
oversight. It landed in the pack's same commit as `status_marker`
(`d9aca2e0`), so it discriminates no pack version that has ever existed, and
a guard entry that can never fire is decoration rather than a check. If the
two are ever split, this list grows by one name and this paragraph goes.

Probing `delivered` alone was the first version of this file and was wrong.
`delivered` answers "has this item shipped" out of git history and is
untouched by anything the retire removes; it landed two pack commits before
the row readers (`eb7695c7`, then `d9aca2e0`). A pack installed from
`eb7695c7` passed that guard and could not read the row, which is the exact
failure the four refusals below exist to prevent.

**Every way of not finding them is a refusal.** Four of them, and each one is
a separate case rather than a shared `except`:

* no receipt -- the pack was never installed on this machine;
* a receipt naming a checkout that is gone, or holding no `bin/sd_lib.py`;
* an `sd_lib.py` that will not import;
* an `sd_lib` that imports and is missing any wanted name, or binds one to
  something that is not callable.

They are enumerated because the tempting shape is to answer "no pack found"
with "nothing to check, carry on". That is the fail-open reading, and it
retires the source under exactly the pack that cannot read what replaces it.
The default here is refusal, and a `Pack` is usable only when every wanted
name was found and calling none of them was necessary to find out.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

#: Where the pack's installer keeps its receipt, under the state home. The
#: two names are the pack's (`sd_install.STATE_DIR`, `RECEIPT_NAME`) and are
#: spelled here rather than imported: this library must answer with no pack
#: importable at all, which is the whole case it is written for.
STATE_DIR = "sd-ai-command-pack"
RECEIPT_NAME = "installed.json"

#: The module the retire needs, relative to the checkout the receipt names.
LIBRARY = Path("bin/sd_lib.py")

#: The names whose presence is the version check, and what each one is for.
#: `status_marker` reads `docs/work/.status-source` for a checkout that has a
#: database; `delivered` is what a database-free one asks git once that
#: marker exists. The retire commit turns both paths on at once, so a pack
#: carrying one and not the other cannot read what the retire leaves behind.
WANTED = ("status_marker", "delivered")

#: What a version reads as when the receipt cannot supply one.
UNKNOWN = "unknown"


def state_home(home: Path | str, environ: dict[str, str] | None = None) -> Path:
    """`$XDG_STATE_HOME` when it is absolute, else `<home>/.local/state`.

    Same rule as the pack's own helper, including the absoluteness test: a
    relative value in that variable resolves against the working directory,
    which for a cron job is not the home anybody meant.
    """
    environ = os.environ if environ is None else environ
    value = environ.get("XDG_STATE_HOME", "")
    if value and Path(value).is_absolute():
        return Path(value)
    return Path(home) / ".local" / "state"


def receipt_path(home: Path | str, environ: dict[str, str] | None = None) -> Path:
    return state_home(home, environ) / STATE_DIR / RECEIPT_NAME


@dataclass(frozen=True)
class Pack:
    """The installed pack as the retire needs to know it.

    `usable` is the only thing a caller should branch on, and `why` is the
    sentence the refusal carries. `version` is filled in even when the pack
    is unusable, because it is the half of the refusal that says which
    install this is.

    `version` is the receipt's recorded commit and **not** the commit of the
    file that was probed: the probe reads `checkout/bin/sd_lib.py` as it
    stands on disk, and the checkout may be many commits from where it was
    when the pack was installed. `refusal` therefore labels the two
    separately, and must go on doing so -- an operator sent to inspect a
    commit that is not what failed inspects the wrong thing.
    """

    receipt: Path
    checkout: Path | None
    version: str
    usable: bool
    why: str

    def refusal(self) -> str:
        wanted = ", ".join(f"`sd_lib.{name}`" for name in WANTED)
        # Two facts, kept apart on purpose. The version is what the receipt
        # recorded at install time; the probe read a file on disk that may
        # have moved any distance since. Merging them into "pack <version>
        # defines no ..." sends the operator to inspect a commit that is not
        # what failed.
        if self.checkout is None:
            probed = "no checkout was opened, so nothing was probed"
        else:
            probed = (
                f"what was probed is {self.checkout / LIBRARY} as it stands on "
                f"disk now, which is the file the readers load and need not be "
                f"the commit above"
            )
        return (
            f"the installed pack cannot answer {wanted}: {self.why}. The "
            f"receipt at {self.receipt} records version {self.version} as the "
            f"commit it was installed from; {probed}. The retire hands status "
            f"over to the database, and every reader has to be able to ask the "
            f"new way first. Bring that checkout to a commit that carries them "
            f"and run the sitting again."
        )


#: The name the probe registers under. Not `sd_lib`, so nothing that imports
#: `sd_lib` can pick the probe up by accident, and removed again either way.
PROBE = "_sd_db_pack_probe"


def _load(path: Path):
    """Import one file as a module, under a private name, and unregister it.

    Registered while it executes, and only while. `dataclasses._is_type`
    reads `sys.modules.get(cls.__module__).__dict__` with no guard, and it
    is reached whenever a field annotation is a *string* needing resolution
    against the defining module. `bin/sd_lib.py` has `from __future__ import
    annotations`, so every annotation there is a string, and its first
    dataclass (`:254`) dies unregistered with `'NoneType' object has no
    attribute '__dict__'`.

    The first version left the registration out and so refused *every* pack.
    Fail-closed, and therefore silent: the refusal named a version and read
    as "your pack is too old" for a pack that was perfectly current.

    The worry that kept it out was real and is answered by the name. The
    entry is `_sd_db_pack_probe`, never `sd_lib`, so no later import can
    resolve to a half-initialised module; and the `finally` removes it even
    when the body raises, so a failed probe leaves nothing behind.
    """
    spec = importlib.util.spec_from_file_location(PROBE, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} is not loadable as a module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PROBE] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(PROBE, None)
    return module


def installed(*, home: Path | str, environ: dict[str, str] | None = None) -> Pack:
    """What the receipt says is installed, and whether its `sd_lib` can answer."""
    receipt = receipt_path(home, environ)
    if not receipt.exists():
        return Pack(
            receipt=receipt,
            checkout=None,
            version=UNKNOWN,
            usable=False,
            why=f"there is no receipt at {receipt}, so no pack is installed here",
        )
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        return Pack(
            receipt=receipt,
            checkout=None,
            version=UNKNOWN,
            usable=False,
            why=f"{receipt} will not parse ({failure})",
        )
    if not isinstance(payload, dict):
        payload = {}
    commit = str(payload.get("commit") or "")
    version = commit[:12] if commit else UNKNOWN
    named = payload.get("checkout")
    if not named:
        return Pack(
            receipt=receipt,
            checkout=None,
            version=version,
            usable=False,
            why=f"{receipt} names no checkout",
        )
    checkout = Path(str(named))
    library = checkout / LIBRARY
    if not library.exists():
        return Pack(
            receipt=receipt,
            checkout=checkout,
            version=version,
            usable=False,
            why=f"{library} does not exist",
        )
    try:
        module = _load(library)
    except BaseException as failure:  # noqa: BLE001 - a module body runs here
        return Pack(
            receipt=receipt,
            checkout=checkout,
            version=version,
            usable=False,
            why=f"{library} will not import ({type(failure).__name__}: {failure})",
        )
    # Every wanted name, and the report says which ones were missing rather
    # than stopping at the first: "no `status_marker`" sends the operator to
    # a pack that then turns out to be missing the other one too.
    absent = [name for name in WANTED if getattr(module, name, None) is None]
    if absent:
        return Pack(
            receipt=receipt,
            checkout=checkout,
            version=version,
            usable=False,
            why=f"{library} imports and defines no {', '.join(repr(n) for n in absent)}",
        )
    uncallable = [
        f"{name!r} as {type(getattr(module, name)).__name__}"
        for name in WANTED
        if not callable(getattr(module, name))
    ]
    if uncallable:
        return Pack(
            receipt=receipt,
            checkout=checkout,
            version=version,
            usable=False,
            why=f"{library} defines {', '.join(uncallable)}, not a function",
        )
    return Pack(
        receipt=receipt,
        checkout=checkout,
        version=version,
        usable=True,
        why=f"{library} defines {', '.join(repr(name) for name in WANTED)}",
    )
