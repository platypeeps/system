# local-mirror-sync

Scheduled rsync mirroring of configured directory and file pairs in archive
mode. Directory destinations become **exact copies** of their sources, so
extra destination files are deleted (`rsync -rlptgo -W --delete`: archive mode without sockets, fifos or devices, whole files), unless the job sets
`MIRROR_SYNC_ADDITIVE` (see the repos list below). File sources are copied by
basename into existing destination directories.

## Usage

```sh
./mirror-sync.sh list   # show configured pairs
./mirror-sync.sh plan   # dry run: what would copy / be DELETED
./mirror-sync.sh sync   # mirror everything (what the cron job runs)
```

Pairs live in `mirrors.conf` (`source|destination`, one per line, `#`
comments). The pair lists are local, gitignored files; copy the committed
`mirrors.conf.example`, `mirrors-repos.conf.example` and
`mirrors-nas.conf.example` and set your own paths. Directory sources mirror their contents. File sources require an
existing destination directory. Run `plan` before the first `sync`.

A pair may carry an optional **third field**: comma-separated rsync exclude
patterns that apply to that pair alone.

```
/Users/example/repos|/Volumes/.../repos|node_modules/,target/,__pycache__/
```

The patterns are written to a file rather than passed on the command line, so
the shell never word-splits or glob-expands one. `--delete-excluded` is always
on, so an excluded path is never copied and is removed from the destination if
an earlier run put it there.

Installed as the `mirror-sync-nightly` job in `local-cron-jobs` (03:30,
listed in the machine's cron profile). A failed or refused pair exits 1,
which triggers the cron failure notification (banner + ntfy push).

## The three lists

| List | Destination | Job | Schedule |
|---|---|---|---|
| `mirrors.conf` | iCloud and `/Volumes/local` | `mirror-sync-nightly` | 03:30 daily |
| `mirrors-repos.conf` | `/Volumes/local` | `repos-mirror` | 04:30 and 16:30 |
| `mirrors-nas.conf` | the NAS, `/Volumes/Offsite` | `sd-db-backup`, after its snapshot | 02:10 daily |

One more writer uses the same disk without this folder: `sd-db-backup-hourly`
(`local-cron-jobs`) snapshots the sd database into
`/Volumes/local/Backup Local/sd-backups` at :50 every hour, keeps a rolling
week, and refuses to write when `/Volumes/local` is not mounted.

`/Volumes/local` is a portable USB disk plugged into this Mac. A copy there
does not survive theft, fire or a dead Mac. `mirrors-repos.conf` replaced
two lists on 2026-09-23: `mirrors-weekly.conf`, and `mirrors-offsite.conf`,
which copied the repo fleet to the NAS over SMB at about 80 minutes per no-op
pass.

**`mirrors-nas.conf` is the off-machine copy.** `sd-db-backup` copies
`/Volumes/local/Backup` to the NAS share's `Backup` folder (the
`--destination` in its job file) once a day, right
after its snapshot and only if the snapshot succeeded. A separate schedule
would not order them: after a night asleep, launchd runs missed jobs
together at wake. That
folder holds the nightly `sd-db-backup` snapshot, in `sd-backups/`, so this
pass is how the snapshot leaves the machine. It runs additive, like
`repos-mirror`: the NAS folder already holds `sd-backups/` history, which
an exact mirror would delete. It keeps no overwrite store: a file the pass
overwrites on the NAS is gone (operator decision, 2026-09-26). The only thing this
folder checks on the NAS is that snapshot (see `offsite-verify.py` below).

**A detached drive fails loudly.** It looks like a missing destination
parent, which the safety rails already refuse, so the pass exits 1 and raises
the failure notification instead of mirroring nothing in silence.

