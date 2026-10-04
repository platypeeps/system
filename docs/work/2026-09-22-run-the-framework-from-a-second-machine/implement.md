---
title: run the framework from a second machine
created: 2026-09-22
item: sd:1335
---
# Implement — run the framework from a second machine

## Order

Each step ships alone. Steps 1–7 change nothing the hub runs.

1. **Trace the connection surface.** List every attribute of
   `sqlite3.Connection` and `sqlite3.Cursor` the 46 `sd_db` modules touch.
   Check: a grep-driven table in the design log names each attribute and its
   count; a run of the suite under a wrapper connection that raises on any
   attribute outside that table passes. The same trace lists, for the
   library and the pack: every plain `BEGIN` (gap C2); every
   `PRAGMA database_list` and every use of `executions`, `runner-journal`,
   `runner-ending`, `publications` and the two recovery-evidence directory
   names, with the verb that reaches each (gap (b)); and every caller that
   passes `default_path()` or tests that it exists (gap x1). Its result
   says whether R1 holds for x1.
2. **`sd_db.remote` and `sd_db.serve`, loopback only.** The proxy class and
   the server, no auth, bound to `127.0.0.1`. The frame format carries the
   protocol version and a request id field from the first commit, so later
   steps add checks and not fields. The server takes `<database>.serve.lock`
   beside the database through `runner_journal.lock` and refuses a second
   server.
   `sd-db.sh serve --loopback` and `sd-db.sh test --remote` land together.
   Check: the `local-sd-db` suite summary line over loopback equals the
   local one, zero skips (criterion 1); a second `serve` against the same
   file exits non-zero naming the lock, and the first keeps serving. The
   server logs the gap between frames inside each write transaction, and
   the longest gap across the suite goes in the log below (gap (g)).
3. **Handshake.** The first frame carries the client's package version,
   `SCHEMA_VERSION` and protocol version; the hub refuses any other package
   version with `BuildMismatch`, whose text names the side to upgrade.
   Check: two builds that share `SCHEMA_VERSION` and differ in package
   version are refused in both directions against one loopback server, no
   statement runs, and the message names the right side (criterion 10). A
   third test upgrades the server build under a connected client and
   asserts the client's next session is refused.
4. **`connect` reads `hub.json`.** The selector, the both-files refusal and
   `HubUnreachable`. Check: three unit tests — file absent opens locally;
   file present with no server raises `HubUnreachable` and creates nothing;
   file present beside a local `sd.db` refuses.
5. **Unknown outcome: ownership from `BEGIN`, the record in the
   transaction, rows retained.** One migration:
   `request_outcome (id TEXT PRIMARY KEY, committed_at TEXT)`
   under `local-sd-db/sd_db/schema/`, `SCHEMA_VERSION` bumped, the table
   added to the list in `local-sd-db/sd_db/schema.py`. The migration takes
   slot `019`, the next free slot: the schema is at 18 since sd:2581
   (`018_runner_run_repo_nullable.sql`). If another migration lands first,
   take the next free slot. The scope is Q1 option B in `design.md`,
   ruled 2026-10-04: no prune, no lifetime, no `expired`, no `ack(R)`, no
   `ClockSkew`. Rows are never deleted.
   The hub: one lock per id; `BEGIN
   IMMEDIATE` with R registers ownership before the statement runs, and
   refuses an R it has already seen; `COMMIT` from the owner inserts
   the record on the same connection, then commits; `COMMIT` from anyone
   else is refused; `ROLLBACK` and socket close settle ownership; the
   open-transaction idle timeout is 4 seconds, below `BUSY_TIMEOUT`
   (gap (g)); a plain `BEGIN` with no R opens a read transaction under
   `query_only`, whose `COMMIT` or `ROLLBACK` needs no R and writes no
   outcome row (gap C2); `outcome(R)` answers
   `recorded`, `in_flight` or `absent`. No job prunes the table, so gap (e)
   is moot. The client: `transaction` generates
   a ULID at `BEGIN IMMEDIATE`, sends R with `COMMIT`, raises
   `UnknownOutcome(R)` on a lost response, asks `outcome(R)` with a
   60-second backoff bound, returns `recorded` without re-running, and
   raises `TransactionLost(R)` on `absent`. It keeps no copy of the
   statements; nothing is re-sent under an old R.
   Check (criterion 11): (a) response dropped → `UnknownOutcome`,
   `recorded`, +1; (b) `kill -9` after the commit, restart, retry →
   `recorded`, +1, row exists; (c) `kill -9` before the commit, restart,
   retry → `absent`, `TransactionLost`, +0; verb run again, +1; (d) `COMMIT` frame held, reconnect, ask →
   `in_flight`; release → `recorded`, +1; (e) `COMMIT` from a non-owner →
   refused, +0; (f) to (i) were dropped with the prune (Q1, ruled
   2026-10-04); (j) hub answers `absent` for R →
   `TransactionLost(R)`, +0; the verb run again under a new id → +1, and
   the note's owner is the item that run created. A local connection ignores R: the local suite is
   unchanged. Check (gap C2): `usage.read`, `reads.usage_month` and
   `registry.merge` each return the local answer over a `write=False`
   remote connection and over a write connection; a write inside a plain
   `BEGIN` fails; `COMMIT` without R on a write transaction and `COMMIT`
   with R on a read transaction are both refused; no read adds a
   `request_outcome` row.
