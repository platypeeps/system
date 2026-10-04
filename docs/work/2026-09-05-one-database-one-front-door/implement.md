---
title: implement — three slices, twelve pull requests, all in system
status: planning
created: 2026-09-05
---

# Implement

Twelve pull requests in three slices, and the slices are not contiguous:
item A's slice sits between B's first and second, and item D's after B's
last. The `prd.md`'s landing order numbers all five, and the numbers below
keep that numbering rather than renumbering B's own.

**This item lands code in two repositories, and the library is here.**
`local-sd-db/` in this repository holds `sd_db`, and
`local-project-dashboard/` here is the dashboard that is rebuilt on it
(`prd.md:158-163`, `prd.md:468`). The pack gets the `sd` verbs, which
import `sd_db` from the pack's virtualenv as a built copy installed at this
checkout's tag, `pip install` and never `-e`. So **the pack imports the
library and never the reverse**, and a slice whose two halves are separate
pull requests lands `system`'s first. Criterion 1 asserts exactly this
shape: `sd_db` imported from the pack's virtualenv resolves to a file
outside this checkout, which is only true when the source lives here and
the virtualenv holds a copy.

The pack's own `dashboard` package is not rebuilt. It is retired
(`prd.md:353-354`), and nothing below builds anything inside it.

**Settled 2026-09-05, on the operator's word: the `sd` verbs are the pack's,
and seven of the twelve pull requests below are pairs.** Round four found
that no pull request here built an `sd` verb while six criteria asserted
against one. `git grep -n "add_parser(" bin/sd` in `sd-ai-command-pack`
returns `plugin` (`:2777`), `store` (`:2795`), `config` (`:2895`) and
`sweep` (`:2916`) and nothing else, so `restore` (criterion 1), `today`
(4), `exec-log` (12), `deps` (21), `usage` (15), `note` (16) and
`skill review` (19) are all greenfield. `~/repos/system` has no
`bin/` and no `sd` binary, so `system` cannot hold them. Items C and D both
wrote their plans against pack verbs already; this item was the outlier, and
the C-90/C-101 reversal is narrowed rather than undone.

**What the reversal keeps.** C-90's finding was that the *library* is
`system`'s and the installer step is item A's. Both stand. What is corrected
is the sentence C-90's fix over-reached into — "every pull request in this
item is `system`'s" — which was true of the library and false of the CLI.

**The seven pairs, and which half lands first.** PR 2 (`sd restore reimport`
and `sd restore resume`), PR 4 (`sd today`), PR 5 (`sd exec-log`), PR 6
(`sd deps`), PR 8 (`sd usage`), PR 10 (`sd note resolve` and
`sd note list`) and PR 12 (`sd skill review`). **`system` lands first in
every one of these**, the reverse of item D's rule, because here the pack
verb is the caller and `sd_db` is what it calls: a pack half merged first
would import a library that is not yet installable from a tag that does not
exist. D's pairs run the other way because there the runner is the caller
and the verb is what it calls.

**`sd shadow sync` is item A's, not PR 3's.** PR 3 placed it inside
`local-sd-db/`; item A's PR 8 lists `sd shadow sync (new)` in its own
Touches and says "it is built here". Two items scheduled the same new
command in two repositories and neither named the other. Under the decision
above the pack owns it, so A's PR 8 keeps it and PR 3 keeps only the cron
entry that invokes it — hand-off 10.

## Slice 1, PR 1 — the harness, before anything claims it

**Repository:** `system`. **Touches:** `local-sd-db/` (new, whole) —
`pyproject.toml`, `_build.py`, `sd-db.sh`, `README.md`, `.gitignore`,
`sd_db/__init__.py`, `sd_db/testing/` and the package's own `tests/` — and
`CLAUDE.md`. An earlier draft named `sd_db/testing/` alone. That directory has
no parent package, no packaging metadata and no test runner today, so it does
not import, and this pull request's own Verification is that the package
imports — asserted, per hand-off 7, "from the library's own package tests",
which this pull request therefore also writes. `CLAUDE.md` is here because
`local-sd-db/` is the first folder in this repository that builds and the
first that tests, which `:4` says no folder does.

An earlier draft of this paragraph said the folder breaks three conventions,
adding convention 1's one shell entrypoint per runnable folder and convention
2's POSIX sh. It breaks neither. `local-sd-db/sd-db.sh` is that entrypoint, it is
`#!/bin/sh` with `set -e` and the `DIR="$(cd "$(dirname "$0")" && pwd)"`
resolution convention 2 asks for, and it takes the subcommands
`test|build|install|check|help` with usage on stderr and exit 1 for no
argument. Only `:4` gives, and it gives once. `_build.py` is why nothing else
does: a build backend of sixty stdlib lines, in the folder, so `pip install .`
needs no network and no setuptools — which also means `sd-db.sh test`, whose
`tests/test_installed.py` builds a wheel and installs it into a throwaway
virtual environment, runs offline.

PR 2 keeps `local-sd-db/` for the database and the library and no longer
stands up the skeleton.

`sd_db.testing`, one package, holding all of it: a fixture remote, a bare
repository with a default branch, a protection state and a pull-request
table, behind an HTTP double of exactly the GitHub calls the pack and the
runner make — open, state, `merge_commit_sha`, mergeable, merge with `sha`,
checks, protection, collaborators — recording every call and answering from
its table; a recording double per provider API the registry names; a
fixture provider whose `start` is a script the test writes; stubs for
`launchctl`, `tailscale`, `curl`, `caffeinate`, `lsof` and `local-notify`;
and a fixture home with `~/.local/share/sd/`, `~/Documents/sd-backups/` and
`~/.config/shell/env.sh` inside it.

**Why it is first, and why that is asserted and not asked.** Dozens of
criteria across items A, B and D say "a fixture repository" or "a recording
GitHub double" and until now nothing named the thing they assume. Criterion
23 does not merely require the package to exist; it requires this pull
request to be merged **before any pull request that claims a criterion
naming it**, asserted from the log. So the ordering is a test, not a
convention.

**It ships inside the library because the library is what both repositories
install.** `sd_db.testing` reaching the pack's test suite is the same
mechanism as `sd_db` reaching the pack's runtime — one installed copy at a
tag. There is no second path to build.

**Verification.** Criterion 23's `system` half only: the package imports,
and the `system` side of the grep. **Its pack half cannot close here**, and
an earlier draft claimed it. The pack reaches `sd_db` through the pack's
installer, which is item A's criterion 13 and lands in A's slice 2 — two
slices after this pull request, which the same document requires to merge
before every other. Until then the pack's virtualenv has no `sd_db` and a
test in the pack's suite importing `sd_db.testing` cannot exist. The grep
half is pack work too: `tests/test_sd_dashboard.py:328,332,388`,
`tests/test_dashboard_actions.py:292,300,305,325` and
`tests/test_sd_ledger.py:183` hold exactly the `launchctl` and `tailscale`
doubles the criterion requires return nothing, and migrating them is a
`sd-ai-command-pack` change. Both halves are hand-off 7 below, closing in
A's slice 2. What closes here is the package and the
fixture remote refusing a merge whose `sha` is not the pull request's head
and recording the refusal.

## Slice 1, PR 2 — the database and the library

**Repository:** `system` for `local-sd-db/` and `CLAUDE.md`, **and the pack
for `sd restore`**. **Touches:** `local-sd-db/`, `CLAUDE.md`, and in
`sd-ai-command-pack`, `bin/sd`'s new `restore` verb group —
`sd restore reimport <repository>` and `sd restore resume`, which criterion
1's restore clauses and `prd.md:119-120`, `:143` and `:1256` all assert
against. `system` lands first: the pack half imports `sd_db` from a copy
installed out of this checkout's tag.

**Not the installer step**, which is item A's criterion 13, as this page's
own hand-off 3 and `prd.md`'s open question 3 both say. An earlier draft
made this pull request one, on the strength of "every pull request in this
item is `system`'s" — a sentence C-90's fix over-reached into, true of the
library and false of the CLI. Item C's PR 7 builds the piece-specific half
of `reimport` against this verb group and waits on it.

**Eleven tables on day one, and no other** (`prd.md:197-211`): `repo`,
`item`, `note`, `shadow`, `assignment`, `skill_use`, `trial`, `cost`,
`provider`, `bill`, `state`. `report` and `dep` are **`item.kind` values,
not tables** (`prd.md:202`) — an earlier draft of this page listed them as
tables and dropped five real ones, which criterion 1's "and no other" plus
its grep for an insert into an unnamed table would have failed twice. Since then migrations 004, 005, 006 and 011 added five tables, so `sd_db.schema.TABLES` names sixteen (2026-09-23; see "The schema since day one" below).

Every write behind named functions. `~/.local/share/sd/sd.db` in WAL mode —
not a cache path, because caches get deleted.

`sd-db.sh backup` as its own verb, because a live WAL database is not a file a
clone can copy consistently and `local-backup-verify` samples only files
unchanged for seven days: `VACUUM INTO` a dated directory under
`~/Documents/sd-backups/`, `providers.yaml` and `commands.yaml` copied
beside it, then a restore of the file just written, `PRAGMA
integrity_check`, and a comparison against the checkpoint row the job wrote
before the snapshot. Thirty kept.

Restore fails closed. A restored database is a record, not a permission:
one `restore` row, and while it is unreconciled the runner dispatches
nothing and the palette runs nothing, with Today saying so first. Source
authority is reconciled against the checkout and never restored from the
snapshot — the marker proves the checkout migrated, not that this snapshot
holds its data.

**The pack half is the installer, and it is item A's criterion 13.**
`prd.md:1750-1755` settles it: the pack's installer provisions `sd_db` into
the pack's virtualenv from this repository's checkout, as a built copy at
the checkout's tag and never editable. One installer, one place that knows
the path. Without it PR 2 lands a library nothing installs, and the first
`sd today` after this PR fails on import. A's plan carries the criterion;
this page names the dependency so it is not discovered late.

**The invariant this PR establishes and every later one inherits.**
Criterion 2: a grep of the pack, this repository and the dashboard for
`sqlite3.connect` returns only the library. This pull request **establishes**
that invariant and does not close it. Run on 2026-09-05,
`git grep -n sqlite3.connect` over the pack returns one line —
`dashboard/store.py:89` — so at this merge there are two callers, the new
library and that one, and `dashboard/store.py` is in no Touches list here.
The second caller goes when PR 6 retires the pack's `dashboard` package, two
slices later, so criterion 2 closes there. An earlier draft claimed the
criterion whole and called this the moment "there is exactly one caller to
keep honest", which was true only of a tree with the retirement already
done.

**Criterion 18's guide edit lands here**, in the first `system` pull
request: `CLAUDE.md` named neither `docs/work` nor `sd-docs-lint` on 2026-09-05,
checked with a grep that returned nothing, and criterion 18 asks that it
name both as the place and the check. It names both since 6ea2a3c (2026-09-06).

**Verification.** Criterion 2 whole; criterion 3 whole, including the
registry refusing `author` and `reviewer` resolving to one provider, and an
`exo` entry with a URL resolving with no code change; criterion 18; and
**criterion 1 in part only** — the schema, the eleven tables and no other,
the version refusals, the WAL mode, the backup and its restore-and-compare,
and the virtualenv resolution. **Plus criterion 1's backup-failure paths,
which an earlier draft left with no pull request** (`prd.md:1228-1237`): the
backup run against an empty fixture database, one whose newest row is a
month old and one with a fresh row, all three restores passing; a truncated
copy making the count comparison fail; the database made unopenable, and
separately the backups volume filled, each asserting one email leaves
through the cron mail path and nothing is written to the database.
Criterion 14's cron-mail exception rests on these tests by name
(`prd.md:1505-1507`), so they cannot sit in PR 9's `sd-db-backup`-loaded
clause. Criterion 1's other remaining clauses are listed under "What closes
the criteria" against the pull requests that can reach them; this PR does
not claim them.

**What landed, 2026-09-06.** In `system`: `sd_db/schema/001_initial.sql`
with the eleven tables and no other; `schema.py`, whose migration list refuses
a gap, a duplicate number and a last file that disagrees with
`SCHEMA_VERSION`; `database.py`, holding both version refusals, WAL, foreign
keys and a five-second busy timeout; `migrate.py`, which is the only thing
that applies a migration and is never reached by `connect`; `writes.py`, every
write as a named function, with `transition` the only writer of `item.status`
and its `status_change` note in the same transaction; `registry.py` over
`yaml_lite.py`, a stdlib subset parser, merging `providers.yaml` with the
`provider` and `bill` rows and carrying the three refusals; `backup.py`, whose
checkpoint row is written before the snapshot and not after, because a row
written after would be in the source and not the copy and its absence would
prove nothing; `jobs/`, the runnable halves; and `sd-db.sh`'s `init`,
`migrate`, `status`, `restore` and `backup` verbs, whose failure path sends
one mail through `local-notify -c email` and touches no database. In the pack:
`bin/sd_restore.py` and `bin/sd`'s `restore` group. 147 package tests pass; the
pack's `make check` passes at 1,234 tests. Three invariants were falsified by
mutation before acceptance — a twelfth table, the checkpoint row moved after
the snapshot, and the author-equals-reviewer refusal removed — and each turned
the suite red.

**`sd restore reimport`'s per-kind half is deferred, and says so at runtime.**
The verb opens the restore, refuses on an unproven repository, a frozen bill
or a blocked assignment, and then reports that reimporting a kind's lines from
each row's `source_commit` is the migration's half and is not installed yet,
naming rerunning the sitting as what to do until it is. That is item C's PR 7,
which this page's own paragraph above already says waits on this verb group.
A silent no-op would have read as success.

## Slice 1, PR 3 — the migrations, as rehearsals only

**Repository:** `system` for the migration commands and anything reading a
source that lives here. **Touches:** `local-sd-db/`,
`local-cron-jobs/jobs/` — the nightly job entry that **invokes**
`sd shadow sync`; item C (sd:232) later removes four vault jobs from it, and two
stay (requirement 3) — and the migration sources that live in this repository.

**The command itself is item A's**, not this pull request's. An earlier
draft built `sd shadow sync` inside `local-sd-db/`, which was the one place
this item decided where a verb lives and decided it against the settled
answer above. Item A's PR 8 lists `sd shadow sync (new)` in its own Touches
and says "it is built here". This pull request lands the `watermark` state
kind the command resumes from (`prd.md:211`) and the job entry; A builds the
verb — hand-off 10.

Five sources, each a migration: one command, idempotent, reporting counts,
in the fixed order freeze, import, verify, retire. **This PR lands import
and verify and retires nothing.** Every source stays authoritative and
stays written by whatever writes it today.

| Source | Into | Counted 2026-09-05 | How counted |
|---|---|---|---|
| `index.sqlite` | `shadow` | 1,175 rows | 1,175 in `issue`, exact |
| `docs/work/*/prd.md`, every registered repository | `item` | **64 across six repositories** | enumerated from the filesystem, below |
| the decision register, open entries O27–O29 | `item` with `repo` | 3 | not checked; the register is in another repository |
| the vault's Blog Ideas and Topics | `item` of kind `idea` | 179 and 11 | not checked; the vault is outside every repository here |
| the two open GitHub issues | **`shadow`**, not `item` | 2 | not checked |

**The `docs/work` count, enumerated rather than recalled.** Most of the 64
sit in one repository the operator does not own; eight are in the pack, two
here, and the rest in three other repositories. An earlier draft of
this page said "8 pack, 4 in the register's repository" and recorded it as
re-checked exact; it was neither, and B's C-88 records why — the check
enumerated the two repositories already named instead of the filesystem the
requirement binds it to.

**The enumeration is bounded by the `repo` table, not by the disk**
(`prd.md:355-364`, criterion 6). Item D's runner clones a registered
repository's whole working tree onto the work volume, so a clone carries
its own `docs/work/*/prd.md` files; on the old wording those files were "on
the machine", had no `item` row, and failed criterion 6 for as long as the
retention held the clone. A path under the worktrees directory is not a
registered repository and is never enumerated.

**Most of that one repository's items predate the forty-five day threshold**, so
each row's idle clock is seeded from the file and not from the import.
Otherwise the first sweep after the migration offers them all at
once, all of them freshly touched by nothing but the import.

`index.sqlite` carries a second obligation that is easy to read past: the
pack's `dashboard` package holds the only collector that refreshes those
rows, so a one-time import would leave `shadow` frozen on the day of the
switch. **The collector moves into the library first, as `sd shadow sync`,
keeping the `tracker_watermark` incremental fetch, with a nightly job in
this repository running it.** That is real work and it belongs in this PR,
because PR 6's retire is gated on a sync *after* the import having brought
in a new issue.

**The sync's configuration names the repositories it fetches**, one the
operator does not own among them, since one of the shadowed issues is there
(`prd.md:393-400`). The `prd.md` locates no other, and an earlier draft of
this page placed one in the pack as though it did; the configuration is built
by reading the issues, not from that sentence. The rehearsal answers three,
every one of them filed by the operator in a repository somebody else owns,
which is why the bullet cited above now names no count and no owner
(ledger C-168). The operator's classic token carries `repo` scope, which
reaches them.

TaskNotes and the vault's Skill Proposals are explicitly not migrated.

