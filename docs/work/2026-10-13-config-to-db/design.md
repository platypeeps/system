---
title: Five config files and three sd settings move into the workflow database, each with a dashboard control
created: 2026-10-13
item: sd:3983
---

# Design: config-to-db

Write this page only when the shape needs agreement before the code.
The item's row holds status, progress and decisions; this page holds the shape.
It holds no code. It read `origin/main` 6f544cd8 of this repository, the command pack at a4ee200fb, the channels C1
branch (migration 029), the live config folder (key names and line counts only) and the live database read-only
(schema 28).

It covers sd:3983, sd:3984, sd:3986, sd:3987 and sd:3988 (system) and sd:3989 (pack).
The 2026-10-13 config sweep filed them. sd:3985 and sd:3990, from the same sweep, have merged.

## Terms

- **Row wins**: the transition rule every slice uses (see Approach).
- **Profile**: the machine kind `repo-sync` reads from `~/.config/machine-setup/profile`: `personal` (the hub),
  `work` (the work satellite) or `terra` (the third machine).
- **Reference clone**: a checkout `repo-sync` clones for reading only, such as an upstream tool in `~/repos/ai/`.
  It has no `repo` row today.
- **Setting row**: a `state` row of the new kind `setting`, keyed by a setting name. The latest row for a key is the
  answer, as for the `view` kind (migration 028).

## Problem

Five files in the config folder and three keys in the pack's `config.json` hold values the operator changes by
hand in an editor, on one machine, with no dashboard view. Some duplicate rows the database already holds.

| Item | File today | Live shape (counted 2026-10-13) | Readers |
| --- | --- | --- | --- |
| sd:3983 | `<config>/repo-sync/repos.{common,personal,work,terra}.conf` | 17, 53, 27 and 1 lines; 75 distinct repos. 55 match the 56 `repo` rows; 20 have no row (15 reference clones on the hub, 5 work-only checkouts). 22 repos sit in both the personal and work files and not in common | `local-repo-sync/repo-sync.sh:65-117` (`repo_list`), reconcile `:188-301` (writes the files nightly); pack `sd_db/repos.py` `conf_paths`, `checkouts` (`sd-db repo seed`) |
| sd:3984 | `<config>/dependabot/allowed-repos.conf` | 1 slug; it is a registered, managed repo | `local-dependabot/holds.py:51-68`, `:490`; the `dependabot-daily` agent through `ROUTINE.md:102-106` |
| sd:3986 | `<config>/project-dashboard/documents.conf` | 15 `label` lines, 1 `root` line, no `skip` or `img` line. 8 of the 15 labels name a key no checkout claims | `sd_dashboard/documents.py:84`, `_configured` `:273`, `roots` `:311`, `unresolved` `:333`; `documents_screen.py:290` |
| sd:3987 | `<config>/project-dashboard/home-tiles.conf` | 1 `headline`, 17 `tile` lines | `sd_dashboard/home_screen.py:55`, `parse` `:77`, `document` `:208` |
| sd:3988 | `<config>/mail-intake/mail-intake.conf`, `<config>/drive-intake/drive-intake.conf` | mail: `account`, `me`, 2 `alias`, `window`. drive: 2 `root`, 35 `route`; 33 routes equal the tracked example. Both last edited 2026-09-19 | `local-mail-intake/mail_intake.py:1057`; `local-drive-intake/drive_intake.py:1097`, `drive-intake.sh:78` |
| sd:3989 | `~/.config/sd-ai-command-pack/config.json` `config.sd` | `fleet_owners`, `copilot_review`, `external_reviews` set on the hub, beside per-machine keys | pack `bin/sd_lib.py:603` `core_setting`, called from `sd-review:1900-1905`, `sd-ship:829`, `sd_fleet.py:396`, `sd_ship_bindings.py:158`, `sd_install.py:1772`; `bin/sd` `config_get` `:1293`, `config_set` `:1314` |

The hub, the work satellite and the third machine each keep their own copy of the config folder.
The repo-sync files drift per machine: reconcile rewrites each machine's copy every night.

## Approach

### Two rules for every slice

