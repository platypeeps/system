---
title: A satellite installs the hub's sd_db build itself
created: 2026-10-05
item: sd:2802
---
# PRD — a satellite installs the hub's sd_db build itself

## Problem

The hub's `sd_db` changes with each merge that touches `local-sd-db`.
The hub then refuses every session of a satellite that runs the old build, with `BuildMismatch`.
Every `sd` verb on the satellite fails until the operator runs `sd-db.sh install <venv>` by hand.
This happened twice on 2026-10-05.

The machine-setup `satellite` stage only reported the mismatch as `DIFFERS`.

## Decision

The operator approved two triggers: "on refusal + nightly".

## Requirements

1. One library function installs the hub's build into the venv that holds `sd_db`.
   It installs only bytes whose build digest equals the hub's digest.
2. The bytes come from `origin/main:local-sd-db` of the system checkout, after a bounded fetch.
   The export is a temporary copy; the checkout's worktree and `HEAD` do not change.
3. After the install, a fresh `python -I` in the venv reports the hub's digest, or the outcome says the install did not verify.
4. A lock in the venv keeps two installers apart.
5. `SD_SATELLITE_SELF_INSTALL=0` switches it off.
6. It never acts for a loopback hub, the hub's own machine.
7. The source checkout is not hard-coded. `sd-db.sh install` records it in the venv; `SD_DB_SOURCE_CHECKOUT` overrides it.
   An unknown source refuses, naming the variable.
8. Every handshake refusal carries the hub's build digest, whatever field differs.
   A refusal from an older hub without the digest installs nothing and keeps today's error, plus the reason.
9. On a refusal, the satellite's command installs, prints one stderr line, and runs the same argv again once.
   A refused install keeps today's error, plus the reason.
10. `sd_db.satellite --apply` installs on a mismatch; without `--apply` it prints the plan line.

## Acceptance criteria

- [x] An equal digest installs and verifies; a different digest installs nothing.
- [x] The off switch, the lock, the single rerun and an older hub each have a test.
- [x] `sd_db.satellite` plans in a dry run and installs with `--apply`.
- [x] `make check` passes.

## Log

- 2026-10-05 created and built on branch `feat/sd-2802-satellite-self-install`.
