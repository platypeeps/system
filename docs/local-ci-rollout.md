# Local CI rollout, September 2026

This page records history, not rules.
The mechanism lives in the command pack's `WORKFLOW.md` (the `repo.ci` section).
`local-sd-db` holds the `repo.ci` column (sd:1843).

## Who turned Actions off, and why

The operator decided the switch on 2026-09-27.
An assistant session, acting as the single merge integrator, carried it out on 2026-09-27 and 2026-09-28.
The operator approved each repository's switch in that session.

GitHub Actions billing had blocked CI runs on the private repositories.
No pull request could get a green check, so nothing could merge.
Disabling Actions or the CI workflows is therefore deliberate.
Do not turn them back on to "fix" a missing check; the local gate is the check now.

## Which repositories moved

Do not trust a list in prose; ask the workflow database:

    local-sd-db/sd-db.sh repo list

A row whose `ci` field reads `local` gates merges locally.
Twenty-two repositories read `local` on 2026-09-29.

Most are private repositories in the `platypeeps` organization; Actions is disabled there.
Three are public: this one, the command pack, and one more.
Two are repositories in the operator's employer organization.
The public ones and the employer ones keep Actions on.
There, only the CI workflow files are disabled or deleted, so CodeQL still runs.
Dependabot runs where a repository keeps a `.github/dependabot.yml`; this one deleted its own, which watched only the deleted workflows.
One employer repository finished its switch on 2026-09-29.
Its branch protection now requires only `sd/local-gate`, and its CI workflows are disabled.
Its `make check` can run past the default `sd-check` timeout under load.

The other repositories in the employer organization stay on GitHub CI.

## How the gate works

Each moved repository gained a `check` entrypoint, usually `make check`.
The entrypoint mirrors what the repository's required workflow ran.

`sd-ship merge` runs that entrypoint through the pack's local gate:

1. It checks the reviewed head out into a clean, detached worktree.
2. It runs `sd-check` there.
3. It posts the result to that commit as the `sd/local-gate` status.
4. The merge requires that status as `success` at the head.

Branch protection or a ruleset requires `sd/local-gate` on each moved repository.
It replaces the old required checks, such as an aggregate `CI Result` job.
A merge done outside `sd-ship` never gets that status, so it cannot pass protection.

## How to check a repository

- **Mode:** the `ci` field of `sd-db.sh repo list`.
- **Required check:** the branch protection or ruleset names `sd/local-gate`.
- **Workflows:** `gh workflow list --all` shows no CI workflow, or shows it as `disabled_manually`.
- **Actions on a private repository:** the repository's Actions settings show Actions disabled.

## How to switch a repository, or switch it back

To switch a repository, first add a `check` entrypoint and prove it with `sd-check`.
Then run the command pack's `sd ci local` (sd:1914) from its checkout.
It is a dry run; `--apply` writes. It needs `admin` on the repository.
It makes three changes together, each skipped when it already holds:

1. It sets `repo.ci` to `local`, as `sd-db.sh repo ci <path> local` does.
2. It makes `sd/local-gate` the one required check, in branch protection or the ruleset.
3. It disables Actions on a private repository, or only the declared CI workflows on a public one.

It refuses when a requirement it cannot rewrite names another context.
Examples are an organization ruleset, or a ruleset shared with another protected branch.

No verb switches back.
To go back to GitHub CI, reverse each step.
Set the mode to `github`, restore the old required checks, and re-enable the workflows.
A deleted workflow comes back from git history.
Re-enable Actions only once billing allows it again.

## What the local gate needs

The gate runs on the operator's machine, so each `check` needs that machine to hold what CI held.
These needs showed up during the rollout:

- **Two Node repositories** need `SOCKET_SECURITY_API_KEY` and a running Docker engine.
- **One of them runs long.** Its `check` takes about 460 to 630 seconds.
  The `sd-check` default timeout is 900 seconds, so the margin is small.
- **The same repository builds its image for arm64 locally.** CI built it for amd64.
  Trivy therefore scans a different architecture than the one CI scanned, and findings can differ.
- **Both Node repositories must declare `check:` in `CLAUDE.local.md`.**
  The local block replaces the gate's `package.json` detection.
- **A writing repository's gate fetches the command pack** with the operator's git credentials.
- **Load-sensitive tests can fail when several gates run at once.**
  A timeout under load is not a regression; rerun the gate once the machine is quieter.
