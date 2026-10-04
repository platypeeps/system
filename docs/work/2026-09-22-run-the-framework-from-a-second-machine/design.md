---
title: run the framework from a second machine
created: 2026-09-22
item: sd:1335
---
# Design — run the framework from a second machine

## The shape: hub and satellite

One machine is the **hub**. It holds `~/.local/share/sd/sd.db`, the runner,
the dashboard, `task-actions`, the backup job, the cron intake and the
digests. Today that is the personal profile's machine
(`local-machine-setup/profiles/personal.agent`, `personal.cron`).

Every other machine is a **satellite**. It runs sessions, `sd-review` and
`sd-ship` on its own checkouts, and it reaches rows over Tailscale. It holds
no database file. The prior item `docs/work/2026-09-05-one-database-one-front-door/`
made the hub the one store; this item makes that store reachable.

## The seam

`local-sd-db/sd_db/__init__.py` promises that nothing outside the package
calls `sqlite3.connect`. `source:local-sd-db/sd_db/database.py::connect` is
therefore the one place a second backend can be selected, and the pack does
not change (R1). The selector is a machine-config file, not an environment
variable: cron and launchd read no shell profile, and a satellite must refuse
the same way from every caller.

    ~/.config/sd/hub.json      {"hub": "sol", "port": 8769}

When the file exists, `connect` returns a remote connection to that hub. When
it does not, `connect` opens the local file as today. When it exists **and**
the local file exists, `connect` refuses: a machine cannot be both. That
refusal is what R4 rests on, and the satellite stage asserts the file's
absence.

The config lives beside `dashboard.json` and `runner.json`, which already sit
in `~/.config/sd/` on the hub, so the satellite stage adds one file to a
directory the fleet already manages.

## Options

### A1 — SQL over the wire with sqld / libsql

The row's own first suggestion: run `sqld` on the hub bound to the Tailscale
IP, and have `connect` return a libsql connection.

Measured against the code, it is not a drop-in:

- The library leans on `sqlite3` API details a libsql client does not carry
  the same way: 37 `sqlite3.Row` sites, 13 `lastrowid`, 13 `in_transaction`,
  savepoints inside `BEGIN IMMEDIATE`, in `transaction` of
  `local-sd-db/sd_db/database.py`, 43 `PRAGMA` sites including
  `journal_mode`, `busy_timeout`, `query_only`, `foreign_keys` and
  `user_version`. Each would need an adapter, and the row's note already
  names "pragmas or WAL semantics" as the expected break.
- Neither `sqld` nor `libsql_client` is installed here today (`which sqld`
  empty; the pack venv raises `ModuleNotFoundError: No module named
  'libsql_client'`). It adds a server binary the fleet does not manage and a
  Python dependency the pack's installer does not provision.
- It carries no handshake and no request id, so R9 and R12 would be built
  beside it rather than in it.

Kept as the fallback if A2's proxy proves too slow (criterion 8).

### A2 — a hub-side connection proxy: `sd-db.sh serve` — recommended

The hub runs a small server, `sd_db.serve`, started by `sd-db.sh serve` and
kept up by a LaunchAgent in the hub profile. Per satellite session it opens
one real `sqlite3.Connection` with the library's own `connect`, and it
executes the satellite's statements on it. The satellite side is
`sd_db.remote.Connection`, a class carrying only the attributes the library
uses: `execute`, `executemany`, `executescript`, `in_transaction`,
`lastrowid`, `row_factory`, `close`, and cursors with `fetchone`, `fetchall`
and `description`. Rows come back as `sqlite3.Row`-compatible objects built
from `description`.

Why this and not A1:

- **Same semantics by construction.** The hub side *is* a `sqlite3.Connection`
  from the library's own `connect`, so `BEGIN IMMEDIATE`, savepoints,
  `busy_timeout`, WAL, the version refusals and `foreign_keys` behave exactly
  as for a local caller. R2 and R3 fall out: the write lock is SQLite's own,
  taken on the hub.
- **No new dependency.** Standard library on both sides: `http.server` or a
  length-prefixed JSON stream over one TCP connection, `json`, `sqlite3`.
  The dashboard already ships an `http.server` listener on a Tailscale IP;
  the peer check is the same code.
- **The locks never cross the wire.** `source:local-sd-db/sd_db/runner_journal.py::lock`
  is `fcntl.flock` on a hub file, and its two callers that guard an
  operation — `ship-locks`, in `repository_lock` of `local-sd-db/sd_db/ship.py`,
  and `operation-locks` under `control_gate` — stay hub-only. Under a remote
  connection both refuse by name, and the satellite's `sd-ship` stops at the
  pull request. A hub-held lock released on session drop was the first
  draft, and it is wrong: a partition or an idle timeout drops the session
  while the satellite's git and GitHub subprocesses keep running, another
  delivery takes the lock, and R3 is broken with no error anywhere.
- **The build is checked before the first statement.** The first frame of
  a session carries the client's package version, `SCHEMA_VERSION` and the
  protocol version. The hub refuses any package version other than its own
  with `BuildMismatch`, whose text says which side to upgrade. The hub-side
  `connect` still runs `schema_version` against the file, so `SchemaTooOld`
  and `SchemaTooNew` guard the file against the hub's library; they cannot
  guard it against the client's, because two builds can share a schema
  number and send different SQL, and a hub upgraded after the satellite was
  set up would otherwise admit the older client.

Costs, stated:

