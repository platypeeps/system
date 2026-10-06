---
title: A satellite installs the hub's sd_db build itself
created: 2026-10-05
item: sd:2802
---
# Design — a satellite installs the hub's sd_db build itself

## The proof: one digest algorithm

The handshake digest is a SHA-256 of the package's `.py` and `.sql` files, 16 hex characters.
`remote.tree_digest(root)` now holds the algorithm; `_hash_files` and `build_digest` call it unchanged.
The installer hashes the exported `sd_db` with the same function.
Equal digests mean equal bytes for every file the digest covers, and the wheel ships exactly those files.

## The hub names its digest

`BuildMismatch` gains `hub_build`, a keyword argument and a class default of `None`.
`check_handshake` and the server's protocol refusal pass `build_digest()`.
`describe_error` sends every plain attribute, so the field crosses the wire with no frame change.

Backward compatibility, both ways:

- An older satellite rebuilds the error and sets an attribute it never reads.
- A newer satellite reads `hub_build` as `None` from an older hub.
  For a `build` refusal, the old `hub` field is the digest; for any other field there is none, and nothing is installed.

## The library function: `sd_db.self_install.install_hub_build`

In order, under the venv's lock:

1. The off switch; then the venv that holds this package (`<venv>/lib/pythonX.Y/site-packages/sd_db`).
2. The source checkout: the caller's, then `SD_DB_SOURCE_CHECKOUT`, then `<venv>/sd-db-source`, then pip's `direct_url.json` for a `git+file` install.
   The command pack provisions that way, so its venv names the checkout before any `sd-db.sh install`.
3. The venv's lock, `<venv>/sd-db-self-install.lock`, waiting up to `LOCK_WAIT` (600 s).
   It goes through `runner_journal.lock`, the one hardened lock opener both packages share.
4. If a fresh probe already reads the hub's digest, another process installed it: done.
5. `git fetch origin` (60 s bound, no prompt), then `git archive origin/main local-sd-db` into a temporary folder.
6. The export's digest must equal the hub's, or nothing is installed: the hub runs a build that is not `origin/main`'s tip.
7. Build the wheel with the export's own `_build.py`, then `pip install --no-index --force-reinstall`.
8. A fresh `python -I` in the venv must read the hub's digest.
9. Record the checkout in `<venv>/sd-db-source`.

The target venv is found from the package's own path, not from `sys.prefix`.
The pack's entrypoints run under `#!/usr/bin/env python3` and add the venv's `site-packages` to `sys.path`.
So `sys.prefix` is the system interpreter there, not the venv. (Changed from the brief, which named `sys.prefix`.)

## On a refusal: `hub.Hub.open`

`Hub.open` is the one place a satellite opens the default database.
The refusal arrives in answer to the first frame, so the hub ran no statement of that session.
`self_install.after_refusal` then decides:

| Case | Action |
| --- | --- |
| Loopback hub (`token_file`, `127.*`, `localhost`, `::1`) | today's error, unchanged |
| Off switch | today's error and the reason |
| `SD_SATELLITE_SELF_INSTALL_RERUN` set | today's error and the reason: the rerun met it again |
| This process opened a hub session before | today's error and the reason; no install, no rerun |
| No hub digest (older hub) | today's error and the reason |
| `upgrade == "hub"` (satellite newer) | today's error and the reason: upgrade the hub |
| Install refused | today's error and the outcome's reason |
| Installed, and the entrypoint did not call `declare_replayable()` | today's error, the install line, and "run it again" |
| Installed, declared, and argv cannot replay the command | today's error, the install line, and "run it again" |
| Installed, declared, and argv replays the command | one stderr line, then `execve(sys.executable, sys.orig_argv)` with the rerun variable set |

`replay_refusal` decides whether argv replays the command.
A program read from standard input (`python -`, an interactive interpreter) does not, nor does a missing program file.
A pipe or a file on standard input may already have given this run its input, so it does not either.
A terminal, `/dev/null` and a closed standard input do.
Review of the first head found the `python -` case: the rerun read an empty stdin and exited 0 without the command (sd:2802).
Review of the second head found that a script's local work before `Hub.open` ran twice.
Only the entrypoint knows whether that work repeats safely, so the rerun needs its declaration.
No entrypoint declares it yet; the command pack's `sd` can, verb by verb, as a follow-up.
Until then a refusal costs one manual rerun, not a manual install.

The session counter (`remote.sessions_opened`) guards the one case where a rerun could repeat hub writes.
That case is a hub upgraded while a process already holds sessions.

The newer-satellite case is not installed over: the pack's library guard also refuses older code over newer.

## Nightly: `sd_db.satellite --apply`

On a mismatch, the stage prints the `DIFFERS` line as before.
Then it prints the plan line, or runs the install with `--apply` and prints `ok` or `DIFFERS` with the outcome.
It does not continue to `providers.yaml` after an install: the running process still holds the old code, and the next run checks it.
`machine-setup.sh` passes its own checkout as `SD_DB_SOURCE_CHECKOUT`.
An environment variable and not a flag: an older installed `sd_db.satellite` ignores the variable but would refuse an unknown flag.

## Known limits

- A declared rerun repeats any local work the command did before its first hub session; the declaration is the entrypoint's promise that this is safe.
- The lock is per venv. The pack's own `sd_install.py` takes another lock, so the two installers do not serialise with each other.
- A wheel install leaves no `vcs_info` in `direct_url.json`, as `sd-db.sh install` does today.
  The pack's ancestry guard then sees no installed commit.