The verify compares source with rows by identity and content, never by
count, and names every difference. The `docs/work` migration reads
committed trees and never a working copy, refusing while `git status
--porcelain` names anything under the source, and reads every branch of the
remote rather than the checkout's `HEAD` alone — an item lives on its
branch until its merge, so a branch can carry an item the default has never
seen. Two branches whose lines disagree refuse the sitting naming the item
and the branches. Each row records `source_commit`.

**The `stage` vocabulary is checked here, against item C's manifest, which
lives in another repository.** `writing-pack/sd-plugin.json` is on disk
today, so the test can read it, but this pull request's repository does not
hold it and the hand-off list did not record the dependency. It does now,
as hand-off 9.
Criterion 5's tail (`prd.md:1250-1255`) requires the `stage` set comparison,
a refusal naming an unmapped stage word, and the mapping table naming every
transition target in the writing manifest, asserted by reading the manifest.
The vault import lands `idea` rows carrying `stage` verbatim, so the check
belongs with the import that writes them and not with a later retire.

**Why retire is not here.** Criterion 23's last clause: the retire step of
each source runs in a pull request **after** the one that lands its writer,
asserted from the log. `docs/work` retires in item A's slice 2, in the run
that lands `sd-ship`'s tiered path. Splitting import from retire is what
makes the freeze minutes rather than a slice.

**Verification.** Criterion 6 whole; criterion 5's import half — each
migration runs twice with the same counts, and the `stage` and manifest
clauses above; and criterion 23's rehearsal clauses: the import landing
rows and retiring nothing against a fixture whose `sd-ship` still writes the
frontmatter line, a status change through the old command read back by the
next import, and a seeded verify difference lifting the freeze with the
source unchanged. **Criterion 7's import clauses**: a `done` item landing as
a `done` row with its line untouched (`prd.md:1278-1280`), an uncommitted
`prd.md` making the sitting refuse naming it (`prd.md:1300-1301`), and the
branch-only item landing from its branch with `source_commit` while a
divergent one refuses (`prd.md:1302-1308`) — the criterion's other eighteen
clauses belong to five other pull requests, split in the closure table.
**Criterion 5's four retirement-refusal tests are not
here** — a writer still loaded, a source row changed after the import, no
snapshot, and a snapshot predating the import — because this PR retires
nothing to refuse. They land with the first real retire, PR 6.

**What landed, 2026-09-06.** In `system`: migration
`sd_db/schema/002_source_commit.sql`, which adds `item.source_commit` and
takes `SCHEMA_VERSION` to 2 -- the column requirement 2 needs and 001 did not
have, added rather than edited into a migration that is already committed.
`repos.py`, which is how the `repo` table fills: `sd-db.sh repo add` for one
checkout and `repo seed` deriving the fleet from
`local-repo-sync/repos.personal.conf`, registering only what this machine has
actually cloned, and refusing a path under the worktrees directory.
`shadow_sync.py`, the collector moved out of the pack's `dashboard` package,
keeping the four separate buckets, the hour of overlap and the rule that a
watermark moves only on its own collect's success, now writing `shadow` rows
and a `watermark` row in `state`. `sources/`, the five migrations behind one
protocol -- freeze, import, verify, and no retire -- with `Counts` reporting
inserted, updated and unchanged separately so a second run is assertable, and
a verify that compares identity and content and names every difference.
`writes.upsert_item`, which lands a source's row once and still routes the
status through `transition`, so an import that moves a status writes its
`status_change` note like any other change. `sd-db.sh`'s `repo`, `import` and
`verify` verbs. In `local-cron-jobs/jobs/`: `shadow-sync-nightly.job`, which
invokes `sd shadow sync` at 02:20; the verb is item A's PR 8 and the
`personal.cron` entry is PR 9, so nothing installs a job whose command does
not exist yet. 237 package tests pass.

**Four things the real sources taught, none of which the plan had.**
Enumerating every `prd.md` under `docs/work` found 1,258 across eleven
repositories, of which 486 were the pack's archive alone; the requirement's
glob has one star and criterion 7 says "outside the archive", and matching
`docs/work/<slug>/prd.md` exactly returns **64 across six repositories**,
which is the number requirement 2 states, enumerated rather than recalled.
A branch already merged into the default still carries the file at whatever
it said the day it landed, and reading that as a competing claim refused the
sitting over a disagreement a merge had settled -- so a candidate whose last
commit touching the file is an ancestor of the default is superseded, and two
of the three real disagreements were exactly that. The register hard-wraps at
eighty columns and two of its three open entries carry a heading spanning two
lines, which a line-anchored pattern read as absent; the migration refuses on
a header that names an entry it cannot find, which is how that was caught
rather than silently importing one of three. And GitHub answers **three**
open issues, not two, one in a repository the operator does not own,
`mProjectsCode/obsidian-meta-bind-plugin` and `mindfold-ai/Trellis` -- which
is the requirement's own instruction working: the configuration is built by
reading the issues, not from the sentence that located one of them.

**A fifth thing, found by acting on the fourth.** The sitting refused on two
`prd.md` files in the pack, each with a branch that touched the file and said
something other than `done`, and the refusal named branches that no longer
existed. `git ls-remote` answered four heads; the checkout's
`refs/remotes/origin/**` held eleven, seven of them deleted on the server
weeks earlier. Criterion 7 says the migration reads every branch **of the
remote**, and the reader reads a cached copy of the remote instead, which is
not the same claim: a checkout that has not fetched sees deleted branches as
live and cannot see new ones at all. Both refusals dissolved on
`git remote prune origin`, with no branch merged and none deleted. The reader
is not yet fixed -- see the ledger's C-170 for the shape the fix should take,
which is a refusal naming the drift rather than a fetch, because a migration
that mutates the checkout it reads is a worse thing than one that stops.

**What the sitting does on real data.** All five sources rehearsed clean and
idempotent on 2026-09-06, each with a second run reporting the same total and
zero inserted: `docs/work` **64 items across six repositories**, verify clean
on a source hash of `d6ab52f038a0`; `index.sqlite` 1,175 rows; the vault 192
notes; the register 3 entries; GitHub 3 issues. Five items live on exactly one
branch each and are reported as such rather than silently taken from the
default, which is the same machinery the false refusals came through, working
as specified once the refs were true.

## Slice 2 is item A's

The registry reader, `sd-ship`'s tiered path, the protection and the
trailers, the installer step of PR 2's pack half, and the `docs/work`
retire with them. Nothing else in this item lands in that slice; it is named
here because B's slice 3 cannot open until it is on `main`.

## Slice 3, PR 4 — the dashboard's first three screens, read-only

**Repository:** `system` **and the pack**. **Touches:**
`local-project-dashboard/`, and in `sd-ai-command-pack`, `bin/sd`'s new
`today` verb group. Criterion 4 requires `sd today` and the Today screen to
list the same item ids in the same order from the same library query; an
earlier draft claimed the criterion whole while no pull request built the
verb. `system` lands first.

Today, Backlog and Item, each reading through the library, with no write
control anywhere on the page. The Backlog's three views of one row set,
list, board and Eisenhower matrix, with the age histogram above them.
Today's runner board, timeline and provider scorecard, all rendering rows
the runner does not yet write. The front door is
`https://<mac>.<tailnet>.ts.net` through Tailscale Serve, the operator's
login only.

**The shared list component lands here** — the filter and the selection at
`prd.md:859-873`, the paging at `prd.md:892-894`, which is outside the range
an earlier draft cited for all three: one filter,
selection and paging component every list uses. It is the thing three later
screens assume exists, and building it inside the first screen that needs it
is what stops three divergent copies.

**The escaping rules land with the first rendering, not after it.**
Everything rendered that the dashboard did not write is escaped text,
Markdown sanitized, a Content-Security-Policy with no inline script behind
both, and no page anywhere may frame the dashboard. A read-only dashboard
is exactly when those are cheap to establish and impossible to forget.

**No hover-only affordance anywhere**, and no horizontal scroll
(`prd.md:734`, `prd.md:852`). Criterion 15's "inputs visible on hover or
in a detail view" is satisfied by the detail view, never by the hover; the
prd offers both and only one of them is allowed.

**Verification.** Criterion 4 whole — `sd today` and the Today screen list
the same item ids in the same order, the same library query, proven rather
than intended. Criterion 12's Today, Backlog and Item clauses only.
**Criterion 7's clause 7.16** (`prd.md:1315-1319`): three status changes
write three `status_change` notes in order, the item screen lists them, and a
grep of the repository for a second `UPDATE item SET status` returns nothing.
It needs the library rows and this screen and nothing from item A, which the
earlier closure note obscured.

**Historical finding, resolved by the operator on 2026-09-07.** Criterion
12 originally required that "the repository contains no `package.json`,
`node_modules`, or bundler config". At commit
`c090f8b715b60760790b83d5372e7c95d0b1751f`, these were PRD lines 1343–1346;
"No build step:" opened on line 1343. Two earlier drafts cited 1312 and
1313 by arithmetic; the second incorrectly called its anchor re-derived.
The tracked `mezmo-webhook/package.json` made that repository-wide clause
false. The original choice was to narrow the clause to the dashboard or
remove `mezmo-webhook/`; C-176 in the PRD Log preserves that finding.
The operator chose the dashboard scope and retained the working Node
service. The current clause (`prd.md:1360-1364`) names only
`local-project-dashboard/`; "No build step:" now opens on `prd.md:1360`.
This records the settled decision and leaves its historical evidence intact.

## Slice 3, PR 5 — the dashboard's remaining screens and the charts

**Repository:** `system` **and the pack**. **Touches:**
`local-project-dashboard/`, and in `sd-ai-command-pack`, `bin/sd`'s new
`exec-log` verb group, which criterion 12 asserts lists the same three
entries as the screen. `system` lands first.

Skills and System, still read-only, with System's Dependencies sub-screen.
The `prd.md` has five sections, and Dependencies is under System
(`prd.md:599`); an earlier draft made it a sixth. The Dependencies screen's
held-count and missed-run tiles. The command log and `sd exec-log`
(`prd.md:586-598`), which is a reading surface and belongs with the other
reading surfaces. Five server-rendered SVG charts (`prd.md:841-848`), the
operator's decision after round thirty-two, vendored nothing.

**Verification.** Criterion 12's Skills and System clauses, Dependencies among System's,
and the chart rendering. Still no write control, so still no clause of
criterion 12 that performs a write.

## Slice 3, PR 6 — reports on rows, and `index.sqlite` retires

**Repository:** `system` **and the pack**. **Touches:** `local-sd-db/`,
`local-project-dashboard/`, the report senders that stop emailing, and in
`sd-ai-command-pack`, `bin/sd`'s new `deps` verb group, which criterion 21
asserts lists the same rows as the page — **plus every importer of the
`dashboard` package this pull request retires**, which an earlier Touches
list reached none of. Run on 2026-09-05,
`git grep -ln 'from dashboard|import dashboard' -- bin tests` returns
sixteen files: `bin/sd-dashboard:34` (`collect, server, store`),
`bin/sd-status:82` (`store`), `bin/sd-trackers:35` (`github, jira`),
`bin/sd:2717` (`dashboard.collect.discover_checkouts`), and twelve test
modules — `tests/test_dashboard_{actions,markup,now,plugins,sessions,skills,work}.py`,
`tests/test_sd_dashboard.py`, `tests/test_sd_dashboard_index.py`,
`tests/test_sd_ledger.py`, `tests/test_sd_status.py`,
`tests/test_sd_trackers.py`. All sixteen break at this merge and all sixteen
are Touches. `bin/sd-status` this pull request already changes; `bin/sd`'s
`discover_checkouts` needs a home.

**`bin/sd-trackers` is the one that needs a decision, and it is not about
Jira.** An earlier note here called `dashboard/jira.py` "Jira collection that
`shadow` does not replace" and asked where the Jira path goes. Checked on
2026-09-06: **nowhere, because nothing collects Jira today.**
`select tracker,count(*) from issue group by tracker` over
`~/.cache/sd-ai-command-pack/index.sqlite` returns `github|1175` and no Jira
row; `tracker_watermark` holds one line, GitHub's. `jira.settings` needs
`JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN`, only the token is set,
and the dashboard agent's `EnvironmentVariables` are `PATH`,
`SD_DASHBOARD_TAILNET_BIND` and `SD_REPO_ROOT` — no Jira at all. So
`jira.missing()` returns two names on every run and `jira.collect` has never
produced a row. `dashboard/jira.py` retires with the package and nothing is
lost.

What does break is `bin/sd-trackers`, which is **not a collector**: its own
docstring says it "writes no file, touches no repository, and does not consult
the issue index". It is the resolver behind `sd-plan --from gh:o/r#N|jira:KEY`,
named at `skills/sd-plan/SKILL.md`, and it imports `dashboard.github` for the
half that works. Retire the package and that seeding path breaks. Nothing runs
`sd-trackers` on a schedule — no cron job, no launch agent — so this is a
hand-invoked path with a skill pointing at it. This pull request keeps the
GitHub reference resolver alive across the retirement, either moved beside
`bin/sd-trackers` or kept as the one surviving module; the Jira half of
`resolve_jira` already exits 2, "a tracker that is not configured", so it goes
with `jira.py` and `sd-trackers` reports the tracker as unconfigured the way it
does today. `system` lands first.

Every report that reaches the operator by email today becomes a `report`
row; an `attention` row still emails and now also pushes, with each
delivery marked on the row so a send that died is retried by the next job
or the dashboard's tick. `dependabot-daily` writes `dep` rows and keeps its
own merger for now — the cut to enqueue-only is one commit in slice 5,
after the runner's merge check passes, and that commit is **item D's**.

`index.sqlite` retires here: `sd-status` switches to `shadow`, and the
pack's `dashboard` package and `bin/sd-dashboard` are retired with it. The
gate is not the calendar — it is a `sd shadow sync` run **after** the
import having brought in a new issue and a changed state, asserted by a
test. This is the first retire in the item, and it obeys the same rule PR 3
deferred to: it lands in a pull request after the one that landed its
replacement reader.

**Criterion 5's four retirement-refusal tests land here**, because this is
the first retire there is: a writer still loaded, a source row changed after
the import, no snapshot, and a snapshot predating the import.

The decision register retires in **A's slice 2**, alongside
`docs/work`, which is what B's landing order means by "`docs/work` and the
register retire here". The register keeps its decisions and links to the
rows rather than being emptied, and it has one writer — the operator's
hand, who is running the sitting — which is why it needs no freeze
machinery of its own. Nothing in this repository schedules it; it is named
here so the fifth source is not lost between two items.

**Verification.** Criterion 5's retirement half; criterion 14's `report`
row half. **Criterion 21 is not closed here** — see the hand-offs below.

## Slice 4, PR 7 — the dashboard's writes, the palette and `exec`

**Repository:** `system`. **Touches:** `local-project-dashboard/`,
`local-sd-db/` for the `exec` and `commands.yaml` reads.

Every action a button with its command beside it. Writes are same-origin
with a login-bound token, and every write updates in place. The palette runs
allow-listed argument vectors from `commands.yaml`, **never a shell** — that
is the whole security posture of the palette and it is a construction, not a
check, asserted by a grep for `sh -c`, `shell=True` and `os.system`
returning nothing.

The three `scope` values (`prd.md:784-802`): `worktree` and `supervisor`
rows are **item D's runner** to execute and are not dispatched here;
`control` scope is this dashboard's own, and the kill, clear and
`runner restart` via `launchctl kickstart -k` entries are control rows. An
`exec` note is written before the command starts, never after.

The Dependencies screen's six bulk actions, `protect main`, and bulk merge
from a selection. The run dialog's `budget_usd`. The iPad is the reference
device; the iPhone gets Today, the Backlog list, the Item screen and the
palette, and the rest is postponed rather than degraded.

An assignment created here waits `queued` until slice 5, which Today says
in those words, and the library's `cancel` takes it back with no runner in
existence.

**This pull request gains a pack half, and that makes eight paired slices.**
Criterion 7 asserts a status refusal from five surfaces — "the board's drag,
a bulk action, the item screen, `sd status` and the palette's status entry"
(`prd.md:1319-1322`) — and a cancel "from the item screen and from
`sd assign cancel`" (`prd.md:1334-1336`). `bin/sd` carries four verb groups
today (`plugin`, `store`, `config`, `sweep`) and the seven pack halves
scheduled above add `restore`, `today`, `exec-log`, `deps`, `usage`, `note`
and `skill`. Neither `status` nor `assign` is among them, and `bin/sd-status`
is not the fallback: its own docstring reads "Read-only status for the
repository you are standing in." So (as planned here; superseded below) `bin/sd`'s `status` and `assign` verb
groups land here, `system` first, calling the same library functions the
screens call — which is what makes the criterion's "each is refused" one
refusal and not five. Round four settled this defect for six criteria and
did not re-derive it for criterion 7. Superseded 2026-09-16 by the owner's decision (a): no `status` or `assign` group lands; the existing `sd task status`, `sd run` and `sd assignments cancel` / `sd runner cancel` are the pack half (the Log line of that date).