**Row wins** (sd:3987, sd:3989). Once the database holds a row for a key, the old file is not read for that key.
An unset written through the new control is a row too (value null), so a stale file value cannot come back.
The old file answers only for a key the database has never held. This lets each slice merge with no flag day,
and lets the operator's first save on the dashboard be the import.
After every machine has read the row for one nightly cycle, the lead deletes the file, with a backup under
`<bulk root>/system/config-backup-<item>/`, as sd:3990 did.
sd:3983 has its own fallback order instead (below). sd:3984 and sd:3986 move in one step right after their merge:
one row and 7 cosmetic labels do not need a transition rule.

**A failed read is unknown.** A database read that fails (no hub, hub refuses, schema too new, verb missing)
is never read as "no row" and never falls back to the file silently.
Each reader names the failure and takes the action listed under "Satellite with no hub" for its item.

### Storage: two migrations, shared

- **Migration 030, `state` kind `setting`** (pack). Rebuild `state` as 027 and 028 did, adding `setting` to the
  CHECK list. The 27,600 live rows copy with their ids. `sd_db.writes.set_setting(connection, key, value, before=)`
  writes one row through `record_state`; `settings(connection, keys)` reads the latest row per key.
  sd:3987 and sd:3989 use it.
- **Migration 031, two `repo` columns** (pack). `ADD COLUMN` in place, as 015, 024 and 029 did:
  - `clone_profiles TEXT`, CHECK `IS NULL OR IN ('personal','work','terra','personal,work','personal,terra',
    'work,terra','personal,work,terra')`. NULL means `repo-sync` clones it nowhere. One canonical spelling per set
    keeps the CHECK a closed list.
  - `dependabot INTEGER NOT NULL DEFAULT 0 CHECK (dependabot IN (0, 1))`.
  sd:3983 and sd:3984 use them.

Neither column is added to `sd-db repo list` text: its last field must stay `runner_merge`, which
`machine-setup.sh:1117` and the operator's instructions read. C1 keeps the same rule for `channels`.

The numbers assume C1's 029 lands first. Numbers follow merge order: if the 031 slice is ready first, it takes 030.

sd:3986 needs no migration: it extends the `view` kind that 028 added.

Rejected: a new `setting` table (`key PRIMARY KEY, value`). It is simpler SQL, but it is a second key-value store
beside `state`, and it keeps no history, so Undo needs its own column.

### sd:3983, repo-sync clone lists

- **Target**: `repo.clone_profiles`, plus rows (`managed=0`, `runner_merge=manual`, `ci=github`) for the 20 checkouts
  with no row. The row's `path` gives the subdir (`~/repos/<subdir>/<name>`, or `.` for `~/repos/<name>`) and its
  `remote` gives `owner/repo`. Those two make the conf line, so no new column holds the group.
