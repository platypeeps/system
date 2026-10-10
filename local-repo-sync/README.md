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
./repo-sync.sh refresh    # move every pinned checkout to origin's default branch
./repo-sync.sh follow     # satellite: move system and pack to the hub's pins
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

## Pinned checkouts

A checkout on a detached HEAD is pinned (sd:3097). The hub runs system and
the command pack from pinned checkouts, so they change only when the
operator refreshes them, not under work in other repositories.

```sh
git -C ~/repos/system switch --detach        # pin at the current commit
./repo-sync.sh refresh                       # move every pinned conf checkout
./repo-sync.sh refresh ~/repos/system        # move one checkout
git -C ~/repos/system switch main            # unpin; sync pulls it again
```

`sync` and `nightly` fetch a pinned checkout and never pull it. They list it
as `pinned at <sha>`, with `, N behind origin/<default>` when origin moved
on. A pinned checkout is not a failure, and nightly mails nothing about it.

`refresh` moves each pinned checkout to `origin/<default>`, still detached,
and prints the old and new sha. It refuses a checkout with uncommitted
changes or a submodule, never overwrites an ignored file that origin now
tracks (the switch fails and nothing moves), and leaves a checkout on a
branch alone. In the command pack (it has
`bin/sd_install.py`) it then runs `make setup`. It moves the system
checkout first, whatever the conf or argument order: the pack's setup
installs `sd_db` from it and refuses a system older than the copy installed
(sd:3218). When local-sd-db's
`SCHEMA_VERSION` changed, it prints the steps: stop the dashboard, the runner
and `sd-serve`, run `sd-db.sh backup`, then `sd-db.sh migrate`. It runs
neither. It exits 1 when any checkout failed.

`refresh` drains the lanes before it moves anything (sd:3099). POSIX sh
cannot hold a flock, so `refresh_drain.py`, a stdlib helper beside the
script, holds every lane's `runner.lock` under the lane root. The root is
`SD_LANE_ROOT`, else the `sd.lane_root` setting, else
`$XDG_STATE_HOME/sd/lanes` (default `~/.local/state/sd/lanes`), as the
pack's lanes read it. While a lock is held, that lane's `lane run` exits at
once and its queued entries stay pending. A running lane keeps its lock until
its run ends, so refresh waits for it. A lane whose runner never ran has no
lock yet. The helper makes and holds one for every folder under the lane
root, every repository in the registry (`sd-db.sh repo list`) and every
checkout in the conf, since a lane is named after its checkout's folder. A
registry it cannot read refuses with nothing moved. Each pass tries every
lock first and keeps the ones it gets, then waits for the busy ones, so
`lane run --hosted` cannot lead the drain from lane to lane (sd:3265). With
every lock held, the helper reads the holders `sd gate status --json` lists
and waits until each of them has ended. A waiter, or a gate admitted later,
does not count: no lane gates without its runner lock, so it is no lane's.
Waiting for an idle gate starved a busy satellite for hours. A pass that
takes a new lock reads the holders again. Then it runs the refresh steps as its child and
releases the locks when it exits. TERM, INT or HUP sent to the helper alone
does not end it early: it waits for the child, which is in its process
group. Ctrl-C reaches both. The child inherits the lock descriptors, so a
`kill -9` of the helper leaves the locks held until the child's last step
ends. No step leaves a process behind to hold them: services restart through
launchd, which passes no descriptor on, and the child's git runs with
`gc.autoDetach`, `maintenance.autoDetach` and `core.fsmonitor` set to
`false`.

A gate started by hand (`sd-ship prepare`, `sd-check`) while the refresh
steps run is not excluded: no `sd gate` verb holds every slot. Run no gate
by hand during a refresh.

The wait is bounded at 45 minutes in total, for the locks and the gate
together, not 45 minutes each. `REPO_SYNC_DRAIN_WAIT` overrides it in
seconds (default 2700). It prints what it waits on
when the wait starts and once a minute after that. Past the bound, refresh
refuses with nothing moved and names the busy lane or the gate. When `sd`
itself fails, as a broken pack would make it, the refusal prints the manual
move: `git -C <checkout> switch --detach origin/main`, then `make setup` in
the pack.

### Satellites follow the hub

A satellite never moves its system and pack checkouts on its own (sd:3100).
After a refresh with no failure, the hub pushes one annotated tag, `hub-pin`,
to the system origin, as `+refs/tags/hub-pin`. The tag names the pinned
system sha, and its message carries `pack=<sha>`, so one push publishes the
pair. That one tag may move backwards; nothing else is forced. A failed
push, or a pinned system without a pinned pack, exits 1: satellites keep the
old pair until refresh runs again. The workflow database is not involved, so a satellite whose
`sd_db` lags the hub's build still follows. A machine with
`~/.config/sd/hub.json` is a satellite. There, `sync` and `nightly` never pull
system or pack, pinned or on a branch, and `refresh` refuses and names
`follow`.