6. **Hub-only refusals.** `repository_lock` and `control_gate` raise
   `HubOnly` under a remote connection, naming the verb and the hub. So
   does every resolution of a directory beside the database that step 1
   listed, first `publication_journal.root` (gap (b)). `ledger.reserve`
   raises `LedgerRefused` naming the hub, and `ledger.release_orphans`
   sweeps nothing (gap C1). Criterion 1 survives by Q2 option A in
   `design.md`, ruled 2026-10-04. Tests that reach a hub-only path carry a
   `hub_only` marker, and under `--remote` the harness opens their
   connections locally and prints how many. A guard test runs each marked
   test over the wire and expects `HubOnly`.
   Check: over loopback, `sd-ship`'s delivery path refuses with that
   message and takes no lock; `sd-ship` to the pull request passes without
   reaching either gate; the serve log for the session records no lock.
   The `--remote` summary line equals the local one, zero skips.
   `sd-review` over loopback reports a URL provider as refused, runs its
   CLI lanes, and leaves every existing reservation row unchanged; no
   directory appears beside the client's default path.
7. **Peer identity.** Bind the Tailscale IP, resolve the peer with
   `tailscale whois --json --proto=tcp`, reuse the dashboard's refusal rules
   (`direct_context`, `local-project-dashboard/sd_dashboard/auth.py`).
   Check: a request from a tagged node is refused with no SQL run
   (criterion 6); the operator's node is accepted. Q3 option A in
   `design.md`, ruled 2026-10-04: no token on the tailnet listener, and a
   peer whose address is one of the hub's own is refused. Loopback keeps
   the owner-only token. Check also: a session from the hub's own Tailscale
   address is refused with no SQL run.
8. **Hub LaunchAgent and profile.** `local.system-tools.sd-serve` in
   `launchagents/`, listed in `personal.agent`; `stage_sd` loads it and
   prints `MISSING` when absent, `EXTRA` for a `hub.json` on the hub. The
   agent is bootstrapped in the operator's `gui/$(id -u)` domain; `serve`
   never opens with `create=True` and prints the resolved database path at
   start (gap (h)). The plist names an absolute path into this repository
   (gap (f)).
   Check: `machine-setup.sh status` on the hub counts zero drift after
   install; removing the plist counts one. `HOME=$(mktemp -d) sd-db.sh
   serve --loopback` exits non-zero naming the missing path and creates no
   file (gap (h)). The first launchd run of the agent raises no TCC prompt,
   and `sd-db.sh status` answers from it (gap (c)).
9. **Satellite stage.** `satellite` in `STAGES`: writes `hub.json`, asserts
   no local `sd.db`, verifies the installed `sd_db` package version equals
   the hub's, installs `providers.yaml` from the hub over the wire.
   Check: criterion 7; and a deliberate tag mismatch prints `DIFFERS` with
   the two versions.