- **Migration**: `sd-db repo seed --clones PROFILE` (pack, an option on the existing verb). It reads
  `repos.common.conf` and `repos.<PROFILE>.conf` on the machine it runs on (only `repos.terra.conf` for terra).
  It registers each present checkout that has no row (the existing seed rule), and adds PROFILE to each line's row.
  It only adds. Each line is its own transaction, so a rerun finishes an interrupted run.
  A line whose checkout is absent and which has no row is named, and the run exits 1 after the other lines.
  The lead runs it with `personal` on the hub, `work` on the work satellite and `terra` on the third machine
  (through each machine's remote session), before S1 merges.
  The five work-only checkouts get their rows from the satellite run, the machine whose disk holds them.
  The profile is an argument: the pack's `repos.profile()` reads `REPO_SYNC_PROFILE`, which the satellite may not set.
- **Reader change** (`repo-sync.sh`): one read per run, after reconcile and before sync, into `$TMPD`, which every
  `repo_list` caller then reads:
  1. `sd-db repo list --clones PROFILE` (pack, an option on `list`). It prints `<subdir> <owner/repo>` lines, the conf
     grammar, for rows whose `clone_profiles` hold PROFILE. Each such row must sit under `~/repos` at depth 1 or 2
     and carry a GitHub remote; one that does not makes the verb exit 1 naming it, so a partial list is never an
     answer. An empty answer is unknown too: every profile holds at least `system`, so zero lines means the profile
     was never imported. On a non-empty exit 0 the run writes the snapshot
     `$(st_state_dir repo-sync)/clones.<profile>` (temp file, then `mv`).
  2. Else the snapshot, with a stderr line and an `SD_REPORT_ATTENTION` reason in `nightly`.
  3. Else the old `repos.*.conf` files, read by today's code: the fresh-machine bootstrap, which the item keeps.
  4. Else the existing "missing config" refusal, now naming both remedies.
  `refresh` reads the list once, before it moves any checkout, so a pack move in the same run cannot change the answer.
- **Reconcile** stops editing the list from disk, because the dashboard now says what each profile clones:
  an absent checkout that is listed is cloned by the next `sync`, and a present one that is not listed is reported.
  It writes only one thing, and only when it can reach the database: a GitHub checkout on disk with no row gets
  `sd-db repo add PATH`, then `sd-db repo clones PATH +PROFILE`. That keeps "clone it by hand, and it joins".
  Everything else is a report line: "present, not listed for PROFILE: check it on Management or delete the
  checkout". No run removes a profile, so no outage or empty disk can look like a deletion.
  To stop cloning a repo on a profile, the operator clears its box on Management; deleting the checkout alone no
  longer stops the next sync re-cloning it. The README says so.
  - `sd-db repo clones PATH WORDS` (pack) takes `personal,work`, `none`, `+P` or `-P`. It reads and writes in one
    transaction.
- **Dashboard control**: Management, per repo row, "Clones on" with three checkboxes (personal, work, terra).
  It goes through `management_screen.set_repo` (a new `SETTERS` entry, stale `before` refused, `HubOnly` on a
  satellite). Rows with `managed=0` sit in a collapsed "Reference clones" group, so the managed rows stay first.
- **Satellite with no hub**: steps 2 and 3. A satellite that never reached the hub syncs from the conf it was given.
  One that has reached it syncs from its last answer. Neither writes rows.

### sd:3984, Dependabot allowed repos

- **Target**: `repo.dependabot`. The owner gate stays as `ROUTINE.md` states it: every `platypeeps` repo, minus
  `DENIED`. The column replaces only the "other owners" list.
- **Migration**: one command after the 031 slice lands: `sd-db repo dependabot PATH yes` for the one live slug.
  It needs no import code.
- **Reader change**: `holds.py` `_allowed_repos` reads the slugs of rows with `dependabot=1` from `sd_db`. It puts
  `$SD_PACK_ROOT/lib` (default `~/repos/platypeeps/sd-ai-command-pack/lib`) on `sys.path` itself, as the dashboard
  does; today `holds.py:30` adds only this repository's `lib/`. A missing pack is a failed read.
  `dependabot.sh allowed` prints the same list for the
  `dependabot-daily` agent; `ROUTINE.md` step 3 runs it in place of reading the file.
  That subcommand replaces both the file and `DEPENDABOT_ALLOWED_REPOS_FILE`, which go.
- **Dashboard control**: Management, per row, "Dependabot" yes/no, through `set_repo`. A `platypeeps` row shows
  "always (owner)" and a `DENIED` row shows "denied", both with no toggle.
- **Satellite with no hub**: the gate fails closed. No repo outside `platypeeps` is swept, and the run log and the
  agent's report say "allowed list unknown: <reason>", not "none allowed".

### sd:3986, Documents labels and hidden roots

- **Target**: `view` rows keyed by the root key (no `/`), beside the per-file rows keyed `<root key>/<file>`.
  The pack's `set_view` accepts `label` (1 to 60 characters, or null) on a root key only, and `views` returns it.
  `hidden` on a root key hides the root from the listing, as Hide does for a file. Serving does not change.
- **What stays in the file**: `root|` (a root outside `~/repos`; one live line), `img|` and `skip|`. `skip|` still
  withholds serving and settles a contested key. None of those has a database reader, so the server's
  `resolve` path still needs no database.
- **Migration**: one step, not row wins. Labels are cosmetic and only 7 are live, so the reader stops reading
  `label|` lines in the same slice, and right after the merge the lead writes the 7 labels in one loop over
  `sd_db.writes.set_view` and deletes the `label|` lines. Between the merge and the loop, those roots show derived
  names. The 8 dead labels are deleted, not moved. A `label|` line still in the file is named on the page as
  "not read: rename the root here". A per-key row-wins rule would need to know whether a row's missing label means
  "never set" or "cleared", because Hide before Rename writes a root row with no label.
- **Reader change**: `documents_screen.document` already reads `views`. It passes the root rows to `documents.roots`,
  which takes a root's label and hidden flag from its row, else the derived name. `views` keeps a root row whose
  only field is a label (today it drops a row equal to `VIEW_CLEAR`, which has no label).
- **Dashboard control**: each root heading on Documents gets Rename and Hide. Hidden roots sit in a "Hidden" fold
  with Unhide. The page writes through the existing `POST /api/documents/view`, with `{"root"}` and no `file`.
- **Satellite with no hub**: none needed. `sd-dashboard` is hub-only (`SD_HUB_ONLY_AGENTS`).
  A failed `views` read already shows `store.available` false (`documents_screen.py:238-240`). Labels then show the
  derived name, no root is hidden, and the page names the reason. No file label comes back.

### sd:3987, Home tiles

- **Target**: setting row `dashboard.home_tiles`, value the same pipe text the file holds, at most 16 KiB.
  `home_screen.parse` stays the one grammar.
- **Migration**: row wins. With no row, the page reads the file as today. The editor opens on the file's text, so
  the first Save is the import.
- **Reader change**: `home_screen.document` takes the connection and reads the row first.
  On a failed read it shows "tile list not read: <reason>", not "No tile list", and calls Home Assistant for nothing.
- **Dashboard control**: Home gets "Edit tiles", a text editor opened on the current text (the row, else the file,
  else `home-tiles.conf.example`). `POST /api/home/tiles {text, before}` parses first. It refuses a text with
  problems (400, each problem with its line number) and a stale `before` (409, the digest of the text the page
  showed). The rows keep history, so Undo posts the previous text.
- **Satellite with no hub**: none needed; the dashboard is hub-only.

### sd:3988, mail and drive intake rules

Recommended: do not build. Close the item and record why (Open question 2).
The files were last edited 2026-09-19. 33 of the 35 drive routes equal the tracked example. The mail file holds
account values and a window, not routing rules. A dashboard editor for 35 ordered glob rules costs an M slice for
an edit made about once a month.
If the operator wants it anyway, the cheap shape reuses sd:3987: two setting rows (`intake.mail`, `intake.drive`)
holding the same pipe text, the same editor and the same row-wins rule. Then it is an S slice after S3.

### sd:3989, fleet-wide sd settings

- **Target**: setting rows `sd.fleet_owners`, `sd.copilot_review` and `sd.external_reviews`, each value checked against
  its `CORE_CONFIG` pattern. `gate_slots`, `review_slots`, `gate_load_max`, `bulk_storage_root`, `assistant_merge`,
  `lane_root` and the plugin keys stay in `config.json`.
- **Precheck before the build**: a key moves only when every machine (hub, work satellite, third machine) holds the
  same value or none, an unset `external_reviews` counting as its own value (local consent).
  The P1 builder reads the satellites' keys through their remote sessions, names and equality only, before writing
  code. A key whose values differ is left out of P1's fixed key list, `FLEET_KEYS` in `sd_lib`, so it keeps today's
  file reads and writes; the item records why.
  `external_reviews` is the likely one: it is the standing consent to send private code to external reviewers,
  and the work satellite holds the work repos.
- **Migration**: the lead runs `sd config set sd.<key> <value>` on the hub for each moved key. After P1 that writes
  the row. The file keys are deleted on every machine after one nightly cycle (row wins covers the gap).
- **Reader change**: `sd_lib.core_setting` for a key in `FLEET_KEYS`: the latest row (None when cleared), else
  `config.json` when the database never held a row, else unset. A failed read raises `ConfigError` naming the reason.
  Every caller already handles `ConfigError`. `sd config set` and `unset` write the row and never the file.
  `sd config get` and `sd config list` (text and `--json`, `bin/sd:1381-1391`) read through the same function and
  print each key's source; a file key present and ignored gets a stderr line.
- **Dashboard control**: Settings gets a "Fleet" section above the browser-only ones. It shows the moved keys, each
  with its value, its source (database, file or default) and its `CORE_CONFIG` description, and a control
  (select for `copilot_review` and `external_reviews`, text for `fleet_owners`). The server runs
  `sd config list --json` and `sd config set`, as Management already runs `sd config get sd.assistant_merge`, so the
  pattern lives in one place. There is no stale-write check: two concurrent manual writes are outside the threat
  model. The page shows the old value `sd config set` prints, and flags it when it differs from the value shown.
  A timeout is an unknown outcome: the page says so and reads the value again.
- **Satellite with no hub**: the read fails and names the hub. The callers that read these keys (`sd-review`,
  `sd-ship`, `sd fleet stamp`) also need the hub for their items, so they stop either way.
  `external_reviews` unknown means no external review: fail closed.

## Delete first

Each of these goes instead of moving. The config-folder deletions are lead actions with a backup; the code ones
belong to the named slice.

- `<config>/project-dashboard/hoa.conf`: its reader went with sd:3985; the file was left behind.
- `<config>/repo-sync/repos.common.conf.bak-3036` and two `repos.common.conf.pre-*-removal-*` copies: dead backups.
- 8 `label|` lines in `documents.conf` whose key no checkout claims: they rename nothing today.
- `<config>/project-dashboard/.env.bak-20260928`: a dead backup beside the dashboard's config.
- `allowed-repos.conf`, its `.example` and `DEPENDABOT_ALLOWED_REPOS_FILE` (S2).
- The deprecated `fleet.owners` fallback in `sd_fleet.py:400-410` (`configured_owners`). The hub's `config.json` has no `fleet` key; the
  P1 builder checks the work satellite before deleting it.
- After each slice's nightly cycle: `repos.*.conf` on the hub and the work satellite (the third machine keeps its
  one-line bootstrap copy), `home-tiles.conf`, the moved `label|` lines, and the three `config.json` keys.