- **One round trip per statement.** `sd-status` runs many. Criterion 8
  budgets it at three times the local wall clock and measures it on the LAN
  and over the internet. If it fails, A1 with batching is the fallback, or
  the proxy learns to pipeline reads.
- **A session is a hub-side connection.** The server caps sessions (one
  operator, a handful of shells) and closes idle ones after a timeout, so a
  crashed satellite cannot hold `BEGIN IMMEDIATE` forever. The cap and the
  timeout are config, and both are printed by `sd-db.sh status`. **One
  server per file.** The server takes an exclusive `fcntl.flock` on
  `<database>.serve.lock` beside the database at start, through the same opener as
  the runner journal, and a second one refuses with
  `another server owns this database`. Ownership lives in the server's
  memory, so two servers over one file would be two registries, and an id
  owned in one would read `absent` in the other.
- **Not a general SQL endpoint.** It accepts a session only from the
  operator's login, and it runs whatever SQL that session sends. That is the
  same trust the local file grants a shell today; the boundary is the
  tailnet identity, not statement filtering.
- **A lost response is not a local failure.** Locally, a `COMMIT` either
  returns or raises, and the caller knows which. Over the wire, the hub can
  commit and the response can still be lost; closing the session rolls
  nothing back, and the caller sees a transport error. Retrying a
  non-idempotent verb would then duplicate the row. So every write
  transaction carries a request id R, generated by the client when
  `transaction` sends `BEGIN IMMEDIATE` and sent again with `COMMIT`.

  **R is a ULID**: 48 bits of the client's clock in milliseconds and 80
  random bits. The randomness is what makes R unique across satellites —
  two machines generate ids with no coordination, and a collision would let
  one satellite read, acknowledge or expire another's outcome. The time
  prefix is what lets the hub tell an id's age without a row.

  **Ownership from `BEGIN`.** When the hub receives `BEGIN IMMEDIATE` with
  R, it registers R as owned by that connection at that moment, under one
  hub-side lock per id, before the statement runs. Ownership leaves a
  connection only by that connection's own settlement: `COMMIT` applied,
  `ROLLBACK`, or the socket closing, which makes SQLite roll the open
  transaction back. `outcome(R)`, settlement and `BEGIN` admission all run
  under the same per-id lock, so a lookup cannot interleave with a late
  `COMMIT`: while R is owned the answer is `in_flight`, whatever frame is
  still in transit or queued ahead of the handler. A `COMMIT` for R from a
  connection that does not own R is refused and not applied. Absence
  therefore proves termination, because nothing unowned can still commit.
  Tracking only the handler that is executing `COMMIT` was the second
  draft, and a frame delayed in transit slipped past it.

  **The outcome record is inside the forwarded transaction.** When the hub
  applies `COMMIT` for R, its COMMIT handler first executes
  `INSERT INTO request_outcome (id, committed_at) VALUES (R, now)` on the
  same connection, then `COMMIT`. One SQLite transaction carries the
  application writes and the record, so either both exist or neither, and
  a hub crash at any point leaves the file in one of exactly two states.
  Recording "before answering" beside the transaction was the first draft,
  and a crash between the two left a committed row with nothing to
  deduplicate against. The seam is the proxy's COMMIT handler, not the
  caller; `transaction` sends R and never sees the table. `request_outcome`
  is one new table, in the migration the implement page lists.

  **Recovery, as the protocol's reply to `outcome(R)`.** Four answers:

  - `recorded` — the row exists; the transaction committed. The client
    sends `ack(R)`, then returns the outcome to its caller; nothing runs.
  - `in_flight` — R is owned by a live connection. The client waits with a
    bounded backoff and asks again; at the bound it raises
    `UnknownOutcome(R)` naming the id. The bound (60 seconds) is longer
    than the hub's open-transaction idle timeout (4 seconds, below
    `BUSY_TIMEOUT`: a session that has held `BEGIN IMMEDIATE` silent that
    long holds the write lock the runner and the dashboard wait on, and is
    closed; gap (g) below says why 4 and not 30), so an abandoned owner is
    always settled inside the bound.
  - `absent` — no row, no owner, and R inside the retry lifetime: the
    transaction did not commit. The client raises `TransactionLost(R)`,
    and R is never sent again: a `BEGIN IMMEDIATE` carrying an R the hub
    has seen — owned, recorded or acknowledged — is refused, so an id is
    used once.
  - `expired` — R is older than the retry lifetime, or already
    acknowledged. Never answered `absent`, so a pruned row cannot be read
    as a transaction that did not run.

  What `TransactionLost(R)` is: the exception leaves the
  `with transaction(connection)` block the way `sqlite3.OperationalError`
  leaves it today when `BEGIN IMMEDIATE` finds the file busy past
  `BUSY_TIMEOUT`. Nothing was written, and the verb exits non-zero naming
  R. The verb is then run again — by the operator, or by a caller that
  chooses to — and its body computes every value afresh, `lastrowid`
  among them, so a re-run cannot bind an id that another writer took in
  between. The helper itself re-runs nothing: `transaction` is a
  `contextmanager` that yields once (`yield connection`, in `transaction`
  of `local-sd-db/sd_db/database.py`), and the remote connection keeps no
  copy of the statements it forwarded. Re-executing forwarded statements
  was the third draft, and it was wrong: a write produces values, among
  them the `lastrowid` that `create_item` binds into the item's opening
  note, that later statements depend on, and no re-execution can know them
  without running the body again.

  **Retention and lifetime.** A `recorded` row stays for R's retry
  lifetime, **7 days** from the time in R: longer than any hub outage or
  laptop suspension the operator would tolerate before intervening, and
  short enough that the table stays small. `ack(R)` marks the row consumed
  and never deletes it — an acknowledged row deleted early would answer a
  young id `absent`, which is the ambiguity this paragraph exists to
  remove — and the unacknowledged rows older than the backoff bound are
  what `sd-db.sh status` lists as outcomes no satellite ever read. A lost
  `ack` reply costs nothing: the client already holds the outcome, a
  repeated `ack(R)` is idempotent, and the row stays listed as unread. The
  nightly prune deletes only rows past the lifetime, and `expired` is
  answered from R's own age before any row is looked up, so prune and
  refusal agree by construction. The hub refuses at `BEGIN` any R whose
  embedded time is more than five minutes from its own clock
  (`ClockSkew`, naming both), which keeps the age it reads out of R honest.

  Statement forwarding stays, rather than operation-level requests, because
  the seam is `sd_db.connect` and 46 modules speak SQL through it; an
  operation API would be a second seam the pack would have to learn.
  Ownership from `BEGIN` gives the same boundary an operation record would,
  and the record-in-transaction design gives the same atomicity; what is
  left is one table plus a COMMIT handler against that second seam.

