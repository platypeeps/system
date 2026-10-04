---
title: run the framework from a second machine
created: 2026-09-22
item: sd:1335
---
# PRD — run the framework from a second machine

## Problem

Every sd command opens one local file. Before step 4,
`source:local-sd-db/sd_db/database.py::connect` resolved `default_path(home)`,
and `default_path` returned `$HOME` joined with `DEFAULT_RELATIVE`, which is
`.local/share/sd/sd.db`; step 4 renamed that local resolution
`source:local-sd-db/sd_db/database.py::local_path`. No environment variable and no config key selected
another path: `path=` is a Python argument, and the only callers that pass one
are backups, migrations and tests. A second machine therefore has no rows. It
cannot read the backlog, it cannot file a note, and `sd-ship` on it cannot
record a receipt anywhere the hub reads.

The operator's requirement (sd:1335, 2026-09-22): "the ability to run the
framework on a second machine with the sd database (and any other capabilities
that should remain centralized here) being maintained here." The umbrella plan
(sd:1334, decision note of 2026-09-22) names this track T2: "second machine as
satellite, hub services stay here".

Two shapes are ruled out by the measured facts, and the design records why:

- **Copying the file.** A synced or network-mounted `sd.db` has two writers.
  WAL mode, set by `PRAGMA journal_mode = WAL`, in `_configure` of
  `local-sd-db/sd_db/database.py`, depends on shared memory and POSIX locks
  that neither Syncthing nor SMB honours. The result is a silent fork or a
  corrupt file, and the backup job would faithfully snapshot whichever one
  won.
- **A local database per machine.** Rows would diverge the first day, and the
  runner, the dashboard, the digests and the backups read one file here.

## What exists today (measured)

- **One seam for every open.** `local-sd-db/sd_db/__init__.py` states that
  nothing outside the package calls `sqlite3.connect`, and that a grep of the
  pack, this repository and the dashboard enforces it. In the pack, every
  verb reaches rows through `sd_handoff_rows.connect`, `sd_lib.Rows`, or a
  direct `sd_db.connect`: 20 call sites across `sd-note`, `sd-status`,
  `sd-ship`, `sd-review`, `sd_work.py`, `sd_notes.py`, `sd_operations.py`,
  `sd_runner.py`, `sd_shadow.py`, `sd_suggest.py`, `sd_skill.py`,
  `sd_controls.py`, `sd_codex.py`, `sd_restore.py`, `sd_install.py`,
  `sd_check_receipts.py`, `sd_registry.py` and `sd_writing.py`. None of them
  passes a path. The library is installed as a copy at a tag into the pack's
  virtualenv (`import_sd_db` in the pack's `sd_lib.py` prepends that
  `site-packages`).
- **Three file locks live beside the database, not in it.**
  `source:local-sd-db/sd_db/runner_journal.py::lock` is the one opener both
  packages share; it takes `fcntl.flock` on a file. Its callers:
  `source:local-sd-db/sd_db/operations.py::control_gate`, which derives its
  path from `PRAGMA database_list` and refuses with
  `service controls require a file-backed database`, in `control_gate` of
  `local-sd-db/sd_db/operations.py`; `source:local-sd-db/sd_db/ship.py::repository_lock`,
  which locks `ship-locks`, in `repository_lock` of `local-sd-db/sd_db/ship.py`,
  one file per repository, and which the pack's `sd-ship` and
  `sd_ship_no_item.py` enter around every delivery; and the removal and
  backup verbs. A lock taken on a second machine's disk serializes nothing
  on the hub.
- **Other state beside the database is files.** `executions/` logs
  (`runner_exec`), `runner-journal/`, `runner-ending/`, `evidence/`,
  `publications/`, `commands.yaml`, and `providers.yaml`.
  `source:local-sd-db/sd_db/registry.py::beside` resolves the registry from
  the connection's own directory, not from `$HOME`.
- **The dashboard already has a peer-authenticated Tailscale surface.**
  `local-project-dashboard/RUNTIME.md` documents HTTPS on 8443 through
  Tailscale Serve and a direct listener on 8768 bound to the node's Tailscale
  IP. Each request on 8768 resolves its TCP peer with `tailscale whois`, and
  `lookup(peer) != frontdoor.operator_login`, in `direct_context` of
  `local-project-dashboard/sd_dashboard/auth.py`, refuses any other login,
  tagged devices and forwarded identity headers. Its write routes are a
  browser API: `source:local-project-dashboard/sd_dashboard/server.py::action_route`
  maps `/api/items`, `/api/items/N/status`, `/api/items/N/notes`,
  `/api/notes/N/resolve`, `/api/run`, `/api/runner/N/...` and the service
  controls onto `workflow.*` calls, and every write needs a session cookie
  and the `X-SD-CSRF` header. Nothing there speaks to a CLI.
