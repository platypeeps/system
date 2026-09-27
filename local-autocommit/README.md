# local-autocommit

Commit and push the scoped paths a job just generated, in one commit.

Jobs in this repository write tracked files and used to leave them
dirty: `local-ai-apps`' inventory and `local-repo-sync`'s repo list. A generated file nobody commits is worse than
no file. `git status` is permanently noisy, so a real edit hides in the
noise, and the churn rides onto whatever branch somebody switches to next.

    ./autocommit.sh check  --scope local-ai-apps/profiles --author ai-apps
    ./autocommit.sh commit --scope local-ai-apps/profiles --author ai-apps \
                           --message "chore(ai-apps): refresh inventory"

`--scope` repeats. A generator that rewrites several tracked files names each
one, and they land in a single commit:

    ./autocommit.sh commit --author repo-sync \
                           --scope local-repo-sync/repos.common.conf \
                           --scope local-repo-sync/repos.personal.conf \
                           --message "chore(repo-sync): reconcile repo list"

These scopes exist in the checkout only when a tool keeps its generated file
there. By default `local-repo-sync` and `local-ai-apps` keep theirs under
`${SYSTEM_TOOLS_CONFIG:-~/.config/system}/`, outside the checkout, and have
nothing to commit; pointing a tool back at its repository folder brings the
commit back.

A file the caller leaves out is a file left dirty. `check` then defers on it
every run after, which is how repo-sync left `repos.common.conf` dirty for
good: `remove_entry` rewrites every conf the profile reads, and the commit
named only the profile one.

## Two verbs, and a job calls both

`check` runs **before** the generator writes. `commit` runs after.

They are separate because the interesting refusal is only answerable before
the job runs. `ai-apps.sh capture` replaces each `.inv` wholesale, so a hand
edit sitting in the scope is already gone by the time a commit could notice
it. Asking afterwards asks about a file that no longer exists.

`check` refuses three things:

| Condition | Exit | Why |
|---|---|---|
| Checkout is not on the default branch | 3 | A generated commit on someone's feature branch rides into their pull request. Three sessions share this checkout. |
| Any one scope is already dirty | 3 | Someone is mid-edit, or an earlier run left it uncommitted. Committing on top bundles work nobody reviewed. |
| This author has an unpushed commit | 1 | The run that failed to push says so once; every later run reports "no changes" and exits 0 while the commit sits local. |

`commit` stages the `--scope` paths and nothing else, proves no other path
came with them, commits under `--author`, then runs `pull --ff-only` and
`push` as two separate checks.

`status` reports on the scopes this repository auto-commits. It enumerates
them from the filesystem rather than reading a list, because a written list
drifts the moment a generator gains a file.

## Exit codes

`0` committed and pushed, or nothing to do. `3` deferred on purpose. `1`
broken — a commit exists that the remote does not have, or (for `status`) a
pushing job is installed without a usable deploy key.

local-health-check reads these codes. `cron-jobs.sh` raises a failure banner
on any non-zero exit, so a deferral is visible rather than silently skipped
night after night.

## The deploy key (sd:1417)

`main` requires `route` and the four `system-native` legs through a
repository ruleset, and a direct push is refused unless its commit passed
them. The jobs push generated commits straight to `main`, which no check ever
ran on. The ruleset therefore exempts one kind of actor: a deploy key.

- Each machine has its own key at `~/.ssh/system_autocommit`
  (`AUTOCOMMIT_SSH_KEY` overrides it). The key has no passphrase, because
  launchd cannot type one, and it is mode `0600`.
- A deploy key reaches this repository alone and no API. A second ruleset,
  with no bypass, still refuses it a force push or a deletion of `main`.
- `check` and `commit` set `GIT_SSH_COMMAND` to that key for the fetch, the
  pull and the push. The ssh agent is off for those calls: ssh offers
  agent-held keys first, and `Host *` in `~/.ssh/config` names the
  operator's key, which the ruleset would refuse. Another job that pushes
  can read the same command from `autocommit.sh key ssh-command`, and stop on
  any answer other than the key or "missing". The operator's own git is
  unchanged.
- Without the key, both print `MISSING deploy key` and push as the default
  identity, which the ruleset refuses.
- `status` exits `1` when a job that pushes (`repo-sync-nightly` and
  `ai-apps-nightly` by default; `AUTOCOMMIT_PUSHING_JOBS` replaces the
  space-separated list) is installed and the key is missing or readable by
  others, even on a machine with no scopes. Installed means a
  `<prefix>.cron.<job>.plist` in `~/Library/LaunchAgents`, with the prefix from
  `SYSTEM_TOOLS_LABEL_PREFIX` (default `local.system-tools`). A machine
  with none of them installed is not checked.
- `status` also exits `1` when the org disallows deploy keys, which refuses
  every key at once. The enterprise owns that setting, and the org cannot
  change it (its own PATCH answers 422), so a flip is not something this
  repository would otherwise see before a push is refused. It asks with
  `gh api orgs/<owner>`, where `<owner>` comes from `origin`. When `gh` cannot
  answer (not installed, not logged in, offline), `status` prints a note on
  stderr and does not fail, because that says nothing about the setting.

To set up a machine:

    sh local-autocommit/autocommit.sh key create

It makes the key and prints the `gh api` command that registers it as a
deploy key with write access. To revoke one machine, delete its deploy key on
GitHub; the other machines keep working.

## Why the pull and the push are checked separately

`set -e` exempts a failing command inside an AND-OR list, so `pull && push`
can fail whole while execution carries on to the success message:

    $ sh -c 'set -eu; git -C /nonexistent pull -q --ff-only && git push -q
             echo "reached the success line"'
    reached the success line
    script exit=0

That is this tool's failure mode in miniature — reporting success having not
done the thing — in the one place where nobody is watching to notice.

## Provenance

The logic was extracted from a profile-capture script that committed and
pushed its rosters correctly and alone. Its comments are kept where they
explain a trap rather than a roster. That script still carries its own copy.

## Tests

    ./autocommit.sh test -v

The fixture is a throwaway work tree with a local bare repository as
`origin`, so `push` and `pull --ff-only` run against a path on disk and
exercise the real git code paths. Nothing touches the network.

Cases marked REGRESSION fail against a script without the guard they name;
`AUTOCOMMIT_TEST_SCRIPT` aims the suite at another copy so that can be
checked. Cases marked PIN record a deliberate decision.
