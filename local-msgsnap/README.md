# local-msgsnap

Snapshot `~/Library/Messages/chat.db` from a binary that holds Full Disk Access
and can do nothing else with it.

## Why this exists

macOS has **no TCC scope for Messages**. The granular "Files and Folders" grants
only cover Desktop, Documents, Downloads, removable volumes and network volumes;
`~/Library/Messages` sits in the Full Disk Access set alongside `~/Library/Mail`,
`~/Library/Safari` and the TCC databases themselves. Reading `chat.db` costs FDA
or it costs nothing, and FDA cannot be requested programmatically — it is a
manual entry in System Settings.

So the permission cannot be narrowed. What can be narrowed is **who holds it**.

FDA granted to Terminal is inherited by everything the terminal spawns — every
script, every agent, every `npm install`. FDA granted to a 150-line binary that
copies one compiled-in path stops there. That is the entire point of this folder.

## Usage

    ./msgsnap.sh build      # compile + ad-hoc sign
    ./msgsnap.sh grant      # opens the FDA pane; you add bin/msgsnap by hand
    ./msgsnap.sh verify     # launchd probe — the only trustworthy answer
    ./msgsnap.sh snap       # writes ~/.local/state/msgsnap/chat.db

`status` shows what is built and snapshotted, `clean` removes the binary and
leaves snapshots alone. `snap` passes extra flags through to the binary
(`--out NAME`, `--allow-shm`).

### Exit codes

The binary's exit codes are an interface: `msgsnap.sh snap` branches on them
and `<config>/cron-jobs/jobs/msgsnap-nightly.job` (examples in `local-cron-jobs/examples/`) tells you what
each one means.

| Code | Meaning | What to do |
|---|---|---|
| 0 | snapshot written (or `--check` found the source readable) | nothing |
| 1 | usage error | fix the arguments |
| 2 | **TCC refused** — Full Disk Access is not granted to this binary | regrant, see "Gotchas" |
| 3 | SQLite could not read the source or write the snapshot | read the message |
| 4 | the source could not be opened for a reason that is **not** TCC: no database, `EACCES` from mode bits or an ACL, an I/O error, or ten `EINTR`s in a row | read the message; a regrant will not help |

2 means TCC and nothing else. TCC denies with `EPERM` (measured: errno 1 from
an ungranted shell, on a file that is mode 0644 and yours), so that is the one
errno that exits 2. Before sd:935 every open failure exited 2, including
ENOENT, `EACCES` and a transient `EINTR`, and the job comment sent you to
regrant FDA for faults that had nothing to do with it. `open(2)` returning
`EINTR` is retried, up to ten times, before it counts as a failure at all;
`tests/eintr-retry.sh` runs the real binary with `open(2)` stood in by a
dyld interposer and checks all of this without needing the grant.
`msgsnap.sh test` runs it as a unittest suite, which is how the
`system-native` CI `tools` leg runs it on every pull request and every push
to `main` (sd:946).

## What makes it safe to grant

- **No source-path argument.** The path is compiled in. An FDA holder that
  accepts a path is an arbitrary-file-read oracle for anything that can exec it.
- **Output is confined** to `~/.local/state/msgsnap/`, basename validated against
  `[A-Za-z0-9._-]`, written 0600 into a 0700 directory, temp-then-replace so a
  failed run cannot leave a half-copied database behind.
- **It reads no message content.** It copies a file and prints a path.
- **Read-only by default.** Escalation to a read-write open exists only for the
  WAL case below, is opt-in via `--allow-shm`, and always announces itself.

## Gotchas

**Every rebuild invalidates the grant.** TCC keys an ad-hoc-signed binary by its
cdhash, and the cdhash changes on every compile. After `build`, the old FDA entry
is dead weight — remove it and add the new binary. Build rarely. (A Developer ID
signature would survive rebuilds, since TCC would key on the stable identity
instead; not worth the setup for a personal tool, but that is the fix if this
starts changing often.)

**`cp` is the wrong tool.** `chat.db` is in WAL mode. Copying the `.db` alone
gives you a database that opens cleanly and is missing recent messages — they
live in `chat.db-wal` until a checkpoint. This uses SQLite's `sqlite3_backup_*`
online-backup API, which reads through the WAL and emits one consistent
self-contained file. No `-wal`/`-shm` sidecars to carry around.

**A read-only open needs the `-shm` index to already exist.** If Messages.app has
not opened the database this session, SQLite wants to create the shared-memory
index and cannot while read-only, and you get `SQLITE_CANTOPEN`. Open Messages
once, or pass `--allow-shm` to let it open read-write for that purpose.

**The grant belongs to the responsible process, not to the binary alone.** This
is the one that will waste your afternoon. TCC attributes a file access to the
*responsible process* — for a spawned child, normally whatever started it. So the
same binary gives opposite answers depending on who runs it:

| Invoked from | Result |
|---|---|
| a one-shot launchd job | **granted** — the binary is its own responsible process |
| a launchd job via `bash -c` (how `local-cron-jobs` runs it) | **granted** — launchd re-roots the chain, so the intervening shell does not shadow it |
| a shell whose parent holds no FDA (an agent, a sandboxed tool) | **denied** |
| a terminal that itself holds FDA | granted, but that proves nothing about the binary |

Measured here on 2026-08-31: `verify` reported `ok: readable` under launchd while
a direct run from an agent shell reported `denied by TCC` in the same minute. Row
two was the one worth checking rather than assuming — `../.claude/rules/macos-tcc.md` predicts it
("TCC attributes a read to the executing binary, not the job's shell") but the
agent-shell result above shows a parent *can* shadow a grant, so the two needed
reconciling. Kickstarting the `msgsnap-nightly` launchd job settled it: exit 0 and
a refreshed snapshot. The difference is launchd, not the shell — under launchd
each exec'd binary is evaluated on its own; under an app, the app is responsible
for everything it spawns.
`../.claude/rules/macos-tcc.md` documents the leak in the other direction — a terminal's own FDA
making an ungranted binary look granted. Same mechanism, opposite sign.

Consequences: `verify` is the only trustworthy check, and `snap` tries a direct
run first, then reruns itself through launchd when TCC refuses (exit 2). A
`verify` that prints nothing for 30s means **not granted** — an ungranted TCC
read under launchd blocks rather than failing, and once hung a job here for
27 minutes.

## Files

    msgsnap.sh              entrypoint
    src/msgsnap.swift       the FDA holder
    bin/msgsnap             build output (gitignored)
    tests/eintr-retry.sh    end-to-end check of the open(2) retry and exit codes;
                            builds its own copy of the binary, never bin/msgsnap
    tests/interpose_open.c  the open(2) stand-in that test loads with dyld
    tests/test_eintr_retry.py
                            `msgsnap.sh test`: runs eintr-retry.sh under unittest
                            for CI, and proves a wrong expectation reads as red

Snapshots go to `~/.local/state/msgsnap/`, deliberately outside this repo —
a copy of the Messages database has no business anywhere near a git tree.