- **Hub services are already named per machine.**
  `local-machine-setup/profiles/personal.agent` lists `local.system-tools.sd-dashboard`,
  `local.system-tools.sd-runner` and `local.system-tools.task-actions`; `work.agent` and
  `terra.agent` list none of them. `personal.cron` carries `sd-db-backup`,
  `sd-plan-nightly`, `mirror-sync-nightly`, the digests and the intake jobs;
  `work.cron` is comment-only for the personal jobs. The stage
  `source:local-machine-setup/machine-setup.sh::stage_sd` prints
  `MISSING database`, in `stage_sd` of `local-machine-setup/machine-setup.sh`,
  and `status_stage` counts drift by the words
  `DIFFERS|MISSING|STALE|ABSENT|UNLOADED|EXTRA`, in `status_stage` of
  `local-machine-setup/machine-setup.sh`.
- **Backups are hub-only by construction.** `local-cron-jobs/jobs/sd-db-backup.job`
  runs `sd-db.sh backup` nightly against the local file and preflights the NAS
  mount with `offsite-verify.py`.
- **The tailnet has one other macOS node online today** (`tailscale status`,
  2026-09-22): the second laptop. iOS nodes and funnel ingress nodes are the
  rest.
- **The Jev meter opens with `busy_timeout=0`**, in `local-jev/jev_meter.py`,
  and honours `JEV_METER_DB`, in `local-jev/jev_meter.py`. A round trip to a
  hub is time added to a judgment already printed.

## Requirements

- **R1 — every row verb works from the satellite.** Every pack verb that
  reads or writes rows (`sd-note`, `sd-status`, `sd task`, `sd today`,
  `sd-review`, `sd-ship`, `sd work register`, `sd config`, `sd store`,
  `sd suggest`, `sd skill`) works from the second machine against the hub's
  database, unchanged. The pack is not edited for this item.
- **R2 — the hub is the only writer of its file.** Every write lands through
  a `sqlite3.Connection` opened on the hub. The satellite holds no `sd.db`
  and no WAL sidecar.
- **R3 — writes are serialized by the hub.** `BEGIN IMMEDIATE`, in
  `transaction` of `local-sd-db/sd_db/database.py`, runs on the hub, so a
  satellite transaction waits on the same SQLite lock the runner and the
  dashboard wait on. The three file locks never cross the wire: an operation
  under `repository_lock` or `control_gate` runs on the hub, and under a
  remote connection it refuses by name. A dropped session must not be read
  as "the satellite's operation stopped", because its git and GitHub
  subprocesses may still be running.
- **R4 — offline is explicit.** With the hub or Tailscale unreachable, a
  verb fails with one named error naming the hub, and exits non-zero. It
  never creates, opens or writes a local file. The satellite's
  `~/.local/share/sd/sd.db` must not exist.
- **R5 — hub services stay on the hub, and a satellite runs none.** A
  satellite is a machine with `~/.config/sd/hub.json`. It runs no hub
  service: no dashboard (8443 or 8768), no runner, no backups, no
  `task-actions`, no cron intake and no digests. It also holds no local sd
  database: `~/.local/share/sd/sd.db` must not exist (R4). The hub keeps
  running these services, and they keep opening the local file. Nothing
  about them changes in this item. Operator ruling, 2026-10-04.
- **R6 — receipts land in the hub database.** `sd-ship`'s delivery receipt
  (`ship.save`), `sd-review`'s judgments and meter samples written from the
  satellite are rows on the hub the next morning's digest reads. Ledger
  reservations are not written from a satellite at all: the ledger's owner
  is a local process id, so reservations and the provider calls charged
  through them stay on the hub (design, gap C1).
- **R7 — secrets never leave the machine that holds them.** Provider keys,
  the GitHub token and `TYPESAFE_API_KEY` stay in each machine's env and
  `.env`. The hub endpoint carries no bearer secret; the peer's Tailscale
  identity is the credential, exactly as the dashboard's 8768 listener does
  it.
- **R8 — Tailscale is the transport boundary.** The hub listens on its
  Tailscale IP only, refuses tagged devices, and refuses any login other
  than the operator's. No LAN listener, no Funnel.
