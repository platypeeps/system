# local-drive-intake

Notice files arriving in the mounted Google Drives, and say what they are.

Both watched Drives are mounted by Google Drive for Desktop and mirrored
nightly to the NAS by `local-mirror-sync`. That mirror is `rsync -a --delete`
and keeps no change log, so a file could land in either Drive, be copied to the
NAS, and never produce a signal anybody saw. In the thirty days before this
module was written, **488 files changed across the two mounts** and nothing
reported one of them.

## Usage

```sh
./drive-intake.sh peek     # say what a fetch would find, change nothing
./drive-intake.sh fetch    # walk the roots, append arrivals, advance state
./drive-intake.sh report   # print undelivered arrivals, grouped by route
./drive-intake.sh stamp    # mark arrivals delivered, --through TIMESTAMP
./drive-intake.sh status   # config, mounts, undelivered count, state freshness
./drive-intake.sh test     # the unittest suite
```

**Type `peek`, not `fetch`, when you only want to look.** `fetch` advances the
state. It no longer loses anything, because the arrivals log is append-only,
but `peek` is the verb that answers "what is new" without touching a thing.

`peek` and `report` print one line per file. A file that arrived more than once
shows its count, `(new, 16 arrivals)`: a Drive-hosted Doc re-syncs without
changing. The arrivals log still keeps every row.

Exit codes follow the repo convention, and `local-cron-jobs` depends on them:
**0** something changed, **3** nothing to do, **1** a real error. A quiet day
must be 3 and not 1 — a nightly job that fails every quiet day trains everyone
to ignore its banner.

## It never writes to a Drive

The rule it was built under is that reading a Drive is allowed and writing one
is not.
This module opens a Drive path exactly once, through `os.scandir`, and never
with a mode that can modify. State and reports land in
`~/.local/share/drive-intake`, outside both mounts.

That is asserted empirically rather than by inspection, because "the source
contains no `open(..., 'w')`" would keep passing while somebody added
`os.utime`. `test_fetch_never_modifies_the_source` snapshots a fixture tree's
paths, sizes, nanosecond mtimes and SHA-256 hashes, runs a baseline fetch, a
reporting fetch, a report and a status, then compares the snapshot entry by
entry.

Confirmed against the real mounts on 2026-09-19: a `find`-based snapshot of all
3,340 files taken before and after a real `fetch` came back identical in path,
size and mtime.

## How it decides what is new

Three attributes per file — relative path, size, modification time — compared
against `~/.local/share/drive-intake/intake-state.csv`. Not a content hash. The
mounts hold 2,892 walked files and the question is "what is new", not "what is
corrupt". A full walk takes **57 ms**.

**The first run is a baseline.** It records the state and reports nothing,
because 2,892 files being new is not news. Every run after that reports.

**A plain deletion is not reported.** Nothing downstream acts on one, and Drive
produces them constantly.

## The arrivals log is append-only

`arrivals.csv` accumulates. A run only ever adds rows, and a consumer marks the
rows it has actually delivered by writing `reported_at`.

The bug this fixes: there used to be one report file that every `fetch`
replaced. Running `fetch` by hand at nine silently ate the arrivals the 06:45
digest would have mailed. Now nothing but a `stamp` consumes anything, and
`report` never stamps.

It also makes "what arrived in August" answerable, which a replaced report
never could. `report --all` reads the whole history.

**`stamp` takes a cutoff, not "everything undelivered".** A `fetch` can append
between the moment the digest reads the log and the moment it stamps. Stamping
the lot would mark rows that were never mailed, and those rows are then
invisible forever. The digest writes the newest `detected_at` it included to a
cutoff file and passes it to `stamp --through`.

## Moves are detected, so a reorganisation does not read as 300 new documents

Google removes and re-creates a file on a move. Reorganising one folder would
otherwise report every file in it as newly arrived and bury the two that
genuinely were.

A vanished file is paired with an appeared one on **basename, size and
modification time**, all three of which a move preserves while a rename or an
edit changes one.

**Hashing was the obvious design and it cannot work here.** By the time a move
is visible the source file is already gone, so there is nothing left to hash.
Storing a hash for every file at every walk would make it exact, at 19 GB of
reading on the first run and permanent bookkeeping afterwards, to solve what a
filename and a timestamp already solve.

**An ambiguous pair is refused rather than guessed.** Measured across the real
corpus, that key collides for a third of all files:

