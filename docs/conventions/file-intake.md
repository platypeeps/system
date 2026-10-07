# The file-intake convention

An intake notices what arrived in one source since its last run, sorts each
arrival into a route, and reports it. It reads. It never acts.

`local-drive-intake` is the reference instance. It walks two mounted Google
Drive roots. `local-mail-intake` is a second instance: its source is a Gmail
query, not a folder, and it keeps the same verbs, exit codes, state layout and
log. An export-directory importer, built later outside this repo, is the
third, and it walks an export directory.

Each rule below comes with the failure that produced it. Keep the failure with
the rule: a rule without its reason gets dropped by the next person who finds
it inconvenient.

Written from item `sd:1096`, `docs/work/2026-09-19-a-file-intake-convention/`.

## Shape

One folder, one entrypoint, per the repository conventions in `CLAUDE.md`. The
entrypoint is a POSIX `sh` wrapper that hands the verbs to a Python module and
runs the suite on `test`. It resolves its own folder through symlinks, because
`local-bin-links` may put it on `PATH`.

State lives outside the source, in `~/.local/share/<name>/`, overridable with
`<NAME>_STATE`. The config lives in the config folder,
`<config>/<name>/<name>.conf`, overridable with `<NAME>_CONFIG`. `<config>` is
`$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`. The repository ships only
`<name>.conf.example`: a real config names roots, and a root name can carry an
account address.

## The source

**A source is a set of roots**: a mounted folder, an export directory, or a
query against a service. The config names each root with a short label that
appears in every report line.

**Unreachable is not empty.** Check every root before walking any of them. A
missing root exits 1 and names the root. It is never walked as an empty tree.
Google Drive for Desktop signed out looks exactly like every file deleted.
Reporting that as "nothing new" is a silent failure on the one day it matters.

**One bad entry does not stop the walk.** A read error on one file mid-walk
skips that file. A mounted Drive raises on an entry the daemon is evicting, and
one transient error must not hide the other three thousand files.

**Never write to a source.** Open a source path only with read operations.
Prove it with a test, not by reading the code: snapshot a fixture tree's paths,
sizes, nanosecond mtimes and SHA-256 hashes, run `fetch`, `report` and
`status`, and compare. `test_fetch_never_modifies_the_source` in
`local-drive-intake/tests/` is the model. A source that is a service gets the
equivalent: `TestNeverSends` in `local-mail-intake/tests/` fails if a write
call appears in the module at all.

**Skip churn, and say why.** Platform artefacts (`.DS_Store`, `._*`, `Icon\r`)
and sync staging folders (`.tmp.drivedownload`) are never walked. Each skip
carries its reason in the source. A staging folder that is walked reports a
dozen new files a day that are gone before anyone looks.

## The state file

**One file, `intake-state.csv`, replaced on every `fetch`.** It records what
the last walk saw. The reference instance keys a row on `root` and `rel_path`
and compares `size` and `mtime_ns`. Its columns are `STATE_COLUMNS` in
`source:local-drive-intake/drive_intake.py::STATE_COLUMNS`.

**Compare cheap attributes, not content.** The question is "what is new", not
"what is corrupt". Path, size and mtime answer it; the reference walk takes
57 ms. An instance whose source is not a file tree keeps its own key. The mail
instance keys on thread id and compares the newest message id.

**Absent state means first run, and the first run is a baseline.** It records
the state, logs nothing, and exits 3. Three thousand files being new is not
news.

**Replace it atomically**: write a sibling and rename it. A fetch interrupted
halfway through a direct write leaves a truncated state, and the next run
reports every missing row as new.

**Do not report a plain deletion.** Nothing downstream acts on one.

**Pair moves only when the pairing is unique.** A service that implements a
move as delete-and-create makes a folder reorganisation read as hundreds of
arrivals. Pair a vanished entry with an appeared one on a key a move preserves
(the reference uses basename, size and mtime). Refuse a pair that is not unique
on both sides and leave it as a new arrival. Under-reporting a move costs one
line; inventing one hides a real document.

## The arrivals log

**Append-only.** `fetch` adds one row per arrival and never rewrites what is
there. There used to be one report file that every `fetch` replaced. A
hand-run `fetch` at nine silently ate the arrivals the 06:45 digest would have
mailed.

