# Health

Health is at `/fleet-health` (sd:2115); `/health` stays the service's own
check. `GET /api/health` (`health_screen.document`) lists eight of the
design's nine areas in its order; it leaves out Attribution (sd:3011). Each has
a reader. Three read what the dashboard had: Worktrees (registrations whose
directory is gone, from the fleet child Sessions reads), Ports (Operations > Ports' reader, with
its counts and warnings) and Protection (`protection.rows`, drawn as a matrix
with one column per repository and a table carrying the same cells; an unread
repository shows no cell).

Dependencies, Security and Credentials read rows nightly jobs stored; the page
calls no service and reads no credential.

- Dependencies (sd:2205) and Security (sd:2206) read the `alerts` the nightly
  `sd shadow sync` stores in each managed repository's `repo_protection` row.
  Dependencies is a row per repository with open Dependabot alerts, a warning
  when one is critical or high or the list ran past its first page; an
  archived repository is left out. Security
  is a row per public repository with open secret-scanning alerts (warning) or
  with scanning off (caution); a private repository is not scanned, by policy.
  A repository whose alerts were not re-read in 48 hours is a caution row.
  The sync reads one page of 100 alerts, so a longer list shows as `100+`.
- Credentials (sd:2203) reads the latest `credentials:nightly` heartbeat that
  `sd-db.sh credentials` writes from `credentials-nightly.job`: the GitHub PAT
  (present, accepted, expiry), `gh auth status`, `HA_TOKEN` against `HA_URL`,
  and `claude mcp list`. An expiry lights caution 30 days ahead and warning 7
  days ahead; a heartbeat older than 48 hours is a caution row.
- In all three, a read that failed is an unknown row naming the reason, never
  a count of zero.

Disk and Branches (sd:2202, sd:2204) are read by `health_collectors`, each
inside an 8-second budget; past it the area says it stopped rather than
waited on.

The readers that wait on subprocesses (Disk, Worktrees, Branches,
Ports) run at once, so the slowest decides how long the page
waits, not the sum of their budgets. The registry they walk is read first,
on the request's thread, and handed to them as paths. `PAGE_SECONDS` (13)
bounds the whole document, and the page does not wait for a reader still
running at it. Each area runs one scan at a time: a later request joins a
scan still running instead of starting another, so a stalled mount holds one
thread, not one per refresh. Meanwhile the area shows its last answer under
"Not re-read", or is its error if it has none.

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

Prune registrations, Attribute, Delete merged, Remove build output, Show
biggest, Show use and Inspect listener are CLI lines for Copy, and Re-check
reads the document again. Re-run collector posts `/api/shadow/sync`, as
Contributions does, and reads Health again when the run ends (sd:2894).

Snooze, on every row that is not ok, hides the row for 1 hour, until the next
08:00 or for 1 week (sd:1896). It posts `/api/snooze`, as Today does, and the
row waits under Snoozed with its time and an Unsnooze until then. A snooze
holds only while the row reads as it did: more alerts, a worse state or another
count shows it again. A snooze read that fails hides nothing and says so.