| Key | Files in colliding groups |
|---|---|
| size + mtime | 1,150 of 2,892 (39.8%) |
| basename + size + mtime | 996 (34.4%) |
| size + mtime, over 1 KB only | 933 of 2,293 (40.7%) |

The Archive folder duplicates material that also lives elsewhere, so a
nearest-match rule would invent moves that never happened. Ambiguity is judged
only inside one run's own candidate sets, which are small. A pair that is not
unique on both sides stays a new file. Under-reporting a move costs a line in
the digest; over-reporting one hides a real document.

**Above 40 rows in one route the report names folders instead of files**, so a
reorganisation that could not be paired unambiguously still arrives readable.

**A missing mount is an error, not an empty walk.** Google Drive for Desktop
being signed out looks exactly like every file having been deleted. Reporting
that as "nothing new" would be a silent failure on the one day it matters.

## Routes

Four, from the plan it was built for. Rules live in the conf, first match
wins, and anything no rule matches is noise — which is why there is no
catch-all row at the bottom. A new kind of file shows up as a number in the
noise count before anybody writes a rule for it.

| Route | What it is for | Share of the corpus |
|---|---|---|
| `data-export` | Machine-readable feeds. Telemetry exports, spreadsheets, CSV. An export importer's inbox. | 268 (9.3%) |
| `document` | Anything else worth opening. PDFs dominate at 1,912 of 2,892. | 1,739 (60.1%) |
| `correspondence` | Governance paper that carries a date somebody must act on: minutes, agendas, letters, notices, and everything under Meetings or Communications. | 525 (18.2%) |
| `noise` | Photographs, sync artefacts, and both `Reference (generated)` folders — those are this repo's own publish surface, so a file appearing there is outbound, never inbound. Counted in the digest, never listed. | 360 (12.4%) |

Order in the conf is meaning: the narrow folder rules come before the broad
extension rules, so board minutes route to `correspondence` and not to
`document` on the strength of their `.pdf`.

## Config

The conf is private: each `root` row names a mount, and a Google Drive mount
name carries the account address. It lives outside the checkout, at
`<config>/drive-intake/drive-intake.conf`. `<config>` is
`$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`. Start from
`drive-intake.conf.example`; `DRIVE_INTAKE_CONFIG` names another file.

A rule written `*/meetings/*` matches at the top of a root as well as below it.
`fnmatch` alone would make that pattern mean "at least one folder deep", so
each path is tested in both its bare and leading-slash forms.

## Jev can name what no rule matched, and it is on

A path no rule matches is noise. That is a routing decision and not a failure,
but it is also where a genuinely new kind of file goes to be invisible until
somebody reads the count and writes a rule. Jev can name a route for those
paths instead.

It runs unless one of these stops it:

- `JEV_DRIVE_INTAKE` switches this stage off, or
- Jev cannot answer on this machine — unkeyed, or the switch thrown.

Either one and this module behaves byte for byte as it did before Jev existed.

**Unset means on.** `0`, `off`, `false`, `no` and `disabled` switch this stage
off, in any case; every other value leaves it on, the `1` this used to require
included. It only ever subtracts: `jev enabled` reads the fleet switch and the
key first, so this variable can take the stage out and can never put it back in
on a machine that cannot answer.

This used to be an opt-in, on the reasoning that an integration which has to be
switched off to be safe is the wrong way round. The opposite failure costs
more: a per-caller switch that defaults to off makes every integration added
after it silently never run. Nothing depends on Jev either way — the rules run
first and are unchanged, and every way of missing falls to `noise`.

The rules run first and unchanged. Jev is asked only after they all miss, and
the question is a `choice` over **the route names the loaded conf defines**,
plus an explicit `none-of-these`. The names are read from the rules at runtime
rather than written down in the module, because a hand-kept list of routes is
right on the day it is typed and wrong the first time somebody edits the conf.
The no-match option is there so an unplaceable path has somewhere to go other
than the least wrong route.

Every degradation ends in `noise` — the answer today — and says why on stderr:
Jev switched off, unkeyed, timing out, failing, or naming something that is not
a route here. None of it changes an exit code, and none of it is silent, because
a lane that quietly stops running is the defect this shape exists to avoid.

What `--fallback` prints is **not** `noise`, though `noise` is where a
degradation ends. It is `not-judged`, which `ROUTES` does not contain and
`parse_config` rejects, so no conf can define it. It used to be `noise`: the
route check below is `answer in routes`, and `noise` is a route, so a call
that judged nothing returned the same string as a call that judged
confidently. The marker is read first and mapped to `noise` here, which keeps
the outcome identical and the two cases distinguishable.