### B — SSH: run the verb on the hub

`ssh hub sd-note add ...`. Correct for locks, rows and receipts, because
everything runs where it already works. Rejected as the primary shape for
two measured reasons:

- The verbs that matter read the **checkout**. `sd-review --scope branch`
  diffs the local branch; `sd-ship` reads the branch, the PR and the
  worktree; `sd-status` reads `docs/work` of the cwd. A satellite branch is
  not on the hub until it is pushed, and the hub's primary checkouts have one
  owner each (`CLAUDE.md`, "Three sessions share this checkout"): an SSH
  session may not move their HEAD.
- It keeps a second copy of every checkout current on the hub, which is the
  runner-clone problem again without the runner's leases.

Kept for one use: an operator's ad-hoc `ssh hub sd-db.sh status` and the
hub-only verbs (`repo remove`, `restore`, `migrate`), which a satellite must
not run.

### C — copy or sync the file — rejected

Syncthing, rsync, iCloud, or an SMB mount. Two writers on one SQLite file
without a shared `fcntl` implementation corrupt it or fork it; WAL mode makes
it worse, because the `-wal` and `-shm` sidecars must be seen by every
process through one kernel. A fork is silent, and `sd-db-backup.job` would
snapshot it. This is the failure R4 names, and no amount of scheduling makes
it safe.

## Decision

**A2**, with **B** for hub-only verbs and **A1** as the named fallback.

**Delivery is handed to the hub.** A satellite `sd-ship` stops at the pull
request; the hub's lane merges, the runner for `repo.runner_merge=auto` and
a person for `manual`. Every operation that runs under `repository_lock` or
`control_gate` — the merge, anything that moves a HEAD or a journal file,
service controls, restore — runs on the hub, and a satellite verb that
reaches one of those gates refuses by name.

The reason is the review finding on the first draft, not preference. A
dropped connection does not mean the satellite's operation stopped: a
partition or an idle timeout releases a hub-held lock while the satellite's
git and GitHub subprocesses continue, and another delivery can take the same
repository lock. That breaks R3 and the concurrent-ship guarantee, and the
"kill the client" test the first draft named would not have caught it,
because the client's death is exactly the case where the subprocesses die
too. Keeping the protected operations on the hub removes the case instead
of fencing it.

Flipping this later is its own row, and it needs fencing: a lease with a
fencing token that every write under the lock checks, and a test that drops
the connection while the original subprocess is still running.

## What must remain on the hub

A row marked *hub only* never exists on a satellite (R5, ruled
2026-10-04): a satellite runs none of these services and holds no local sd
database. Their launchd jobs are named once, in
`source:local-machine-setup/machine-setup.sh::SD_HUB_ONLY_AGENTS`; the
satellite stage reports each one installed or loaded as `EXTRA`.

| Capability | Why it cannot move | Where it is declared |
|---|---|---|
| The database file — *hub only* | One writer; WAL sidecars | `DEFAULT_RELATIVE`, `local-sd-db/sd_db/database.py` |
| Runner (`local.system-tools.sd-runner`) — *hub only* | Leases and `executions/` are hub paths | `personal.agent` |
| Dashboard (`local.system-tools.sd-dashboard`) — *hub only* | Reads the file; serves 8443 and 8768 | `personal.agent`, `RUNTIME.md` |
| `task-actions` — *hub only* | Funnel-published; per-machine TCC | `personal.agent` |
| `sd-db-backup`, `offsite-*`, `mirror-sync-nightly` — *hub only* | Snapshot the local file and the NAS mount | `personal.cron` |
| Cron intake and digests — *hub only* | Write rows on a schedule; one clock | `personal.cron` |
| `repo remove`, `item remove`, `restore`, `migrate` | Take `control_gate` and move journal files | `sd-db.sh help` |
| Delivery: the `sd-ship` merge and every path under `repository_lock` | A lock held over a droppable session cannot outlive the subprocesses it guards | `repository_lock`, `local-sd-db/sd_db/ship.py`; Decision |
| Service controls and every path under `control_gate` | Same reason; `PRAGMA database_list` names a hub path | `control_gate`, `local-sd-db/sd_db/operations.py` |
| `providers.yaml`, `commands.yaml` | Read beside the database | `registry.beside`, `runner_exec` |
| Ledger reservations, the orphan sweep, and every provider call charged through `sd_db.calls` | The sweep judges an owner by a local `os.kill`; a pid from another kernel reads as dead | `release_orphans`, `local-sd-db/sd_db/ledger.py`; gap C1 |
| Every directory beside the database (`executions/`, `runner-journal/`, `runner-ending/`, `publications/`, the two recovery-evidence directories) | Under a remote connection there is no directory to be beside | `publication_journal.root`, `registry.beside`; gap (b) |

