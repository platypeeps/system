# local-repo-sync

Clone-or-pull the whole repo fleet in one sweep. Symlinked as `repo-sync` in `~/bin/common` (replaced the old `resync` script there).

## Usage

```sh
./repo-sync.sh list       # resolved profile, root and repo list — touches nothing
./repo-sync.sh sync       # clone what is missing, fast-forward what is not
./repo-sync.sh check      # read-only: what is missing or on another origin
./repo-sync.sh reconcile  # update the conf files to match the checkouts on disk
./repo-sync.sh hygiene    # report stale worktrees, dead locks, landed branches
./repo-sync.sh hygiene --apply  # act on the safe classes, list the rest
./repo-sync.sh nightly    # reconcile + sync + hygiene --apply, emailing (cron)
./repo-sync.sh test       # regression suite (tests/), run by make check
repo-sync                 # symlink in ~/bin/common, works from anywhere
```

`sync` exits 1 if any repo failed, but only *after* trying all the others, and
prints the failures as a summary at the end.

`reconcile` treats disk as the source of truth: GitHub checkouts under the
root that are missing from the conf are appended to the profile conf, and
conf entries whose checkout is gone are deleted — so a repo cloned or removed
by hand is picked up automatically and a deliberately deleted repo is not
re-cloned every night. Checkouts without a GitHub origin are listed as
unmanaged and left alone. Matching is by directory name, so a repo renamed
upstream keeps its existing entry.

A conf line clones to `$ROOT/<subdir>/<repo>`, so it can only describe a
checkout whose directory is named after its repo. A checkout in a
differently named directory is reported as `MISMATCH` and left alone rather
than added — adding it looped, because the entry reconcile wrote could never
match the checkout that prompted it, and the clone `sync` then made from that
entry kept the removal probe passing. Rename the directory or the repo to
bring it under management.

A linked worktree placed under the root is not a checkout. `reconcile` lists
it as `WORKTREE <dir> (of <parent>)` and leaves the conf alone; `hygiene`
reaches it through its parent's worktree registrations.

## Hygiene

Agents leave worktrees and branches behind. `hygiene` sweeps each conf
checkout. Without `--apply` it only reports; with `--apply` it acts on the
safe classes:

- prune worktree registrations whose directory is gone;
- clear a lock whose pid is not running, or runs with a different start
  time, then prune it (a lock without a pid is kept);
- prune remote-tracking refs for deleted remote branches
  (`git remote prune`);
- delete a local branch whose content is on the default branch: an
  ancestor, a tree equal to its merge base, or a patch-equivalent squash;
  the branch must also sit unmoved for `REPO_SYNC_HYGIENE_MIN_AGE` seconds
  (default 86400, at most 12 digits, read from its reflog), so a fresh
  branch survives;
- remove a worktree that holds such a branch, when it has no uncommitted,
  untracked or ignored files and no process has its cwd inside it.

Every deletion prints the branch and its tip sha with the command that
restores it. Of the branches whose content is not on the default branch,
it lists and never deletes: one whose upstream is gone, one with commits
on no remote, one named for a done sd item
(read from the sd database when present, `REPO_SYNC_SD_DB`), and a checkout
still behind its upstream. It never touches a stash, a remote branch, the
default branch, or a dirty or in-use worktree. All checks are local git
plumbing, except two calls to `origin`: the remote prune, and
`git ls-remote` for the default branch. When `origin` cannot be read, the
local `origin/<default>` is missing, or it differs from `origin`'s, no landed
branch is deleted that run.

Without `--apply` it exits 1 when it found anything. With `--apply` it exits
1 only when an action failed. It is not a `status` subcommand.

`nightly` is what the `repo-sync-nightly` cron job (local-cron-jobs, 02:45)
runs: reconcile — emailing the diff via local-notify's email channel whenever
the repo list changed — then sync, emailing the failure summary if any repo
failed, then `hygiene --apply`, emailing its report when it changed, listed
or failed anything. It exits 1 when an email could not be delivered, and since #200 also
when half or more of the fleet failed to sync — one unreachable repo is a
report, the cron failure push is for a lost report or an outage. The
threshold is not "all": a dead network still lets the odd repo through, so
an equality test would have gone on reporting success. After a reconcile the
updated conf sits in `<config>/repo-sync/`; the email is the record of what
changed.

## Profiles

The work and personal machines carry different repo lists, so the list lives in
config instead of an `if` in the script. The conf files are private and
per-machine, so they live outside the checkout in
`<config>/repo-sync/`, where `<config>` is `$SYSTEM_TOOLS_CONFIG` (default
`~/.config/system`). The repository ships a `.example` for each. Copy
the ones your profile reads and list your own repos:

```sh
mkdir -p ~/.config/system/repo-sync
cp repos.common.conf.example ~/.config/system/repo-sync/repos.common.conf
cp repos.personal.conf.example ~/.config/system/repo-sync/repos.personal.conf
```

`REPO_SYNC_CONF_DIR` points the script at another folder.

| File | Contents |
| --- | --- |
| `repos.common.conf` | personal and work, not terra — for example the `ai` and `platypeeps` groups |
| `repos.personal.conf` | personal machine — for example `ha`, `platypeeps` |
| `repos.work.conf` | work machine — for example `work` and the toolbox itself |
| `repos.terra.conf` | terra machine — the toolbox repo and nothing else |

The confs keep no git history: `nightly` rewrites them and commits nothing.

