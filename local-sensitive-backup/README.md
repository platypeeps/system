# local-sensitive-backup

Monthly backup of sensitive files (keys, tokens, cloud configs) to the
1Password vault `Backups`, as one document per month.

```sh
./sensitive-backup.sh plan   # the files a run would store
./sensitive-backup.sh run    # store them, keep the newest 12 months
./sensitive-backup.sh test   # the unittest suite in tests/
```

The files come from `$SYSTEM_TOOLS_CONFIG/sensitive-backup/targets` (copy
`targets.example`): one glob per line, relative to `$HOME`, and
`!<pattern>` lines that drop files by name. A wildcard may stand only in
a line's last part (`.config/tool/*.env`, not `*/credentials`). A run
fails, and stores nothing, when a line or any folder it matches adds no
file, a line ending in `/` matches something other than a folder, a folder
cannot be read, a directly named path is not a regular file, or a symlinked folder
leads to a match: a smaller archive would replace the month's complete
one. A symlinked file is stored as its contents; inside a folder, broken
links and sockets are skipped.

`run` packs the files into a gzip tarball in a private temp folder
(umask 077), stores it with `op document create` titled
`<host> sensitive files <YYYY-MM>` with tags `backup,sensitive`, and
deletes the tarball. It then deletes this host's older items: all but
the newest per month, and every month past the newest 12. A second run
in one month replaces that month's item. The report gives the file
count, the bytes and the item id; it never prints a file or a token.

`run` signs in only with `OP_SERVICE_ACCOUNT_TOKEN`, a 1Password service
account that can read and write `Backups` alone: with app integration op
waits for Touch ID, which an unattended run never gets. The example job
`local-cron-jobs/examples/sensitive-backup-monthly.job` sets it from
`OP_BACKUP_SERVICE_ACCOUNT_TOKEN` in `~/.config/shell/env.sh`, so no
other op call in a shell acts as the service account.
