# Local CI rollout, September 2026

This page records history, not rules.
The mechanism lives in the command pack's `WORKFLOW.md` (the `repo.ci` section).
`local-sd-db` holds the `repo.ci` column (sd:1843).

## What happened

On 2026-09-27 and 2026-09-28, sixteen private repositories in the `platypeeps` organization moved to `repo.ci = local`.
GitHub Actions billing had blocked their CI runs, so no pull request could get a green check.

Each repository gained a `check` entrypoint.
The entrypoint mirrors what the repository's required workflow ran.

`sd-ship merge` now runs that entrypoint through the pack's local gate:

1. It checks the reviewed head out into a clean, detached worktree.
2. It runs `sd-check` there.
3. It posts the result to that commit as the `sd/local-gate` status.
4. The merge requires that status as `success` at the head.

Branch protection or a ruleset requires `sd/local-gate` on each moved repository.
Actions is disabled on the private repositories.

## Public repositories

The public repositories (this one and the command pack) keep Actions on.
CodeQL and Dependabot still run there.
Their CI workflows are disabled; the local gate replaces them.

## Repositories that stay on GitHub CI

Repositories in the operator's employer organization stay on GitHub CI.

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