**The repos list is a backup, not an exact mirror.** An exact mirror
copies a destructive edit as faithfully as a useful one: `git reset --hard`
deletes the work, the next pass deletes the copy, and the pass reports
success -- which is the loss sd:1107 records, reproduced with the backup
working perfectly. So `repos-mirror` sets `MIRROR_SYNC_ADDITIVE`, which
drops `--delete` and `--delete-excluded`, and `MIRROR_SYNC_BACKUP_ROOT`, which
moves whatever a pass is about to overwrite into
`Backup Local/replaced/<run>/` first. Neither a deletion nor an overwrite can
take the last copy with it. The cost is a destination that keeps what the
source dropped. The age prune below covers only the replaced store; the
destination itself is never pruned, so remove a tree that no longer belongs
there by hand. `mirrors.conf` keeps its exact semantics; only this list is
additive.

**The replaced store is pruned by age.** With `MIRROR_SYNC_BACKUP_ROOT` set,
each `sync` deletes a run folder under it once the folder is older than
`MIRROR_SYNC_BACKUP_KEEP_DAYS` days (default 14), and logs
`pruned replaced run: <path>`; `plan` logs `would prune` and deletes nothing.
The age comes from the folder name, which is the run's timestamp
(`2026-09-26T043000`), not from its mtime. Only a direct child of the root
with exactly that shape, and naming a real date and time, is a candidate:
another name, an impossible stamp such as `2020-99-99T999999`, a symlink, and
the current run are never touched.

**The backup root must be disjoint from every source and destination.** A
root that is not absolute or resolves to `/` stops the pass before any pair
runs. So does a root that equals a source or destination, lies inside one, or
encloses one; the check covers both sides of every pair. Inside a source, the
pair copies the store into its own mirror. Inside a destination, rsync would
already use it as `--backup-dir`. Enclosing either, the prune would delete one
named like a run as if it were one. Every path is compared as a physical path
(`cd -P`, `pwd -P`): a text prefix is not containment, and
`/a/./data/replaced` or `/a/alias/replaced`, with `alias` a link to `data`,
names a folder inside `/a/data` without starting with it. Links are followed
to the end, not only links to directories: a source `/a/current` that links to
the file `/a/backups/2020-01-01T000000/work.txt` is judged by that file, so a
root `/a/backups` is refused. A chain of links, a relative link, and a dangling
link are followed the same way.

Overlap is then decided by filesystem identity (device and inode, from
`stat`), never by path text. The root lies inside a side when the side's
identity is among the identities of the root and its existing ancestors; it
encloses a side in the reverse case. Text cannot tell two spellings of one
folder apart: a case-folding volume (APFS by default) takes `/Backups` for
`/backups`, and a firmlink gives `/private/var` a second physical spelling
under `/System/Volumes/Data`, which `pwd -P` keeps as typed. A part that does
not exist yet is judged by its deepest existing folder plus its names, folded
to lower case, so the check errs toward refusing on a case-sensitive volume.
A source or destination that resolves to `/` encloses every root. A path
cannot be resolved when it has a `.` or `..` in a part that does not exist yet
(`file/..` included), runs through more than 40 links, such as a link loop, or
has an identity `stat` cannot read. A root, source or destination that cannot be
resolved stops the pass too, and nothing is deleted. The
prune walks the root only when the root as written, trailing slashes aside, is
already its physical path. A root that is a link (`link/` included), passes
through a link, or has a `.` component is skipped with a `not pruning` note;
the backup still runs and the pass still succeeds. Write the physical path.
`KEEP_DAYS` is decimal, so `08` is eight days; a value that is not a whole
number stops the pass before any pair runs.

`stat` and `date` differ between BSD (macOS) and GNU, and each reads the
other's flags as something else: GNU `stat -f` means file-system mode, and
GNU `date -r` reads a reference file. So the pass probes once which flavour
answers, on a fixed input, and then passes only that flavour's flags. With
neither flavour, an identity cannot be read and the pass stops; with no usable
`date`, the prune is skipped with a `not pruning` note and the pass fails.

