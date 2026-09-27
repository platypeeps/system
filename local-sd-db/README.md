# local-sd-db

The one database, and the fixture harness both repositories test against.

This folder is a Python package, `sd-db`. It is the only folder here that
builds and the only one with a test suite, which is why the repository guide
has a note about it.

## What is here

The database, the library that owns every write, the provider registry, the
backup, and the fixture harness both repositories test against.

    sd_db/
      schema/001_initial.sql   the initial workflow tables
      schema/004_publication_claim.sql   immutable publication payloads and active claims
      schema/002_source_commit.sql  the commit a migrated row was read from
      schema/007_state_check.sql  `check` joins the `state` kinds: the runner's
                    passed repository check, one row per run keyed by run id,
                    carrying the tree hash it checked (sd:495)
      schema/009_personal_and_followup_kinds.sql  `personal`, `followup`,
                    `work-idea` and `personal-idea` join the `item` kinds, so a
                    to-do, a followup and an idea that is not an article are
                    rows rather than a convention in free text (sd:730)
      schema/012_recurrence.sql  `recurrence` and `recurrence_anchor` on
                    `item`: a task that recurs, and whether its next date
                    counts from its due date or its completion (sd:1099)
      schema/013_state_by_kind_key.sql  the index `state (kind, key, id)`:
                    the latest checkpoint or heartbeat for one key is one
                    index read, not a scan and a sort of its kind (sd:1433)
      schema/014_home_relative_repo_paths.sql  repository paths under
                    `$HOME` become `~/` keys, so a second machine resolves
                    the same rows; needs the functions `migrate` registers,
                    and carries its reverse in its header (sd:1439)
      schema/015_repo_managed.sql  `repo.managed`, 0 or 1: whether the
                    operator manages the repository. The column only, set
                    by hand with `repo managed`. Carries its reverse in its
                    header, run before 014's (sd:1619)
      schema.py     the version, the table list, the migration files
      recurrence.py the RRULE subset a recurring task carries -- FREQ,
                    INTERVAL, BYMONTH, BYMONTHDAY, stdlib only -- and the
                    next occurrence after a date; `workflow.change_status`
                    creates that occurrence when it completes the row
      database.py   opening it: WAL, foreign keys, and the two version refusals
      migrate.py    applying migrations, by command and never on open
      writes.py     every write, as a named function
      registry.py   providers.yaml merged with the provider and bill rows;
                    the first read through a writable connection seeds the
                    rows from the file, a read-only one gets the merged view;
                    given a connection and no path, the file is the one
                    beside that connection's database, not `$HOME`'s, so a
                    fixture never reads the operator's registry
      paths.py      a repository's stored key (`~/` under `$HOME`) and its
                    disk path; writers store the key, disk uses expand it,
                    and lookups probe the key and the legacy absolute form
                    (sd:1439)
      repos.py      how the `repo` table fills, which is what bounds the
                    `docs/work` enumeration
      backup.py     VACUUM INTO a dated directory, then restore and compare
      retention.py  the nightly row prune `backup` runs after its snapshot
                    passed: exec output files at ninety days, one heartbeat
                    row per key, clean run reports settled `done` after a
                    week, never a cost row or an exec note
      shadow_sync.py  the tracker collector `sd shadow sync` calls, with the
                    watermark it resumes from
      shadow_jira.py  the second tracker's collector, the same `Collected`
                    over Jira's REST API and the operator's involvement;
                    lifted from the pack's `dashboard/jira.py` on sd:361;
                    `sync(tracker="jira")` dispatches to it
      contributions.py  local contribution metadata, activity, dependencies, and acknowledgements
      contribution_github.py  bounded GitHub observations and release/package proof
      contribution_sync.py  durable refresh queue and notification delivery
      protection.py  the fleet's branch protection, one `repo_protection`
                    row per registered GitHub repository, observed on the
                    tracker's budget and classified as `sd-status` classifies
      sources/      the five migrations: freeze, import, verify, and the one
                    retire that exists -- `docs/work`'s
        frontmatter.py  the block at the top of a prd and a vault note
        index_cache.py  the pack's index.sqlite  -> shadow
        docs_work.py    docs/work/*/prd.md       -> item, from committed trees
        register.py     the simulator's register -> item
        vault.py        Blog Ideas and Topics    -> item of kind idea
        issues.py       the open GitHub issues   -> shadow, never item
      jobs/         the runnable halves of `sd-db.sh`'s verbs
      testing/
        remote.py     a real bare repository, a pull-request table, and the
                      answers to the questions the system asks about them
        github.py     an HTTP door and a `gh` door onto that one object
        providers.py  the two registry shapes: a `url` endpoint and a
                      `start` command that records its spawn
        stubs.py      launchctl, tailscale, curl, caffeinate, lsof and
                      local-notify, on PATH rather than patched over
        home.py       a `$HOME` with the system's directories, and the one
                      environment that points every door inside it

## Using it

    ./sd-db.sh init           # create ~/.local/share/sd/sd.db and seed it
    ./sd-db.sh migrate        # apply migrations; nothing migrates on open
    ./sd-db.sh status         # the path, the schema version, what is unresolved
    ./sd-db.sh restore DIR    # put a dated backup directory back
    ./sd-db.sh backup         # snapshot, then restore the snapshot to prove it

    ./sd-db.sh repo seed      # register what <config>/repo-sync/
                              # repos.common.conf and repos.<profile>.conf
                              # name and this machine has actually cloned
    ./sd-db.sh repo add PATH  # register one checkout
    ./sd-db.sh repo list      # the repositories the enumeration is bounded by
    ./sd-db.sh repo list --managed   # only the ones the operator manages
    ./sd-db.sh repo runner-merge PATH manual|auto   # may the runner merge it
    ./sd-db.sh repo managed PATH yes|no   # does the operator manage it

A `repo list` row reads `path remote status_source managed runner_merge`.
`remote` is the checkout's origin URL as written, ssh or https: the runner clones over it, so it is not respelled.
Rows are compared by repository identity, so the two spellings of one GitHub repository match.
`repo add` on a registered path rereads the checkout, so it refreshes a row whose repository moved to a new owner.
The `mode` column is the checkout's shape, `work` or `bare`, which the runner reads.
It is not the pack's `full`, `minimal` or `guest` mode; that lives in the repository's `CLAUDE.local.md`, and the table does not copy it.
`runner_merge` stays the last field, because instructions read it there.
`managed` is `yes` or `no`, and every row starts at `no`.
No rule derives the flag: the GitHub owner does not decide it, so the operator sets each row.

`status_source` says who owns `docs/work` item status: the prd files (`file`) or the rows (`row`).
Every new row starts at `file`; only `retire docs-work` switches it to `row`, directly.
`retiring` is not part of that switch: a restore sets it on a repository whose snapshot predates its retire,
and `sd restore reimport` clears it.
The retire switched every repository registered at that time, with or without a `docs/work` folder.
A repository registered later stays at `file`.
A completed retire commits the marker `docs/work/.status-source` in each such repository with a `docs/work` folder.
An interrupted retire can leave a `row` repository without the marker, so a missing marker is not proof of `file`.
Without a `docs/work` folder the value changes nothing, because the repository has no work items.
So `file` there is correct, and it needs no migration.
`work register` refuses a `file` repository; run `retire docs-work` before a first work item there.

    ./sd-db.sh work register docs/work/<item>/prd.md
                              # register one folder as the row that owns it,
                              # from inside the checkout that holds it
    ./sd-db.sh import SOURCE  # freeze, import and verify one source; SOURCE is
                              # index, docs-work, register, vault or issues
    ./sd-db.sh verify SOURCE  # compare a source with the rows, and name every
                              # difference
    ./sd-db.sh retire SOURCE  # hand a source over to the database, once;
                              # docs-work is the only one with this step

    ./sd-db.sh usage          # the month's cost, per bill and per role, and
                              # every `bound` row; `--month YYYY-MM` for
                              # another month, `--json` for the read as the
                              # Usage screen's `/api/usage` serves it. Its
                              # one write is the orphan sweep a reservation
                              # runs (`release_orphans` in `sd_db/ledger.py`)

    ./sd-db.sh test           # the package's own tests; extra arguments go
                              # to unittest, e.g. `test -k name`
    ./sd-db.sh check          # what CI runs: `test`, the whole suite
    ./sd-db.sh build          # a wheel, into ./dist
    ./sd-db.sh install VENV   # install that wheel into a virtual environment

From a test:

    from sd_db.testing import FixtureHome, FixtureRemote, GitHubDouble
    from sd_db.testing import gh_environment, install_gh

    home = FixtureHome(tmp_path)
    remote = FixtureRemote(tmp_path / "remote")
    with GitHubDouble(remote) as github:
        install_gh(github, home.bin)
        home.merge(gh_environment(github, home.bin, base={"PATH": ""}))
        subprocess.run([...], env=home.environment())

    assert [call.door for call in remote.calls] == ["gh"]

## Machine-specific settings

The library reads these from the environment; each has a default.

| Variable | Default | What it sets |
| --- | --- | --- |
| `SYSTEM_TOOLS_LABEL_PREFIX` | `local.system-tools` | launchd label prefix of cron jobs (`<prefix>.cron.<job>`), the runner and the dashboard |
| `SYSTEM_TOOLS_ROOT` | `~/repos/system` | checkout whose `local-cron-jobs/cron-jobs.sh` the Jobs controls run |
| `SYSTEM_TOOLS_CONFIG` | `${XDG_CONFIG_HOME:-~/.config}/system` | private config root: `repo-sync/repos.*.conf` for `repo seed`, `cron-jobs/jobs/*.job` for the Jobs area |
| `REPO_SYNC_PROFILE` | `personal` | which `repos.<profile>.conf` `repo seed` reads beside `repos.common.conf` (terra reads its own alone) |
| `CRON_JOBS_EXTRA_DIRS` | none | further job-file directories, colon-separated, read after the config jobs dir |
| `OBSIDIAN_VAULT` | `~/Documents/Obsidian Vault` | vault the `vault` source reads |
| `SD_REGISTER` | `$SD_REPO_ROOT/research/world-simulator/00-overview/open-questions.md` | register the `register` source reads |
| `SD_WRITING_DESTINATIONS` | none | extra hand-recorded publication targets beside `blog` and `substack`, comma-separated |

## `judgments`: what the judgment models cost, by stage

`sd-db.sh judgments` compares the stages that route a decision through a
judgment model. `local-jev` writes a `judgment` row per call, and the
mechanism each caller used before it writes one too, under the same stage key
and in the same table -- a fallback that cannot be counted against a judgment
answers nothing.

The table holds identifiers and counts and nothing that was submitted. No
prompt, state, path, subject or body reaches it, and the two columns that
could carry content are shaped rather than merely capped, because a length cap
lets anything short through: `answer` is a **number** -- a probability, a
score, or a position into the caller's own criteria -- and `ordering` is
positions into the caller's own input. `sd_db.judgment` refuses a row rather
than truncating either.

The names are shaped too. `caller`, `stage`, `primitive`, `question_id` and
`pair` are identifiers -- letters, digits and `.`, `_`, `:` or `-` -- because
they are caller-controlled and a cap alone accepts `--stage
/Users/someone/private.txt` and a short subject line with it. `provider` and
`model` are exempt on purpose: they carry vendor names such as
`anthropic/claude-opus-5`, which come from a vendor and not from a subject
line.

A gate event is not a decision. A caller that declines at the gate and then
records what its own mechanism did writes two rows for one decision, so the
comparison leaves the gate row out of the calls and the declines and counts it
on its own line instead. Nothing is lost: that line is the only number a
stage whose callers only decline has ever had.

It is deliberately not a `cost` row. `cost` is money against a `bill` and its
`provider` is a foreign key into the registry; a judgment model is on neither,
so writing there would need a bill invented to satisfy the key and every sum
the Usage screen takes would have to learn to skip a source it never asked
about. `judgment.usd` stays NULL until there is a price list; the token counts
are the record until then.

    sd-db.sh judgments                 # every stage, both arms
    sd-db.sh judgments --since 2026-09 # a month; bounds are compared as text
    sd-db.sh judgments --json          # the same read, for a screen

## Automatic provider selection

Provider `roles` declare capability. Enabled flags control availability.
Automatic role orders choose which capable providers run without an explicit selection.
An enabled provider omitted from an order remains available for an explicit request.
Consent, independent-review rules, and spending controls still apply.

`provider_controls.configure` saves both nonempty role orders and all enabled choices atomically.
Each list must contain distinct, known providers with the declared capability.
Each role needs an enabled provider; the first author and reviewer must differ.
Rank-only changes preserve disabled reasons, bills, and identity fields.

The `provider-orders:v1` checkpoint records explicit membership without a schema migration:

```json
{"schema_version": 1, "explicit_providers": ["claude", "codex", "minimax", "kimi", "baseten", "exo"]}
```

For listed providers, row ranks are authoritative. A `NULL` rank excludes automatic selection.
The v1 reader selects only `provider-orders:v1` and refuses unsupported versions stored under that key.
Without this checkpoint, legacy `NULL` ranks still inherit YAML ranks.
Malformed checkpoints or missing explicit provider rows refuse reads; they never restore automatic selection silently.
Later YAML additions retain their seed behavior. Previously excluded providers remain excluded.
Temporary YAML removal does not remove a provider from the checkpoint.

Snapshot revisions include this policy. The first explicit save records it, even when orders remain unchanged.
Later identical saves write nothing. A stale revision refuses the complete save.
Restore previous orders through the same API with a fresh revision, without restoring unrelated database rows.
Older libraries ignore this checkpoint; update their YAML orders and refresh consumers during a coordinated rollout.

## The item kinds, and the two words called `followup`

`item.kind` is a closed list, guarded by a CHECK. Eleven values:

| Kind | What it is |
|---|---|
| `work` | a change to a repository, planned under `docs/work/` |
| `task` | a scoped piece of work in a repository |
| `report` | a run report a scheduled job wrote |
| `dep` | something a `work` item waits on |
| `skill-review` | a review of a skill, run by the catalogue |
| `proposal` | reserved; nothing creates one today |
| `idea` | **a writing piece on the article ladder, and nothing else** |
| `work-idea` | an idea about a repository or product, not yet planned work |
| `personal-idea` | an idea about life, which never enters the writing pipeline |
| `personal` | a to-do, an errand, a chore; it belongs to no repository |
| `followup` | something to come back to, filed as an item of its own |

`idea` looks like the generic word and is not. `sd_db/writing.py` selects
`kind = 'idea' AND piece IS NOT NULL`, `sources/vault.py` imports Blog Ideas as
`idea`, and the dashboard renders writing controls on it. An idea filed as
`idea` is therefore a draft article in a publishing queue. That is why a
personal or a work idea has its own kind rather than a `stage` on this one.

**A `followup` item and a `followup` note are different things.** The note kind
hangs a followup off an item that already exists and is resolved against it
(`sd task note <item> --kind followup`). The item kind is a followup that
belongs to nothing else and is worked in its own right (`sd task add --kind
followup`). One sentence to decide by: if it cannot be resolved without the
parent, it is a note; if it would still make sense with the parent deleted, it
is an item.

**`task`, `personal` and `followup` items share one set of statuses.**
`workflow.allowed_statuses` offers these three kinds (`workflow.TASK_STATUS_KINDS`)
the five `TASK_STATUSES`: `planning`, `ready`, `in_progress`, `blocked` and
`done`. `workflow.change_status` accepts the same five, so `sd task status
<item> done` closes a followup item or a personal to-do as it closes a task.
Before sd:768 the task controls offered a followup or personal item no choices,
so they could not close one. The dashboard item screen offers a personal item
the status control without the details form (sd:772), because
`workflow.edit_item` refuses its details. It accepts a followup's since sd:809,
and since sd:816 the screen renders the matching form rather than naming
`sd task edit` in a hint. `work` keeps its delivery rules. `idea`, `work-idea`,
`personal-idea` and `report` keep their own workflows and get no status
choices. Any item with a queued or running assignment gets none.

**`proposal` collides the same way, and only these two do.** `note.kind` is
its own closed list of seven -- `followup`, `decision`, `proposal`,
`question`, `comment`, `exec`, `status_change` -- and the intersection with
the eleven item kinds above is exactly `followup` and `proposal`. Every other
word belongs to one table or the other and cannot be misread.

The two halves of `proposal` are not symmetric, which is the part worth
knowing. The **note** kind is live and ordinary: `sd task note <item> --kind
proposal` records a proposal against an item, and `reads.brief_notes` carries
it into a brief. The **item** kind is reserved -- nothing creates one, and
`sd task add --kind proposal` is refused, because `--kind` offers only the
five in `ADD_KINDS`. So when a screen, a document or a person says
"proposal" unqualified, it is the note; a `proposal` item would have to be
written by a direct library call, and none is.

That asymmetry is why the table above reads `reserved; nothing creates one
today` rather than describing a proposal. It is a row in a CHECK constraint
waiting for a use, not a thing you can file.

**`workflow.edit_item` is the verb a person uses to change a row's kind.** It
is not the only writer of the column. `writes.set_item_fields` accepts `kind`,
and the pack's `sd task add --kind` uses it for a new row. `writes.upsert_item`
writes the kind a source declares on every import, with no note. So a
vault-imported `idea` moved to another kind goes back to `idea` the next time
the vault lands. An edit through `edit_item` is checked and noted; the other
two writers are neither.

The target must be a kind this store's CHECK allows (`workflow.schema_kinds`
reads it from the schema) and one of `workflow.HAND_KINDS`, the same five the
pack's `sd task add --kind` files. A row may leave `task`, those four, or an
`idea` with no `piece`. A `work`, `report`, `dep`, `skill-review` or `proposal`
row keeps its kind, because its producer owns it. An `idea` that carries a
`piece` keeps its kind, because `writing.list_pieces` reads it by that pair. A
row whose `fields` carry `contribution` or `skill_review` keeps its kind,
because `contributions` and `skills_catalog` read those rows by kind
(`workflow.PRODUCED_FIELDS`). A move into one of the three repository-less kinds
(`workflow.REPO_LESS_KINDS`) must clear `repo` in the same edit, which a row
outside `workflow.DETAIL_KINDS` may also do. A
row with a queued, running or ending assignment is refused. `who` is required,
and the change writes a `comment` note reading `Changed kind <old> -> <new> by
<who>` in the same transaction.

Three kinds in that table -- `work-idea`, `personal-idea` and `personal` --
are filed with no repository, which is how they are read as a group:
`reads.backlog_items(connection, repo=reads.NO_REPO)`, or
`/backlog?repo=none`. They are `workflow.REPO_LESS_KINDS`, which the pack's
`sd task add` reads.

`followup` was the fourth until sd:809 (2026-09-14). A followup filed from a
code review is about one repository, and with no `repo` a checkout's brief
(`reads.brief_items`) and `backlog_items(repo=path)` never list it. So `sd task
add --kind followup` takes the enclosing registered checkout the way a task
does, and `workflow.edit_item` edits a followup's title, body, priority, due
date and repository (`workflow.DETAIL_KINDS`), so `sd task edit ID --belongs-to PATH`
moves one. Followups filed before then carry no repository until someone moves
them.

**That is filing behaviour and not a constraint**, which matters if you are
reasoning about what the store can hold rather than what it does hold.
`item.repo` is a nullable `REFERENCES repo(path)` with no per-kind `CHECK`,
and `create_item` accepts a `repo` for any kind, so nothing in this library
stops a `personal` row carrying one. What makes the group real is the pack
CLI: `REPO_LESS_KINDS` settles the repository question before it is asked, so
a row of these kinds carries a repository only if something outside that CLI
wrote one. Read the selector as
"the items nobody filed against a repository", not as a guarantee about the
schema.

`idea` is not among them despite sitting beside them in the table: a writing
piece belongs to the repository it is written in, and 22 of the 24 in the live
store carry one. (`report` also carries none, but a job writes it; these three
are what a person captures.) An empty `repo` means "every
repository", so the selector is what makes "no repository" askable at all. It is
an object and not a word, because `upsert_repo` validates nothing and
`repo.path` carries no CHECK: any string this library reserved could also be
registered as a repository, and one value would then answer two questions.
`reads.NO_REPO_TOKEN` is the spelling a URL uses, translated once at the edge.

None of the three can be run by an agent, and neither can a `followup` that
carries a repository and a branch. `runner._item`, which readiness, enqueue,
claim and the dashboard's run selection all go through, refuses every kind
outside `runner.RUNNABLE_KINDS` (`work`, `task`, `report` and `skill-review`)
with the kind as its reason, whatever repository and branch the row carries.
`delivery_candidates` is an allow-list of `work`, `task` and `report`. It used
to read `kind != 'skill-review'`, which was an exception list standing in for
"a personal item has no repository, so the join drops it" -- and the schema does
not enforce that. `create_item` accepts a repository for any kind and the item
screen can set one, so a `personal` row with a repository and a branch was a
delivery candidate. The three kinds named are the ones the dashboard's run panel
offers, less `skill-review`, which the panel offers and delivery does not.


## Four decisions worth knowing

**The schema version is `PRAGMA user_version`, not a table.** `schema.TABLES`
declares every storage table. Schema 4 adds `publication_claim` for immutable
rendered payloads and durable dispatch/receipt state, with a database-enforced
unique active claim per piece. Large image-bearing HTML stays outside ordinary
item reads. New storage still needs an explicit schema migration and inventory
update; version bookkeeping does not need another table. The `state` kinds
are a `CHECK` constraint on the table and `writes.STATE_KINDS`, kept the
same: `checkpoint`, `verified`, `restore`, `watermark`, `heartbeat`, and
since schema 7 `check` -- the runner's passed repository check, one row per
run keyed by the run id with the tree hash it checked, written by
`runner.record_check` for sd-review to read (sd:495). SQLite cannot change a
`CHECK`, so migration 7 rebuilds `state` in place, rows and ids preserved.

**Nothing migrates on open.** An upgrade that happens because a process
started is an upgrade nobody chose, under processes still running against the
old shape. So `migrate` is a command, and opening a database older than this
library refuses **writes** while still allowing reads -- a machine stopped
halfway through an upgrade can be inspected. A database *newer* than this
library is refused outright, reads included: a wrong answer read quietly is
worse than a process that will not start.
`sd-db.sh` runs the package from this checkout, so there the refusal names the
checkout and the fix: `git -C <checkout> pull --ff-only`. An installed copy
names a reinstall instead.

**A checkout behind the database runs the installed copy (sd:1765).** The
primary checkout is shared, and no session may move a HEAD it did not open,
so it can sit behind `origin/main` after a migration landed. The pack's
virtualenv holds a copy pinned to a commit; it does not follow the checkout.
So the verbs that open a database (`backup`, `serve`, and every verb
`sd_db.jobs.cli` answers) compare two numbers before they run: the
checkout's `SCHEMA_VERSION`, read from its file, and the one the copy
installed into `$PYTHON` reports under `python -I`. A newer installed copy
runs, with one line on stderr naming both versions. A tie or an older copy
runs the checkout, so a branch that adds a migration still runs its own.
`SD_DB_LIBRARY=checkout` or `SD_DB_LIBRARY=installed` forces one side.
`test` and `check` set `checkout`, so the suite tests this folder whatever is
installed. The pull remedy above now appears only when neither copy can open
the database.
`sd-db.sh library` prints the choice alone, `checkout` or `installed`, for a
caller that imports `sd_db` in its own process; `local-sd-plan`'s nightly
follows it (sd:1812).

## Two more decisions worth knowing

**It installs, it is never linked.** Both repositories run `pip install` at a
tag and never `pip install -e`. An editable install puts the checkout back on
`sys.path`, so a file missing from the package would still import and the
mistake would surface on another machine. `tests/test_installed.py` asserts
the imported copy is not the checkout — and `_build.py` has no PEP 660
`build_editable` hook, so `pip install -e .` is refused outright rather than
left to discipline.

**The build backend is in this folder.** `_build.py` is sixty lines of stdlib
that writes the wheel. The usual choice, setuptools, is fetched from the
network to build, which would make an offline install — and every test that
performs one — fail for a reason that has nothing to do with this library.
The package is pure Python with no dependencies, so there is nothing for a
general-purpose backend to do here.

## The backup restores itself

A backup nobody has restored is a file. `sd-db.sh backup` writes a
`checkpoint` row *before* the snapshot, `VACUUM INTO`s a dated directory, copies
`providers.yaml` and `commands.yaml` beside it, then reopens the copy, runs
`PRAGMA integrity_check`, finds that checkpoint row and compares every
table's count against the source. A path-bound manifest records every backup entry,
its content hash, and the database checkpoint. The default keeps every backup.
The default destination is `/Volumes/local/Backup/sd-backups/`, on the USB disk attached to this Mac.
`local-mirror-sync` mirrors `/Volumes/local/Backup` to iCloud Drive nightly, so the default copy also leaves the machine.
With the disk detached, a run on the default refuses and writes nothing: mount the disk or pass `--destination`.
`sd-db.sh backup --destination PATH` changes the root for one invocation and is not asked about the mount.
`SD_DB_BACKUP_ROOT` names the root the same way; a relative value is taken under `$HOME`.
The default was `~/Documents/sd-backups/` until 2026-09-25; older snapshots may remain there, and `restore` still reads them.
The disk is mounted `noowners`: every account sees itself as a snapshot's owner, so the `0700` directories keep out no local user.
The nightly `sd-db-backup` job writes to the default, `/Volumes/local/Backup/sd-backups`, and refuses when the disk is not mounted.
After the snapshot, the same job copies `/Volumes/local/Backup` to the NAS; that copy is the off-machine one.
The hourly `sd-db-backup-hourly` job writes to the attached USB disk, `/Volumes/local/Backup Local/sd-backups`.
`sd-db.sh backup --keep N` explicitly requests retention of N verified owned backups.
`sd-db.sh backup --keep-days N` instead deletes verified owned backups older than N days.
A directory goes only when its whole day lies more than N days back, so 7 keeps between 7 and 8 days.
Age, not a count, because a sleeping Mac misses hourly runs and 168 runs would then reach past a week.
Only directories past the window are opened, so an hourly run does not verify the whole week.
`--require-mount PATH` refuses before writing anything unless PATH is mounted and holds the destination.
A detached disk's mount point is an ordinary directory on the boot disk; the refusal exits 1 and mails.
`--no-row-prune` skips the nightly row prune and its report item; the hourly job passes it.
Retention checks the complete manifest and checkpoint before deleting a directory.
Legacy backups without this manifest remain available for restore and are never pruned.
Changed or moved backups, symlinks, and unrelated directories also remain untouched.
Same-day backups sort by their numeric suffix.

**A database waiting for `migrate` is backed up read-only.** The writable open
is the version gate, and the checkpoint row is a write, so until 2026-09-13
`backup` refused the very database `migrate` told the operator to back up
first. Now `SchemaTooOld` on the writable open reopens the file `mode=ro`
(`VACUUM INTO` writes only its output file), takes no checkpoint row, proves
the copy by integrity and counts, and says so: `taken read-only before
migrate`. The manifest records `checkpoint: false` as a note; retention does
not read it — it re-verifies through the checkpoint row, so such a snapshot is
never owned, never counted for `--keep`, never pruned, and the job runs no row
prune after it. It restores like any older snapshot: `restore` migrates the
staged copy up to the library's version. A database *newer* than the library
is still refused on open; the library that migrated it is the one to back it
up.

The row before the snapshot is what makes the check mean anything: written
afterwards it would be in the source and not in the copy, and its absence
would prove nothing.

Failures of the store never depend on the store. The library raises and
writes nothing; `sd-db.sh backup` sends one mail through the path the cron
jobs use and exits 1. The scheduled job is named `sd-db-backup` and runs that
subcommand: one entrypoint per folder, as the repository's convention 1 asks,
rather than a second script beside it.

### A broken source is exit 3, and is not a failed backup

`backup` runs `PRAGMA foreign_key_check` on the snapshot it just wrote.
On the copy and not on the live source, because the source connection is
autocommit and holds no read transaction across the check and the
`VACUUM INTO`: a writer landing between the two would orphan a row the check
never saw, while the table counts `verify` compares stayed equal. The copy
cannot move, and it is the image `restore` would consume.
`integrity_check` -- which `verify` runs on the same copy -- does not answer
this question: it asks whether the b-tree pages are well formed, so a database of
nothing but orphans passes it, and `VACUUM INTO` copies the orphans
faithfully. sd:744's six orphan rows survived the
nightly backups of 2026-09-11, -12 and -13, each of which reported "restored
and compared".

Violations do not stop the backup. The snapshot is taken, the prune runs, and
the summary line gains `BROKEN: N foreign key violation(s) in the source, in
<tables>` on stderr. The job exits **3** and `sd-db.sh` mails it under "the
source is referentially broken", which is deliberately not the "sd-db backup
FAILED" subject: everything the backup promised to do, it did.

Three and not two, because `argparse` already owns 2: `sd-db.sh backup --keep
nope` exits from argument parsing before the job runs, and the shell reads a
status rather than a traceback. On 2 a typo in the options would mail a
referential finding about a database nobody had looked at.

That snapshot is a **forensic copy and not a restore point**. `restore`
refuses a candidate with foreign key violations, and it refuses this one, so
the recovery story is: repair the live database, then the next night's backup
is the restorable one. The copy's value in the meantime is the pre-repair
record -- the orphans stay queryable in it after they are deleted from the
live store, which is how sd:744 was reconstructed.

### The nightly prune runs after the backup, never before

Once the snapshot restored and compared, `sd-db.sh backup` runs
`retention.prune` against the live database, with one retention table:

| rows                | retention                                                        |
|---------------------|------------------------------------------------------------------|
| `cost`              | never: the ledger a budget is admitted against and an item is reported by |
| `exec` notes        | never: the audit record -- entry, arguments, exit code -- leaves only with its item |
| `exec` output files | ninety days: the file under `executions/` goes, the note is marked `output expired` |
| `heartbeat` rows    | one per key: the newest, which is the only one any reader asks for |
| clean `report` rows | seven days in `planning`, then `done` by `retention`: the row stays, an attention report or one with an open followup waits for a person |
| backups             | thirty files, by `backup --keep N`; the scheduled job runs `--keep all` |
| kept worktrees      | never: a kept clone may hold work, and its retention is the runner's |

The prd's table also lists a request log at thirty days. There is no such
store -- the dashboard silences its HTTP log and writes no request rows --
so nothing is pruned for it, and nothing was invented to be pruned.

The prune does not take the caller's word that the backup passed: it
verifies the dated directory again (`backup.passed`) and looks for the run's
checkpoint row in the database it is about to prune. A backup that raised is
never reached; a snapshot that changed since, or another database's, is
refused with nothing removed. A failed backup therefore means no prune, and
the job's mail says which half failed. A broken source (exit 3) is not a
failed backup and does not skip the prune; if the prune then fails as well,
its message carries the referential finding too, so the mail never reports
one and swallows the other.

What it removed goes into one `report` item (`sd-db-prune:<run id>`, on the
Operations screen's Reports list) with the counts under `report.removed`,
and onto the verb's one output line as `pruned: N exec output(s) expired,
M stale heartbeat row(s) removed, K clean report(s) settled`. A clean report
is one `ingest` opened with `attention` false: no followup was ever opened
for it, so `acknowledge` was the only thing that moved one and the operator
had nothing to review on it -- 127 sat in `planning` on 2026-09-12 with
nothing to move them. An expired execution still reads through
`sd runner commands output` and the dashboard's history: the entry, the
arguments and the exit code are there, and the output says it expired
instead of reading as empty. The next backup knows the mark too -- a
completed execution without its log is otherwise an incomplete backup.

## What the harness will not do

It does not patch `subprocess`. A suite that patches it proves a call was
made; a suite that puts the command on PATH proves the call was made *and
spelled correctly*, because the stub parses the same arguments the real
command does. `tests/test_one_double.py` greps the repository to keep it that
way: a second GitHub double, or a `subprocess` patch around one of the
stubbed commands, fails the suite.

## `import` retires nothing; `retire` is the other verb

`import` runs three of the four steps -- freeze, import, verify -- and stops,
whatever source it is pointed at. Four of the five sources stay authoritative
and stay written by whatever writes them today, because the retire step of a
source lands in a pull request *after* the one that lands its writer, which
is what keeps the window in which a source is frozen down to minutes.

`docs/work` is the one that has reached that pull request. `retire docs-work`
is one sitting: it refuses under a pack whose `sd_lib` cannot answer both
`status_marker` and `delivered`, naming the version -- the first is how a
checkout with a database reads `.status-source` under the retiring repository's `docs/work/`, the second is what
a database-free one asks git once that marker exists, and the retire's commit
turns both paths on at once; refuses without a `verified` row for the
hash it just froze, naming the differences; and refuses on an uncommitted
file, naming it. Then it imports and verifies once more, takes a backup, sets
each repository's `status_source` to `row`, and makes one commit removing
every active item's `status:` line and adding `.status-source` under the retiring repository's `docs/work/`. The
archive keeps its lines, all 491 of them on the pack's default branch on
2026-09-06: they are records of what a finished item's status *was*.

The row is switched before the commit and not after, and that order is the
answer to being killed halfway. Killed before the switch, every line is still
in place and still authoritative, so a rerun repeats the sitting. Killed
after it, the rows answer and the lines are a stale copy of the same words,
so a rerun goes on to the commit. The other order has a window where the
lines are gone and the rows are not yet the answer.

Two things follow that are easy to read past:

* **A second run is the property, not a nicety.** Each migration reports
  seen, inserted, updated and unchanged separately, and a second run reports
  the same total with zero inserted. That is what makes rehearsing one safe.
* **A verify difference lifts the freeze and changes nothing.** The verify
  compares source and rows by identity and content, never by count, and names
  every difference. A clean verify writes a `verified` row in `state`; a
  difference writes none, and `retire` refuses without one carrying the hash
  it just froze.
* **The git-backed sources read committed trees, never the checkout's
  working copy.** Both read with `git show` and record the commit as the
  row's `source_commit`. Neither fetches: what lands is what the checkout
  last fetched. They differ in which branches they read:
  * `register` reads only the default remote branch, `origin/HEAD`'s target
    (`default_branch` in `sd_db/sources/docs_work.py`). Until 2026-09-11 it
    read the working copy, and on 2026-09-10 it landed O30 from a feature
    branch three days before `main` carried it. The sitting's report names
    the ref and the commit. The ref is not written to `item.branch` -- that
    column is the branch the runner works on, and `origin/main` is a
    remote-tracking name, not one.
  * `docs/work` reads every candidate `branches()` returns: the default
    plus each remote branch not yet merged into it. `live()` drops a branch
    whose change to a file is already in the default. Branches that still
    disagree on a status refuse the sitting. Otherwise the newest commit
    wins, and its branch is written to `item.branch`. An item that lives
    only on a feature branch lands from it, and the sitting names it in a
    note. See `freeze` in `sd_db/sources/docs_work.py`.

Measured against the real sources on 2026-09-06: `index.sqlite` 1,175 rows,
the vault 192 notes, the register 3 open entries, GitHub 3 open issues, and
`docs/work` 64 items across six repositories -- the last enumerated from the
`repo` table, with the archive excluded, which is the count requirement 2
states.

## After the retirement, a folder needs `work register`

The retirement left a hole nobody noticed for five days. `import docs-work`
reads the file source; every repository has now retired that source; so the
importer refused on the first repository it met and wrote nothing, for any
repository. That was the intended end state -- the rows are the answer, the
files no longer are -- except that the import was also the only thing that
*made* a row. (The refusal itself has since been settled as intended and made
a reported no-op: `import` and `verify` name each retired repository, read
whatever is still on `file`, and exit 0 when nothing they read failed. A fleet
with nothing left to read says so in one line and points here.) A `docs/work` folder created after the cutover therefore had no
row and, its `status:` line having been removed by the same retirement, no
readable status anywhere. `sd-status` reports that as `status-unreadable`, and
on 2026-09-11 it took a hand-written `INSERT` into the shared database to
clear one.

`work register` is that step, made a verb. It creates one row for one folder
out of what the folder and git already say, and decides nothing:

* the **title** and the **created date** come from the prd's frontmatter, and
  it refuses a folder missing either -- a row dated at its registration is a
  row whose idle clock starts today, and the first age sweep offers it as
  fresh work;
* the **source commit** is the newest commit touching the file, which is what
  the old import recorded. A folder is usually registered while it is still
  uncommitted; the verb says so and leaves the column null;
* the **branch** is the branch the work is done on -- the one meaning every
  reader of `item.branch` has: `runner.py:_item` refuses a row without one,
  `configure_item` refuses the remote default for it, `sd_plan.py` checks it
  out. It is the checkout's own branch when that is a local branch other than
  the default, which is what a runner clone on `plan/<slug>` is when `sd-plan`
  registers the folder it just wrote; on the default, or detached, it is left
  NULL for `sd runner prepare --branch` to fill. Until sd:462 it was
  `docs_work.default_branch`, `origin/main` -- a remote-tracking name that
  passes the runner's shape check and names no head, so 65 rows read as
  runnable and would have failed only inside the clone. `default_branch`
  keeps its meaning, the branch a merge lands on, for the file source;
* the **status** is always `planning`, because an item nobody has started is
  what a new folder is.

It takes no repository argument. The repository is the one enclosing the
working directory and the path is relative to it (R10-D6) -- a path that
leaves the checkout is refused rather than resolved, because the row it would
write names a file nobody standing there can read. It refuses a repository
that is not registered, and a repository still on the file source, where the
row would be a second answer to a question the prd already answers. Running it
twice is not an error: it reports the row that exists and changes nothing.

## Recovery after a restored snapshot

`sd-db.sh restore DIR` first validates a staged database, blocks old queued or
running assignments, and records an unresolved restore before replacing the
live contents through SQLite. A checkout cut over after that snapshot marks
its still-file-owned authority `retiring`; an already-row-owned snapshot keeps
its authority. Dispatch stays paused until `sd restore resume`.

For each retiring repository, run:

    sd restore reimport /registered/repository --dry-run
    sd restore reimport /registered/repository --if-fingerprint PRINTED_FINGERPRINT
    sd restore resume

The replay enumerates current artifacts as well as restored rows. It reads
`docs/work` statuses at each row's complete `source_commit`, and can recover
missing rows from committed source history. Writing rows use their captured
source body only when its recorded SHA-256 and path match; a missing row needs
committed historical source. An invocation without `--if-fingerprint` is a
preview and changes no rows. Apply requires and checks that preview's
fingerprint, then writes every recovered row, receipt and authority in
one transaction. Interrupted apply rolls back and can be retried. Neither
operation edits the checkout, requeues assignments, or recreates post-snapshot
progress. Missing evidence, conflicting later progress, corrupt source hashes
or vanished artifacts keep the authority held; use a newer verified backup or
reconcile the specific source rather than clearing the hold by hand.
Different row metadata is preserved even when no note records its edit;
absence of a note does not prove a row is disposable rehearsal data.

Backups include the complete `publications/` directory beside the database.
Its catalog, immutable manifests and numbered event hash chains must validate,
and every database claim must match the recorded payload and a state event.
Restore validates both the backup and live journals before installing the
database. It adds missing compatible evidence and retains newer live events;
conflicting or corrupt evidence refuses the restore. Journal additions are
never rolled back if database installation fails, because external operations
may already have happened. A legacy backup without a journal can restore, but
publication remains held until that journal is recovered and reconciled.
Before merging publication evidence, restore writes a durable
`publication-restore-intent.json` and a complete fingerprinted recovery copy.
A crash leaves publication and incomplete-creation repair held; rerunning the
same backup finishes the verified copy without mistaking a dispatched claim
for a new one. Completed restore evidence remains in a diagnostic archive.

`runner-journal/` is also backed up and validated against the database's run
identities and versions. Restore copies only missing run identities and keeps
every existing live run record, because restoring a database cannot decide
whether a process is still alive. Conflicting same-version evidence refuses.
A durable `runner-restore-intent.json` holds startup during an interrupted
copy; rerunning the same backup completes it. The runner then reconciles any
database/journal mismatch before dispatch. Recovery diagnostic archives are
copied into backups with a SHA-256 inventory for inspection; they are never
substituted for active ownership journals. `sd-db.sh item remove` and
`sd-db.sh repo remove` move a removed run's journal pair into
`runner-recovery-evidence/removed-<fingerprint>/`; those directories are
diagnostic, like the others.

The personal machine profile installs `sd-db-backup` at 02:10,
`sd-db-backup-hourly` at :50 every hour, and `shadow-sync-nightly` at 02:20
through `local-cron-jobs/cron-jobs.sh`. Install and verify those jobs
explicitly after refreshing the source. The nightly backup job uses this
folder's `backup --keep all` verb and the shared cron failure log; the hourly
one uses `backup --require-mount /Volumes/local --keep-days 7 --no-row-prune`;
shadow sync uses the pack's `sd shadow sync --strict`.
The search watermark advances only after complete search coverage and durable staging of contribution identities.
Incomplete search coverage keeps that cursor and retains valid observations.
Contribution details refresh separately through a durable queue, on the same request and time budget as the search,
until that budget runs out -- stopping short of what the protection collector needs, and before any observation the
remaining requests could not finish. Explicit local work is ordered first, then two ordinary authored pulls, then the
rest of the queue; what a run attempts rotates behind what it did not reach.
Deferred details remain queued after the search window advances.
Known open contributions refresh again even when GitHub search returns no recent updates.
A single contribution's incomplete observation -- a dependency with no configured release tag, a pull whose API read
failed -- is held on that contribution's own `contribution-observe:` heartbeat with its reason, listed as `incomplete`
in the result and both heartbeats, and reported; it does not make the collect unsuccessful, hold the cursor, or mark
the tracker degraded. A remaining backlog is reported as `queued`, not as a failure. Only the detail collector's own
failures -- no authenticated operator, a queue checkpoint that moved -- make strict collection unsuccessful, and
none of them erase successful observations.
These are LaunchAgents: the user must be logged in, and Documents access must
be granted to the actual Python interpreter under launchd. A terminal run does
not verify that grant. Inspect `cron-jobs.sh status JOB` and the dated backup's
integrity before treating the schedule as operational.

Shadow sync searches a fixed UTC interval for each of its four buckets. It
follows cursors and divides ranges above GitHub's 1,000-result search limit.
Inclusive child ranges share their boundary second, so fractional timestamps
cannot fall between them. URL identities deduplicate the shared endpoints.
Each terminal range must return its advertised unique count. Split descendants
must also account for the parent's count. Changed counts, invalid timestamps,
malformed pages, exhausted limits, or saturation within one second fail coverage.

The defaults allow 1,000 requests within 600 seconds, shared by the search,
the contribution details and the branch-protection sweep that follow it (the
search alone was allowed 200 in 300 until 2026-09-12; see `MAX_REQUESTS`).
Authentication has a separate 60-second timeout. Each GraphQL timeout uses the remaining budget,
capped at 60 seconds. The heartbeat records `window_start`, `window_end`,
`requests`, and per-bucket terminal `coverage` entries with counts and completion.
Overlapping leaf counts must not be summed as unique issues.
The heartbeat timestamp records actual attempt completion. Historical recovery
bounds remain in `window_end`; they do not hide a later failed attempt.

For a bounded recovery, `sync(connection, since=lower, now=upper)` accepts aware
datetimes and normalizes them to UTC seconds. Equal bounds are allowed. The
pack exposes these as `sd shadow sync --since LOWER --until UPPER --strict`,
with optional `--max-requests` and `--max-seconds`. A complete historical or
disjoint interval retains an existing cursor. Advancement requires coverage
from that cursor through the new upper bound. A first collect without a cursor
uses the requested start, or the default 90-day window.

### Contribution tracking

`sd task contribution add/edit/list/show/ack` manages explicit contributions through the shared library.
Unfiled work retains its local clone, branch, test evidence, and dependencies in a database task.
Ordinary authored pull requests use durable URL checkpoints without creating tasks automatically.
Every form shares one ordered projection for the dashboard and `sd-status`.
External observations never change a local task's workflow status.

A row carries exactly one identity, and `_configuration` refuses a second one:
a **pull request** (`pull_url`), a **filed issue** (`issue_url`), an **issue
draft** (`target_repo`, `draft_title` and `draft_path`, all present and no
URL), or an **unfiled branch** (`local_clone` and `local_branch`, the rule
that applies when the row is none of the others). `pull_url` with `issue_url`
is refused, and so is a draft carrying a clone or branch; a draft with only
some of its fields is refused by name. An issue URL has its own pattern and
validator — `PULL` also proves `merge` and `release` dependencies, so it is
not widened. `target_repo` is `owner/repo`, stored as given, and must agree
with `issue_url` case-insensitively when both are present. `draft_path` is
`{"path": <absolute file>, "sha256": <digest>}`, shaped like an evidence
artifact and checked the same way: the file must be readable and match its
digest on write, and the projection re-checks it on every read and reports
`draft_verified` rather than raising — a draft whose body changed after
review is not ready to file. Filing a draft is an edit that adds `issue_url`
to the same item, so the ID and the `draft_*` fields survive it. The word is
*draft*; "unfiled" keeps meaning a contribution with no `pull_url`.

A filed issue is keyed `issue:<url>`, beside `github:<pull URL>` and
`item:<id>`; its checkpoint is `contribution:issue:<url>` and its heartbeat
`contribution-observe:issue:<url>`. The prefix is deliberate: the projection
picks a row's observation by key prefix and reads a `github:` source as
pull-request-shaped, so an issue sharing it would be a pull request that never
merges. A draft has no key of its own — it lives under `item:<id>` alone, as
an unfiled branch does. `issue_url` and a draft's `(target_repo, resolved
path)` are unique across rows, as `pull_url` and `(local_clone,
local_branch)` are. Changing `blocking_labels` invalidates the `issue:`
snapshot exactly as it does the `github:` one.

`LANES` orders the projection: `newly_unblocked`, `awaiting_you`,
`awaiting_them`, `merged`, then `closed`. `closed` is the terminal lane for
any row whose observation reads `state: closed` — an issue, or a pull request
closed without merge; it sorts after every non-terminal lane so finished work
never sits among live work. A closed-unmerged pull request awaits no one, so
it no longer sits in `awaiting_them`, where nothing could retire it; a later
observation that reads `open` returns it there. `merged` stays pull-request vocabulary.
Active attention still wins: a closed issue with an unacknowledged event, or a
pull request with an unacknowledged `Closed without merge`, is `awaiting_you`
until the event is acknowledged. The projection lists an
unregistered `issue:` checkpoint once, as it does a `github:` one, and a
registered one only through its item.

`observe_issue` is the issue counterpart of `observe_pull`, storing the
collector's issue observation under `issue:<url>` with the same freshness,
revision and actor-identity checks, the blocking-label staleness refusal for
a registered row, and no CI or mergeability baselines — an observation that
carries any pull-only key (`reviews`, `ci`, `head`, `base`, `mergeable`,
`draft`) is refused as a pull read at an issue URL. The event vocabulary is
one list for both sources: an issue reuses `comment`, `label_added`,
`label_removed`, `closed` and `reopened`, and adds `closed_completed` and
`closed_not_planned` because GitHub reports the close reason as a field and
the two are different news. `converted_to_draft` and `ready_for_review`
never occur on an issue and are refused there. The attention triggers are
deliberately wider than the pull request's on one point: **any comment by
someone other than the operator fires** (`Maintainer comment`, `Mentioned
you`, else `New comment`), because a watched issue has no review machinery
to carry a maintainer's answer. The label rule is the pull rule unchanged —
a configured blocking label, currently applied. Closes state their reason
(`Closed as completed`, `Closed as not planned`, plain `Closed`) and a
reopen is `Reopened`; `Closed without merge` is pull-request vocabulary and
never appears on the issue path. Lifecycle families collapse to their latest
event as they do for pulls, and the operator's own actions never fire. A
re-collected unchanged issue announces nothing twice.

The `depends_on` kind **`issue`** takes a `url` and is resolved when the
issue is closed as `completed` or a pull request referencing it has merged;
closed as not planned resolves nothing. Its proof is `{"url", "closed",
"state_reason"}` plus `merged_pull` (a canonical pull URL) on the merged
path, and `_satisfied` demands the exact URL and one of those two facts. It
is remote like `merge`: the collector reads it and the core holds the item
while its state is unknown. A dependency cycle is still walked through
`item` links only, so an item depending on an issue that depends back on it
is refused as before, and the release/merge cross-proof neither includes nor
is disturbed by an issue dependency. `contribution_sync` prices every
`issue:` entry at `ISSUE_REQUESTS` and an `issue` dependency in
`DEPENDENCY_REQUESTS`, both measured against the fake transport in the
tests; `_cost` now refuses a dependency kind the table does not price
instead of reserving zero requests for it, which is how a kind that
validated but never resolved used to pass unnoticed. `plan` queues an
`issue:` entry for every row whose URL is an issue and whose observed state
is not `closed`; `refresh` routes each queued entry by its key prefix —
`github:` to the pull collector and `observe_pull`, `issue:` to the issue
collector and `observe_issue` — and retires a closed issue from the queue
once its notices are delivered, as it does a merged or closed pull.

Merge and release requirements are separate.
Release proof checks the configured tag and merged commit ancestry.
An optional package requirement also checks the exact published version and usable distribution hashes.
Missing proof keeps the dependency unknown; stored evidence commands never run during collection.
A release dependency may be recorded before its tag exists, but once its pull merges there is no proof to read
without one: the item is held with `exact release repository/tag is required` on every collect until `tag` is
configured (`sd task contribution edit`), and the collect itself is unaffected.

Each attention event has durable acknowledgement and notification state.
The collector claims notifications before calling `notify` outside the database write transaction.
Ambiguous sends remain held for reconciliation instead of automatic retry.
Acknowledgement does not claim notification delivery or complete local work.

Coverage describes the current search results observed within the updated
interval. GitHub's mutable search index cannot provide an immutable historical
snapshot through this API. Count checks detect observed inconsistencies; they
cannot prove that the index never changed between requests.
See GitHub's [search limit](https://docs.github.com/en/graphql/reference/search),
[pagination contract](https://docs.github.com/en/graphql/guides/using-pagination-in-the-graphql-api),
and [inclusive range syntax](https://docs.github.com/en/search-github/getting-started-with-searching-on-github/understanding-the-search-syntax).

### Branch protection, fleet-wide

`sd_db.protection` writes one `repo_protection` row per `repo` row whose
remote is github.com: two GETs each (`repos/{o}/{r}`, then the default
branch's classic `protection`), a third for the branch's rules when the
classic endpoint answers 404 — that is every repository's basic observation,
made first for all of them — and then, from whatever budget is left, one per
gating ruleset for its bypass list, so a bypass lookup never costs a later
repository its row. All of it is made by `shadow_sync.sync` after the
contribution refresh and on the same request budget. `classify` is pure and names the
gaps exactly as the pack's `sd-status` does — `enforce_admins`,
`required_checks`, `strict`, `required_not_produced`,
`produced_not_required`, `reviews`, and `bypass` for a ruleset actor that
does not reach administrators (an app, a team, a user, a deploy key): only
`OrganizationAdmin` is `enforce_admins` off, and a `RepositoryRole` bypass
is `enforce_admins` unknown naming the role, since which role its numeric
id names is confirmed nowhere here. Either bypass is reported per ruleset
with the merge-gating rules that ruleset carries, `review (#7)
[pull_request] by OrganizationAdmin 1 (always)`, because GitHub layers
rulesets and a bypass on one reaches none of another's rules: the sentence
then names the rulesets still binding administrators and the ones not known
either way, and says "every rule below" only when neither is left; the
detail's `bypass` and `admin_bypass` lists carry the same scope — plus the
`squash_message` and
`rebase_merge` merge-settings flags; `produced_contexts` reads the registered
checkout's `.github/workflows/*.yml` (files only, never git) for the checks
it produces. A 200 protection object makes a row `protected`, and so does a
ruleset whose rules gate a merge (`pull_request`, `required_status_checks`),
read before the classic 404 is interpreted at all, since a ruleset-protected
branch answers 404 there to its admin too. The rules are read beside a 200
as well, and layered onto it per rule, strictest source wins (sd:1430): a
source nobody can bypass is firm, a stricter one a bypass can skip is only
`advisory`, a bypass is the `bypass` gap only when it removes a rule's last
firm source (otherwise `bypass_info`), and `enforce_admins` holds when every
rule has a source binding administrators. Two or more sources read
`source: combined` with a `sources` map per rule; one source reads as it
did. A rules read that fails beside a 200 keeps the classic row and names
the fault in `rules_read_error`. A 404 with no gating rule is
`unprotected` only from a token with `admin` on the repository: GitHub
answers 404 on classic protection to any other token whether or not the
branch is protected, so that 404 is `unknown` with the permission as its
reason. A 403, a timeout, a malformed body or an exhausted budget is
`unknown` with a `reason`, and a registered repository nothing has observed
reads as `unknown` too. The collector cannot fail the tracker's sync or hold
its watermark; it records its own `protection-sync:github` heartbeat.
`rows` is what the dashboard's Protection screen reads.

## Retiring a row: `item remove` and `repo remove`

    sd-db.sh item remove ID --who NAME --reason TEXT [--apply --if-fingerprint HEX]
    sd-db.sh repo remove PATH [--with-items] --who NAME --reason TEXT [--apply --if-fingerprint HEX]

The verbs take one `item` row, or one `repo` row, and everything that hangs
off it: notes, assignments, runner runs and leases. `--who` and `--reason`
are required for the preview too; nothing falls back to the login name. The
preview changes nothing. It prints every row by table and key, every refusal
with its code, the journal files that move after the commit, the files and
references the remove leaves, a warning for each row an importer can bring
back, the note count and the fingerprint; its last line is the exact apply
command. Exit 0 is a clean preview, 3 a preview with refusals, 1 an error.
The apply requires the fingerprint the preview printed and refuses when the
store has changed since (G3). It takes a backup first, then in one
transaction under `control_gate` files the record, deletes the rows children
first, and checks foreign keys; then it moves each removed run's journal
pair into `runner-recovery-evidence/removed-<fingerprint>/`. A signal before
the commit removes nothing and exits 1. Exit 4 means the rows are gone and
the record is filed, but the move did not finish; see below.

**The record.** Each apply files one `report` item, `<kind>-remove:<fingerprint>`,
whose `fields.record` is `item-remove` or `repo-remove`. Its body is the
manifest: the target, the reason, who, the fingerprint, the backup
directory, and one `<table> <key>` line per removed row. Its `comment` notes
hold the rows themselves, one JSON line each, in `n of N` chunks headed by
a sha256 of the lines that follow. `fields.report.actor` records `who`,
`reason`, `principal`, `program`, `pid`, `ppid` and `session`.

**Finding a removed row.** `removal.records(connection, table, key)` lists
the records whose manifest names `<table> <key>`; the reports list and the
dashboard's reports screen show each record as a `report` item. To read the
row back, take the chunk notes in `n of N` order, check each sha256, and
parse the line. The snapshot on the manifest's `backup:` line is a second
source, but only through a scratch home: `restore(directory, home=<scratch
home>)` in `sd_db/backup.py`, then read the row from that copy. Never
restore the snapshot over the live store to get one row back: `restore`
replaces the whole store, and every write since the snapshot, the record
included, is lost.

**Putting a row back.** There is no re-insert verb. The manual path:

1. Stop and read the record: every chunk note, each sha256 checked.
2. Open the store with `connect()`, so `foreign_keys` is on, and take
   `control_gate` and one `BEGIN IMMEDIATE` transaction.
3. Insert parent-first, the reverse of the delete order: `repo`,
   `repo_protection`, `item`, `note`, `assignment` (one statement for all,
   so an `after` or `parent` chain inserts together), `runner_run`,
   `runner_lease`, with the recorded column values, ids included.
4. Run `PRAGMA foreign_key_check`; commit only when it returns no row.
5. Still inside `control_gate`, and after the commit, move each put-back
   run's journal files back. List `runner-recovery-evidence/removed-<fingerprint>/`
   and take the run's files from that listing, checking each name and
   sha256 against `receipt.json`. A file the receipt names that is still in
   `runner-journal/` is checked there and left in place; when the quarantine
   directory does not exist, both files are still in `runner-journal/` and
   nothing moves. Refuse when the target in `runner-journal/` exists. Then
   `os.rename` `<quarantine>/<run>.json` to `runner-journal/<run>.json`,
   then the same for `<run>.lock`, in the same session so `control_gate` is
   still held.
6. Add a note to the record saying the rows are back and why.

**An apply killed after its commit.** The rows are gone and the record is
filed; only the journal move is unfinished. The verb prints the lines to run
when it can, and they are the same by hand: create
`runner-recovery-evidence` when it is absent, then the record's
`removed-<fingerprint>` directory, each with `mkdir -m 700`; then, for each
listed file still in `runner-journal/`, `.json` first, run
`mv -n <file> <to>/<name> && test ! -e <file> && test ! -L <file>` with each
path quoted. A line that fails most often means the target exists: nothing
is replaced, and the operator compares sha256 before anything else; read the
command's own error first, since `mv` also fails on a missing source or a
permission it lacks.

**A retained clone.** A run whose retained clone directory still exists is
refused (I6 for an item, P4 for a repo). So is a `.pruning-clone` that a
stopped prune left beside it. The preview prints one line for the run:
`runner.sh retained-remove --clone-only --assignment N --who NAME`
(sd:1793). The operator puts their own name for `NAME`: the plan takes no
actor values. That verb (sd:1780) checks the assignment's runs and locks,
finishes a stopped prune, and files a record naming the operator. With
`--clone-only` it has the old `chflags -R nouchg` and `rm -rf` pair's scope:
it removes each released attempt's `clone` and `.pruning-clone` only, and
keeps `kept.tar`, `archives/`, `ignored/` and the directories. The refusal
says what stays. The alternative is to wait until the run is 30 days old, run
`runner.sh prune` for the plan, then `runner.sh prune-apply --fingerprint FP
--who NAME` with its fingerprint. An unmounted volume refuses without the
line.
