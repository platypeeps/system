---
title: the runner works the queue
created: 2026-09-05
status: done
item: sd:235
---

# PRD — the runner works the queue

## Problem

Item B, `docs/work/2026-09-05-one-database-one-front-door/`, specified the
runner as its requirement 4, and the runner is where the review went to
die: twenty of B's first forty-eight findings landed on it, and rounds
twenty to twenty-six still found a high-severity defect there at about one
and a half per round, each fix a paragraph on a paragraph. Prose review
reached its limit on a process whose defects are about pids, worktrees,
remotes and restarts. So the runner is its own item, split from B on
2026-09-05 by the operator's decision, lands last, and is built first as a
spike, a few hundred lines against B's fixture harness that run one
assignment end to end, whose findings go on this item before its prose is
reviewed again.

## What this changes

`local-sd-runner/`, a launchd service, `local.system-tools.sd-runner` in
`personal.agent`. It reads and writes only through B's library, in the
vocabulary B's requirement 4 keeps: `assignment` rows, `runner` rows,
`merge_policy` on the repository row. Item A's `sd-ship` is the merge path
it calls; item A's registry names the providers it starts and the
variables each receives. The three requirements below are B's requirement
4 and the runner's three backbone bullets from B's requirement 9, moved
here on the day of the split with the changes the log records.

### Requirement 1 — the runner turns an assignment into a session

`local-sd-runner/`, a launchd service. It watches `assignment` rows with status
`queued`, one at a time per repository, and for each:

- resolves the provider for the row's role from the registry;
- starts a session in a worktree of its own, never in the operator's
  checkout, under
  `~/.local/share/sd/worktrees/<item>/<assignment>` on the item's branch,
  fresh for every row and never reused. The worktree is a clone, every
  row's, from D's round eight, which is what the word means in this
  document from here on and what the commands named `sd worktree`
  manage: `git clone --reference
  <checkout> --dissociate <remote>` on the item's branch, the
  checkout's store read while the clone is made and never again, the
  objects it needs copied into its own store before the session
  starts, `.git/objects/info/alternates` absent, its own refs, its own
  remote-tracking refs and its own `fetch`, from D's rounds six and
  seven, and, before any provider starts, the checkout's own hooks,
  from D's rounds thirty-two and thirty-seven: the checkout's
  effective hooks directory, `git rev-parse --git-path hooks` run
  in the checkout, which honors a `core.hooksPath` from any config
  and resolves a relative one against the checkout's working tree,
  is read at dispatch and copied whole, from D's round thirty-eight,
  every file and subdirectory with its mode and its layout, the
  `.sample` files left out, to the place in the clone that keeps
  what a hook resolves: the default `.git/hooks/` to the clone's
  `.git/hooks/`, a `core.hooksPath` relative to the checkout's root
  to the same path under the clone's root, and an absolute one
  outside the checkout not copied at all, the clone using it where
  it stands; the copy is recorded on the clone's config as
  `sd.hooksForward`, the one directory the runner's guard forwards
  to, `core.hooksPath` being the guard's own, because a hook that
  sources a sibling helper without an executable bit, or finds the
  repository's scripts by a path relative to its own directory,
  loses it when the executables alone are copied elsewhere, and a
  hook that hard-codes the checkout's absolute path is the stated
  limit; because a clone of the remote carries
  neither the checkout's hooks nor its local config, a
  repository whose validation or whose Git LFS lives in a hook
  installed there would run in the clone without it, and a
  `core.hooksPath` such as `.husky/_`, relative, generated and
  ignored, names a directory the clone does not have; the copy is
  the checkout's hooks as they stand at dispatch, and a hook the
  operator installs later reaches the next row and not this one,
  because a clone that keeps the checkout as its alternate
  loses objects the operator's `gc` prunes after a branch deletion
  there, which the runner's lane never constrains, so the cost of one
  copy of the reachable objects per parallel row is what isolation
  costs. A linked worktree of the checkout, `git worktree add`, was the
  serial row's until D's round eight, and it goes: two worktrees of one
  checkout share every ref and the object store, a session's `fetch -p`,
  branch deletion or `gc` in one reaches the other, and the operator
  edits in the checkout while a serial row runs, the runner's lane
  serializing rows and never the operator, so a worktree of the checkout
  isolated a serial row from nothing that mattered. The clone starts at
  one commit the row records, from D's rounds eight and nine: the
  remote's head of the item's branch, since a session that pushed from
  its clone left the checkout behind, and it stays behind until the
  operator pulls; when the checkout is strictly ahead of the remote,
  the remote's head an ancestor of its own, the runner pushes that
  fast-forward first and starts there, because a clone of the remote
  carries no commit the operator made and did not push, and a push
  that fails refuses the row naming the branch; when the two have
  diverged the row is refused naming both heads, for the operator to
  reconcile; a branch the checkout does not have starts at the
  remote's; a branch the remote does not have and the checkout does,
  the ordinary case for an item's first row, since the item's branch is
  made in the checkout and nothing before this row pushed it, is
  published first, `git push -u` with a lease expecting the remote to
  have no branch of that name, so that a branch made on the remote
  between the check and the push refuses the row naming the branch
  instead of being overwritten, and the row starts at the commit it
  pushed; a branch neither has refuses the row naming it, requirement
  4, from D's round sixteen. Before the session starts,
  in that worktree, the runner fetches the remote and merges the default
  branch as it now stands into the item's branch, because the branch may
  predate the batch and a worktree on it alone would author against an old
  base. A merge and never a rebase: a rebase rewrites every commit on
  the branch and loses item A's attribution trailers, which name commits
  by hash, A's requirement 3; a merge keeps every commit, and the merge
  commit never reaches the default branch, since `sd-ship` squash-merges. A merge
  that conflicts ends the row through `ending` to `blocked` with the
  conflicting files named, the end run below retaining the clone and
  releasing the lease as for any row, from D's round thirty-nine,
  and starts no session, and the operator resolves it on the item. That
  merge is what makes a sequential successor see its predecessor's merged
  change, not the order alone. When the session ends, however it ends,
  the row goes `running` to `ending` in one transaction that records the
  outcome the end will carry, `done` or `blocked` with its note, and
  nothing else is written to the row until the cleanup below has run to
  its last step, from D's round five: the cleanup spans the database and
  the filesystem, a survivor check, a push, a retention or an archive,
  a copy of ignored files and a release, and a runner
  that dies between two of them must be able to finish on its next
  start, which a row already marked terminal would hide from the restart
  rule that reads `running` rows. So every step is idempotent, a
  rename of a clone already retained renames nothing, an
  archive written again replaces the last one whole or not at all, a copy of
  files already copied writes the same bytes again, and the terminal status is written last, in the
  transaction that releases the repository, so that a row is `ending`
  exactly while something remains to do and the branch stays held
  exactly that long. The row records the step the end reached,
  `end_step`, written as each completes, and no clone the runner made
  is ever unlinked on the queue's path, from D's rounds nineteen,
  twenty-nine and thirty-one: once the push gate has passed, the
  clone's directory is renamed in one `rename` into the retained
  path, `~/Documents/sd-backups/worktrees/<assignment>/<run>/clone/`,
  and every file and directory under it is made immutable, `chflags
  -R uchg`, the runner running on the operator's Mac where that flag
  refuses `open` for write, `unlink` and `rename`, and the row
  records `retained` with the time; the copy of ignored files reads
  that directory, and the release follows. The flag does **not**
  revoke a descriptor already open, from D's round forty-two, which
  ran the sequence: `UF_IMMUTABLE` is checked at `open(2)` and not at
  `write(2)`, so a `write` through a descriptor taken before the
  `chflags` succeeds and lands in the retained clone. A fresh open, an
  unlink under an immutable parent and a rename of the immutable
  directory are each `EPERM`, and those the flag does close. The
  rename takes the path the operator's editor and shell know, so a
  save after it fails where the operator sees it. The open descriptor
  is closed by order instead of by the flag: before the rename the
  runner checks with `lsof` for any process holding a file or a
  directory under the clone, the same check the nightly removal makes
  before it records `deleting`, and holds the row `ending` naming
  `clone busy` and the paths until nothing does, which is the check
  the end run of a dirty row already makes. A descriptor opened
  between that check and the flag still writes; that window is the
  stated limit, beside `--no-verify` past the push guard, and a write
  that landed before the flag is in the retained clone either way,
  because a write that landed after the clone was read and before it
  left would be archived by nothing and pushed by nothing, and a
  check only moves that window rather than closing it. Nothing is
  removed here: a teardown on the queue's path needs the flags off
  again before the unlink, an immutable parent refuses its
  children's removal, and every window between a thaw and an unlink
  is the same window again, so removal is the nightly job's and not
  the end's, thirty days after the retention, below, off the path
  every queued row waits on. A rename is one step that is done or
  not, so a rerun at `retained` finds the clone whole under the
  retained path, immutable, and continues from the recorded step,
  and no recovery runs a `git` command against a clone in any state
  but whole; the worktrees directory and the retained path must
  share a device, since a rename across two is no rename, and the
  runner refuses to start naming both paths when `st_dev` differs,
  and the database must not share it, below, from D's round
  thirty-nine.
  In `ending`, the runner looks for survivors before
  it releases the row's lease, and tells the row's own
  from strangers. The row's own:
  members of its process group still alive, any process `lsof` shows
  holding a working directory or an open file under its worktree, and
  any process of this user whose environment carries the row's
  `SD_ASSIGNMENT`, which the supervisor sets before it starts the
  provider and which a child keeps across `setsid` and a change of
  directory, read with `ps -E`, from D's round four. A sibling: a
  process a `running` row of the same repository owns by the same three
  marks, those rows' groups and ids being on them. A sibling's hold is
  ordinary and counts for nothing. Every other holder of something under
  the row's worktree is a stranger, and the runner does not call a
  stranger harmless: a child that scrubbed its environment, left its
  group and moved its working directory away is what a stranger looks
  like from outside, and so is an editor the operator opened on the
  worktree, and the runner cannot tell the two apart. So a
  stranger's hold quarantines the repository as an own survivor's does,
  from D's rounds ten and eleven: the row stays `ending`, its lease
  held and its worktree in place, and nothing `exec` or `merge` is
  dispatched into the repository and no author starts in it until the
  hold ends, running rows finishing what they have, because a clone
  isolates local state and not the remote, and an escaped child can
  still push, delete another item's branch or act on the repository
  from its clone while a merge row lands; what the checkout's `.git`
  holds is the exception, the operator's editor there, since no row
  runs in the checkout and no clone shares a file with it, so a holder
  of it is nobody's concern and stops no row. Today names
  the pid with its command line, so that the operator can; the runner
  kills a stranger never, since the editor is the likelier of the two,
  and the operator ends it or clears the quarantine by name. The ended
  row ends as it ended. With no own
  survivor, and the worktree clean, it passes one durability gate before
  it removes anything, the same gate for every ended row however it
  ended: the branch's head must be on the remote, `git ls-remote` showing
  the branch at or past `HEAD`, or, for a `merge` row, the merge
  confirmed by GitHub with the squash commit on the default branch,
  A's requirement 5, from D's round thirteen, because a squash merge
  with `delete_branch_on_merge` leaves no branch to find and a gate
  that pushed it back would recreate what the merge deliberately
  removed, so a confirmed merge passes the gate, the worktree's
  commits stay in the row's retained clone, since the
  squash carries their diff and not their identities, and no branch
  is recreated; when it is not, a session that committed
  and died before its push, the runner pushes, and when the push fails it
  keeps the worktree and archives it exactly as the dirty case below does,
  the row naming the unpushed commits, because clean proves committed and
  not pushed, and a commit held by this disk alone is not recovered by
  anything in B's requirement 9. With the head on the remote it
  retains the clone, above, from D's
  rounds twenty-seven and thirty-one, whatever its state, because clean and pushed
  speak of the working tree and of the item's branch and of nothing
  else the clone holds, a stash the session left, a commit on a branch
  the session made, a commit reset away and held by the reflog alone,
  and a removal that read them as proof would take the only copy;
  the retained clone stays until `sd-db-backup` removes it thirty
  days after the retention, `chflags -R nouchg` and then the
  recursive removal, off the path any queued row waits on, skipping
  one any process holds a file under, `lsof`, naming the pid on
  Today and trying the next night, finishing the next night a
  removal it died inside without reading what is left, and naming
  each removal on Today, the kept worktree's archive of that run
  going with it and the ignored copies never; the number is the
  operator's to change. A clone is a main worktree that `git worktree
  remove` refuses, from D's rounds seven and eight, and the rename is
  how it leaves; the retention releases the
  repository. Clean is
  `git status --porcelain` empty: every tracked change committed and
  nothing untracked left behind. Ignored files count for nothing in that
  check, but ignored is not disposable: this repository ignores
  `storage/`, `state/`, `volumes/`, logs, `.env` and `*.bak*`, and a
  session that writes a fetched document or a generated dataset under one
  of those loses it to a `--force`. So disposable is an allow-list of
  caches nobody keeps, which the runner carries, `__pycache__/`,
  `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`, `node_modules/`,
  `.venv/`, `venv/`, `*.pyc`, `.DS_Store`, `.sd-run/`, the run's own
  scratch and cache above, plus whatever the repository
  itself declares, `disposable:` in the pack's block of its
  `CLAUDE.local.md`, paths relative to the root, from D's round four:
  the runner's own copy of the checkout's effective hooks directory,
  which `sd.hooksForward` names on the clone's config, is skipped by
  the ignored copy rather than removed, from D's round forty-two: a
  `core.hooksPath` such as `.husky/_` is ignored by its own
  repository, so the copy the runner makes at dispatch shows as an
  ignored untracked directory in the clone and would be preserved and
  shown on Today as the session's work on every run of every
  repository that uses one;
  `target/`, `dist/` and `build/` were on the runner's list, and a name
  is not a promise, since a session can leave its only deliverable or an
  expensive dataset under a directory so named, so a repository that
  builds into one says so and everywhere else those are preserved like
  any other ignored file; the disposable are removed from the clone
  before it is retained, the one unlink the end run makes on the
  queue's path, of files nobody keeps, from D's round thirty-two,
  since a frozen `node_modules/` or `target/` held thirty days per
  run would fill the volume the live worktrees need. Every other ignored file present, minus what
  the runner itself wrote at dispatch — which is the hooks copy at
  `sd.hooksForward` and nothing else, so each of the rest is the
  session's, from D's round forty-two — is copied with
  its relative path from the retained clone to
  `~/Documents/sd-backups/worktrees/<assignment>/<run>/ignored/`, where
  `run` is a counter on the row that every requeue increments, from D's
  round seventeen, because a `resume` requeues the same assignment and
  a second run that writes the same `storage/report.pdf` would
  otherwise meet the first run's under one path, and overwriting loses
  the first while passing over loses the second; the copy reads a
  frozen tree, file by file, and its rerun after a kill copies
  again over whatever the destination holds, since under this run's
  own path a file there can only be this run's earlier partial copy,
  until the two are equal by content hash, from D's rounds seventeen
  and thirty-one; done
  after the retention and before the release, the row and Today name them by
  run, they outlive the retained clone's thirty days, and `discard`
  makes none, the retained clone carrying them for its thirty days,
  since the operator chose to discard. The
  branch is then released, so preserved output never holds a merge. A kept
  worktree's archive, below, carries those files as they are.
  With none and the worktree dirty, it keeps the worktree exactly as it is,
  untracked files and binaries included, because a patch does not carry
  those and a session interrupted mid-edit is the case that matters, and
  in the same step it puts that work where a lost disk cannot take it:
  it archives the clone's whole directory, `.git` included, as it
  stands, to `~/Documents/sd-backups/worktrees/<assignment>/<run>/kept.tar`,
  `run` the counter on the row that every requeue increments, the one
  the retained clone and the preserved ignored files are kept under, from D's rounds
  twenty-eight and thirty-one, because a `resume` requeues the assignment on a fresh
  clone from the remote, which holds none of the stash, the side
  branch or the reflog the first run's clone was retained to keep,
  and a second run's end that wrote over one path per assignment
  would take the first run's only copy inside its thirty days; each
  run's is its own, and a kept worktree's archive goes with the
  retained clone the worktree becomes, on that clone's clock,
  inside the pair the clone copies, from D's round twenty-six, in
  place of the snapshot ref, the index commit and the bundle of D's
  rounds nineteen to twenty-four, which put the working tree, the
  index, its stages, the merge state and the split index into Git's
  object graph one piece at a time so that a bundle could carry them,
  and would have gone on doing so, one round per piece, because the
  clone holds state the graph never reaches, a rebase in progress,
  and the payload of a Git LFS object staged and then overwritten,
  which lives under `.git/lfs/objects/` behind a pointer the index
  names, so that a bundle restored that snapshot with the pointer
  and without the file, and no list of such pieces ends. The archive
  reaches all of it at once, because a clone is a directory and
  what is in the directory is in the archive: tracked, untracked
  and ignored files as written, the index as it is, split or not,
  every stage of an unresolved merge and `MERGE_HEAD` with it, a
  rebase, a cherry-pick, a revert or a bisect stopped where it
  stopped, the LFS store, the stash, the reflogs, the clone's
  config with its `core.hooksPath`, and every commit the clone holds,
  the unpushed ones included; nothing is committed and no ref is
  written to make it so. The archive is written to
  `<assignment>/<run>/kept.tar.partial`, listed back to its end, and renamed
  over the previous one of that run in one `rename`, so that the file under the
  name is always whole, and a runner that dies inside the write
  leaves a partial beside a good archive and never a bad archive
  under a good name; the rerun writes the partial again. Every
  writer of it, the end run of a dirty row and the nightly job,
  and `restore` as its reader, holds an `flock` on
  `<assignment>/<run>/kept.tar.lock` beside it from before the walk below to
  after the rename, from D's round twenty-seven, released by exit
  and by death alike — every one of those three is the runner's own
  Python and takes the lock through `fcntl.flock`, not through
  `flock(1)`, which macOS does not ship, from D's round forty-two,
  and `shlock(1)`, which it does, is a pid file and is not released
  by death, so that two never write one partial, the
  nightly job never replaces an archive the end run is writing, and
  no restore reads one half renamed. And an archive is written of a
  quiet clone alone: the writer walks the clone before and after the
  `tar`, every path with its size and its mtime to the nanosecond,
  and takes the archive only when the two walks agree, because a
  `tar` of a directory a `git commit` or a `git gc` is writing into
  can carry an index or a ref naming an object the walk passed before
  it existed, and the listing proves the tar's structure and nothing
  of that; when the walks differ the writer discards the partial and
  tries again, the end run of a dirty row on every tick with
  the row `ending` naming `clone busy` and the paths that changed,
  since the survivor check has passed and what writes the clone then
  is the operator, who finishes, and the nightly job once more after
  a minute, then keeps the previous archive and names the skip on
  Today, a kept worktree the operator is working in that night being
  one the next night reaches; a kept worktree is archived as it
  stands and never frozen, since nothing removes it, and a write
  after its walk is in the next night's archive and lost to nothing,
  from D's round twenty-nine; a clone that leaves its path is never
  archived, it is retained whole, above, from D's round thirty-one. A write that changes neither a file's
  size nor its mtime is the stated limit, which no write of git's is,
  since git writes a new file and renames it into place. A retained
  clone and a kept worktree's archive are each the size of the
  clone, history and all, the disposable aside, and that is the cost of carrying
  everything; the backup path holds a month of retained clones, every
  run of an assignment its own, and every kept worktree's archive,
  under a floor and a quota, from D's rounds thirty-two to forty and
  the operator's word after them: the worktrees directory and the
  backup path live on one work volume, an APFS volume the operator
  adds once with a quota, `diskutil apfs addVolume <container> APFS
  sd-work -quota <size>`, and configures the two paths under, and
  `sd.db` stays on the operator's own volume, so that the quota is
  the bound the volume keeps, a provider gets `ENOSPC` inside it and
  takes nothing outside it, the retention's rename stays on one
  device, and a full work volume leaves every row write possible;
  the runner refuses to start naming the three paths when the
  database's `st_dev` equals the worktrees directory's, as it
  refuses when the worktrees directory's and the backup path's
  differ. `free_floor_gb` on the runner's configuration, forty by
  default and the operator's to change, is the free space the work
  volume must keep: the runner reads it with `statvfs` on every tick
  and dispatches nothing while the free space is under it, naming
  the floor and the free space on Today and sending the operator one
  email through the path B's requirement 9 keeps for the store's own
  failures, once per crossing, rows already running finishing what
  they have and every `ending` row ending, because a retention is a
  rename and needs no space and a release needs none. A retained
  clone leaves at thirty days and never earlier, whatever the free
  space, because a released clone is the one copy of a stash, a side
  branch, a reflog-only commit or a Git LFS payload the push did not
  carry, and a removal that made room by taking it inside the thirty
  days would have spent the copy the retention exists for. The
  runner bounds no run's writes and reserves nothing at dispatch:
  the growth allowance, the reservation and the fastest-grower kill
  of rounds thirty-four to thirty-seven and the ballast of rounds
  thirty-eight to forty are withdrawn on the operator's word after
  round forty, for one policy a solo operator can hold in mind, the
  quota the bound and the floor the warning; what the design
  promises is not that the work volume never fills but that nothing
  of a run is lost when it does. The retention is a rename and needs
  no space, so an ending row's clone is preserved, frozen, with
  every ignored file in it, on a full volume; the disposable,
  unlinked before it, is the space the builds took and the first
  space returned; the ignored copy and the kept archive, second
  copies of what the retained clone and the kept worktree already
  hold, wait in `ending` with `no space` until the space returns,
  and the nightly job removes no clone whose row's `end_step` is
  before the release, so a clone whose copy is waiting outlives the
  thirty days. A session's `TMPDIR` and `XDG_CACHE_HOME` point under
  the clone, above, so a build's scratch and a package manager's
  cache land under the quota; what a provider writes under `HOME`
  outside them lands on the operator's volume, and that volume is
  the operator's own: the runner reads the floor there too and
  pauses dispatch under it with the same email, and a row write that
  fails there is the store's own failure, B's requirement 9, emailed
  by that path, the stated limit. A volume per run with a quota of
  its own would put each clone on its own device, which the
  retention's rename cannot cross, and is declined for the one work
  volume; the thirty days, the forty and the one work volume are
  reversible.
  The nightly job's removal at thirty days takes an exclusive `flock`
  on `<assignment>/<run>/clone.lock` beside the clone, which `restore
  --run` holds shared for the length of its copy, so that a run being
  restored is skipped naming it and removed the next night, the
  `lsof` skip standing beside it for a reader that holds no lock,
  from D's round thirty-three; a copy or an archive that fails with
  `ENOSPC` all the same keeps its row
  `ending` naming `no space`, rechecked on every tick, Today naming
  the floor, the free space and the space the copy needs for the
  operator to free or to lower the floor, and nothing is removed to
  make it fit, since a row
  released with its files unpreserved would have lost them. The row names the
  archive or the retained clone beside the path. `sd worktree
  restore <assignment>` recreates a lost kept worktree from its
  run's archive
  by extracting it to the clone's path when nothing is there, and
  `restore <assignment> --run <n>` copies a retained run's clone, while
  it stands, thawed, to `<assignment>.run<n>` beside that path and never
  over a clone, from D's rounds twenty-eight and thirty-one, for the operator to
  take a stash or a branch out of, the retained clone itself readable
  in place and immutable; and
  the clone comes back as it was left, staged and unstaged apart, an
  unresolved merge still unresolved with its stages for `git merge
  --continue`, a stopped rebase still stopped for `git rebase
  --continue` or `--abort`, and every LFS payload in its store.
  `discard` and `resume` each retain the clone as the clean end
  does, from D's rounds twenty-seven and thirty-one, so that a stash or a branch
  the operator left in a kept worktree is in the retained clone for
  its thirty days; `resume`
  retains the clone only after the item's branch, the commits the operator made
  in the kept worktree included, is pushed and the remote reports the
  head, so that the requeued run starts from commits the remote
  holds; a push that fails keeps the
  worktree and archives it again so that the archive holds the
  commits the operator just made and not the session's state from before
  them, and refuses naming the reason. The archive is one operation,
  idempotent as above, the
  same wherever a worktree is retained: at a row's end, after a failed
  push in `resume` or in the durability gate, and nightly by
  `sd-db-backup` for every kept worktree, because the operator edits in a
  kept worktree between those moments and an archive is only as current as
  its last write. A dated database
  file may name an archive the nightly job has since pruned; `sd worktree
  restore` then finds the branch on the remote at or past the row's head,
  or the row's merge confirmed on the default branch, from D's round
  thirteen, and says so instead of failing. The row names the path and the file count, and the next row on that item
  refuses naming the kept path, since the kept worktree holds the
  branch's lease, requirement 4. Two ways out, both on the item screen and in
  the terminal: `discard`, `sd worktree discard <assignment>`, removes the
  worktree with its changes, and it is the same journaled end run as
  `resume`, from D's round fifteen: the row goes `ending` with `discard`
  as the outcome recorded, in one transaction, and the idempotent steps
  follow, the clone retained, the run's preserved ignored files
  removed, each step passing over what is already done,
  the retained clone kept for its thirty days, and
  the lease released in the transaction that writes the row to its
  recorded outcome, so that a runner that dies between the removal and
  the release finds an `ending` row on its start and finishes it, and
  never a terminal row holding a lease on a worktree that no longer
  exists, which the start reconciles for `running` and `ending` rows
  alone; `resume`, `sd worktree resume <assignment>`,
  checks the worktree is clean by the same rule, everything committed and
  nothing untracked, ignored files aside, and refuses naming the dirty
  files when it is not; when it is, it is the ordinary end run again,
  from D's round twelve: the row goes `ending` with `resume` as the
  outcome recorded, in one transaction, and the same idempotent steps
  follow, the push gate confirming the remote head, the clone
  retained, ignored files
  copied to the backup path by the same rule as any row's end,
  since a deliverable under `storage/` in a kept worktree exists there
  and in the archive alone, a push makes neither durable and the
  retained clone goes at thirty days, and the lease released in the
  transaction that requeues the row on a fresh worktree from those
  commits, so that a runner that dies anywhere in between finds an
  `ending` row on its start and reruns the steps, and never a terminal
  row with a lease and no worktree. The repository's lane is released either way, since a
  kept worktree holds its branch's lease and nothing else. With survivors,
  it fails closed: the repository is quarantined, the row's lease held,
  nothing dispatched into the repository, from D's rounds ten and
  eleven, the worktree stays as
  it is for the operator to look at, the row stays `ending` with its
  recorded outcome untouched and the quarantine and the pids named on
  it, from D's round six, since a row written terminal here would be
  one the restart rule no longer reruns; the item screen and Today show
  the quarantine from that row, and the runner
  rechecks on every tick and on every start, an `ending` row being what
  the start reruns, lifts it, then cleans up as above, once the
  survivors are gone, by the operator's hand or their own exit, and the
  row reaches its outcome only then. Removal is
  cleanup, not containment; the containment is that the repository is not
  released while anything the runner can see still holds it. A process
  that holds nothing under either path at the moment of the check is
  outside what the runner can see, and this design says so rather than
  claiming otherwise. A successful row is never turned into a failed one
  by a process that was not its own.
  Interactive sessions and restored `herdr` agents are writers the runner
  cannot see, so the branch name is not isolation; the worktree is. The
  session gets the item's artifacts and open notes as the brief, through
  `claude -p`, `codex exec`, or the provider's `start` line, and an
  environment that is the entry's `env` list from the registry, item A,
  plus `PATH`, `HOME`, `LANG`, `TERM` and `TMPDIR`, and nothing else,
  requirement 3, `TMPDIR` and, from D's round forty, `XDG_CACHE_HOME`
  set to a directory of the run's own under the clone,
  `<clone>/.sd-run/tmp` and `<clone>/.sd-run/cache`, on the work
  volume, listed as disposable
  and gone with the disposable, so that a build's scratch and a
  package manager's cache that honors the variable land under the
  quota and not on the operator's volume; `HOME` stays the
  operator's, since a provider's credentials live there, and what a
  provider writes under it outside those two is the stated limit,
  which the floor on the database's volume, below, watches;
- claims the row before anything runs, in an order that leaves no window
  in which a provider runs with no `running` row, from D's round three,
  and no window in which a clone, a branch on the remote or a merge
  exists with no row that owns it, from D's round thirty-nine: the
  claim's transaction writes the row `running` with `start_step`
  `claimed` before any file or remote is touched; the runner then
  spawns the supervisor, with nothing started, and writes its own
  pid and the supervisor's pid, process group and start time on the
  row, `start_step` `supervised`, in one transaction, and from
  there every effect of the setup runs inside the supervisor's
  group and on a line from the runner, from D's round forty: the
  clone, the branch's publication, run from the clone and never
  from the operator's checkout, and the dispatch merge, each
  started by the supervisor on its own acknowledgement line, each
  reported done on the supervisor's stdout, and each recorded on
  the row as it completes, `cloned`, `branched`, `merged`, so that
  a runner killed inside any of them leaves a `running` row whose
  leader the restart rule below finds and kills with its group, a
  push half sent included, because a setup step journaled and not
  fenced could finish a push after the lease was released and
  another assignment had started, and a holder of the operator's
  checkout is exempt from the quarantine; the row goes through the
  end run as any, the directory at the clone's path retained in
  whatever state the kill left it, a rename asking nothing of it,
  the lease released, and the assignment's next dispatch finding
  its path empty; a partial clone is never removed in place and
  never reused. The supervisor's stdin is a pipe the runner holds,
  and a pipe that closes before the next line ends the supervisor:
  it kills what it started, waits for its group to empty and
  exits, at a setup step as at the provider. After the merge the
  runner writes `start_step` `started` in one
  transaction; and only after that commit
  it writes one acknowledgement line down the pipe. The supervisor
  starts the provider on that line and on nothing else: when the pipe
  closes before the line arrives, the runner died between the spawn and
  the commit or between the commit and the write, and the supervisor
  exits having started nothing, so a provider never spends or edits
  under a row that does not say `running`, and a row that says
  `running` with a dead supervisor is the restart rule's case below.
  Each child is a small supervisor the runner ships, leading
  a process group of its own: it starts the provider inside that group,
  waits for it, then kills whatever the provider left behind in the group,
  and exits only when the group is empty, so the leader's lifetime is the
  group's lifetime by construction and one assignment can be killed without
  touching another. The runner kills every group it claimed when it exits
  on its own. On start, for every `running` row: when the leader is alive
  and its start time matches the row, the runner kills the group; when the
  leader is gone, it lists the group's remaining members and names them on
  the row for the operator to end; a row whose `start_step` is
  `claimed` has no leader and no group yet and goes the gone leader's
  way with nothing to list, and one at `supervised` or later has
  the supervisor's group to kill or to list as any row does, from
  D's rounds thirty-nine and forty. Either way it then runs the survivor
  check above and either cleans up and releases the repository or
  quarantines it. And for every `ending` row, a runner that died
  mid-cleanup, it runs the cleanup again from its first step, from D's
  round five, each step finding its own work done or not, and the row
  reaches the outcome the end recorded, never a different one, since
  the outcome was decided by the session and not by the cleanup. A
  recycled pid is never killed, because the start time is
  checked before any kill, read from `kern.proc.pid`'s `p_starttime`
  and not from `ps -o lstart`, whose resolution is one second and
  would leave a collision window a pid recycled inside the same
  second falls through, from D's round forty-two. The row's recorded outcome is `blocked` with a
  `runner restarted` note, written when its cleanup completes as every
  outcome is, and nothing is dispatched for that item until the
  operator requeues it from the item screen, into a fresh worktree. One row
  `running` per repository is the serialization for serial rows, and the
  restart rule keeps it true;
- records started, ended, provider, and cost on the row, and every followup,
  decision and proposal the session writes as `note` rows;
- stops on the hard stops item A names, a failing test, a blocking review
  finding open past the cap, a write outside the repository, and marks the
  item `blocked` with the reason;
- ends at pull-request-ready, always, and releases the repository with its
  session. Under `merge_policy: manual` it marks the item `ready_to_send`
  and stops, and the runner watches the pull request from then on, below.
  Under `merge_policy: auto` it creates a `merge` row for the
  pull request, role `merge`, no provider, `parent` the author row, which
  the runner takes in queue order when nothing else runs in that
  repository: merge the default branch into the branch, by the same rule
  as at dispatch, once; then review the combined head once, `sd-review
  --scope branch --challenge`, unless the head after the update is
  exactly the `reviewed_head` the author row recorded when its own
  branch review passed, from D's round eighteen, since two changes each
  reviewed alone were never reviewed together, and a commit anyone
  pushed to the branch between the author's end and this row's start
  was reviewed by nobody, which a rule that asked whether the update
  made a commit would miss whenever the pushed commit already carried
  the default branch; a blocking finding
  ends the row with the item `blocked` naming it; wait for CI; merge
  naming the head that passed both; `git fetch -p`. The
  update is bounded because the row holds the repository's serial merge
  lane from the update to the merge, so no merge of the runner's own can
  move the base under it; a hand can, and then GitHub refuses the merge,
  the row updates once more, and a third move ends the row `blocked`
  naming it. From D's round one and A's rounds seventeen and eighteen:
  three protection settings with no update were tried on 2026-09-05 and
  withdrawn the same day, because two parallel authors that each pass
  alone can merge without a conflict and break the default branch, found
  after delivery with a successor already dispatched onto it. A conflict
  in the update ends the row with the item `blocked` naming the files, and
  the runner resolves none. The merge goes through `sd-ship`, which
  at that moment asks the remote again whether the repository is the
  operator's alone and whether the default branch is protected, item A; a
  no ends the row with the item `ready_to_send` and the answer named,
  `merge_policy` notwithstanding. The
  local queue is not the remote's safety boundary, because a head can move
  without the runner, a push from a session or a bot's rebase, so every
  merge names the head it validated, the `sha` GitHub's merge call takes,
  and GitHub refuses when the head has moved. The review and the CI
  result are recorded on the row against the exact `sha` each passed on,
  and a head that moved invalidates both: the row re-validates the new
  head from the branch review on, then CI, then the merge, once more, and
  never merges a revision that did not pass both under its own `sha`. A
  phase completed for one head is no phase for another. Each of those is a remote side effect
  that cannot be taken back, so the row records its `phase` as each one
  completes, `updated`, `ci_passed`, `merged`, and a merge row
  requeued after a restart reconciles against the remote before it acts:
  it asks GitHub for the pull request's state and `merge_commit_sha`,
  skips the merge when GitHub says merged, and only then does what is
  left, and it asks before it resolves a branch or makes a clone, from
  D's round twenty-one, because a merge GitHub accepted with
  `delete_branch_on_merge` and a runner dead before it recorded the
  phase leaves a branch that the remote no longer has and the checkout
  may never have had, requirement 1, and a dispatch that resolved the
  branch first would refuse the row naming it with the work delivered
  and every successor waiting; so a merge row whose pull request GitHub
  reports merged finishes from that evidence alone, records the
  `merge_commit_sha` and `merged`, and needs no worktree; there is no closure, from A's rounds thirty-one and
  thirty-three, and no file carries a status. A merge GitHub has accepted is never attempted twice, and a merge row that finds the work delivered but the
  row behind finishes the row rather than the work. An author never merges
  from inside its own session, so a running author never waits on the
  repository it holds.

### Requirement 2 — a selection runs as a batch

A selection of items runs as a batch, from the Backlog or Today list with
the two bulk actions `Run sequential` and `Run parallel`, or from the
terminal as `sd run --sequential <ids>` and `sd run --parallel <ids>`. Both
create one `assignment` per selected item, role `author`, brief the item's
artifacts and open notes. Sequential chains them in selection order: each
row carries `after`, the previous row's id, and the runner dispatches a row
only when its `after` row is delivered. Delivered is one barrier under both
policies: the `after` row ended `done`, and a `merge` row whose `parent` is
that row has recorded phase `merged`, GitHub having confirmed the product
pull request's merge. An author's end is completion, not delivery; the
confirmed merge is delivery, and nothing follows it, from B's round
twenty-nine and A's rounds thirty-one and thirty-three: no status is
written into a file, and the merge message is the record.
Until a merge row reaches `merged`, the successor stays
`queued` and Today shows it waiting on the predecessor's merge, by item
name. The `after` row ending any way but `done`, its merge row ending
before `merged` any way but `done`, or its pull request closing unmerged,
marks every successor `blocked` with a `predecessor <id>
ended <status>` note in the same transaction that records the end, and
the operator requeues from there. Under `auto` the merge row is created at
the author's end; under `manual` the author row ends `done` with the item
`ready_to_send`, the operator merges its pull request by hand, the watch
below creates the merge row, and the successor waits for it, by the
operator's decision on 2026-09-05:
sequential then means one thing under both policies, the successor's diff
and review are made on the merged base and not on a stacked branch that a
squash merge would leave to conflict, and the wait shows on Today as the
chain waiting on the operator, by item name, which is true. To see the
merge, the runner watches the pull request of every `ready_to_send` item,
whatever its repository's policy, since an `auto` repository whose safety
check answered no ends `ready_to_send` too and is merged by hand from
there: policy decides who may merge, not who observes a merge. The watch
asks the GitHub question a requeued merge row asks, every few minutes and
at every runner start; when GitHub says
merged, it creates a `merge` row for the item, `parent` the author row,
starting at phase `merged`, which is the delivery the successor waits on;
the successor then
starts, with the merged default branch merged in at dispatch as for every
author. A pull request closed without a merge ends the item `blocked` with
the pull request named and every successor `blocked` with the `predecessor`
note. Before the runner exists,
the next `sd-ship` run in the repository confirms the merge the same way,
item A. A chain is checked acyclic when it is created. Parallel marks each row `lane: parallel`, and
the runner starts every parallel row at once, regardless of repository:
the operator chose the set, and no cap second-guesses the choice; what the
machine and the providers can carry is visible on Today as the rows run.
`serial` rows keep the one-per-repository rule. The two
lanes exclude each other per repository, as readers and a writer do:
parallel author rows share a repository's remote and nothing on this
disk, since each runs in a clone of its own, requirement 1, with its
own refs and its own fetch, so that one session's `fetch -p`, branch
deletion or `gc` reaches no other, from D's round six, and a parallel
author's clone carries a pre-push guard the runner owns, `core.hooksPath`
set in the clone's config to a directory under `~/.local/share/sd/hooks/`
that no session edits by editing the repository, whose `pre-push`
refuses every push that is not the item's branch to the branch of
the same name, every deletion and every force, naming the refspec,
records the refusal on the row, and otherwise runs the repository's
own hook of the same name from the directory the clone's
`sd.hooksForward` names, the copy of the checkout's effective hooks
directory of requirement 1, and from nowhere else, with the same
arguments and standard input, and every other hook name in the
runner's directory does nothing but run the repository's, from D's
rounds twenty-four and twenty-five, because `core.hooksPath`
replaces the whole directory and not one hook, and a repository
whose `pre-push` uploads Git LFS objects would push pointers without
them under a guard that stood alone, because two authors
share one remote and one credential, and a clone isolates this disk
and not the remote, so an author that pushed `--delete` on another
item's branch or onto `main` would reach past the lane while both
sessions ran as they should; the hook guards a session's mistake and
not an adversary, `--no-verify` passes it, which is the limit stated;
a serial, `exec` or `merge` row holds the lane and gets no such hook; a serial row, an
`exec` row and a `merge` row
take the repository alone in the runner's lane, one at a time, because
a merge to `main` and a repository-wide act on the remote, a branch
deletion, a force, reach what every clone fetches, and the lane is the
order they land in; each runs in a clone of its own like a parallel
row, from D's round eight, since the operator's checkout is never the
runner's to hold and the lane serializes rows and not the operator. The remote is shared by construction, and two clones
fetching it at once contend for nothing on this disk; a push from one
that the other's branch does not carry is the ordinary case the merge
lane below resolves. An `exec` row whose entry has `supervisor`
scope, B's requirement 5, runs in the runner's own process against the
assignment it names and makes no worktree, which is how a kept worktree
is discarded or resumed from the palette; it takes the repository alone
like any `exec` row. An `exec` row of `control` scope, kill,
quarantine-clear and runner restart, takes nothing and needs no runner:
B's dashboard process acts on it at once, from B's round thirty-eight,
kills the named group after the start-time check and, in the same
transaction, writes the row `ending` with `killed by operator` as the
outcome recorded, from D's round eighteen, and leaves the rest to the
ordinary end run, which the runner's next tick or start performs on
every `ending` row, the survivor check, the retention or the archive, the preserved
files and the release, and only then the terminal
`blocked` with the note, because a row written terminal by the kill
would be one the start never reconciles, its lease held and its dirty
work archived by nothing; the runner reads a lifted quarantine on
its next tick. So the operator can end a row or free a quarantined
repository from the iPad while an author in the same repository keeps
running, and while this runner is hung or stopped. A parallel row starts only when
nothing serial,
`exec` or `merge` is running in its repository, and a serial, `exec` or
`merge` row starts only when nothing at all is; among the rows that are
eligible, the ones with no `after` or with a delivered `after`, whichever
was queued first holds the next turn, and a parallel row does not start
while an eligible serial, `exec` or `merge` row queued before it waits
for the repository to empty, from D's round fourteen, because without
that barrier a merge queued behind one running author would wait for
every parallel author queued after it too, and a steady supply of them
would hold it off without bound, which no assignment timeout limits;
with it the running authors drain, the exclusive row takes its turn,
and the parallel rows behind it start after. So neither lane starves
the other.
A row waiting on its `after` holds no turn and reserves nothing: a
successor queued before its predecessor's merge row exists would
otherwise hold the repository against the very merge row it waits on,
and the batch would stand still. No row ever
upgrades shared access to exclusive while it runs: an author that needs
the repository alone ends and leaves a `merge` row behind, above. Three rules keep
parallel rows from colliding or stalling, and they are the operator's own
parallelism doctrine written into the runner:

- Writers are isolated. Every row runs on its own branch in a clone of
  its own, requirement
  1, and an author or `merge` row holds a lease on the pair, repository
  and branch, from D's rounds eight and nine: the claim's transaction
  takes it, unique across every such row that is `queued`, `running` or
  `ending` and every kept worktree not yet discarded, whatever batch
  created them, and a row whose branch is leased is refused at creation
  naming the holder, because clones bypass git's check that a branch is
  checked out once and two of them would author and push one branch; an
  `exec` row of `supervisor` scope names the assignment it acts on and
  works under that assignment's lease, taking none of its own, which is
  how `discard`, `resume` and `restore` reach a kept worktree whose
  lease is the one they release. A selection with two items
  on one branch is refused naming both,
  and an item with no branch is refused naming it; the runner never runs two
  rows in one worktree, and a clean worktree never outlives its row.
- One serial merge lane. Two parallel rows in one repository land through
  their `merge` rows one at a time, after both authors have ended: the
  second merges the new `main` in, reviews the combined head once, reruns
  CI, and a conflict marks that item `blocked` with the conflicting paths
  named. Nothing merges
  concurrently in one repository, and nothing merges while it holds the
  repository as an author.
- Explicit budgets, no silent death. Every assignment carries
  `budget_minutes`, default ninety from the `system` row and typed on the
  run dialog when the operator wants another number, and `budget_usd`
  when the operator typed one, no default, accepted only on a row whose
  author is a `url` entry and refused naming a `start` entry, because
  B's library enforces it by reserving before every call it makes and
  it makes none of a session's, from B's rounds thirty-one and
  thirty-two; on a `url` row every call, the merge row's integration
  review included, reserves against it, and the row ends `blocked` with
  `budget spent` at the call that would pass it; at the budget the
  runner kills that assignment's own process group, no other, and, in
  the same transaction, writes the row `ending` with `blocked` and a
  `timed out` note as the outcome recorded, from D's round
  twenty-five, and the ordinary end run finishes it, the survivor
  check, the retention or the archive, the preserved files and the release,
  the terminal `blocked` written last, exactly as a `control` kill
  does, because a row written `blocked` at the kill is one the start
  never reconciles, and a runner that died after it left the lease
  held and the dirty work archived by nothing; the rest keep running.
  Parallel rows never wait on each other, and a row in a chain waits only
  on its `after`, so the queue cannot deadlock.

No cost cap on assignments by default. Cost is logged per assignment and
shown. The one standing cap is on the capped provider bill, B's requirement
6; a batch shows an estimate before it runs and, when the operator typed
`budget_usd`, a bound, B's requirement 5. The runner never asks a
question; it records what it decided.


### Requirement 3 — the runner has a pulse, stays awake, and holds only named keys

Three of the six backbone things B's requirement 9 asked for on 2026-09-05
belong to the runner and moved here with it.

- **The runner has a pulse.** Every tick the runner writes one `runner`
  row, its pid, the tick's time, and the pack version it runs, replacing
  the last. Today shows the last tick; a tick older than three intervals
  shows on Today as the runner silent, and `local-health-check` gains a
  check that runs `sd runner status`, which exits non-zero on a stale or
  missing heartbeat, so the nightly email names a runner that is loaded
  and doing nothing, which a loaded-and-exited-clean check cannot see.
- **Sleep does not kill a session.** The runner starts each session under
  `caffeinate -i -w <pid>` so idle sleep waits for it; the budget stays
  wall-clock because the machine stays awake. A lid closed on battery is
  beyond that, and a session cut by forced sleep is handled as a killed one
  is, survivors checked, worktree kept when dirty, row `blocked` with the
  reason, never silently restarted.
- **Keys reach a headless session, and only the named ones.** The runner's
  launchd plist runs it through the login shell with
  `~/.config/shell/env.sh` sourced, the one place the operator keeps keys,
  so the runner sees what a terminal sees; a session inherits less: the
  variables its registry entry names under `env`, item A, and the base
  five, `PATH`, `HOME`, `LANG`, `TERM`, `TMPDIR`, with `XDG_CACHE_HOME`
  beside them from D's round forty, both pointed under the clone,
  requirement 1, and nothing else, asked
  for on 2026-09-05, so that a session for one vendor does not inherit
  another vendor's key. Inheritance is all it is, from A's round
  nineteen: the session runs as the operator in the operator's `HOME`,
  can read the environment file, and a login shell it starts sources it
  back, so a session that wants another key can have it, and criterion 5
  asserts the gap rather than hiding it. `machine-setup.sh doctor` names each enabled entry whose
  variable is unset, without printing a value, B's requirement 9. The cron
  jobs keep the CLI's stored credentials, as today.

## Landing order

Last, as slice five of the order B's `prd.md` records under the same
heading: B's harness, library and migrations; A's ship path and
protection; B's dashboard, read-only and then writing; then this item.
Before it lands, an assignment row is created and shown but dispatched by
nothing, and a merge the operator makes is confirmed by the next
`sd-ship` run alone.

## Acceptance criteria

1. The runner starts a session for a queued assignment in a worktree under
   `~/.local/share/sd/worktrees/<item>/<assignment>`, records provider,
   start, end, cost and the worktree path on the row, and writes at least
   one `note` from the session. The cost comes from one of two places and
   the runner knows which by the entry's kind, from D's round forty-two:
   on a `url` entry every call is one B's library made and reserved, so
   the row's cost is the sum of its `run` and `bound` rows and the runner
   writes none of them; on a `start` entry the library is in no call's
   path, so the runner reads the total the session reports at its exit and
   writes one `run` row itself, the assignment and the pass on it, which is
   what B's open question 8 settled and what B's clauses 15.13 and 15.16
   assert. A test with a `start`-line provider whose script calls a vendor
   double three times asserts one `run` row carrying the session's reported
   total, no per-call row, and that same row summing into B's cost per
   shipped item. A test runs it against a fixture repository
   with a provider whose `start` is a script that writes a known note,
   asserts the operator's checkout is untouched including its uncommitted
   edits, asserts a second assignment on the same item gets a fresh worktree
   and the first's, clean, is gone, and asserts an assignment whose branch
   is checked out in the operator's checkout with uncommitted edits
   starts in its own clone at the remote's head, the edits untouched
   and absent from the clone, from D's round eleven. A test's provider script commits its source change and leaves
   `__pycache__` and a `.venv` behind, both ignored by the fixture; the test
   asserts the worktree is removed, the item's `merge` row starts, and the
   merge completes; a variant's provider also writes a PDF under an
   ignored `storage/` path and asserts the worktree is still removed and
   the merge completes, the PDF sits under the backup path at its relative
   path, and the row and Today name it; a variant leaves a `dist/` behind
   and asserts it is preserved to the backup path in a fixture with no
   `disposable:` line and removed in one whose local block says
   `disposable: dist/`. A test's provider script commits and exits with the
   fixture remote refusing pushes; the test asserts the worktree is kept,
   an archive exists under the backup path, the row names the unpushed
   commit, and, with the local repository deleted, `sd worktree restore`
   brings the commit back from the archive; with the remote accepting, the
   same script's worktree is removed and the remote holds the commit. A
   test's provider script leaves a modified tracked
   file, an untracked text file and an untracked binary and exits; the test
   asserts
   the worktree is still there with all three intact, the row names the
   path and the count of three, and the next row on the item refuses
   naming the path; the test then forks: one branch runs `sd worktree
   discard` and asserts the worktree is gone and the next row starts on a
   worktree without the three, and, in a second run, kills the runner
   after the worktree is removed and before the lease is released,
   asserts the row is `ending` with `discard` recorded and the lease
   held, restarts the runner, and asserts the row reaches its recorded
   outcome, the lease is released, the preserved files are gone, the
   retained clone holds the three, and the next row on the item starts, from D's round
   fifteen; the other commits the three in the kept
   worktree, runs `sd worktree resume`, and asserts the worktree is gone,
   the requeued row's fresh worktree contains the commit, and a `resume`
   attempted before the commit refused naming the three files. A test runs
   two parallel authors in one repository, ends the first while the second
   is inside a `git` command, and asserts the first ends `done`, nothing is
   quarantined, and a serial row queued for the repository waits until the
   second ends; a test queues a `merge` row behind one running parallel
   author, then queues two more parallel authors for the repository, and
   asserts that neither starts while the merge row waits, that the merge
   row starts when the running author ends, and that both start after
   the merge row ends, in queue order, from D's round fourteen; a
   test's parallel provider runs `git push origin --delete <other>`,
   `git push origin HEAD:refs/heads/<other>` and a non-fast-forward
   push of the item's branch to its own name, and asserts each is
   refused by the hook naming the refspec, the fixture remote's
   branches byte-identical, the refusals on the row, and a plain
   fast-forward push of the item's branch accepted; the third case
   was written as `git push --force origin <branch>` until D's round
   forty-two, which ran a plain push and a forced push of one branch
   against a hook that dumped its input and got byte-identical argv,
   the same stdin shape and no force-related variable in the
   environment — a `pre-push` hook is not told about `--force`. What
   it can see is the rewind: the four fields it reads per ref carry
   the remote's old sha, and `git merge-base --is-ancestor <old
   remote sha> <new local sha>` exits non-zero exactly when the update
   is not a fast-forward. So the guard's rule is two clauses, refuse
   any refspec whose destination is not the item's branch, and refuse
   an update to the item's branch that is not a fast-forward, and a
   `--force` that happens to be a fast-forward passes it unseen,
   which is the same limit as `--no-verify` and changes nothing on
   the remote that a plain push would not have; an `exec` row's clone is asserted to carry no such hook;
   a fixture whose checkout alone carries a `.git/hooks/pre-push` and
   a `pre-commit` that each write a marker file, the remote and the
   clone as made carrying neither, is asserted to have both markers
   after a commit and
   a push in a parallel author's clone, the push's marker carrying
   the refspec the guard passed through, and none after a push the
   guard refused, and the same with the fixture checkout's hooks
   under a local `core.hooksPath` of `.husky/_`, relative, untracked
   and ignored, the clone as made having no such directory, the
   `pre-push` there sourcing a sibling `h` with no executable bit
   that writes the marker, a serial row's clone running them from
   its own `.husky/_` with no guard in front, a parallel row's from
   behind the guard with `core.hooksPath` the guard's and
   `sd.hooksForward` the copy, and a fixture with an absolute
   `core.hooksPath` outside the checkout running that directory's
   hook from the clone with nothing copied, from
   D's rounds twenty-four, twenty-five, thirty-two, thirty-seven and
   thirty-eight; a stranger process holding a file under the first's
   worktree is asserted to hold that row `ending` with its lease, to
   delay every queued row of the repository, the serial row included,
   to be named on Today with its command
   line, to stop neither running author, and to be alive after the
   runner's tick; an editor process holding a file under the operator's
   checkout's `.git` is asserted to delay no row, from D's rounds ten
   and eleven. A test's provider script starts a `setsid` writer whose
   working directory is the worktree, then exits; the test asserts that the
   row is `ending` with the quarantine naming the writer's pid and its
   recorded outcome unchanged, the worktree is still there, a
   row queued for the repository does not start, and Today shows the
   quarantine; it restarts the runner while the writer lives and asserts
   the quarantine holds and the row is still `ending`; it then kills the
   writer and asserts that on the next tick
   the worktree is gone, the lease is released, the row reaches its
   recorded outcome, and the queued row
   starts. A second test's writer holds an open file under the worktree's
   `.git` from elsewhere on the machine and asserts the same quarantine;
   a third test's writer does `setsid`, changes its working directory out
   of the worktree, keeps a file under `.git` open and keeps its
   environment, and asserts it is the row's own by `SD_ASSIGNMENT`, that
   no queued row starts while it lives, and that the quarantine clear
   kills it; a fourth does the same with its environment scrubbed and
   asserts it is a stranger, alive after the clear, named on Today with
   its command line. A test kills the runner at each point of a
   dirty row's end, after the row goes `ending` and before the survivor
   check, inside the archive's write with its partial half written,
   after the archive and before the release, and, in a clean variant,
   after the rename into the retained path and before the flags,
   after the flags and before the copy of ignored files, after the
   copy and before the
   release, and after the release and before the terminal status, and
   asserts on the next start that the row reaches the outcome the end
   recorded, that no `git`
   command ran against the retained clone, asserted with a recording `git` on the
   path, from D's round nineteen, that exactly one archive of a kept
   worktree exists
   under the name, no partial beside it, and lists to its end, from
   D's round twenty-six, that a retained clone is whole and immutable
   under its run's path, from D's round thirty-one, that the worktree is kept or gone as the
   rule says, that the lease and the lane are released, and that a row
   queued for the branch started at none of those points before the
   release, from D's round five. A test kills the runner at each of three points, after the
   supervisor is spawned and before the row is written, after the row is
   written and before the acknowledgement, and after the acknowledgement,
   with a provider script that writes a marker file on start, and asserts
   that at the first two points the marker never appears and the
   supervisor is gone, that at the first point no row says `running`,
   that at the second the next runner start finds the row `running` with
   its leader gone and marks it `blocked`, and that at the third the
   marker exists. A test kills the runner mid-assignment and
   asserts that the next runner start kills the child, whose process group
   is on the row, marks the row `blocked` with a `runner restarted` note,
   and dispatches nothing for that item until it is requeued; the same test
   recycles the child's pid before the restart and asserts the new process
   is not killed. A test makes the provider script exit leaving a
   background writer in its group and asserts the supervisor kills it and
   exits after it; another kills the supervisor itself, leaving the writer,
   and asserts the next runner start names the writer's pid on the row and
   quarantines the repository until it is gone. A test stops the
   runner cleanly and asserts every claimed child is gone before the runner
   exits.
2. The runner stops at pull-request-ready in a repository whose row has
   `merge_policy: manual`, and marks the item `ready_to_send`; it merges
   only under `auto`, through a `merge` row created when the author row
   ended, asserted by the author row's end preceding the merge row's start.
   Both asserted against the fixture repository. A test merges a
   `ready_to_send` item's pull request by hand on the fixture remote under
   `manual` and asserts the runner's next watch creates a `merge` row at
   phase `merged`, no second pull request is opened, and the row ends
   `done`; another closes the pull request unmerged and asserts the item
   is `blocked` with the pull request named; a third does the same under
   `auto` for an item the safety check sent to `ready_to_send`, and
   asserts the hand merge is seen and the row ends `done`. A test kills the runner
   after each of the merge row's three side effects in turn,
   requeues the row, and asserts the pull request was merged exactly
   once, no second pull request was opened, the row's `phase` advanced
   past the point of the kill, and the item is `done`; a merge row
   requeued after GitHub already reports the pull request merged makes no
   merge call, asserted with a recording fixture, and one requeued
   after the fixture remote squash-merged and deleted the branch, the
   checkout never having had it, finishes `merged` with the
   `merge_commit_sha` recorded, makes no clone and resolves no
   branch, and its successor starts, from D's round twenty-one. A test pushes a new
   commit to the branch after review and CI passed and before the merge
   call, and asserts the pinned merge was refused, the branch review ran
   again on the new head, and, with a seeded blocking finding on that
   head, the row ends with the item `blocked` naming it and nothing
   merged, though the new head's CI passed; a second variant with a
   clean review asserts one merge naming the new head. A test merges an
   item and asserts Today shows it `done` at once and a successor
   chained on the item starts. A
   test moves the fixture's default branch after the author ended and
   asserts one update, one branch review over the combined head, CI and
   a merge naming that head; moved again by a fixture push during the CI
   wait, the merge is refused, the row updates once more and merges;
   moved a third time, the row ends `blocked` naming it, with two updates
   and no merge in the recording. A seeded conflict in the update ends
   the row with the item `blocked` naming the files and no merge call.
3. A failing test in the fixture repository marks the item `blocked` with the
   failure named in a note, and the branch is left in place.
4. Batch runs. A test selects three fixture items and runs them sequential,
   asserts three rows chained by `after`, that the second starts only after
   the first ends `done`, that the second's worktree contains a file the
   first's provider added and its merge landed, with both branches created
   before the batch, that the second's branch keeps every commit it had
   after the dispatch merge, that a second whose dispatch merge conflicts is
   `blocked` naming the file through `ending`, its `end_step` at the
   release and its clone retained, and starts no session, that a
   runner killed after the claim and before the supervisor, inside
   the clone, and after the branch's publication and before the
   merge
   leaves a `running` row at that `start_step` which the next runner
   start ends `blocked` with `runner restarted`, whatever stands at
   the path retained and the lease released, the assignment's next
   dispatch cloning afresh at an empty path, from D's round
   thirty-nine, that a runner killed while the branch's publication
   is a `git push` held open by a remote double has the push's
   process in the supervisor's group, the next runner start killing
   the group before the release, the double asserting the push never
   completed and no process of the setup alive after the release,
   and the push asserted to have run from the clone and not from the
   checkout, from D's round forty, and that when the second is made to fail the
   third is `blocked` with a `predecessor` note and never started; under
   `auto`, the second starts only after the first's `merge` row recorded
   phase `merged`, when the first's pull request is made to fail CI the
   second is `blocked` with a `predecessor` note and never started, and
   and nothing after the first's merge is waited on; under `manual`,
   the second is still `queued` and shown waiting on the first's merge
   when the first's author row has ended `done`, starts only after the
   first's pull request is merged by hand and the watch's merge row
   exists at phase `merged`, and is `blocked` with a `predecessor` note when that pull
   request is closed unmerged; the same eligibility query is asserted to
   serve both policies. A test runs two items sequential in one fixture
   repository under `auto` and asserts the first's `merge` row starts
   while the second is still queued behind its `after`, and that the
   second starts after it; the second, queued earlier than the merge row,
   is asserted to have held no turn. A test
   runs three items parallel across two fixture repositories and asserts
   all three are `running` at once, each in a clone of its own with no
   `.git/objects/info/alternates`, as is a serial row started after them,
   whose `gc --prune=now` and `fetch -p` leave the checkout's refs and
   packs byte-identical, that a second Run of an item whose row is
   `ending` or whose worktree is kept is refused naming the holder,
   that `discard` from the palette on that kept worktree is accepted
   as an `exec` row under the kept row's lease and the second Run is
   accepted after it, that a row on a branch the checkout holds
   two commits ahead of the remote starts at the checkout's head with
   the remote pushed, and is refused when the push fails, that after
   an author pushed from its clone the merge row and a `resume` start
   at the remote's head with the checkout left behind and untouched,
   and that a checkout diverged from the remote refuses the row naming
   both heads, from D's rounds
   eight and nine, that a row on a branch the checkout alone holds
   starts at the checkout's head with the branch published on the
   remote, and is refused naming the branch when the fixture remote
   gains a branch of that name between the check and the push, the
   remote's branch left as it was, from D's round sixteen, that a `resume` of a kept worktree holding an
   ignored `storage/report.pdf` lands the PDF under the backup path
   of that run, named on the row, copied from the retained clone,
   that the requeued run's provider writes a different
   `storage/report.pdf` and ends, and both PDFs sit under their own
   run's path with their content intact, that a runner killed inside
   the copy reruns to one copy under
   the run's path, equal to the retained clone's, from D's rounds
   seventeen and thirty-one, and that
   the runner killed after the retention and before the requeue finds
   the row `ending` on its start and requeues it with the lease
   released, from D's round twelve, that a `merge` row whose pull
   request the fixture remote squash-merged and whose branch it
   deleted passes the gate without a push, the retained clone holding the
   worktree's commits, the worktree gone from its path, the lease released and no
   branch recreated on the remote, from D's round thirteen, that a
   clean parallel row reaches
   `done` with its directory gone, its `node_modules/` under the
   runner's disposable list absent from the retained clone and a
   `storage/` file present in it, from D's round thirty-two, and the next serial row on that
   repository starts, that a dirty one is kept with its archive
   beside it, and that after the operator
   deletes the item's branch in the checkout and runs `gc
   --prune=now` there, a running clone's commit still reads and a
   kept clone's archive still restores, from D's round seven, and that
   both operators' checkouts are untouched, their refs and remote-tracking
   refs byte-identical before and after; a selection with two items on
   one branch is refused naming both; two parallel rows in one fixture
   repository whose provider scripts each run `git fetch -p` and one of
   them a branch deletion, released together at a rendezvous so the two run at the same moment, end `done`
   in one synchronised run with the other's refs untouched, from D's round six, reduced from twenty runs by the operator's decision of 2026-09-16 on sd:235: since C-13 the two clones are `--dissociate`d with no alternate, so they write to disjoint `.git` directories and the outcome is deterministic, and the one regression that reintroduces C-7, a linked worktree of the checkout, fails the byte-identity assertions above in one run — a linked worktree's `gc --prune=now` repacks the checkout's store and its `fetch -p` writes the checkout's remote-tracking refs — where the `alternates`-absent check alone would not, since a linked worktree's `.git` is a file and has no `objects/info/alternates` either. A test runs two parallel items in one
   fixture repository to merge and asserts that both author rows ended
   before either `merge` row started, that the second merged the first's merge in,
   ran the branch review on the combined head, asserted with a recording
   fixture that also asserts the first's merge row, whose update was a
   no-op and whose head equalled its author's `reviewed_head`, ran
   none, and merged after it, and that a commit the test pushes to
   the first's branch between its author's end and its merge row's
   start, one that already carries the default branch so the update
   is a no-op again, makes the merge row review that head, from D's
   round eighteen, that a seeded blocking finding
   on the second's combined head leaves it `blocked` naming the finding
   and unmerged, and that a seeded conflict leaves the second `blocked`
   with the path named. A test runs two parallel rows
   whose provider scripts sleep, sets `budget_minutes` to one on the first,
   and asserts that its child is gone and its row is `ending` with
   `timed out` recorded and then `blocked` with that note once the
   end run completes, while the second child is still alive and the
   runner is still running; a variant's first provider writes a
   dirty file before it sleeps, the runner is killed after the
   budget kill and before the end run, and the next start is asserted
   to find the row `ending`, archive the file, release the lease and
   write `blocked` with `timed out`, from D's round twenty-five. A test creates a palette request for a mutating entry
   and asserts an `exec` assignment exists, that it ran in the item's
   worktree and not the checkout, that queued while an `author` assignment
   on the same repository is running it waits, and that two parallel
   authors queued while it runs wait until it ends; an `exec` row queued
   while two parallel authors run starts only after both end. A read-only
   entry runs at once and writes no assignment. A `supervisor` entry, `sd
   worktree discard` on a kept worktree, runs as an `exec` row in the
   runner's own process with no worktree made, taking the repository
   alone, and the kept worktree is gone after it; a `worktree` entry on
   the same item while it is kept is refused naming the path. A `control`
   entry, kill, requested while an author runs and a parallel author
   shares the repository, and with this runner stopped, acts at once:
   the named group is gone, the row is `ending` with `killed by
   operator` recorded, the other author is untouched, and no worktree
   was made, and, in a variant whose killed session left dirty files
   with this runner stopped, that the row stays `ending` until the
   runner starts, and on its start the archive exists with the dirty
   files in it, the lease is released and the row is `blocked` with
   `killed by operator`, from D's round eighteen;
   clear on a quarantined repository kills the named survivor and this
   runner's first tick after it starts lifts the quarantine. A
   test gives the first of two parallel rows, both on a `url` author answered by the recording double,
   a `budget_usd` of one call's bound and a brief that needs two calls, and asserts it ends `blocked` with
   `budget spent` while the second, with no budget, completes; a `budget_usd` on a row whose author is a
   `start` entry is refused at creation naming the entry. [2026-09-16, owner decision on sd:235: the two-row
   `budget spent` test is assigned to sd:234 slice 8b, the runner's half of the reservation library, to be asserted there once a `url`-author execution path exists: the dispatch loop skips at `if not provider.start:` in `local-sd-runner/sd_runner/runtime.py` every entry without `start`, which every `url` entry is, so no path executes one today;
   the `start`-author refusal landed in #415, `source:local-sd-db/sd_db/runner.py::enqueue` refusing before any row is written, asserted by `source:local-sd-db/tests/test_runner_budget.py::test_a_budget_on_a_start_author_is_refused_at_creation_naming_the_entry`.]
5. Pulse, sleep and keys. A test runs the runner for three ticks and
   asserts one `runner` row whose time advanced each tick; a test ages the
   row past three intervals and asserts Today shows the runner silent and
   `sd runner status` exits non-zero; `local-health-check check` with that
   stub reports it. A test asserts each session's process tree under the
   runner contains a `caffeinate` holding the session's pid. A test renders
   the runner's plist and asserts it sources `~/.config/shell/env.sh`; a
   test starts a session for an entry naming one variable, with a provider
   script that writes its environment to a file, and asserts the file holds
   that variable, the base five, and no other variable from the runner's
   own environment, a second fixture variable set there among the absent;
   from a `bash -lc env` the same script runs, that second variable is
   present, recorded as the stated gap and not as a failure.
6. Retention, restore and storage. A test
   keeps a dirty worktree with a modified tracked file, an untracked file,
   a binary, one unpushed commit and one file whose staged version
   differs from both `HEAD`'s and the working tree's, asserts the
   archive exists under the backup path and its row names it, deletes
   the worktrees directory, runs `sd worktree restore`, and asserts
   all four are back and the fifth shows its three versions in
   `HEAD`, the index and the working tree, from D's rounds nineteen,
   twenty-one and twenty-six; a
   variant keeps a worktree whose update left one file in an
   unresolved merge, asserts the end reaches its recorded outcome
   with the archive written, restores it, and asserts `git ls-files
   --stage` shows the file's stages one, two and three with the
   blobs present, `MERGE_HEAD` is back and names a commit the
   restored store holds, and resolving the file and `git merge
   --continue` completes the merge, the clone deleted
   and the fixture remote made unreachable before the restore, from
   D's rounds twenty and twenty-two; a variant enables
   `core.splitIndex` in the kept worktree and stages a file so that a
   `sharedindex.*` exists, restores with the clone
   deleted, and asserts the index reads with the staged entry, from
   D's round twenty-three; a variant leaves a rebase stopped on a
   conflict in the worktree, asserts the row ends as dirty with the
   archive written, restores with the clone deleted, and asserts
   `git status` reports the rebase in progress on the same commit
   and `git rebase --abort` there completes, from D's rounds
   twenty-four and twenty-six; a variant in a fixture with Git LFS
   tracking `*.bin` commits one version of a binary, stages a
   second and writes a third to the working tree, asserts the end
   writes the archive, restores with the clone deleted and the
   fixture remote unreachable, and asserts the three versions read
   in full, `HEAD`'s through `git lfs checkout`, the staged one's
   payload present under `.git/lfs/objects/` by its pointer's oid,
   and the working tree's as written, from D's round twenty-six; a
   variant kills the runner inside the archive write and asserts a
   `.partial` beside the previous archive, the previous listing to
   its end, and the rerun replacing both with one whole archive,
   from D's round twenty-six; a variant's provider script stashes a
   change and exits with a clean tree and its commit pushed, and the
   test asserts the clone is retained whole and immutable under its
   run's path with the stash in `git stash list` read there, the
   path under the worktrees directory gone, and `restore --run 1`
   copying it beside the path thawed with the stash, and the same
   after the operator stashes in a kept worktree and runs `resume`,
   and, the requeued run then completing clean and retained, the two
   runs' clones both stand under the assignment's path by run, from
   D's rounds twenty-eight and thirty-one; a
   retained clone is asserted present after the row's end, untouched
   by the nightly job at twenty-nine days, and gone at thirty-one,
   named on Today, with a kept worktree's archive untouched at both,
   a first run's retained clone removed on its own clock while
   the second run's is inside its thirty days, one a test process
   holds a file open in skipped by the job naming the pid and
   removed the night after it closes, and one the job is killed
   inside removing finished the next night with no `git` run against
   it, from D's round thirty-one; the runner started with the
   worktrees directory and the backup path on two devices, a
   fixture volume mounted for it, is asserted to refuse naming both;
   the nightly job started while a fixture `git commit` is held
   mid-write in a kept worktree is asserted to keep the previous
   archive, name the skip on Today, and write a new one when run again
   with the commit done; the nightly job started while a dirty row's
   end holds the lock is asserted to wait and then archive again;
   and the end run of a row whose dirty clone the
   test keeps writing into is asserted to stay `ending` naming `clone
   busy` and the paths, and to complete on the tick after the writing
   stops, from D's round twenty-seven; a `resume` of a kept worktree
   in which the test holds a tracked file open across the retention is
   asserted to have the retention refused while it is held, the row
   `ending` naming `clone busy` and the path, and to complete on the
   tick after the descriptor closes, from D's round forty-two, which
   ran `chflags uchg` against a descriptor opened before it and got
   `write SUCCEEDED`; the same test then opens the retained file
   fresh and is asserted `EPERM`, saves a second file by
   write-and-rename at the old path and is asserted it lands nowhere
   under either path, and unlinks a child under the immutable parent
   and is asserted `EPERM`, the retained clone holding the file as
   it was, and the row requeued, from D's
   rounds twenty-nine to thirty-one; and a `resume` in which the
   test writes between the rename and the flags is asserted to have
   that write in the retained clone, the row's `end_step` read at `retained`
   on a rerun and the flags found set, from D's round twenty-nine;
   `discard`
   retains the clone; `resume` after a local commit with the fixture
   remote refusing the push keeps the worktree, refreshes the archive, and
   refuses naming the push, and with the local repository then deleted
   `restore` brings back that new commit; a kept worktree edited after
   its row ended has the edit in the archive after the nightly job; and
   with the push accepted `resume` retains the worktree only after
   the remote reports the head; a dated database file naming a
   retained clone the job has removed makes `restore` report the branch on the remote;
   with a `statvfs` double reporting the work volume under the floor,
   a queued row is asserted not to start, Today naming the floor and
   the free space and one email sent through the store's failure
   path and no second on the next tick, a running row to finish, its
   retention and its ignored copy complete and its lease released,
   and a row whose copy the double fails with `ENOSPC` to stay
   `ending` naming `no space` with its clone present and its partial
   copy in place through ten ticks, Today naming the floor, the free
   space and the space needed, every retained clone on the backup
   path present throughout, the oldest released run's included, the
   kept worktree's archive and every ignored copy untouched, and the
   copy completing on the tick after the double reports space, the
   row released with `end_step` at the release; with the double
   reporting no free space at all while two rows end, each row's
   clone is retained, frozen, with its ignored files in it and its
   disposable gone, both rows stay `ending` naming `no space`, the
   nightly job run past thirty days removes neither, and the copies
   complete on the tick after the double reports space; a fixture
   whose database path shares the worktrees directory's device makes
   the runner refuse to start naming the three paths; a provider
   script that writes a file under `$TMPDIR` and one under
   `$XDG_CACHE_HOME` has both under the clone's `.sd-run/` on the
   work volume and absent from the retained clone; and with the
   double reporting the database's volume under the floor, nothing
   dispatches and one email is sent until the double reports it
   above, from D's rounds thirty-two to forty and the operator's word
   after them; with a released
   run's clone at thirty days and a `restore --run` holding its
   `clone.lock` shared, the nightly job skips it naming the run and
   removes it the next night, from D's round thirty-three.
7. An item with no branch is refused naming it. A test creates an item
   with a registered repository and no branch and asserts readiness
   names it, that `runner.enqueue` and `runner_controls.enqueue` refuse
   naming it with nothing queued, and that a claim on a hand-made
   assignment refuses naming it, from the operator's decision of
   2026-09-16 on sd:235: `test_an_item_with_no_branch_is_refused_naming_it`
   in `local-sd-db/tests/test_runner.py`, against
   `source:local-sd-db/sd_db/runner.py::_item`.
8. A chain is checked acyclic when it is created. A test plants a cycle
   between two queued assignments and asserts the next `enqueue` is
   refused naming the assignment the walk met twice, with no row left
   behind, from the same decision:
   `test_a_chain_is_checked_acyclic_when_it_is_created` in
   `local-sd-db/tests/test_runner.py`, against
   `source:local-sd-db/sd_db/runner.py::_acyclic`;
   `test_cycle_rejected_before_claim` there covers the same walk at the
   claim.

## Open questions

None open. Reviews of this item were paused after round three, by the
operator's decision on 2026-09-05, until the spike had run, and resumed
the same day on the operator's word, before the spike, beside item C's
rounds; what the spike finds is recorded here as findings when it runs.
`budget_usd` has no default, settled the same day: a bound exists only
when the operator typed one.

## Log

- **2026-09-05** — Item opened on `feat/one-database-one-front-door`, split
  from item B by the operator's decision: B's requirement 4, criteria 9,
  10, 11 and 20, and the runner's three bullets and their tests from B's
  requirement 9 and criterion 22, moved here as they stood except where
  the entries below say otherwise. B's ledger, C-1 to C-52, stays on B;
  the findings that shaped the runner are readable there. This item's
  ledger starts at C-1 with its first review.
  - B's round twenty-nine, second finding, addressed here at the split:
    the delivery barrier waited for the closure pull request, so a closure
    whose CI failed blocked every successor although the code they needed
    was on the default branch. Addressed: delivery is the confirmed merge
    of the product pull request, phase `merged`; the closure follows, its
    failure blocks no successor, shows as `closure pending`, and is
    retried by the requeued row or the next `sd-ship` run. Requirement 2,
    criteria 2 and 4.
  - Item A's round seventeen, cross-item: the up-to-date protection
    setting was dropped and the automatic integration update with it, so
    a merge row asked whether the pull request was mergeable, waited for
    CI and merged as reviewed. Withdrawn in round one below.
  - Operator's decision, the same day: a session receives only the
    variables its registry entry names and a fixed base. Requirements 1
    and 3, criterion 5.
- **2026-09-05** — Planning review, round one of forty, run together with
  item B's round thirty in one prompt: one blocking finding on this item,
  addressed, and one cross-item change from B's C-53.
  - C-1, requirement 1: with no up-to-date requirement and no integration
    update, two parallel authors that each pass alone can change an
    interface and add a caller in different files, merge without a textual
    conflict, and break the default branch, which CI finds after delivery
    with a successor already dispatched onto it. Addressed by restoring
    what round seventeen of A removed, bounded this time: the merge row
    merges the default branch in once while it holds the serial merge
    lane, reviews the combined head once, waits for CI and merges naming
    that head; a hand-moved base gets one more update and a third move
    ends the row `blocked`. `phase` has `updated` again. Requirements 1
    and 2, criteria 2 and 4. Items A and B carry the same change.
  - B's C-53, cross-item: a palette entry of `supervisor` scope runs as an
    `exec` row in the runner's own process with no worktree, so a kept
    worktree can be discarded or resumed from the iPad. Requirement 2,
    criterion 4.
- **2026-09-05** — Planning review, round two of forty, with B's round
  thirty-one in the same prompt: no finding on this item; two cross-item
  changes from B's C-55 and C-56.
  - B's C-55: a `control` scope for kill and quarantine-clear acts on the
    runner's next tick with no exclusivity, so recovery works while an
    author runs or survivors hold the repository. Requirement 2, criterion
    4.
  - B's C-56: `budget_usd` on the row when the operator typed one, enforced
    by B's library on every call of the assignment. Requirement 2,
    criterion 4.
- **2026-09-05** — Planning review, round three of forty, with B's round
  thirty-two in the same prompt: one blocking finding on this item,
  addressed, and one cross-item change from B's C-57.
  - C-2, requirement 1: the claim wrote the child's pid and start time
    after the spawn, and the supervisor started the provider at once, so a
    runner that died between the spawn and the commit left a provider
    spending and editing under no `running` row. Addressed: the supervisor
    starts the provider only on an acknowledgement line the runner writes
    after the commit, and exits when the pipe closes first. Criterion 1
    kills the runner at all three points with a marker-file provider.
  - B's C-57: `budget_usd` is accepted only on a `url` author, whose every
    call the library reserves; refused naming a `start` entry. Requirement
    2, criterion 4.
- **2026-09-05** — Operator's decisions after round three: reviews of this
  item pause until the spike has run, and `budget_usd` has no default.
  Open questions records both.
- **2026-09-05** — B's C-67, cross-item while this item's reviews are
  paused: control entries act from B's dashboard process at once, not on
  this runner's tick, and work with this runner stopped; a `runner
  restart` control entry exists. Requirement 2, criterion 4.
- **2026-09-05** — A's C-61, cross-item while this item's reviews are
  paused: there is no closure pull request, so the merge row's phases
  end at `merged`, the requeued row looks for no closure branch, and a
  pull request closed unmerged names the pull request. Requirement 2,
  criterion 2.
- **2026-09-05** — A's round thirty-three, cross-item while this item's
  reviews are paused: no status is written into a file and there is no
  closure, so the dispatch merge's reason is A's attribution trailers,
  the merge row ends at the confirmed merge, Today shows a merged item
  `done` at once, and criterion 4's batch test checks the branch's
  commits, not a guard. Requirements 1, 2 and 3, criteria 2 and 4.
- **2026-09-05** — Planning review, round four of forty, resumed on the
  operator's word before the spike: two blocking findings, addressed.
  - C-3, requirement 1: a process outside the row's group that held only
    the shared `.git` was called a stranger and let parallel rows go on,
    so a child that did `setsid`, moved away and kept `.git` open
    authored beside them unquarantined. Addressed: the row's own are
    marked positively, group, worktree hold, and the `SD_ASSIGNMENT`
    the supervisor puts in the environment; every other `.git` holder is
    a stranger and quarantines the repository as an own survivor does,
    never killed, named on Today with its command line. Criterion 1
    adds the escaped child, marked and scrubbed.
  - C-4, requirement 1: `target/`, `dist/` and `build/` were disposable
    by name, and a session's only deliverable under one was deleted
    without the backup other ignored files get. Addressed: the runner's
    list is caches alone; a repository declares its own disposable paths
    in its local block; everything else ignored is preserved. Criterion
    1 preserves a `dist/` and removes a declared one.
- **2026-09-05** — Planning review, round five of forty: one blocking
  finding, addressed.
  - C-5, requirement 1: the cleanup at a row's end spanned the database
    and the filesystem with the terminal status written first, so a
    runner that died between the status and the bundle left work with no
    recovery copy, and the restart rule, reading `running` rows, skipped
    it. Addressed: the row goes `ending` with its outcome recorded, every
    cleanup step is idempotent, the terminal status is written last in
    the transaction that releases the repository, and the restart rule
    reruns the cleanup of every `ending` row. Criterion 1 kills the
    runner at six points of the cleanup.
- **2026-09-05** — Planning review, round six of forty: two blocking
  findings, addressed.
  - C-6, requirement 1: a quarantine wrote the row `blocked` before the
    cleanup, against round five's `ending` rule, so a restart during a
    quarantine missed the unfinished cleanup. Addressed: the row stays
    `ending` with its recorded outcome and the quarantine named on it,
    the start reruns it, and the outcome is written when cleanup and
    release complete. The restart note is an outcome the same way.
    Criterion 1 restarts the runner during a quarantine.
  - C-7, requirement 3: parallel authors in one repository ran in
    worktrees of one checkout, which share every ref and the object
    store, so a session's `fetch -p`, branch deletion or `gc` reached
    the other. Addressed: a parallel row runs in a clone of its own with
    the checkout's store as a read-only alternate — **superseded by C-13**,
    which found `--reference` alone still left the clone reading the
    checkout's store and moved to `--dissociate` with no alternate at all;
    the shipping text is `prd.md:49`, `design.md:24-25` and criterion 4's
    `alternates`-absent assertion. Recorded rather than rewritten, since a
    reviewer reasoning about the concurrent-fetch test lands on this entry
    first and must not take its design for the one being built; serial,
    `exec` and
    `merge` rows keep the worktree. Reversible by the operator: "serial
    per repository" drops parallel authors within a repository instead.
    Requirements 1 and 3, the doctrine bullet, criterion 4 with a
    concurrent fetch.
- **2026-09-05** — Planning review, round seven of forty: two blocking
  findings, addressed.
  - C-12, requirement 1: a parallel row's clone is a main worktree, and
    the cleanup removed every checkout with `git worktree remove
    --force`, which refuses one, so a clean parallel row stayed `ending`
    and held the repository. Addressed: the row records `worktree` or
    `clone`, the removal reads it, a clone's directory is removed, the
    snapshot ref lives in the clone and the bundle outlives it, restore
    recreates the kind named. Criterion 4.
  - C-13, requirement 1: `--reference` alone left the clone reading the
    checkout's store, and the operator's `gc` after a branch deletion
    there pruned objects the clone still needed. Addressed:
    `--dissociate`, the clone owning a copy of its objects, no
    alternate; one copy per parallel row is the cost. Criterion 4 runs
    `gc --prune=now` in the checkout under a running and a kept clone.
- **2026-09-05** — Planning review, round eight of forty: three blocking
  findings, addressed.
  - C-14, requirement 1: a serial row in a linked worktree of the
    checkout still shared its store and refs with the operator, whom the
    runner's lane never serializes, so the hybrid kept for serial rows
    the interference round six removed for parallel ones. Addressed:
    every row runs in a clone of its own; the linked worktree goes; the
    lane orders what lands on the remote. Reversible: a serial row in a
    linked worktree again, if the copy per row costs too much.
  - C-15, requirement 1: a clone of the remote carried no commit the
    operator made and did not push, where a worktree would have.
    Addressed: the runner pushes the checkout's branch before it clones,
    the row records the start commit, a failed push refuses the row.
  - C-16, requirement 4: clones bypass git's one-checkout-per-branch
    check, and the selection rule caught duplicates within one Run only,
    so two Runs, or a Run against a kept worktree, could author one
    branch. Addressed: a lease on repository and branch, taken in the
    claim's transaction, unique across `queued`, `running`, `ending` and
    kept rows across batches, refused at creation naming the holder.
    Criterion 4 covers all three.
- **2026-09-05** — Planning review, round nine of forty: two blocking
  findings, addressed.
  - C-17, requirement 1: pushing the checkout's head before every clone
    broke continuation, since an author that pushed from its clone left
    the checkout behind and the next row's push was a non-fast-forward.
    Addressed: the start commit is the remote's head; the checkout's
    commits are pushed first only when it is strictly ahead; divergence
    refuses the row naming both heads. Criterion 4 runs author-to-merge
    and `resume` with the checkout left behind.
  - C-18, requirement 4: the lease refused the `exec` row that
    `discard` and `resume` create on the same item, because the kept
    worktree held it. Addressed: author and `merge` rows hold leases;
    a `supervisor` `exec` row names its assignment and works under that
    lease, taking none. Criterion 4 discards a kept worktree from the
    palette with its lease present.
- **2026-09-05** — Planning review, round ten of forty: one blocking
  finding, addressed.
  - C-19, requirement 1: a stranger holding the checkout's `.git`
    quarantined the whole repository, a rule from when rows ran in
    worktrees of it, so the operator's editor stopped every queued row
    though no row shares a file with the checkout since round eight.
    Addressed: the quarantine holds the row, its lease and its worktree,
    and nothing else waits; a holder of the checkout's `.git` is
    nobody's concern. Criterion 4's stranger tests scope to the
    worktree and add the editor on the checkout.
- **2026-09-05** — Planning review, round eleven of forty: two blocking
  findings, addressed.
  - C-20, requirement 1: round ten narrowed the quarantine to the row,
    but a clone isolates local state and not the remote, and an escaped
    child can push or delete a branch while a merge row lands.
    Addressed: an own survivor or a stranger under the worktree
    quarantines the repository again; the checkout's `.git` alone is
    exempt, since no row runs there. Criterion 4.
  - C-21, criterion 1: a test still refused an assignment whose branch
    the operator's checkout had out, from the linked-worktree design.
    Addressed: the test starts it in a clone at the remote's head with
    the checkout's uncommitted edits untouched.
- **2026-09-05** — Planning review, round twelve of forty: two blocking
  findings, addressed together.
  - C-22, requirement 1: `resume` removed the kept worktree and its
    bundle once the branch was pushed, and an ignored deliverable under
    `storage/` existed in those two places alone. C-23, requirement 1:
    `resume` was a second destructive path with no `ending` state, so
    a crash between the removal and the requeue left a terminal row
    holding a lease. Addressed: `resume` is the ordinary end run, the
    row `ending` with `resume` as its outcome, the same steps, ignored
    files preserved, the lease released in the requeue transaction.
    Criterion 4 resumes with an ignored PDF and kills the runner before
    the requeue.
- **2026-09-05** — Planning review, round thirteen of forty: one blocking
  finding, addressed.
  - C-24, requirement 1: the durability gate demanded the branch's head
    on the remote for every row, and a `merge` row's squash merge with
    `delete_branch_on_merge` leaves no branch, so the gate pushed it
    back. Addressed: a confirmed merge passes the gate, the worktree's
    commits go into the bundle, no branch is recreated; `sd worktree
    restore` accepts the confirmed merge too. Criterion 4.
- **2026-09-05** — Planning review, round fourteen of forty: one blocking
  finding, addressed.
  - C-25, requirement 3: a queued `merge`, serial or `exec` row waited for
    the repository to empty, and parallel authors queued after it kept
    starting, so a steady supply of them held it off without bound.
    Addressed: a parallel row does not start while an eligible exclusive
    row queued before it waits; the running authors drain, the exclusive
    row takes its turn. Criterion 4 queues two authors behind a waiting
    merge row and asserts neither overtakes it.
- **2026-09-05** — Planning review, round fifteen of forty: one blocking
  finding, addressed.
  - C-26, requirement 2: `discard` removed the worktree, the bundle and
    the preserved files and released the lease with no journaled state,
    so a runner that died between the removal and the release left a
    terminal row holding a lease on nothing, which the start never
    reconciled. Addressed: `discard` is the same journaled end run as
    `resume`, the row `ending` with `discard` recorded, the lease
    released in the transaction that writes the outcome. Criterion 4
    kills the runner between the removal and the release.
- **2026-09-05** — Planning review, round sixteen of forty: one blocking
  finding, addressed.
  - C-27, requirement 1: the start commit was the remote's head, and an
    item's first row runs on a branch made in the checkout that nothing
    has pushed, so neither the fast-forward rule nor the clone had a
    remote head to start from. Addressed: a branch the checkout alone
    holds is published first with a lease expecting no remote branch,
    and the row starts at the pushed commit. Criterion 4 publishes and
    races the push.
- **2026-09-05** — Planning review, round seventeen of forty: one blocking
  finding, addressed.
  - C-28, requirement 2: preserved ignored files went under one path per
    assignment, and a `resume` requeues the same assignment, so a second
    run's `storage/report.pdf` met the first's, and either overwriting or
    passing over lost one. Addressed: the path carries a `run` counter
    the requeue increments; the move is copy then remove, its rerun
    copying over the run's own partial copy. Criterion 4 runs the same
    assignment twice and kills between copy and removal.
- **2026-09-05** — Planning review, round eighteen of forty: two blocking
  findings, addressed.
  - C-29, requirement 3: the merge row skipped the branch review when
    its update made no commit, and a commit pushed to the branch between
    the author's end and the merge row's start that already carried the
    default branch was reviewed by nobody. Addressed: the author row
    records `reviewed_head`, and the merge row skips the review only on
    an exact match. Criterion 4 pushes between the rows.
  - C-30, requirement 4: a `control` kill wrote the row `blocked` at
    once, and the start reconciles `running` and `ending` rows alone, so
    a kill with the runner stopped stranded the lease and bundled
    nothing. Addressed: the kill writes `ending` with `killed by
    operator` recorded, and the ordinary end run finishes it. Criterion
    4 kills with the runner stopped and dirty files, then restarts.
- **2026-09-05** — Planning review, round nineteen of forty: two
  blocking findings, addressed.
  - C-31, requirement 2: the removal was one recursive delete, and a
    runner killed inside it left a clone with half a `.git` that no
    rerun could confirm a head on or bundle. Addressed: the row records
    `end_step`, and the removal renames the clone to `.deleting` after
    every step that reads it, then removes without reading. Criterion 4
    kills inside the removal with a recording `git`.
  - C-32, requirement 2: the snapshot took the working tree alone, and a
    file staged in one version and written in another lost the staged
    one. Addressed: a second commit on `refs/sd/kept/<assignment>-index`
    holds the index tree, and `restore` reads it back. Criterion 4
    restores a file with three versions.
- **2026-09-05** — Planning review, round twenty of forty: one blocking
  finding, addressed.
  - C-33, requirement 2: the index snapshot was `git write-tree`, which
    fails on an unmerged index, and the updates can leave one, so the
    end could not finish its snapshot and the row stayed `ending` with
    its lease. Addressed: the index commit's tree holds the raw index,
    the merge-state files and every stage blob, and `restore` copies
    them back. Criterion 4 restores an unresolved merge and continues
    it.
- **2026-09-05** — Planning review, round twenty-one of forty: two
  blocking findings, addressed.
  - C-34, requirement 2: the index commit made the unmerged stages'
    blobs reachable and not the stage-zero ones, so a version staged
    and then overwritten was named by the raw index and carried by
    nothing. Addressed: a `stage0/` tree built from every stage-zero
    entry rides beside the stage trees. Criterion 4 restores after the
    store is deleted.
  - C-35, requirement 3: a requeued merge row resolved its branch before
    it asked GitHub, and a merge accepted with `delete_branch_on_merge`
    before the runner recorded it left no branch anywhere, so the row
    was refused with the work delivered. Addressed: the row asks first
    and finishes from the remote's evidence with no clone. Criterion 4.
- **2026-09-05** — Planning review, round twenty-two of forty: one
  blocking finding, addressed.
  - C-36, requirement 2: `MERGE_HEAD` was restored as a file naming a
    commit nothing in the bundle reached, so a conflicted update could
    not be continued from a restore. Addressed: the index commit takes
    every commit `MERGE_HEAD` names as a parent, so the bundle carries
    it. Criterion 4 restores with the store deleted and the remote
    unreachable.
- **2026-09-05** — Planning review, round twenty-three of forty: one
  blocking finding, addressed.
  - C-37, requirement 2: the raw index copy left a split index's
    `sharedindex.<hash>` behind, so a restore could not read it.
    Addressed: the snapshot stores a standalone copy made with
    `update-index --no-split-index` against the copy, the operator's
    index untouched, and the clones are made with `core.splitIndex`
    off. Criterion 4 restores a split index with the store deleted.
- **2026-09-05** — Planning review, round twenty-four of forty: two
  blocking findings, addressed.
  - C-38, requirement 3: parallel authors share one remote and one
    credential, and a clone isolates this disk and not the remote, so
    an author could delete another item's branch or push onto `main`
    past the lane. Addressed: a runner-owned pre-push hook in every
    parallel author's clone refuses every push but the item's branch
    to itself, deletions and forces included; `--no-verify` is the
    stated limit. Criterion 4.
  - C-39, requirement 2: the snapshot carried a merge in progress and
    no other operation. Addressed: a rebase, cherry-pick, revert or
    bisect in progress makes the end fail closed, the worktree kept in
    place until the operator finishes or aborts it. Reversible: an
    archive of the clone's directory is the alternative, for the
    operator to choose. Criterion 4.
- **2026-09-05** — Planning review, round twenty-five of forty: two
  blocking findings, addressed.
  - C-40, requirement 3: the budget kill wrote `blocked` at once, past
    the `ending` journal, so a runner that died after it left the lease
    held and the dirty work bundled by nothing. Addressed: the kill
    writes `ending` with `timed out` recorded and the ordinary end run
    finishes it, as a `control` kill does. Criterion 4 kills the runner
    between the budget kill and the end run.
  - C-41, requirement 3: `core.hooksPath` replaced every hook and not
    one, so a repository's own `pre-push`, Git LFS's for one, no longer
    ran. Addressed: the guard runs the repository's hook of the same
    name after its check, and every other hook name passes through.
    Criterion 4 asserts a fixture's own hooks still run.
- **2026-09-05** — Planning review, round twenty-six of forty: one
  blocking finding, addressed.
  - C-42, requirement 2: the snapshot put the working tree, the index,
    its stages, the merge state and the split index into the object
    graph one piece at a time so that a bundle could carry them, and
    a Git LFS payload staged and then overwritten lives under
    `.git/lfs/objects/` behind a pointer, outside the graph, so a
    restore put back the pointer without the file; the seventh such
    piece in eight rounds, and no list of them ends. Addressed: the
    archive of the clone's whole directory that round twenty-four
    named as the alternative replaces the snapshot ref, the index
    commit and the bundle; it carries everything in the directory,
    the rebase in progress that round twenty-four failed closed on
    included, so that restriction goes too; written to a partial and
    renamed whole. Criterion 4 restores three LFS versions, a stopped
    rebase, and a kill inside the write. Reversible: rounds nineteen
    to twenty-four are in this log, and the operator can restore the
    bundle at the cost of naming every piece of state outside the
    graph, which this round holds cannot be done.
- **2026-09-05** — Planning review, round twenty-seven of forty: two
  blocking findings, addressed.
  - C-43, requirement 2: a clean tree and a pushed head let the
    removal take the clone, and a session that stashed its unfinished
    change and exited satisfied both, so the only copy of the stash
    went with it; `resume` removed the archive the same way. Addressed:
    every removal the runner makes is preceded by the archive,
    `discard` and `resume` included, and the archive of a removed
    clone is pruned thirty days after the row went terminal, a kept
    worktree's never. Reversible: the thirty days. Criterion 4
    restores a stash and prunes at thirty-one days.
  - C-44, requirement 2: the nightly job archived a clone the operator
    could be committing into, and a `tar` of a directory git is
    writing can carry an index or a ref naming an object the walk
    missed, while the listing proves structure alone; and every writer
    shared one `.partial` with no exclusion. Addressed: every writer
    and `restore` hold an `flock` per assignment, released by death;
    an archive is taken only when a walk of the clone before and after
    the `tar` agrees on every path, size and mtime; the end run
    retries on every tick naming `clone busy`, the nightly job once
    and then skips naming it. Criterion 4 holds a commit mid-write.
- **2026-09-05** — Planning review, round twenty-eight of forty: one
  blocking finding, addressed.
  - C-45, requirement 2: one archive per assignment, and a `resume`
    requeues the assignment on a fresh clone from the remote, so the
    second run's end wrote over the first run's archive, the stash or
    side branch it was written to keep gone inside its thirty days.
    Addressed: the archive is keyed by assignment and run, under the
    path the preserved ignored files already use, each run's pruned on
    its own removal's clock; `restore --run <n>` extracts an earlier
    run beside the clone's path and never over a clone. Criterion 4
    resumes a clone with a stash, completes the second run, and
    restores the first.
- **2026-09-05** — Planning review, round twenty-nine of forty: one
  blocking finding, addressed.
  - C-46, requirement 2: the archive's lock excluded other archivers
    and not the operator, whom the plan lets edit a kept clone, so a
    save after the archive's second walk and before the removal was
    archived by nothing and removed with the clone, and no further
    walk closes that. Addressed: a clone to be removed is taken
    first, renamed to `.deleting` and made immutable with `chflags -R
    uchg` once the push gate has passed, the row recording `taken`;
    the archive and the move read the taken directory; the flags come
    off only after `deleting` is recorded. The rename takes the path
    the operator knows and the flag takes the file. **The clause "an
    open descriptor included" was false and is superseded by C-127**:
    `UF_IMMUTABLE` is checked at `open(2)`, so a descriptor taken
    before the flag still writes. The `lsof` check this same entry
    added for the removal is what closes it, and the retention now
    makes it too. A kept worktree is never frozen, since nothing
    removes it. Criterion 4's `EPERM` assertions are on a fresh open,
    an unlink under an immutable parent and a rename of the immutable
    directory, not on a write through a held descriptor.
- **2026-09-05** — Planning review, round thirty of forty: one blocking
  finding, addressed.
  - C-47, requirement 2: the flags came off before the removal, and a
    descriptor opened before the rename outlives it, so a write between
    the clearing and the unlink reached neither the archive nor the
    remote and the removal reported success. Addressed: before
    `deleting` is recorded the runner checks with `lsof` for any
    process holding a file or a directory under the taken directory
    and waits naming it, as the survivor check does, rechecked on every
    tick; then each file's flag comes off immediately before its own
    unlink, nothing between. A descriptor opened under `.deleting`
    between the check and the unlink is stated as outside what the
    runner sees, as the survivor check's limit is. Criterion 4 holds a
    descriptor across the freeze and asserts the removal waits.
- **2026-09-05** — Planning review, round thirty-one of forty: one
  blocking finding, addressed.
  - C-48, requirement 2: the flag that froze the clone froze its
    directories too, and an immutable parent refuses its children's
    removal, so the per-file thaw and unlink of round thirty could not
    finish and every clean row would have sat `ending` with its lease;
    and every thaw before an unlink reopens the window the freeze
    closed. Addressed: no clone is ever unlinked on the queue's path.
    Once the push gate has passed the clone is renamed whole into the
    retained path under its run, frozen there, the ignored files
    copied from it, and the row released; the nightly job removes a
    retained clone thirty days later, off the path any queued row
    waits on, skipping one a process holds and finishing the next
    night one it died inside. The archive of rounds twenty-six to
    thirty is a kept worktree's alone. `discard` and `resume` retain.
    The worktrees directory and the retained path must share a device.
    Criterion 4 reads a stash in the retained clone, prunes at
    thirty-one days, and starts the runner across two devices.
- **2026-09-05** — Planning review, round thirty-two of forty: two
  blocking findings, addressed.
  - C-49, requirement 2: a retained clone held its build caches thirty
    days, parallel dispatch has no cap, and nothing paused dispatch
    when the volume the live worktrees need ran low. Addressed: the
    disposable are removed before the retention, the one unlink on the
    queue's path; `free_floor_gb`, forty by default, is the free space
    the volume must keep, dispatch pauses under it naming it on Today
    while running rows finish and `ending` rows end, and the nightly
    job, or the runner on `ENOSPC`, removes retained clones oldest
    first back above it, never a kept worktree's archive or an ignored
    copy. Reversible: the forty. Criterion 6 runs under a `statvfs`
    double.
  - C-50, requirement 1: the clone is made from the remote and
    carries neither the checkout's `.git/hooks/` nor its local
    `core.hooksPath`, so the guard of rounds twenty-four and
    twenty-five forwarded to an empty directory and a Git LFS or
    validation hook installed in the checkout never ran. Addressed:
    at dispatch, before any provider starts, the checkout's executable
    hooks are copied into the clone and a local `core.hooksPath` is
    recorded as the guard's forward target. Criterion 4's fixture
    installs the hooks in the checkout alone.
- **2026-09-05** — Planning review, round thirty-three of forty: one
  blocking finding, addressed.
  - C-51, requirement 2: the removal of round thirty-two took retained
    clones oldest first, and the retention comes before the ignored
    copy, so a copy that failed with `ENOSPC` while its own clone was
    the oldest, or while another run's copy was half made, could remove
    the source the copy was preserving. Addressed: candidates are the
    retained clones of released rows alone, never the ending row's own,
    each removed under an exclusive `flock` on its `clone.lock` that
    `restore --run` holds shared for its copy; with no candidate the row
    stays `ending` naming `no space`, Today naming the floor, the free
    space and the space needed. Criterion 4 runs the only-clone case
    and the restore-in-progress case.
- **2026-09-05** — Planning review, round thirty-four of forty: one
  blocking finding, addressed.
  - C-52, requirement 2: the removal under the floor took a released
    run's retained clone inside the thirty days, and that clone is the
    one copy of a stash, a side branch, a reflog-only commit or a Git
    LFS payload the push did not carry, so disk pressure from an
    uncapped parallel batch could spend the copy the retention exists
    for. Addressed: no clone leaves before thirty days, whatever the
    free space; dispatch reserves twice the checkout's size on the row
    as `reserved_bytes` and starts a row only when the free space less
    the floor covers every running and `ending` row's reservation and
    its own; `ENOSPC` keeps the row `ending` naming `no space` for the
    operator, and nothing is removed to make it fit. The `clone.lock`
    of round thirty-three stays for the thirty-day removal. Reversible:
    the factor of two. Criterion 4 asserts every retained clone present
    through the `no space` case and the second of two queued rows
    waiting on the first's release.
- **2026-09-05** — Planning review, round thirty-five of forty: one
  blocking finding, addressed.
  - C-53, requirement 2: the reservation of round thirty-four measured
    the checkout and bounded nothing a provider writes, so a batch of
    small checkouts each installing a dependency tree could fill the
    volume between two dispatch checks with every reservation honored.
    Addressed: `run_disk_gb`, ten by default and per entry as
    `budget_usd` is, is the most a run's clone may grow; the runner
    reads each running clone with `du` on every tick as `disk_bytes`
    and ends a row past its allowance through `ending` with `disk
    limit`; the reservation includes the allowance; a tick under the
    floor with rows running ends the row that grew most, one per tick,
    with `no space`. Reversible: the ten. Criterion 4 runs a provider
    past the allowance and the floor crossing under two running rows.
- **2026-09-05** — Planning review, round thirty-six of forty: one
  blocking finding, addressed.
  - C-54, requirement 2: the reservation counted the growth allowance
    once, and the ignored copy or the kept archive of a grown clone can
    be as large as the clone, so four small checkouts at their
    allowances could be admitted and strand every one in `ending` when
    their copies needed twice what was left. Addressed: the
    reservation is twice the sum of the checkout's size and the
    allowance. Criterion 4 admits two of four rows under the doubles
    and ends both with their copies complete.
- **2026-09-05** — Planning review, round thirty-seven of forty: two
  blocking findings, addressed.
  - C-55, requirement 2: a `du` on a tick cannot hold the allowance or
    the floor against providers writing together inside one tick, and
    the text read as if it did. Addressed by saying what is promised:
    the bound is best effort between ticks, and the guarantee is that
    nothing of a run is lost when the volume fills, because the
    retention is a rename, the disposable is the first space returned,
    the ignored copy and the kept archive are second copies that wait
    in `ending` with `no space`, and the nightly job removes no clone
    whose row is before the release. A volume per run with an APFS
    quota is declined, since the retention's rename cannot cross
    devices; reversible. Criterion 4 runs two rows ending on a full
    volume.
  - C-56, requirement 1: recording the checkout's `core.hooksPath` on
    the clone carried a string and not a directory, and a relative,
    generated, ignored one such as `.husky/_` names nothing in the
    clone. Addressed: dispatch resolves the checkout's effective hooks
    directory with `git rev-parse --git-path hooks` and copies its
    executables into the clone's `.git/hooks/`, the one directory the
    guard forwards to, the config string never carried. Criterion 4's
    fixture uses an untracked `.husky/_`.
- **2026-09-05** — Planning review, round thirty-eight of forty: two
  blocking findings, addressed.
  - C-57, requirement 2: `sd.db` shares the volume the providers can
    fill inside a tick, and the runner's `ending` write is what the
    lossless promise of round thirty-seven rests on. Addressed: the
    runner writes a preallocated ballast file of `ballast_gb`, one by
    default and reversible, at start; a row write that fails with
    `SQLITE_FULL` or `ENOSPC` unlinks it, ends every running row with
    `no space`, writes, and recreates it when the free space is above
    the floor; a death between the unlink and the write is the
    dead-leader case the start already handles. Criterion 4 fails the
    first `ending` write under a database double and kills the runner
    between the unlink and the retry.
  - C-58, requirement 1: copying the executables alone out of the
    checkout's hooks directory lost a sibling helper without the bit
    and any path a hook resolves relative to its own directory.
    Addressed: the directory is copied whole, layout and modes kept,
    to the same place under the clone, `.git/hooks/` or the relative
    `core.hooksPath`, an absolute one used where it stands; the guard
    forwards to `sd.hooksForward`. A hard-coded absolute path is the
    stated limit. Criterion 1's `.husky/_` hook sources a helper.
- **2026-09-05** — Planning review, round thirty-nine of forty: two
  blocking findings, addressed.
  - C-59, requirement 2: the ballast of round thirty-eight returned
    its space to the volume, where the fastest writer took it before
    the journal's retry, and the double that failed one write could
    not show it. Addressed: the worktrees directory and the backup
    path live on one work volume the operator adds once with an APFS
    quota, `sd.db` stays on the operator's volume, the runner refuses
    to start when the database shares the worktrees directory's
    device, the floor and the allowance are read against the work
    volume, and the ballast is withdrawn. The one work volume is
    reversible. Criterion 4 fills the work volume under the double and
    asserts every row write lands.
  - C-60, requirement 1: the clone, the branch's publication and the
    dispatch merge ran before any row said `running`, so a runner
    killed inside them left a directory at the clone's fixed path
    that no restart reconciled, and a dispatch conflict wrote
    `blocked` outside the end run. Addressed: the claim's transaction
    writes `running` with `start_step` `claimed` before any effect,
    each setup step records itself, a row before `started` is routed
    through the end run by the restart rule as a gone leader, and a
    conflict ends through `ending`. Criterion 1 kills the runner at
    three setup points.
- **2026-09-05** — Planning review, round forty of forty: two blocking
  findings, addressed. The automatic rounds end here; every finding
  through this one is addressed, and the item waits on the operator's
  read before implementation approval.
  - C-61, requirement 2: a session's `HOME` and `TMPDIR` were the
    operator's, so a build's scratch and a package manager's cache
    filled the database's volume past the work volume's quota, unseen
    by `du` on the clone. Addressed: `TMPDIR` and `XDG_CACHE_HOME`
    point under the clone's `.sd-run/` on the work volume, disposable
    and counted; the floor is read against the database's volume too,
    ending the fastest grower and pausing dispatch under it; the
    ballast returns on the database's volume alone, unlinked only
    after every group is killed and every supervisor has exited, the
    order round thirty-nine found missing. Reversible: the one
    gigabyte. Criterion 4 fails the `ending` write and asserts the
    groups empty before the unlink.
  - C-62, requirement 1: the setup steps of round thirty-nine were
    journaled and not fenced, so a `git push` of the branch from the
    operator's checkout, whose holders the quarantine exempts, could
    finish after the lease was released. Addressed: the supervisor is
    spawned right after the claim, `start_step` `supervised`, and the
    clone, the publication, run from the clone, and the merge each run
    inside its group on a line from the runner, a closed pipe ending
    the supervisor with its group at any step; the restart rule kills
    or lists that group as any. Criterion 1 kills the runner under a
    push a remote double holds open.
- **2026-09-05** — Consolidation pass after round forty, no review: a
  read of requirement 1's retention and storage passage as a whole,
  patched piecewise over rounds nineteen to forty. Two references to
  "requirement 2" that meant this passage now say "below"; the
  ballast's history moved out of the requirement into this log, the
  rule stated once; a tick under the floor on the database's volume
  ends every running row, since no `du` measures a write under `HOME`
  and the fastest grower cannot be named there, and criterion 6 says
  the same; `.sd-run/` joined the disposable list it was said to be
  on.
- **2026-09-05** — Operator's decision after round forty: the simpler
  storage policy. One work volume with a quota holds the clones and
  the backup path, `sd.db` stays off it, a retained clone leaves at
  thirty days and never earlier, the floor pauses dispatch and emails
  the operator once per crossing, and a full volume leaves rows in
  `ending` with `no space` for the operator. Withdrawn: the growth
  allowance `run_disk_gb` and `disk_bytes`, the reservation
  `reserved_bytes`, the fastest-grower kill, and the ballast, of
  rounds thirty-four to forty. Kept: `TMPDIR` and `XDG_CACHE_HOME`
  under the clone, the disposable removed before retention, and the
  device rules. Criterion 6 reduced to the floor, the full volume,
  the device rule and the scratch paths. Reversible: the thirty days,
  the forty, the one work volume.

- **2026-09-05** — Operator's decision, no review: the free space floor
  doubles. `free_floor_gb` is forty by default, not twenty, and the
  `design.md` and `implement.md` follow it. Nothing else moves: the floor
  is still the warning and the quota still the bound, dispatch still
  pauses under it with one email per crossing, and a retained clone still
  leaves at thirty days and never earlier. The work volume's own size is
  unchanged because the `prd.md` never named one; `<size>` in the
  `diskutil apfs addVolume` line is the operator's to fill at setup. The
  forty stays reversible with the thirty days and the one work volume.

- **2026-09-05** — Adversarial planning review of `design.md` and
  `implement.md`, one lane, twenty-three findings. Both files had been
  reviewed once; the `prd.md` reached its forty-round cap without
  converging, so these two were the thinnest artifacts in the set and were
  reviewed hardest. The lane confirmed what it could before attacking:
  criterion 5's split across two halves is real and both halves are
  scheduled, and three of four filesystem claims check out. The findings
  are addressed in `implement.md` and `design.md`; those that reach this
  file are recorded here.
  - **C-41, the start condition was two slices early.** `implement.md` said
    the item starts after B's slice one and A's ship path, "which is the
    order both `prd.md` files record". It is not: B's landing order, this
    file at line 858 and `design.md` all put this item in slice five, after
    B's slice four. The sentence authorised opening PR 1 before the
    dashboard, the palette and cost exist, and B's slice four says in its
    own words that an assignment created there waits `queued` until this
    slice. Severity: blocking. Addressed.
  - **C-42, the batches pull request was ordered before the merge lane it
    needs.** `implement.md` justified the order with "a batch with no merge
    lane still runs under `manual`", and four paragraphs earlier explained
    that a chain under `manual` waits on a merge row nobody creates unless
    the watch exists. The delivery barrier is the predecessor `done` and its
    merge row at phase `merged` under both policies, so shipping batches
    first leaves every successor in every chain `queued` forever. Severity:
    blocking. Addressed: the merge lane is PR 5 and batches PR 6.
  - **C-43, three things were scheduled in no pull request at all.**
    `exec` row execution of `worktree` and `supervisor` scope, which
    criterion 4 asserts and without which `sd worktree discard` from the
    palette has no executor; the hard stops criterion 3 needs, since the end
    run records an outcome and does not decide it; and the row's own record
    of provider, start, end, cost and worktree path with the session's
    notes, which is criterion 1's opening sentence. Severity: material for
    each, blocking in aggregate, since criterion 4 could not pass with every
    pull request merged. Addressed: PR 7 is new and holds all three.
  - **C-44, the retention rename shipped a slice before the rule that makes
    it safe.** The three `st_dev` refusals were in PR 4 and the rename in
    PR 3, so the rename would land on every machine whose backup path is not
    yet on the work volume — which is every machine, the volume being PR 4's
    setup — and `EXDEV` mid-end-run leaves the clone at neither path. The
    `ENOSPC` path had its code in PR 3 and its test in PR 4 for the same
    reason. Severity: material. Addressed: both move to PR 3.
  - **C-45, the lease was used by three pull requests before the one that
    defined it.** PR 1's branch publication, PR 2's claim transaction and
    PR 3's kept worktree all take the lease that PR 5 introduced. Either a
    placeholder gets ripped out or three pull requests ship unguarded, which
    is the hazard the lease exists for, since clones bypass git's check that
    a branch is checked out once. Severity: material. Addressed: the lease
    is defined in PR 1.
  - **C-46, the database volume's floor was absent from both new files.**
    Requirement 1 reads the floor against the database's volume as well, and
    a tick under that floor ends every running row rather than letting them
    finish. Neither `design.md` nor `implement.md` mentioned it, so the one
    behaviour that distinguishes the two floors was being designed out
    silently. Severity: material. Addressed in both.
  - **C-47, nothing constrained the quota to exceed the floor.** `<size>` is
    the operator's to fill and no sentence relates it to `free_floor_gb`. A
    forty-gigabyte volume with the floor at forty stops dispatch from the
    first tick, sends one email, and never starts, with no error naming why.
    The floor's doubling to forty widened the trap. Severity: material.
    Addressed: the runner refuses to start when the quota does not clear the
    floor, alongside the three device refusals.
  - **C-48, `design.md` dropped the pass-through shims.** `core.hooksPath`
    replaces the whole directory and not one hook, so the runner's hooks
    directory needs a shim for every other hook name. `design.md` described
    only the `pre-push` guard's own forwarding and `implement.md` copied it,
    which would have silently removed every repository's `pre-commit` and
    `commit-msg` from every clone — the exact failure requirement 1 names as
    the reason the rule exists. Severity: material. Addressed in both, and
    the shims ship in PR 1 with the copy that creates the directory.
  - **C-49, the storage assertions are routed to the wrong criterion, three
    times.** Lines 1778, 1942 and 1955 all attribute the floor, the volume,
    the device rule and the `statvfs` double to criterion 4. Criterion 4
    contains none of them; every one is in criterion 5. The consequence is
    not cosmetic: the "ends every running row" behaviour of the database
    volume's floor has no criterion at all, in either place this file claims
    it does. Severity: material. **Not addressed**, because renumbering
    assertions across two criteria is an edit to this file's criteria list
    and belongs with the criterion 5 split it is tangled with. Recorded so
    the split, when it happens, carries this with it.
  - **C-50, two claims that said they were checked were not.** The standing
    lesson's label is `com.platypeeps.sdw-meter`, not `local.system-tools.sdw-meter`,
    in the one paragraph whose whole subject is a label that was listed and
    never loaded; and "all three under `docs/work/`" is two. Both were in a
    paragraph whose stated point was that it checked rather than assumed.
    Severity: minor each, material together, because a paragraph that claims
    verification and misses twice is worse than one that claims nothing.
    Addressed.
  - **C-51, the `statvfs` question no test can answer.** Every `statvfs`
    assertion in criterion 5 runs against a double, so nothing exercises the
    real call, and whether `statvfs` on a quota-limited APFS volume reports
    quota-remaining or container-remaining is settled nowhere. If it reports
    the container's, the floor never trips on a large disk and the warning
    half of the storage policy is inert. The reviewer flagged this as the
    one finding it could not test itself, which is the right way to report
    it. Severity: material. Addressed: PR 0's spike creates a small
    quota-limited volume, fills it, and records the answer before PR 4
    depends on it.
- **2026-09-05** — C-52, `implement.md`: the closure table carried two rows
  numbered 5, with different captions — "pulse, sleep, keys" and
  "retention, restore and the storage floors". The `prd.md` has five
  criteria, and criterion 5 is one long item covering pulse, sleep, keys,
  the `st_dev` refusal, the nightly archive and both floors. Two rows read
  as two criteria and would have let a reviewer count six. The retention
  and restore assertions are criteria 1 and 4's, not 5's. Addressed: one
  row for criterion 5 naming its three pull requests, and the archive,
  retention and restore clauses moved to the rows whose criteria assert
  them.
- **2026-09-05** — Adversarial re-review, round two, of the rewritten
  `implement.md`. Nine findings: three blocking, four material, two minor.
  Nine of round one's ten fixes hold. Two did not, and both failed the same
  way the originals did.
  - C-53, blocking: criterion 1's closure row omitted PR 5. Criterion 1
    asserts twice that the item's `merge` row starts and the merge completes
    (`prd.md:878-881`, `prd.md:911-916`), and the merge lane is PR 5's. The
    C-52 rewrite that was supposed to fix this table ran on a false premise
    and dropped a pull request while merging two rows. Addressed.
  - C-54, blocking: the nightly re-archive of every kept worktree
    (`prd.md:435-440`, asserted at `prd.md:1248-1252` and `1272-1273`) was
    scheduled by no pull request. The closure table credited it to PR 3,
    whose Touches exclude `sd-db-backup`, while PR 4 scoped its nightly job
    to removal alone. C-43's fix covered the archive at a row's end and
    missed the fourth moment. Addressed: the re-archive lands in PR 4, which
    is the pull request that edits that job.
  - C-55, blocking: PR 4's verification claimed criterion 5 asserts the
    database volume's floor ends every running row, while the same file's
    C-49 restatement says that behaviour has no criterion at all. Criterion
    5 at `prd.md:1298-1301` asserts only that nothing dispatches and one
    email is sent. Addressed: the verification claims what the criterion
    holds and names the gap rather than papering over it.
  - C-56, material, **and an open question for the operator, not resolved
    here.** Requirement 1 at `prd.md:391-394` says the runner reads the
    floor on the operator's volume and *pauses dispatch* under it with the
    same email. The consolidation pass at `prd.md:1939-1941` says a tick
    under the floor on the database's volume *ends every running row*. If
    those are the same volume — and both passages reason from writes under
    `HOME` that no `du` measures — the requirement contradicts its own log,
    and the criterion follows the requirement. The implementation pages now
    state what criterion 5 asserts. Which behaviour the operator wants is
    theirs to say.
  - C-57, material: four cross-document citations pointed at unrelated
    passages, two of them inside the paragraphs written to fix C-41 and to
    enumerate the cross-item dependencies. `B/prd.md:1110-1133` is the
    fixture-harness paragraph, not B's landing order (`1131-1157`, the
    slice-five sentence at `1152-1157`); `design.md:218-220` is pulse and
    keys, not the landing order (`227-234`); `B/prd.md:1061` is mid-sentence
    about sampling the clone, not the prune-after-backup rule
    (`1081-1082`); `design.md:171-174` is the parallel-lane paragraph, not
    `sd-ship`'s safety check (`208-211`); `prd.md:737-747` and `748-751` are
    the control-scope kill and the lane rules, not the worktree-scope and
    supervisor-scope rules (`718-725`, `728-733`). Round one's fix for the
    two false "checked" claims replaced prose with line numbers that were
    not read off the files. All corrected.
  - C-58, material: "the three `st_dev` refusals", written three times.
    There are two rules, at `prd.md:163-165` and `prd.md:354-356`, asserted
    at `prd.md:1245-1247` and `prd.md:1294-1295`. The count came from
    reading "the three paths" in the second rule as a count of refusals.
    Same defect class as C-50 and C-52. Corrected to two everywhere.
  - C-59, material: PR 5's verification claimed "criterion 2 whole" while
    criterion 2's two chained-successor assertions (`prd.md:1023`,
    `prd.md:1030-1031`) need `after` and the delivery barrier, which are
    PR 6's. The same file states the rule this breaks. Addressed.
  - C-60, minor: PR 1's Touches listed "the `sd worktree` commands", which
    its body never builds and which PR 3 claims in full. Removed, with the
    pointer to PR 3 left in its place.
  - C-61, minor: criterion 1's last sentence (`prd.md:999-1001`), a clean
    runner stop draining every claimed child before exit, was scheduled by
    no pull request. PR 8's `KeepAlive` is restart, not shutdown. Addressed:
    it is the supervisor's, so PR 2.
- **2026-09-05** — Adversarial re-review, round three. Sixteen findings: four
  blocking, five material, seven minor. Two of the four blocking findings are
  the closure table failing to be re-derived for the third round running, and
  two are work this file requires that no pull request built.
  - C-62, blocking: C-54 moved the nightly re-archive into PR 4's body and
    left criterion 5's closure row crediting "the nightly archive and its
    skip" to PR 3, whose Touches hold no `sd-db-backup` — the exact objection
    PR 4's own paragraph raises. The table contradicted the paragraph written
    to fix it, and C-54 is recorded "Addressed". Addressed, and the whole
    table was rebuilt clause by clause from the criteria rather than patched
    again: every previous patch was right about the clause it named and wrong
    about a neighbour.
  - C-63, blocking: criterion 4's closure row omitted PR 1 while criterion 4
    asserts PR 1's work four times — the clone's absent
    `.git/objects/info/alternates` and the checkout's byte-identical refs and
    packs at `prd.md:1078-1081`, and the three starting-commit cases at
    `prd.md:1086-1097`. PR 1's own Verification named criterion 1 only. This
    is C-53's shape exactly: that fix restored PR 5 to criterion 1 and
    stopped there. Addressed.
  - C-64, blocking: `prd.md:503-507` puts the session's scratch under
    `<clone>/.sd-run/tmp` and `<clone>/.sd-run/cache`, `prd.md:243` lists
    `.sd-run/` as disposable, and criterion 5 asserts both at
    `prd.md:1295-1298`. `implement.md` mentioned `.sd-run/` nowhere, and its
    only `TMPDIR`/`XDG_CACHE_HOME` sentence was PR 8's inheritance list,
    which is a different thing from repointing. Addressed: PR 4, beside the
    quota and the floors.
  - C-65, blocking: `sd run`, `sd worktree discard|resume|restore` and
    `sd runner status` are verb groups of the pack's `bin/sd`, in
    `sd-ai-command-pack`. `system` has no `bin/` and no `sd` binary, and the
    pack's `bin/sd:4` says it carries four groups today. Three pull requests
    add to it and no Touches line named a repository, the cross-boundary
    count said six, and the landing order was written as if one repository
    were involved. Addressed: PR 3, PR 6 and PR 8 are each a pair whose pack
    half lands first, and the count is seven.
  - C-66, material: the paragraph written for C-54 cited `prd.md:1271-1272`
    for an assertion that is at `prd.md:1251-1252`, inside the range it had
    already cited, and counted two where there are three. `prd.md:1271-1272`
    is "a kept worktree edited after its row ended has the edit in the
    archive after the nightly job" — the assertion that most directly
    motivates the re-archive, and the one going unnamed. C-57's class
    recurring inside C-54's fix. Addressed.
  - C-67, material: PR 6's Verification filed criterion 1's two lane tests
    (`prd.md:907-916`) under criterion 4, which contains neither; the closure
    table gave the second of them to PR 5, whose Verification never took it.
    Three sections of the page disagreed about who closes one pair of tests.
    Addressed in all three.
  - C-68, material: the closure table credited PR 6 with criterion 2's two
    chained-successor assertions and PR 6's Verification never named
    criterion 2. C-59 fixed PR 5's over-claim and added the PR 6 half of the
    row without re-deriving PR 6. Addressed.
  - C-69, material: "five of five KeepAlive agents on this machine set both"
    is a number carried out of a passage where it counted something else.
    `local.system-tools.sd-dashboard.plist:16` says "**Every other** KeepAlive agent on
    this machine sets both keys, five of five" — written when the dashboard
    complied with neither. It now sets both, so the comment is stale in its
    own file. Ground truth: `local-machine-setup/launchagents/` holds seven
    plists setting `KeepAlive`, six `<true/>` and all six setting both keys;
    on the machine, `homebrew.mxcl.herdr` and `homebrew.mxcl.moshi-hook` set
    neither and are not this repository's. C-58's class, in the one paragraph
    whose subject is checking before claiming. Addressed.
  - C-70, material: cross-item dependency 3 recorded agreement with item B
    about `control`-scope kills without reading B's text.
    `B/prd.md:789-792` had kill "marks the row `blocked`"; `prd.md:734-741`
    here forbids exactly that, "because a row written terminal by the kill
    would be one the start never reconciles", and criterion 4 asserts
    `ending` first. This item's reading is the correct one — it is the
    end-run invariant at `design.md:73-74`. Addressed here, and `B/prd.md` is
    corrected to match (B's ledger records it).
  - C-71 through C-76, minor, all citations or wording:
    `prd.md:770-772` cited for a hazard stated at `prd.md:775-776`;
    B's slice-five sentence labelled `1152-1157` when it begins at
    `B/prd.md:1153`; the defect section saying every device assertion is at
    `prd.md:1276-1301` when the first refusal is at `prd.md:1245-1247`, which
    the same page cites correctly elsewhere; PR 7's "Criterion 3 whole"
    against the table's "with PR 3 recording the outcome", which ordering
    rescues but wording did not; PR 3's quiet-clone rule framed as a `git gc`
    when `prd.md:322-330` makes it any disagreement between the two walks and
    the assertion at `prd.md:1253-1256` is a test writing into the clone; and
    criterion 5's row under-describing PR 3, which carries roughly fifty
    lines of archive, restore, split-index, LFS, rebase and stash assertions.
    All addressed.
  - Confirmed not re-raised: `run_disk_gb`, `disk_bytes`/`du` accounting,
    `reserved_bytes`, the fastest-grower kill and ballast appear only as
    withdrawals. `free_floor_gb` is forty throughout. C-58's corrected count
    of two `st_dev` refusals holds at all four sites.
  - Not reached, and recorded as not checked rather than clean: item C was
    not read (no dependency of this item references it), and this file was
    not re-verified internally beyond the passages `implement.md` and
    `design.md` cite.
- **2026-09-05** — Round-four adversarial re-review of `implement.md`, three
  blocking findings and seven material. The round-three pattern held: each
  blocking finding is a round-three fix that landed on the sentence its
  finding quoted and left the sentence that had to agree with it.
  - C-77, blocking, and the fix is item B's: C-70 corrected B's requirement
    prose and B's ledger and left B's acceptance criterion 12 asserting that
    a `control`-scope kill leaves the row `blocked` with the runner stopped —
    the state `prd.md:734-741` here forbids. Item B's round-four reviewer
    found the same thing independently. Corrected in B and recorded there as
    B's C-124: the criterion now asserts `ending` while the runner is
    stopped and the terminal `blocked` on the runner's first tick after it
    starts, matching criterion 4 here.
  - C-78, blocking: the C-62 table rebuild credited PR 7 with "palette,
    `exec`, supervisor and control scope", and PR 7's own body four lines
    above its Verification says control-scope rows are B's dashboard's and
    are not there, while cross-item dependency 3 assigns the runner's side to
    PR 3. The table was rebuilt from the Verification lines rather than from
    the criteria, which is how it came to credit a pull request with work
    that pull request disclaims. Corrected: criterion 4's control-scope
    runner half is PR 3's — the end run on any `ending` row and the
    quarantine lifted on the first tick — and PR 7's Verification now says
    so. Same shape as C-67, one round later.
  - C-79, blocking: C-63's own text records that "PR 1's own Verification
    named criterion 1 only" as part of the defect and marks it addressed.
    The table gained PR 1 and PR 1's Verification did not change. Written
    now from the criteria the table credits it with: criterion 4's
    `alternates` and byte-identity assertions (`prd.md:1078-1081`) and the
    starting-commit cases (`prd.md:1086-1097`).
  - C-80, material, addressed: the same defect at criterion 5. C-76 widened the row to
    "every retention, restore, split-index, LFS, rebase and stash assertion
    in the criterion's body" and PR 3's Verification still took only
    `ENOSPC`, the two `st_dev` refusals and the quiet-clone rule. Widened to
    match, `prd.md:1197-1232`.
  - C-81, material, addressed: PR 1 filed the `.husky/_` hooks test as "not in the
    criteria and should be". It is criterion 1's, at `prd.md:928-937`, and
    richer than the page described — the criterion also requires the
    serial/parallel split and a fixture with an absolute `core.hooksPath`
    outside the checkout running that directory's hook with nothing copied.
    Because the page called it an extra, the criterion's harder half was
    scheduled nowhere. Both now in PR 1's Verification.
  - C-82, material, corrected: C-65 raised the cross-boundary count to seven and did
    not add the seventh entry, so a "Seven dependencies" heading stood over a
    list of six. The missing one — the pack's `bin/sd` verb groups — is the
    single dependency that changes the landing order of three pull requests.
    Added as entry 7, and the count is now eight because of C-83.
  - C-83, material: the criteria assert what Today renders ten times, Today
    is B's screen, and `implement.md` named it once in passing. Most of the
    ten render from rows B's Today already draws; two do not — "named on
    Today with its command line" and "Today naming the floor, the free space
    and the space needed" are new fields on a screen B owns, reached by no
    Touches list on either page. Added as cross-boundary dependency 8 and
    recorded rather than scheduled, since which item adds the fields is B's
    landing-order question. No round before this one reached it.
  - C-84, material: C-72 corrected B's slice-five line from `1152` to
    `1154`; `1154` is slice **four**, slice five opens at `B/prd.md:1157`,
    and the enclosing range `1131-1157` stopped one line before it. Third
    consecutive round on one citation, and the first in which the range
    around it was re-derived: B's landing order is `B/prd.md:1134-1163`.
  - C-85, material: cross-item dependency 2 cited `B/prd.md:1080-1081` for
    "after the backup has passed". That is B's pulse-and-keys bullet; the
    nightly prune is at `B/prd.md:1084-1086`. C-57's exact class, inside the
    paragraph list C-57 was raised against and not among the four it fixed.
  - C-86, material, corrected: dependency 3 quoted `B/prd.md:789-792` in the present
    tense as saying kill "marks the row `blocked`" in the same round that
    changed those lines to say `ending`. Re-tensed, so the quotation reads as
    a record of what B said rather than a claim about what B says.
  - C-87, minor: the paragraph under the rebuilt closure table said criterion
    1 "needs five" against a row listing six. It described the table as it
    stood after C-53, not after C-62's rebuild. Corrected to six and the six
    named.
  - C-88, minor: C-69's measured facts are all correct — seven plists set
    `KeepAlive`, six `<true/>`, all six set both keys — and its argument for
    the dashboard comment being stale was a non-sequitur: "every **other**
    agent" excludes the dashboard by construction, so the dashboard's own
    compliance cannot falsify it. What makes the count stale is
    `local.system-tools.second.mcp-obsidian.plist`, a sixth other agent setting `KeepAlive`
    `<false/>` and neither key. Reasoning corrected in the paragraph whose
    subject is checking before claiming.
  - C-89, minor: the `prd.md` mis-routing section presented three sentences
    as the extent of the defect. A fourth exists outside storage:
    `prd.md:1884` calls the `.husky/_` helper-sourcing hook criterion 4's,
    and it is criterion 1's. Named alongside the three. Also recorded: C-63
    counted three starting-commit cases and PR 1's body enumerates four; the
    page now uses four and C-63's three is superseded, not replaced.
- **2026-09-05** — Decision recorded from item B, on the operator's word: the
  `sd` verbs are the pack's. This page already assumed it (C-65), so nothing
  here changes but the hedge: cross-item dependency 7 no longer says the
  pairing could go the other way. Noted alongside it, because the two items
  order their halves oppositely and a reader moving between them will trip on
  it: B's `system` half lands first, since B's pack verb calls `sd_db`; this
  item's pack half lands first, since the runner is the caller and the verb is
  what it calls.
- **2026-09-05** — `sd-docs-lint` gains rule 6. Every `prd.md:N` citation on
  this item is recorded in `.citations.tsv` against a snippet of the line it
  was written for, and the rule reports where the text moved. C-84, C-85 and
  C-89 were all this defect, and C-84 took three rounds because each fix was
  arithmetic on the previous wrong number rather than a read. The rule does
  not certify that a citation was right when recorded; it watches from there.
- **2026-09-05** — Targeted review of the criterion-4 and criterion-5 clauses
  four rounds had recorded as not reached. Three blocking, four material,
  three minor. Two of the four line ranges in the review brief were
  themselves stale and the reviewer corrected them before use, which is the
  defect this batch keeps producing and rule 6 now watches.
  - C-90b, blocking, addressed: the round-forty kill-during-push clause
    (`prd.md:1056-1062`) was taken by no Verification. PR 2 rules itself out
    in its own words — "it cannot assert what happens next, because 'then run
    the end run' is PR 3's" — and every assertion in the clause is about the
    release. PR 3's Verification paraphrased the round-*thirty-nine* clause
    at `prd.md:1052-1055` and stopped. The closure table credited it by
    category. Sixth instance of that defect. Landed in PR 3, which is the
    last of the three pull requests it straddles and the only one that can
    run the whole assertion.
  - C-91, material, addressed, part of the same clause: "a `git push` held open by a
    remote double" needs a fixture that accepts the connection and blocks
    inside `git-receive-pack`. Every other fixture remote in the item is
    ordinary. No pull request built it. Named in PR 3's body beside the
    assertion, with the double's own record of the client's `GIT_DIR` as the
    mechanism for "ran from the clone and not the checkout" — the pre-push
    guard's refspec record would also serve but is PR 6's and lands later.
  - C-92, blocking, addressed: "a selection with two items on one branch is refused
    naming both" (`prd.md:1123-1124`, requirement at `781-782`) is a
    **selection-time** refusal and the lease at `prd.md:774-776` is a
    **creation-time** one. The lease refuses the second row and leaves the
    first running; the criterion demands the whole selection refused with
    nothing created. A lease-only implementation looks right and fails the
    assertion. No pull request took it and `design.md` section 8 named its
    sibling validation, the acyclic check, and not this one — so the gap was
    at design level. Added to `design.md` and to PR 6's body and Verification.
  - C-93, material: `prd.md:782`'s "an item with no branch is refused naming
    it" and `prd.md:687`'s "A chain is checked acyclic when it is created"
    are asserted by no criterion anywhere. Both recorded in the `prd.md`
    defect section; adding the assertions is a `prd.md` edit.
  - C-94, material: the twenty-run concurrent-`fetch` clause
    (`prd.md:1124-1127`) was scheduled by no pull request, and as specified it
    is a flake generator rather than a race probe. The race was real because
    worktrees of one checkout share `.git`; `--dissociate` removed the
    sharing, so the outcome is now deterministic and twenty runs sample one
    point twenty times. A regression would not resurface through it either:
    `alternates` shares objects and never refs. The only regression that
    reintroduces C-7 is reverting to `git worktree add`, already caught in one
    deterministic run by `prd.md:1079-1081`. And "at the same moment" names no
    barrier, so the runs overlap by luck. Scheduled in PR 6 with a FIFO
    rendezvous, because unowned is worse than imperfect; the recommendation to
    reduce twenty runs to one synchronised run is recorded as a `prd.md` edit
    and left for the operator.
  - C-95, blocking, addressed: criterion 5's dated-database-file `restore` clause
    (`prd.md:1274-1275`) sits forty lines past the end of the range PR 3's
    Verification cites, and PR 4's Verification enumerates without it. The
    closure table's category phrase covered it. It does not straddle — the
    nightly removal is only the fixture's premise and every asserted
    behaviour is `restore`'s — so it lands in PR 3 as a separate range.
  - C-96, material: `prd.md:441-442` says the dated file may name "an archive
    the nightly job has since pruned"; the criterion says "a retained clone
    the job has removed". `prd.md:1239` asserts a kept worktree's archive is
    untouched at twenty-nine and thirty-one days, so the requirement's premise
    contradicts the retention rule its own criterion states. The criterion is
    right. Recorded as a `prd.md` defect; PR 3 is written to the criterion.
  - C-97, minor: C-7's Addressed sentence still describes the read-only
    alternate that C-13 replaced with `--dissociate`. Marked superseded rather
    than rewritten, because C-7 is the entry that motivates the
    concurrent-fetch clause and a reviewer reasoning about that test lands
    there first.
- **2026-09-05** — One clause arrives from item B's round five.
  - C-125, material: B's clause 15.7 greps the runner and `sd-review` for a
    cost insert and expects nothing. B credited the whole clause to its PR 8,
    where `local-sd-runner/` does not exist yet — an empty answer from an
    absent directory, which is a pass for the wrong reason and the shape this
    item has recorded before. The runner half is asserted in PR 7, which
    already writes the `exec` row's cost from B's library function, so the
    grep and the rule it protects sit in one pull request. The `sd-review`
    half stays with B's PR 8. B records it as hand-off 13.
  - C-126, decision, recorded: **B's open question 8 settled on 2026-09-05, and the
    writer is this item's runner.** "The usage read" is the total a `start`
    session reports at its exit; the thing that started the session reads
    it and writes one `run` row with the assignment and the pass. Nothing
    else knows an assignment stands behind the session, and this runner is
    what execs `claude -p`, `codex exec` or the entry's `start` line
    (`prd.md:498`). Criterion 1 already asserted the row records a cost and
    never said where the number comes from; it now names both paths and the
    branch between them — a `url` entry's cost is the sum of B's `run` and
    `bound` rows and the runner writes none of them, a `start` entry's is
    the single row the runner writes itself. B's clauses 15.13 and 15.16
    are asserted in PR 7, beside criterion 1's row record and B's clause
    15.7 grep, all three being the same rule seen from three sides: every
    charge goes through B's one library function. B records it as hand-off
    15.
- **2026-09-05** — Round forty-two, executable. Every concrete command and
  syscall the three pages name, run on this machine — Darwin 25.6.0, git
  2.50.1 — against throwaway fixtures. Four prose rounds had read these and
  executed none. Two of the findings are things macOS and git will not do.
  - C-127, blocking: **`chflags -R uchg` does not refuse a write through a
    descriptor already open.** `prd.md` said the flag "is the kernel's
    refusal of every write, a write through a file already open included",
    and criterion 4 asserted `EPERM` on exactly that write. `UF_IMMUTABLE`
    is checked at `open(2)`, not at `write(2)`. Run here: `chflags rc=0`,
    `write through open fd SUCCEEDED n=10`, then `fresh open FAILED:
    Operation not permitted`. The page's own argument said a write landing
    after the clone was read and before it left "would be archived by
    nothing and pushed by nothing, and no check closes that" — the
    `chflags` was the thing meant to close it. Corrected twice over: the
    prose now says what the flag does (`open`, `unlink`, `rename` refused;
    a held descriptor not), and the hole is closed by the `lsof` check
    C-47 already added for the nightly removal, now made before the
    retention too, with the `clone busy` refusal the end run of a dirty row
    already uses. The window between that check and the flag is stated as a
    limit beside `--no-verify`. Criterion 4's `EPERM` assertions move to a
    fresh open, an unlink under an immutable parent and a rename of the
    immutable directory, each of which the same run confirmed. C-46's and
    C-47's forward claim that the flag takes "an open descriptor included"
    is marked superseded rather than rewritten.
  - C-128, blocking: **a `pre-push` hook is not told about `--force`.**
    Criterion 1 asserted the guard refuses `git push --force origin
    <branch>` while accepting a plain push of the same branch. Run here: a
    plain push and a forced push of one branch hand the hook byte-identical
    argv, the same stdin shape and no force-related environment variable, so
    a guard reading them either accepts both or refuses both. What is
    detectable is the rewind, from the two shas the hook already reads:
    `git merge-base --is-ancestor <old remote sha> <new local sha>` exits
    non-zero exactly when the update is not a fast-forward. The criterion
    now asserts a non-fast-forward push of the item's branch, the guard's
    rule is written as its two clauses in `prd.md` and `design.md`, and a
    `--force` that is a fast-forward is recorded as passing unseen. The
    other two cases in that test, the delete and the push to another name,
    were confirmed distinguishable and stand.
  - C-129, material: **the runner writes ignored files into the clone at
    dispatch**, so "a worktree the runner made had none when it was made, so
    each is the session's" is false. The runner copies the checkout's
    effective hooks directory in, and `prd.md:74-75` names the case: a
    `core.hooksPath` such as `.husky/_`, relative, generated and ignored.
    Run here, the clone shows `!! .husky/` under `git status --porcelain
    --ignored`. The end run would preserve the runner's own dispatch
    artefact and show it on Today as the session's work, on every run of
    every repository that uses one. Corrected: the ignored copy skips the
    path `sd.hooksForward` already names on the clone's config.
  - C-130, material: **PR 3's two `st_dev` refusals refuse every start on
    this machine until PR 4's operator setup runs.** `~/.local/share`,
    `~/Documents` and `sd.db`'s volume are all `st_dev 16777229` today, one
    device, so the first refusal fires on the first start — and PR 3's own
    Verification needs a runner that starts. C-44 moved the refusals into
    PR 3 for a good reason and left the volume behind in PR 4. Corrected:
    the `diskutil apfs addVolume` setup moves to PR 3 with the refusals;
    PR 4 keeps the floors and the quota-versus-floor refusal, which are
    about how much room the volume has and not whether it exists.
  - C-131, minor, corrected: `flock(1)` does not ship on macOS, and `shlock(1)`, which
    does, is a pid file and is not released by death — which is the property
    `prd.md:316-318` names. All three holders of that lock are the runner's
    own Python, so the clause now says `fcntl.flock`.
  - C-132, minor, corrected: the recycled-pid rule checks a start time and named no
    source. `ps -o lstart` gives one-second resolution, which leaves a
    collision window; the clause now names `kern.proc.pid`'s `p_starttime`.
  - Recorded, not fixed: a provider that calls `setsid()` leaves the
    supervisor's process group, so `killpg` will not reach it.
    `prd.md:181-183` half-covers this by calling such a child "what a
    stranger looks like from outside", but the budget kill at `prd.md:801`
    reads as if the group is sufficient. The reviewer observed `setsid()`
    creating a new session and pgid but did not build the full
    supervisor-provider-killpg sequence, so it is a suspicion with one
    ingredient confirmed and not a finding.
  - Verified by execution and recorded as sound: `git rev-parse --git-path
    hooks` honouring `core.hooksPath` relative, absolute and from a
    subdirectory; `--reference <checkout> --dissociate` leaving no
    `alternates` and the operator's uncommitted edits absent from the clone;
    `--force-with-lease=<ref>:` with an empty expect rejecting a branch the
    remote gained, `! [rejected] ... (stale info)`; `git worktree remove`
    refusing a clone; `ps -E` showing `SD_ASSIGNMENT` across `setsid` and a
    `chdir`; `lsof +D` seeing both a working directory and an open file;
    `caffeinate -i -w` accepting a non-child pid and exiting with it; the
    FIFO rendezvous releasing both readers together, blocking on `open` and
    not on a read; `git status --porcelain` empty with `__pycache__`,
    `.venv` and an ignored `storage/` present; `git fetch -p` pruning only
    the deleted ref; a hook without the executable bit silently skipped,
    which is what makes the mode-preserving copy load-bearing; and
    `local-sd-runner/` absent from disk as the plan expects.
- **2026-09-07** — Round forty-three, PR 0, the spike. A few hundred lines
  against B's fixture harness taking one `queued` assignment from claim to
  release — clone, dispatch merge, provider start, end, survivor check,
  retention, release — on `spike/sd-runner`, read and thrown away. Two of
  the findings are ordering defects no prose round could reach, and three
  are the item's own vocabulary having nowhere to live. The `statvfs`
  question the plan reserved for this pull request is answered.
  - C-133, blocking: **the end run's survivor check finds the runner's own
    supervisor, and quarantines the repository on every successful row.**
    Section 5 makes the survivor check the first step of the end run and
    says a stranger's hold quarantines exactly as an own survivor's does;
    section 3 makes the supervisor a group leader that "exits only when the
    group is empty". Nothing in `design.md` or this page names the moment
    the runner joins it. Written to the ordering as stated, the first end
    run recorded one survivor: `91429 91429 … runner.py --supervisor 1`,
    the supervisor itself, alive by construction because its pipe was still
    open, and the row parked at `ending` with `end_step` `survivors`. This
    is not a race and no retry clears it: every row that ends normally
    quarantines its own repository, which is the failure the whole
    quarantine rule exists to make loud. Corrected: the runner closes the
    supervisor's pipe and waits for the leader to exit **before** the one
    transaction that moves the row `running` to `ending`, so the group is
    empty before the check that reads it. After the join the same fixture
    ran to `status=done`, `end_step=released`. The join belongs to PR 2,
    which owns the supervisor and the clean stop, and PR 3's Verification
    cannot pass without it.
  - C-134, blocking: **the durability gate has no case for a clone with no
    branch, and criterion 4 asserts exactly that kill.** Criterion 4 kills
    the runner "inside the clone" and asserts the next start leaves whatever
    stands at the path retained and the lease released. A row killed at
    `cloned` has a clone and no item branch — the publication is the next
    step — and section 4's gate reads "the branch's head on the remote, or,
    for a `merge` row, the merge confirmed by GitHub", with no third case.
    Run here, the next start raised `fatal: ambiguous argument 'feat/spike':
    unknown revision or path not in the working tree` and left the row
    `ending` at `end_step` `survivors` with the clone still on the queue
    path — the one outcome the round-thirty-nine clause forbids, since the
    assignment's next dispatch then finds its path occupied. Corrected: the
    gate is skipped, not failed, when the branch is absent from the clone;
    the retention and the release run as they do for any row. With that, a
    kill at each of `claimed`, `supervised`, `cloned` and `branched` left a
    `running` row at that `start_step` and the next start ended each
    `blocked` at `end_step` `released` with the queue path empty. PR 3.
  - C-135, blocking, addressed in sd_db migration 005: **the row has nowhere to record `start_step`,
    `end_step`, the worktree path, the supervisor's pid, group and start
    time, or the run counter, and no pull request adds one.** Section 1
    says the runner "reads and writes only through B's library". On `main`
    the `assignment` table has `id`, `item`, `role`, `provider`, `status`,
    `started`, `ended`, `cost`, `result`, `after`, `parent`, `lane`,
    `budget_minutes`, `budget_usd` and `phase`, and `update_assignment`'s
    allowed set is those columns and no others, raising `SdDbError` on any
    other name. A repository-wide search for `start_step`, `end_step`,
    `pgid`, `process_group`, `supervisor`, `quarantin`, `lease`,
    `pack_version` and `hooksForward` across all of `local-sd-db/sd_db/`
    returns nothing for every one of them, and migration 002 is a single
    `ALTER TABLE item ADD COLUMN source_commit TEXT` — nothing after 001
    touches `assignment` or `repo`. So the journaling spine that PR 2 and
    PR 3 are built on, and that criterion 4's kill assertions read back, has
    no column, and the quarantine and the lease have no home either. The
    spike folded the whole journal into `result` as JSON, which is the only
    writable free-text column, and that collides with the outcome the same
    column is specified to carry. This is a migration and a set of write
    functions in **B's** library, and it is owned by neither item: this
    page's implement opens "Nothing here builds B's library", and B's plan
    has no migration after 002. It has to be settled before PR 1 opens,
    because PR 1 already records `cloned`, `branched` and `merged` and
    already takes a lease.
  - C-136, material, addressed in sd_db `runner.py`: **there is no atomic claim, and `update_assignment`
    cannot be one.** Section 3 rests on a claim transaction that writes
    `running` before any effect, and one `running` row per repository is
    called the serialization. `update_assignment` issues an unconditional
    `UPDATE assignment SET … WHERE id = ?`, so two runners reading the same
    `queued` row both write `running` and both proceed. The spike could not
    express the claim through the library at all and issued
    `UPDATE assignment SET status = 'running' WHERE id = ? AND status =
    'queued'` against the connection directly, checking `rowcount`, which is
    the one thing B's rule that no caller issues SQL forbids and greps for.
    A compare-and-swap claim function in B's `writes.py` is the fix, and it
    lands with C-135's migration. PR 2 names the ordering; nothing names the
    primitive.
  - C-137, material: **the heartbeat is one row replaced per tick, and its
    only write function appends.** Section 10 says every tick writes one
    `runner` row "replacing the last", and B's criterion counts
    `heartbeat` rows one. The home B built for it is `state` with
    `kind` `heartbeat`, and `record_state` is a plain `INSERT`; nothing in
    the library updates or deletes a `state` row, `resolve_state` only
    stamps `resolved_at`. At any plausible tick interval the table grows
    without bound and B's own count assertion fails on the second tick.
    Separately, `design.md`, this page and implement all call it a `runner`
    row, which reads as a twelfth table — `schema.py`'s `TABLES` is a closed
    eleven and `tests/test_schema.py` asserts `len(TABLES) == 11`, so a
    literal reading of PR 8 breaks B's suite. The row is a `state` row of
    kind `heartbeat` and the three pages are corrected to say so; the
    replace-not-append behaviour is a write function B owes, with C-135's.
  - C-138, decision, confirmed: **`statvfs` on a `-quota` APFS volume reports
    quota-remaining, not container-remaining. The floor works and the
    warning half of the storage policy is not inert.** This was the one
    storage question reserved for the spike because every `statvfs`
    assertion in criterion 5 runs against a double. Settled by measurement,
    not by reading: `diskutil apfs addVolume disk3 APFS sd-spike-statvfs
    -quota 1g` into the existing container, which at the time had 83.7 GB
    unallocated and whose data volume `df` reported with 80 Gi available.
    `os.statvfs` on the new volume's mount point returned `f_frsize=4096
    f_blocks=244141 f_bavail=243938`, that is **1.00 GB total and 1.00 GB
    available**, against `f_blocks=242837545 f_bavail=20863596` — 994.66 GB
    total, 85.46 GB available — read at the same moment on `~`.
    The volume reports its quota and not its container, by three orders of
    magnitude. Filling it drove `f_bavail` down linearly — 789 MB, 580 MB,
    370 MB, 160 MB at each 200 MB written — and the write stopped with
    `errno 28 (ENOSPC)` after 985.7 MB while the container still held 80 GB.
    So `free_floor_gb` read with `statvfs` against the work volume measures
    the room the quota actually leaves, dispatch stops under it on a large
    disk as the design promises, and PR 4's floor needs no redesign. The
    volume was deleted with `diskutil apfs deleteVolume disk3s7`.
    Two riders. First, `f_bavail` did not reach zero at `ENOSPC`: the volume
    reported 13.3 MB available on the tick after the write failed, so a
    `statvfs` free-space read is an upper bound and roughly ten megabytes of
    what it reports cannot be written. Immaterial against a forty-gigabyte
    floor and stated here rather than discovered later. Second, and
    confirming PR 3's premise rather than PR 4's, `f_blocks * f_frsize` on
    the quota volume is the quota itself — 1.00 GB, not the container's
    994.66 GB — so PR 4's refusal to start when the volume's total size does
    not clear `free_floor_gb` is readable from the same call and needs no
    `diskutil` parse.
  - C-139, material: **the two `st_dev` refusals are satisfiable and the
    `EXDEV` hazard is real, both measured on the volume before it was
    deleted.** The new volume's `st_dev` was `16777236` against `16777229`
    for `~`, `~/Documents` and `~/.local/share` alike, so C-130's
    reading — that all three are one device today and the first refusal
    fires on the first start — is confirmed and the operator's volume is
    what separates them. A rename of a directory from the volume to
    `~/Documents` failed `errno 18 (EXDEV)`, and the same rename between two
    directories both on the volume succeeded. That is the retention step's
    whole argument, executed: PR 3 shipping the rename without the refusals
    would land `EXDEV` mid-end-run on every machine, and C-44's move of the
    refusals into PR 3 is confirmed correct rather than merely argued.
  - Verified by execution and recorded as sound: `kern.proc.pid`'s
    `p_starttime`, which C-132 named and no round had read — the first
    sixteen bytes of the `sysctl` answer are the `timeval`, and two
    processes started inside one second read back `618861` and `577310`
    microseconds, which is the collision `ps -o lstart` cannot see, so
    C-132's correction is executable and not merely better-sounding;
    `--reference … --dissociate` leaving no
    `.git/objects/info/alternates` in the retained clone and the operator's
    uncommitted edit untouched in the checkout across both runs; the hooks
    copy preserving mode `755` on a `pre-commit` and leaving `.sample` files
    out; `chflags -R uchg` refusing a fresh `open` for write on the retained
    clone with `EPERM`, consistent with C-127; and one assignment reaching
    `done` with `end_step` `released`, its clone absent from the queue path
    and present at the retained path, on two consecutive runs from clean
    fixtures.
  - Recorded, not fixed: an `exec` row is an `assignment` row here
    (`prd.md:1204` asserts "an `exec` assignment exists") carrying a scope,
    a lane and a lease, while B's schema gives `exec` a home as a `note`
    kind with `started`, `ended`, `exit_code` and `output_path` and a
    `CHECK` forbidding those columns on any other kind. The two are probably
    complementary — the assignment is the run, the note is its output — but
    `assignment` has no `scope` column and PR 7 needs one, so this rides on
    C-135's migration and should be settled explicitly there rather than
    assumed.
  - The eight pull requests are the right cut and the order stands, with
    one insertion. Nothing the spike ran argues for moving work between PR 1
    and PR 8: the clone really is the foundation, and every kill point the
    spike exercised was reconciled by the restart rule PR 2 owns and the end
    run PR 3 owns, which is the seam those two are cut on. C-133 and C-134
    both land inside pull requests that already exist and neither crosses a
    boundary. What the cut is missing is a step before PR 1: C-135, C-136
    and C-137 are all one thing seen three ways — the runner's vocabulary
    has no schema and B's library has no function to write it — and they sit
    in the gap between "nothing here builds B's library" on this page and no
    migration after 002 on B's. That is a ninth pull request, or B's, and it
    is the first one, because PR 1 records three setup steps and takes a
    lease on its first line. The eight below it are unchanged.
- **2026-09-12** — PR 7 landed as #278 on `feat/runner-hard-stops`: the
  hard stops (session signal, the repository check run in the clone, the
  ship receipt), session note ingestion, the `start` session's one `run`
  cost row, and B's 15.7 grep of the runner. Its `exec` paragraph had
  already been met by #227. Details in `implement.md`'s PR 7 section.
- **2026-09-12** — PR 3's `ENOSPC` path landed on `fix/runner-enospc`: a
  second copy that meets `ENOSPC` — the ignored copy or the kept archive —
  keeps its row `ending` naming `no space`, the clone and the partial copy
  in place, retried every tick and completing on the tick after space
  returns; the heartbeat carries the floor, the free space and the space the
  copy needs. The rest of PR 3 was #227. Details in `implement.md`'s PR 3
  section.
- **2026-09-16** — The three criteria edits `implement.md` reserved for the
  operator, approved on sd:235 ("2.1 yes, 2.2 yes, 2.3 yes") and made
  here. Criterion 4's concurrent-`fetch` clause is one synchronised run at
  a rendezvous, not twenty: since C-13 the two clones share no store, so
  the outcome is deterministic and criterion 4's byte-identity assertions
  already catch the one regression that reintroduces C-7, a linked worktree, which the `alternates` check alone would not. Criterion 5 is
  split at its retention sentence: 5 is pulse, sleep and keys; 6 is
  retention, restore and storage. Of the log's routing sentences that
  named criterion 4, the three that describe the storage assertions as
  they stand (C-49's, rounds thirty-two and forty's consolidation and the
  operator's decision after it) now name criterion 6, and C-89's hook
  sentence names criterion 1, whose assertion it is. Not in the approved
  edit and left as written: the routing sentences of rounds nineteen to
  thirty-one that name criterion 4 for retention and restore assertions
  now in criterion 6, and those of rounds thirty-three to thirty-nine that
  name it for storage assertions the consolidation after round forty
  reduced away; `implement.md`'s section on this edit lists them by line.
  Criteria 7 and 8 assert the two
  behaviours no criterion asked for, "an item with no branch is refused
  naming it" and "a chain is checked acyclic when it is created", each by
  a test in `local-sd-db/tests/test_runner.py` that dies on a byte-copy
  mutation of its refusal. The `budget_usd` clause is not in this edit; it
  waits on sd:234's slice 8a.
- **2026-09-16** — The item closes. The `budget_usd` clause of criterion 4
  is split by the owner's decision on sd:235: the creation-time half, a
  `budget_usd` on a row whose author is a `start` entry refused naming the
  entry, landed in #415 (`source:local-sd-db/sd_db/runner.py::enqueue`,
  asserted by
  `source:local-sd-db/tests/test_runner_budget.py::test_a_budget_on_a_start_author_is_refused_at_creation_naming_the_entry`
  and
  `source:local-sd-db/tests/test_runner_budget.py::test_no_budget_writes_null_and_a_budget_on_a_url_author_is_stored`;
  the dashboard's field by
  `source:local-project-dashboard/tests/test_run_budget.py::test_both_run_dialogs_offer_a_budget_usd_and_the_refusal_reaches_the_page`).
  The other half, a `url` row ending `blocked` with `budget spent` and the
  two-row test criterion 4 asks for, is assigned to sd:234 slice 8b, the
  runner's half of the reservation library, and is asserted there once a
  `url`-author execution path exists: the dispatch loop skips at
  `if not provider.start:` in `local-sd-runner/sd_runner/runtime.py` every
  entry without `start`, which every `url` entry is, so no path exists to
  run that test on today. The clause carries
  the pointer in place. This page has no `status:` field to move; the
  store's row is the item's status.