**The reference schema** is `ARRIVAL_COLUMNS` in
`source:local-drive-intake/drive_intake.py::ARRIVAL_COLUMNS`:

| Column | Meaning |
|---|---|
| `detected_at` | when the `fetch` that found it ran, ISO 8601 with offset |
| `root` | the root's label from the config |
| `rel_path` | path relative to the root |
| `route` | the route the rules (or the judgment step) chose |
| `change` | `new`, `modified` or `moved` |
| `size` | bytes |
| `mtime` | the file's modification time, ISO 8601 |
| `from_path` | the old path of a `moved` row, else empty |
| `reported_at` | the delivered stamp, empty until a consumer stamps it |

Every instance carries `detected_at`, an identity (here `root` plus
`rel_path`), `route`, `change` and `reported_at`. The other columns follow the
source.

**The delivered stamp is written by the consumer, after delivery.** Only
`stamp` rewrites the log, and only to fill `reported_at`. `fetch` never stamps
and `report` never stamps, so looking costs the consumer nothing.

**`stamp` takes a cutoff, never "everything undelivered".** A `fetch` can
append between the moment a consumer reads the log and the moment it stamps.
Stamping the lot marks rows that were never delivered, and they are then
invisible forever. The consumer records the newest `detected_at` it included
and passes it to `stamp --through`. A consumer that reads two logs keeps one
cutoff per log. `stamp` rewrites through a temporary file, so an interrupted
stamp cannot truncate the log.

The log also answers "what arrived in August", which a replaced report never
could. `report --all` reads the whole history.

## Routes

**The route set is closed and declared by the module.** The reference declares
`data-export`, `document`, `correspondence` and `noise`. A config row naming
any other route is a config problem, and `status` reports it.

**The rule table is ordered, and order is meaning.** One row per rule,
pipe-separated, three fields: `route|<route>|<glob on rel_path>`. Pipes,
because a Drive path holds spaces and an `@`. The first matching rule wins.
Put the narrow folder rules before the broad extension rules, so board minutes
route to `correspondence` and not to `document` on the strength of `.pdf`.
Match case-insensitively, and match both the path and its leading-slash form,
so a rule written `*/meetings/*` also matches a `Meetings/` folder at the top
of a root.

**The default is `noise`, and it is implicit.** A path no rule matches is
`noise`. There is no catch-all row at the bottom of the table, on purpose. A
new kind of file then shows up as a number in the noise count before anyone
writes a rule for it. `noise` is counted in every report and never listed. A
convention that lists its default, or ends its table with a catch-all into a
listed route, produces a digest that grows until nobody reads it.

**Route your own output to `noise` first.** A folder the repository itself
publishes into is outbound. A file appearing there is never an arrival.

**A judgment step, if any, runs only after every rule misses.** The reference
asks Jev to name a route for an unmatched path. It follows the repository's
Jev rule: rules first and unchanged, the stage switchable off, every
degradation lands in `noise` and says why on stderr, and only the path leaves
the machine, never the contents.

**A report stays readable.** Above 40 rows in one route, the reference names
folders instead of files.

## Duplicate arrivals

The same file can arrive many times without changing. A Drive-hosted Doc syncs
as a ~179-byte `.gdoc` stub whose content never changes, while every sync
touches its mtime. On 2026-09-19 eighteen `correspondence` arrivals were two
documents; sixteen were one notice re-syncing.

**Decision: the log keeps every row; each reader collapses on the identity.**

- **The log never collapses.** It is evidence. Each row is one detection with
  its own `detected_at`, and `stamp` marks rows, so a collapsed log could not
  say which detections a digest delivered.
- **The instance's own views collapse.** `peek` and `report` print one line per
  identity (`root` plus `rel_path`), with the number of arrivals when it is
  more than one. They already own presentation (the 40-row folder collapse),
  and a person reading `report` by hand is a consumer too.
- **A consumer that reads the log directly collapses on the same identity.**
  Presentation differs between consumers. The site morning digest lets `new`
  win over `modified` and counts `moved` rows instead of listing them; that is
  its `collapse` in the `site` repository's `tools/morning-digest.py`.

