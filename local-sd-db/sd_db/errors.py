"""What the library refuses, and how it says so.

Each refusal names what it found and what to do about it. A refusal that
says only "failed" sends the operator to the source; these are read at a
terminal, often months later, and the message is the whole diagnosis.
"""

from __future__ import annotations

from pathlib import Path


def _checkout() -> Path | None:
    """The git checkout this package runs from, or None for an installed copy.

    `sd-db.sh` sits beside the package only in the source tree; a wheel ships
    the package alone. The checkout is the folder above it, when git owns it
    (a worktree's `.git` is a file, so `exists` and not `is_dir`).
    """
    folder = Path(__file__).resolve().parent.parent
    if not (folder / "sd-db.sh").is_file():
        return None
    root = folder.parent
    return root if (root / ".git").exists() else None


class SdDbError(Exception):
    """Every refusal this library raises."""


class SchemaTooNew(SdDbError):
    """The database was migrated by a newer library than this one.

    Refused on open, not on write: an older process reading a schema it does
    not understand reads wrong answers silently, which is worse than not
    starting.

    `sd-db.sh` runs the package from the checkout, so there the usual cause
    is a checkout behind the code that migrated the database (sd:1664). The
    remedy names the pull; reinstalling a virtualenv the script never reads,
    or restoring an older snapshot over live rows, would be the wrong advice.
    """

    def __init__(self, found: int, built_for: int) -> None:
        self.found = found
        self.built_for = built_for
        checkout = _checkout()
        if checkout is None:
            remedy = ("install the matching version of sd-db into this "
                      "virtualenv, or restore a snapshot taken before the migration")
        else:
            remedy = (f"this library runs from the checkout at {checkout}, which "
                      f"is behind the code that migrated the database; update it with "
                      f"`git -C {checkout} pull --ff-only` and run the verb again")
        super().__init__(
            f"the database is at schema version {found} and this library is "
            f"built for {built_for}; {remedy}"
        )


class SchemaTooOld(SdDbError):
    """The database has not been migrated up to this library's version.

    Refused on write and not on read, so a stopped-mid-upgrade machine can
    still be inspected. The message names the command, because the migration
    never runs on open.
    """

    def __init__(self, found: int, built_for: int) -> None:
        self.found = found
        self.built_for = built_for
        super().__init__(
            f"the database is at schema version {found} and this library is "
            f"built for {built_for}; run `local-sd-db/sd-db.sh migrate` after a "
            f"backup, with the dashboard, the runner and sd-serve stopped, then "
            f"start all three again"
        )


class RegistryError(SdDbError):
    """The provider registry cannot be read, or says something impossible."""


class BackupError(SdDbError):
    """A backup or a restore did not hold up.

    The store's own failures never depend on the store, so the caller that
    catches this sends mail through the cron path rather than writing a row.
    """
