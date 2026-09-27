import os

# `sd_db.backup` writes to the USB disk when no destination is named. Relative,
# this root lands under each test's own home instead (`tests/__init__.py` of
# local-sd-db says why it is assigned, not defaulted).
os.environ["SD_DB_BACKUP_ROOT"] = "Documents/sd-backups"
