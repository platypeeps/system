---
title: repo-sync sweeps stale worktrees, dead locks and landed branches
created: 2026-09-28
item: sd:1987
---
# Design — Repo hygiene after the nightly sync

## The shape

`repo-sync.sh hygiene [--apply]` walks the conf fleet that `sync` walks and
runs one pass per checkout, in this order:

1. prune remote-tracking refs for deleted remote branches;
2. prune worktree registrations whose directory is gone, after clearing a
   dead lock;
3. classify every local branch except the default branch: landed, or one
   of the report-only classes;
4. report a checkout that is still behind its upstream.

The order matters. Step 1 runs first so that `[gone]` upstreams are known in
step 3. Step 2 runs before step 3 so that a branch held only by a pruned
registration counts as free.

Without `--apply` each safe action prints as `would <action>`. With
`--apply` the action runs and prints what it did. Report-only lines print
the same in both modes. Notes (a live lock, an unreachable remote) print but
count as nothing, so they neither fail a report nor trigger a nightly mail.

Findings are counted through four files, because the fleet loop runs in a
pipeline subshell, as `sync` and `check` already do: `acted`, `found`
(would act), `listed` and `failed`. The exit code and the nightly mail read
those files.

## The default branch

`source:local-repo-sync/repo-sync.sh::hyg_default` takes `origin/HEAD`,
else a local `main`, else `master`. A checkout with none still gets both
prunes; only its branch pass is skipped, with a note. The comparison ref is `refs/remotes/origin/<default>` when it exists.
A branch therefore counts as landed only once its content is upstream. A
local-only merge into `main` keeps the branch, which is the safe side.

## Landed branches

`source:local-repo-sync/repo-sync.sh::hyg_merged` tries three tests, in
order of cost:

| Test | Plumbing | Catches |
| --- | --- | --- |
| ancestor | `git merge-base --is-ancestor` | fast-forward and merge commits |
| tree equals merge base | `git rev-parse <b>^{tree}` against the merge base's tree | a branch with nothing left to land |
| squash | `git commit-tree <b>^{tree} -p <merge-base>`, then `git cherry <default> <sha>` shows `-` | a squash merge with the same patch |

The synthetic commit goes to a throwaway object directory under the run's
temporary folder, with the repository's objects as an alternate. Report mode
therefore writes nothing into the checkout. Its identity is passed inline,
so the probe needs no user configuration.

`git cherry` compares patch ids, and patch ids ignore whitespace. A cherry
match is therefore confirmed with `git patch-id --verbatim`: the verbatim id
of the branch's net diff must equal the verbatim id of a commit on the
default branch since the merge base. Prefixes, renames, colour and external
diff drivers are pinned on both sides, so user config cannot make them
differ; a mismatch keeps the branch. A squash that landed and was reverted
later still matches, so a last check compares every path the branch
changed: the default branch must hold the same mode and object as the
branch tip (`git ls-tree`), or the branch stays.

Three limits are accepted, not fixed. An ancestor whose change was later
reverted on main still counts as landed: the PRD names ancestry as landed,
and every commit stays reachable from main. The default ref may move
between classification and delete; only the branch tip is re-read, and
each delete line carries its restore command. The lock parse assumes
whole-minute timezone offsets, true of every current IANA zone. A path main touched again after the squash also keeps
the branch; that is a missed cleanup, never a wrong delete. A squash whose landed content differs
(a conflict resolved on the way in, or other spacing) stays and shows in a
report-only class.

## Worktree registrations

`source:local-repo-sync/repo-sync.sh::hyg_worktrees` parses
`git worktree list --porcelain` into records split by the unit separator
(`\037`). The separator is not whitespace, so `read` keeps an empty branch
or an empty lock reason in place.

A registration whose directory is gone:

- unlocked: pruned by one `git worktree prune`; each path is checked
  afterwards and reported as pruned or failed;
- locked: `source:local-repo-sync/repo-sync.sh::hyg_lock_dead` parses
  `(pid <n> start <lstart>)` from the lock text. The pid not running, or
  running with a different `ps -o lstart=` value, means dead: the lock is
  cleared with `git worktree unlock`, then pruned. "Not running" means
  `kill -0` reported "No such process"; any other failure to look, a `ps`
  that exits non-zero, or a start time with no parseable clock keeps the
  lock. The lock's start time was formatted in its writer's timezone, and
  every offset is whole minutes, so only a difference in the seconds field
  proves the pid was reused. Any other difference keeps the lock; a reused
  pid with the same seconds (1 in 60) is a missed cleanup. A live pid with
  the same start time is kept with a note. A lock without a pid is kept and listed,
  because nothing can prove it stale.

Blanks are collapsed on both sides before the start-time comparison,
because `ps` pads its fields.

## Removing a worktree that holds a landed branch

A landed branch held by a worktree is deleted only when that worktree goes
first. The worktree goes only when all of these hold:

- it is a linked worktree, not the main checkout;
- its directory exists and it is not locked;
- `git status --porcelain --ignored` prints nothing (untracked and ignored
  files both count: an ignored `.env` or local database is not rebuildable);
- no process has its cwd at or under it.

`source:local-repo-sync/repo-sync.sh::hyg_cwds` reads process cwds from
`/proc` on Linux, else from `lsof -d cwd`. A failed `lsof`, an empty scan,
or neither source makes the worktree count as in use. The scan runs afresh
for each candidate, as its last check before removal, so a process that
moved in after an earlier scan is still seen. A scan took 6 seconds on a
loaded workstation; only candidates pay for it. The window between the scan
and `git worktree remove` remains; no agent in the fleet takes a
cooperative lock the sweep could honour instead.

Removal is `git worktree remove` without `--force`, so git applies its own
clean check as a second guard. A failed removal keeps the branch.

## Deleting a branch

A landed branch whose newest reflog entry is younger than
`REPO_SYNC_HYGIENE_MIN_AGE` seconds (default 86400) is kept with a note, as
is one with no reflog. A fresh agent branch sits at the default tip and
counts as landed; the age is the only sign it is new.

The worktree list is read again just before the delete; a branch checked
out since classification is a failure, not a delete. The delete is
`git update-ref -d refs/heads/<b> <sha>`, which compares the tip with the
classified sha and removes the ref in one step, so a moved tip also fails.
`update-ref` leaves the config section, so `git config --remove-section`
follows it. The line printed carries the name, the sha and the restore
command:

    deleted branch <b> <sha> (landed by squash; restore: git -C <repo> branch <b> <sha>)

## Report-only classes

| Line | Rule |
| --- | --- |
| `KEEP` | a landed branch held by the main checkout, a dirty worktree, or a worktree whose lock holder is not running; or a gone directory with a pidless lock |
| `GONE` | upstream `[gone]` and content not on the default branch |
| `LOCAL` | `git rev-list <b> --not --remotes` is not empty |
| `DONE` | the name carries `sd-<n>`, `sd_<n>` or a trailing `-<n>`, and item `<n>` is `done` |
| `BEHIND` | the main checkout is behind its upstream after sync: local changes, local commits, or not pulled |

A landed branch in a worktree in live use is kept with a note, not a `KEEP`
line: its lock holder runs, or a process has its cwd inside it. A fresh
agent branch sits at the default tip and counts as landed, so listing it
would mail every night an agent runs.

`GONE` wins over `LOCAL` for one branch. `DONE` is checked on its own, so a
branch can carry two lines.

`source:local-repo-sync/repo-sync.sh::hyg_item_state` reads the sd database
read-only with `sqlite3`, at `REPO_SYNC_SD_DB` or the default path. A
machine without the database or `sqlite3` lists no `DONE` lines. That keeps
repo-sync independent of the command pack. A git branch name cannot hold a
colon, so `sd:<n>` in the PRD reads as `sd-<n>` or `sd_<n>` here.

## Remote-tracking refs

The PRD names `git fetch --prune`. The sweep uses `git remote prune <remote>`
instead. It prunes the same refs without downloading objects, and its
`--dry-run` gives the report mode the same list. In report mode an
unreachable remote is a note. With `--apply` it is a failed action.

## Worktrees under the root

`source:local-repo-sync/repo-sync.sh::scan_disk` already globs `.git` one
and two levels down. A `.git` that is a file, with a git dir that differs
from the common dir, is a linked worktree; a submodule has the two equal.
`scan_disk` writes `WORKTREE <dir> (of <parent>)` and skips the origin check,
so the directory is neither a `MISMATCH` nor an addition. The parent path is
made relative to the root's real path, because git reports resolved paths.
`hygiene` needs no change for it: the parent's `git worktree list` already
names it.

## Nightly

`nightly` runs `hygiene --apply` in a subshell after `sync`, whatever sync
returned. It mails the report through local-notify when `acted`, `listed` or
`failed` is non-empty. A hygiene failure is in the mail and does not fail
the job. A failed mail does, as for the other two mails.

## Exit codes

| Mode | 0 | 1 | 2 |
| --- | --- | --- | --- |
| report | nothing found or listed | something to act on or listed | unknown option |
| `--apply` | no action failed | an action failed | unknown option |

`hygiene` is not a `status` subcommand, and its help says so, so
local-health-check does not read it.

## Tests

`local-repo-sync/tests/test_hygiene.py` builds fixture repositories in a
temporary directory: a bare origin, a clone under the fixture root, and the
branches and worktrees each case needs. It reuses `Fixture` from
`test_repo_sync.py`, so the script runs as a copy with its own conf, `HOME`
and dead SSH transport. The existing `run_suite repo-sync` line discovers
the new file; nothing else is wired.