openrsync -- the rsync this machine has, reporting "rsync version 2.6.9
compatible" -- cannot do delete-with-backup at all, failing with
`mk_backup_dir: File exists`. Additive avoids that path rather than working
around it.

## Proving the database snapshot on the NAS (`offsite-verify.py`)

```sh
# under an interpreter that can import sd_db -- the one sd-db.sh picks first
~/repos/platypeeps/sd-ai-command-pack/.venv/bin/python ./offsite-verify.py
python3 ./offsite-verify.py --root DIR   # against another share or a fixture
```

A file-exists check proves nothing, so this does not run one. It **runs the
restore**: it copies the newest `sd-db-backup` snapshot directory off the share
and calls `sd_db.backup.restore()` -- the whole verb `sd-db.sh restore` runs --
against a throwaway home. That is safe from a cron job because the snapshot is
copied first, so nothing writes to the share; `home` is a temporary directory,
so the restored database, configuration and journals land there and the live
store is never touched; and `control_gate` is a file lock under that same
throwaway home, not a service control.

Running the whole verb matters because its parts do not add up to it. The
database validator alone passes a snapshot whose runner journal holds a
`.partial` file, which restore refuses with "interrupted journal write requires
reconciliation", and no hash of unchanged bytes can notice that: unchanged
bytes are not the same claim as usable recovery evidence. Alongside the full
restore it still compares the validator's per-table counts against
`backup-manifest.json` and re-hashes every companion file the manifest names,
because those say *what* came back, not only that something did. Every
check runs, every failure is printed, and any failure exits 1. An interpreter
that cannot import sd_db is reported as a failure, never as a pass.

Both comparisons run in both directions, because the manifest is held to what
`sd-db-backup` writes. The writer counts every table the snapshot holds, so a
table the restored copy has and the manifest does not count fails, and so
does one the manifest counts and the copy lacks. The writer inventories every
file and directory under the snapshot except the manifest, so a file the
manifest does not name fails, and so does a manifest that does not hash
`sd.db`. A manifest that is valid JSON but not an object, a row count that is
not a nonnegative integer, and an entry that is neither a SHA256 nor `null`
are each a named failure, not a traceback. Finder's `.DS_Store` and `._*`
files are ignored: the writer never records them and restore never reads them.

Containment says the backups are under the root; it cannot say the root left
the machine. A `Backup` linked at local storage resolves to a real directory
and passes every other check, while the copies it certifies would die with the
Mac. A different filesystem does not say it either: an attached USB disk is one
too, and it is lost in the fire, theft or spilled drink this copy exists to
survive. Only the mount table says which machine serves the bytes, so the run
reads it: the root's mount has to be a network filesystem, and its device has
to name the share the job expects, which `--expected-share` holds
(for example `192.0.2.10/Offsite`). Its default comes from
`OFFSITE_VERIFY_EXPECTED_SHARE`, exported or set in this folder's gitignored
`.env` (copy `.env.example`); unset, the run fails and names the variable.
`OFFSITE_VERIFY_ROOT` names the root, the share's `Backup` folder; like the
expected share it is a local value with no built-in default, and unset, the run fails and names it.
`--root` overrides it.
The device is split into its server and share and both are
compared whole: `//user@192.0.2.100/Offsite` contains the expected
address and `Offsite-old` starts with the expected share, and each of
them is a different disk. A second NAS mounted at the same path is refused for the same
reason a local disk is: it is not where the backups were written.
`--allow-local-root` turns the whole preflight off for the test fixture, which
is a local directory by construction; the cron job never passes it.

`--preflight-only` runs those root checks and stops, and `sd-db-backup` calls
it before it copies anything to the NAS, on the default root. A snapshot written onto local storage has
already put the backup where the fire takes it, and finding that out at the
next verify run is a night late. One preflight answers for the writer and the
verifier, so they cannot drift into disagreeing about what off-machine means.

