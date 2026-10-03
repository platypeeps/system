# Health

Health is at `/fleet-health` (sd:2115); `/health` stays the service's own
check. `GET /api/health` (`health_screen.document`) lists the design's nine
areas in its order. Six have a reader. Four read what the dashboard had: Worktrees (registrations whose directory
is gone, from the fleet child Sessions reads), Attribution (your own commits,
by each repository's `user.email`, of the last five weeks on its default branch
`origin/HEAD`, merges left out, that lack `Authored-with:`; a repository with no
`origin/HEAD` or no `user.email` is named in its own row, not read on its
checkout's HEAD; the walk runs inside a 10-second budget, and past it the area
says it stopped rather than waited on), Ports (Operations > Ports' reader, with
its counts and warnings) and Protection (`protection.rows`, drawn as a matrix
with one column per repository and a table carrying the same cells; an unread
repository shows no cell). Credentials, Dependencies and Security have no
collector yet; each shows as unknown and names what it does not read.

Disk and Branches (sd:2202, sd:2204) are read by `health_collectors`, each
inside an 8-second budget; past it the area says it stopped rather than
waited on.

The readers that wait on subprocesses (Disk, Attribution, Worktrees,
Branches, Ports) run at once, so the slowest decides how long the page
waits, not the sum of their budgets. The registry they walk is read first,
on the request's thread, and handed to them as paths. `PAGE_SECONDS` (13)
bounds the whole document: a reader still running at it is its area's
error, and the page does not wait for it.

- Branches lists, per registered repository, the local branches
  `origin/HEAD` already contains, the default branch left out. A merged
  branch a worktree has checked out is its own queued row, since
  `git branch -d` refuses it. A repository with no `origin/HEAD`, or one git
  cannot read, is named in an unknown row, never counted as clean.
- Disk draws a bar per volume from `df -kPl` (the data volume and each volume
  under `/Volumes`; local file systems only), with a row for each volume past
  80% (caution) or 90% (warning). It lists the three biggest subfolders of
  each folder a `storage|<path>` line of `<config>/project-dashboard/disk.conf`
  names (`disk.conf.example`), one `du -k -d 1` per folder in what is left of
  the budget; a folder `du` cannot finish, or that is not mounted, is an
  unknown row, and no `disk.conf` is one too. It finds registered worktrees
  whose `HEAD` is in `origin/HEAD` and that still hold `target/`,
  `node_modules/` or `.venv/`, the rule of 2026-09-25; it reads their
  presence, not their size.

Nothing on the page writes: Prune registrations, Attribute, Delete merged,
Remove build output, Show biggest, Show use, Inspect listener and Re-run
collector are CLI lines for Copy, and Re-check reads the document again.