10. **First real satellite: the second laptop.** The laptop runs as
    `/Users/<second-login>`; repository paths are home-relative since sd:1439
    (gap (a)). Prerequisites, before the stage runs:
    - **One interactive account.** List the satellite's local accounts
      with a uid of 501 or more. This checks logins only: under Q3 A every
      process on the satellite, service accounts included, is trusted (the
      trust boundary in `design.md`, Q3). A second login reopens Q3: take
      option B, a token beside the identity, first.
    - **Home-relative paths.** Check: a satellite session under
      `/Users/<second-login>` reads `sd today` and resolves every registered
      checkout.
    - **The autocommit deploy key.** `repo-sync-nightly` and
      `ai-apps-nightly` are in `local-machine-setup/profiles/common.cron`,
      so they run on every machine. They push to `main` with a per-machine
      deploy key (`local-autocommit/README.md`). On the laptop, run
      `sh local-autocommit/autocommit.sh key create`. It makes
      `~/.ssh/system_autocommit` and prints the `gh api` command that
      registers it.
    - **Registration, by the operator.** Register that key as a deploy key
      with write access on `platypeeps/system`. No session does this.
    - **Confirmation.** `sh local-autocommit/autocommit.sh status` exits 0
      on the laptop. It exits 1 when a pushing job is installed and the key
      is missing or readable by others. It also exits 1 when the org
      disallows deploy keys, which refuses every key at once. Only the
      enterprise can change that setting, so it is an operator escalation,
      not a retry.

    Then run the stage, then criteria 2, 3, 4 and 8. Record the two latency
    numbers here. Record also the longest gap between frames inside one
    write transaction, and the longest write transaction's wall clock; the
    second must stay under `BUSY_TIMEOUT` (gap (g)).
11. **Ship from the satellite, to the pull request.** Runs on the laptop
    after step 10, so it waits for the same item. `sd-ship` on the
    satellite opens the PR and records the receipt row; the hub's lane
    merges. Check: criterion 5. Satellite-side merge is not a config key
    and not a step here: it needs fencing (a lease with a fencing token
    checked by every write under the lock) and a test that drops the
    connection while the original subprocess still runs, as its own row.
12. **Docs.** `local-sd-db/README.md`, `local-machine-setup/README.md`,
    `CLAUDE.md` hub/satellite paragraph, and `local.system-tools.sd-serve` added to
    its list of plists that hold absolute paths into this repository
    (gap (f)). Check: `sd-docs-lint` clean;
    `python3 -m unittest tests.test_citations` green.

## Rollout

- Steps 1–7 merge with the hub unchanged: no LaunchAgent, no cron edit, no
  `runner.json` change. Criterion 9 checks it after each merge.
- Step 8 is the first hub change and is reversible by unloading one agent.
- Step 10 is the proof. Until it passes, the satellite stage is not run on
  any other machine.
- Fallback: if criterion 8 fails twice, open the A1 spike (sqld) as its own
  row; the handshake, the request id and the hub-only refusals carry over,
  because none of them depends on the transport.

## Verification

Named before the work:

- **The suite over the wire equals the suite on disk** (criterion 1). A
  smaller count means a path the proxy cannot carry.
- **No local file after a refusal** (criterion 3). `test ! -e` on the
  satellite after `sd today` fails.
- **One writer** (criterion 4). `lsof` on both machines.
- **Ship stops at the pull request** (criterion 5). No lock message in the
  serve log; the hub's lane merges.
- **Same schema, different build, refused** (criterion 10). Both
  directions; after a simulated hub upgrade too.
- **Lost `COMMIT`, six ways** (criterion 11). Response dropped, hub killed
  after and before the commit, frame delayed, non-owner commit, `absent`:
  the count moves by exactly one where a commit is promised and by zero
  everywhere else.
- **Drift both ways** (criterion 7). `--fail-on-drift` exits 1 on each.
- **Latency** (criterion 8). Two numbers in the log.
- **Gaps raised after #517.** Read transactions over `write=False` (step 5,
  C2); URL lanes refused and reservations untouched (step 6, C1); no
  directory beside the client's default path (step 6, (b)); every
  checkout resolved under another home (step 10, (a)); serve under an empty home fails closed (step 8, (h)); the
  longest in-transaction gap and write wall clock (steps 2 and 10, (g)).
- `sd-docs-lint` clean; `tests.test_citations` green; the `local-sd-db`
  suite green with zero skips.

## BLOCKING

None open. The operator ruled on the three step 5+ questions on
2026-10-04, recorded in `design.md`, "Questions for a ruling
(2026-10-04)". No step waits. Gap (a), the satellite's home, is closed: repository paths
became home-relative in sd:1439, which landed on 2026-09-24. The design
page's gap table maps every gap to its decision.

## Log