- sd:3988's files, if Open question 2 closes the item: nothing moves, and the drive file could shrink to its two
  `root|` lines plus the two routes that differ, if the reader layered the tracked example under it. That layering
  is a new behaviour and is not part of this design.

## Non-goals

- No dashboard path to create a repo row for a repo not yet cloned. `add`, `seed` and reconcile stay the only ways
  a row appears.
- No change to `DENIED` or the `platypeeps` owner rule in `ROUTINE.md`.
- No move of `root|`, `img|` or `skip|` documents lines, of `disk.conf` or `report-families.conf`, or of plugin
  settings (`hoa.*`, `sdw.*`, `songs.*`).
- No cache of setting rows on a satellite.

## Slices

| Slice | Repo | Items | Migration | Size | Needs |
| --- | --- | --- | --- | --- | --- |
| P1 | pack | sd:3989; `setting` kind for sd:3987; `view` label for sd:3986 | 030 | M | C1's 029 merged |
| P2 | pack | `repo.clone_profiles`, `repo.dependabot`, `repo clones`/`dependabot` verbs, `list --clones`, `seed --clones` | 031 | M | 029 merged |
| S1 | system | sd:3983 reader and reconcile | none | L | P2 merged, `seed --clones` run on all three machines |
| S2 | system | sd:3984; sd:3986; sd:3983's Management control | none | M | P1 and P2 merged |
| S3 | system | sd:3987; sd:3989's Settings control | none | M | P1 merged |