The satellite keeps: sessions, `sd-review` (its judgments go to the hub; its
CLI provider lanes run, and its URL-provider lanes are refused by name, gap
C1), `sd-ship` up to the pull request, `sd-note`, `sd-status`, `sd task`,
`sd today`, `sd work register`, and a local Jev meter or none.

## Seams to touch, enumerated

1. `source:local-sd-db/sd_db/database.py::connect` — read `hub.json`;
   return `sd_db.remote.Connection` or refuse.
2. `source:local-sd-db/sd_db/runner_journal.py::lock` — unchanged. Its
   guarding callers refuse under a remote connection instead:
   `source:local-sd-db/sd_db/ship.py::repository_lock` and
   `source:local-sd-db/sd_db/operations.py::control_gate` raise one named
   error, `HubOnly`, whose text names the verb and the hub. `control_gate`
   already refuses a non-file database with
   `service controls require a file-backed database`, in `control_gate` of
   `local-sd-db/sd_db/operations.py`; over the wire `PRAGMA database_list`
   would answer with the hub's path, so the refusal must come from the
   connection's kind, not from the pragma. Verify, do not assume. The same
   rule covers every resolution of a directory beside the database, first
   `source:local-sd-db/sd_db/publication_journal.py::root`; gap (b).
3. `source:local-sd-db/sd_db/database.py::transaction` — generates R, a
   ULID, when it sends `BEGIN IMMEDIATE`; sends R again with `COMMIT`;
   raises `UnknownOutcome` on a lost response and asks `outcome(R)` before
   it returns; acknowledges `recorded` before returning; raises
   `TransactionLost(R)` on `absent`. It keeps no copy of the statements it
   forwarded. A local connection
   ignores R. R belongs to the outermost transaction only; a savepoint
   inside it carries none.
4. The proxy's `BEGIN` and `COMMIT` handlers and the per-id lock —
   register ownership at `BEGIN`; refuse `ClockSkew`; refuse a `COMMIT`
   from a non-owner; insert the `request_outcome` row on the same
   connection, then commit; settle ownership on `COMMIT`, `ROLLBACK` and
   socket close; answer `outcome(R)` with `recorded`, `in_flight`, `absent`
   or `expired`; refuse a `BEGIN` carrying an R already seen; record
   `ack(R)`. A plain `BEGIN` carries no R and opens a read transaction
   instead, which none of the above applies to; gap C2.
5. `request_outcome` — one table,
   `(id TEXT PRIMARY KEY, committed_at TEXT, acked_at TEXT)`, added by a
   migration under `local-sd-db/sd_db/schema/` and a `SCHEMA_VERSION` bump
   in the same commit. It takes slot `015`: sd:1099's migration landed as
   `012_recurrence.sql` and sd:1439's as `014_home_relative_repo_paths.sql`.
   If another migration lands first, take the next free slot. The table joins the
   list in `local-sd-db/sd_db/schema.py`. Rows live for the 7-day retry
   lifetime measured from the time in R, acknowledged or not; the nightly
   prune deletes only rows past it, from the `sd-db-backup` job; gap (e).
6. The session's first frame — package version, `SCHEMA_VERSION`, protocol
   version; `BuildMismatch` on any difference in package version. It
   carries no `$HOME`: repository paths are home-relative; gap (a).
7. `source:local-sd-db/sd_db/registry.py::beside` — resolves the registry
   next to the connection's database. Over the wire that is a hub path; the
   protocol serves the registry's bytes, so the hub's `providers.yaml` stays
   the one file (R7: it holds no keys; keys are env).
8. `source:local-sd-db/sd_db/writes.py::now` — stamps rows on the caller.
   Accept satellite clocks; both machines run NTP. Revision checks are
   content hashes (`StaleItem`, `local-sd-db/sd_db/workflow.py`), not clocks,
   so skew cannot make a stale write pass.
9. `local-jev/jev_meter.py` — unchanged; the satellite sets `JEV_METER_DB`
   to a local file or leaves Jev off.
10. `local-machine-setup/machine-setup.sh` — a `satellite` stage
   (`STAGES`, `source:local-machine-setup/machine-setup.sh::STAGES`) beside
   `stage_sd`; the hub's `stage_sd` gains the serve agent and the `EXTRA`
   check for a stray `hub.json`.
11. `source:local-sd-db/sd_db/ledger.py::reserve` and
   `source:local-sd-db/sd_db/ledger.py::release_orphans` — under a remote
   connection `reserve` raises `LedgerRefused` naming the hub, and
   `release_orphans` sweeps nothing and returns an empty `Released`; gap C1.

## Gaps raised after the merge

Ten gaps were open when #517 merged. Two are Codex findings on the merged
pages, in the item's note of 2026-09-22 (C1, C2). Eight are portability gaps,
in its note of 2026-09-23 ((a) to (h)). Each is decided below with its
reason, or carried to a named step. The table lets a reader check that none
is unaddressed; the subsections give the reasons.