2026-09-22 — filed. Prompted by sd:1335, track T2 of sd:1334. Facts read:
`sd_db.connect` has no path override; three `fcntl` locks live beside the
file; the dashboard's 8768 listener already authenticates a Tailscale peer;
the personal profile already names every hub agent and job; the tailnet has
one other macOS node online. Recommendation: hub-side connection proxy
(`sd-db.sh serve`) behind the existing `connect` seam, locks carried on the
same channel, sqld kept as the fallback, file sync rejected.

2026-09-22 — revised after the Codex review of the planning PR, three
findings. (1) Locks over the session were wrong: a dropped connection does
not stop the satellite's subprocesses, so the hub would release the lock
under a live delivery. Decided: every operation under `repository_lock` or
`control_gate` runs on the hub; a satellite `sd-ship` stops at the pull
request. The open operator decision is closed by that finding. (2) The
hub-side schema check compares the file with the hub's library, not the
client's; added the first-frame handshake on package version, schema and
protocol, with `BuildMismatch` naming the side to upgrade. (3) A lost
`COMMIT` response is not a rollback; added `UnknownOutcome`, a
client-generated request id per write transaction, and retry by id.
Statement forwarding stays because the seam is `sd_db.connect`.

2026-09-22 — revised again, Codex round 2, one finding: "recording the
outcome before answering" was not atomic with the application write, so a
hub crash between the two left a committed row with no record. Now the
COMMIT handler inserts the `request_outcome` row on the same connection
before it commits, in one SQLite transaction; `outcome(R)` answers
`recorded`, `in_flight` or `absent`, with a closed socket's in-flight id
rolled back before `absent`; criterion 11 gained the `kill -9` cases on
both sides of the commit. The table needs a migration, added to step 5.

2026-09-22 — revised a third time, Codex round 3, two findings. Tracking
only the handler executing `COMMIT` missed a frame delayed in transit:
ownership is now registered at `BEGIN`, and `outcome(R)`, settlement and
retry admission share one lock per id, so absence proves termination.
Nightly pruning made `absent` ambiguous: rows are retained for a 7-day
retry lifetime measured from the ULID's time prefix, acknowledged or not,
and an older or acknowledged R is `expired`, never `absent`. Closed on the
same reading: the ULID's randomness as the cross-satellite uniqueness
guarantee, `ClockSkew` at `BEGIN`, a read check on re-execution (removed
in the next round), a backoff bound longer than the open-transaction
idle timeout, one server per file by `serve.lock`, and a lost `ack` reply
as harmless.

2026-09-22 — revised a fourth time, Codex round 4, one finding, taken as
written. Re-executing the transaction's statements on `absent` was wrong:
a write produces values, `lastrowid` among them, that later statements
bind — `create_item` writes the new id into the opening note — and no
read comparison sees them. Now `absent` raises `TransactionLost(R)` out
of the `with` block, the busy-file shape, and the verb is run again under
a new id; the remote connection keeps no copy of what it forwarded, and
an R is sent once. Step 5 and criterion 11 (c) and (j) follow.

2026-09-23 — revised to close the gaps raised after #517; plan only, no
code. The two Codex findings in the item's note of 2026-09-22 and the eight
portability gaps in its note of 2026-09-23 each have a row in the design
page's new gap table. Decided: C1, ledger ownership and URL-provider calls
stay on the hub; C2, a plain `BEGIN` opens a read transaction under
`query_only` with no R; (b), directories beside the database are hub-only
by connection kind; (c) and (d), stated; (e), the prune is a command in
`sd-db-backup`; (f), the plist gotcha; (g), the idle timeout drops from 30
to 4 seconds, below `BUSY_TIMEOUT`, with two measurements; (h), the serve
agent runs in the operator's gui domain and never creates a database.
Split: (a), the `HomeMismatch` refusal is decided, and the answer for a
satellite with another home is a `BLOCKING:` line, because the second laptop
runs as `/Users/<second-login>`. Carried: x1, pack callers that name
`default_path()`, to step 1. Added: the autocommit deploy key as a step 10
prerequisite. The migration now takes the next slot after sd:1099's lands.

2026-09-24 — gap (a) decided by the operator: repository paths become
home-relative, as `~/repos/...`, in a separate item that this one depends
on. The `BLOCKING:` line is replaced by the decision in `prd.md`. The design
records why the key is relative to the home and not to the repositories
root, and that `HomeMismatch` guards until that item lands. Steps 10 and 11
wait for it; step 10 then drops the home equality from the first frame.