P1 and P2 are separate pull requests because each moves state in steps. P2 can be built in parallel with P1.
S1 stands alone for the same reason. S2 groups the Management controls (one file pair, `management_screen.py` and
`management.js`) with Documents. S3 groups the two pages that write setting rows.
Each migration follows the existing hub-pin sequence: the hub's `refresh` migrates and publishes `hub-pin`, and each
satellite runs `repo-sync.sh follow` before its next database use. Between those two, the hub refuses the satellite's
older build by name (`remote.py:337-353`), as for every migration since sd:3279. In that window `repo-sync` on the
satellite still syncs from its snapshot or conf (steps 2 and 3), and the other readers report the refusal.
Nothing here widens that window, and no slice needs a satellite to work on an older pin.
Dashboard work starts with the `hallmark` skill and `ui-design/foundation/`.

## Failure table

| Slice | Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- | --- |
| P1 | migration 030 rebuilds `state` | table renamed, recreated, rows copied, indexes recreated | stop mid-file | `migrate` runs the file in one transaction; the store stays at 29 with every row | migrate a 29 fixture with rows of every kind, abort before commit, assert version 29 and row count |
| P1 | `sd config set` of a fleet key | one setting row | write fails | nothing written; the old answer stands; the verb exits non-zero | set with a connection that raises; get still returns the old value |
| P1 | `sd config unset` of a fleet key | a null row | file key still present | null row wins; the file value never returns | unset with a file key present; get reads unset |
| P1 | read on a satellite with the hub down | none | hub unreachable | `ConfigError` naming the hub, not the file value | `core_setting` with a refusing connection and a file key present raises |
| P1 | rollback to the 29 library | rows of kind `setting` | old library refuses schema 30; reversing drops the only copy of values set after the cleanup | before reversing: `sd config list --json` and the `dashboard.home_tiles` row's text go to a file under the bulk root; reverse 031 first (its header), then 030; then the old `sd config set` writes each moved key back to `config.json` and the tile text goes back to `home-tiles.conf` | schema test runs both headers' reverse in order on a 31 fixture; a test asserts `sd config list --json` carries every moved key with its source |
| P2 | migration 031 | two columns | stop mid-file | one transaction; the store stays at 30 | migrate fixture, abort, assert version and columns |
| P2 | `seed --clones P` | rows added, P added per line | stop after line k | each line commits alone and only adds; a rerun finishes | inject a failure at line k, rerun, compare with one clean run |
| P2 | `seed --clones P` | none for that line | a line absent on disk with no row | named; exit 1 after the other lines; the lead runs seed on the machine that holds it, or drops the line | fixture with one such line: other lines land, exit 1, the line named |
| P2 | `repo clones PATH -P` | profile removed | concurrent `+P` from the dashboard | read and write in one transaction; the second sees the first | two writers on one row in threads; final set is one of the two orders |
| P2 | `list --clones P` | none | a row holding P that is not a conf line (path or remote) | exit 1 naming the row; `repo-sync` uses its snapshot | fixture with one bad row: exit 1, nothing on stdout |
| S1 | snapshot write | `clones.<profile>` | killed between write and `mv` | the temp file is ignored and overwritten next run | leave a temp file; next run reads the DB and replaces it |
| S1 | snapshot write | `clones.<profile>` | the profile was never imported (empty answer) | empty is unknown; the snapshot is kept; steps 2 and 3 answer | stub `sd-db` printing nothing with exit 0: snapshot unchanged, attention line |
| S1 | reconcile add | row, then `+P` | stop between `add` and `+P` | the next run finds a present checkout whose row lacks P and reports it, not writes it: a cleared P may be the operator's choice. The report and Management show the half-done add; one click finishes it | stub `sd-db` failing `clones +P`; rerun prints the "present, not listed" line and makes no write |
| S1 | an absent listed checkout | none | sync fails to clone | the existing failed-repo report; the row keeps P | existing sync failure tests, run on a DB-backed list |
| S1 | list from snapshot or conf | none | hub down | reconcile writes nothing; sync and pulls run; attention line names the source | stub `sd-db` exiting 1; assert no write call and the line |
| S1 | rollback to the previous `repo-sync.sh` | none | confs already deleted | copy the snapshot to `repos.<profile>.conf` and write an empty `repos.common.conf` (personal and work need both, `repo-sync.sh:79-98`); the README's revert line says so | copy a snapshot and an empty common into a temp `REPO_SYNC_CONF_DIR`; step 3, today's reader kept as is, runs `list` and prints every line |
| S2 | first Rename | one root `view` row | write fails | page shows the error; the derived name still shows | POST with a refusing store; page keeps the derived name |
| S2 | label import loop | 7 root `view` rows | stops at row k | rerun the loop: `set_view` with the same label writes the same answer | run the loop twice in a test; `views` equal |
| S2 | `dependabot` read in `holds.py` | none | hub down or no pack | fail closed; "allowed list unknown" in the log | stub connection raising, and a missing pack folder; no other-owner repo swept, line printed |
| S3 | first Save of tiles | one setting row | stale `before` | 409; nothing written | two saves from one page read; second gets 409 |
| S3 | tiles read | none | read fails | "tile list not read", no Home Assistant call | refusing connection with a file present; no HTTP call made |
| S3 | Fleet set from Settings | one setting row via `sd config set` | `sd config` times out, before or after its commit | the outcome is unknown: the page says so and reads the value again | stub `sd` that writes, then sleeps past the bound: the page reports unknown, and the re-read shows the new value |

