# Health

Health is at `/fleet-health` (sd:2115); `/health` stays the service's own
check. `GET /api/health` (`health_screen.document`) lists eight of the
design's nine areas in its order, then Doctor (sd:3393); it leaves out
Attribution (sd:3011). Each has a reader. Three read what the dashboard had: Worktrees (registrations whose
directory is gone, from the fleet child), Ports (`ports_screen`'s reader, which the retired Operations > Ports used, with
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
  `sd-db credentials` writes from `credentials-nightly.job`: the GitHub PAT
  (present, accepted, expiry), `gh auth status`, `HA_TOKEN` against `HA_URL`,
  and `claude mcp list`. An expiry lights caution 30 days ahead and warning 7
  days ahead; a heartbeat older than 48 hours is a caution row.
- In all three, a read that failed is an unknown row naming the reason, never
  a count of zero.

Disk and Branches (sd:2202, sd:2204) are read by `health_collectors`, each
inside an 8-second budget; past it the area says it stopped rather than
waited on.

The readers that wait on subprocesses (Disk, Worktrees, Branches,
Ports, Doctor) run at once, so the slowest decides how long the page
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
  presence, not their size, and `git status --porcelain` in each: a worktree
  with uncommitted changes keeps its folders.
- Doctor runs `local-machine-setup/machine-setup.sh doctor` in a child process
  with a 180-second deadline and reads its `ok`, `WARN` and `FAIL` lines: a row
  per WARN (caution) and FAIL (warning), then one row with the rest. A run
  without the doctor's closing line, or whose exit disagrees with it, is the
  area's error, never clean. An answer stands for 10 minutes; a failed run
  drops it. The page waits 2 seconds on a run in flight, then says it is still
  running.

Prune registrations, Delete merged and Remove build output run from the page
(sd:3393), behind a confirm that names the registrations, branches or folders.
Each posts `/api/health/cleanup` with the row and its `revision`, a digest of
what the page showed. The server reads that subject again and answers 409 when
it differs, so nothing is written. Then git's own guards hold:
`git worktree prune` keeps a locked registration, and `git branch -d` refuses
an unmerged branch. A registration git could not read refuses the prune. Build
output goes only from a worktree read again just before: the same head, still
in `origin/HEAD`, and clean; a symlink is never followed. A step whose result
is not known answers 502 with `outcome: unknown` and what landed, and the page
reads Health again; a check before a step that could not be read is unknown
too, never a refusal. A write where git or a check refused every step wrote
nothing and answers 400. None of the three runs in bulk. `health_cleanup`
holds the writes.

Attribute, Show biggest, Show use and Inspect listener are CLI lines for Copy,
and Re-check reads the document again. Re-run collector posts `/api/shadow/sync`, as
Contributions does, and reads Health again when the run ends (sd:2894).

Snooze, on every row that is not ok, hides the row for 1 hour, until the next
08:00 or for 1 week (sd:1896). It posts `/api/snooze`, as Today does, and the
row waits under Snoozed with its time and an Unsnooze until then. A snooze
holds only while the row reads as it did: more alerts, a worse state or another
count shows it again. A snooze read that fails hides nothing and says so.