`follow` fetches the `hub-pin` tag from the system origin, then the pack sha
it names from the pack origin; that sha is on the pack's main, and a fetch
by a reachable sha works. Where a sha differs from HEAD, it drains the lanes
as `refresh` does, then switches the checkout, detached and with
`--no-overwrite-ignore`, to exactly that sha, and runs `make setup` in the
pack. Then, with the lanes still held, it runs the system checkout's
`local-machine-setup/machine-setup.sh update bin --apply` and
`update satellite --apply`, each bounded at 450 s (sd:3168). The satellite
stage installs the hub's `sd_db` into the pack's venv and the bin stage
relinks commands, so neither may run under a lane run. It runs no other
stage: the cron and agents stages can reinstall the follow job's own
LaunchAgent, and launchd would boot out the running follow. Neither stage's
exit says its install worked, so after each `--apply` follow runs the stage's
dry run, within the same bound, and counts the drift words `status` counts.
No drift is no proof, since a `SKIP` is not drift: the dry run must also print
the stage's proof line, `bin-links.sh status`'s last line (`PATH on PATH`) or
the satellite's `ok sd_db ... matches the hub's`, which only a hub that
accepted this build prints. A failed stage, drift in its dry run, a dry run
that cannot answer, or one without its proof line exits 1 and names the
command to run by hand; the move and the marker stay. When the system move
changed `local-agent-prompt/prompt/shared.md`, follow then runs that
checkout's `agent-prompt.sh refresh --apply`, bounded at 120 s, with the
lanes still held (sd:3262). A run that finds the marker below refreshes too,
since the run that left it may have moved system and stopped first. The
refresh refuses a `DIFFERS` or `UNKNOWN` target and writes nothing; follow
prints a `!!!` line naming the target and the commands to run by hand, and
still exits 0, since the move stands. A no-op or a
rolled-back follow runs no update. It never moves to origin's default branch. It checks every checkout
first, so a failed fetch, a tag with no `pack=` line, uncommitted changes or
a drain timeout refuses with nothing moved. It moves system, then pack; when
a move fails, it switches each checkout it moved back to its old sha, pack
first, so the pair is never left split and the next run retries both. Once
the pack is back, and while system is still at the pin, it runs `make
setup` in the pack again at the old sha (sd:3218), so the installed commands and the
venv's pinned requirements match the pack's HEAD again; a package only the
hub's pin added stays in the venv, unused. If that fails too, it prints the
one command to run by hand.

A follow killed during the pack's `make setup` leaves HEAD at the pin, so
HEAD alone does not show it. By operator ruling on sd:3100, `follow` keeps an
intent marker, `${XDG_STATE_HOME:-~/.local/state}/repo-sync/follow-intent`.
It writes the target system and pack shas there, atomically, before it moves
any checkout. It deletes the marker only once `make setup` and the update
are proven at the pin (sd:3168). A rollback leaves it: its `make setup` at the
old sha proves no update. While the marker is left, the next run drains,
finishes the move and runs `make setup` and the update again, even with HEAD
at the pin or with no pack checkout in the conf. With no `hub-pin` tag yet, or with every checkout already there, it
does nothing and drains nothing. On the hub it says so and does nothing. If
the move changed `SCHEMA_VERSION`,
`follow` prints the hub's migrate note; the hub's own refresh printed it
too.

One-time setup per satellite; `follow` itself detaches a checkout that is on
a branch:

```sh
cp local-cron-jobs/examples/repo-sync-follow.job \
  ~/.config/system/cron-jobs/jobs/<satellite host>/
```

The job runs `follow` every five minutes. Its timeout is above the
45-minute drain bound plus the two 450 s stage bounds and the 120 s prompt
refresh. A job that chains a
full `machine-setup.sh update --apply` after `follow` (sd:3154) finds the bin
and satellite stages already done.

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
- remove a worktree that holds such a branch, wherever it sits, when it has
  no uncommitted or untracked files, no ignored files but build output
  directories (`target/`, `node_modules/`, `dist/`, Python caches; a regular
  file of that name still counts), and no process has its cwd or an open
  file inside it; the build output goes with it;
- delete lane logs and scratch under the bulk storage root
  (`sd config get sd.bulk_storage_root`): in each `<root>/<repo>/lane/`, a
  file unmodified for 14 days, then a folder unchanged for 14 days that is
  empty after that. `lane/queue/` and anything a process holds open stay;
  unset or not mounted, nothing is swept.

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

After the hygiene mail, `nightly` asks local-jev, in shadow, whether each
listed line holds live, abandoned or superseded work (sd:2094). Jev's
answers go to its ledger and corpus beside the report's own order; the
report is mailed first and unchanged. The request holds fixed words, counts
and the tip's age in days only: no repository, branch, sha or path. Stage
`JEV_REPO_SYNC_HYGIENE`; set it to `0` to switch this off. Unset means on,
and `jev off`, no key or a spent budget skip it too. The suite stubs
`local-jev` and never reaches it.

**Ledger subject and run (sd:2953).** The hygiene `ask` passes `--subject repo-sync:<16 hex>`: the first 16 hex of the sha256 of the listed records (class, count, reason, sha, checkout, unit-separated), sorted, one per line. An outcome recomputes it from the same hygiene listing. The script exports `JEV_RUN=repo-sync-<UTC yyyymmddThhmmss>-<4 hex>` once, so the reports' `notify.sh` calls join the same run.

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

`tests/test_pinned.py` covers pinned checkouts in `sync`, `nightly` and
`refresh` with the same fixtures and a `make` stub; its cases are `NEW` too.
`tests/test_refresh_drain.py` covers the drain with a fixture lane root of
real lock files, held from another process, and an `sd` stub.
`tests/test_follow.py` covers the `hub-pin` tag push and `follow` with the same
bare-origin fixtures, one origin shared by a hub and a satellite checkout.

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
- One exception (sd:3125): a branch other than origin's default, with its own
  commits, whose upstream moved on is work in progress, such as a pull
  request branch. sync lists it under `diverged:` with its ahead and behind
  counts, leaves it alone, and does not count it as failed. A failed fetch
  still fails.
- The old script also had a commented-out `git log --since=` block writing to a
  `github-commit-audit/output` folder. It was dead and is not carried over.