## Acceptance criteria

- [ ] P1: `python3 -m unittest tests.sd_db.test_schema tests.sd_db.test_settings` passes; `sd config get sd.copilot_review`
      on the hub prints the row's value after the import, and `sd config get` names an ignored file key on stderr.
- [ ] P2: on the hub, `sd-db repo list --clones personal | sort` equals the sorted union of the hub's
      `repos.common.conf` and `repos.personal.conf` lines (75 distinct repos across all four files, 0 lines missing).
- [ ] S1: `make test SUITE=repo-sync` passes; on the hub `repo-sync.sh list` prints the same total as before the
      switch; with `sd-db` stubbed to fail, `nightly` makes no `sd-db` write call and prints the fallback reason;
      `grep -n 'remove_entry' local-repo-sync/repo-sync.sh` prints nothing.
- [ ] S2: `sh local-dependabot/dependabot.sh allowed` prints the one live slug from the row; `dependabot.sh test`
      passes; the Documents page shows the 7 labels from rows after the import, with `documents.conf` reduced to
      its `root|` line; `grep -rn 'DEPENDABOT_ALLOWED_REPOS_FILE' .` prints only `docs/work` lines.
- [ ] S3: the Home page draws the same 18 entries from the row as from the file; a save with a bad entity id
      returns 400 naming its line.
