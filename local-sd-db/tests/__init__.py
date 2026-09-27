"""The library's own tests. Run with `local-sd-db test`."""

import os

# A run with no destination writes to the USB disk (`sd_db.backup.DEFAULT_ROOT`).
# Relative, this root lands under each test's own home instead; assigned, not
# defaulted, so an operator's exported value never reaches a test either.
os.environ["SD_DB_BACKUP_ROOT"] = "Documents/sd-backups"
