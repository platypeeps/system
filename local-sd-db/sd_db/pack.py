"""Which pack checkout is installed, as the installer's receipt names it.

The installer records the checkout it ran from in `installed.json`; nothing
under `bin/` is copied, so a caller that runs the pack's `bin/sd` runs it
from that checkout. `sources.vault` reads the plugin list that way.

The probe of `sd_lib` that the `docs/work` retire asked for went with the
retire (sd:3231): the pack reads every item's status from its row (sd:3015).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

#: Where the pack's installer keeps its receipt, under the state home. The
#: two names are the pack's (`sd_install.STATE_DIR`, `RECEIPT_NAME`) and are
#: spelled here rather than imported: this library must answer with no pack
#: importable at all.
STATE_DIR = "sd-ai-command-pack"
RECEIPT_NAME = "installed.json"

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
    """The receipt, the checkout it names or `None`, and why there is none.

    `version` is the receipt's recorded commit, a record of the install and
    not of the checkout as it stands now.
    """

    receipt: Path
    checkout: Path | None
    version: str
    why: str


def installed(*, home: Path | str, environ: dict[str, str] | None = None) -> Pack:
    """What the receipt says is installed."""
    receipt = receipt_path(home, environ)
    if not receipt.exists():
        return Pack(receipt=receipt, checkout=None, version=UNKNOWN,
                    why=f"there is no receipt at {receipt}, so no pack is installed here")
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        return Pack(receipt=receipt, checkout=None, version=UNKNOWN,
                    why=f"{receipt} will not parse ({failure})")
    if not isinstance(payload, dict):
        payload = {}
    commit = str(payload.get("commit") or "")
    version = commit[:12] if commit else UNKNOWN
    named = payload.get("checkout")
    if not named:
        return Pack(receipt=receipt, checkout=None, version=version,
                    why=f"{receipt} names no checkout")
    checkout = Path(str(named))
    return Pack(receipt=receipt, checkout=checkout, version=version,
                why=f"{receipt} names {checkout}")