- [ ] Privacy: `git diff origin/main...HEAD | grep -c -E -f ~/.config/system/privacy-patterns` prints 0 in each
      slice; no test reads the real config folder (each sets `SYSTEM_TOOLS_CONFIG` to a temp folder).

## Risks

- **20 new repo rows reach every `repo` reader.** Known readers: dashboard `controls.py:67` (repo pickers),
  `contribution_screen.py:179`, `health_cleanup.py:112,170`, `health_screen.py:847`, `health_collectors.py:122`,
  `queue_screen.py:284,333`, `hoa_screen.py:60`, `management_screen.py:238`, `server.py:670` (capture repos),
  `local-repo-sync/refresh_drain.py:200`, `machine-setup.sh:1037,1111`, and the pack's protection collector,
  `sd fleet`, runner and lane. The defaults keep the acting readers away: `machine-setup.sh:1111` writes only
  `runner_merge=auto` rows, and `:1037` only `--managed`. P2's builder enumerates every reader again from the code
  and filters each one that acts on a checkout (writes a file, calls GitHub, takes a lock) by `managed`. The
  pull-request body lists them.
- **One profile, more than one machine.** A cleared box stops the repo on every machine of that profile. Today each
  profile has one machine; a second personal machine that wants a different list would need a per-machine column.
  Accepted, and named in the README.
- **Deleting a checkout no longer stops its sync.** Reconcile used to drop an absent checkout's line, so `rm -rf`
  was the way to stop cloning a repo. Now the box on Management is, and sync re-clones a deleted checkout that is
  still listed. The nightly report names each re-clone with that remedy.