- **R9 — same `sd_db` build on both machines, checked on every session.**
  The first frame of a session carries the client's package version, its
  `SCHEMA_VERSION` and the protocol version. The hub refuses any package
  version other than its own with one named error whose text says which side
  to upgrade. A schema number alone is not the check: two builds can share a
  schema and differ in the SQL they send, and the hub-side `connect` compares
  the file against the hub's library, never against the client's.
  `SchemaTooNew` and `SchemaTooOld` still guard the file against the hub's
  own library.
- **R10 — setup is one `machine-setup.sh` stage.** A `satellite` role names
  the hub, installs the config key, verifies the library build, and prints
  the same drift words `status` already counts. A hub profile prints
  `EXTRA` for a hub config on a machine that serves.
- **R11 — paths stay valid.** Both machines keep the
  `~/repos/<org>/<repo>` layout. `repo.path` is stored home-relative, as
  `~/repos/...`, since sd:1439 (migration 014, platypeeps/system#554), so a
  satellite with another login resolves every row under its own home
  (design, gap (a)).
- **R12 — a lost response is an unknown outcome, never a silent failure.**
  When the transport fails after a statement was sent and before its
  response arrived, and that statement could have committed, the verb
  raises `UnknownOutcome` naming the request id R. R is a ULID, unique
  across satellites by its randomness. The hub
  owns R from `BEGIN IMMEDIATE`, not from `COMMIT`, and answers
  `outcome(R)` under one lock per id with `recorded` (return
  it, run nothing), `in_flight` (an owner is live; wait with a bound, then
  refuse by name) or `absent` (no owner, no row: it did
  not commit; `TransactionLost(R)`, and the verb is run again under a new
  id). The outcome
  record commits **inside the same SQLite transaction** as the application
  writes; a `COMMIT` from a connection that does not own R is refused; rows
  are never deleted, so `absent` never stands for a pruned row. No verb
  duplicates a row by retrying, whatever the hub crash or the frame delay.
  The retry lifetime, `expired`, `ack(R)` and the prune are deferred to a
  later row (operator ruling 2026-10-04, design Q1).

## Acceptance criteria

Each one names its check. A partial pass is not a pass.

1. **Suite over the wire.** The `local-sd-db` test suite, pointed at a
   loopback `sd-db.sh serve`, passes with the same count it passes locally
   and zero skips. Check: `sd-db.sh test --remote 127.0.0.1:<port>`
   summary line equals the local summary line.
2. **A satellite note is a hub row.** From the second machine,
   `sd-note add sd:1335 "from satellite"` exits 0; on the hub,
   `sqlite3 ~/.local/share/sd/sd.db "select body from note where item=1335 order by id desc limit 1"`
   prints it.
3. **Offline refuses by name.** With `tailscale down` on the satellite,
   `sd today` exits non-zero and prints the hub's name in one line; after
   it, `test ! -e ~/.local/share/sd/sd.db` on the satellite passes.
4. **One writer.** `lsof ~/.local/share/sd/sd.db` on the hub during a
   satellite write shows only hub processes; the satellite's `lsof` shows
   no `sd.db`.
5. **Ship from the satellite stops at the pull request.** `sd-ship` on the
   satellite opens the pull request, leaves a `state` row with a `ship:` key
   on the hub, and never enters `repository_lock`. Check: the serve log for
   that session records no lock message; the hub's lane merges (the runner
   for `runner_merge=auto`, a person for `manual`) and the item reaches its
   delivered state. A satellite verb that reaches `repository_lock` or
   `control_gate` prints the hub-only refusal and exits non-zero.
6. **Peer refusal.** A request from a tagged node or a second login is
   answered with a refusal and no SQL runs. Check: the serve log records the
   refusal and `PRAGMA data_version` on the hub is unchanged.
7. **Drift markers.** On the satellite, deleting the hub config makes
   `machine-setup.sh status` count one `MISSING`; on the hub, adding a
   satellite config makes it count one `EXTRA`. `--fail-on-drift` exits 1
   in both cases.
8. **Latency budget.** `time sd-status` on the satellite stays under three
   times the hub's wall clock for the same checkout, measured once on the
   LAN and once over the internet. The numbers go in the implement log.
9. **Hub unchanged until proven.** The loopback steps of the implement page
   land with the hub's LaunchAgents, cron jobs and `runner.json`
   byte-identical: `machine-setup.sh status` on the hub counts zero drift
   after each merge.
10. **Build mismatch with identical schema.** Two `sd_db` builds that share
    `SCHEMA_VERSION` and differ in package version: the client is refused in
    the first frame, no statement runs, and the refusal names the side to
    upgrade. Check: the test runs both directions (client newer, hub newer)
    against a loopback server, and after a simulated hub upgrade the
    satellite's next verb is refused rather than admitted.