The writer also names the directory it is about to write into, with
`--destination`: `Backup` for the NAS copy. The root being the share says
nothing about where that points, and a link there sends the night's writes to
local storage while the root check passes and every later check follows the
same link and agrees. A directory that is not there yet is judged by the nearest parent
that is, which is what the writer will create it in.

The snapshot is asked the same question before any of it is read: a
`sd-backups` directory, a dated snapshot or an `sd.db` linked back at this
machine restores, validates and hashes exactly like a real backup, and losing
the Mac would destroy the snapshots the run certified as being off it. The
manifest's companion names are contained inside the snapshot for the same
reason: the manifest is only as trustworthy as the snapshot holding it, and an
absolute name or a `..` would have the check hash a file elsewhere and report
it as present in the backup.

`sd-db-backup` names a snapshot for its day, with an unpadded `.N` suffix when
a day has more than one. The newest is therefore chosen by date and then by
that suffix **as a number**: sorted as text, `2026-09-21.9` would win over
`2026-09-21.10` and the verifier would restore the ninth run of the day while
calling it the newest.

## Evicted iCloud files and files that vanish mid-pass

An iCloud Drive destination evicts files it keeps only in the cloud. openrsync
reads a file it replaces, and on an evicted one that read fails with
"Resource deadlock avoided"; the pair then aborts at that file. `-W` does not
prevent it (sd:1947). On that error `sync` lists, by dry run, the files the
pair is about to update, asks `brctl download` for the evicted ones among
them and waits for them. The wait is bounded per pair by
`MIRROR_SYNC_MATERIALIZE_WAIT` seconds in total (default 300). It logs each
downloaded file and runs the pair once more. Nothing is deleted. A file still
evicted after the wait fails the pair by name.

A live source loses files while a pass reads it: a cron attempt file, a git
ref that a fetch prunes. GNU rsync exits 24 for that; openrsync exits 23 and
prints `<path>: open (2) in <cwd>: No such file or directory`. A pair whose
only errors are those lines counts as mirrored, with a note on stderr. Any
other error line still fails it.

## Known limits

These overlap and prune checks do not cover the cases below. Each line names
what a fix would need.

- A second path into a source or destination BELOW the root (a bind mount or
  a directory hard link) is invisible to the ancestor chain; a fix needs a
  walk of the root's subtree comparing identities.
- A part that does not exist yet is compared by text, folded to lower case in
  ASCII only; non-ASCII case and NFC/NFD forms that one volume treats as one
  name can differ. A fix needs Unicode case folding and normalization.
- A path can change between the check and rsync or the prune (a link swapped
  in). A fix needs deletes relative to a held directory descriptor.

## The source is never written to

rsync copies `source/ -> destination/` and `--delete` / `--delete-excluded`
act on the **destination only**. The sole rsync flags that would ever modify
a source are `--remove-source-files` / `--delete-source-files`, and
`mirror-sync.sh` refuses to run if either ever appears in `RSYNC_FLAGS`.

Verified empirically, not just inferred: a controlled pair (source with two
files, destination holding an extra file) was synced and the source came back
byte-, mtime- and size-identical while the destination's extra file was
removed.

## Safety rails

Per pair, the source must exist and be non-empty. Directory destination
parents must exist. File destinations must already be directories. Source
and destination paths must differ and must not nest. Nesting is checked twice:
by path text, and by identity (device and inode) in the same way as the
backup root, so a link into the other side does not hide it. A refused pair
counts as a failure and never touches the destination.

Finder metadata (`.DS_Store`, `._*` AppleDouble, `Icon\r`) is excluded
from copying and scrubbed from destinations via --delete-excluded.

`.tmp.drivedownload` is excluded for the same reason. It is Google
Drive's download staging directory: it appears and vanishes as Drive
syncs, and holds transient symlinks into `.shortcut-targets-by-id`
whose numeric names are regenerated each time. Unexcluded it churns
every run — copied when Drive happens to be mid-sync, deleted when not.
Excluded, it is never copied and any existing copy is removed from the
destination; the source's own copy is untouched.