- **A work checkout root outside `~/repos`.** `list --clones` derives the subdir from `~/repos/`; a row elsewhere is
  skipped and named. The live work rows sit under `~/repos/`.
- **A `core_setting` caller that cannot open the database.** It gets `ConfigError`, never a silent default. P1's
  builder checks each of the seven callers.
- **Fleet values that differ by machine.** Handled by the precheck; a skipped precheck would send the hub's
  `external_reviews` consent to the work satellite.
- **Evidence ties.** The snapshot belongs to one profile (its name carries it), and is written only from a non-empty
  exit-0 `list --clones` for that profile. A Save of tiles carries the `before` the page read, so it applies only to
  the text shown. A Fleet set carries none; the page shows the value it replaced.

## Open questions

1. sd:3986: where do Documents labels and hidden roots live?
   - Option: `view` rows keyed by the root key, edited on the Documents page (Recommended). No migration; it reuses
     sd:3430's store, endpoint and Undo; it covers a root that has no repo row.
   - Option: two `repo` columns (`doc_label`, `doc_hidden`) in migration 031, edited on Management. The item's
     wording; it covers only roots that are repo rows (7 of the 7 live labels are), and adds a setter and a control
     away from the page that shows the label.
2. sd:3988: build the intake editors?
   - Option: do not build; close sd:3988 with this page as the reason (Recommended). Last edit 2026-09-19; low value.
   - Option: build the cheap shape after S3: two setting rows edited with S3's text editor (S).
   - Option: build a structured rule editor for the 35 drive routes (M each), as the item first sized it.

## Status

Design only; no code.

One read-only adversarial review ran on the first draft (`codex exec`, 2026-10-13). Its 15 findings and their
dispositions follow. Each fix is in the section named.

1. Reconcile undid dashboard choices (it removed P from an absent checkout before sync and re-added P to a present
   one). Fixed: reconcile no longer edits profiles from disk; it adds rows only for checkouts with no row and reports
   the rest (sd:3983, Reconcile).
2. The run's list was read before reconcile, so a removal still synced. Fixed: the list is read after reconcile, and
   reconcile no longer removes (sd:3983, Reader change).
3. A skipped row made a partial list look complete. Fixed: `list --clones` exits 1 on any row it cannot print.
4. An unimported profile answered with an empty list; terra was not seeded. Fixed: an empty answer is unknown;
   seed runs on all three machines before S1; an absent unregistered line exits 1.
5. The half-present guard let a partial outage delete. Fixed by removal of the cause: no run removes a profile.
6. Hide before Rename lost the file label under per-key row wins. Fixed: Documents moves in one step and stops
   reading `label|` (sd:3986, Migration); `views` keeps a label-only root row.
7. A failed `views` read brought file labels back. Fixed: it shows derived names and the reason.
8. A key that failed the precheck had no code path. Fixed: P1's `FLEET_KEYS` holds only keys that passed; the
   precheck covers all three machines and treats an unset `external_reviews` as a value.
9. `sd config list` was missed. Fixed: it reads through the same function and prints the source.
10. The Fleet control claimed a stale-write check `sd config set` cannot make. Fixed: the claim is gone; concurrent
    manual writes are outside the threat model, and the page shows the value it replaced.
11. A timeout after commit was asserted as "row count unchanged". Fixed: the outcome is unknown and re-read.
12. Reversing 030 lost values set after the cleanup, and the reverse order was unstated. Fixed: export first,
    reverse 031 then 030, write the values back through the old verbs (P1 rollback row).
13. An older satellite cannot use the database after a migration. Clarified, not new: the existing hub-pin `follow`
    sequence covers it, and `repo-sync` keeps syncing from its snapshot meanwhile (Slices).
14. Restoring only the profile conf could not start the old script. Fixed: the recovery writes an empty common conf
    too, and the test runs `list`.
15. `dependabot.sh` does not put the pack's `lib` first. Fixed: `holds.py` adds `$SD_PACK_ROOT/lib` itself, and a
    missing pack is a failed read.

Not verified here: the satellites' config values and `config.json` keys (the builders read them through the remote
sessions), and the migrations themselves (no code yet).