**Only the relative path is sent. Never a file's contents.** Every call leaves
the machine, and these two Drives are full of governance paper.

The entrypoint is resolved from this folder — `../local-jev/jev.sh` — and not
from `PATH`. Cron and CI run with a `PATH` that `local-bin-links` never
touched, and a caller that only works from an interactive shell is a caller
that quietly stops running at 06:45. `DRIVE_INTAKE_JEV` overrides the path,
which is how the test suite injects a fake and stays offline.

Unmatched paths are asked in batches of up to eight, one `jev ask` per batch
(sd:1160); a batch of one is the `choice` above. Eight is the most the Haiku
comparison arm takes. The state is the batch's paths under the keys `p1`,
`p2`, ..., and nothing else; the questions file names the keys and the routes.
A batch answer is used whole or not at all. One that misses a path, answers a
key nobody asked, or names something outside the criteria routes every path in
that batch to noise and says so, so no answer is ever read against the wrong
path. A failed batch leaves the other batches alone: each path's route depends
only on its own answer.

A first run against a large tree is still a lot of calls. That is the reason to
reach for `JEV_DRIVE_INTAKE=0` on one, and the reason a declined probe says so
once per run rather than once per path.

**Ledger subject and run (sd:2953).** Each call passes `--subject drive-intake:<16 hex>`: the first 16 hex of the sha256 of its relative paths, sorted, one per line. That is one path for a `choice` and the batch for an `ask`. An outcome recomputes it with `printf '%s\n' PATH... | LC_ALL=C sort | shasum -a 256 | cut -c1-16`. No path leaves in it. Every call of one run shares `JEV_RUN=drive-intake-<UTC yyyymmddThhmmss>-<4 hex>`, or the run's inherited one.

## Config

Pipe-separated, three fields, because a Drive path contains spaces and an `@`
and `read -r name value` in the shell splits it in the wrong place.

```
root |<label>|<path>              a mount to walk, ~ allowed
route|<route>|<glob on rel path>  first match wins, case-insensitive
```

## Never walked

`.DS_Store`, `._*` AppleDouble files, `Icon\r`, any dotfile or dot-directory,
and `.tmp.drivedownload`. The last is Google Drive's download staging
directory: it appears and vanishes as Drive syncs and its contents are
regenerated each time, which `local-mirror-sync` already documents as churn
worth excluding. Walking it would report a dozen new files a day that are gone
before anybody looks.

Those exclusions account for exactly the gap between the 3,340 files `find`
sees and the 2,892 this walks: 448 files, 2,892 + 448 = 3,340.

## Environment

| Variable | Default |
|---|---|
| `DRIVE_INTAKE_CONFIG` | `<config>/drive-intake/drive-intake.conf` |
| `DRIVE_INTAKE_STATE` | `~/.local/share/drive-intake` |
| `DRIVE_INTAKE_STALE_HOURS` | `36` |
| `JEV_DRIVE_INTAKE` | unset means on — `0`/`off`/`false`/`no`/`disabled` stops Jev routing what no rule matched |
| `DRIVE_INTAKE_JEV` | `../local-jev/jev.sh` beside this folder |

## Where it fits

Written for a plan in a separate private repository, section 5.7. It is the
first module built to the fetch-and-retain pattern that plan calls F3; an
export-directory importer, built outside this repo, is the second, consuming
the `data-export` route. A skill there turns a `report` into the morning
digest. Nothing it finds leaves the digest without the owner.

It is the reference instance of the file-intake convention,
`docs/conventions/file-intake.md` at this repository's root. Build the next
intake from that document, not from this folder.

### A quiet day is 3, including when the log does not exist yet

`report` and `stamp` used to exit **1** whenever `arrivals.csv` was absent, printing
"run fetch first". That is right only before the first fetch. After a baseline,
an absent log means nothing has changed since — a quiet day, which this
module's own contract says must be 3.

The state file is what separates the two. With `intake-state.csv` present, a fetch has
run, so an absent arrivals log reports "nothing undelivered" and exits 3. With
neither file, the advice to run a fetch is still correct and still exits 1.

Found 2026-09-19: every `report` on this machine had been failing since the
baseline run, telling the caller to run the command it had just run.