Format is `<subdir> <owner/repo>`; `#` comments and blank lines are ignored.
`<subdir>` is the folder under the checkout root, so `ai jomjol/AI-on-the-edge-device`
lands in `~/repos/ai/AI-on-the-edge-device`.

An entry repeated across the pair, or within one file, is read once. Spacing
does not matter to that: separators are collapsed before the comparison, so
`a owner/one` and `a   owner/one` are the same entry. The list keeps the order
entries first appear in rather than sorting, so `list` still reads in conf
order. This is deliberate and not merely tidy — `check`, `sync` and `nightly`
iterate that list directly and four totals count it, so a repeated entry used
to mean a repo pulled twice and a fleet count that overstated itself. The
duplicate line stays in the file for as long as its checkout does; it just
no longer does anything, so a hand-edit that repeats an entry is inert rather
than a nightly double-pull. When the checkout does go away, `reconcile` takes
every copy of the entry rather than the first — `remove_entry` filters all
matching lines, and it has always compared them with the same normalisation
this list now applies.

`terra` is the one profile that does **not** layer on `repos.common.conf`.
Everywhere else the pair is common + profile; a terra machine carries only
the toolbox, and inheriting common's `ai` group would contradict that.

Profile selection follows the profile `machine-setup` recorded in
`~/.config/machine-setup/profile`. Without that file it falls back to the old
condition — `work` if the work root names an existing directory, else
`personal`. Override with `REPO_SYNC_PROFILE`, and the checkout root with
`REPO_SYNC_ROOT`. The work profile's default root is the work root, else
`~/repos`.

The work root is `REPO_SYNC_WORK_ROOT`, else `SYSTEM_TOOLS_WORK_ROOT`.
`local-ai-apps` reads the same meaning from `AI_APPS_WORK_ROOT`, with the same
`SYSTEM_TOOLS_WORK_ROOT` fallback; set the shared name once to drive both.

Deferring to the recorded profile matters: the fallback calls any machine
without a work root `personal`, so on a terra machine `reconcile` would see
nearly all of `personal`'s checkouts missing and — disk being the source of
truth — delete them from the confs.

## Tests

`./repo-sync.sh test` runs the suite in `tests/`, and `make check` runs it
through `tests/ci-native.sh`. It is Python rather than shell for one reason: the
`run_suite` wrapper asserts a unittest summary (`Ran N tests`) and fails on
any skip, and a shell harness produces neither. The tests still work the way
the script does — build a tree of `git init`ed directories with an origin and
no commits, write a conf, run the script, read back what it wrote.

Each case says which kind it is: its docstring opens with `REGRESSION` or
`PIN`. A `REGRESSION` case reproduces a defect that shipped. Three of them
fail against the code from before their fix, which is the only way to know a
regression test tests anything; point the suite at an old copy to see it:

```sh
REPO_SYNC_TEST_SCRIPT=/tmp/old-repo-sync.sh ./repo-sync.sh test
```

A `PIN` case records a decision rather than reproducing a bug — that the
list keeps conf order instead of sorting, that a nightly with nothing to
report stays silent.

`tests/test_hygiene.py` covers `hygiene` and the `WORKTREE` line with a
third kind, `NEW`: each case was written before the feature and seen to
fail against the script without it. Its fixtures have history: a bare
origin beside the root and a clone under it, so the remote prune runs
offline.

One case reads the conf files in this folder instead of building a tree:
`ShippedConfTest` enumerates the shipped `repos.*.conf.example` files, plus
any local `repos.*.conf`, and asserts no entry is repeated,
within a file or across the pair a profile reads. It guards the cost of making
the dedupe silent. A repeated line used to announce itself by pulling a repo
twice; now it does nothing at all, so the file can hold a mistake that nothing
would otherwise mention. It enumerates rather than naming the four confs so
that a conf nobody remembered to add is still checked.

`sync` is not covered. It clones over SSH, which CI has no key for and no
network policy to allow, so the suite exercises `reconcile`, `list` and
`nightly` — where every defect this script has had so far actually lived.

No test reaches the network, and the fixture enforces that rather than relying
on there being nothing to clone. `GIT_SSH_COMMAND=false` makes any clone or
pull fail instantly while leaving reconcile and the notification real. That is
not belt-and-braces: aimed at pre-#231 code with `REPO_SYNC_TEST_SCRIPT`, the
`nightly` case reconciles the mismatched checkout into the conf — that being
the bug — and `sync` then clones it for real. Review caught that; before the
fix the suite made an SSH connection every time it was pointed at old code,
and on a machine without network it would have waited for a connect timeout
instead of failing.

Fixtures use the `terra` profile: profile names are validated against a fixed
list, and `terra` is the one that reads a single conf instead of layering on
`repos.common.conf`.

## Gotchas

- Clones over SSH (`git@github.com:`), so the agent needs a key loaded —
  `mac-utils.sh addkey` if it is not.
- After a reboot the agent is empty, and a key with a passphrase stays locked.
  `nightly` checks GitHub SSH once before the sweep. If no key answers, it runs
  `ssh-add --apple-load-keychain` and checks again. A key still locked is named
  at the top of the failure report. Run `mac-utils.sh addkey` once so the
  keychain holds the passphrase (sd:2160).
- Pulls are `--ff-only`. A repo with local commits or a dirty tree is reported
  as a failure instead of being silently merged; the old script used a bare
  `git pull` and could leave merge commits behind.
- The old script also had a commented-out `git log --since=` block writing to a
  `github-commit-audit/output` folder. It was dead and is not carried over.