**Verification.** Criterion 13's first part only — status change, assign,
`merge_policy` round-tripping, and `protect main`: the four settings written
through the recording API with the row showing protected after on a fixture
repository the operator owns, and on one they do not own the page offering
no such action and the request refused (`prd.md:1475-1486`). **Not
criterion 13 whole**, which an
earlier draft claimed. Its promote and demote clauses need the Skills
screen's writes, which PR 5 left read-only and no pull request has yet; its
registry clauses — enable, disable, reorder and cap round-tripping with the
registry file's hash unchanged — are the Providers-and-bills screen's
writes, in no pull request body; and its last clause asserts the next
`sd-review` resolution in the **pack** reflects the change, which no
`system`-only pull request can exercise. Those three are hand-off 8.
Criterion 12's write, execution, `commands.yaml`, bulk-action and
`budget_usd` clauses. **Criterion 7's surface clauses**: `done` on an
unmerged item refused from every surface that carries a status write (the item screen, `sd task status` and the palette's status entry; the board's drag and a bulk action carry none), each refusal the sentence `source:local-sd-db/sd_db/workflow.py::change_status` raises, "work completion requires verified delivery
or cancellation evidence" (7.17; amended 2026-09-17, approved by the owner 2026-09-17 (note 2706), from "naming the merge and offering cancel"); a status write on a `running` assignment refused
from the same three, naming `cancel` or the control entry (7.19); and, with no
runner process in existence, an assignment created, its row `queued`, a
status write refused, and the cancel landing `cancelled`, the library's terminal status, with `cancelled by
<who>`, the caller's value, from the item screen and from `sd assignments cancel` / `sd runner cancel` (7.20; amended 2026-09-17, approved by the owner 2026-09-17 (note 2706), from `blocked` with `cancelled by operator` and, since 2026-09-16, from `sd assign cancel`, with `sd task status` for `sd status` and `sd run` for the assign). All three
need this pull request's two halves and nothing from item A.

## Slice 4, PR 8 — cost and the four numbers

**Repository:** `system` **and the pack**. **Touches:** `local-sd-db/`,
`local-project-dashboard/`, the provider registry reader,
`local-agent-meter/agent-meter.py`, and in `sd-ai-command-pack`, `bin/sd`'s
new `usage` verb group and `bin/sd-review`'s provider-call path. `system`
lands first.

**Three of those need saying, because an earlier draft named only the first
two.** The `usage` group **reports and mutates**: `prd.md:994-997` releases a
`reserved` row whose owner pid is dead "by the next reservation on any bill
and by `sd usage`", and `prd.md:1016-1017` settles a dead-owner `sending` row
to `bound` the same way. Clauses 15.19 and 15.20 run `sd usage` between two
assertions and require the ledger to have changed. The sweep is a library
function in the `system` half; the verb calls it, so the pack half is thin
and lands second like every other. — `local-agent-meter/agent-meter.py`
writes the `meter` cost row on its four-hourly schedule
(`local-cron-jobs/jobs/agent-meter.job`, `0 */4 * * *`), which clause 15.1
asserts "on the meter's schedule" and not from a fixture. It **dual-writes**:
the library row and the existing JSONL at
`writing-pack/content/<piece>/data/meter.jsonl`, because
`prd.md:909-911` keeps that file "until item C decides otherwise" and item C
has not. — `bin/sd-review` calls provider CLIs by subprocess today
(`bin/sd-review:18`); clause 15.6 charges a review pass inside an assignment
once and in both totals, and clause 15.7 forbids `sd-review` inserting the
row itself, so its provider calls route through the library's calling
function. Item A's PR 6 touches the same file to delete its `BACKENDS` table
and verifies only A's criteria; this is a second, separate change to it, and
the two pull requests must not race — A's PR 6 lands in A's slice, before
this one, by the ordering above.

One `cost` row per provider call, written by one library function.
Reservations are rows too, settled by call id, with the
`reserved` → `sending` → `run`/`bound` claim transition and the zero-retry
rule (`prd.md:984-994`), and orphan release by dead pid or timeout
(`prd.md:994-998`). Bills are personal subscriptions, prepaid balances, a
plan with a meter, and one capped provider bill. One exposure function shared by
a bill's cap and a row's budget (`prd.md:976-979`). The registry reader
refuses a `start` entry whose bill carries a cap (`prd.md:1023-1025`).

Today shows spent this month, reserved, room, and the projection's crossing
day, with the month-boundary double-count and the operator's correction on
the usage screen (`prd.md:951-964`). A batch shows its cost bound before it
runs. The cost burn chart and cap projection land here rather than with the
other charts, because they read rows that do not exist until this PR.

**The four numbers get a detail view, not a hover.** `prd.md:1591-1592` wants
the weekly numbers' inputs "visible on hover or in a detail view"; PR 4
decided which — the detail view, since `prd.md:734` forbids a hover-only
affordance anywhere — and then neither PR 4's Verification nor this one
claimed it. It is built here, with the tiles.

**A `start` session's charge is written by item D's runner, not here.**
Open question 8, settled 2026-09-05: "the usage read" is the total a
`start` session reports at its exit, one `run` row per session with the
assignment and the pass on it. This pull request builds the schema, the
`url` entry's reserve-claim-settle path and the sweep; the runner writes
the `start` row, because it is what execs the entry's `start` line and the
only thing that knows the assignment. Clauses 15.13 and 15.16 are asserted
in item D's PR 7 against a fixture provider, and this pull request asserts
that such a row sums into cost per shipped item and the four numbers like
any other. Hand-off 15.

**"Shipped is not done" is arithmetic here and `shipped_at` elsewhere.**
Clause 15.25 asserts the four numbers count one shipped item in a week
holding a declined idea, a report and a merged work item, and that
`shipped_at` is set only on the merged one and does not move on a second
merge. `prd.md:923-925` names three writers of that field: `sd-ship` on a
merge, the send-box on a `ready_to_send` item, and the import for a piece
already published. The arithmetic over rows is this pull request's; the
merge writer is item A's `sd-ship` (hand-off 12) and the send-box is item
C's; the import's is PR 3's. Criterion 7's clause 7.21 asserts the same
field from the other side and lands with A's PR 6.

**Verification.** Criterion 15 **except three clauses**, which is not the
same as whole and an earlier draft said whole. Not here: clause 15.15, the
dashboard's refusal to raise a cap on a bill holding a `start` entry
(`prd.md:1549-1550`) — the cap control is PR 12's, and PR 12 lands after this
by the ordering, so there is nothing to refuse from at this merge; clause
15.25's `shipped_at` half, split above; and clause 15.7's runner grep, which
would pass against a directory that does not exist (hand-off 13). Everything
else, including 15.1 on the meter's own schedule, 15.6's review pass, and
15.19 and 15.20's `sd usage` sweep. Criterion 12's cost-chart clauses.
**Criterion 16 is not closed here** — see the hand-offs below, and the
requirement 7 pull request that now carries it.

## Slice 4, PR 9 — the backbone

**Repository:** `system`. **Touches:**
`local-machine-setup/profiles/personal.agent`,
`local-machine-setup/profiles/personal.cron`,
`local-machine-setup/machine-setup.sh`, `local-cron-jobs/jobs/`,
`local-backup-verify/`, `sd-db-backup`, and
`local-machine-setup/launchagents/local.system-tools.sd-dashboard.plist`.
`local-health-check/` was listed
by an earlier draft with no work behind it; `prd.md:1089-1094` names it as
existing furniture and criterion 22 asks nothing of it.

**The `local.system-tools.sd-dashboard` agent execs a file PR 6 deletes.** The tracked
plist's `ProgramArguments` is
`~/repos/platypeeps/sd-ai-command-pack/bin/sd-dashboard serve
--port 8767`, read on 2026-09-05, and `personal.agent` lists the label on
purpose. After PR 6 that agent starts a removed binary at every login, and
nothing launchd starts serves `local-project-dashboard/`, so criterion 12's
front door — loopback-bound, fronted by `tailscale serve`, `doctor`-checked
to answer over its HTTPS name — has no process behind it. This pull request
rewrites the plist to exec the new dashboard's server on the loopback port
criterion 12 names, keeping the label so `machine-setup.sh status` still
reconciles it. This is the `com.platypeeps.sdw-meter` lesson below, in the
same file, one release later.

`personal.agent` gains `local.system-tools.sd-runner`; `local.system-tools.sd-dashboard` stays in
the list and is repointed rather than removed —
the setup stages gain `local-sd-db/sd-db.sh init` and the `tailscale serve`
route; `machine-setup.sh doctor` gains four checks — the database opens and
passes `PRAGMA integrity_check`, the runner is loaded with a fresh
heartbeat, the dashboard answers over its HTTPS name, the serve route
exists — and reports each virtualenv's installed `sd_db` version
(`prd.md:173-175`).

`backup-verify.conf` needs no new line, and an earlier draft of this page
said the opposite. The file already carries
`~/Documents|/Volumes/ccc/Users/<login>/Documents`, and
`~/Documents/sd-backups/` sits under it, which is precisely why
`prd.md:1129-1135` chose that path over `~/.local/share/sd/`: "inside a pair
`backup-verify.conf` already has … with no new configuration". `design.md`
says the same. Criterion 22 asks for an assertion and not an edit
(`prd.md:1679-1680`): a test asserts the backup directory is under a source
path in the file as it stands. The earlier draft told the implementer the
reverse of the prd's own reason and scheduled an edit nothing needs.

One retention table read by one nightly prune inside `sd-db-backup`,
**after** the backup passed and never before, refusing to run when it did
not: `cost` rows never, `exec` output files ninety days and the `exec` note
never, backups thirty files, no request log (none is written), `heartbeat` rows
one. A kept worktree is never pruned.

The six vault jobs do **not** go in this PR — amended 2026-09-12 by the
operator's decision, requirement 3. `vault-cleanup.job` and `vault-map.job`
are maintenance and stay for good. `obsidian-review-daily.job`,
`blog-idea-accept.job`, `tips-accept.job` and `tips-weekly.job` stay loaded
until item C (sd:232) lands what replaces them, and their removal is item
C's change, with criterion 8 asserted there. All six are present in
`local-cron-jobs/jobs/` and listed in `personal.cron` today, checked.
`obsidian-tasks-nightly` stays too, by the operator's standing decision —
the vault keeps its tasks until the new system has proven itself, and the
operator removes them explicitly then.

**When item C removes the four, `personal.cron` is edited in the same
commit as `local-cron-jobs/jobs/`.** All four are listed in that profile
today, checked by exact-match grep.
Removing the `.job` files alone passes criterion 8 and then leaves
`machine-setup.sh status` reporting four drift items, with the next
`setup personal --apply` reinstalling four jobs whose scripts are gone. The
two jobs this item adds — the nightly `sd shadow sync` from PR 3 and
`sd-db-backup` from PR 2 — need `personal.cron` entries for the opposite
reason: without them neither is ever installed, criterion 1's "`sd-db-backup`
is loaded" cannot pass, and `shadow` freezes on the day of the switch.

**The entry in `personal.agent` is not a detail.** `com.platypeeps.sdw-meter` was
in `personal.agent` and not installed, the commit landed and the machine
never got it, and `machine-setup-drift` reported five items at 03:30 and
not that one. `CLAUDE.md` keeps that as a standing lesson. This PR is done
when `machine-setup.sh status` reports no drift on the new pieces, not
when the profile is edited. The `personal.cron` half above is that same
lesson one file over, with the sign flipped.

**Verification.** Criterion 8, which names two enumerations —
the four vault process jobs absent from `local-cron-jobs/jobs/` and from
`~/Library/LaunchAgents` — **plus a third this pull request adds**: absent
from `personal.cron` as well. The criterion does not ask for the third; the
paragraph above shows why removing the `.job` files alone leaves four drift
items, so the plan is deliberately stronger than the criterion here; criterion 22 whole (`prd.md:1658-1681`), which is nine assertions and not
the one an earlier draft reduced it to: `personal.agent` naming
`local.system-tools.sd-runner`; `machine-setup.sh setup personal` in dry run listing
four actions and `status` reporting four drift items against a fixture
home; `doctor` against a corrupted database, an unloaded runner, a stale
heartbeat and a missing route, naming each with stubbed `launchctl`,
`tailscale` and `curl`; `doctor` naming an enabled entry whose variable is
unset and printing no value; the prune over rows past every retention age
asserting the removed counts, the `report` row carrying them, and a
fifteen-day kept worktree still present and counted stale; the
failed-backup test asserting the prune refuses and removes nothing; an
`exec` note past ninety days surviving marked `output expired` with its
file gone; and a fourteen-month `cost` row on a `blocked` assignment
surviving with the budget still refused and the item total unchanged. And
criterion 1's `sd-db-backup`-loaded, `backup-verify.conf` and
`doctor`-version clauses.

**Landed, 2026-09-12, on `feat/backbone-doctor`, in two parts.** Already on
`main` from #227 before this branch: `personal.agent` naming
`local.system-tools.sd-runner`, the tracked `local.system-tools.sd-runner.plist`, and
`personal.cron` listing `sd-db-backup` and `shadow-sync-nightly` with both
job files in `local-cron-jobs/jobs/`. This branch: the tracked
`local.system-tools.sd-dashboard.plist` repointed to
`local-project-dashboard/dashboard.sh serve --port 8767` as the exact bytes
the dashboard's installer writes, so `status` reads `ok` after an install
instead of `DIFFERS`; an `sd` stage in `machine-setup.sh` that runs
`local-sd-db/sd-db.sh init` and adds the `tailscale serve` route, each gap a
`MISSING` line; `doctor`'s four checks — `PRAGMA integrity_check` over the
database, `launchctl` for the runner plus the heartbeat row it writes (fresh
is `heartbeat_state`'s rule: within three intervals and healthy), the
`tailscale serve` route to `127.0.0.1:8767`, and `curl` of `/health` over
the route's HTTPS name — with the installed-`sd_db` report beside them;
`doctor sd` to run those alone; and `local-machine-setup/tests/` bound into
`system-native`, holding the profile, dry-run, drift, stubbed-doctor and
`backup-verify.conf` assertions. **Still open:** the four vault process jobs
(item C's; `vault-cleanup` and `vault-map` stay for good), by the operator's
call (decided 2026-09-12: the middle path, see requirement 3's amendment);
the retention table and the nightly row prune, since `sd-db-backup` runs `backup --keep all` under the September 9 amendment and
no prune of `cost`, `exec` or `heartbeat` rows exists (no request log); the
`doctor` check that names an enabled entry whose variable is unset (landed
2026-09-12, two paragraphs down); and the tracked `local.system-tools.sd-runner.plist`,
which `status` reports `DIFFERS` against the installed copy and which item
D's PR 8 owns.

**The prune landed, 2026-09-12, on `feat/sd-db-retention-prune`.**
`local-sd-db/sd_db/retention.py` is the retention table and the prune;
`sd_db/jobs/backup.py` calls it after `backup.run` returned, so a backup
that raised never reaches it, and the prune verifies the snapshot again and
finds its checkpoint row before removing anything (`backup.passed`). `exec`
output files past ninety days go and the note's body carries
`output_expired`; `read_execution` and `executions` report it, the
dashboard's history and output reader say so, and the backup's evidence
capture and restore exempt a marked note from "missing its output
evidence" -- without that the first night after a prune would have failed
the backup. `heartbeat` rows are cut to one per key in code, not by a
migration, since the live database holds the rows a unique index would
collide with. `cost` rows are never touched. One `report` item per backup
run id carries the counts under `report.removed`, and the verb's output
line prints them. `local-sd-db/tests/test_retention.py` binds criterion
22's five retention clauses. Two things it does not do, on purpose: there
is no request log to age -- the dashboard silences its HTTP log and writes
no request rows, and the prd has since dropped the rule; and the `budget spent`
refusal the cost test is meant to call does not exist yet (requirement 6's
reservation), so the test asserts the ledger that refusal will read -- the
assignment's summed rows against `budget_usd` -- unchanged across the
prune. The job's `--keep all` is untouched.

**The unset-variable check landed, 2026-09-12, on
`feat/doctor-names-unset-provider-keys`.** `doctor_sd_provider_keys` in
`local-machine-setup/machine-setup.sh` asks the runner's own interpreter
(`SD_RUNNER_PYTHON`, the installed plist's value, or the pack's venv — the
same precedence as `runner.sh`) for the merged registry through
`sd_db.registry.read`, so an entry the dashboard switched off is skipped and
the file alone is never trusted for `enabled`; then, in a subshell, sources
`~/.config/shell/env.sh` the way `runner.sh serve` does and tests each
enabled entry's `env:` names. One `FAIL … MISSING` line per unset name, the
entry and the variable named and no value printed; a missing registry is a
`MISSING` failure; a missing runner interpreter is a `--` line, as the
version report already treats it. Five cases in
`local-machine-setup/tests/test_backbone.py` — the stub `python` now answers
the version report's question with its canned line and every other question
with this checkout's library — including criterion 22's: `FIXTURE_KEY_ONE`
empty in the fixture's `env.sh` is named, `FIXTURE_KEY_TWO` on the disabled
entry is not, and `hunter2-fixture-value` set in that file or exported
around `doctor` satisfies the check and never appears in the output. A
fifth switches the entry off through `provider_controls.configure` with the
file still saying enabled, which passes only if `doctor` reads the merged
view. Found on the way: the live `provider` table on `sol` holds no rows
(`init` reports "will seed on first read", and no read seeds — only the
dashboard's `configure` does), so the file's `enabled` is what decides
there until the providers screen writes a row. Closed (sd:234): `registry.read` now seeds through `ensure_seeded` on the first read over a writable connection, and `init` says so instead of "on first read".

## Order and dependency

PR 1 before every other pull request in every item, asserted from the log.
Then 2 and 3. Then item A's slice. Then 4, 5 and 6. Then 7, 8 and 9. Then
10, 11 and 12 — 10 and 12 after 7, since both write on screens PR 4, PR 5
and PR 7 build, and 11 in any order after that, since `local-herdr/` was new (it landed as #279)
and collides with nothing. Then item D. An earlier draft's ordering stopped
at 9, having added three pull requests and not returned to this list.

**Eight of the twelve slices span two repositories**, and in every one
`system` lands first: PR 2, PR 4, PR 5, PR 6, PR 7, PR 8, PR 10 and PR 12
each have a pack half that adds an `sd` verb group calling `sd_db`. PR 7's is
the eighth, added when criterion 7's five refusal surfaces were enumerated
and two of them turned out to be verbs nothing built. The pack
imports `sd_db` from a copy installed out of this checkout, so a pack pull
request that merged first would install from a tag that does not exist.
This is item D's rule inverted, and deliberately: D's pack halves land first
because there the runner is the caller and the verb is what it calls.

Two earlier drafts got this wrong in opposite directions. The first put the
library in the pack, which C-90 reversed. The second read C-90 as putting
*everything* in `system`, which left six criteria asserting against verbs no
pull request built — round four's finding, settled above. C-90's actual
holding is narrower than either: the library is `system`'s and the installer
is item A's.

## Hand-offs that leave this item

These have to be watched rather than assumed. An earlier draft of this page
said there were two, then six, then nine; there are ten, the tenth added
when the `sd` verb question was settled. Three were cited by round
two's fixes — at the harness pull request, at the migration pull request and
at PR 7 — and never written down, which is the same defect one level down:
the fix named a hand-off and did not add it.

1. **`docs/work` retires in A's slice 2**, not in any pull request here. If
   A's slice slips, PR 3's rows stay rehearsals and nothing in B is
   blocked, which is the property the split was for.
2. **The vault notes** are imported by PR 3 and their **authority** switches
   in this item's sitting, but the notes themselves are stripped and their
   views, routine and queues removed under **item C**, whose landing order
   says it runs after B's sitting for the vault kinds and never before,
   since the dropdown is the vault's writer until then.
3. **The pack's installer** provisions the virtualenv and is **item A's
   criterion 13** (`prd.md:1750-1755`). PR 2's library is unreachable until
   it lands.
4. **Criterion 19** needs **item D**. `prd.md:1616-1617` has the *runner*
   open one pull request in a fixture pack changing only the named files.
   PR 7 lands the `skill-review` row; the runner half is D's slice 5.
   **D's plan does not schedule this half today**: `git grep -n
   "skill-review\|dependabot\|enqueue"` across
   `system/docs/work/archive/2026-09/2026-09-05-the-runner-works-the-queue/` returns
   nothing, and D's PR 0 through PR 8 are `local-sd-runner/`, `sd
   worktree`, `sd run`, `local-health-check` and `sd-db-backup`. Until D
   adds it, criterion 19's runner half closes on neither item.
5. **Criterion 16** needs **item A**. `prd.md:1597-1599` requires three
   followups to appear in the context the `SessionStart` hook injects, and
   `prd.md:1064` says item A owns that hook. **PR 10** writes the rows and
   the eight-kilobyte brief bound; A's hook is what injects them. An earlier
   draft said PR 8 here and was corrected in PR 8's own Verification, in PR
   10's body and in closure row 16, but not in this list — the fix landing
   everywhere the finding pointed and not where the finding's own text still
   stood.
6. **Criterion 21** needs **item D** and PR 7. `prd.md:1629-1632` requires
   the post-cutover half — the runner merged it, the job's own merger gone
   from its script — which is D's slice 5, and `prd.md:1634-1640` requires
   bulk merge from a dashboard selection, which is PR 7. PR 6 closes only
   the `dep`-rows half. **D's plan does not schedule the cutover commit
   either** — the same grep that fails for hand-off 4 fails for
   `dependabot` and `enqueue`, and this item's PR 6 already says the cut
   "is **item D's**". Criterion 21's cutover half currently closes on no
   item.

**Criterion 14 additionally waits on a person.** `prd.md:1487-1489` gates
the email-retirement ask on the adoption gate in criterion 12
(`prd.md:1471-1474`) being recorded on this item. PR 6 closes the `report`
row half; the ask cannot be made until the gate is written down.

7. **Criterion 23's pack half** needs **item A**. `sd_db.testing` must be
   importable from the pack's test suite and a grep of both suites must
   return nothing outside the package, and the pack cannot import `sd_db`
   until A's installer provisions the virtualenv — A's criterion 13, in A's
   slice 2. **A's plan does not schedule this half today**: no pack test
   file appears in any of A's Touches lists and `sd_db.testing` appears
   nowhere in A's pages, which carry only the installer step itself. A's
   slice 2 has to add the pack-side test and migrate the `launchctl` and
   `tailscale` doubles now in `tests/test_sd_dashboard.py`,
   `tests/test_dashboard_actions.py` and `tests/test_sd_ledger.py`. Until it
   does, this half closes on neither item. `system` had no Python test suite
   on 2026-09-05 (it has several now), so PR 1 asserts the import from the library's own package tests.

8. **Criterion 13's `sd-review` clause** needs **item A**, and only that
   clause: its last assertion is that the next `sd-review` resolution in the
   pack reflects a registry change, which no `system` pull request can
   exercise. The other two things an earlier draft filed here — the Skills
   screen's promote and demote writes, and the Providers-and-bills screen's
   enable, disable, reorder and cap writes — are `local-project-dashboard/`
   work inside this item, and calling them a hand-off did not schedule them.
   They are PR 12. **A's plan does not schedule the clause today**: A's PR 6
   is the provider-registry reader, its Verification names A's own criteria
   2, 3, 6, 10, 11 and 32, and the only "reads the row" sentence in it is
   about `status_source` for item status. Criterion 13 needs `sd-review` to
   resolve `provider` and `bill` **from rows** while the registry file's
   hash is unchanged (`prd.md:1482-1485`), which A's reader is not written
   to do. Until A adds it, this clause closes on neither item.

10. **`sd shadow sync` is built by item A's PR 8**, and this item's PR 3
    lands only the `watermark` state kind it resumes from and the cron entry
    that invokes it. A's PR 8 already lists the command in its Touches as
    new work and says "it is built here", so the destination accepts it —
    the one hand-off of the ten that needed no note added. PR 3's verify and
    the shadow migration cannot run end to end until A's PR 8 merges.

11. **Criterion 7's clause 7.2 inverts a lint rule**, and needs **item A**.
    `prd.md:1263-1265` requires `sd-docs-lint` to **fail on** a `status:`
    line in any `prd.md` under `docs/work/` outside the archive, asserted
    with one seeded. The lint today fails on that line's **absence** —
    `bin/sd-docs-lint:121-123`, rule 1's `if status not in ITEM_STATUSES`.
    The sign has to invert in the same commit that removes the lines, which
    is item A's PR 7: its Touches already name `bin/sd-docs-lint` for a
    different obligation, deriving an item's status from its row, and its
    Verification named A's criteria 13 and 21 and not this. No pull request
    in this item can reach the file — PR 3, which the closure row credited,
    is `system`-only. **Scheduled in A's PR 7 on 2026-09-05**, in the same
    commit that removes the lines, since the window between two merges is
    what the ordering exists to avoid. A's round six then showed both signs
    must hold at once — `item_directories` returns archived items too — so it
    is a signature change and not an inversion; A records that as its C-155.

    **Rule 2 goes silent at the same commit, and this is the larger half.**
    `check_ready` reads the item's status from the same frontmatter and
    returns early on `if status not in WORKABLE_STATUSES: continue`
    (`bin/sd-docs-lint:141-143`, with `WORKABLE_STATUSES = ("ready",
    "in_progress")` at `:53`). Remove the `status:` line and rule 2 matches
    nothing, for every active item in every registered repository. Run on
    2026-09-05 against a fixture holding one `in_progress` item with no
    acceptance-criteria heading, an open `BLOCKING:` line and no `branch:`
    field: with the line, three failures — criteria stated, no open blocking
    line, branch recorded; without it, `rule 2 failures: []`. Rule 1's sign
    says nothing about this, and `rule 2`, `check_ready` and
    `WORKABLE_STATUSES` appear in neither this item's pages nor A's. So
    criterion 7 gains a clause requiring rule 2 to read the status the same
    way the rest of the pack does — from the row where `status_source` is `row`, from the
    line where it is `file` — asserted with one `in_progress` fixture that
    fails all three of its checks in both modes. `check_ready` joins
    `item_directories` and `check_shape` in A's PR 7 Touches. A's PR 7 also gained the tracked marker
    `.status-source` under the retiring repository's `docs/work/` that clause 7.6 reads, which `sd-ai-command-pack/docs/work/2026-09-05-the-pack-runs-a-team-process-for-one-person/prd.md:577-588`
    required and A's `implement.md` named nowhere.

12. **Clause 15.25's `shipped_at` on a merge** needs **item A**. Three
    writers set the field (`prd.md:923-925`); `sd-ship` is A's, and clause
    7.21 asserts the same write from criterion 7's side. `shipped_at` and
    `Closes:` appeared in **no** file of item A before this round — A's
    PR 6 and PR 7 both list `skills/sd-ship/` and neither named either
    string. **Scheduled in A's PR 7 on 2026-09-05**, which owns the merge
    path through `sd-ship --deliver`. The four-numbers arithmetic is PR 8's
    and closes here.

13. **Clause 15.7's runner grep** belongs to **item D**. `prd.md:1528-1529`
    greps "the runner and `sd-review`" for a cost insert and expects
    nothing. `local-sd-runner/` did not exist on disk on 2026-09-05 (it landed with #227, 2026-09-10), and item D lands
    after every pull request here, so the grep would have passed at PR 8 for the
    wrong reason — an empty answer from an absent directory. The
    `sd-review` half stays with PR 8, which routes that file's provider
    calls through the library. **Scheduled in D's PR 7 on 2026-09-05**, the
    pull request that writes the `exec` row's cost from this item's library
    function, so the grep and the rule it protects sit together.

14. **Open question 1's `github` meter job** is scheduled by nothing.
    `prd.md:1709-1710` commits to "a nightly job writes the personal rows as
    `meter` cost rows on a `github` bill". PR 9 enumerates the jobs this
    item adds as exactly two, `sd shadow sync` and `sd-db-backup`. This is a
    third, and clause 15.27 groups cost rows by bill, so the `github` bill
    has no row source without it. The writer lands with PR 8's meter work
    and its `personal.cron` entry with PR 9's, both named here so the
    enumeration in PR 9's body stays the authority on the count.

15. **Clauses 15.13 and 15.16's `start`-session row** belongs to **item
    D**. Settled with open question 8 on 2026-09-05: the charge is the
    total the session reports at its exit, written by whatever started it,
    which is D's runner (`D/prd.md:524` — `claude -p`, `codex exec`, or the
    entry's `start` line). Scheduled in D's PR 7, which already writes the
    `exec` row's cost from this item's library function, so the two costs
    sit in one pull request. PR 8 keeps the schema and asserts the row sums
    like any other.

9. **Criterion 5's manifest tail** needs **item C**, and not merely to read
   a file. `writing-pack/sd-plugin.json` is on disk, so PR 3's test can
   read it — but C's own PR 3 **rewrites** it, extending `blog-idea`'s
   ladder from four targets to seven with `researching`, `review` and
   `ready`. B's mapping table must already carry all seven, or criterion 5's
   tail becomes false the moment C's PR 3 merges. An earlier draft recorded
   the file's existence and not the change coming to it.

## Closing the item

When PR 12 merges — the last of the twelve, not PR 9, which an earlier
draft named because PR 10, PR 11 and PR 12 did not exist yet —
`machine-setup.sh status` reports no drift on the database, both agents, the
serve route and the cron profile, **and** the eight cross-item hand-offs
that gate closure — 3 through 10 — are each closed on their own item,
the item's row goes
to `done`; status lives in the database here, and `docs/work/.status-source` says `row`. No `prd.md` under `docs/work/` carries a `branch:` or `status:` field
(checked 2026-09-23; this said "the two under `docs/work/` have `title`, `status` and `created`"), so unlike
an item in `sd-ai-command-pack` there is nothing to drop alongside it.

## Slice 4, PR 10 — requirement 7's notes, which no pull request carried

**Repository:** `system` **and the pack**. **Touches:** `local-sd-db/`,
`local-project-dashboard/`, and in `sd-ai-command-pack`, `bin/sd`'s new
`note` verb group — `sd note resolve <id>` and `sd note list <item>`, both
named by criterion 16. `system` lands first.

The library's note writes by kind; `sd note resolve <id>`, which sets
`resolved_at` and nothing else; `sd note list <item>`; the item screen's
resolve button on every open note; Today's open-followup list; and the
eight-kilobyte brief builder — the open `followup` and `question` notes of
the item whose branch is checked out, or of every not-`done` item in the
repository when no branch matches, newest first, cut at eight kilobytes
with the count of what was cut and the command that lists the rest
(`prd.md:1060-1071`).

**This is a whole requirement's surface, and the closure table had it on a
pull request whose body disclaims it.** Round one's rewrite assigned
criterion 16 to the cost pull request, whose body is `cost` rows,
reservations, bills, exposure, the four numbers and the burn chart, and
which says in its own verification that criterion 16 is not closed there.
The words *note*, *followup*, *resolve* and *brief* appear nowhere in it.
The Item screen pull request builds that screen read-only, with no write
control on the page, so the resolve button could not have landed there
either. C-92's mechanism, one criterion over: a closure row absorbing work
no pull request describes.

**Verification.** Criterion 16's library and dashboard clauses — **not
criterion 16 whole**, which an earlier draft claimed against hand-off 5 and
its own closure row, both of which say the injection is item A's
`SessionStart` hook (`prd.md:1597-1606`): three followups
written, Today listing three, one resolved and Today listing two with the
item screen showing all three and the resolved one marked; the bound test
asserting a `done` item's notes are absent and that no decision, proposal,
comment or `exec` note is in the brief.

**Landed, `system` half, as #277 (2026-09-12).** Most of the surface was
already on `main` before this pull request and had no row saying so: the
note write by kind (`local-sd-db/sd_db/writes.py:354`), `resolve_note`
setting `resolved_at` and nothing else (`local-sd-db/sd_db/writes.py:381`),
Today's open-followup list (`local-sd-db/sd_db/reads.py:195`, rendered at
`local-project-dashboard/sd_dashboard/screens.py:163`), the item screen's
history (`local-sd-db/sd_db/reads.py:817`) and its per-note resolve route
(`local-project-dashboard/sd_dashboard/server.py:269`) — all from #227. What
this pull request adds is the one piece no pull request had built: the
brief. `reads.brief_items` and `reads.brief_notes`
(`local-sd-db/sd_db/reads.py:212` and `local-sd-db/sd_db/reads.py:240`) are
the two queries — the branch's live items or every live item in the
repository, then the open `followup` and `question` rows on them newest
first — and `sd_db/brief.py` is the renderer: `note_brief(connection, repo,
*, branch=None, limit=8192)` (`local-sd-db/sd_db/brief.py:133`) returns a
`Brief` whose `text` is within the bound, whole notes only, with the count
cut and one `sd note list <item>` per item that lost a note; `branch=None`
reads the checkout with `git symbolic-ref` and a detached HEAD is the
repository-wide case. The bound tests are `local-sd-db/tests/test_brief.py`
— criterion 16's three-written, one-resolved, two-remaining clause against
the brief, Today and the item history, and the eight-kilobyte clause with a
`done` item's open notes absent and no decision, proposal, comment or `exec`
note in the text. One dashboard line moved with it: the resolve button's
CLI equivalent said `sd task resolve`, a verb the pack does not have; it now
says `sd note resolve <id>`
(`local-project-dashboard/sd_dashboard/controls.py:135`), which is the verb
requirement 7 names and the one `bin/sd-note resolve` already answers to.
**What the pack half still owes**: `sd note list <item>` (the trailer names
it and nothing runs it yet — `reads.item_notes` is the query it will
print), and pointing `bin/sd-handoff-restore` at `note_brief` in place of
its own `open_followups` query and `render`, which is the second reader
this brief exists to retire. The injection stays item A's hook.

## Slice 4, PR 11 — `local-herdr/`, landed as #279

**Repository:** `system`. **Touches:** `local-herdr/` (new).

The wrapper around `herdr` with one named persistent session. On start it
resumes each pane's last agent session — `claude --resume <id>` for Claude
Code, the equivalent for Codex — read from a small state file the wrapper
maintains as panes start (`prd.md:1078-1084`).

**Greenfield, and scheduled by nothing before this.** `local-herdr/` was not
on disk when this was planned; `git grep` found the name only in this item's `prd.md`. The
closure table gave criterion 17 to "PR 7's code", and PR 7's Touches names
`local-project-dashboard/` and `local-sd-db/` — neither could hold a
terminal-multiplexer wrapper. The by-hand note below discusses the
*assertion* only, so a reader checking the table saw a covered criterion
and no missing build.

**Verification.** By hand, as the note below says, since the assertion needs
a terminal: panes resumed from their rows after a restart.

**Landed as #279 (2026-09-12), thinner than planned, because `herdr 0.9.0`
carries the resume itself.** The binary on this machine was read before a
line was written: its installed integrations (`herdr integration status`:
claude v9, codex v8) report each pane's agent session id through
`herdr pane report-agent-session`, `herdr agent list` and `herdr pane get`
answer with it as `agent_session.value` beside `cwd` and `pane_id`, and
`[session] resume_agents_on_restore = true` — the default — restarts
supported agent panes with `claude --resume <id>` and `codex resume <id>`
after a client attaches to a restarted server. So requirement 8's second
branch (drop the wrapper, file upstream) does not apply, and neither does
the first as written: nothing has to teach a pane to report its id. What
`local-herdr/herdr.sh` adds is one named session (`sd`, `HERDR_SESSION`
overrides, exported on every call with the session's socket from
`herdr session list --json`, `local-herdr/herdr_wrap.py:149`) and a record
this repository owns — `~/.local/state/sd-herdr/<session>.json`, one entry
per pane with `pane`, `agent`, `session_id`, `cwd`, `recorded_at` and the
live agent `name`, filled by `snapshot` from `herdr agent list`
(`local-herdr/herdr_wrap.py:269`) or by `record` from a pane
(`local-herdr/herdr_wrap.py:227`) — and a `resume` from that record for
what herdr's own restore does not cover: a session stopped and deleted, a
pane closed by hand, an agent that had exited. Per pane it asks `pane get`
and decides `live`, `busy`, resume in place after a `cd`, or `workspace
create --cwd` and resume there, moving the record to the new pane id
(`local-herdr/herdr_wrap.py:293`); `start` runs it when the session is
already running and only attaches when it is not, since herdr resumes as
the client attaches and a second pass would start every agent twice
(`local-herdr/herdr_wrap.py:365`). `status` is convention 6 and declares
itself to `local-health-check` (`local-herdr/herdr.sh:36`): 3 with no herdr
or no state file, 1 when the session or a recorded pane is gone or holds
another session, 0 otherwise (`local-herdr/herdr_wrap.py:387`). Twenty-eight
tests in `local-herdr/tests/test_herdr.py` run the entrypoint against
`local-herdr/tests/doubles/herdr`, a POSIX-sh stand-in that journals argv
and answers from fixture files — the installed binary is never called, and
one test asserts the double is the `herdr` PATH resolves — wired as
`run_suite herdr`, in `tests/ci-native.sh`. Against the
real binary, read-only, `snapshot` of the live `default` session recorded
four Claude agents with their ids and `status` answered 0. Criterion 17
stays by hand: the steps are `local-herdr/README.md:133`, and the state
file's shape, which requirement 8 said an upstream request would carry, is
`local-herdr/README.md:83`; no request is filed, since none is owed.

## Slice 4, PR 12 — the two write surfaces a hand-off number did not schedule

**Repository:** `system` **and the pack**. **Touches:**
`local-project-dashboard/`, `local-sd-db/`, and in `sd-ai-command-pack`,
`bin/sd`'s `skill` and `providers` verb groups. Both halves are on `main`;
what is left is sliced below.

**What landed, and under which number.** This section as first written said
the Skills screen "has no writes before this pull request", that `sd skill
review <name>` was "unscheduled", and that the Providers-and-bills enable,
disable and reorder writes were still to build. All three had landed by
2026-09-10, and the 2026-09-12 Log line below still lists `sd skill review`
under "Not landed" because it was measured against the pack at `cddd3b98`,
two days after the verb reached `main`. Re-measured 2026-09-17 at `75086383`
here and at the pack's `ef7c0c7b`:

- **Skills writes and the review queue** — system #227 (`89dcd86`,
  2026-09-10, "Complete shared workflow runtime closeout", under sd:40).
  `source:local-sd-db/sd_db/skills_catalog.py::request` queues intent only:
  a `task` item for promote ("Promote <name>", brief naming `sd-ship`, one
  `author` assignment under scope `skill-apply`) and for demote, a
  `skill-review` item with one `reviewer` assignment under `skill-review`
  for review; its docstring is "No branch, skill or installed surface is
  changed here". `source:local-sd-db/sd_db/skills_catalog.py::record_review_proposals`
  writes a reviewer's result as `proposal` notes and
  `source:local-sd-db/sd_db/skills_catalog.py::apply_proposals` turns the
  accepted ones into one `skill-apply` task. The dashboard routes
  `/api/skills/(sd-[a-z0-9-]+)/(try|review|promote|demote)` and
  `/api/skill-reviews/([1-9][0-9]*)/apply`, in
  `local-project-dashboard/sd_dashboard/server.py`; the Skills screen
  carries the review, try, promote and demote forms and the item page's
  `source:local-project-dashboard/sd_dashboard/skills_screen.py::review_controls`
  ("Accept and apply selected"). The runner half: the reviewer's structured
  answer lands at `.git/sd-skill-review.json` and is recorded through
  `record_review_proposals`, in `local-sd-runner/sd_runner/runtime.py`, and
  the two scopes are bound to the seed commit in
  `local-sd-runner/sd_runner/gitops.py`. The pull request a promote ends in
  is opened by the runner's session from that brief; no library function
  opens one, and the dashboard's own code has no `gh` and only read-only
  `git`. That is the reading of "through the library" the pack accepted as
  its criterion 27 in #802 ("the library queues the task, never the
  dashboard"); the owner may read it otherwise.
- **Providers-and-bills enable, disable and reorder** — #227, then #297
  (`abc6195`, 2026-09-12, the registry resolved beside the connection's
  database) and #322 (`fd07ea9`, 2026-09-13, `who` with no default).
  `source:local-sd-db/sd_db/provider_controls.py::snapshot` carries the
  file's `configuration_sha256` and a `revision` over the whole state;
  `source:local-sd-db/sd_db/provider_controls.py::configure` validates both
  role lists and every enabled flag, then writes `UPDATE provider` rows
  after `registry.seed`, and never the file. The route is
  `/api/providers/configure` and the Operations form is
  `source:local-project-dashboard/sd_dashboard/operations_screen.py::_provider_controls`.
  Asserted by `test_provider_replacement_is_atomic_and_guarded`, in
  `local-sd-db/tests/test_controls.py`
  (file text unchanged, stale revision refused) and by the dashboard's
  `test_provider_atomic_valid_alternate_and_stale_refusal`, in
  `local-project-dashboard/tests/test_controls_actions.py` (400, 200, 409).
- **The pack half** — `sd skill promote|demote` first as a direct
  `gh api --method POST` opener in pack #775 (`17d80480`, 2026-09-07,
  "PR 8c, criterion 27"), rewritten to the library queue with `sd skill
  review <name>` and `sd skill apply <item> <notes…>` added in pack #802
  (`505431b8`, 2026-09-10, `register_extra` of the pack's `bin/sd_skill.py`);
  every verb but `list`, `try` and `apply` hands to `skills_catalog.request`
  with `who=getpass.getuser()`, the dashboard's route with `who="dashboard"`,
  so both surfaces write the same row and differ in `session` alone.
  `sd providers list|configure --file` over `snapshot` and `configure`
  (enabled and orders only) is the pack's `bin/sd_controls.py`, #802. The
  enable/disable/reorder half of criterion 13's `sd-review` clause is closed
  on the pack side by its
  `test_row_reordering_and_disable_changes_control_the_real_review_chain`;
  the cap half waited on sd:788 slice 3, since the pack's `bin/sd-review`
  called `reviewer_chain` with no `capped_bills`. That slice landed as pack
  #1012 (`22183d3c`, 2026-09-17): `bin/sd-review` now reads `capped_bills`
  from the rows and hands it to `reviewer_chain` and `pick`. What stays open
  is named in the cap-routing bullet below.
- **The cap write's library half, slice 12a** — this item, 2026-09-17, the
  Log line of that date: `source:local-sd-db/sd_db/provider_controls.py::set_cap`
  and `source:local-sd-db/sd_db/registry.py::refuse_capped_start_entries`,
  with the hash assertion, clause 15.15's refusal and the raised-cap test.

**What the slices landed, and the one that stays open.** Sliced from the
read-only audit of 2026-09-17 (the session's `audit-234-pr12`), against the
same two commits. Of the three slices and one bullet below, the slices
12b, 12c and 12d have landed (system #435, system #432, pack #1016); the
unnumbered `sd-review` cap-routing bullet is sd:788's, and its code landed
as pack #1012 (sd:788 is `done`). One pack-side assertion under it stays
open, named in that bullet:

- **12b, the cap control on the Providers screen** (`system`, dashboard) —
  landed in system #435, 2026-09-17, the Log line of that date: the route
  `/api/bills/<name>/cap` in `source:local-project-dashboard/sd_dashboard/server.py::action_route`
  over `set_cap` with `who="dashboard"`, and the per-bill form with the new
  number typed beside the old one (`prd.md:571-573`) in
  `source:local-project-dashboard/sd_dashboard/operations_screen.py::_bill_caps`,
  with `snapshot`'s `warnings` rendered above it and a clear-only form for
  the bill a warning names. Its tests, in
  `local-project-dashboard/tests/test_controls_actions.py`, post a cap on
  the `url` bill (200 and the row, `null` clears), on a `start` bill (400
  with the reader's sentence and the snapshot's revision unmoved), a stale
  revision (409), an unknown bill and a bad number (400 with the library's
  sentence), and read the Usage area with the form, the current cap, the
  hidden revision and a seeded legacy warning.
- **12h, 2026-09-17: the cost tile and the month card read a bill's cap
  the way `snapshot` does** (`system`, library and dashboard) — #435's
  residue. `reads.cost_by_bill` selected `bill.cap_usd_month` from the row
  and `reads.usage_month` took `cap` from it, while `registry.merge` leaves
  a legacy row cap on a bill a `start` entry is billed to out of the merged
  view and reports it in `warnings`, so the tile and the card printed a cap
  the reader refuses beside the panel that warned about it. Of the two
  shapes offered — a `caps` mapping the reads accept, or an overlay applied
  after the read — the fix is the mapping, because `room` then keeps its
  one formula in `reads` and `usage_month` hands the same `caps` to
  `cost_by_bill`, so the tile and the month cannot disagree by
  construction; `reads` stays a database-only module, a mapping being data
  and not the registry. `source:local-sd-db/sd_db/reads.py::cost_by_bill`
  takes `caps` (bill to cap or None) and overlays it in the statement,
  `json_each` of the mapping joined on the bill's name, so its rows stay
  `sqlite3.Row`s, the module's contract, and the cap is read in the same
  snapshot as the spend; `source:local-sd-db/sd_db/reads.py::usage_month`
  takes the same and passes it through. The registry read lives in the
  module that already prints the month: `source:local-sd-db/sd_db/usage.py::caps`
  is the file beside the database (`registry.beside`) parsed and merged
  (`registry.parse`, `registry.merge`; not `registry.read`, whose
  first-open seeding is a write the verb does not make), None on
  `SdDbError`, `OSError` or `ValueError`, the set `_provider_controls`
  catches (no file, a file that cannot be opened, one that cannot be parsed
  — the row is then the only cap, and the panel is where the registry's
  trouble is reported); `source:local-sd-db/sd_db/usage.py::read` is
  `reads.usage_month` under it and `source:local-sd-db/sd_db/usage.py::bills`
  is `reads.cost_by_bill` under it, each holding one deferred read
  transaction around the registry's rows and the cost rows, so a cap
  committed between the two is in all of the read or in none of it. Every
  caller found goes through them: `usage.report` (`sd-db.sh usage`), the
  dashboard's `usage_screen` (`document`, `/api/usage`, and `usage_panel`,
  the card) and `operations_screen._usage` (the tile, `usage.bills`);
  `_bill_caps` keeps asking the row without `caps`, which is what its clear
  form is for; `test_runner`'s one call reads a bill with no `start` entry
  and is unchanged. The pack has no `sd usage` caller (`git grep` of
  `usage_month`, `cost_by_bill`, `usage.report`: none); when its `usage`
  group lands it calls `usage.report` as `usage.py`'s docstring says, and
  gets the merged caps with it. Tests: four in
  `local-sd-db/tests/test_usage.py`, class `TheMergedCaps`, on the ledger
  fixture whose `open` bill holds the `start` entry `claude` — the row cap
  seeded by the bare `UPDATE` #433's test uses, then `usage.caps` equal to
  `snapshot`'s bills, `usage.read` and `usage.report` with `cap` and `room`
  None for `open` and `10.0`/`5.0` for the `url` bill `capped`, the tile
  rows the same, the row itself still `5.0` without `caps`; the verb's
  text (`cap —`, no room) and its `--json` as `usage.read`'s bytes and not
  `reads.usage_month`'s; `set_cap(..., None)` clearing the warning with
  `usage.read` then equal to the raw read, and a cap raised on `capped`
  printing with its room; and a store whose registry is unlinked, where
  `usage.caps` is None and the row prints; and, from #438's Copilot round,
  a registry that is a directory (`OSError`) or cannot be parsed
  (`RegistryError`) leaving the row as the cap, the registry read seeding
  nothing (a file bill with no row is in the mapping and stays out of the
  table, `iterdump` unchanged), and the caps and the spend one snapshot
  (a cap committed by another connection before the second SELECT is in
  neither the month nor the tile, and in the next read of each). Three in
  `local-project-dashboard/tests/test_usage_screen.py`, class `MergedCaps`,
  over the server with a registry beside the home store: the tile line
  `a: $2.00` without `of $5.00`, the card's `data-number="cap">—` with no
  room while the `url` bill's card carries `$20.00`, `room $16.00` and
  `data-cap="20.00"`, and the reader's sentence on the page; `/api/usage`
  and `sd-db.sh usage --json` the same bytes with `"cap": null` and
  `"room": null` for `a`; the clear and a cap of `30.0` set on `c` printing
  on both. Red first on `a0054d54`: sd-db `FAILED (failures=1, errors=3)`,
  dashboard `FAILED (failures=2)`; the two clear tests are the row path's
  control and were green before the fix; the round's three against
  `e1b7f01`, `FAILED (failures=1, errors=4)`. The round also had
  `local-project-dashboard/RUNTIME.md` and `README.md` say the panel reads
  `sd_db.usage.read`, one sentence each. The verification round found two
  escapes at the same boundary, both measured: `float` of a cap the file
  spells as a word or a list (`parse` does not type one) sat outside the
  `try`, and a section that is a list or a scalar raised `AttributeError`
  out of `.items()` in `source:local-sd-db/sd_db/registry.py::parse` --
  the earlier fixture's `bills: [not, a, mapping]` alone had been refused
  as `no 'providers' section` first. So `parse` refuses a section that is
  not a mapping with its own sentence (`test_a_section_that_is_not_a_mapping`
  in `local-sd-db/tests/test_registry.py`, red first with the three
  `AttributeError`s), and `caps` builds the mapping inside its `try` and
  catches `TypeError` too (`test_a_registry_that_parses_with_a_bad_cap_or_a_list_section_leaves_the_row_as_the_cap`,
  red first with `ValueError`, `TypeError`, `AttributeError`). Eighteen
  mutations, each killed by name and restored from a byte copy. Appended,
  so the ratchet keys above stay put.
- **12c, criterion 19's dashboard half over HTTP** (`system`, tests only)
  — landed in system #432 (`3b6b1ec`, 2026-09-16, the Log line of that
  date), no production change. Before it the library test reviewed one
  skill state, `contrib`, and no test posted `/review`, `/promote`,
  `/demote` or `/skill-reviews/<id>/apply` through the server. Its module,
  `source:local-project-dashboard/tests/test_skill_states.py::SkillStates`,
  is a fixture pack with a skill in each of the three states (`path`,
  `trial`, `contrib` — the catalog's names, not the
  `proposal`/`active`/`retired` the first draft wrote, which are item kinds
  and statuses) and five tests:
  `test_a_review_queues_one_reviewer_assignment_in_every_skill_state`,
  `test_the_item_page_offers_each_recorded_proposal_under_one_apply_form`
  (the proposals through the library's fixture-reviewer shape),
  `test_applying_both_proposals_queues_one_apply_assignment_carrying_exactly_them`,
  `test_promote_and_demote_refuse_the_wrong_state_and_queue_a_task_for_the_right_one`
  and `test_the_skill_arms_of_the_dashboard_call_no_git_or_gh` (the grep
  that `--method`, `git push`, `pr create` and `subprocess` are absent from
  the Skills screen and the skill arms of the server, the way the pack
  asserts it of its gateway). Of this bullet's original list nothing is
  missing: the two added skills are the fixture, and the three-state
  review, the fixture-reviewer proposals, the item page's two choices, the
  apply, the refusals and the grep are the five tests, in that order.
- **12d, `sd skill review` and `sd skill apply` in the pack's test** (pack,
  tests only) — landed in pack #1016 (`ea7067cd`, 2026-09-16), no
  production change. Before it the pack's `tests/test_sd_skill_promotion.py`
  drove promote and demote and sliced `bin/sd` before `register_extra`, so
  `review` and `apply` were outside even its help check. Now, in that same
  module, `test_review_queues_one_reviewer_and_the_dashboard_writes_the_same_row`
  is the row equality: `sd skill review` files one `skill-review` item with
  one `reviewer` assignment of scope `skill-review`, and the same request
  twice and `skills_catalog.request(..., who="dashboard")` on the same
  fixture resolve to that one item id;
  `test_apply_after_a_reviewer_result_queues_one_isolated_change` queues one
  `skill-apply` author assignment carrying exactly the accepted proposals
  after a reviewer result recorded through `record_review_proposals`; and
  `test_help_matches_what_each_verb_really_queues` now measures `review` and
  `apply` against a real run of each. The fixture seeds the library's test
  registry beside its database. Of this bullet's original list nothing is
  missing. Not in #1016, by its own body: the runner's real reviewer path
  (faked, as the library's own test fakes it) and review asserted on a
  `contrib` or `trial` skill beyond the help check's run.
- **`sd-review`'s cap-aware routing** — sd:788 slice 3, not this item.
  Landed as pack #1012 (`22183d3c`, 2026-09-17), with the meter reader in
  pack #1031 (`6e20d4d4`); sd:788 is `done`. The pack's test module
  `tests/test_sd_review_ledger.py` holds
  `test_the_chain_and_the_pick_read_the_rows_through_capped_bills`, which
  asserts that a bill at its cap is passed over. Row 3d's "a skipped bill resolves again" from the pack side is still
  unasserted: no pack test raises the cap and then resolves the bill through
  `sd-review` (checked 2026-09-23 by a grep of the pack's `tests/`). The
  library half is 12a's `test_raising_a_cap_lets_a_refused_bill_reserve_again`.

**A hand-off number is not a schedule.** Round two found criterion 13
over-claimed by PR 7 and filed three clauses under "hand-off 8" — while
saying, in the same sentence, that two of them were "in no pull request
body". Both are `local-project-dashboard/` screens in this repository; PR 5
builds them read-only and says so, and PR 7's body lists the Dependencies
bulk actions, `protect main`, bulk merge, the run dialog and the phone set
and no Skills or Providers write control. Deferring in-repository work to a
cross-item hand-off is the same defect that produced PR 10 and PR 11, one
level further down: the fix named the gap and did not close it. Closing it
took #227 under another item's number, which is the same lesson one more
time: the section that names the work is not the one that lands it.

**Verification.** Criterion 13's promote, demote and registry clauses
(`prd.md:1475-1486`, the bulk actions at `prd.md:862-867`, the screen under
System at `prd.md:565`): promote and demote round-trip in #227 and pack
#802; enable, disable and reorder round-trip in #227; the cap round-trips
in 12a, with the registry file's hash asserted as the snapshot's own
`configuration_sha256` before and after, and the raised-cap test — a bill
the ledger refused at its cap takes the same reservation once the cap is
raised — in 12a's `test_raising_a_cap_lets_a_refused_bill_reserve_again`,
in `local-sd-db/tests/test_controls.py`. **Criterion 15's clause 15.15**
(`prd.md:1549-1550`): the reader's refusal of a `start` entry on a capped
bill is one function now, run by `parse` over the file and by `set_cap`
over the registry as it would read after the write, so the dashboard's
refusal is the reader's sentence and not a copy of it; the refusal is on
the write, and on the read `merge` leaves a row cap it finds on such a bill
out of the merged view and reports it in `warnings`, so a legacy row fails
no read and `set_cap(..., None)` clears it; the HTTP surface that carries
both is 12b. Criterion 19's dashboard
half: the library test covers one skill state and two accepted proposals;
the three states and the HTTP path are 12c (landed, system #432),
`sd skill review <name>` is landed and its test is 12d (landed, pack
#1016), and the runner's pull request in a fixture pack is item D's slice
5 (hand-off 4).

## What closes the criteria

| Criterion | Closed by |
|---|---|
| 1 — the database and its restore | PR 2, both halves: in `system` the schema, the version refusals, the WAL mode, the restore-and-compare, the virtualenv resolution and the six backup-failure paths; in the pack the `restore` verb group, `sd restore reimport` and `sd restore resume`, whose piece-specific clauses item C's PR 7 builds on; `sd-db-backup` loaded, `backup-verify.conf` and `doctor` version in PR 9; the palette fixture entry and the `exec` snapshot in PR 7 with item D; `budget restored` in PR 8 |
| 2 — one `sqlite3.connect` | PR 6, which retires the `sqlite3.connect` in the pack's `dashboard/store.py`, the second caller (the line is cited above, in the PR 6 section); PR 2 establishes the invariant in `system` and every later PR re-asserts it |
| 3 — the registry | PR 2 |
| 4 — `sd today` and Today from one query | PR 4, both halves: the screen in `system` and the `today` verb group in the pack |
| 5 — migrations run twice; retirement refuses | PR 3 import half and the `stage`/manifest clauses; PR 6 retirement half |
| 6 — every registered repository's items have rows | PR 3 |
| 7 — a status change writes the row and touches no file | Twenty-one clauses across five pull requests in two items, enumerated from `prd.md:1262-1328` and not from any one body: PR 2's pack half (the `sd restore reimport` clauses, 7.9-7.12 and 7.14); PR 3 (the import clauses, 7.5 and 7.13-7.15's build half); PR 4 (the item screen listing three ordered `status_change` notes, 7.16); PR 7, both halves (the merge and assignment refusals from every surface, 7.17, 7.19 and 7.20; its pack half is the existing `sd task status <item> <status>`, `sd run --sequential|--parallel` and `sd assignments cancel` / `sd runner cancel`, accepted in place of literal `status` and `assign` groups by the owner's decision (a) of 2026-09-16, so no verb group is added; the names are decided; the system half's fixture tests landed in #431 (`d4b756b`), `source:local-sd-db/tests/test_criterion_7.py::Criterion7` and `source:local-project-dashboard/tests/test_criterion_7.py::Criterion7`, and the pack half's are still owed (`sd task status` refused on an unmerged work item and on a `queued` or `running` row, and `sd assignments cancel` / `sd runner cancel` clearing a `queued` row with no runner, in the pack's `tests/test_sd_work.py`, `tests/test_sd_operations.py` and `tests/test_sd_runner.py`), so the clauses stay open; the page's 7.19 parenthesis carries the open gap recorded 2026-09-17, a `running` row with no `runner_run` that no surface can cancel, `source:local-sd-db/sd_db/runner.py::request_cancel` and `source:local-sd-db/sd_db/runner_controls.py::control` refusing that row and `source:local-sd-db/sd_db/operations.py::cancel_assignment` every `running` one, an owner decision and not designed there); item A's PR 7 (the retire, the `.status-source` marker under the retiring repository's `docs/work/`, `status_source`, `sd_lib.delivered` and the lint's sign, 7.1-7.4 and 7.6-7.8); item A's PR 6 (`sd-ship`'s `Closes:` and `shipped_at`, 7.18 and 7.21) |
| 8 — the four vault process jobs absent | item C (sd:232), from `jobs/` and from `personal.cron`; moved 2026-09-12, `vault-cleanup` and `vault-map` stay |
| 9, 10, 11, 20 — moved to item D | not this item |
| 12 — the dashboard | PR 4 (Today, Backlog, Item), PR 5 (Skills, System, Dependencies, charts, and `sd exec-log` in the pack), PR 7 (writes, palette, `exec`, bulk, `budget_usd`), PR 8 (cost charts: the burn line with its cap rule and projection, and the gauges, landed with slice 8d in `local-project-dashboard/sd_dashboard/charts.py`, the 2026-09-16 line at the end); three clauses by hand, below |
| 13 — writes round-trip | PR 7 (status change, assign, `merge_policy`, and `protect main` with its owner-gated refusal); PR 12, landed as system #227 and pack #802 for the Skills promote and demote writes and the Providers-and-bills enable, disable and reorder writes, slice 12a (2026-09-17) for the cap write with the hash assertion and the raised-cap test, slice 12b (system #435, 2026-09-17) for the cap control on the screen, and slice 12c for the dashboard's no-`git`-or-`gh` grep; the `sd-review` clause's enable/disable/reorder half is closed by the pack's `test_row_reordering_and_disable_changes_control_the_real_review_chain` (#802); its cap half's routing landed as pack #1012 (sd:788 slice 3, `22183d3c`), and the pack-side test that a raised cap resolves a skipped bill again is still owed |
| 14 — reports as rows | PR 6; the email-retirement ask waits on criterion 12's adoption gate |
| 15 — cost rows and the four numbers | PR 8, both halves, for twenty-five of the twenty-eight sub-tests: the screen, the library, the meter's dual write and the release-and-settle sweep in `system` (8a #406, 8b #426, 8c #424, 8d the Usage screen's month, `sd-db.sh usage` and `sd_db.usage` -- the 2026-09-16 lines at the end), `sd usage` as a pack verb over `sd_db.usage.report` and `bin/sd-review`'s routing in the pack. **PR 12** for clause 15.15, the dashboard cap-raise refusal: the reader's refusal over a proposed cap, and the read that leaves a legacy row cap out and reports it, is slice 12a (2026-09-17), the HTTP surface is slice 12b (system #435, 2026-09-17). **Item A's PR 6** for clause 15.25's `shipped_at`-on-merge half, hand-off 12; the four-numbers arithmetic beside it is PR 8's and the import's write is PR 3's. Clause 15.7's runner grep moves to **item D**, hand-off 13, and so do clauses 15.13 and 15.16's `start`-session row, hand-off 15 |
| 16 — followups as rows | PR 10, both halves (the note write API in `system`; `sd note resolve` and `sd note list` in the pack; the eight-kilobyte brief builder, Today's open list and the item screen's per-note resolve button); the injection is item A's `SessionStart` hook |
| 17 — `herdr` resumes each pane from the wrapper's state file | PR 11, landed as #279 (`cdbd4cc`, 2026-09-12); the assertion is by hand, below, and **closed**: the operator accepted it on 2026-09-12 with `herdr 0.9.0` recorded (note 852) |
| 18 — this repository's guide names `docs/work/` and the pack's lint | PR 2's `system` half; **landed** in `6ea2a3c` (2026-09-06): `CLAUDE.md`'s "Planned work lives in `docs/work/`" section names both |
| 19 — a skill review opens a `skill-review` row | **PR 12**, not PR 7: PR 7's body and Verification never name a skill, a review, a proposal or a reviewer. The row, the `proposal` notes and the assignment from two accepted proposals landed in system #227 (`request`, `record_review_proposals`, `apply_proposals`) with the library test on one skill state; `sd skill review <name>` landed in pack #802. The review on each of the three states (`path`, `trial`, `contrib`) and the HTTP path landed as slice 12c (system #432, 2026-09-16, `local-project-dashboard/tests/test_skill_states.py`); the pack's `review` and `apply` tests, with the row equality against the dashboard's `who`, landed as slice 12d (pack #1016, 2026-09-16, `tests/test_sd_skill_promotion.py`). Still owed: the runner's pull request, item D's slice 5, which does not schedule it (hand-off 4) |
| 21 — `dependabot-daily` | PR 6's `dep` rows and its `sd deps` pack half; the enqueue-only cut is item D's slice 5; bulk merge is PR 7 |
| 22 — `personal.agent` names `local.system-tools.sd-runner` | PR 9 |
| 23 — the harness, and retire-after-writer ordering | PR 1 for the package and the `system` half of the grep; its pack half is hand-off 7, closing in item A's slice 2, since the pack cannot import `sd_db` before A's installer lands. Ordering asserted from the log by every later PR |

**The schema since day one.** The PR 2 and PR 3 notes above stop at
migration 002. `sd_db/schema/` in `local-sd-db/` holds eleven migration files, `SCHEMA_VERSION` is
11, and the live `~/.local/share/sd/sd.db` answers `PRAGMA user_version`
11 and lists sixteen tables (checked 2026-09-23). What each later file did:

- 003 adds `item.piece`, `parked_at`, `gate_generation` and
  `ready_digest`, the writing lifecycle on the item.
- 004 adds the `publication_claim` table.
- 005 adds the `runner_run` and `runner_lease` tables, and `scope`,
  `run_count` and `queued_at` on `assignment`.
- 006 adds the `repo_protection` table.
- 007 rebuilds `state` to add the `check` kind.
- 008 keys `shadow` by `(tracker, url)`.
- 009 adds four `item.kind` values for personal to-dos, followups and
  ideas that are not articles.
- 010 renames `repo.merge_policy` to `repo.runner_merge`, with the same
  values. These pages keep the old name where they quote the day-one
  design.
- 011 adds the `judgment` table.

`source:local-sd-db/sd_db/schema.py::TABLES` is the list criterion 1
now names.

**Three rows in that table close on something other than "a test
passes,"** and they carry five non-test records between them: row 12, whose
three by-hand records are all one row; row 14, the email-retirement ask,
which waits on criterion 12's adoption gate; and row 17, by hand. An
earlier draft said three and named three; the three it missed are all inside
criterion 12.

**Criterion 7 is twenty-one sub-tests, and an earlier draft treated it as
one.** The draft read: "Criterion 7 cannot be closed inside this item's own
pull requests. A status change writes the row and touches no file only once
`docs/work` has retired … The test belongs here; the condition that makes it
pass lands there." Singular "the test", and one dependency generalised to
twenty-one clauses. Enumerating from `prd.md:1262-1328` instead of from the
sentence: six clauses depend on the retire (7.1, 7.3, 7.5-7.8); four depend on
item A for other reasons entirely — `sd_lib.delivered` (7.4, `A/prd.md:645`)
and `sd-ship`'s trailer and `shipped_at` (7.18, 7.21); five depend on
`sd restore reimport`, which is **this item's PR 2 pack half** and not PR 3
(7.9-7.12, 7.14); four depend on nothing outside this item at all — the item
screen's note listing (7.16) and the three surface refusals (7.17, 7.19,
7.20), which need PR 7's screens and, since the owner's decision (a) of 2026-09-16, the pack's existing `sd task status`, `sd run` and `sd assignments cancel` / `sd runner cancel` rather than new `status` and `assign` verbs; and
one, the lint's sign (7.2), is a seeded-fixture test blocked by a change
nobody had scheduled, hand-off 11. The closure row above carries the split.

**Criterion 17 is asserted by hand, once, and the criterion says so.** It
reads: after `herdr` restarts, each pane resumes the agent session it held,
"asserted by hand once and recorded on this item with the `herdr` version;
or the wrapper is absent and the upstream request is linked." `herdr 0.8.2`
was on this machine when this was planned, so the second branch does not
apply. The record is written: on 2026-09-12 the operator accepted the
by-hand assertion with `herdr 0.9.0` recorded (note 852). PR 11 landed the wrapper and the small state file it maintains as panes
start — not PR 7, and not a row: `prd.md:1080-1083` says the resume ids are
read from a state file the wrapper keeps, and criterion 17 names no row. A
person then restarts `herdr` and writes the result and the version onto this
item. No
test closes it, and none should pretend to.

**Criterion 18 is a guide edit, not a migration outcome.** It asks that
this item's directory be the first work item here and that "this
repository's guide names `docs/work/` and the pack's lint as the place and
the check." The directory exists. On 2026-09-05 `CLAUDE.md` named neither
`docs/work` nor `sd-docs-lint`, so the edit belonged in the first `system`
pull request, PR 2. It landed there, in `6ea2a3c` (2026-09-06), and
`CLAUDE.md` names both today.

**Criterion 12 carries three records a person must write**, each needing a
person and a line on this item, and none of them closable by a test:

- one write from the iPad over the HTTPS name, performed by hand and
  recorded with the date (`prd.md:1370-1372`);
- the home-screen install and one session on each device, checked by hand
  and recorded (`prd.md:1470-1471`);
- **the adoption gate**: the operator uses it for a week and the database
  holds operator-action rows on five of seven days, recorded with the count
  (`prd.md:1471-1474`; restated by the operator on 2026-09-23, note 3730,
  because the request log the gate first named is written by nothing).
  Criterion 14's email-retirement ask cannot be made until this one is
  written down, so leaving it implicit keeps the mail path on indefinitely
  with no artifact holding the count.

  **What counts.** The row is one `state` row of kind `checkpoint` and key
  `operator-action`, with a JSON body naming the `action` path, the `item`
  and the `channel` (`dashboard`). The dashboard's POST handler writes it
  through `source:local-sd-db/sd_db/reporting.py::observed_action` after an
  action that changed the database, and only then; a page view, a refused
  action and an action that changed nothing write no row. The row does not
  say whether a person or an agent sent the request, which the same
  module's `reporting.metrics` says of its weekly count. No row kind has to
  be added: `checkpoint` is already in `sd_db.writes.STATE_KINDS` and in the
  table's `CHECK`, and the retention prune does not delete `state` rows of
  this kind. `timestamp` is UTC (`+00:00`), so the query counts local
  calendar days with `'localtime'`.

  **The query.** Run it on the machine that serves the dashboard, on the
  last day of the week being counted. The gate passes when `days` is 5 or
  more; record both numbers and the date on this item:

      sqlite3 -readonly ~/.local/share/sd/sd.db "
        SELECT count(DISTINCT date(timestamp, 'localtime')) AS days,
               count(*) AS actions
        FROM state
        WHERE kind = 'checkpoint' AND key = 'operator-action'
          AND date(timestamp, 'localtime')
              BETWEEN date('now', 'localtime', '-6 days')
                  AND date('now', 'localtime');"

  For a week that ended earlier, write the bounds as dates without
  `'localtime'`: `BETWEEN date('2026-09-30', '-6 days') AND '2026-09-30'`
  counts the seven days ending 2026-09-30. Grouping by
  `date(timestamp, 'localtime')` in the same statement gives the count for
  each day. On 2026-09-23 the query answered `1|2`: two actions, both on
  2026-09-17, so the gate is open.

## Log

- **2026-09-12** — Retention settles clean run reports (sd:547). Landed in
  `local-sd-db/sd_db/retention.py` as `settle_clean_reports`, run by `prune`
  beside `expire_exec_outputs` and `compact_heartbeats`; `CLEAN_REPORT_AGE`
  is seven days and the transition is written as `retention`. Tests in
  `local-sd-db/tests/test_retention.py` (`TheCleanReport`): an eight-day
  clean report settles, a six-day one waits, an attention report and a clean
  report with an open followup never settle, and the prune's own report row
  counts what it settled. Appended here rather than inserted, so the
  `path:line` citations this file makes stay where `.citations.tsv` pinned
  them.
- **2026-09-12** — Pack halves audited against the pack's `origin/main` at
  `cddd3b98`, read-only; the row's note of the same day carries the file
  and line for each. **Landed:** PR 2's `restore` verb group
  (`sd restore reimport`, `sd restore resume`; pack `707a8e18`, reworked in
  `#802`), PR 4's `sd today` (`#802`, over `sd_db.reads.today_items`),
  hand-off 3's installer (`#766`, `sd_install.py --provision-library`),
  hand-off 8's `sd-review` reading provider and bill rows through
  `sd_registry` (`#802`), and hand-off 10's `sd shadow sync`. So criteria
  1 and 4 have both halves, and criterion 13's `sd-review` clause and PR 3's
  shadow end-to-end no longer wait on the pack. **Under other names:**
  PR 7's surfaces exist as `sd task status <item> <status>`, `sd run
  --sequential|--parallel` and `sd assignments cancel`, not as literal
  `status` and `assign` groups; whether 7.17, 7.19 and 7.20 close on those
  names was the operator's call, made 2026-09-16: option (a), those are the names; the system half's fixture tests landed 2026-09-17 (#431) and the pack half's are still owed, so the clauses stay open (the Log lines of both dates). **Not landed:** PR 5's
  `sd exec-log` (criterion 12), PR 6's `sd deps` (21), PR 8's `sd usage`
  and `sd-review`'s cost routing (15; the pack's registry module says usage
  accounting "remains separate work"), PR 10's `sd note list` and
  `sd-handoff-restore` reading `note_brief` (16; `add` and `resolve`
  exist), PR 12's `sd skill review` (19), and hand-off 7's doubles
  migration (23). No pack commit since 2026-09-10 names this item. Nothing
  on this side is unblocked by it: the table above has no open box, and
  everything still open is one of those six pack halves.
- **2026-09-16** — PR 8, slice 8a, the reservation ledger as a library
  module and nothing else (sd:234). `local-sd-db/sd_db/ledger.py`:
  `reserve` holds a bound as a `reserved` row after one `BEGIN IMMEDIATE`
  check of the bill's cap and the assignment's `budget_usd`, both read from
  `exposure`, the one sum over `cost` rows; `claim` moves `reserved` to
  `sending` with the month of the attempt and refuses any other state;
  `settle` and `lose` move `sending` to `run` or `bound` once and are no-ops
  after; `release_orphans` deletes a dead owner's `reserved` rows and binds
  its `sending` rows, run before every reservation and exported for
  `sd usage`; `set_budget` accepts `budget_usd` only when the author and
  every resolvable reviewer are `url` entries, naming the `start` entry it
  refuses. Tests in `local-sd-db/tests/test_ledger.py`: the concurrent pair
  with room for one, the bound alone over the room, settling twice, the
  month boundary, the dead owner in both states, `sending` at eight of ten
  against the budget and against the cap with the one-`SUM(` grep, and the
  `start` author. Not here: the two-month `run` row of a settlement that
  falls in a later month than its claim (no column carries the second month
  and this slice changes no schema), the per-entry reservation timeout (the
  registry has no such field), and the runner, dashboard and pack verbs,
  which are slices 8b to 8d. Appended, so the ratchet keys above stay put.
- **2026-09-16** — PR 7's verb names, criterion 7 clauses 7.17, 7.19 and
  7.20, decided by the owner (decision note on this item, option (a)): the
  pack's existing `sd task status <item> <status>`, `sd run
  --sequential|--parallel` and `sd assignments cancel` / `sd runner cancel`
  stand in for the literal `sd status` and `sd assign` groups, and no alias
  group is built. Measured on pack `fa7f870f`: `sd task status` is
  registered in `bin/sd_work.py` and tested in the pack's
  `tests/test_sd_work.py` and `tests/test_delivery_evidence.py`;
  `sd assignments cancel` is registered by `bin/sd_operations.py`, tested
  by `tests/test_sd_operations.py`; `sd runner cancel` and `sd run` are
  registered by `bin/sd_runner.py`, the cancel tested by
  `tests/test_sd_runner.py` and `sd run` by no pack test at all through
  the CLI — it hands to
  `source:local-sd-db/sd_db/runner.py::enqueue`, which is exercised as
  `runner.enqueue(` in `local-sd-db/tests/test_runner.py`. The names are
  decided and nothing else is: the three clauses' fixture tests (the
  five-surface refusal, the running-assignment refusal, the queued cancel)
  exist on neither side and are still owed, so the clauses stay open. The
  three clauses record the decision in place in `prd.md`, and the closure
  row and the 2026-09-12 line above record the call. Slice 8a, the
  reservation ledger, landed as system #406 (`4b240d28`); 8b, 8c and 8d
  remain open. Appended, so the ratchet keys above stay put.
- **2026-09-16** — Slice 8c, the meter's dual write, landed. Clause 15.1's
  `meter` row is written by `source:local-sd-db/sd_db/meter.py::sample`:
  one `cost` row per provider **per window**, `source` `meter`, the
  provider, the bill the registry bills it to (read through the connection,
  which also seeds the rows the foreign keys need), `window_minutes`,
  `used_percent`, and NULL for `usd`, tokens, `assignment`, `pass` and
  `call_id`; an unknown provider, a percentage outside 0..100 or a
  non-positive window is refused as `MeterRefused` before the write.
  `local-agent-meter/agent-meter.py` calls it from `write_rows`, once per
  provider per window that `codexbar` reported, on one write connection in
  one transaction, before the JSONL append `prd.md:911-913` keeps until
  item C decides; every failure -- the import, the open, one refused
  window, the commit -- is an `errors.sd_db` or
  `errors.sd_db.<provider>.<window>` entry on the JSONL line, `sd_db.rows`
  records the count, and the exit stays 0. `--db PATH` and `--no-db` are
  the two new options. `local-cron-jobs/jobs/agent-meter.job` runs the
  script under the pack's `.venv/bin/python`, the interpreter with `sd_db`
  installed, as `RUNTIME_PYTHON_DEFAULT` in
  `local-project-dashboard/dashboard.sh` already does for the dashboard;
  the owner re-installs the job after the merge. Asserted by
  `local-sd-db/tests/test_meter.py` (two windows are two rows at one
  moment; clause 15's cross-check, one `meter` row and two `run` rows
  against two assignments in four hours with each item's cost its own and
  the `meter` row in neither, through
  `source:local-sd-db/sd_db/reads.py::item_assignments`; the refusals write
  nothing) and `local-sd-db/tests/test_agent_meter.py` (four rows on their
  bills beside one JSONL line; a missing database, a missing library and
  `--no-db`; in the library's suite because the workflow's unwired-suite
  guard fails every leg on a folder that grows `tests/` without a
  `run_suite` line, which is the workflow's to add). Not this slice: `sd usage`, the usage screen's gauges
  reading the rows (8d), and `sd-review`'s cost routing (8b). Appended, so
  the ratchet keys above stay put.
- **2026-09-16** — PR 8, slice 8b, the library's `url` calling function
  (sd:234). `local-sd-db/sd_db/calls.py`: `call` is the one road every
  `url` entry's request takes — `reserve` at the bound (the prompt's bytes
  over four as tokens at `price.in`, plus `max_tokens` at `price.out`, per
  million; the ratio is a stated assumption, the pack has no estimator to
  port), `claim`, one POST through urllib with no redirect followed and no
  retry, then `settle` at the usage the body carries or `lose` when the
  body cannot cost the call (timeout, dropped connection, non-JSON, a
  refused redirect, an HTTP error with no usage, 429 among them). It
  writes no `cost` row of its own (clause 15.7, asserted by a grep of the
  module), refuses by name a `start` entry, cleartext off loopback, an
  unset key variable, and — on a capped bill or a budgeted assignment — an
  entry without `price.in`, `price.out` or `max_tokens` (sd:788's
  refusal); a refused reservation is the ledger's `LedgerRefused`, with
  `scope`, `exposure`, `limit` and `bound` on it for the runner's
  `blocked` row and its `budget spent` note. sd:965's R2 and R8–R11 in
  `ledger.py`: `settle` above the bound settles at the actual cost and
  files C-59's `cap overshot` attention report through
  `source:local-sd-db/sd_db/reporting.py::ingest` under `OVERSHOOT_JOB`,
  naming the call, the bound and the actual; `claim`, `settle` and `lose`
  refuse a caller's open transaction as `reserve` does; a bill with no row
  on a fresh store is read once from the registry beside the database
  before `no bill` is said; `release_orphans` releases by row id, so a
  legacy NULL-call-id row is released and reported as `None`; R9 held
  fixed at the base (`_money` catches `OverflowError` since #415) and is
  pinned by a test. sd:235's criterion 4 two-row test is
  `source:local-sd-db/tests/test_calls.py::TheBudgetEndsTheRow` — the
  budgeted row's second call refused with `budget spent` and the amount,
  the row without a budget completing twice. Not here: the runner's
  `url`-author execution path that ends the row `blocked` (the dispatch
  loop still skips every entry without `start`), `sd usage`, the usage
  screen and cost charts (8d), and `bin/sd-review`'s cost routing through
  this function (sd:788 slice 3, the pack's, sensitive and owner-merged).
  Appended, so the ratchet keys above stay put.
- **2026-09-16** — PR 8, slice 8d, runner half: the `url`-author execution
  path (sd:234). `source:local-sd-db/sd_db/runner.py::answer_url` is the
  library's one function for it: it hands the run's prompt to
  `source:local-sd-db/sd_db/calls.py::call` with the assignment as the
  ledger's scope, the run as the pass and `url:<run id>` as the one call id
  per attempt, then writes the ending with `begin_ending` as a `start`
  session's exit code decides it — `run` is `done` with the settled tokens
  and cost in `detail`, `bound` is `blocked` naming the lost response and the
  bound held, and a refused reservation (`LedgerRefused` with scope
  `assignment` or `bill`) is `blocked` with the ledger's exposure line as
  `detail` and the same line filed as an open `followup` on the item, which
  is requirement 6's `budget spent` note with the amount; `CallRefused` is
  raised through, so what `call` refuses by name is refused there alone. In
  `local-sd-runner/sd_runner/runtime.py`, `provider_command` no longer
  passes over an entry without `start` for the author role: it returns the
  library's entry and registry in the provider record, and `Runner.answer`
  is the provider step in place of the supervised process — the same
  `prompt(request, provider)` a `start` session reads on stdin becomes the
  one user message, and the raw response body is written to the clone's
  `.git/sd-provider.log`, which `finish` retains and `release` names on the
  `exec` note; the clone, the retention and the release are the `start`
  path's unchanged. A `url` reviewer is still passed over, since a chat
  completion is not the structured envelope `finish_review` reads. Tests:
  `source:local-sd-db/tests/test_runner.py::UrlAuthor` (settled call ends
  `done` with one `run` cost row and the `exec` note; a budgeted row with an
  earlier pass spent ends `blocked` with the `budget spent: assignment N has
  0.10 of its 1.00 budget left …` note, nothing on the wire, and neither
  `queued` nor `claim` picks it again; a timeout ends `blocked` with the
  ledger row `bound`, read through `cost_by_bill`; an unset key is `call`'s
  refusal with the row untouched) and
  `source:local-sd-runner/tests/test_url_author.py::UrlAuthor` (the first
  `url` author resolves with no argv and a `start` entry first in order
  resolves as before; a run through `Runner.execute` ends `released`/`done`
  with the body in the retained log and the prompt on the wire equal to the
  `start` prompt; a refused budget ends `released`/`blocked` with the
  followup and an empty queue). Review on the PR found two holes, both fixed
  there: the timeout had a one-second floor past the deadline — a spent time
  budget is now refused before the wire with the supervised path's
  `assignment time budget exceeded`, and the remaining budget is the timeout
  as is; and the log was written after `begin_ending`, so a failed write
  left a `done` run with no work product — `answer_url` now takes `retain`,
  called with the body before the ending, the runner's write is it, and a
  failed one ends the row `blocked` on the `OSError`. Three more tests
  across the two classes. Not here: a `url` author that makes more than
  one call, a cancel read mid-call, and the `budget_for_selection` comment in
  `ledger.py` that still says the runner takes the first `start` entry —
  8b's file, one line of drift. Appended, so the ratchet keys above stay put.

- **2026-09-16** — PR 8, slice 8d, the usage half (sd:234). The read is
  `source:local-sd-db/sd_db/reads.py::usage_month`, one function for the
  month: per bill the four numbers -- spent (`run` and `bound` rows of the
  month), estimated (the `bound` share of it), held (every `reserved` and
  `sending` row still open, as `exposure` counts it) and the cap, with the
  room and the cumulative spend by day; the by-bill-provider-role rows with
  calls and tokens; every `bound` row of the month; the latest `meter` row
  per provider and window; the total. Its per-bill numbers are
  `cost_by_bill`'s own rows asked for the month, so Today's cost tile, the
  Usage screen and the verb are one read, and the item screen's
  `item_assignments` sums the same two sources (asserted in
  `local-sd-db/tests/test_usage.py`). A month is a stored timestamp's
  first seven characters, so 23:59 on the last day and 00:00 the next are
  two months. `sd-db.sh usage [--month YYYY-MM] [--json]`
  (`source:local-sd-db/sd_db/jobs/cli.py::command_usage`, over
  `source:local-sd-db/sd_db/usage.py::report`) runs `release_orphans`
  first, in the sweep's own committed transaction, then prints the read;
  `--json` is `usage.json_text`, the bytes `/api/usage` serves. The Usage
  area of Operations gains the month below its cost tile
  (`local-project-dashboard/sd_dashboard/usage_screen.py`): the four numbers
  per bill, a gauge of spend and holds against the cap, the burn line with
  the cap rule and the straight projection naming the day it crosses
  (`burn_svg`) for a capped bill, a zero cap included, a gauge per `meter`
  window (`gauge_svg`; criterion 12's forty and ninety) on a `plan` bill's
  card in place of any burn, the role table (an aggregate, bounded by
  bills, providers and roles) and the `bound` rows through `Listing`, with
  its filter and pager, all server rendered, the month a GET form, no
  script. A start-session `run` row without a total is nothing on the burn
  line, as `cost_by_bill` counts it, and two `meter` samples at one moment
  resolve to the later row (#430's review). Its second round: the month is
  checked (`source:local-sd-db/sd_db/reads.py::month_of`) before the sweep,
  so a refused month writes nothing; the read holds one deferred
  transaction over its SELECTs, so a row committed between two of them is
  in all of the projection or none; and the dashboard's GET connection is
  read-only (`mode=ro`, `query_only`), so the panel and `/api/usage` show
  the read without the verb's sweep, a dead owner's row still held until
  the next reservation or `sd usage` binds it, and the same bytes after.
  The pack's `sd usage` verb is
  a wrapper over `sd_db.usage.report` and stays the pack's; the week
  grouping clause 15 names beside the month, the vendor column (the
  registry's, not the table's) and the operator's correction of a `bound`
  row (the two-month clause, which slice 8a left without a column) are not
  here. Appended, so the ratchet keys above stay put.
- **2026-09-16** — PR 12, slice 12c: criterion 19's dashboard half through
  HTTP, tests only (sd:234). `source:local-project-dashboard/tests/test_skill_states.py::SkillStates`
  is a fixture pack that is a git repository with one
  `Authored-with: codex/openai` commit, a skill in each of the three states —
  `skills/sd-onpath` listed in `paths.json`, `contrib/sd-trial` put on trial
  through `/api/skills/sd-trial/try`, and `contrib/sd-contrib` — and five
  tests: `/api/skills/<name>/review` answers 200 for each state with a
  `skill-review` item and one `reviewer`/`skill-review` assignment, the
  pack's `git status --porcelain` unchanged; a fixture reviewer (the
  assignment marked running for `claude`, two proposals through
  `record_review_proposals`) renders as two `notes` checkboxes under the
  one `Accept and apply selected` form on the item page;
  `/api/skill-reviews/<id>/apply` with both notes answers 200 with one
  `author`/`skill-apply` assignment on a `task` whose body's JSON parses
  back to exactly the two accepted proposals, each carrying its note,
  assignment and provider; `/promote` on the path skill is 400 `choose a
  declared path for a contrib skill`, `/demote` on the trial skill is 400
  `only a skill on a path can be demoted`, both writing nothing, while
  `/promote` with `path_name=build` on the trial skill and `/demote` on the
  path skill each file a `task`; and the criterion 13 grep, narrowed as the
  pack's #775 narrowed it — `--method|git push|pr create|subprocess` absent
  from `skills_screen.py` and from the two route arms of `server.py` cut at
  their `match =` lines, which the test asserts it found. No production code
  changed: every route and library refusal the tests name held at
  `7508638`. Closed for the dashboard half: the three-state review, the
  `proposal` notes through a fixture reviewer, the one assignment holding
  both proposals and nothing else. Still open: the runner opening one pull
  request in a fixture pack (item D's slice 5, the runner half) and
  `sd skill review <name>` writing the same item (slice 12d, in the pack).
  Appended, so the ratchet keys above stay put.

- **2026-09-17** — Criterion 7, the fixture tests, system half. Clauses
  7.17, 7.19 and 7.20 have their library and dashboard tests,
  `source:local-sd-db/tests/test_criterion_7.py::Criterion7` and
  `source:local-project-dashboard/tests/test_criterion_7.py::Criterion7`;
  the pack half — `sd task status <item> <status>` refused on an unmerged
  work item and on a `queued` or `running` row, `sd assignments cancel <id>`
  and `sd runner cancel <id>` clearing a `queued` row with no runner, each
  in the pack's `tests/test_sd_work.py`, `tests/test_sd_operations.py` and
  `tests/test_sd_runner.py` — is still owed, so the three clauses stay open
  and their parentheses in `prd.md` say so. Measured on `75086383`: of the
  five surfaces, four are the dashboard's, and only one route writes a
  status, `/api/items/<id>/status`, the one caller of
  `source:local-sd-db/sd_db/workflow.py::change_status` in the route table
  of `server.py`. The item screen posts it from its `Change status` form;
  the palette's status entry is that form re-listed by `localActions` in
  `dashboard.js` (`commands.yaml` registers no status command); the board
  is `_board` in `screens.py`, cards linking to the item with no drag
  handler in `dashboard.js`; and no section constructs a `BulkAction`. So
  7.17 is one refusal, "work completion requires verified delivery or
  cancellation evidence", proven from the route, from the library, and by
  the two read-only surfaces carrying no write; the grep for a second
  `UPDATE item SET status` is `test_status_history.py`'s and is cited, not
  repeated. 7.19 needed one production hunk, approved by the launching
  session: `change_status` refused a `queued` or `running` row naming the
  row alone, and now appends the way out — "; cancel it with `sd runner
  cancel N`" for `queued`, "; stop it from the runner's control entry,
  `sd runner cancel N`" for `running`; the pack's `sd assignments cancel N`
  is the same cancel. The review round moved that check before the
  same-status no-op, so a write of the current status is refused too (the
  callers are the dashboard route and `sd task status`; the runner moves
  its item through `transition`), the tests loop over every status, and a
  `running` row with no `runner_run` — which `request_cancel`,
  `runner_controls.control` and the item screen all refuse — is told there
  is no supported cancel for it yet rather than sent to one: that recovery
  was a gap for the owner, decided 2026-09-17 (note 2706): `sd runner
  cancel` ends such a row `cancelled` with the note, delivered by sd:991.
  7.20 holds with no runner: `runner.enqueue` leaves the
  row `queued` with no run, the status write is refused, the item screen's
  `sd runner cancel N` control and the `/api/assignments/N/cancel` route
  both end it in two transactions with `subprocess`, `time.sleep` and
  `runner.heartbeat_state` patched to raise, and the item takes a write
  again. Two drifts recorded, no production change, owner decision: the
  clause and `prd.md:241` say the cancelled row is `blocked` with
  `cancelled by operator`; the library's terminal status is `cancelled`,
  guarded in `source:local-sd-db/sd_db/writes.py::update_assignment` and
  accepted by `requeue`, and the note carries the surface's name,
  `cancelled by dashboard` from the item screen. And 7.17's refusal says
  "delivery", not "merge". Appended, so the ratchet keys above stay put.
- **2026-09-17** — PR 12, slice 12a, the bill cap write and the reader's
  refusal (sd:234), with the PR 12 section above rewritten to what #227,
  #297, #322 and pack #775 then #802 landed and to slices 12b-12d for what
  is still owed. `source:local-sd-db/sd_db/provider_controls.py::set_cap`
  raises, lowers or clears one bill's cap as its `bill` row through
  `set_bill_cap`, and refuses before any write a stale revision, an unknown
  bill, a number the ledger would not hold (`_money`, the rule a bound is
  checked by) and a cap on a bill any `start` entry is billed to — the last
  with the reader's own sentence, since
  `source:local-sd-db/sd_db/registry.py::refuse_capped_start_entries` is
  one function now, run by `parse` over the file and by `set_cap` over the
  registry as it would read after the write. The refusal stays on the
  write: on the read, `merge` leaves a row cap it finds on a `start`
  entry's bill out of the merged view (until this, `set_bill_cap` there was
  read back as a cap nothing enforces, the reader-side hole in clause
  15.15) and reports the sentence in `Registry.warnings`, because a merge
  that refused would fail every read for that store and the clearing write
  with it (#433's review); `set_cap(..., None)` judges "unchanged" against
  the row and not the view, so it is the repair. `snapshot` gains a `bills`
  list and `warnings`, so `revision` covers a cap and a cap written or
  cleared since the read is a stale save to `configure` and `set_cap`
  alike; the keys `configure`'s callers read are unchanged. Tests in `local-sd-db/tests/test_controls.py`:
  the round-trip with the file's bytes and its hash equal to the snapshot's
  `configuration_sha256` before and after (criterion 13's hash clause, by
  hash and not by text equality), the `start`-bill refusal equal to
  `parse`'s sentence for the same cap in the file with `total_changes`
  unmoved, the stale, unknown-bill and `nan` refusals, and the raised-cap
  test — the ledger refuses the second reservation with scope `bill`, the
  cap is raised, the same reservation is held; in
  `local-sd-db/tests/test_controls.py` again, a bare `UPDATE` cap on the
  `start` bill read with the warning and cleared by `set_cap(..., None)`;
  in `local-sd-db/tests/test_registry.py`, the same row read by `read` and
  by `merge` without the cap and with the sentence, the file alone with no
  warning. `who` is taken and stored nowhere:
  the `bill` row has no column for it. Not here: the route and the form
  (12b), criterion 19's HTTP tests (12c), the pack's `review`/`apply` tests
  (12d), the dashboard's `set_cap` caller. Appended, so the ratchet keys
  above stay put.
- **2026-09-17** — PR 12, slice 12b, the cap control on the Providers
  screen (sd:234, system #435). One route,
  `/api/bills/<name>/cap` in
  `source:local-project-dashboard/sd_dashboard/server.py::action_route`, in
  the configure route's shape: the bill name from the path against
  `BILL_NAME` (the registry puts no rule on a bill name, so the class is the
  route's own, the services route's), the body exactly `revision` and
  `cap_usd_month` (a number, or `null` to clear), then
  `set_cap(connection, name, cap, expected_revision=revision, who="dashboard")`.
  Nothing new in the `do_POST` mapping: the stale revision is `StaleItem`,
  409; the unknown bill (`WorkflowError`), the number the ledger would not
  hold (`LedgerRefused`) and the `start` bill (`RegistryError`, the
  reader's sentence) are `SdDbError`s it already answers 400 with the
  library's text. The form is
  `source:local-project-dashboard/sd_dashboard/operations_screen.py::_bill_caps`,
  under the Configure providers panel: one compact form per bill with the
  current cap printed, a number field that starts at it (a save with
  nothing typed is the library's "unchanged"; blank clears) and the
  snapshot's revision hidden; a bill a `start` entry is billed to gets a
  sentence and no field, since the library would refuse the cap;
  `snapshot`'s `warnings` render as a list above the forms, and the bill a
  warning names gets a clear-only form posting `null`, the repair `set_cap`'s
  docstring names, reached through `reads.cost_by_bill` because the merged
  view hides that cap. `dashboard.js` gained two lines the configure form's
  handling did not cover: a form field is a string, so `cap_usd_month` is
  sent as a number or `null`, and a saved cap returns to the Usage area
  rather than Jobs. No pack verb sets a cap yet, so the form carries no
  `data-cli`. Tests in `local-project-dashboard/tests/test_controls_actions.py`,
  seven, the first five committed red first: the `url` bill's cap posted, read back through
  `snapshot` and the row with the file's hash unchanged, then cleared with
  `null`; the `start` bill's 400 carrying the reader's sentence with the
  revision and the store unmoved; 409 for a stale revision, 400 for an
  unknown bill, a negative, a string, a boolean and an object, 400 for a
  body with a key missing or added, 404 for a name outside the class; the
  Usage area with one form for the one `url` bill, the current cap, the
  hidden revision and the sentence for each `start` bill; a seeded
  legacy row cap rendered as the warning with the clear-only form, cleared
  through it; the two script lines pinned by text; and, from #435's review,
  a string cap refused by the ledger's sentence with the cap still there --
  the script had sent `Number("abc")`, which is `NaN` and travels as
  `null`, the clear, so now only a blank field becomes `null` and any
  other non-decimal goes as its string for the library to refuse. Not here: the pack verb; the Usage area's month cards and the
  cost tile still print a legacy row cap as the bill's cap, since
  `reads.cost_by_bill` and `reads.usage_month` read the `bill` table and
  not the merged view (measured; the owner's call). Appended, so the
  ratchet keys above stay put.
- **2026-09-17** — PR 12, a doc correction, no code (sd:234). The PR 12
  section still listed slices 12c and 12d under "What stays open" after
  both had landed: 12c in system #432 (`3b6b1ec`, 2026-09-16, the Log line
  above) and 12d in pack #1016 (`ea7067cd`, 2026-09-16, the item's note).
  Five sites in this file said so and now say what landed: the "What
  stays open" intro sentence, which now names the landed three and the
  `sd-review` cap-routing bullet as the one still open; the 12c bullet,
  rewritten in the style of 12a and 12b with the module and its five test
  names measured from #432's diff; the 12d bullet, likewise with the two
  added tests and the widened help check measured from `ea7067cd`; the
  criterion 19 sentence in the slice 12a paragraph, which now marks both
  slices landed; and criterion row 19, whose "Still owed" is now the
  runner's pull request alone, item D's slice 5. Nothing in the code or the
  tests moved. Appended, so the ratchet keys above stay put.
- **2026-09-17** — Criterion 7's page brought to what the code does
  (sd:234), the two drifts and the gap the 2026-09-17 line above recorded
  for the owner, on the page now and awaiting the owner's approval, no
  production change. `prd.md:241` and clause 7.20 said the cancelled row is
  `blocked` with `cancelled by operator`; both now say `cancelled`, the
  library's terminal status guarded in
  `source:local-sd-db/sd_db/writes.py::update_assignment` and accepted by
  `requeue`, with `cancelled by <who>`, the caller's value (`dashboard`
  from the item screen, the login from the pack's verbs, `operator` in the
  fixtures), on the row's `result` from the runner cancel and in an item
  note from `cancel_assignment`, which is what
  `source:local-sd-db/tests/test_criterion_7.py::Criterion7` and
  `source:local-project-dashboard/tests/test_criterion_7.py::Criterion7`
  assert. The same two sentences in requirement 1 (`prd.md:229`) and in
  the PR 7 section above carry the same dated bracket. Clause 7.17 said the refusal names the merge and offers cancel;
  it now quotes the sentence
  `source:local-sd-db/sd_db/workflow.py::change_status` raises, "work
  completion requires verified delivery or cancellation evidence", and
  keeps the intent, that a work item closes on merge or on cancel. The gap
  is named in the 7.19 parenthesis with the three refusals it measured and
  the sentence the status write prints, and the criterion 7 row above
  points at it; the recovery is not designed. Every amended site carries a
  dated parenthesis marked owner approval pending, approved 2026-09-17
  (note 2706; the markers now say so). The planning review
  contract's one pass ran over the diff, a read-only reviewer against the
  code: eleven findings, none blocking, the wording ones folded in
  (library's terminal status, not the schema's, since the DDL enumerates no
  status; the note's `who` is the caller's value, not a surface name;
  where each cancel writes it). The pull request's review round, #437,
  moved four more: the PR 7 section's two sentences now state the current
  behaviour with the old wording in their parenthesis rather than the
  reverse; `prd.md:241` no longer says "in one transaction", the cancel
  being one write and the item's next status write another; 7.17 marks
  the board's drag and a bulk action not applicable, since neither carries
  a status write; and 7.20's clause text names `sd assignments cancel` /
  `sd runner cancel` in place of `sd assign cancel`. The verification
  round moved the last old names out of the active sentences: requirement
  1 and clause 7.17 now ask the refusal from the item screen, `sd task
  status` and the palette's status entry, the two dropped surfaces named
  in the parenthesis; `prd.md:240` names the two cancel verbs in the
  clause itself, the runner board staying since `runner_screen.py` renders
  the cancel form for a `queued` row; and the PR 7 section says "the same
  three". `prd.md` keeps its line count, and this line is appended, so
  the ratchet keys stay put.
- **2026-09-17** — PR 12, slice 12h (sd:234): the cost tile, the month
  card, `/api/usage` and `sd-db.sh usage` read a bill's cap the way
  `snapshot` does, so a legacy row cap on a `start` bill is no cap on any
  of them while the panel warns about it. `reads.cost_by_bill` and
  `reads.usage_month` take a `caps` mapping; `usage.caps` reads it from the
  registry beside the database (None without one, the row then prints)
  and `usage.read` and `usage.bills` are the month and the tile under it,
  one snapshot each; the verb and both dashboard reads go through them.
  `registry.parse` refuses a section that is not a mapping. Twelve tests,
  red first, eighteen mutations killed; the 12h bullet in the PR 12
  section has the measurements. Appended, so the ratchet keys above stay
  put.

- **2026-09-17** — Criterion 7's three amendments (#437) approved by the
  owner, note 2706: the eight approval-pending markers on this page
  and the prd now say so, and the 7.19 recovery gap names its decision
  (`sd runner cancel` ends a `running` row that has no `runner_run`) and
  the row that delivers it, sd:991. No other line changed. Appended, so the
  ratchet keys above stay put.
- **2026-09-23** — Doc correction, no code (sd:1416, from #437's review).
  Criterion row 7 and the "Under other names" sentence of the 2026-09-12
  pack-halves audit line said the three clauses' fixture tests were still owed, while
  the 2026-09-17 Log line above records the system half landed in #431
  (`d4b756b`). Both now say the system half landed and name its two
  `test_criterion_7.py` suites, and that the pack half is still owed, so
  7.17, 7.19 and 7.20 stay open. Appended, so the ratchet keys above stay
  put.

- **2026-09-23** — Criterion 12's adoption gate counts operator-action rows
  (note 3730). The bullet under "Criterion 12 carries three records" names
  the row (`state`, kind `checkpoint`, key `operator-action`), its writer
  and a query a reader can run; no new row kind is needed. Stale present
  tense fixed in place, each site re-read against the tree: `local-herdr/`
  and `local-sd-runner/` exist (#279, #227); `CLAUDE.md` names `docs/work/`
  and `sd-docs-lint` (`6ea2a3c`); `system` has test suites; work items carry
  no `status:` line. A schema note after the closure table covers
  migrations 003 to 011. Status sites: rows 17 and 18 read as closed and
  landed, and the cap-routing sites record pack #1012. Every edit above the
  ratchet keys keeps its line count, so the keys stay put.

- **2026-10-04** — sd:991 delivers the 7.19 recovery (note 2706), on branch
  `runner-cancel-no-run`. `sd runner cancel N` ends a `running` row with no
  unreleased `runner_run` as `cancelled`, with `cancelled by <who>`, in
  `source:local-sd-db/sd_db/runner.py::request_cancel`; and
  `source:local-sd-db/sd_db/runner_controls.py::control` sends such a row to
  that cancel and not to the service. A released attempt counts as no
  attempt, matching the owned test in `workflow.change_status`. The criterion 7
  paragraph above, which says the item screen and both entries refuse this row,
  is history from that branch on. The refusal and the item screen hint now
  name `sd runner cancel N`. Five new 7.19 tests in
  `local-sd-db/tests/test_criterion_7.py` cover the two entries, a row with
  no run and a row whose only run was released. Appended, so the ratchet
  keys above stay put.