Why not only in consumers: every consumer then has to remember, and one that
forgets mails sixteen identical lines. Why not in the log: it loses evidence
and breaks per-row stamping.

`local-drive-intake` meets this since `sd:1427`: `collapse_repeats` in
`source:local-drive-intake/drive_intake.py::collapse_repeats` runs before the
40-row folder collapse, so repeats of one file never trip it.

## Verbs

Every instance answers the same verbs. A consumer can then drive any intake
without reading its source.

| Verb | Does | Exits |
|---|---|---|
| `fetch` | walk, diff against state, append arrivals, replace state | 0 changed, 3 unchanged or baseline, 1 error |
| `peek` | what a `fetch` would find; writes nothing | 0 something, 3 nothing or no state yet, 1 error |
| `report [--all]` | undelivered arrivals by route; never stamps | 0 something listed, 3 nothing, 1 no log and no state |
| `stamp [--through TS]` | fill `reported_at` up to the cutoff | 0 stamped, 3 nothing to stamp, 1 no log and no state |
| `status` | config, each root, undelivered count, state age | 0 fresh, 3 stale or never run, 1 unusable |
| `test` | the unittest suite | the suite's own |
| `help` | usage, verbs, environment | 0 |

**Decision: `peek` is mandatory.** `fetch` advances the state, so a look-only
verb is the only safe way to ask "what is new". Both existing instances carry
it with the same meaning. `local-mail-intake` confirms it on a source that is
not a folder. Type `peek`, not `fetch`, when you only want to look.

## Exit codes

**0** something changed or something to show. **3** nothing to do. **1** a
real error. They follow repository convention 6 in `CLAUDE.md`.

| Situation | Exit |
|---|---|
| A root is missing or unreachable | 1 |
| The config is unreadable or names no root | 1 |
| The source is reachable and nothing changed | 3 |
| First run: baseline recorded | 3 |
| `report` or `stamp` after a baseline, with no log yet | 3 |
| `report` or `stamp` before any `fetch` ran | 1 |
| Something new was appended, listed or stamped | 0 |

**3 and 1 are never confused.** A nightly job that exits 1 on a quiet day
banners a failure every quiet day, and then nobody reads the banner. The
reverse is worse: an unreachable source that exits 3 is the silent failure the
source section forbids.

**Tell the two absent-log cases apart with the state file.** State present
means a `fetch` ran, so an absent log is a quiet day. Neither file present
means no `fetch` ran, and the advice to run one is correct. The reference got
this wrong until 2026-09-19: every `report` after the baseline told the caller
to run the command it had just run, and exited 1.

`local-cron-jobs` treats any nonzero exit as a failure. A job that wraps a
verb maps 3 to 0 before it returns.

## The approval boundary

**Intake reports. Nothing it finds is acted on without a person saying so.**

- The intake never moves, renames, deletes, labels, files, replies to or
  otherwise changes anything in its source. The source tests above enforce it.
- A consumer turns arrivals into a digest for a person. It does not act on an
  arrival either: no file moved, no reply sent, no task closed, until the
  operator says so. Filing a proposed row for the operator to accept is
  reporting; closing or completing one is acting.
- A judgment step may route. It never approves, sends or deletes.

## Checklist for a new instance

1. One folder, one entrypoint, the verbs above, `help` exits 0.
2. Roots checked before the walk; a missing root exits 1 and names it.
3. A test proving the source is unchanged by `fetch`, `report` and `status`.
4. State file replaced atomically; absent state is a baseline that exits 3.
5. Append-only log with `detected_at`, an identity, `route`, `change` and
   `reported_at`; only `stamp --through` writes `reported_at`.
6. A closed route set, an ordered rule table, an implicit `noise` default, no
   catch-all row; `noise` counted and never listed.
7. `peek` and `report` collapse repeated arrivals of one identity.
8. The exit table above, with a test per row.
9. A README that names this convention and states only where it differs.

## Instances

| Instance | Source | Notes |
|---|---|---|
| `local-drive-intake` | two mounted Google Drive roots | the reference |
| `local-mail-intake` | a Gmail query over two aliases, through `workspace-mcp` | keys on thread; headers only, never bodies |
| an export-directory importer | an export directory, built outside this repo | consumes the `data-export` route |
