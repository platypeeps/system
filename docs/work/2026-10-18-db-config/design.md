---
title: Dashboard lists and the adversarial review's model move into workflow database setting rows, edited on Settings
created: 2026-10-18
item: sd:5109
---

# Design: db-config

Write this page only when the shape needs agreement before the code.
The item's row holds status, progress and decisions; this page holds the shape.
This is the system side of sd:5109; the pack side (repo-row config, `config.json` `config.*`) is its own branch.

## Problem

The operator ruled (note #23244) that non-secret configuration lives in the workflow database, files only where truly
needed, secrets never in it. Three dashboard lists still live in files under `<config>/project-dashboard/`:
`realms.conf` (Tasks' and Writing's realm views), `report-families.conf` (Reports' families, and the HOA and Home
pages' Reports and Briefs) and `hoa-thresholds.conf` (HOA's trend levels). The adversarial review's model and effort
live in `<config>/adversarial-gate/.env`. None can be edited from the dashboard, and a file on one machine is not seen
by the database the rest of the fleet reads.

## Approach

**Keys.** One setting row per list, its value the same pipe text the file held, so each file's parser stays the one
grammar (as `dashboard.home_tiles` did, sd:3987):

| File today | Row | Reader |
| --- | --- | --- |
| `<config>/project-dashboard/realms.conf` | `dashboard.realms` | `places.realms` |
| `<config>/project-dashboard/report-families.conf` | `dashboard.report_families` | `reports_screen.families` |
| `<config>/project-dashboard/hoa-thresholds.conf` | `dashboard.hoa_thresholds` | `hoa_screen.thresholds` |
| `<config>/adversarial-gate/.env` `ADVERSARIAL_MODEL` | `adversarial.model` | `adversarial.settings` |
| `<config>/adversarial-gate/.env` `ADVERSARIAL_EFFORT` | `adversarial.effort` | `adversarial.settings` |

**Row wins** (the transition rule of `docs/work/2026-10-13-config-to-db/design.md`). Once the database holds a row
for a list, its file is not read. A cleared row is a row too, so an old file cannot come back. The file answers only
for a list the database has never held, so the merge needs no flag day and the operator's first Save on Settings is
the import. A failed row read is unknown: the reader says "not read" with the reason and reads no file in its place.
Each reader takes the connection its caller already holds; one module, `setting_rows`, holds the read and the rule.
The callers: Tasks (`tasks_screen`), Writing, the rail's realm labels (`server._sections`, signed-in requests only, as
now), Reports, HOA, and the HOA and Home pages' Briefs and Reports (`places._family`). A family list that did not read
leaves those Briefs unknown too, even when the HOA checkout's own briefs could be listed: unknown is not "no family".
The adversarial keys have no file step: on 2026-10-18 the hub, the one machine that runs the dashboard
(`SD_HUB_ONLY_AGENTS`), held no `<config>/adversarial-gate/.env`, and its `sd-dashboard` LaunchAgent exports neither
`ADVERSARIAL_*` name. So a row, else the built-in default, is the whole rule. The run reads them before it writes any
state, so a failed read leaves no run behind and releases the lock.

**Settings.** A Dashboard section lists the five keys: a text area for each list, a field for model and effort, each
with its source (workflow database, the old file, unset). Two writes:

- A list posts `POST /api/settings/list {key, text, before}`, as Home's tiles do (sd:3987). `before` is the digest of
  the source and text the page showed. The write parses first and refuses a text with a problem (400, each problem
  with its line). It reads the current source and text again on its connection and answers 409 when the digest
  differs: a clear (a row holding none) and a file edit both change it, so an old tab cannot bring a value back. Then
  `set_setting` checks the row in its own transaction (`before` the row's value when the source was the row).
  An empty text writes a cleared row.
- Model and effort post `POST /api/settings/row {key, value, before}`, which replaces `/api/settings/sweep` and serves
  the Sweeps keys too, with the same rule: `before` is the row value shown, and an empty value unsets. These keys have
  no file, so a cleared row and no row both mean the default.

**Deletions.** `realms.conf.example`, `report-families.conf.example`, `hoa-thresholds.conf.example` and
`local-adversarial-gate/.env.example` go: their grammar moves to the Settings descriptions and `docs/pages/settings.md`.

**Kept as files, with the reason.**

- `documents.conf`: serving a document reads no database by the sd:3986 design, and its readers include `server.py`
  and `health_collectors.py`, which sd:5134 changes now. Filed as a question on sd:5109.
- `disk.conf`: the hub's volume paths, read by Health, which sd:5134 changes now. Same question.
- `home-tiles.conf`: already row wins on `dashboard.home_tiles` (sd:3987); the live hub has no row yet, so Home's
  "Edit tiles" Save is its import. No code change.
- `shared.env`: `OBSIDIAN_VAULT` has pre-database shell readers (`machine-setup.sh` obsidian stage, `dashboard.sh`
  exports it to the LaunchAgent, `task-actions.sh`, `lib/config.sh`), and the pack reads it from the environment.
  It stays, beside `SYSTEM_TOOLS_LABEL_PREFIX`.
- `SD_NOTION_PRIVATE_FOLDER`, `SD_NOTION_TEAM_FOLDER`: no system code reads them. The pack's
  `bin/sd_research_publish.py` reads them from its environment; the move is a pack change, sent to the pack builder.
- `research.conf.py` `drive=` / `notion=` keys: the pack's `sd-research-kit publish` reads them; system's
  `publishing.designate` only writes them. A row needs the pack reader first; same handoff.

**Tool `.env` inventory** (every committed `.env.example`; key names only):

| Keys | Keep or move | Reason |
| --- | --- | --- |
| `ADVERSARIAL_MODEL`, `ADVERSARIAL_EFFORT` | move | Read by the dashboard alone; the gate reads neither. |
| `*_PASSWORD`, `*_API_KEY`, `*_KEY`, `*_TOKEN`, `*_AUTH*`, `HA_TOKEN`, `NTFY_TOPIC` | keep | Secrets; an ntfy topic is a capability. |
| `HA_MCP_URL` | keep | `ha-mcp.sh` reads it too; one source with the shell entrypoint. |
| `EMAIL_FROM`, `WORKSPACE_MCP_URL`, `NTFY_SERVER`, `IMESSAGE_TO`, `NOTIFY_EMAIL_TO`, `JEV_NOTIFY` | keep | `notify.sh` reads them; Notes reads two of them beside it. |
| `JEV_*`, `KEV_*` | keep | The `jev` and `kev` shell wrappers read them; experiments too. |
| `AGENT_METER_FILE`, `NAS_COPY_*`, `VOLUME_PROBE_*`, `LLAMA_*`, `KEV_DIR`, `HF_HOME`, `WORLDMONITOR_DIR`, `AI_SONGS_*` | keep | One machine's paths; a row serves the whole fleet. |
| Docker service users and ports (`CLICKHOUSE_*`, `FALKORDB_*`, `POSTGRES_USER`, `*_PORT`) | keep | Shell entrypoints read them before any service runs. |
| `CRON_JOBS_*`, `MACHINE_SETUP_*`, `TASK_ACTIONS_PORT` | keep | Bootstrap and launchd: read before the database, on satellites too. |
| `POLICY_NAME`, `MANAGED_TAG_KEY`, `SIMULATE_PROFILE`, account `*.env` | keep | `aws-setup.sh` shell, per account. |
| `GRAPHITI_*`, `MOCK_*`, `OTLP_*`, `FLUENTBIT_*`, `AURA_*`, `LLM_*`, `MEZMO_*` | keep | Compose and shell services read them at start. |

## Non-goals

- No change to `sd config`: at the gate's pack pin it writes rows only for `sd.*` fleet keys. The lead imports through
  Settings, or the `set_setting` lines in the PR body.
- No deletion of the operator's files: the lead deletes each one, with a backup, after its row is written.
- No change to `documents.conf`, `disk.conf`, the Home tiles, `shared.env` or pack readers.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Merge, before any row | nothing; readers read the old file | none | the file answers as before | `TheRowWins.test_no_row_reads_the_old_file` |
| Settings Save (import) | one `setting` row via `set_setting` | the row read for `before` or the write fails | nothing written (one transaction); the page names the error; Save again | `TheDashboardSettings.test_a_failed_read_writes_nothing` |
| Settings Save, concurrent change | none | the row changed since the page read it | 409, the page reads the value again | `TheDashboardSettings.test_a_stale_digest_refuses_with_409` |
| Settings Save, bad text | none | a line the parser cannot read | 400 with each problem; the row keeps its old value | `TheDashboardSettings.test_a_text_with_a_problem_is_refused_and_writes_nothing` |
| Page read after import | reader reads the row | the database read fails | "not read" with the reason; no file read in its place | `TheRowWins.test_a_realms_row_that_does_not_read_is_unknown_and_reads_no_file`, `test_a_families_row_that_does_not_read_leaves_reports_and_briefs_unknown`, `test_a_levels_row_that_does_not_read_is_unknown_on_every_trend` |
| Clear on Settings | a cleared row (`None`) | none | the list is unset; the old file is not read again | `TheRowWins.test_a_cleared_realms_row_hides_the_file`, `TheDashboardSettings.test_an_empty_save_clears_the_row_and_the_file_is_not_read_again` |
| Settings Save, list cleared or file edited since the read | none | the digest of source and text differs | 409; the page reads again | `TheDashboardSettings.test_a_clear_or_a_file_edit_since_the_read_refuses_with_409` |
| Adversarial run, settings read | nothing yet: read before any state file | the read fails | 503 naming it; no run file, lock released | `FailedRuns.test_a_failed_settings_read_starts_no_run_and_releases_the_lock` |
| Lead deletes the old file | file gone (backup kept) | the row was never written | the lead reads the row back first (PR body); else the page says "not configured", and the backup or a Save restores it | covered by the missing case of each reader |
| Rollback to the previous commit | rows stay; old code reads the files | the files are gone or older than the rows | before rolling back, the lead writes each row's current text to its file (PR body); a cleared row is a deleted file | not testable here; named for the lead |

## Acceptance criteria

- `make test SUITE=dashboard` (the leg holding these suites) passes with 0 failures and 0 skips.
- With a `dashboard.realms` row and a `realms.conf` that disagrees, Tasks shows the row's realms; the same holds for
  families on Reports and levels on HOA.
- With a failing settings read, each of the three pages says "not read" and none reads its file.
- `POST /api/settings/row` writes `adversarial.model`, and the next adversarial run's state file names that model.
- `git grep -n 'conf\.example' -- local-project-dashboard` names none of the three lists' files.

## Risks

- The adversarial review in the design round (codex, read-only, 2026-10-18) found five issues: the reader callers were
  not all named; `before=None` let an old tab overwrite a newer clear; a failed family read could read as "read"
  through place Briefs; the adversarial fallback was dropped on one machine's evidence; cleanup and rollback could
  restore old values. Each is answered above: the callers listed, the digest write, unknown Briefs, the evidence
  named with a read-before-state run, and the readback and export steps.

- A list saved on Settings that the old file still names differently: row wins, so the file is dead text until
  deleted. Accepted; Settings names the source of every value.
- The lists hold the operator's job names and repository globs. The rows sit in the private database, never in this
  public repository; the examples in docs use `example` names.
- Moving `/api/settings/sweep` to `/api/settings/row` breaks a page served by an older server build until restart.
  Accepted: `make post-merge` restarts the server, and the page and server ship together.