| Id | Gap | Disposition | Lands in |
|---|---|---|---|
| C1 | Ledger ownership is machine-local: the orphan sweep asks the local kernel about every owner pid | **Decided.** Ledger reservations, the sweep, and provider calls charged through `sd_db.calls` stay on the hub | Seam 11; hub table; implement step 6 |
| C2 | A plain `BEGIN` read transaction has no R, so seam 4 refuses its `COMMIT` | **Decided.** A read-transaction path: no R, no outcome row, run under `query_only` | Seam 4; implement step 5 |
| (a) | `$HOME` is assumed equal on both machines; 62 of 62 `repo` rows start `/Users/<login>/` | **Closed 2026-09-24.** Repository paths are home-relative (sd:1439, #554); no `HomeMismatch` refusal is built | `prd.md` Open decisions; seam 6; implement step 10 |
| (b) | Directories beside the database have no satellite answer | **Decided.** Hub-only, refused by connection kind; the registry bytes are the one exception | Seam 2; hub table; implement steps 1, 6 |
| (c) | TCC under launchd is moot by construction, and unstated | **Decided.** Stated below; one launchd run measures the serve agent | Implement step 8 |
| (d) | The SSH agent is not involved, and that is unstated | **Decided.** Stated below; the deploy key covers the `common.cron` pushes | Implement step 10 |
| (e) | The nightly prune of `request_outcome` names no job and no slot | **Decided.** A command in the `sd-db-backup` job | Seam 5; implement step 5 |
| (f) | `local.system-tools.sd-serve` carries an absolute path into this repository | **Decided.** Named as a gotcha with its three readers | Implement steps 8, 12 |
| (g) | A satellite's held `BEGIN IMMEDIATE` blocks the runner and the dashboard | **Decided.** Open-transaction idle timeout of 4 s, below `BUSY_TIMEOUT`, with two measurements | A2 recovery; implement steps 2, 5, 10 |
| (h) | The serve agent's user decides which database is opened | **Decided.** Operator's gui domain; `serve` never creates a database | Implement step 8 |
| x1 | Found while closing (b): pack callers pass `default_path()` or test that it exists | **Closed 2026-09-25.** `default_path()` is hub-aware on a satellite; no pack edit | Implement steps 1, 4 |

### C1 — ledger ownership stays on the hub

`sd_db.calls.call` records its owner as the local process id
(`` `pid = os.getpid() if owner_pid is None else owner_pid`, in `call` of
`local-sd-db/sd_db/calls.py` ``).
`source:local-sd-db/sd_db/ledger.py::release_orphans` runs before every
reservation and asks `source:local-sd-db/sd_db/ledger.py::alive`, which is
`` `os.kill(pid, 0)`, in `alive` of `local-sd-db/sd_db/ledger.py` ``, on the
local kernel. Over the wire, a satellite's sweep reads every hub owner as
dead. It deletes a live `reserved` row and settles a live `sending` row to
`bound` early. The hub does the same to a satellite's rows. SQLite
serializes the statements; it cannot correct a decision made about a pid
from another kernel.

**Decision: keep provider execution and ledger ownership together on the
hub.** The note offers this as the simpler of two answers. Under a remote
connection:

- `reserve` raises `LedgerRefused`, naming the hub. The pack's `sd-review`
  already turns that class into a per-provider `REFUSED` outcome in
  `charged_call`, so R1 holds and no pack verb changes.
- `release_orphans` sweeps nothing and returns an empty `Released`.
  `capped_bills` in `sd-review` calls it outside any handler, so a refusal
  there would end the run. The hub sweeps before each of its own
  reservations, so nothing is left unswept.

The cost: a satellite `sd-review` runs its CLI provider lanes as today and
reports each URL-provider lane as refused. The hub's runner and hub
sessions run those lanes. Judgments still land on the hub.

The alternative is machine-qualified ownership: an owner of
`(machine, pid)`, a sweep that judges only its own machine's rows, and a
lease by which the hub reclaims a silent satellite's rows. It is its own row
if URL review from a satellite is ever wanted.

### C2 — read transactions complete without a request id

Three library sites open a transaction with a plain `BEGIN`, not through
`transaction`. `source:local-sd-db/sd_db/usage.py::_snapshot` and
`source:local-sd-db/sd_db/reads.py::usage_month` end it with `COMMIT`.
`source:local-sd-db/sd_db/registry.py::merge` ends it with `ROLLBACK`. Each
holds one snapshot across several reads, on write connections and on
`write=False` connections alike. As merged, seam 4 refuses their `COMMIT`,
because no connection owns an R. Handing them an R fails as well: the
`COMMIT` handler would insert `request_outcome` on a read-only connection.

**Decision: the proxy tracks what opened each connection's transaction.**

- `BEGIN IMMEDIATE` carrying R opens a **write transaction**, and seam 4
  applies unchanged.
- A plain `BEGIN`, which carries no R, opens a **read transaction**. The hub
  sets `PRAGMA query_only = ON` before it forwards the `BEGIN`, and restores
  the connection's own value when the transaction settles. It accepts
  `COMMIT` or `ROLLBACK` with no R, inserts no outcome row, and takes no
  per-id lock. A write inside it fails as it would on a `write=False`
  connection.
- A lost response to a read `COMMIT` raises `HubUnreachable` and nothing
  else. Nothing was written, so nothing needs recovery, and the verb runs
  again.
- `COMMIT` without R on a write transaction is refused. `COMMIT` with R on a
  read transaction is refused.

Step 5 runs all three helpers over a `write=False` remote connection and
over a write connection. Step 1's trace lists every plain `BEGIN` in the
library and the pack; one that writes inside it is a finding against that
site.

### (a) — `$HOME` differs between machines

R11 covers the `~/repos/<org>/<repo>` layout. The login name is a separate
assumption. Measured 2026-09-23: all 62 `repo` rows are absolute and begin
`/Users/<login>/`. Also measured: the second laptop's agents in
`local-machine-setup/launchagents/local.system-tools.second.*` name `/Users/<second-login>`.
So the first real satellite, step 10's, is exactly a machine on which every
row resolves to nothing.

**Superseded: the refusal.** The 2026-09-23 plan had the first frame carry
the hub's `$HOME` and raise `HomeMismatch` on a difference, until paths were
home-relative. sd:1439 landed first, on 2026-09-24, so the refusal is not
built.

**Decided 2026-09-24, by the operator: home-relative repository paths.**
Three answers were open, and the operator took the second.

1. **The satellite's login is the hub's `<login>`.** A second account on the laptop, or
   another machine as the first satellite. No code changes. This page
   recommended it; not taken.
2. **Home-relative repository paths.** Taken. A migration stores
   `repo.path` as `~/repos/...`, and every reader expands it. The `repo`
   path is the key every `item.repo` foreign key names, so this is its own
   item: the home-relative repo paths item, which this one depends on.
3. **A symlink `/Users/<login>` to the real home on the satellite.** Rejected:
   the pack's `_belongs_to` returns `str(path.resolve())`, which is the real
   home, and a registered-checkout lookup then misses.

Rewriting the home prefix at the remote boundary is rejected outright.
Paths also sit inside JSON `fields`, note bodies and receipts, and a prefix
rewrite over strings would miss some and corrupt others.

**Why `~/` and not the repositories root.** The key could also be relative
to `source:local-sd-db/sd_db/repos.py::repo_root`, as `system` or
`group/repo`. Three reasons choose `~/repos/...` instead:

- R11 already fixes the layout under `~/repos`. The one measured
  difference between the machines is the login name, and a home-relative
  key removes exactly that.
- `repo_root` reads `SD_REPO_ROOT`, an environment variable that launchd
  and cron do not see. A key relative to it would mean one directory in a
  shell and another in a job, the failure that rejected `SD_DB_HUB` below.
- `repos.add` registers any checkout, not only one under the root. A `~/`
  key still names a checkout elsewhere under the home; a root-relative key
  cannot.

`os.path.expanduser` expands the key on either machine. The rest is the
item's own design: the migration, which columns it rewrites, and every
reader.

**The item landed first.** sd:1439 merged as platypeeps/system#554
(`78e43d8`) before step 1. Follow-ups sd:1447 (#560) and sd:1450 (#561)
fixed the raw repository comparisons it exposed. The first frame therefore
carries no `$HOME`, and the satellite stage checks no home. Values that
stay absolute after that item are its own to list.

### (b) — directories beside the database are hub-only

The note named five. Measured, they are these:

- `executions/`, `runner-journal/` and `runner-ending/` belong to the runner
  and the backup, both hub services (R5).
- The note's `evidence/` is two directories, `publication-recovery-evidence`
  and `runner-recovery-evidence`. They belong to `restore` and `repo remove`,
  which are already hub-only.
- `publications/` is the publication journal. Its root comes from `PRAGMA
  database_list` in `source:local-sd-db/sd_db/publication_journal.py::root`.
  Over the wire that answers the hub's path, and a satellite would write a
  journal into a directory the hub never reads.

**Decision: every resolution of a directory beside the database decides by
the connection's kind**, as seam 2 does for `control_gate`. A remote
connection raises `HubOnly`. Publishing a writing piece is therefore a hub
verb. The registry bytes (seam 7) are the one exception, served by the
protocol. Step 1 greps the library and the pack for `PRAGMA database_list`
and for each directory name. A verb that reaches one and is missing from
the hub table is added to it before step 6.

### (c) — TCC is moot by construction

The satellite stage installs no LaunchAgent and no sd cron job.
`work.agent` and `terra.agent` list none, so no per-binary grant is needed
there. `local.system-tools.sd-serve` reads `~/.local/share/sd`, not `~/Documents`, so
it should raise no prompt on the hub. "Should" is unmeasured; step 8
measures it with one launchd run. If a satellite ever runs an sd
LaunchAgent, grants are per binary and per machine, and `/usr/bin/python3`
cannot hold one under launchd (`CLAUDE.md`, "macOS TCC under launchd").

### (d) — the SSH agent is not involved

The transport is TCP over Tailscale, not SSH. No verb this item adds runs
git under launchd on either machine. A satellite `sd-ship` runs in the
operator's shell, with the operator's `SSH_AUTH_SOCK`.

The unattended git a satellite does run comes from `common.cron`, which
every machine carries. `repo-sync-nightly` and `ai-apps-nightly` push
generated commits to this repository with the per-machine deploy key. They
set `GIT_SSH_COMMAND` to that key with the agent off
(`local-autocommit/README.md`), and the key has no passphrase. So those
pushes do not depend on the launchd `SSH_AUTH_SOCK` either. They do need
the key registered, which is why step 10 makes it a prerequisite. The rest
of the 02:45 slot on a laptop is covered by `CLAUDE.md`, "The 02:xx cron
slot needs a scheduled wake", satellite or not.

### (e) — the prune runs in `sd-db-backup`

All cron stays on the hub, so the satellite has none. The prune is one more
command in the hub's `sd-db-backup` job
(`local-cron-jobs/jobs/sd-db-backup.job`), after the backup. That job runs
at 02:10, inside the DarkWake window. That is acceptable here, for two
reasons. The prune is a local SQLite delete and needs no network, so the
DarkWake network failure does not reach it. And a missed night changes no
answer: `expired` is answered from R's age before any row is read, so the
prune only bounds the table's size. A slot at 03:15 or later would buy
nothing. When the backup fails first, the prune is skipped, which is
harmless for the same reason.

### (f) — the serve plist names a path into this repository

`local.system-tools.sd-serve` carries an absolute path to
`~/repos/system/local-sd-db/sd-db.sh`, as `local.system-tools.sd-dashboard` does for
the dashboard. Renaming `local-sd-db` or `sd-db.sh` then means editing and
reloading that plist. The other two readers of repository paths of that
kind are the pack's plugin registry and `sd-plugin.json`. Step 12 adds the
agent to `CLAUDE.md`'s list of plists that hold absolute paths.

### (g) — a silent satellite may not outlast a hub writer's wait

The runner and the dashboard wait `BUSY_TIMEOUT` (5000 ms, in
`local-sd-db/sd_db/database.py`) for the write lock, then fail. With a
30-second open-transaction idle timeout, an abandoned satellite transaction
guaranteed those failures for 25 seconds.

**Decision: the open-transaction idle timeout is 4 seconds**, below
`BUSY_TIMEOUT`. A hub writer that starts waiting when a satellite goes
silent then gets the lock before its own wait expires. An abandoned
satellite costs the runner and the dashboard a wait, not an error. The
60-second outcome bound stays longer than the idle timeout, so the
`in_flight` argument still holds.

The cost: a transaction body that is silent for 4 seconds is closed and
rolled back, and its client gets `absent` and `TransactionLost`. Two
measurements replace the choice:

- Step 2 records the longest gap between two frames inside one write
  transaction across the loopback suite. The pack's transaction bodies
  (`sd-ship`, `sd_ship_no_item.py`, `sd_ship_dispositions.py`,
  `sd_work.py`) hold database calls only, read 2026-09-23.
- Step 10 records the same gap on the satellite, and the longest write
  transaction's wall clock. That wall clock must stay under
  `BUSY_TIMEOUT`, because an active satellite transaction blocks hub
  writers as a silent one does.

A gap above half the timeout, or a wall clock above `BUSY_TIMEOUT`, is a
finding against that transaction body. The fix moves the slow work out of
the transaction, as `reserve` already does.

### (h) — the serve agent runs as the operator and never creates a database

`connect` resolves the path from `$HOME` at call time. A server run as
another user would open another home's path.

**Decision:**

- `local.system-tools.sd-serve` is a LaunchAgent in `~/Library/LaunchAgents`,
  bootstrapped in the operator's `gui/$(id -u)` domain. It is never a
  LaunchDaemon and never another user's agent.
- `serve` opens the database through `connect` without `create=True`.
  `connect` already refuses a missing file, through `open_local` (`` `no database at`, in
  `open_local` of `local-sd-db/sd_db/database.py` ``). So a server under the
  wrong home fails closed and does not create a second, empty database.
  Neither `serve` nor the hub stage runs `init`.
- `serve` prints the resolved database path at start, and
  `sd-db.sh status` prints it.

### x1 — pack callers that name the default path

Found while closing (b); not in either note. Three pack verbs reach the
database by its default path rather than through `connect()` alone:

- `sd-review` opens the ledger with `default_path(HOME)` passed explicitly
  (`open_ledger`).
- `sd-ship` passes `args.database or default_path()`.
- `sd-status` tests `sd_db.default_path().exists()` before its tracker and
  contribution panels. On a satellite that test is false, and each panel
  reports "no shared database yet" instead of reaching the hub.

Seam 1 therefore has to treat an explicit path equal to `default_path()` as
the default. It must also answer the existence test without a local file.
Whether it can do that inside the library is step 1's question. If it
cannot, R1 ("the pack is not edited") needs the operator. That would be a
new `BLOCKING:` line, and it is not added before the trace measures it.

Resolved by the operator's decision after step 1, and built in step 4.
When `hub.json` names a hub, `default_path()` returns
`source:local-sd-db/sd_db/hub.py::HubPath`. It is the same path, and its
`exists()` opens a read session on the hub. `connect` sends a `None` path,
this path, or any path equal to it to the hub. R1 holds without a pack
edit.

## Failure modes

| Condition | Behaviour | Named by |
|---|---|---|
| Hub down | `connect` raises `HubUnreachable(host, port)`; verb exits non-zero; no local file | R4, criterion 3 |
| Tailscale down on the satellite | Same as hub down; the message names the tailnet, not DNS | R4 |
| Session drops with a transaction open, before `COMMIT` was sent | Hub rolls back on close; no hub lock existed to release, because satellite verbs hold none | A2, Decision |
| `COMMIT` sent, response lost | `UnknownOutcome(R)`; `outcome(R)` answers `in_flight` until the owner settles, then `recorded`; the client acknowledges and returns it without re-running | R12, criterion 11 |
| `COMMIT` delayed in transit or queued ahead of the handler while the client reconnects | R is owned from `BEGIN`, so `outcome(R)` answers `in_flight`; once the frame is applied the answer is `recorded`; the count moved by exactly one | R12, criterion 11 |
| Hub killed after the application `COMMIT`, before the response | The `request_outcome` row committed in the same transaction; after restart `outcome(R)` answers `recorded`; the row count moved by exactly one | R12, criterion 11 |
| Hub killed before `COMMIT` completed | Neither the rows nor the record exist; ownership died with the process; `outcome(R)` answers `absent`; the client raises `TransactionLost(R)` and the verb is run again under a new id | R12, seam 4 |
| Late `COMMIT` from a connection that no longer owns R | Refused, not applied; the count is unchanged | seam 4 |
| Original session's socket closed with R owned | SQLite rolls back on close and ownership is released under the per-id lock; only then is `absent` answered | seam 4 |
| `absent` answered for R | `TransactionLost(R)` leaves the `with` block; nothing written; the verb run again computes every value afresh, `lastrowid` included | seam 3 |
| Retry lifetime passed, row pruned | `expired`, answered from R's own age before any lookup; never `absent`; the verb reports it and runs nothing | R12, criterion 11 |
| Retry of an acknowledged R | `expired`; the row is retained for the lifetime, so a young acknowledged id is never `absent` | R12, criterion 11 |
| Satellite clock off by more than five minutes | `ClockSkew` at `BEGIN`, naming both clocks; nothing runs | seam 4 |
| Hub restarts | Sessions gone; the next statement raises `HubUnreachable`; ownership is empty and `request_outcome` is the whole truth | A2, seam 5 |
| A second server started over the same file | Refused at start by `<database>.serve.lock`; one ownership registry per file | A2 |
| Clock skew in row stamps | `writes.now()` values differ by the skew; no ordering decision depends on them across machines | seam 8 |
| Package version differs, schema equal | `BuildMismatch` in the first frame, naming the side to upgrade; no statement runs | R9, criterion 10 |
| Hub library upgraded after satellite setup | Same `BuildMismatch` on the satellite's next session; the satellite stage reinstalls the tag | R9, R10 |
| File older or newer than the hub's library | `SchemaTooNew` / `SchemaTooOld` from the hub-side `connect` | R9 |
| A satellite verb reaches `repository_lock` or `control_gate` | `HubOnly` refusal naming the verb; nothing locked, nothing written | R3, criterion 5 |
| A second login or a tagged node | Refused before any SQL; logged | R8, criterion 6 |
| Idle session | Closed after the timeout; the satellite reconnects on its next verb | A2 |
| Both files present on one machine | `connect` refuses; the stage prints `EXTRA` | R4, R10 |
| Satellite `$HOME` differs from the hub's | Nothing to refuse: repository paths are home-relative and expand under the satellite's home | R11, gap (a) |
| Satellite reaches `reserve` | `LedgerRefused` naming the hub; `sd-review` reports that URL provider as refused | gap C1 |
| Satellite reaches `release_orphans` | Sweeps nothing; returns an empty `Released`; the hub sweeps its own | gap C1 |
| Plain `BEGIN`, then `COMMIT` or `ROLLBACK` | Read transaction under `query_only`; no R, no outcome row; a write inside it fails | gap C2 |
| Response to a read `COMMIT` lost | `HubUnreachable`; nothing to recover; the verb runs again | gap C2 |
| Satellite resolves a directory beside the database | `HubOnly` naming the verb; nothing written locally | gap (b) |
| Write transaction silent for 4 s | Closed and rolled back; its client gets `absent`, then `TransactionLost` | gap (g) |
| `serve` started under a home with no database | `connect` refuses; the server exits non-zero naming the path; no file is created | gap (h) |

## Rejected alternatives, with the reason

- **Extend the dashboard's `/api/*` for the CLI.** It is a browser API with
  a session cookie and `X-SD-CSRF`, in
  `local-project-dashboard/sd_dashboard/server.py`, and its routes name a
  dozen workflow functions, not the 20 call sites the pack uses. Reusing
  its peer check is right; reusing its routes is a second library surface
  that would drift.
- **A local read replica with hub writes.** Reads would be stale by the
  replication interval and `sd today` would lie by design.
- **Environment variable selector (`SD_DB_HUB`).** Not seen by launchd or
  cron, and a shell without it silently opens a local file. A config file
  fails the same way from every caller.
- **Hub-held locks over the session (the first draft).** A lock released
  when the session drops outlives nothing it guards: the satellite's
  subprocesses keep running after the partition, and the next holder runs
  beside them. Fencing (a lease plus a token every guarded write checks)
  would make it correct; keeping the guarded operations on the hub makes the
  case not exist.
- **Statement replay on `absent` (the third draft).** The remote connection
  buffered the transaction's statements and re-sent them under the same R
  when the hub had no record, admitting the replay if every read returned
  the same rows. Wrong, because a write produces values that later
  statements bind: `create_item` takes `cursor.lastrowid` and writes it
  into the item's opening note, and no `SELECT` is in that transaction for
  a read comparison to catch. The reviewer's reproduction: the first run
  gets item 1 and rolls back, another writer takes id 1, the replay creates
  item 2, attaches the note to item 1 and returns 1. A replay would have to
  compare every write-generated value, `lastrowid` first, and roll back on
  any mismatch; running the body again gets the same result for free.
- **Operation-level requests with durable identifiers instead of statement
  forwarding.** A cleaner recovery boundary, and the row's fallback if the
  proxy fails its latency budget. Rejected as the primary shape because the
  seam is `sd_db.connect` and 46 modules speak SQL through it; the durable
  identifier is taken at the transaction instead, where `transaction` is the
  one function that sends `BEGIN IMMEDIATE` and `COMMIT`. On ownership and
  on atomicity the two are equal: R owned from `BEGIN` under a per-id lock
  is the boundary an operation record would give, and the `request_outcome`
  row commits with the forwarded writes as an operation record would with
  its own. What is left of "simpler" is one table plus a COMMIT handler
  against a second seam over 46 modules.