2026-09-24 — sd:1439 landed (#554, `78e43d8`) before step 1 was built.
Operator decisions: build steps 1–2 now and decide steps 3–7 after
criterion 1; do not build `HomeMismatch` or the satellite's home
`DIFFERS`, since no absolute row is left for them to guard; step 5's
migration takes slot `015`, because `012` (sd:1099) and `014` (sd:1439) are
on `main`, and the wait on sd:1099 is removed.

2026-09-24 — steps 1 and 2 built, loopback only. Step 3 onward is not
built; the operator decides it after criterion 1.

Step 1, the connection surface. "Library" and "tests" count grep hits in
`sd_db` (without `remote`, `serve` and `testing`) and in `tests` (without
`test_wire`). "Run" counts touches by `sd-db.sh test --surface` with
`--record`, which runs the whole suite through every `connect` that does
not create.

| Object | Attribute | Library | Tests | Run |
|---|---|---|---|---|
| Connection | `execute` | 381 | 616 | 102559 |
| Connection | `executescript` | 2 | 19 | 1 |
| Connection | `in_transaction` | 13 | 25 | 18976 |
| Connection | `close` | 28 | 76 | 2281 |
| Connection | `commit` | 0 | 56 | 90 |
| Connection | `total_changes` | 0 | 30 | 44 |
| Connection | `iterdump` | 0 | 59 | 223 |
| Connection | `set_trace_callback` | 0 | 4 | 4 |
| Connection | `row_factory =` | 4 | 5 | 5 |
| Connection | `backup` | 1 | 19 | 239 |
| Connection | `with connection:` | — | — | 2 |
| Connection | `create_function` | 2 | 0 | 0 (create opens only) |
| Connection | `executemany`, `cursor` | 0 | 0 | 0 |
| Cursor | `fetchone` | 150 | 251 | 22466 |
| Cursor | `fetchall` | 35 | 21 | 2796 |
| Cursor | `lastrowid` | 13 | 2 | 9092 |
| Cursor | `rowcount` | 8 | 5 | 2771 |
| Cursor | iteration | — | — | 63794 rows |
| Cursor | `description`, `fetchmany` | 0 | 0 | 0 |

`hasattr(cursor, "keys")` also reads 8 times; it is a probe, absent on
`sqlite3.Cursor` too. The surface is the twelve connection and six cursor
names in `CONNECTION_SURFACE` and `CURSOR_SURFACE` of
`sd_db.testing.surface`. The guarded suite passes, `sd-db.sh test
--surface`: "Ran 1672 tests in 219.848s", "OK". Findings:

- The PRD's list missed five names. The tests use `commit`,
  `total_changes`, `iterdump` and `set_trace_callback`; `restore` and the
  tests use `backup`. The remote connection carries all five.
- `create_function` stays out. Only `paths.install` calls it, from
  migration 014, on a `create=True` open: a hub verb.
- `control_gate` tested `isinstance(database, sqlite3.Connection)` and sent
  anything else to `Path`. Under the guard, 61 tests failed with
  `TypeError`. It now tests for a path instead.
- Two `test_publication` tests used a library connection as a backup
  target and as a migration connection. They now use a raw connection to
  the file.
- Plain `BEGIN` (C2): `registry.merge`, `usage._snapshot` and
  `reads.usage_month` in the library. `migrate` and the dashboard's
  `operations_screen` also open one; both run on the hub. The pack has none.
- `PRAGMA database_list` (b): 15 library sites, none in the pack. By verb:
  `backup.run` and `retention._output_files` (backup); `migrate`;
  `control_gate` (operations, restore, dashboard controls);
  `publication_journal.root`, `writing._journal_path` and
  `writing.cutover_pieces` (`sd writing`); `registry.beside` (registry
  reads); `removal._main_file` and `removal._store_directory` (repo and
  item remove); `runner_controls.control` (runner and dashboard); and
  `runner_exec` `prepare`, `reconcile`, `read_execution` and `_validate`
  (runner and dashboard).
- Directories beside the database (b): `backup`, `runner_exec_backup`,
  `removal`, `retention`, `publication_journal`, `runner_journal`,
  `runner_exec` and `writing.piece_state`. The pack names none. New:
  `writing.piece_state` reads the publication journal, so the `sd writing`
  state read is hub-only. The design's hub table does not list it.
- x1, pack callers of `default_path()`. `sd-status` tests `.exists()`
  three times. `sd_install` (trials), `sd_codex` and `sd_registry` test it
  before they connect. `sd-review`, `sd-ship` and `sd_check_receipts` pass
  it to `connect`. `sd_runner` passes it to `service_installation`, which
  is hub-only. `sd_registry` falls back to the local `providers.yaml`,
  which step 9 installs.
- x1 result: R1 holds without a pack edit only if step 4 makes
  `default_path()` hub-aware. That means a path whose `exists()` asks the
  hub, and `connect` treating that path as the default. If step 4 cannot,
  R1 needs the operator.
- For step 6: a refusal of hub-only operations by connection kind breaks
  criterion 1's equality. The control, beside-directory and publication
  tests would refuse over loopback. Step 6 must keep those tests local or
  serve them as the hub.

Step 2. `sd_db.remote.Connection` carries the surface above over one TCP
session. A frame is a 4-byte length and UTF-8 JSON with `v` (protocol 1),
`rid` (null until step 5) and `op`. Rows come back as `sqlite3.Row`.
Errors keep their class, text, SQLite error code and `sd_db` attributes.
`backup` fetches the hub's serialized image in chunks of at most 64 MiB,
then copies it through a local backup, so the WAL header bytes match a
local backup. `sd_db.serve` opens each session with the library's own
`open_local`, binds `127.0.0.1` only, refuses a session that would create
a database, and takes `<database>.serve.lock` through `runner_journal.lock`.
It writes a fresh token to `<database>.serve.token`, mode 0600, and
refuses an `open` frame without it. `sd-db.sh serve` without `--loopback`
refuses. `sd-db.sh test --remote HOST:PORT TOKEN_FILE` runs the suite with
every non-creating `connect` sent to that server. `create=True` opens
(`init`, `migrate`) stay local, since they are hub verbs. `test_wire`
covers the frame, the token, the refusals, the lock, the error classes,
the backup bytes and chunks, and the gap log. Its children run under a
scratch `HOME`, so no mutation of `serve` can reach the live database.

Review round 1 (`sd-ship prepare`, Codex), two blocking findings, both
taken. (1) High, security: loopback is not a user boundary, and this
machine has a second account (`test`, uid 502). The plan's "no auth" for
step 2 assumed one user. Fixed with the owner-only token above; step 7
still adds peer identity. (2) Medium: one `serialize` frame capped a
backup near 192 MiB, and an oversized answer dropped the session. Fixed
with chunks, and an oversized answer now returns an error frame. The live
database is 82 MB, so no backup had failed yet.

Review round 2 (Copilot on #568), five findings, all taken. (1) An
explicit `surface.uninstall` in a guarded test emptied the opener stack,
so the setUp cleanup cleared the wire opener for the rest of a `--remote`
run. `uninstall` with nothing to undo now changes nothing, and that test
moved to its own case. (2) The lock and token had fixed names per folder,
so serving `a.db` blocked `b.db` beside it. They are now
`<database>.serve.lock` and `<database>.serve.token`. (3) The token was
written before the bind, so a port in use left it behind. The server now
binds first, names the refused port, and exits 1 with no token. (4) The
`--remote` harness accepted `localhost` and `::1`, which the server does not
bind. It now accepts `127.0.0.1` only. (5) A stale "no authentication" line
in the `sd_db.remote` docstring now names the token.

Criterion 1, on the final commit (rebased onto `22c9591`, with both
review rounds), local and loopback run in parallel:

- Local, `sd-db.sh test`: "Ran 1676 tests in 422.606s", "OK".
- Loopback, `sd-db.sh test --remote 127.0.0.1:PORT TOKEN_FILE`: "Ran 1676
  tests in 503.493s", "OK"; "2465 sessions over the wire, 1363 local
  creates".
- No skips in either run.
- Before round 2's fix (1), the same suite sent 2290 sessions and made
  1221 local creates. Four new tests do not account for the rise. The
  likely cause is the dropped wire opener: modules discovered after
  `test_wire` ran locally. Not proven per test.

Gap (g), from the serve log of that loopback run: "longest in-transaction
frame gap 1065.8 ms across 9906 write transactions, before 'COMMIT'". The
test that holds it is `test_removal`'s concurrent-writer test, which sleeps
1.0 s inside `BEGIN IMMEDIATE` on purpose. The next are 539 ms, 252 ms and
203 ms, matched by duration to deliberate sleeps in `test_recurrence`
(0.5 s) and `test_ledger` (0.2 s). The longest write wall clock is 1066 ms.
An earlier run under heavier load reached 3100 ms for one transaction of
4006 frames, 0.8 ms a frame. Every gap is below the 4 s idle timeout of
step 5. That timeout is per gap, not per transaction, so a long bulk write
does not reach it.

Not verified. A test that starts `sd-db.sh` or Python in a child process
opens its database locally, because the seam is in-process. The three
gaps below the longest are matched to their tests by duration, not by
session. CI runs `test_wire` in the library suite; it does
not run the whole suite over loopback.

2026-09-25 — steps 3 and 4 built, loopback only. Operator decisions
before the build: `HomeMismatch` stays dropped, so the handshake has three
fields; and x1 is resolved in step 4 by a hub-aware `default_path()`, so
the pack is not edited.

Step 3. The `open` frame carries `package` (`sd_db.__version__`) and
`schema` (`SCHEMA_VERSION`) beside `v`. Before it opens anything, the hub
checks the protocol, then the token, then the package, then the schema.
It refuses the first difference with `sd_db.remote.BuildMismatch`. The
text names both versions and the side to upgrade, the older one by
numeric order, or both when there is no order. The serve log records
`refused before open`. The tests serve a copy of this folder whose package
version is one higher and whose schema is equal. They cover both
directions, a hub upgraded under a connected client (the old session is
`HubUnreachable`, the next is refused), the schema and the protocol, and a
raw session whose `INSERT` after the refusal is refused as "the first
frame must be open", with zero rows.

Step 4. `sd_db.hub` reads `~/.config/sd/hub.json` (`hub`, `port`, and
`token_file` for loopback until step 7). With the file, `connect` sends a
`None` path, or any path equal to the local default, to the hub, with a
5-second connect timeout. Without it, `connect` opens the local file as
before. An explicit other path still opens locally, such as the Jev meter
or a backup target. The file beside a local `sd.db` raises `HubConflict`.
A hub that does not answer raises `HubUnreachable`, which names the hub
and the config file. `create=True` is refused as a hub verb, and nothing
is created: no file, no folder. A malformed file raises `HubConfigError`
and never falls back to the local file.

x1, resolved as decided. `default_path()` returns `HubPath` when
`hub.json` exists. It compares, prints and hashes as the plain path;
derived paths are plain. Its `exists()` opens a read session on the hub.
`tests/test_hub.py` writes through the three pack shapes: `connect()`,
`connect(default_path())` (`sd-ship`) and `connect(Path(str(default_path())))`
(`sd-review`). Each row lands in the hub's file and none on the satellite.
`sd-db.sh serve` now defaults to `local_path()`, never the hub-aware path.

Fail-first, each mutation run against its tests and restored:

- The hub's `check_handshake` call removed: all four `TheHandshake` tests
  fail, "BuildMismatch not raised".
- `connect` never reads `hub.json`: four of six `test_hub` tests fail,
  "HubConflict not raised" and "FileNotFoundError: no database at".
- `default_path` never returns `HubPath`: the x1 test fails, "is not an
  instance of <class 'sd_db.hub.HubPath'>".
- Routing only a `HubPath` and not an equal path: the `sd-review` shape
  fails, "FileNotFoundError: no database at".

Criterion 1, on the first commit of this branch, based on `2a6e30a`,
local and loopback run in parallel:

- Local, `sd-db.sh test`: "Ran 1687 tests in 683.944s", "OK".
- Loopback, `sd-db.sh test --remote 127.0.0.1:PORT TOKEN_FILE`: "Ran 1687
  tests in 758.639s", "OK"; "2475 sessions over the wire, 1373 local
  creates".
- No skips in either run. This branch adds ten tests; the step 2
  measurement of 1676 predates three commits on `main`, and the eleventh
  is not traced.

Review round 1 (`sd-ship prepare`, Codex), two blocking findings, both
taken.

1. Medium: the package version is `0.1.0` and has not changed since the
   file was first committed. So two builds with different SQL passed the
   handshake. Fixed: the first frame also carries `build`, a SHA-256
   digest of the package's `.py` and `.sql` files. It is checked after
   the package and the schema, whose order names the side to upgrade. A
   digest difference says "install the same sd_db build". A new test
   serves a copy with the same version and one changed file. With the
   digest check removed, it fails with "BuildMismatch not raised".
2. Medium: `connect` parsed `hub.json` before it knew the target, so a
   malformed file also blocked an explicit meter or backup database.
   Fixed: the file is read only when the target is the default. The
   malformed-file test now also opens another database. On the unfixed
   `connect`, it fails with "HubConfigError: ... names no 'hub'".

Criterion 1 after round 1, local and loopback run in parallel:

- Local, `sd-db.sh test`: "Ran 1688 tests in 558.191s", "OK".
- Loopback: "Ran 1688 tests in 697.400s", "OK"; "2477 sessions over the
  wire, 1376 local creates".
- No skips in either run.

Review round 2 (`sd-ship prepare`, Codex), one blocking finding, taken.
Medium: `connect` still ran `hub.configured` before it compared the
target. A `stat` refused inside an unreadable `.config` raised
`PermissionError` for another database. Fixed: the target is compared
first, so another database never touches the selector's folder. A new
test makes `.config` mode 0 and opens a meter database. On the round-1
`connect`, it fails with "PermissionError: [Errno 13] Permission denied".

Criterion 1 after round 2, local and loopback run in parallel:

- Local, `sd-db.sh test`: "Ran 1689 tests in 501.453s", "OK".
- Loopback: "Ran 1689 tests in 659.822s", "OK"; "2478 sessions over the
  wire, 1377 local creates".
- No skips in either run.

Review round 3 (`sd-ship prepare`, Codex), one blocking finding, taken.
Medium: comparing the target resolved the default path. So a folder
under the home that could not be searched blocked another database. Now
the comparison is lexical (`os.path.abspath`), with no filesystem access.
The pack passes `default_path()` or its string back, and both compare
equal. The round-2 test now also refuses `realpath` under the home. On
the round-2 `connect`, it fails with "PermissionError: [Errno 13]
Permission denied: .../satellite/.local/share/sd/sd.db".

Criterion 1 after round 3, the final code, local and loopback run in
parallel on a loaded machine:

- Local, `sd-db.sh test`: "Ran 1689 tests in 1077.744s", "OK".
- Loopback: "Ran 1689 tests in 1690.116s", "OK"; "2478 sessions over the
  wire, 1377 local creates".
- No skips in either run.

Review round 4 (`sd-ship prepare`, Codex, head `22eea47`), one blocking
finding, taken. High: the lexical comparison missed an aliased spelling
of the default, such as a symlink or macOS `/var` against `/private/var`.
That path skipped the selector and `HubConflict`, and could open or create
a divergent local record. Now the comparison is lexical first and then
canonical (`resolve()`). An `OSError` while resolving means "not the
default", so the round-3 unreadable-home test still passes. A new test
reaches the default through a symlinked home. On the round-3 code it fails
with "AssertionError: HubConflict not raised".

Criterion 1 after round 4, local and loopback run in parallel:

- Local, `sd-db.sh test`: "Ran 1690 tests in 430.652s", "OK".
- Loopback: "Ran 1690 tests in 470.489s", "OK"; "2478 sessions over the
  wire, 1378 local creates".
- No skips in either run.

Not verified. Nothing ran on a second machine: the server still binds
loopback only, and `token_file` stands in for step 7's peer identity. The
digest covers the package's own files, not the Python interpreter or the
`sqlite3` library it links.

2026-10-04 — the step 5+ questions drafted for a ruling; docs only, no
code. The operator asked for them on 2026-09-30 (item note 6996). The
design page gains "Questions for a ruling (2026-10-04)". Q1 is step 5's
scope. Q2 is how criterion 1 survives step 6's refusals. Q3 is whether a
token joins step 7's peer identity. The evidence gives three questions, as the
ruling expected. x1 is closed, satellite merge is out of scope, and
`writing.piece_state` joins the hub table under gap (b). Step 5's migration
moves from slot `015` to `019`: migrations 015 to 018 landed meanwhile, the
last for sd:2581.

2026-10-04 — the operator ruled and took every recommendation: Q1 = B,
Q2 = A, Q3 = A. Q1 changes `prd.md`. R12 drops the retry lifetime,
`expired` and `ack(R)`, and criterion 11 drops (f) to (i). Step 5 drops the
prune, `ClockSkew` and the `acked_at` column. Gap (e) is moot, because no
job prunes the table. Step 6 carries the `hub_only` test carve-out. Step 7
refuses the hub's own address, and step 10 lists the satellite's accounts
first. The `BLOCKING` section is clear again.
