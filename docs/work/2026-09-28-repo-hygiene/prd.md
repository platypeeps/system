---
title: repo-sync sweeps stale worktrees, dead locks and landed branches
created: 2026-09-28
item: sd:1987
---
# PRD — Repo hygiene after the nightly sync

## Problem

Agents create worktrees and branches in every managed checkout, and few of
them clean up. A manual sweep of `~/repos` on 2026-09-28 found:

- 52 worktree registrations whose directory was gone;
- 173 local branches whose content was already on the default branch,
  50 of them still held by a worktree registration;
- 1 worktree locked by a Claude agent whose process (pid 3662) had exited
  two days earlier, which kept its merged branch alive;
- 5 branches that had landed through a squash merge with different content,
  so git reported them as unmerged;
- 6 branches with commits that existed only on this disk.

The dashboard already reports abandoned worktrees and names
`git worktree prune` as the fix, but nothing applies it. `repo-sync sync`
runs `git pull --ff-only` without `--prune`, so remote-tracking refs for
deleted branches also stay.

The cost is not disk space alone. Stale branches hide the few branches that
hold real work, and the manual sweep took a full review of PR and item state
to separate the two.

## Requirements

1. `repo-sync.sh` gets a `hygiene` subcommand. It reads the same fleet as
   `sync` (the resolved conf repos), and never acts outside it.
2. Without `--apply`, `hygiene` only reports. With `--apply`, it acts on the
   safe classes below and still reports the rest. `nightly` runs
   `hygiene --apply` after `sync`.
3. Safe classes, acted on with `--apply`:
   1. prune worktree registrations whose directory is gone
      (`git worktree prune`);
   2. unlock, then prune, a locked registration whose directory is gone and
      whose lock names a pid that is not running. The lock text also names
      the process start time; a live pid with a different start time counts
      as not running, and a lock without a pid is never cleared;
   3. prune remote-tracking refs for deleted remote branches
      (`git fetch --prune`);
   4. delete a local branch whose content is on the default branch: an
      ancestor, a tree equal to its merge base, or a patch-equivalent squash
      (`git cherry` on a synthetic squash commit);
   5. remove a worktree that holds a branch from class 4, only when
      `git status --porcelain --ignored` is empty and no process has its cwd
      inside it; ignored files (an `.env`, a local database) keep it.
   Class 4 skips a branch whose newest reflog entry is younger than
   `REPO_SYNC_HYGIENE_MIN_AGE` seconds (default 86400), or that has no
   reflog: a fresh branch sits at the default tip and counts as landed.
4. Never act on: the checked-out branch of any worktree not removed in 3.5;
   the default branch; a branch in a dirty or live worktree; a stash; any
   remote branch.
5. Report-only classes, listed per repo in the report:
   - branch whose upstream is gone and whose content is not on the default
     branch;
   - branch with commits on no remote ref (never pushed);
   - branch whose name carries `sd:<n>` or `-<n>` for an item that is done
     (a landing candidate for the operator, not an automatic delete; sd:1479
     closed with its PR unmerged, so "done" does not mean "landed");
   - checkout that could not fast-forward because of local changes.
6. A linked worktree placed under the root is not a checkout. `hygiene`
   finds it through its parent's registrations and treats it by the rules
   above. `reconcile` lists it as `WORKTREE <dir> (of <parent>)`, not as
   `MISMATCH`: on 2026-09-28, an agent's worktree named after its branch
   showed as a mismatched checkout that needs a rename.
7. Every deletion prints the branch name and its tip sha, so a report line is
   enough to restore it with `git branch <name> <sha>`.
8. `nightly` emails the report through local-notify when `--apply` changed
   anything or a report-only class is non-empty, like the reconcile diff.
9. `hygiene` without `--apply` exits 0 when clean and 1 when it found stale
   items; `--apply` exits 1 only when an action failed. It is not a `status`
   subcommand, so local-health-check does not read it.
10. POSIX sh, per the repo conventions. The squash check and the lock parse
   may call git plumbing only; no GitHub API call, so the sweep runs offline.
11. Nothing depends on Jev.

## Out of scope

- Deleting remote branches or closing PRs; both are outward-facing.
- Checkouts outside the conf fleet, and the runner's preserved clones, which
  `sd worktree` owns.
- Unarchiving or moving archived repositories.

## Acceptance criteria

- [ ] A fixture repo with a missing worktree directory: `hygiene` reports it;
      `hygiene --apply` removes the registration.
- [ ] A locked registration with a dead pid is cleared; the same lock with a
      live pid and matching start time is kept; a lock without a pid is kept.
- [ ] A branch merged by fast-forward, by merge commit, and by squash is
      deleted, and its tip sha is printed.
- [ ] A branch with a unique commit and a gone upstream is reported, not
      deleted.
- [ ] A merged branch in a dirty worktree keeps both worktree and branch.
- [ ] A stash is never touched.
- [ ] A linked worktree under the root, in a directory not named after its
      repo: `reconcile` lists it as `WORKTREE`, not `MISMATCH`, and leaves
      the conf unchanged; `hygiene` classifies its branch through the parent.
- [ ] `nightly` runs `hygiene --apply` after `sync`, and its email carries
      the report only when something changed or is listed.
- [ ] The suite runs through `repo-sync.sh test` and the `run_suite` line
      that already covers `local-repo-sync/tests`.