11. **A lost COMMIT is an unknown outcome, through crashes and
    delays.** Runs against a loopback server, each counting rows before
    and after; the count moves by exactly one where a commit is promised
    and by zero everywhere else. (a) Response dropped after `COMMIT`:
    `UnknownOutcome`, then `recorded`, one. (b) `kill -9` between the
    application `COMMIT` and the response, restart, retry: `recorded`, one,
    and the `request_outcome` row exists. (c) `kill -9` before `COMMIT`
    completes, restart, retry: `absent`, `TransactionLost`, zero; the verb
    run again: one. (d) The
    `COMMIT` frame held in transit, the client reconnects and asks:
    `in_flight`; frame released: `recorded`, one. (e) A `COMMIT` for R from
    a connection that does not own R: refused, zero. (f) to (i), the
    prune, expiry, acknowledgement and clock-skew cases, were dropped with
    the prune on 2026-10-04 (design Q1). (j) The hub answers `absent` for R: `TransactionLost(R)` leaves the
    `with` block, zero; the verb run again under a new id: one, and the
    note's owner is the item that run created.
12. **A satellite runs no hub service.** On the satellite, a hub-only
    LaunchAgent or backup job that is installed or loaded makes
    `machine-setup.sh status` count one `EXTRA` each, and the stage
    removes nothing. With `hub.json` present, `update agents --apply`
    prints `SKIP` for each hub-only label and installs none. Check:
    `local-machine-setup/tests/test_hub_and_satellite.py`; on the second
    laptop, `status` counts zero drift, `launchctl list` names no hub-only
    label, nothing listens on 8443 or 8768, and
    `test ! -e ~/.local/share/sd/sd.db` passes.

## Deliberately out of scope

- **Running hub services on the satellite.** No second runner, no second
  dashboard, no second backup. A second merge lane is the defect the runner
  exists to prevent.
- **Satellite-side merge.** Decided against on the design page: a
  satellite's `sd-ship` stops at the pull request, and the hub's lane
  merges. Flipping it needs fencing (a lease with a fencing token checked
  by every write under the lock) and its own row.
- **Multi-operator access.** One login, the operator's, on both machines.
- **Replacing SQLite.** The file stays; the wire is a new way to reach it.
- **The dashboard's browser API.** It stays a browser API. The satellite
  does not drive it.
- **Editing pack verbs.** R1 forbids it; the seam is the library.
- **Jev metering from the satellite.** The meter stays local (`JEV_METER_DB`
  to a satellite-local file) or off. A judgment is not delayed by a round
  trip.

## Open decisions

None is open. The design page's gap table maps every gap raised after #517
to its disposition.

Decided 2026-09-24, by the operator: gap (a), the satellite's home. The second
laptop, the first satellite step 10 names, runs as `/Users/<second-login>` (its
agents in `local-machine-setup/launchagents/local.system-tools.second.*`), and all 62
`repo` rows begin `/Users/<login>/`. Repository paths become home-relative:
`repo.path` is stored as `~/repos/...` and expanded by every reader. That
change is its own work item, sd:1439, and it landed on 2026-09-24 as
platypeeps/system#554 (`78e43d8`) before any step of this item was built.
So no step waits for it, and the `HomeMismatch` refusal planned as a
stopgap is not built. The design recommended
a satellite that logs in as the hub's `<login>`; the operator chose the path change
instead. The design page, gap (a), says why the key is home-relative and
not relative to the repositories root.

## Not verified

- Traced in step 1 (implement log, 2026-09-24): the suite passes under a
  guard that refuses any attribute outside twelve on the connection and six
  on the cursor, and over loopback. Still open: tests that start a child
  process open their database locally, so no run covers the child's path.
- The per-statement round-trip cost over WireGuard between the two
  machines. Not measured; criterion 8 measures it.
- Whether the pack's `sd_check_receipts.py` and `sd_codex.py`, which pass an
  explicit path to `connect`, ever run on a satellite. Both pass
  `default_path()`, which step 4 made hub-aware, so the path they pass
  reaches the hub. Not run on a satellite.
- Gap x1 is closed in the library (implement log, 2026-09-25). On a
  satellite, `default_path()` is a path whose `exists()` asks the hub, and
  `connect` treats it, and any path equal to it, as the default. The pack
  callers are not edited. They are exercised by their shapes in
  `tests/test_hub.py`, not by running the pack on a satellite.
